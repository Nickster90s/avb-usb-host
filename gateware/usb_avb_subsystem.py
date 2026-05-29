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
                      ResetSignal, ClockDomain, Cat, Const)
from amaranth.lib.fifo import AsyncFIFO

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
    # 32-bit container, full bit width. UAC2 S32_LE today carries 24
    # valid bits MSB-aligned (lower 8 = 0); future-true-32-bit and Milan
    # AAF 32-bit INT both bit-compatible. See README §"UAC2 bit depth".
    AUDIO_BITS         = 32
    MAX_PACKET_SIZE    = 224
    # Bridge FIFO: cd_usb → sys. Sized for 96 kHz × 8 ch headroom
    # (384 samples/ms = ~10 USB µframes). One RAMB36 SDP-mode footprint.
    FIFO_DEPTH         = 1024

    def __init__(self, *, standalone=False):
        self.standalone = standalone

        # --- sys-domain audio sample drain interface (host playback) ---
        # An AsyncFIFO inside the subsystem crosses the cd_usb→sys boundary
        # so the consumer (avb-aes3 SoC) does NOT need any cd_usb-side
        # logic — the first P3.3 attempt that wired the FIFO externally
        # broke 60 MHz ULPI HS chirp because any new cd_usb cell scattered
        # out of the wrapper's already-validated placement region.
        #
        # Read protocol:
        #   while (sample_readable) {
        #       lo = sample_lo;      # bits  0..31 of head: 32-bit signed payload
        #       hi = sample_hi;      # bits 32..63: {reserved[28], first[1], channel[3]}
        #       sample_pop = 1;      # one-cycle strobe to advance
        #   }
        # Head is stable while no pop happens, so lo+hi reads are atomic.
        self.sample_lo              = Signal(32)    # OUT (sys): payload[31:0]
        self.sample_hi              = Signal(32)    # OUT (sys): {0[28], first, channel[3]}
        self.sample_readable        = Signal()      # OUT (sys): FIFO has data
        self.sample_pop             = Signal()      # IN  (sys): advance read ptr
        self.sample_overflow_count  = Signal(32)    # OUT (sys): drops at FIFO full

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
            self.sample_lo, self.sample_hi, self.sample_readable, self.sample_pop,
            self.sample_overflow_count,
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

        # cd_usb → sys AsyncFIFO. Pack into 64-bit words; firmware reads
        # them as a (lo, hi) pair before pulsing pop.
        # Layout: [27..0]=reserved | [27]=first | [26:24]=channel | [23:0]=...
        # actually: bits 0..31 = payload (32-bit); bits 32..34 = channel;
        # bit 35 = first; bits 36..63 = reserved.
        m.submodules.bridge_fifo = bridge_fifo = AsyncFIFO(
            width=64, depth=self.FIFO_DEPTH,
            r_domain="sync", w_domain="usb")

        # Build the 64-bit FIFO word in cd_usb (Cat is LSB-first):
        #   bits  0..7  : zero pad (LSBs of MSB-aligned 32-bit sample)
        #   bits  8..31 : 24-bit audio MSB-aligned (= Milan AAF 32-bit INT)
        #   bits 32..34 : channel (0..7)
        #   bit  35     : first (start-of-frame marker)
        #   bits 36..63 : reserved (0)
        # The MSB-alignment puts the 24 valid audio bits in the upper 24
        # bits of a 32-bit container — bit-exact Milan AAF 32-bit format.
        # Firmware can pass sample_lo straight to aaf_tx_push as a signed
        # int32 with zero further shifting. When USBStreamToChannels is
        # later upgraded to payload_width=32, the same wiring still works
        # (just no zero-pad LSBs).
        w_word = Cat(
            Const(0, 8),         # bits  0..7  : zero pad
            cs.payload[:24],     # bits  8..31 : 24-bit audio, MSB-aligned
            cs.channel_nr,       # bits 32..34
            cs.first,            # bit  35
            Const(0, 28),        # bits 36..63
        )

        # Write side (cd_usb): push a word per valid+ready beat.
        # Wrapper is told ready=1 always — overflow goes to a counter
        # rather than backpressuring the wrapper (any back-edge into
        # cd_usb logic risked breaking 60 MHz ULPI timing on this
        # placement-marginal stack; FIFO is sized for headroom instead).
        m.d.comb += [
            bridge_fifo.w_data.eq(w_word),
            bridge_fifo.w_en  .eq(cs.valid & bridge_fifo.w_rdy),
            cs.ready          .eq(1),
        ]

        # Overflow counter (cd_usb domain).
        overflow_usb = Signal(32)
        with m.If(cs.valid & ~bridge_fifo.w_rdy):
            m.d.usb += overflow_usb.eq(overflow_usb + 1)

        # Sync the overflow counter to sys for CSR readout. Single-flop
        # MultiReg per bit gives best-effort readout — fine for a
        # monotonically-increasing diagnostic; transitions may briefly
        # appear inconsistent but the trend is correct.
        overflow_sys = Signal(32)
        m.d.sync += overflow_sys.eq(overflow_usb)
        m.d.comb += self.sample_overflow_count.eq(overflow_sys)

        # Read side (sys / sync):
        m.d.comb += [
            self.sample_lo      .eq(bridge_fifo.r_data[ 0:32]),
            self.sample_hi      .eq(bridge_fifo.r_data[32:64]),
            self.sample_readable.eq(bridge_fifo.r_rdy),
            bridge_fifo.r_en    .eq(self.sample_pop & bridge_fifo.r_rdy),
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
