#!/usr/bin/env python3
#
# VARIANT: holds ULPI RST HIGH to test whether the RST wire is CONNECTED.
#   reading CHANGES vs the rst=0 baseline -> RST reaches the PHY (wire OK)
#   reading IDENTICAL to baseline        -> RST never reaches the PHY (OPEN)
# Baseline (rst=0, good module @ W4): 60,006,640 Hz, 1 Hz spread.
# W19 FREQUENCY-MEASURING clock test — how many Hz is actually coming out of
# the backup USB3300 CLKOUT into the ULPI clock pin?
#
# The plain clock-test only answers "is a clock present?" (blink rate). It
# CANNOT tell 60 MHz from 24/48 MHz — but that distinction is decisive: the
# FPGA USB PLL is configured for a 60 MHz input (x16 -> 960 MHz VCO). If the
# breakout's CLKOUT is not ~60 MHz the VCO falls outside 800-1600 MHz and the
# PLL physically CANNOT lock (solid LED) even though a clock IS present.
#
# This test is PLL-FREE: everything is clocked from the always-running 25 MHz
# board crystal via an explicit BUFG. The ULPI clock (W19) is brought in on its
# own BUFG and counted with a 1-second gate. The measured count == frequency in
# Hz, printed once per second on the CH347 UART (R3, 115200, 8N1):
#
#     W19=03938700
#
# Read the hex as a plain integer = Hz. Expected values:
#     0x03938700 = 60000000 = 60.00 MHz   <- what the PLL needs
#     0x02DC6C00 = 48000000 = 48.00 MHz
#     0x018CBA80 = 26000000 = 26.00 MHz
#     0x016E3600 = 24000000 = 24.00 MHz
#     0x00B71B00 = 12000000 = 12.00 MHz
#     0x00000000 = 0                        = no clock at W19
# Anything far from 60 MHz explains the PLL not locking.
#
import os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockDomain,
                      ClockSignal, Cat, Const, Mux, Array)


class UlpiFreqTestRstHigh(Elaboratable):
    REFHZ    = 25_000_000          # board crystal
    BAUD_DIV = REFHZ // 115200     # 217

    def elaborate(self, platform):
        m = Module()
        clk25    = platform.request("clk25", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        ulpi     = platform.request("ulpi", 0)      # release PHY reset
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        # --- sync domain = 25 MHz crystal via EXPLICIT BUFG (always runs) ---
        m.domains += ClockDomain("sync")
        refbuf = Signal()
        m.submodules += Instance("BUFG", i_I=clk25.i, o_O=refbuf)
        m.d.comb += ClockSignal("sync").eq(refbuf)

        # --- meas domain = the ULPI clock pin (W19) via EXPLICIT BUFG ---
        m.domains += ClockDomain("meas")
        measbuf = Signal()
        m.submodules += Instance("BUFG", i_I=ulpi_clk.i, o_O=measbuf)
        m.d.comb += ClockSignal("meas").eq(measbuf)

        # USB3300 reset is active-HIGH -> drive 0 to release (so CLKOUT runs).
        m.d.comb += ulpi.rst.o.eq(1)   # HOLD PHY IN RESET (active HIGH)

        # --- 1-second measurement gate, generated in the crystal domain ---
        # gate HIGH for REFHZ sync cycles (1 s), then LOW for 1 s (read+print).
        gate = Signal(reset=1)
        gcnt = Signal(range(self.REFHZ))
        with m.If(gcnt == self.REFHZ - 1):
            m.d.sync += [gcnt.eq(0), gate.eq(~gate)]
        with m.Else():
            m.d.sync += gcnt.eq(gcnt + 1)

        # --- frequency counter in the meas (W19) domain ---
        g1 = Signal(); gate_m = Signal(); gate_m_d = Signal()
        m.d.meas += [g1.eq(gate), gate_m.eq(g1), gate_m_d.eq(gate_m)]
        fcount = Signal(27)                 # up to 134 M
        with m.If(gate_m & ~gate_m_d):      # rising edge -> start fresh
            m.d.meas += fcount.eq(1)
        with m.Elif(gate_m):                # count while gate high
            m.d.meas += fcount.eq(fcount + 1)
        # when gate_m low, fcount HOLDS (frozen) -> safe to read from sync.

        # --- latch the frozen count into sync, well after gate falls ---
        gate_d = Signal(); freq = Signal(27)
        settle = Signal(range(2048)); pending = Signal()
        m.d.sync += gate_d.eq(gate)
        with m.If(gate_d & ~gate):          # gate just went low: window ended
            m.d.sync += [settle.eq(2047), pending.eq(1)]
        with m.Elif(settle != 0):
            m.d.sync += settle.eq(settle - 1)
            with m.If(settle == 1):         # ~82 us later fcount is rock-stable
                m.d.sync += [freq.eq(fcount), pending.eq(0)]

        # --- UART TX (8N1) in the sync (crystal) domain ---
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

        # --- build the message "W19=xxxxxxxx\n" once per new measurement ---
        def hexd(nib):
            return Mux(nib < 10, nib + ord('0'), nib + (ord('A') - 10))
        # 7 nibbles cover 27 bits; print 8 for alignment (top nibble = 0).
        nibs = [freq[4*i:4*i+4] for i in range(7)] + [Const(0, 4)]
        chars = ([ord('W'), ord('1'), ord('9'), ord('=')]
                 + [hexd(nibs[7 - i]) for i in range(8)]      # MSB first
                 + [ord('\r'), ord('\n')])
        N = len(chars)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])

        # latch the message when a fresh freq is ready (pending 1->0)
        pend_d = Signal(); m.d.sync += pend_d.eq(pending)
        arm = Signal()
        with m.If(pend_d & ~pending):        # freq just latched
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

        # --- heartbeat LED so you know the crystal domain is alive ---
        hb = Signal(24)
        m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])         # ~1.5 Hz blink off the 25 MHz xtal

        return m
