#!/usr/bin/env python3
#
# Phase 3 / P3.1 — USB UAC2 sink as a reusable subsystem, exposing the
# decoded audio channel stream + the async-feedback input as ports,
# instead of the internal loopback. Intended to be emitted as a Verilog
# module and instantiated inside the avb-aes3 LiteX SoC, where the
# channel stream feeds the AAF talker and the feedback is driven by the
# MCR (recovered media clock) rate. See docs/phase3-bridge.md.
#
# This reuses the WORKING USB bring-up recipe (tag usb-hs-uac2-working):
#   - ultraembedded ulpi_wrapper.v as the ULPI link layer
#   - LUNA USBDevice on a UTMI bus, always_fs=False / data_clock=60e6
#   - raw ULPI input pins, wrapper startup reset pulse
#   - USB_PHASE=0 clock (PLL) by default
#   - requesthandlers with interface.claim (in requesthandlers.py)
#
# STATUS: scaffold. The streaming-port plumbing + Verilog emission entry
# point are stubbed where marked TODO(P3.1).

import os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockSignal,
                      ResetSignal, ClockDomain)

from luna.gateware.usb.usb2.device   import USBDevice
from luna.gateware.interface.utmi    import UTMIInterface
from luna.usb2                       import (
    USBIsochronousOutStreamEndpoint,
    USBIsochronousInMemoryEndpoint,
    USBIsochronousInStreamEndpoint,
)
from usb_protocol.types              import USBRequestType, USBStandardRequests

from usb_descriptors                 import USBDescriptors
from requesthandlers                 import UAC2RequestHandlers
from usb_stream_to_channels          import USBStreamToChannels
from channels_to_usb_stream          import ChannelsToUSBStream


class USBAVBSubsystem(Elaboratable):
    """USB UAC2 sink exposing the audio channel stream for AVB bridging.

    Ports (for Verilog emission / SoC instantiation):
      OUT (device→fabric, host playback audio):
        channel_stream_payload  : Signal(24)  current channel sample
        channel_stream_channel  : Signal(3)   channel index 0..7
        channel_stream_valid    : Signal()
        channel_stream_first    : Signal()    first channel of a frame
        channel_stream_last     : Signal()    last channel of a frame
        channel_stream_ready    : Signal(), input  (from AAF/FIFO consumer)
      IN (fabric→device, async feedback):
        feedback_value          : Signal(32), input  10.14 fixed samples/uframe
    The ULPI bus + clock domains are still requested from the platform
    inside elaborate (so a thin platform shim is needed for standalone
    Verilog emission — see TODO(P3.1)).
    """

    NUMBER_OF_CHANNELS = 8
    AUDIO_BITS         = 24
    MAX_PACKET_SIZE    = 224

    def __init__(self):
        # Exposed streaming interface (these become module ports).
        self.channel_stream_payload = Signal(self.AUDIO_BITS)
        self.channel_stream_channel = Signal(range(self.NUMBER_OF_CHANNELS))
        self.channel_stream_valid   = Signal()
        self.channel_stream_first   = Signal()
        self.channel_stream_last    = Signal()
        self.channel_stream_ready   = Signal()      # input from consumer
        self.feedback_value         = Signal(32)    # input from MCR

    def elaborate(self, platform):
        m = Module()

        with open(os.path.join(os.path.dirname(__file__),
                  "../rtl/ulpi_ultraembedded/ulpi_wrapper.v")) as f:
            platform.add_file("ulpi_wrapper.v", f.read())

        ulpi_clk = platform.request("ulpi_clock", 0)
        ulpi     = platform.request("ulpi", 0)

        usb_phase_env = os.environ.get("USB_PHASE", "0")
        if usb_phase_env == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(usb_phase_env))

        m.d.comb += ulpi.rst.o.eq(0)

        utmi = UTMIInterface()
        ulpi_data_drive = Signal(8)
        m.d.comb += [
            ulpi.data.o .eq(ulpi_data_drive),
            ulpi.data.oe.eq(~ulpi.dir.i),
        ]

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
            i_ulpi_data_out_i   = ulpi.data.i,
            i_ulpi_dir_i        = ulpi.dir.i,
            i_ulpi_nxt_i        = ulpi.nxt.i,
            o_ulpi_data_in_o    = ulpi_data_drive,
            o_ulpi_stp_o        = ulpi.stp.o,
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

        # Expose the decoded channel stream on the module ports.
        cs = out2ch.channel_stream_out
        m.d.comb += [
            self.channel_stream_payload.eq(cs.payload[:self.AUDIO_BITS]),
            self.channel_stream_channel.eq(cs.channel_nr),
            self.channel_stream_valid  .eq(cs.valid),
            self.channel_stream_first  .eq(cs.first),
            self.channel_stream_last   .eq(cs.last),
            cs.ready.eq(self.channel_stream_ready),
        ]

        # EP1 IN async feedback ← media-clock rate from the SoC (MCR).
        # TODO(P3.1): wire self.feedback_value into ep1_in's memory in
        # the 10.14 HS feedback format. For now tie nominal 48k:
        #   48000/8000 = 6.0 samples/uframe → 6<<14 = 0x18000 (3 bytes).
        # Replace with the live MCR-derived value once integrated.
        # (ep1_in memory write API TBD — see USBIsochronousInMemoryEndpoint.)

        m.d.comb += usb.connect.eq(1)
        return m


# Standalone Verilog emission entry point.
# TODO(P3.1): build a minimal platform shim that maps ulpi_clock/ulpi
# to top-level Verilog ports (instead of real i9plus pins), then:
#   from amaranth.back import verilog
#   sub = USBAVBSubsystem()
#   print(verilog.convert(sub, ports=[sub.channel_stream_payload, ...]))
# so avb-aes3 (Migen) can `Instance("usb_avb_subsystem", ...)` it.
if __name__ == "__main__":
    print("USBAVBSubsystem: scaffold. See docs/phase3-bridge.md, P3.1.")
    print("Verilog emission shim is the next concrete step.")
