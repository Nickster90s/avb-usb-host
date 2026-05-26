#!/usr/bin/env python3
#
# Phase 3 / P3.1 — USB UAC2 sink as a reusable subsystem, exposing the
# decoded audio channel stream + the async-feedback input as ports,
# instead of the internal loopback. Emitted as a Verilog module and
# instantiated inside the avb-aes3 LiteX SoC, where the channel stream
# feeds the AAF talker and the feedback is driven by the MCR (recovered
# media clock) rate. See docs/phase3-bridge.md.
#
# Reuses the WORKING USB bring-up recipe (tag usb-hs-uac2-working):
#   - ultraembedded ulpi_wrapper.v as the ULPI link layer
#   - LUNA USBDevice on a UTMI bus, always_fs=False / data_clock=60e6
#   - raw ULPI input pins, wrapper startup reset pulse
#   - requesthandlers with interface.claim
#
# Two modes:
#   standalone=False : I/O from platform.request (on-FPGA build/test).
#   standalone=True  : I/O from module ports + cd_usb from usb_clk port,
#                      no PLL (avb-aes3's CRG supplies the phase-0 60 MHz).
#                      Used for amaranth.back.verilog.convert emission.

import os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockSignal,
                      ResetSignal, ClockDomain, Cat)

from luna.gateware.usb.usb2.device   import USBDevice
from luna.gateware.interface.utmi    import UTMIInterface
from luna.usb2                       import USBIsochronousOutStreamEndpoint, \
                                            USBIsochronousInMemoryEndpoint
from usb_protocol.types              import USBRequestType, USBStandardRequests

from usb_descriptors                 import USBDescriptors
from requesthandlers                 import UAC2RequestHandlers
from usb_stream_to_channels          import USBStreamToChannels


class USBAVBSubsystem(Elaboratable):
    NUMBER_OF_CHANNELS = 8
    AUDIO_BITS         = 24
    MAX_PACKET_SIZE    = 224

    def __init__(self, *, standalone=False):
        self.standalone = standalone

        # --- audio channel stream out (device→fabric, host playback) ---
        self.channel_stream_payload = Signal(self.AUDIO_BITS)
        self.channel_stream_channel = Signal(range(self.NUMBER_OF_CHANNELS))
        self.channel_stream_valid   = Signal()
        self.channel_stream_first   = Signal()
        self.channel_stream_last    = Signal()
        self.channel_stream_ready   = Signal()      # input from consumer
        # --- async feedback in (fabric→device) ---
        self.feedback_value         = Signal(32)    # input, 10.14 samples/uframe

        if standalone:
            # ULPI + clock become module ports.
            self.usb_clk      = Signal()            # input: phase-0 60 MHz
            self.ulpi_dir_i   = Signal()            # inputs from pins
            self.ulpi_nxt_i   = Signal()
            self.ulpi_data_i  = Signal(8)
            self.ulpi_data_o  = Signal(8)           # outputs to pin IOBUFs
            self.ulpi_data_oe = Signal()
            self.ulpi_stp_o   = Signal()
            self.ulpi_rst_o   = Signal()

    def ports(self):
        """Port list for verilog.convert (standalone only)."""
        return [
            self.usb_clk,
            self.ulpi_dir_i, self.ulpi_nxt_i, self.ulpi_data_i,
            self.ulpi_data_o, self.ulpi_data_oe, self.ulpi_stp_o, self.ulpi_rst_o,
            self.channel_stream_payload, self.channel_stream_channel,
            self.channel_stream_valid, self.channel_stream_first,
            self.channel_stream_last, self.channel_stream_ready,
            self.feedback_value,
        ]

    def elaborate(self, platform):
        m = Module()

        if self.standalone:
            # cd_usb fed by the usb_clk input port (avb-aes3 supplies it).
            cd_usb = ClockDomain("usb")
            m.domains += cd_usb
            m.d.comb += cd_usb.clk.eq(self.usb_clk)
            dir_i, nxt_i, data_i = self.ulpi_dir_i, self.ulpi_nxt_i, self.ulpi_data_i
            data_o, data_oe      = self.ulpi_data_o, self.ulpi_data_oe
            stp_o, rst_o         = self.ulpi_stp_o, self.ulpi_rst_o
        else:
            with open(os.path.join(os.path.dirname(__file__),
                      "../rtl/ulpi_ultraembedded/ulpi_wrapper.v")) as f:
                platform.add_file("ulpi_wrapper.v", f.read())
            ulpi_clk = platform.request("ulpi_clock", 0)
            ulpi     = platform.request("ulpi", 0)
            phase = os.environ.get("USB_PHASE", "0")
            if phase == "raw":
                m.submodules.car = platform.clock_domain_generator(
                    ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
            else:
                m.submodules.car = platform.clock_domain_generator(
                    ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(phase))
            dir_i, nxt_i, data_i = ulpi.dir.i, ulpi.nxt.i, ulpi.data.i
            data_o   = Signal(8); m.d.comb += ulpi.data.o.eq(data_o)
            data_oe  = Signal();  m.d.comb += ulpi.data.oe.eq(data_oe)
            stp_o    = ulpi.stp.o
            rst_o    = ulpi.rst.o

        # USB3300 RESET active-high; release. Drive bus when DIR low.
        m.d.comb += [rst_o.eq(0), data_oe.eq(~dir_i)]

        utmi = UTMIInterface()

        # wrapper startup reset pulse (the final piece that made it work)
        wrap_rst    = Signal(reset=1)
        wrap_rstcnt = Signal(7, reset=0)
        with m.If(~wrap_rstcnt.all()):
            m.d.usb += wrap_rstcnt.eq(wrap_rstcnt + 1)
        with m.Else():
            m.d.usb += wrap_rst.eq(0)

        m.submodules.ulpi_wrap = Instance("ulpi_wrapper",
            i_ulpi_clk60_i      = ClockSignal("usb"),
            i_ulpi_rst_i        = wrap_rst,
            i_ulpi_data_out_i   = data_i,
            i_ulpi_dir_i        = dir_i,
            i_ulpi_nxt_i        = nxt_i,
            o_ulpi_data_in_o    = data_o,
            o_ulpi_stp_o        = stp_o,
            i_utmi_data_out_i   = utmi.tx_data,
            i_utmi_txvalid_i    = utmi.tx_valid,
            i_utmi_op_mode_i    = utmi.op_mode,
            i_utmi_xcvrselect_i = utmi.xcvr_select,
            i_utmi_termselect_i = utmi.term_select,
            i_utmi_dppulldown_i = utmi.dp_pulldown,
            i_utmi_dmpulldown_i = utmi.dm_pulldown,
            o_utmi_data_in_o    = utmi.rx_data,
            o_utmi_txready_o    = utmi.tx_ready,
            o_utmi_rxvalid_o    = utmi.rx_valid,
            o_utmi_rxactive_o   = utmi.rx_active,
            o_utmi_rxerror_o    = utmi.rx_error,
            o_utmi_linestate_o  = utmi.line_state,
        )
        m.d.comb += [
            utmi.vbus_valid.eq(1), utmi.session_valid.eq(1),
            utmi.session_end.eq(0), utmi.host_disconnect.eq(0),
            utmi.id_digital.eq(0),
        ]

        m.submodules.usb = usb = USBDevice(bus=utmi)
        usb.always_fs  = False
        usb.data_clock = 60e6

        descriptors     = USBDescriptors(use_ila=False, ila_max_packet_size=512)
        usb_descriptors = descriptors.create_usb2_descriptors(
                              self.NUMBER_OF_CHANNELS, self.MAX_PACKET_SIZE)
        control_ep = usb.add_control_endpoint()
        control_ep.add_standard_request_handlers(usb_descriptors, blacklist=[
            lambda setup: (setup.type    == USBRequestType.STANDARD)
                        & (setup.request == USBStandardRequests.SET_INTERFACE)
        ])
        control_ep.add_request_handler(UAC2RequestHandlers())

        ep1_out = USBIsochronousOutStreamEndpoint(
            endpoint_number=1, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep1_out)
        ep1_in = USBIsochronousInMemoryEndpoint(
            endpoint_number=1, max_packet_size=4)
        usb.add_endpoint(ep1_in)

        m.submodules.out2ch = out2ch = USBStreamToChannels(
            max_no_channels=self.NUMBER_OF_CHANNELS)
        m.d.comb += out2ch.usb_stream_in.stream_eq(ep1_out.stream)

        cs = out2ch.channel_stream_out
        m.d.comb += [
            self.channel_stream_payload.eq(cs.payload[:self.AUDIO_BITS]),
            self.channel_stream_channel.eq(cs.channel_nr),
            self.channel_stream_valid  .eq(cs.valid),
            self.channel_stream_first  .eq(cs.first),
            self.channel_stream_last   .eq(cs.last),
            cs.ready.eq(self.channel_stream_ready),
        ]

        # EP1 IN async feedback ← media-clock rate (self.feedback_value).
        # TODO(P3.4): write feedback_value into ep1_in's memory in the HS
        # 10.14 format. Nominal 48k = 6.0 samples/uframe = 0x0001_8000.
        # Wired live from MCR once integrated in avb-aes3.

        m.d.comb += usb.connect.eq(1)
        return m


# --- Standalone Verilog emission (P3.1) ---------------------------------
# Emits usb_avb_subsystem.v with ULPI bus + usb_clk + audio stream as
# module ports, for Instance() inside the avb-aes3 LiteX SoC. The
# ulpi_wrapper.v sub-module is referenced by name; add it as a source
# alongside in the avb-aes3 build.
if __name__ == "__main__":
    import sys
    from amaranth.back import verilog
    sub = USBAVBSubsystem(standalone=True)
    out = sys.argv[1] if len(sys.argv) > 1 else "usb_avb_subsystem.v"
    with open(out, "w") as f:
        f.write(verilog.convert(sub, name="usb_avb_subsystem", ports=sub.ports()))
    print(f"wrote {out}  (instantiate with ulpi_wrapper.v alongside)")
