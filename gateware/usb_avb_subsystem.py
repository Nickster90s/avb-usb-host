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
                      ResetSignal, ClockDomain, Cat, Const, DomainRenamer,
                      EnableInserter, Mux, signed)
from amaranth.lib.fifo import AsyncFIFO
from amaranth.lib.cdc  import FFSynchronizer
from amlib.utils       import EdgeToPulse

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
        # --- localisation diagnostics (sys-synced from cd_usb) ---
        self.dbg_rx_beats   = Signal(32)   # OUT: core->EP raw byte beats (rx.next & rx.valid)
        self.dbg_ep_out     = Signal(32)   # OUT: EP->decoder beats (stream.valid & ready)

        # --- async feedback in (fabric→device) ---
        # USB async-feedback inputs (sys domain). The feedback value is now
        # MEASURED + computed inside this wrapper, SOF-synchronised (ADAT/LUNA
        # canonical topology) — see elaborate(). The fabric just supplies the
        # media-clock tick and the consumer-FIFO level.
        self.sample_strobe          = Signal()       # input: MCR fs strobe (media clock)
        self.block_level            = Signal(8)       # input: consumer FIFO occupancy (0..depth)
        self.fb_ovr                 = Signal(32)      # input: firmware feedback OVERRIDE (0=auto)

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
            self.dbg_rx_beats, self.dbg_ep_out,
            self.sample_strobe, self.block_level, self.fb_ovr,
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
        uac2_handler = UAC2RequestHandlers()
        control_ep.add_request_handler(uac2_handler)

        ep1_out = USBIsochronousOutStreamEndpoint(
            endpoint_number=1, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep1_out)
        ep1_in = USBIsochronousInMemoryEndpoint(
            endpoint_number=1, max_packet_size=4)
        usb.add_endpoint(ep1_in)

        # The decoder MUST run in the USB (cd_usb) domain — same as the EP that
        # feeds it (ep1_out) and the bridge_fifo WRITE side (w_domain="usb").
        # USBStreamToChannels uses m.d.sync; without this DomainRenamer it lands
        # in the wrapper's sync(=sys) domain, so ep1_out.stream (usb) -> decoder
        # (sys) and decoder cs.valid (sys) -> bridge write (usb) are BOTH
        # unsynchronised crossings. The sys-clocked FSM mis-samples the usb
        # stream and the bridge re-samples cs.valid -> the decoder over-produces
        # ~4x (on-HW usb_samp~400k vs real 96k). The Amaranth sim was clean only
        # because it wrapped the decoder in DomainRenamer("usb"). Match it here so
        # EP->decoder->bridge-write are one clean cd_usb chain; the AsyncFIFO is
        # then the ONLY (proper) cd_usb->sys crossing.
        m.submodules.out2ch = out2ch = DomainRenamer("usb")(
            USBStreamToChannels(max_no_channels=self.NUMBER_OF_CHANNELS,
                                sample_width=self.AUDIO_BITS))   # true 32-bit
        # Connect the EP stream to the decoder, but DO NOT pass usb_first. The EP
        # boundary detector's `first` (USB packet/transaction start) is NOT
        # 4-byte/sample-aligned on the real host stream, so USBStreamToChannels'
        # usb_first resync fires mid-sample and truncates frames -> cs.first lands
        # on ~75% of samples instead of 50% for 2ch (on-HW first=72k vs 48k),
        # over-pressuring the SRC ring so it never centres. Tie first=0: the
        # decoder then FREE-RUNS its channel counter (it already wraps at
        # last_channel = no_channels_in-1), giving clean 1-first-per-frame framing.
        # Safe here: the EP path is drop-free (usb_samp is exactly 96k, sim-proven)
        # so the free-running count stays aligned. NOTE: free-run starts the count
        # one sample early (channels emit 1,0,1,0.. for 2ch) — a cosmetic phase
        # offset (L/R swap); a one-shot start-of-stream resync can fix the phase
        # later if needed. usb_last is unused by the decoder.
        # ANCHORED framing (port of usb-dac-96K32B, which runs clean on macOS+Linux):
        # PASS usb_first so the decoder RE-ALIGNS its channel counter to each packet
        # boundary instead of free-running from an arbitrary start phase. The OUT
        # endpoint delivers whole frames (MAX_PACKET_SIZE 224 = 7x32B for 8ch/32-bit,
        # i.e. the frame-aligned base _out_packet would compute), so usb_first lands
        # on a frame edge. The old usb_first=0 free-run made channel identity a
        # startup lottery -> AAF ch1 landed on the wrong source channel, and the
        # phase could drift -> drops. stream_eq forwards valid/payload/first/last+ready.
        m.d.comb += out2ch.usb_stream_in.stream_eq(ep1_out.stream)
        # CRITICAL: drive no_channels_in or the decoder mis-frames and FLOODS the
        # bridge (last_channel = no_channels_in-1 = -1 if undriven -> the channel
        # counter never wraps cleanly -> cs.first fires on the wrong cadence ->
        # ~14x too many frames, ring pins full). Set to the host's ACTIVE channel
        # count. Today the host streams 2ch (the 8ch altset won't enumerate, see
        # the UAC2 8ch-terminal descriptor TODO); set 8 here once that's fixed.
        # Single 8ch OUT altset (altset1): no stereo fallback, so the decoder
        # always frames N=8 channels. n_active is a constant used by the re-frame
        # counter below so its wrap matches no_channels_in.
        n_active = self.NUMBER_OF_CHANNELS
        m.d.comb += out2ch.no_channels_in.eq(n_active)

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
        # ANCHORED channel id (port of usb-dac-96K32B's StereoPairExtractor model):
        # use the DECODER's channel_nr + first DIRECTLY. Now that usb_first is passed
        # (above), the decoder's framing is re-anchored to each packet boundary, so
        # channel_nr is the TRUE channel (0..7) and cs.first marks channel 0 = frame
        # start. This replaces the old free-running mod-N counter, whose arbitrary
        # start phase rotated the channels (AAF ch1 on the wrong source channel) and
        # could drift -> drops. (The old code ignored channel_nr because with
        # usb_first=0 it was as un-anchored as the counter; the anchor is what makes
        # it trustworthy now — same reason the DAC could use it and stay clean.)
        w_word = Cat(
            cs.payload[:32],     # bits  0..31 : full 32-bit audio (byte0 = real LSB)
            cs.channel_nr,       # bits 32..34 : channel (decoder, anchored)
            cs.first,            # bit  35     : first (decoder frame start, anchored)
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

        # Localisation counters (cd_usb): where does the ~25x replay enter?
        #  rx_beats = raw bytes the USB core/ULPI hands the EP (rx.next & rx.valid)
        #  ep_out   = bytes the EP emits to the decoder (stream.valid & ready)
        # If rx_beats >> real 384k B/s -> real RX duplication (ULPI/UTMI/placement).
        # If rx_beats ~= 384k but ep_out/cs.valid floods -> EP re-presents (stale buf).
        rx_beats_usb = Signal(32); ep_out_usb = Signal(32)
        with m.If(ep1_out.interface.rx.next & ep1_out.interface.rx.valid):
            m.d.usb += rx_beats_usb.eq(rx_beats_usb + 1)
        with m.If(ep1_out.stream.valid & ep1_out.stream.ready):
            m.d.usb += ep_out_usb.eq(ep_out_usb + 1)
        rx_beats_sys = Signal(32); ep_out_sys = Signal(32)
        m.d.sync += [rx_beats_sys.eq(rx_beats_usb), ep_out_sys.eq(ep_out_usb)]
        m.d.comb += [self.dbg_rx_beats.eq(rx_beats_sys), self.dbg_ep_out.eq(ep_out_sys)]

        # Read side (sys / sync):
        m.d.comb += [
            self.sample_lo      .eq(bridge_fifo.r_data[ 0:32]),
            self.sample_hi      .eq(bridge_fifo.r_data[32:64]),
            self.sample_readable.eq(bridge_fifo.r_rdy),
            bridge_fifo.r_en    .eq(self.sample_pop & bridge_fifo.r_rdy),
        ]

        # EP1 IN async feedback — PI servo on the SRC-ring level, ported from
        # usb-dac-96K32B (HW-verified glitch-free). Computed in the usb domain
        # (usb.sof_detected) and updated ONCE per 256 µframes (~32 ms) so the
        # reported rate is smooth and never fights the host's own servo.
        #
        #   fb = (Σ 1×fs strobe over 256 µframes) << 8  +  PI_correction
        # The strobe sum (~1536) << 8 is the HONEST consume rate in Q16.16
        # samples/µframe (48k → 0x0006_0000) that the host slaves to. The PI
        # correction (err = CENTER − ring_level; P + slow integral) nudges the
        # host ±0.38 % to hold the ring at mid-depth. The INTEGRAL is what the
        # old proportional-only loop lacked: it drives the steady-state error to
        # zero so the ring sits CENTERED (not low → no underruns) while the
        # correction stays tiny + slow so the host's rate never flutters. (The
        # P-only loop had to choose between riding low or wobbling the host at
        # 1 ms; the integral removes that trade-off — see usb-dac-96K32B.)

        # Bring the sys-domain media-clock strobe + FIFO level into cd_usb.
        strobe_usb = Signal()
        m.submodules.fb_strobe_sync = FFSynchronizer(self.sample_strobe, strobe_usb, o_domain="usb")
        m.submodules.fb_strobe_edge = fb_strobe_edge = DomainRenamer("usb")(EdgeToPulse())
        strobe_tick = Signal()
        m.d.usb  += fb_strobe_edge.edge_in.eq(strobe_usb)
        m.d.comb += strobe_tick.eq(fb_strobe_edge.pulse_out)

        level_usb = Signal(8)
        m.submodules.fb_level_sync = FFSynchronizer(self.block_level, level_usb, o_domain="usb")

        clock_counter = Signal(20)          # 1×fs strobe ticks per 256-SOF window
        sof_counter   = Signal(8)
        fb_value      = Signal(32, reset=0x0006_0000)

        # PI servo on the ring level (constants as the proven usb-dac-96K32B).
        # block_level (from avb_soc) = ring_level>>2 (0..128); >>1 more → 0..64,
        # so CENTER=32 ⇒ block_level 64 ⇒ ring 256 = mid of the 512-deep ring —
        # matching the DAC's w_level>>4 (1024→64) range so KP/clamps carry over.
        CENTER    = 32
        KP        = 128
        INT_CLAMP = 1024
        CORR_MAX  = 1500                     # ±0.38 % of the ~0x60000 base
        level_b = Signal(7)
        err     = Signal(signed(9))
        integ   = Signal(signed(16))
        new_i   = Signal(signed(17))
        corr    = Signal(signed(24))
        corr_c  = Signal(signed(20))
        m.d.comb += [
            level_b.eq(level_usb >> 1),
            err.eq(CENTER - level_b),
            new_i.eq(integ + err),          # integral candidate (pre anti-windup)
            corr.eq(err * KP + integ),      # P + I
        ]
        with m.If(corr > CORR_MAX):
            m.d.comb += corr_c.eq(CORR_MAX)
        with m.Elif(corr < -CORR_MAX):
            m.d.comb += corr_c.eq(-CORR_MAX)
        with m.Else():
            m.d.comb += corr_c.eq(corr)

        # Final safety clamp to the host's accept window: Linux snd-usb-audio
        # REJECTS feedback outside [freqn−freqn/8, freqmax] and strands at a
        # stale rate, so a glitched strobe count must never escape the window.
        NOMINAL = 0x0006_0000
        FB_LO   = NOMINAL - 0x8000           # 0x58000
        FB_HI   = NOMINAL + 0x8000           # 0x68000
        fb_raw  = Signal(signed(34))
        m.d.comb += fb_raw.eq((clock_counter << 8) + corr_c)

        with m.If(strobe_tick):
            m.d.usb += clock_counter.eq(clock_counter + 1)
        with m.If(usb.sof_detected):
            m.d.usb += sof_counter.eq(sof_counter + 1)
            # ONE update per 256 µframes (~32 ms): advance the integral with
            # anti-windup, latch fb = measured-base + PI-correction (final
            # clamped), and reset the window (carry the live strobe tick). The
            # slow cadence + tiny clamp keep the host's reported rate smooth.
            with m.If(sof_counter == 0):
                with m.If(new_i > INT_CLAMP):
                    m.d.usb += integ.eq(INT_CLAMP)
                with m.Elif(new_i < -INT_CLAMP):
                    m.d.usb += integ.eq(-INT_CLAMP)
                with m.Else():
                    m.d.usb += integ.eq(new_i)
                with m.If(fb_raw < FB_LO):
                    m.d.usb += fb_value.eq(FB_LO)
                with m.Elif(fb_raw > FB_HI):
                    m.d.usb += fb_value.eq(FB_HI)
                with m.Else():
                    m.d.usb += fb_value.eq(fb_raw)
                m.d.usb += clock_counter.eq(strobe_tick)

        # Firmware feedback OVERRIDE: when usb_fb_ovr != 0, report that fixed
        # value instead of the measured/centring loop — lets firmware sweep
        # hardcoded feedback values live (no rebuild) to characterise the host's
        # response. 0 = normal auto loop.
        fb_ovr_usb = Signal(32)
        m.submodules.fb_ovr_sync = FFSynchronizer(self.fb_ovr, fb_ovr_usb, o_domain="usb")
        fb_out = Signal(32)
        with m.If(fb_ovr_usb != 0):
            m.d.comb += fb_out.eq(fb_ovr_usb)
        with m.Else():
            m.d.comb += fb_out.eq(fb_value)

        m.d.comb += [
            ep1_in.bytes_in_frame.eq(4),
            ep1_in.value.eq(0xff & (fb_out >> (ep1_in.address << 3))),
        ]

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
