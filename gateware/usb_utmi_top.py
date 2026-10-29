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
                      ResetSignal, ClockDomain, Array, C, Cat, Mux, signed, DomainRenamer)
from amaranth.lib.cdc import FFSynchronizer
from amaranth.lib.fifo import AsyncFIFO
from amlib.utils import EdgeToPulse
from clean_i2s_tx import CleanI2STx

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
from stereopair_extractor             import StereoPairExtractor
from amlib.io.i2s                     import I2STransmitter


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
        # NOTE the OE assignment is deferred to AFTER the input-delay stage
        # below, so it is driven from the SAME dir_s the wrapper sees. Driving
        # it from the raw `ulpi.dir.i` here while an IDELAYE2 also claims that
        # pad asks one pin for a delayed AND an undelayed copy of itself, which
        # a 7-series IOI cannot deliver: PAD -> IBUF -> (IDELAY | direct) is a
        # one-of-two mux, not a fan-out. That is the standing explanation for
        # the persistent `overused=3` -- identical in VAR_LOAD, VARIABLE, FIXED
        # and FIXED with i_C tied off, and unchanged by a BUFG on REFCLK or by
        # injected clock constraints. With no IDELAY, dir_s IS ulpi.dir.i, so
        # the behaviour of every existing build is bit-identical.
        ulpi_data_drive = Signal(8)
        m.d.comb += ulpi.data.o.eq(ulpi_data_drive)

        # Feed the wrapper the RAW ULPI input pins directly — exactly
        # like the ulpi_phasescan FSM that reads reg 0x24 correctly at
        # phase 0. The earlier falling-edge re-sampling layer added a
        # half-cycle that mis-timed the wrapper (worked for the read FSM
        # without it, failed for the wrapper with it). SAMPLE_EDGE can
        # re-enable a sampling layer for A/B, default off (raw).
        sample_edge = os.environ.get("SAMPLE_EDGE", "off")
        if sample_edge == "off":
            data_s, dir_s, nxt_s = ulpi.data.i, ulpi.dir.i, ulpi.nxt.i
        else:
            cd_sample = ClockDomain("usb_sample", clk_edge=sample_edge, local=True)
            m.domains += cd_sample
            m.d.comb += cd_sample.clk.eq(ClockSignal("usb"))
            data_s = Signal(8); dir_s = Signal(); nxt_s = Signal()
            m.d.usb_sample += [
                data_s.eq(ulpi.data.i), dir_s.eq(ulpi.dir.i), nxt_s.eq(ulpi.nxt.i),
            ]

        # --- IDELAYE2 on the ULPI inputs (openXC7-proven in avb-aes3) ----------
        # USB_IDELAY_TAP=<0..31> inserts a per-input IDELAYE2 (~78 ps/tap) on
        # data/dir/nxt to CENTRE the 60 MHz source-synchronous RX sampling in
        # the data eye. This is the fix for a narrow / placement-shifted eye
        # that the PLL-phase knob cannot hit (esp. the scattered backup pins) —
        # avb-aes3 uses the same mechanism (ulpi_idelay_tap + 'u' command).
        # Needs the 200 MHz cd_fast + exactly one IDELAYCTRL. Unset = no IDELAY.
        _tap_env = os.environ.get("USB_IDELAY_TAP")
        if _tap_env is not None:
            tap = int(_tap_env)
            # IDELAYCTRL reset pulse in the crystal-clean sync domain.
            idc_rst = Signal(reset=1); idc_cnt = Signal(8, reset=0)
            with m.If(~idc_cnt.all()):
                m.d.sync += idc_cnt.eq(idc_cnt + 1)
            with m.Else():
                m.d.sync += idc_rst.eq(0)
            # IDELAYCTRL's REFCLK must arrive on a GLOBAL clock net. The CAR
            # drives cd_fast straight off PLL CLKOUT1 with no BUFG
            # (colorlight_i9plus_car.py:126), and in this design fast_clk's
            # ONLY load is this REFCLK -- so without a buffer nextpnr has to
            # drag a 200 MHz PLL output to an IDELAYCTRL site over general
            # interconnect. That is the standing suspect for the persistent
            # `overused=3` that stalled the router in VAR_LOAD, VARIABLE,
            # FIXED and FIXED-with-i_C-tied-off alike (same count every time,
            # which points at one structural net, not at the sweep logic).
            refclk_g = Signal(name="idelay_refclk_g")
            m.submodules.idelay_bufg = Instance("BUFG",
                i_I=ClockSignal("fast"), o_O=refclk_g)
            m.submodules.idelayctrl = Instance("IDELAYCTRL",
                i_REFCLK=refclk_g, i_RST=idc_rst)

            def _idelay(sig_in, nm):
                o = Signal(name="idly_" + nm)
                m.submodules["idelay_" + nm] = Instance("IDELAYE2",
                    p_IDELAY_TYPE="FIXED", p_DELAY_SRC="IDATAIN",
                    p_HIGH_PERFORMANCE_MODE="TRUE", p_SIGNAL_PATTERN="DATA",
                    p_REFCLK_FREQUENCY=200.0, p_CINVCTRL_SEL="FALSE",
                    p_PIPE_SEL="FALSE", p_IDELAY_VALUE=tap,
                    # IDELAY_TYPE=FIXED leaves C/CE/INC/LD/CNTVALUEIN UNUSED
                    # (UG471). Feeding C the 60 MHz sync clock put a SECOND
                    # clock into all 10 IOI tiles for nothing, on top of
                    # IDELAYCTRL's 200 MHz REFCLK -- a prime suspect for the
                    # persistent `overused=3` that stalls the router in every
                    # IDELAY mode tried so far. Tie it off.
                    i_C=0, i_LD=0, i_CNTVALUEIN=0,
                    i_CE=0, i_INC=0, i_LDPIPEEN=0, i_REGRST=0,
                    i_IDATAIN=sig_in, o_DATAOUT=o)
                return o

            data_s = Cat(*[_idelay(data_s[i], f"d{i}") for i in range(8)])
            dir_s  = _idelay(dir_s, "dir")
            nxt_s  = _idelay(nxt_s, "nxt")

        # Drive the data-bus OE from the (possibly delayed) dir, matching what
        # the wrapper samples -- see the note at the ulpi.data.o assignment.
        m.d.comb += ulpi.data.oe.eq(~dir_s)

        # Startup reset pulse for the wrapper: ResetSignal("usb") is
        # never asserted, so the wrapper never gets a clean reset to
        # kick off its init / register-config sequence. Hold its reset
        # high for the first ~64 usb cycles after boot, then release.
        wrap_rst   = Signal(reset=1)
        wrap_rstcnt = Signal(7, reset=0)
        with m.If(~wrap_rstcnt.all()):
            m.d.usb += wrap_rstcnt.eq(wrap_rstcnt + 1)
        with m.Else():
            m.d.usb += wrap_rst.eq(0)

        # Instantiate the wrapper, fed by the raw (or sampled) inputs.
        m.submodules.ulpi_wrap = Instance("ulpi_wrapper",
            i_ulpi_clk60_i      = ClockSignal("usb"),
            i_ulpi_rst_i        = wrap_rst,
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

        # Name the device after the PHY it is running on. Two instances of this
        # gateware share VID:PID 1209:eab1, so with both plugged in the host
        # showed two identical "N-Series AVB Switchover Main" cards (ALSA
        # disambiguating only by appending _1). The backup build now says
        # "Backup" and carries a distinct serial, so lsusb / aplay -l / udev can
        # tell them apart. usb_descriptors.py was written for this from the
        # start -- the call site simply never passed the arguments.
        # USB_PRODUCT overrides the name outright.
        _is_backup   = os.environ.get("USB_BACKUP", "") == "1"
        _product     = os.environ.get(
            "USB_PRODUCT",
            "N-Series AVB Switchover " + ("Backup" if _is_backup else "Main"))
        # Distinct PID for the backup port, following the upstream
        # adat-usb2-audio-interface pattern (ADA1 / ADA2). NOTE 0xEAB2 under
        # VID 0x1209 is a pid.codes slot that has to actually be yours --
        # fine on the bench, but confirm the allocation before shipping, or
        # override with USB_PID=0xeab1 to put both ports back on one PID.
        _pid = int(os.environ.get("USB_PID",
                                  "0xEAB2" if _is_backup else "0xEAB1"), 16)
        descriptors     = USBDescriptors(use_ila=False, ila_max_packet_size=512,
                                         product_name=_product,
                                         serial="1" if _is_backup else "0",
                                         product_id=_pid)
        # DESC_CH env overrides the advertised channel count (default = 8). Set
        # DESC_CH=2 for a clean stereo device (only the 2ch altset, FL/FR
        # positions) — macOS test for the "starting channel zero" / open-loop
        # feedback failure. The gateware still tracks the active altset for
        # no_channels_in, so decode matches whatever the host selects.
        desc_ch = int(os.environ.get("DESC_CH", self.NUMBER_OF_CHANNELS))
        usb_descriptors = descriptors.create_usb2_descriptors(
                              desc_ch, self.MAX_PACKET_SIZE)

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
        ep2_in = USBIsochronousInStreamEndpoint(
            endpoint_number=2, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep2_in)

        # USB_MINIMAL: enumeration-only build. Drops the whole audio/DAC/FIFO/
        # servo stack so the design routes with lots of headroom (the full stack
        # + 10 IDELAYE2 could not converge — 3 stuck overused wires). Keeps
        # USBDevice + control endpoint + descriptors, which is all the ULPI-eye
        # test needs: if the eye is open the host reads the device descriptor and
        # sets the address (no error-71); if not, it fails exactly as now. The
        # ISO endpoints are declared (descriptors match) but tied inert.
        if os.environ.get("USB_MINIMAL"):
            m.d.comb += [
                ep1_out.stream.ready.eq(1),     # drain OUT
                ep2_in.stream.valid.eq(0),      # no capture data
                ep1_in.bytes_in_frame.eq(4),
                ep1_in.value.eq(0),
                usb.connect.eq(1),
            ]
            return m

        # All audio modules run in the USB domain (like ADAT) — the endpoint
        # streams are usb-domain; running these in the default "sync" domain was
        # a usb->sync crossing between two different 60 MHz clocks, which made the
        # DAC path placement-fragile (intermittent no-audio / bleed / glitches).
        m.submodules.out2ch = out2ch = DomainRenamer("usb")(USBStreamToChannels(
            max_no_channels=self.NUMBER_OF_CHANNELS))
        # CRITICAL: no_channels_in MUST match the host's ACTIVE output altset, or
        # the decoder mis-frames. Linux/aplay picks altset 1 (2ch); macOS picks
        # altset 2 (the 8ch native format) and streams 8 channels. Hardcoding 2
        # made macOS's 8ch stream frame as 2ch -> ~4x frame rate -> FIFO flood ->
        # servo slams feedback to floor -> macOS iso scheduling overloads
        # (kIOReturnIsoTooNew) and stops the engine after ~2 s. Track the altset:
        # altset 1 -> 2 ch, altset 2 -> NUMBER_OF_CHANNELS (8). The DAC extractor
        # pulls ch0/ch1 either way, so stereo monitoring works for both.
        out_alt = uac2_handler.output_interface_altsetting_nr
        m.d.comb += [
            out2ch.usb_stream_in.stream_eq(ep1_out.stream),
            out2ch.no_channels_in.eq(Mux(out_alt == 2, self.NUMBER_OF_CHANNELS, 2)),
        ]

        m.submodules.ch2in = ch2in = DomainRenamer("usb")(ChannelsToUSBStream(
            max_nr_channels=self.NUMBER_OF_CHANNELS))
        m.d.comb += [
            ch2in.channel_stream_in.stream_eq(out2ch.channel_stream_out),
            ep2_in.stream.stream_eq(ch2in.usb_stream_out),
        ]
        # CRITICAL: the DAC tap (StereoPairExtractor) snoops the channel stream
        # PASSIVELY — it never drives ready (see its own comment "must not exert
        # upstream back pressure"). So the decoder's drain is owned entirely by
        # ch2in/the capture IN endpoint. During plain playback the host does NOT
        # poll EP2-IN, so ChannelsToUSBStream fills once, drops ready, and FREEZES
        # the whole channel stream -> the DAC tap sees no more valid&ready
        # transfers -> amlib FIFO starves -> stale garbage on L, LED off.
        # Force the decoder to always drain so the DAC always gets every sample;
        # ch2in still receives the data and forwards what it can. (last-assignment
        # wins over the stream_eq ready above.)
        m.d.comb += out2ch.channel_stream_out.ready.eq(1)

        # ---- Local I2S DAC monitor (phase-locked CleanI2STx in cd_audio) ----
        # amlib's I2STransmitter ran in the USB domain and treated BCK as an async
        # input, so DOUT (usb-clocked) drifted in phase against the cd_audio BCK
        # the DAC samples on -> periodic setup/hold violation -> constant harsh
        # crackle (bit corruption). FIX: serialise entirely in cd_audio with
        # CleanI2STx (BCK/LRCK/DOUT from ONE counter -> zero CDC skew). Samples
        # cross usb->audio through an AsyncFIFO; CleanI2STx repeats the last frame
        # on underrun. (See avb-aes3 i2s_clean.py — sim-bit-exact, HW-clean.)
        dac_pads = platform.request("i2s_dac", 0)

        # Free-running cd_audio counter, used ONLY for the rate-feedback tick.
        audio_cnt = Signal(8)
        m.d.audio += audio_cnt.eq(audio_cnt + 1)

        # Extractor (usb): pull ch0/ch1 out of the decoded multichannel stream.
        # It snoops passively, so a modest FIFO is fine — the elastic buffer is the
        # AsyncFIFO below.
        m.submodules.dac_ext = dac_ext = DomainRenamer("usb")(StereoPairExtractor(
            self.NUMBER_OF_CHANNELS, fifo_depth=16))
        m.d.comb += [
            dac_ext.selected_channel_in.eq(0),
            dac_ext.channel_stream_in.valid.eq(
                out2ch.channel_stream_out.valid & out2ch.channel_stream_out.ready),
            dac_ext.channel_stream_in.payload.eq(out2ch.channel_stream_out.payload),
            dac_ext.channel_stream_in.channel_nr.eq(out2ch.channel_stream_out.channel_nr),
        ]

        # Assemble L/R pairs (extractor first=L, last=R) into one 48-bit entry so
        # both channels of a frame come from the SAME FIFO slot (no inter-channel
        # skew), and push into the usb->audio elastic AsyncFIFO.
        m.submodules.dac_fifo = dac_fifo = AsyncFIFO(
            width=48, depth=1024, w_domain="usb", r_domain="audio")
        cs = dac_ext.channel_stream_out
        pair_l = Signal(24)
        have_l = Signal()
        m.d.comb += cs.ready.eq(1)            # always drain the extractor
        with m.If(cs.valid):
            with m.If(cs.first):              # left
                m.d.usb += [pair_l.eq(cs.payload), have_l.eq(1)]
            with m.Elif(have_l):              # right completes the pair
                m.d.comb += [
                    dac_fifo.w_data.eq(Cat(pair_l, cs.payload)),
                    dac_fifo.w_en.eq(1),
                ]
                m.d.usb += have_l.eq(0)

        # Phase-locked serialiser in cd_audio. Pins driven DIRECTLY (no inversion).
        m.submodules.dac_tx = dac_tx = DomainRenamer("audio")(CleanI2STx())

        # One-shot prime at startup: hold the serialiser off until the FIFO has
        # filled to a cushion ONCE, then run free forever (underruns thereafter
        # are handled gracefully by CleanI2STx repeating the last frame — much
        # better than re-gating, which dropped audio during every refill).
        primed = Signal()
        with m.If(dac_fifo.r_level >= 512):           # prime to half (~10.6 ms cushion)
            m.d.audio += primed.eq(1)

        if os.environ.get("DIAG_TONE"):
            # DIAGNOSTIC: drive CleanI2STx from an FPGA-internal sine, bypassing
            # the whole USB->decode->pair->FIFO path. Isolates serialiser+DAC+
            # format from the USB data path. 256-entry LUT, one step per frame ->
            # 47832/256 = 186.8 Hz tone on BOTH channels.
            import math
            N = 256
            sine = Array(C(int(0.4 * (2**23 - 1) * math.sin(2 * math.pi * i / N)) & 0xFFFFFF, 24)
                         for i in range(N))
            ph = Signal(range(N))
            with m.If(dac_tx.audio_ready):
                m.d.audio += ph.eq(ph + 1)
            samp = Signal(24)
            m.d.comb += samp.eq(sine[ph])
            m.d.comb += [
                dac_tx.audio_l.eq(samp),
                dac_tx.audio_r.eq(samp),
                dac_tx.audio_valid.eq(1),
            ]
        else:
            m.d.comb += [
                dac_tx.audio_l.eq(dac_fifo.r_data[0:24]),
                dac_tx.audio_r.eq(dac_fifo.r_data[24:48]),
                dac_tx.audio_valid.eq(dac_fifo.r_rdy & primed),
                dac_fifo.r_en.eq(dac_tx.audio_ready),
            ]

        m.d.comb += [
            dac_pads.bck .o.eq(dac_tx.bck),
            dac_pads.lrck.o.eq(dac_tx.lrck),
            dac_pads.din .o.eq(dac_tx.dout),
        ]

        # ---- Async sample-rate feedback (ADAT/smunaut "honest measured") ----
        # Count cd_audio ticks vs the USB SOF: the tick count over 256 microframes
        # IS the Q16.16 samples/µframe feedback (cd_audio = 256·fs ->
        # count = cd_audio·256/8000 = (fs/8000)·65536; 12.288MHz -> 0x60000). A
        # small proportional trim from amlib's tx_fifo level keeps it centred.
        # Updated once per 256 SOF -> rock-stable (no hunting), and it reports our
        # ACTUAL rate so the host slaves to us -> no drops, no L/R bleed. This
        # replaces the bang-bang servo, which hunted the rate and broke framing.
        # Tap the audio clock as a FABRIC signal (audio_cnt[0] = cd_audio/2, a
        # SLICE output), NOT the global clock net — nextpnr-xilinx cannot route a
        # BUFG clock into a fabric FF (ClockSignal("audio") fails routing). XOR
        # consecutive samples -> a pulse per audio_cnt[0] EDGE = one per cd_audio
        # cycle (both edges), matching ADAT's per-audio-clock-cycle count.
        audio_div_usb = Signal()
        audio_div_p   = Signal()
        m.submodules += FFSynchronizer(audio_cnt[0], audio_div_usb, o_domain="usb")
        m.d.usb += audio_div_p.eq(audio_div_usb)
        audio_tick = Signal()
        m.d.comb += audio_tick.eq(audio_div_usb ^ audio_div_p)
        fb            = Signal(32, reset=0x60000)
        audio_counter = Signal(19)
        sof_counter   = Signal(8)
        with m.If(audio_tick):
            m.d.usb += audio_counter.eq(audio_counter + 1)
        # ---- Closed-loop PI servo on the FIFO level (CENTRED-FIFO experiment) ----
        # Replaces the crude `+1 - (level>>8)` term with a real PI servo (ported
        # from avb-aes3 mcr.c::mcr_usb_lock). `audio_counter` is the measured
        # nominal consume rate (cd_audio ticks / 256 uframes = our Q16.16 rate);
        # the PI adds a correction that drives the host to hold the FIFO at CENTER.
        # Goal: keep dac_fifo CENTRED -> never under/overflows -> bit-perfect
        # regime, IF the host honours feedback. The underrun LED is the witness:
        # if it stays dark, PipeWire is honouring us and the FIFO is centred; if it
        # keeps flashing, the host ignores fine feedback and we need the
        # adjustable-clock (NCO/MMCM) approach instead.
        #   error = CENTER - level   (>0 below centre -> tell host to send MORE)
        #   corr  = err*KP + integral(KI=1), anti-windup + clamp to ~+-1.5%
        #   fb    = audio_counter + corr
        CENTER    = 32         # target for w_level>>4  (fifo ~512 of 1024)
        KP        = 128
        INT_CLAMP = 1024
        # macOS/CoreAudio rejects async feedback that strays too far from nominal
        # and halts the stream (~2 s) — Linux tolerates it. Keep the correction
        # SMALL (~0.38 %): base rate is already -0.35 % off nominal, so this keeps
        # the reported rate within ~-0.7%..+0.03 % of 48 k, inside macOS's window,
        # while the integral still slowly centres the FIFO. (Was 6000 = 1.5 %,
        # which slammed to ~-1.85 % at startup -> macOS dropped the stream.)
        CORR_MAX  = 1500       # ~0.38 % of the ~391837 nominal fb
        level_b = Signal(7)
        err     = Signal(signed(9))
        integ   = Signal(signed(16))
        new_i   = Signal(signed(17))
        corr    = Signal(signed(24))
        corr_c  = Signal(signed(20))
        m.d.comb += [
            level_b.eq(dac_fifo.w_level >> 4),
            err.eq(CENTER - level_b),
            new_i.eq(integ + err),
            corr.eq(err * KP + integ),
        ]
        with m.If(corr > CORR_MAX):
            m.d.comb += corr_c.eq(CORR_MAX)
        with m.Elif(corr < -CORR_MAX):
            m.d.comb += corr_c.eq(-CORR_MAX)
        with m.Else():
            m.d.comb += corr_c.eq(corr)
        with m.If(usb.sof_detected):
            m.d.usb += sof_counter.eq(sof_counter + 1)
            with m.If(sof_counter == 0):               # every 256 uframes (~31 Hz)
                with m.If(new_i > INT_CLAMP):
                    m.d.usb += integ.eq(INT_CLAMP)
                with m.Elif(new_i < -INT_CLAMP):
                    m.d.usb += integ.eq(-INT_CLAMP)
                with m.Else():
                    m.d.usb += integ.eq(new_i)
                m.d.usb += [
                    fb.eq(audio_counter + corr_c),
                    audio_counter.eq(0),
                ]
        bit_pos = Signal(5)
        m.d.comb += [
            ep1_in.bytes_in_frame.eq(4),
            bit_pos.eq(ep1_in.address << 3),
            ep1_in.value.eq(0xFF & (fb >> bit_pos)),
        ]

        m.d.comb += usb.connect.eq(1)

        # LED = live FIFO-LEVEL brightness meter (PWM, usb domain). Duty cycle =
        # w_level / 1024, refreshed at 60 MHz/1024 = 58 kHz (no visible flicker),
        # so LED BRIGHTNESS directly shows the elastic-buffer fill:
        #   DARK / very dim  = near EMPTY  (draining / underrunning  -> not bit-perfect)
        #   ~HALF brightness = CENTRED ~512 (servo holding it -> healthy)
        #   FULL bright      = near FULL  (overfilling, about to drop)
        # This replaces the underrun meter, whose 85 ms hold saturated solid at
        # just ~12 underruns/s and so read "empty" even with steady audio. Watch
        # whether the brightness sits steady at ~half (centred) or sinks to dim
        # (PipeWire under-feeding -> host ignores our feedback -> need NCO path).
        led = platform.request("user_led", 0)
        pwm = Signal(10)
        m.d.usb += pwm.eq(pwm + 1)
        m.d.comb += led.o.eq(pwm < dac_fifo.w_level)


        # ---------------- USB_PROBE=1: live UTMI enumeration probe ------------
        # The ULPI bus at position B is now fully verified (clock, 8 data lines,
        # DIR, NXT, RST, STP -- see the memory file).  Yet the host detects a
        # high-speed device on usb 1-10 and then fails the first descriptor read
        # with EPROTO.  EVERY measurement so far was taken with the PHY idle, in
        # reset, or exchanging ULPI *register* traffic.  Nothing has ever looked
        # at the bus while the host is actually talking 480 Mbps.
        #
        # This probe does, and it lives INSIDE the real design (env-gated, not a
        # copy) so it cannot drift from the known-good configuration -- the
        # lesson from feedback_instrument_must_match_known_good.
        #
        # At high speed the host emits a SOF every 125 us, forever, whether or
        # not we ever answer.  That makes SF a free, always-on RX monitor:
        #
        #   SF climbing (~8 kHz)       RX path decodes real HS packets -> RX OK
        #   SF = 0000                  we never see the host at all -> RX dead
        #   RE climbing alongside SF   packets arrive but mis-framed -> RX noisy
        #   SU/DA > 0 and S0..S7 right the GET_DESCRIPTOR arrived INTACT, so the
        #                              fault is our TX / response path
        #   AK > 0                     the host ACKed something we sent -> TX OK
        #
        # S0..S7 is the payload of the first DATA packet after the first SETUP
        # token.  The host's first control request is always
        #       GET_DESCRIPTOR(device):  80 06 00 01 00 00 40 00
        # so any other value there is RX corruption caught red-handed, with the
        # expected bytes as a built-in control.
        #
        # UART on R3, 115200 8N1:
        #   SF=xxxx SU=xxxx DA=xxxx AK=xxxx RE=xxxx TV=xxxx LS=x S=xx xx ...
        if os.environ.get("USB_PROBE") == "1":
            uart = platform.request("uart_tx", 0)

            def _sat(nm):
                return Signal(16, name=nm)

            def _bump(sig, cond):
                with m.If(cond & (sig != 0xFFFF)):
                    m.d.usb += sig.eq(sig + 1)

            p_sf, p_su, p_da = _sat("p_sf"), _sat("p_su"), _sat("p_da")
            p_ak, p_re, p_tv = _sat("p_ak"), _sat("p_re"), _sat("p_tv")

            # ---- packet framing on the UTMI RX bus --------------------------
            ra_d = Signal(); m.d.usb += ra_d.eq(utmi.rx_active)
            ra_fall = Signal()
            m.d.comb += ra_fall.eq(~utmi.rx_active & ra_d)

            byte_ok = Signal()
            m.d.comb += byte_ok.eq(utmi.rx_active & utmi.rx_valid)

            # byte index within the current packet; cleared at end of packet so
            # that at the START of the next one it already reads 0.
            bidx = Signal(4)
            first = Signal()
            m.d.comb += first.eq(byte_ok & (bidx == 0))
            with m.If(byte_ok & (bidx != 15)):
                m.d.usb += bidx.eq(bidx + 1)
            with m.If(ra_fall):
                m.d.usb += bidx.eq(0)

            pid = Signal(8)
            with m.If(first):
                m.d.usb += pid.eq(utmi.rx_data)

            _bump(p_sf, first & (utmi.rx_data == 0xA5))   # SOF   token
            _bump(p_su, first & (utmi.rx_data == 0x2D))   # SETUP token
            _bump(p_da, first & ((utmi.rx_data == 0xC3)   # DATA0
                                 | (utmi.rx_data == 0x4B)))  # DATA1
            _bump(p_ak, first & (utmi.rx_data == 0xD2))   # ACK from host
            _bump(p_re, utmi.rx_active & utmi.rx_error)

            tv_d = Signal(); m.d.usb += tv_d.eq(utmi.tx_valid)
            _bump(p_tv, utmi.tx_valid & ~tv_d)

            # ---- first SETUP payload, with its own expected value as control -
            armed    = Signal()
            captured = Signal()
            sb = Array([Signal(8, name=f"p_sb{i}") for i in range(8)])
            with m.If(ra_fall & (pid == 0x2D) & ~captured):
                m.d.usb += armed.eq(1)
            with m.If(armed & ~captured):
                with m.If(byte_ok & (bidx >= 1) & (bidx <= 8)):
                    m.d.usb += sb[bidx - 1].eq(utmi.rx_data)
                with m.If(ra_fall & (bidx >= 9)):
                    m.d.usb += [captured.eq(1), armed.eq(0)]

            # ---- snapshot usb -> sync ---------------------------------------
            p_srcs = [p_sf, p_su, p_da, p_ak, p_re, p_tv,
                      utmi.line_state] + [sb[i] for i in range(8)]
            p_hold = [Signal(len(s), name=f"p_h{i}") for i, s in enumerate(p_srcs)]
            p_snap = Signal(22)
            m.d.usb += p_snap.eq(p_snap + 1)
            with m.If(p_snap == 0):
                m.d.usb += [h.eq(s) for h, s in zip(p_hold, p_srcs)]

            def _s2(src):
                a = Signal(len(src)); b = Signal(len(src))
                m.d.sync += [a.eq(src), b.eq(a)]
                return b
            shown = [_s2(h) for h in p_hold]

            # ---- 115200 8N1 UART in the crystal-clean sync domain ------------
            BAUD_DIV = 521                       # 60 MHz sync / 115200
            baud_cnt = Signal(range(BAUD_DIV)); baud_tick = Signal()
            m.d.sync += baud_cnt.eq(baud_cnt - 1)
            with m.If(baud_cnt == 0):
                m.d.sync += baud_cnt.eq(BAUD_DIV - 1)
            m.d.comb += baud_tick.eq(baud_cnt == 0)

            tx_shift = Signal(10, reset=0x3FF); tx_bits = Signal(range(11))
            tx_busy = Signal(); tx_byte = Signal(8); tx_start = Signal()
            m.d.comb += uart.o.eq(tx_shift[0])
            with m.If(tx_start & ~tx_busy):
                m.d.sync += [tx_shift.eq(Cat(C(0, 1), tx_byte, C(1, 1))),
                             tx_bits.eq(10), tx_busy.eq(1)]
            with m.Elif(tx_busy & baud_tick):
                m.d.sync += [tx_shift.eq(Cat(tx_shift[1:], C(1, 1))),
                             tx_bits.eq(tx_bits - 1)]
                with m.If(tx_bits == 1):
                    m.d.sync += tx_busy.eq(0)

            def _hexd(nib):
                return Mux(nib < 10, nib + ord('0'), nib + (ord('A') - 10))

            def _h16(v):
                return [_hexd(v[12:16]), _hexd(v[8:12]),
                        _hexd(v[4:8]), _hexd(v[0:4])]

            def _h8(v):
                return [_hexd(v[4:8]), _hexd(v[0:4])]

            parts = []
            for lbl, v in zip(("SF", "SU", "DA", "AK", "RE", "TV"), shown[:6]):
                parts += [C(ord(lbl[0]), 8), C(ord(lbl[1]), 8), C(ord('='), 8)]
                parts += _h16(v) + [C(ord(' '), 8)]
            parts += [C(ord('L'), 8), C(ord('S'), 8), C(ord('='), 8),
                      _hexd(shown[6]), C(ord(' '), 8)]
            parts += [C(ord('S'), 8), C(ord('='), 8)]
            for v in shown[7:15]:
                parts += _h8(v) + [C(ord(' '), 8)]
            parts += [C(ord('\r'), 8), C(ord('\n'), 8)]

            p_msg = Array(parts)
            N = len(parts)
            sending = Signal(); throttle = Signal(26); pos = Signal(range(N + 1))
            m.d.sync += throttle.eq(throttle + 1)
            with m.If((throttle == 0) & ~sending):
                m.d.sync += [sending.eq(1), pos.eq(0)]
            m.d.sync += tx_start.eq(0)
            with m.If(sending & ~tx_busy & ~tx_start):
                with m.If(pos == N):
                    m.d.sync += sending.eq(0)
                with m.Else():
                    m.d.sync += [tx_byte.eq(p_msg[pos]), tx_start.eq(1),
                                 pos.eq(pos + 1)]

        return m


if __name__ == "__main__":
    top_level_cli(USBLoopbackUTMI)
