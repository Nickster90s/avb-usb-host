#!/usr/bin/env python3
#
# DIR / NXT CONTENTION READBACK — "is this pin really connected to the PHY?"
#
# DIR is an OUTPUT of the USB3300 and is driven by the PHY at all times. So if
# we make the FPGA pin bidirectional, drive it, and read the SAME PAD back:
#
#   CONNECTED : our HIGH fights the PHY's LOW. The pad is held below Vih by the
#               PHY, so the readback DISAGREES with what we drive -> FH > 0.
#   OPEN      : nothing fights us. Readback always AGREES -> FH = 0, FL = 0.
#
# Everything runs off the 25 MHz board crystal via an explicit BUFG, so this
# test does NOT need the ULPI clock, the PLL, or the PHY to answer a command.
# The PHY is held OUT of reset (RESET is active HIGH on the USB3300 -> drive 0)
# so that it is actively driving DIR, which is the whole point.
#
# Drive is a slow square wave (~12 kHz, 50% duty) rather than DC, to bound the
# contention current while still giving thousands of samples per window.
#
# UART (R3, 115200 8N1):
#     DFH=xx DFL=xx NFH=xx NFL=xx
#   DFH = times we drove DIR HIGH and read back LOW  (saturating at 0xFF)
#   DFL = times we drove DIR LOW  and read back HIGH
#   N** = same for NXT
# Expected: a connected, PHY-driven line gives FH=FF (we always lose when we
# push high). An open line gives DFH=00 DFL=00.
#
from amaranth import (Module, Signal, Elaboratable, Instance, ClockDomain,
                      ClockSignal, Cat, Const, Mux, Array)


class UlpiContention(Elaboratable):
    REFHZ    = 25_000_000
    BAUD_DIV = REFHZ // 115200          # 217

    def elaborate(self, platform):
        m = Module()
        clk25 = platform.request("clk25", 0)
        ctl   = platform.request("ulpi_ctl_io", 0)
        uart  = platform.request("uart_tx", 0)
        led   = platform.request("user_led", 0)

        # sync = 25 MHz crystal, explicit BUFG (always runs, no PLL)
        m.domains += ClockDomain("sync")
        refbuf = Signal()
        m.submodules += Instance("BUFG", i_I=clk25.i, o_O=refbuf)
        m.d.comb += ClockSignal("sync").eq(refbuf)

        # USB3300 RESET is active HIGH -> 0 = PHY running and driving DIR.
        m.d.comb += ctl.rst.o.eq(0)

        # ---- drive a slow square wave on both pins, always output-enabled ---
        phase = Signal(11)                       # 2048 cycles ≈ 82 us per half
        m.d.sync += phase.eq(phase + 1)
        drv = Signal()
        m.d.comb += drv.eq(phase[10])            # ~12 kHz, 50% duty
        m.d.comb += [ctl.dir.oe.eq(1), ctl.dir.o.eq(drv),
                     ctl.nxt.oe.eq(1), ctl.nxt.o.eq(drv)]

        # Sample late in each half-phase, after the pad has settled.
        sample = Signal()
        m.d.comb += sample.eq(phase[0:10] == 0x3F0)

        def counter(pad_i):
            fh = Signal(8); fl = Signal(8)
            with m.If(sample):
                with m.If(drv & ~pad_i):                 # drove HIGH, read LOW
                    with m.If(fh != 0xFF):
                        m.d.sync += fh.eq(fh + 1)
                with m.If(~drv & pad_i):                 # drove LOW, read HIGH
                    with m.If(fl != 0xFF):
                        m.d.sync += fl.eq(fl + 1)
            return fh, fl

        dfh, dfl = counter(ctl.dir.i)
        nfh, nfl = counter(ctl.nxt.i)

        # ---- UART TX (8N1) ------------------------------------------------
        baud_cnt = Signal(range(self.BAUD_DIV)); baud_tick = Signal()
        m.d.sync += baud_cnt.eq(baud_cnt - 1)
        with m.If(baud_cnt == 0):
            m.d.sync += baud_cnt.eq(self.BAUD_DIV - 1)
        m.d.comb += baud_tick.eq(baud_cnt == 0)

        tx_shift = Signal(10, reset=0x3FF); tx_bits = Signal(range(11))
        tx_busy = Signal(); tx_byte = Signal(8); tx_start = Signal()
        m.d.comb += uart.o.eq(tx_shift[0])
        with m.If(tx_start & ~tx_busy):
            m.d.sync += [tx_shift.eq(Cat(Const(0, 1), tx_byte, Const(1, 1))),
                         tx_bits.eq(10), tx_busy.eq(1)]
        with m.Elif(tx_busy & baud_tick):
            m.d.sync += [tx_shift.eq(Cat(tx_shift[1:], Const(1, 1))),
                         tx_bits.eq(tx_bits - 1)]
            with m.If(tx_bits == 1):
                m.d.sync += tx_busy.eq(0)

        def hexd(nib):
            return Mux(nib < 10, nib + ord('0'), nib + (ord('A') - 10))

        def field(name, sig):
            out = [ord(c) for c in name] + [ord('=')]
            return out + [hexd(sig[4:8]), hexd(sig[0:4]), ord(' ')]

        chars = (field("DFH", dfh) + field("DFL", dfl)
                 + field("NFH", nfh) + field("NFL", nfl)
                 + [ord('\r'), ord('\n')])
        N = len(chars)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
        throttle = Signal(23); latched = Signal()
        m.d.sync += throttle.eq(throttle + 1)
        with m.If((throttle == 0) & ~latched):
            m.d.sync += [msg[i].eq(chars[i]) for i in range(N)]
            m.d.sync += latched.eq(1)

        pidx = Signal(range(N + 1))
        m.d.sync += tx_start.eq(0)
        with m.If(latched & ~tx_busy & ~tx_start):
            with m.If(pidx == N):
                m.d.sync += [pidx.eq(0), latched.eq(0)]
            with m.Else():
                m.d.sync += [tx_byte.eq(msg[pidx]), tx_start.eq(1),
                             pidx.eq(pidx + 1)]

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
