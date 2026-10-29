#!/usr/bin/env python3
#
# PER-PIN DATA-LINE DECAY, HIGH RESOLUTION — rank the 8 pins, find the outlier.
#
# The 25 MHz version of this test (ulpi_pindecay_top.py) resolved only 40 ns per
# count and returned 16-20 for all eight pins at position B vs 0-1 at position A.
# That is too coarse to tell the pins apart, yet it already hinted that D0 (109)
# and D7 (123) -- the two ENDS of the bundle -- sit consistently higher.
#
# This version counts in the CAR's 200 MHz "fast" domain: 5 ns per count, 8x
# finer, so ~128 counts for a 640 ns decay and a few-percent difference between
# pins becomes visible.
#
# Same method: drive all 8 pins HIGH, release to high-Z, and time each pin
# independently until the FPGA input buffer reads it LOW (the USB3300 holds
# pull-downs on the ULPI data bus). All eight are measured in the SAME pass
# under identical conditions, so the seven good pins are the control for the
# eighth -- the built-in-control pattern, no separate A build needed to spot an
# outlier (position A still gives the absolute scale if wanted).
#
# KNOWN BLIND SPOT: this measures the FPGA PAD. A RESISTIVE contact or short
# downstream still lets our strong driver win at the pad, so a few-hundred-ohm
# fault can hide. Treat a flat result as "no hard fault", not "all good".
#
# UART (R3, 115200 8N1):
#     H0=xxxx H1=xxxx ... H7=xxxx
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Mux, Array,
                      ClockSignal)


class UlpiPinDecayHR(Elaboratable):
    CHARGE   = 4096                      # fast-domain cycles driven high
    CAP      = 0xFFFF
    BAUD_DIV = 521                       # 60 MHz sync / 115200

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi_clk.i,
            usb_phase=float(_os.environ.get("USB_PHASE", "0")))

        m.d.comb += [ulpi.rst.o.eq(0), ulpi.stp.o.eq(0)]

        charging = Signal()
        m.d.comb += [ulpi.data.o.eq(0xFF), ulpi.data.oe.eq(charging)]

        cnt  = Signal(range(self.CHARGE + 1))
        dec  = Signal(16)
        done = Array([Signal(name=f"dn{i}") for i in range(8)])
        res  = Array([Signal(16, name=f"rs{i}") for i in range(8)])
        rep  = Array([Signal(16, name=f"rp{i}") for i in range(8)])

        with m.FSM(domain="fast"):
            with m.State("CHARGE"):
                m.d.comb += charging.eq(1)
                m.d.fast += cnt.eq(cnt + 1)
                with m.If(cnt == self.CHARGE - 1):
                    m.d.fast += [cnt.eq(0), dec.eq(0)]
                    for i in range(8):
                        m.d.fast += [done[i].eq(0), res[i].eq(self.CAP)]
                    m.next = "DECAY"
            with m.State("DECAY"):
                m.d.comb += charging.eq(0)
                m.d.fast += dec.eq(dec + 1)
                for i in range(8):
                    with m.If(~done[i] & ~ulpi.data.i[i]):
                        m.d.fast += [done[i].eq(1), res[i].eq(dec)]
                allz = Signal()
                m.d.comb += allz.eq(Cat(*[done[i] for i in range(8)]).all())
                with m.If(allz | (dec == self.CAP)):
                    for i in range(8):
                        m.d.fast += rep[i].eq(res[i])
                    m.next = "CHARGE"

        # ---- snapshot fast -> sync, then UART ------------------------------
        snap = Signal(22)
        hold = Array([Signal(16, name=f"hd{i}") for i in range(8)])
        m.d.fast += snap.eq(snap + 1)
        with m.If(snap == 0):
            for i in range(8):
                m.d.fast += hold[i].eq(rep[i])

        def s2(src, n):
            a = Signal(n); b = Signal(n)
            m.d.sync += [a.eq(src), b.eq(a)]
            return b
        shown = [s2(hold[i], 16) for i in range(8)]

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
            v = shown[i]
            parts += [Const(ord('H'), 8), Const(ord('0') + i, 8), Const(ord('='), 8),
                      hexd(v[12:16]), hexd(v[8:12]), hexd(v[4:8]), hexd(v[0:4]),
                      Const(ord(' '), 8)]
        parts += [Const(ord('\r'), 8), Const(ord('\n'), 8)]

        N = len(parts)
        msg = Array([Signal(8, name=f"hm{i}") for i in range(N)])
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
                m.d.sync += [tx_byte.eq(msg[pos]), tx_start.eq(1), pos.eq(pos + 1)]

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
