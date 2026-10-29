#!/usr/bin/env python3
#
# PER-PIN DATA-LINE TEST — which of D0..D7 is not reaching the PHY?
#
# METHOD (needs no cooperation from the PHY, no register read, and no PULLUP
# attribute -- openXC7 silently drops those):
#   The USB3300 holds internal PULL-DOWNS on the ULPI data bus. That is why an
#   idle bus reads 0x00. So, per pin:
#       1. drive it HIGH hard for a while  (charge the net)
#       2. release it to high-Z
#       3. count cycles until the FPGA's own input buffer reads it LOW
#   CONNECTED pin -> the PHY's pull-down discharges trace+PHY capacitance in
#                    microseconds -> a SMALL count (tens of cycles)
#   OPEN pin      -> nothing pulls it down; only pA-nA leakage on a tiny stub
#                    -> the charge persists for ms -> count SATURATES at FFFF
#   A resistive/marginal contact lands in between, and the ratio between pins is
#   what matters, so all 8 are measured in the SAME pass under identical
#   conditions.
#
# Everything runs from the 25 MHz board crystal on an explicit BUFG: no PLL, so
# the test works even if the ULPI clock is dead. 40 ns per count.
#
# The PHY is left OUT of reset (RESET is active HIGH -> drive 0) because the
# pull-downs must be active. The PHY's own 1-cycle DIR glitch train can briefly
# make it drive the bus, which would corrupt a single trial, so every trial is
# reported and many are collected -- read the DISTRIBUTION, not one line.
#
# UART (R3, 115200 8N1), one line per trial:
#     D0=xxxx D1=xxxx D2=xxxx D3=xxxx D4=xxxx D5=xxxx D6=xxxx D7=xxxx
#
# INTERPRETATION
#   all eight similar and SMALL          -> all 8 connected (compare vs pos. A)
#   one or more SATURATED at FFFF        -> those pins are OPEN  <-- the answer
#   one much larger than the others      -> that pin is resistive/marginal
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockDomain,
                      ClockSignal, Cat, Const, Mux, Array)


class UlpiPinDecay(Elaboratable):
    REFHZ     = 25_000_000
    BAUD_DIV  = REFHZ // 115200          # 217
    CHARGE    = 2048                     # cycles driven high (~82 us)
    CAP       = 0xFFFF                   # decay counter ceiling (~2.6 ms)

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

        # PHY running (pull-downs active), never abort, never assert STP
        m.d.comb += [ulpi.rst.o.eq(0), ulpi.stp.o.eq(0)]

        charging = Signal()
        m.d.comb += [ulpi.data.o.eq(0xFF),
                     ulpi.data.oe.eq(charging)]

        cnt   = Signal(range(self.CHARGE + 1))
        dec   = Signal(16)
        done  = Array([Signal(name=f"done{i}") for i in range(8)])
        res   = Array([Signal(16, name=f"res{i}") for i in range(8)])
        rep   = Array([Signal(16, name=f"rep{i}") for i in range(8)])

        with m.FSM(domain="sync"):
            with m.State("CHARGE"):
                m.d.comb += charging.eq(1)
                m.d.sync += cnt.eq(cnt + 1)
                with m.If(cnt == self.CHARGE - 1):
                    m.d.sync += [cnt.eq(0), dec.eq(0)]
                    for i in range(8):
                        m.d.sync += [done[i].eq(0), res[i].eq(self.CAP)]
                    m.next = "DECAY"

            with m.State("DECAY"):
                m.d.comb += charging.eq(0)       # release to high-Z
                m.d.sync += dec.eq(dec + 1)
                for i in range(8):
                    # first cycle this pin reads LOW, latch the elapsed count
                    with m.If(~done[i] & ~ulpi.data.i[i]):
                        m.d.sync += [done[i].eq(1), res[i].eq(dec)]
                allz = Signal()
                m.d.comb += allz.eq(Cat(*[done[i] for i in range(8)]).all())
                with m.If(allz | (dec == self.CAP)):
                    for i in range(8):
                        m.d.sync += rep[i].eq(res[i])
                    m.next = "CHARGE"

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
        parts = []
        for i in range(8):
            parts += [Const(ord('D'), 8), Const(ord('0') + i, 8),
                      Const(ord('='), 8)]
            v = rep[i]
            parts += [hexd(v[12:16]), hexd(v[8:12]), hexd(v[4:8]), hexd(v[0:4])]
            parts += [Const(ord(' '), 8)]
        parts += [Const(ord('\r'), 8), Const(ord('\n'), 8)]

        N = len(parts)
        msg = Array([Signal(8, name=f"pm{i}") for i in range(N)])
        latched = Signal(); throttle = Signal(22); pos = Signal(range(N + 1))
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
