#!/usr/bin/env python3
#
# Phase 2b (take 2): USB UAC2 device using the ultraembedded ULPI↔UTMI
# wrapper (Verilog, proven on Xilinx + USB3300) instead of LUNA's own
# ULPI translator (which never completes a register write on the
# yosys+nextpnr-xilinx toolchain).
#
# Architecture:
#   USB3300 ──ULPI──► ulpi_wrapper.v ──UTMI──► LUNA USBDevice ──► UAC2
#            (60 MHz)   (Verilog)              (Amaranth gateware)
#
# LUNA's USBDevice accepts a UTMI bus directly (it only invokes its own
# ULPI translator when the bus has a `dir` attribute). We feed it a
# UTMIInterface Record driven by the wrapper, and override always_fs /
# data_clock so it runs at High Speed (60 MHz) instead of the FS default
# LUNA assumes for plain UTMI buses.

import os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockSignal,
                      ResetSignal, ClockDomain)

from luna                            import top_level_cli
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


class USBLoopbackUTMI(Elaboratable):
    """8ch UAC2 loopback device, ULPI link via ultraembedded wrapper."""

    NUMBER_OF_CHANNELS = 8
    AUDIO_BITS         = 24
    MAX_PACKET_SIZE    = 224

    def elaborate(self, platform):
        m = Module()

        # Add the Verilog ULPI wrapper to the build.
        with open(os.path.join(os.path.dirname(__file__),
                  "../rtl/ulpi_ultraembedded/ulpi_wrapper.v")) as f:
            platform.add_file("ulpi_wrapper.v", f.read())

        ulpi_clk = platform.request("ulpi_clock", 0)
        ulpi     = platform.request("ulpi", 0)

        # usb clock domain: raw PHY 60 MHz via BUFG (no PLL) by default,
        # matching ultraembedded's validated setup. USB_PHASE=raw → no
        # PLL; a numeric USB_PHASE → phase-shift PLL.
        # USB_PHASE: "raw" (BUFG, posedge), "inv" (BUFG, inverted/falling
        # edge — Tang Primer recipe, fixes RX+TX skew together), or a
        # numeric phase value (PLL).
        usb_phase_env = os.environ.get("USB_PHASE", "inv")
        if usb_phase_env == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        elif usb_phase_env == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True,
                usb_phase=float(usb_phase_env))

        # USB3300 RESET is active-high; tie low to keep transceiver enabled.
        m.d.comb += ulpi.rst.o.eq(0)

        # UTMI bus that bridges the Verilog wrapper to LUNA.
        utmi = UTMIInterface()

        # Data-bus tristate: drive when PHY's DIR is low (we own the bus).
        ulpi_data_drive = Signal(8)
        m.d.comb += [
            ulpi.data.o .eq(ulpi_data_drive),
            ulpi.data.oe.eq(~ulpi.dir.i),
        ]

        # --- Falling-edge input sampling -------------------------------
        # openXC7's prjxray FASM backend can't assemble IDDR primitives
        # (IndexError in fasm_assembler), so we get the equivalent
        # half-cycle margin with a falling-edge clock domain made of
        # ordinary fabric flops. The ULPI inputs are registered on the
        # FALLING edge of the usb clock (~8 ns after the PHY drives them
        # on its rising edge) and handed to the wrapper which runs on
        # the rising edge — sampling squarely inside the data-valid
        # window for the PHY→FPGA round-trip delay.
        # SAMPLE_EDGE=pos reverts to rising-edge (no extra margin) for A/B.
        sample_edge = os.environ.get("SAMPLE_EDGE", "neg")
        cd_sample = ClockDomain("usb_sample", clk_edge=sample_edge, local=True)
        m.domains += cd_sample
        m.d.comb += cd_sample.clk.eq(ClockSignal("usb"))

        data_s = Signal(8)
        dir_s  = Signal()
        nxt_s  = Signal()
        m.d.usb_sample += [
            data_s.eq(ulpi.data.i),
            dir_s .eq(ulpi.dir.i),
            nxt_s .eq(ulpi.nxt.i),
        ]

        # Instantiate the wrapper, fed by the IDDR-sampled inputs.
        m.submodules.ulpi_wrap = Instance("ulpi_wrapper",
            i_ulpi_clk60_i      = ClockSignal("usb"),
            i_ulpi_rst_i        = ResetSignal("usb"),
            i_ulpi_data_out_i   = data_s,          # IDDR-sampled bus read
            i_ulpi_dir_i        = dir_s,
            i_ulpi_nxt_i        = nxt_s,
            o_ulpi_data_in_o    = ulpi_data_drive, # data to DRIVE onto bus
            o_ulpi_stp_o        = ulpi.stp.o,

            # UTMI side — connect to the Record.
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

        # Tie off the UTMI status inputs the wrapper doesn't provide.
        # Bus-powered device: VBUS always present, session valid.
        m.d.comb += [
            utmi.vbus_valid     .eq(1),
            utmi.session_valid  .eq(1),
            utmi.session_end    .eq(0),
            utmi.host_disconnect.eq(0),
            utmi.id_digital     .eq(0),
        ]

        # LUNA USBDevice on the UTMI bus. Override the FS defaults LUNA
        # picks for plain UTMI — our UTMI is 8-bit @ 60 MHz = High Speed.
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
        ep2_in = USBIsochronousInStreamEndpoint(
            endpoint_number=2, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep2_in)

        m.submodules.out2ch = out2ch = USBStreamToChannels(
            max_no_channels=self.NUMBER_OF_CHANNELS)
        m.d.comb += out2ch.usb_stream_in.stream_eq(ep1_out.stream)

        m.submodules.ch2in = ch2in = ChannelsToUSBStream(
            max_nr_channels=self.NUMBER_OF_CHANNELS)
        m.d.comb += [
            ch2in.channel_stream_in.stream_eq(out2ch.channel_stream_out),
            ep2_in.stream.stream_eq(ch2in.usb_stream_out),
        ]

        m.d.comb += usb.connect.eq(1)

        # Heartbeat LED (usb domain).
        led      = platform.request("user_led", 0)
        counter  = Signal(26)
        m.d.usb += counter.eq(counter + 1)
        m.d.comb += led.o.eq(counter[-1])

        return m


if __name__ == "__main__":
    top_level_cli(USBLoopbackUTMI)
