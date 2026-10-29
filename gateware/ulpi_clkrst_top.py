#!/usr/bin/env python3
#
# IS THE CLOCK ON SODIMM 87 ACTUALLY PHY-B's CLKOUT?
#
# Never tested.  The clock was hard-rewired to SODIMM 87 by hand, and all we
# have ever shown is that SOME 60 MHz lands there.  If that wire is picking up
# PHY-A's CLKOUT instead of PHY-B's, then the FPGA talks to PHY-B while sampling
# with an unrelated 60 MHz clock -- which would fit every symptom: our TXCMD is
# still accepted (our data sits stable long enough for PHY-B's own clock to
# sample it) but the returned DIR/data are captured with the wrong clock and
# never resolve, and DIR shows up as narrow one-cycle pulses.
#
# The USB3300 stops CLKOUT while RESET is asserted (active HIGH), so holding
# PHY-B in reset must kill the clock on 87 IF it is PHY-B's.  This is a
# BUILT-IN DIFFERENTIAL: one bitstream toggles RESET as a slow square and keeps
# two separate counters of ULPI-clock cycles, one per reset state.  Everything
# is on the 25 MHz crystal via BUFG, PLL-free, so the instrument cannot freeze
# when the measured clock stops (the failure that would otherwise fake a pass).
#
#     L=01312D00 H=00000000 D=xxxxxxxx
#      |          |          `- DIR-high cycles, cross-check vs rstgate
#      |          `- ULPI clock cycles counted while RESET was HIGH
#      `- ULPI clock cycles counted while RESET was LOW
#
# L large, H == 0  -> the clock on 87 IS PHY-B's CLKOUT.  Expected.
# L ~= H  (both large) -> *** the clock on 87 is NOT PHY-B's ***, the rewired
#                         lead is picking up another source (PHY-A) and the
#                         whole usb domain is running on the wrong clock.
# L == 0               -> no clock at all; re-check the lead before anything.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockDomain,
                      ClockSignal, Cat, Const, Mux, Array)


class UlpiClkRst(Elaboratable):
    REFHZ    = 25_000_000
    BAUD_DIV = REFHZ // 115200
    WINDOW   = 24               # measure over 2**24 crystal cycles (~0.67 s)
    RSTBIT   = 21               # RESET square wave = crystal / 2**22 (~3 Hz)

    def elaborate(self, platform):
        m = Module()
        clk25    = platform.request("clk25", 0)
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        m.domains += ClockDomain("sync")
        refbuf = Signal()
        m.submodules.refbufg = Instance("BUFG", i_I=clk25.i, o_O=refbuf)
        m.d.comb += ClockSignal("sync").eq(refbuf)

        # ---- RESET square wave, crystal domain so it runs regardless --------
        tick = Signal(self.WINDOW + 1)
        m.d.sync += tick.eq(tick + 1)
        in_rst = Signal()
        m.d.comb += in_rst.eq(tick[self.RSTBIT])
        m.d.comb += [ulpi.rst.o.eq(in_rst), ulpi.stp.o.eq(0)]
        # ULPI wants the link driving the bus whenever DIR is low
        m.d.comb += [ulpi.data.o.eq(0x00), ulpi.data.oe.eq(~ulpi.dir.i)]

        # ---- measured domain = the ULPI clock pin, own BUFG ------------------
        m.domains += ClockDomain("meas")
        measbuf = Signal()
        m.submodules.measbufg = Instance("BUFG", i_I=ulpi_clk.i, o_O=measbuf)
        m.d.comb += ClockSignal("meas").eq(measbuf)

        # sync the reset state into the measured domain (2 FF)
        r1 = Signal(); rst_m = Signal()
        m.d.meas += [r1.eq(in_rst), rst_m.eq(r1)]
        cnt_lo = Signal(32)        # clock cycles seen while RESET low
        cnt_hi = Signal(32)        # clock cycles seen while RESET high
        with m.If(rst_m):
            m.d.meas += cnt_hi.eq(cnt_hi + 1)
        with m.Else():
            m.d.meas += cnt_lo.eq(cnt_lo + 1)

        # DIR-high census, also in the measured domain
        cnt_dir = Signal(32)
        with m.If(ulpi.dir.i):
            m.d.meas += cnt_dir.eq(cnt_dir + 1)

        # ---- latch all three once per window, from the crystal domain -------
        lo = Signal(32); hi = Signal(32); dr = Signal(32)
        pending = Signal(); settle = Signal(range(2048))
        top_d = Signal()
        m.d.sync += top_d.eq(tick[self.WINDOW])
        with m.If(tick[self.WINDOW] & ~top_d):
            m.d.sync += [settle.eq(2047), pending.eq(1)]
        with m.Elif(settle != 0):
            m.d.sync += settle.eq(settle - 1)
            with m.If(settle == 1):
                m.d.sync += [lo.eq(cnt_lo), hi.eq(cnt_hi), dr.eq(cnt_dir),
                             pending.eq(0)]

        # ---- UART 8N1 --------------------------------------------------------
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

        def hexw(sig):
            return [hexd(sig[4 * (7 - i):4 * (7 - i) + 4]) for i in range(8)]

        chars = ([Const(ord('L'), 8), Const(ord('='), 8)] + hexw(lo)
                 + [Const(ord(' '), 8), Const(ord('H'), 8), Const(ord('='), 8)]
                 + hexw(hi)
                 + [Const(ord(' '), 8), Const(ord('D'), 8), Const(ord('='), 8)]
                 + hexw(dr)
                 + [Const(ord('\r'), 8), Const(ord('\n'), 8)])
        N = len(chars)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
        pend_d = Signal(); m.d.sync += pend_d.eq(pending)
        arm = Signal()
        with m.If(pend_d & ~pending):
            m.d.sync += [msg[i].eq(chars[i]) for i in range(N)]
            m.d.sync += arm.eq(1)
        pidx = Signal(range(N + 1))
        m.d.sync += tx_start.eq(0)
        with m.If(arm & ~tx_busy & ~tx_start):
            with m.If(pidx == N):
                m.d.sync += [pidx.eq(0), arm.eq(0)]
            with m.Else():
                m.d.sync += [tx_byte.eq(msg[pidx]), tx_start.eq(1),
                             pidx.eq(pidx + 1)]

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
