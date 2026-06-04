#
# Phase-locked I2S transmitter for the local no-MCLK PCM5102A monitor.
#
# Amaranth port of avb-aes3 i2s_clean.py CleanI2STx (v4, sim-bit-exact + HW
# verified clean). Root problem it solves: amlib's I2STransmitter runs in the
# USB domain and treats BCK as an ASYNC input (resyncs it), so DOUT is generated
# in the usb clock domain while the DAC samples it on the cd_audio BCK edge — two
# asynchronous clocks. Their phase drifts and periodically the DOUT transition
# lands on the DAC's BCK sampling edge -> corrupted bit -> constant harsh crackle.
#
# Fix: generate BCK, LRCK and DOUT from ONE counter in a SINGLE clock domain
# (cd_audio). Instantiate under DomainRenamer("audio") so that domain is cd_audio
# (12.245 MHz = 256*fs). BCK = audio/4, LRCK = audio/256 -> exactly 32 BCK per
# channel, zero edge jitter, and DOUT is phase-locked to BCK (no CDC skew).
#
# I2S Philips, 24-bit, MSB first, 1-bit delay, LRCK low = left. 32-BCK slot per
# channel: bit0 = delay 0; bits 1..24 = data MSB..LSB; bits 25..31 = pad 0.
# Drive the DAC pins DIRECTLY from bck/lrck/dout (NO inversion — DOUT is already
# updated on the BCK falling edge, ready for the DAC's rising-edge sampling).
#
from amaranth import Module, Signal, Elaboratable, Cat, C


class CleanI2STx(Elaboratable):
    def __init__(self):
        self.bck         = Signal()
        self.lrck        = Signal()
        self.dout        = Signal()
        self.audio_l     = Signal(24)
        self.audio_r     = Signal(24)
        self.audio_valid = Signal()
        self.audio_ready = Signal()
        self.underrun    = Signal()    # pulses when a frame was needed but FIFO empty

    def elaborate(self, platform):
        m = Module()

        # 256 cd_audio cycles per stereo frame.
        cnt = Signal(8)
        m.d.sync += cnt.eq(cnt + 1)
        m.d.comb += [
            self.bck .eq(cnt[1]),   # audio/4   = 3.06 MHz
            self.lrck.eq(cnt[7]),   # audio/256 = 47.8 kHz, low = left
        ]

        shift_l = Signal(24)
        shift_r = Signal(24)
        sr      = Signal(32)

        def word(d):
            # sr[31] sent first: [31]=delay0, [30:7]=data MSB-first, [6:0]=pad0
            return Cat(C(0, 7), d, C(0, 1))

        # Latch the next frame's L+R near the end of the R slot (after R's data,
        # before the next L) and pop the FIFO there — so both channels of a frame
        # come from the SAME FIFO entry (no inter-channel skew). On underrun keep
        # the previous shift_l/shift_r -> repeat the last frame (inaudible).
        prep = Signal()
        m.d.comb += prep.eq(cnt == 250)
        m.d.comb += self.audio_ready.eq(prep & self.audio_valid)
        m.d.comb += self.underrun.eq(prep & ~self.audio_valid)
        with m.If(prep & self.audio_valid):
            m.d.sync += [
                shift_l.eq(self.audio_l),
                shift_r.eq(self.audio_r),
            ]

        bck_fall = Signal()
        m.d.comb += bck_fall.eq(cnt[0:2] == 0)     # cnt % 4 == 0
        with m.If(cnt == 0):                        # LEFT slot start
            m.d.sync += [
                sr.eq(Cat(C(0, 1), word(shift_l)[0:31])),
                self.dout.eq(0),
            ]
        with m.Elif(cnt == 128):                    # RIGHT slot start
            m.d.sync += [
                sr.eq(Cat(C(0, 1), word(shift_r)[0:31])),
                self.dout.eq(0),
            ]
        with m.Elif(bck_fall):
            m.d.sync += [
                self.dout.eq(sr[31]),
                sr.eq(Cat(C(0, 1), sr[0:31])),
            ]

        return m
