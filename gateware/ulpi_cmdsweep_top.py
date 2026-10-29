#!/usr/bin/env python3
#
# COMMAND-BYTE SWEEP — find the byte that the PHY accepts as a TXCMD.
#
# State of play at position B (2026-10-28): CLK, RST, DIR, NXT and STP are all
# PROVEN connected, the D[7:0] pads are alive and the PHY senses bus activity --
# yet a 64-cycle trace showed NXT NEVER asserting for our REGR command 0xC0.
# The remaining explanation is that D[7:0] are PHYSICALLY PERMUTED between the
# FPGA and the PHY, so 0xC0 arrives as some other byte and is not a valid
# command. (Precedent: USB_SWAP_D67 exists in this very platform to cancel a
# physical D6/D7 cross on the other breakout, and the platform notes a wrong
# D-order is SILENT.)
#
# If the mapping is a permutation P, we need the byte B with P(B) = 0xC0. That
# is only 256 candidates, so brute-force it in gateware: drive each candidate
# for a dwell, count NXT assertions, and report the winner. Any NXT at all is
# the breakthrough -- it proves the data path works and the fault is ordering.
#
# STP is held LOW throughout (STP high aborts, proven to silence the PHY).
#
# UART (115200): BEST=xx CNT=xxxx HITS=xx CUR=xx
#   BEST = candidate byte with the most NXT assertions so far
#   CNT  = that candidate's NXT count
#   HITS = how many distinct candidates produced ANY NXT
#   CUR  = candidate currently being driven (shows the sweep is alive)
# BEST=C0 would mean no permutation; anything else names the scrambled encoding.
# HITS=00 after a full pass means the PHY accepts NOTHING -> not a permutation.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Mux, Array,
                      ClockSignal)


class UlpiCmdSweep(Elaboratable):
    DWELL_BITS = 12                     # 4096 ULPI cycles per candidate
    BAUD_DIV   = 521
    CYCLES_1MS = 60_000

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi_clk.i,
            usb_phase=float(_os.environ.get("USB_PHASE", "0")))

        cand = Signal(8)
        m.d.comb += [ulpi.rst.o.eq(0),            # PHY running
                     ulpi.stp.o.eq(0),            # never abort
                     ulpi.data.o.eq(cand),
                     ulpi.data.oe.eq(~ulpi.dir.i)]

        startup = Signal(range(self.CYCLES_1MS + 1)); ready = Signal()
        m.d.usb += startup.eq(Mux(ready, startup, startup + 1))
        with m.If(startup == self.CYCLES_1MS):
            m.d.usb += ready.eq(1)

        dwell = Signal(self.DWELL_BITS)
        nxt_q = Signal()
        m.d.usb += nxt_q.eq(ulpi.nxt.i)
        nrise = Signal()
        m.d.comb += nrise.eq(ulpi.nxt.i & ~nxt_q)

        cnt   = Signal(16)
        best  = Signal(8); bestc = Signal(16); hits = Signal(8)

        with m.If(ready):
            m.d.usb += dwell.eq(dwell + 1)
            with m.If(nrise & (cnt != 0xFFFF)):
                m.d.usb += cnt.eq(cnt + 1)
            with m.If(dwell == (1 << self.DWELL_BITS) - 1):
                # end of this candidate's dwell: score it, then advance
                with m.If(cnt > bestc):
                    m.d.usb += [bestc.eq(cnt), best.eq(cand)]
                with m.If((cnt != 0) & (hits != 0xFF)):
                    m.d.usb += hits.eq(hits + 1)
                m.d.usb += [cnt.eq(0), cand.eq(cand + 1)]

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

        # snapshot the usb-domain values on a slow strobe, then 2-FF them
        snap = Signal(22)
        hb_best = Signal(8); hb_cnt = Signal(16); hb_hits = Signal(8)
        hb_cur  = Signal(8)
        m.d.usb += snap.eq(snap + 1)
        with m.If(snap == 0):
            m.d.usb += [hb_best.eq(best), hb_cnt.eq(bestc),
                        hb_hits.eq(hits), hb_cur.eq(cand)]

        def s2(src, n):
            a = Signal(n); b = Signal(n)
            m.d.sync += [a.eq(src), b.eq(a)]
            return b
        b_s = s2(hb_best, 8); c_s = s2(hb_cnt, 16)
        h_s = s2(hb_hits, 8); u_s = s2(hb_cur, 8)

        def hexd(nib):
            return Mux(nib < 10, nib + ord('0'), nib + (ord('A') - 10))
        def lit(t):
            return [Const(ord(c), 8) for c in t]
        def hx(v, n):
            return [hexd(v[4*(i-1):4*i]) for i in range(n, 0, -1)]

        parts = (lit("BEST=") + hx(b_s, 2) + lit(" CNT=") + hx(c_s, 4)
                 + lit(" HITS=") + hx(h_s, 2) + lit(" CUR=") + hx(u_s, 2)
                 + lit("\r\n"))
        N = len(parts)
        msg = Array([Signal(8, name=f"cm{i}") for i in range(N)])
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
