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
        ulpi_data_drive = Signal(8)
        m.d.comb += [
            ulpi.data.o .eq(ulpi_data_drive),
            ulpi.data.oe.eq(~ulpi.dir.i),
        ]

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

        descriptors     = USBDescriptors(use_ila=False, ila_max_packet_size=512)
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

        return m


if __name__ == "__main__":
    top_level_cli(USBLoopbackUTMI)
