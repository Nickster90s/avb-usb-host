#!/usr/bin/env python3
#
# WALKING-ONE SHORT MAP — WHICH data pin is bad?
#
# Every earlier test was blind to a SHORT:
#   - continuity / pindecay drove ALL 8 pins to the SAME value (0xFF), so a
#     short BETWEEN two data pins carries no current and is invisible
#   - a data pin shorted to STP is worse than invisible: driving that bit HIGH
#     asserts STP, and stpgate PROVED that STP high silences the PHY completely
#     -> every command aborts, at every clock phase, for every opcode including
#     the permutation-proof 0xFF (which drives ALL bits high). That also explains
#     the otherwise unexplained nxtprobe result "driving the data bus silences
#     the DIR glitch train".
#
# METHOD: drive exactly ONE data pin HIGH, all other seven LOW, let it settle,
# then read back all 8 data pins plus DIR and NXT. Repeat for each of the 8
# pins. Then repeat the whole sweep with STP driven HIGH instead of LOW.
#
#   HEALTHY    : driving bit i reads back exactly (1<<i)
#                -> W = 01 02 04 08 10 20 40 80
#   SHORT i<->j: driving bit i reads back (1<<i)|(1<<j)  (j lights up too)
#   i TIED LOW : driving bit i reads back 00 (or missing its own bit)
#   i <-> STP  : bit i's readback CHANGES between the STP=0 and STP=1 sweeps
#   i <-> DIR
#     or NXT   : the DIR/NXT column goes high only on that step
#
# The PHY is held IN RESET (RESET is active HIGH -> drive 1) so its data pins
# stay high-Z inputs and cannot drive the bus and corrupt a reading. This is a
# test of the WIRES, not of the PHY.
#
# UART (R3, 115200 8N1), two lines, re-printed forever:
#     A=01 02 04 08 10 20 40 80 X=00      <- STP driven LOW
#     B=01 02 04 08 10 20 40 80 X=00      <- STP driven HIGH
#   the 8 bytes are the readback for driving D0..D7 in turn;
#   X = bitmap of which steps saw DIR (bit0) or NXT (bit1) high.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockDomain,
                      ClockSignal, Cat, Const, Mux, Array)


class UlpiShortMap(Elaboratable):
    REFHZ    = 25_000_000
    BAUD_DIV = REFHZ // 115200
    SETTLE   = 1024                      # cycles per step (~41 us)

    def elaborate(self, platform):
        m = Module()
        clk25 = platform.request("clk25", 0)
        ulpi  = platform.request("ulpi", 0)
        uart  = platform.request("uart_tx", 0)
        led   = platform.request("user_led", 0)

        m.domains += ClockDomain("sync")
        refbuf = Signal()
        m.submodules += Instance("BUFG", i_I=clk25.i, o_O=refbuf)
        m.d.comb += ClockSignal("sync").eq(refbuf)

        # hold the PHY in RESET: its data pins stay high-Z, out of our way
        m.d.comb += ulpi.rst.o.eq(1)

        step  = Signal(3)                 # which data bit is driven high
        pas   = Signal()                  # 0 = STP low sweep, 1 = STP high
        tick  = Signal(range(self.SETTLE + 1))

        m.d.comb += [ulpi.stp.o.eq(pas),
                     ulpi.data.oe.eq(1),
                     ulpi.data.o.eq(1 << step)]

        resA = Array([Signal(8, name=f"a{i}") for i in range(8)])
        resB = Array([Signal(8, name=f"b{i}") for i in range(8)])
        xA   = Signal(2); xB = Signal(2)

        m.d.sync += tick.eq(tick + 1)
        with m.If(tick == self.SETTLE - 1):
            m.d.sync += tick.eq(0)
            # sample at the end of the settle window
            with m.If(~pas):
                m.d.sync += resA[step].eq(ulpi.data.i)
                with m.If(ulpi.dir.i): m.d.sync += xA[0].eq(1)
                with m.If(ulpi.nxt.i): m.d.sync += xA[1].eq(1)
            with m.Else():
                m.d.sync += resB[step].eq(ulpi.data.i)
                with m.If(ulpi.dir.i): m.d.sync += xB[0].eq(1)
                with m.If(ulpi.nxt.i): m.d.sync += xB[1].eq(1)
            m.d.sync += step.eq(step + 1)
            with m.If(step == 7):
                m.d.sync += pas.eq(~pas)

        # ---- UART ---------------------------------------------------------
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

        parts = [Const(ord('A'), 8), Const(ord('='), 8)]
        for i in range(8):
            parts += [hexd(resA[i][4:8]), hexd(resA[i][0:4]), Const(ord(' '), 8)]
        parts += [Const(ord('X'), 8), Const(ord('='), 8),
                  hexd(Const(0, 4)), hexd(xA)]
        parts += [Const(ord('\r'), 8), Const(ord('\n'), 8)]
        parts += [Const(ord('B'), 8), Const(ord('='), 8)]
        for i in range(8):
            parts += [hexd(resB[i][4:8]), hexd(resB[i][0:4]), Const(ord(' '), 8)]
        parts += [Const(ord('X'), 8), Const(ord('='), 8),
                  hexd(Const(0, 4)), hexd(xB)]
        parts += [Const(ord('\r'), 8), Const(ord('\n'), 8)]

        N = len(parts)
        msg = Array([Signal(8, name=f"sm{i}") for i in range(N)])
        latched = Signal(); throttle = Signal(23); pos = Signal(range(N + 1))
        m.d.sync += throttle.eq(throttle + 1)
        with m.If((throttle == 0) & ~latched):
            m.d.sync += [msg[i].eq(parts[i]) for i in range(N)]
            m.d.sync += latched.eq(1)
        m.d.sync += tx_start.eq(0)
        with m.If(latched & ~tx_busy & ~tx_start):
            with m.If(pos == N):
                m.d.sync += [pos.eq(0), latched.eq(0)]
            with m.Else():
                m.d.sync += [tx_byte.eq(msg[pos]), tx_start.eq(1),
                             pos.eq(pos + 1)]

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
