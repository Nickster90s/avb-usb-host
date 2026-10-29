#!/usr/bin/env python3
#
# RAW ULPI bus monitor — observe the PHY's autonomous behaviour, drive NOTHING.
#
# The reg-read diag returns a constant 0xC0 (== the REGR read command the LINK
# drives). That means the bus never turns around: the PHY is not asserting NXT
# to accept the command / DIR to take the bus, OR the FPGA can't sample those
# lines. This monitor settles it. It TRISTATES our data bus (oe=0), drives
# STP=0, releases RST, and just WATCHES DIR / NXT / the data bus in the ULPI
# clock domain (the PLL, which we already proved locks on W19). It prints once
# per ~0.25 s on the CH347 UART (R3, 115200, 8N1):
#
#     D=11 N=01 R=xx
#
#   D (DIR):  bit1 = DIR was seen HIGH,  bit0 = DIR was seen LOW, in the window.
#             11 = DIR TOGGLES  -> the PHY is alive and asserting periodic
#                  idle RXCMDs, and the DIR line (Y18) is received OK.
#             10 = DIR stuck HIGH,  01 = DIR stuck LOW  -> DIR line dead / PHY
#                  not running / bank-14 input dead.
#   N (NXT):  same encoding for NXT (Y19). With us driving no commands, NXT
#             normally sits LOW (01); if it's stuck HIGH (10) that's a fault.
#   R:        data-bus byte captured while DIR was high (the RXCMD the PHY
#             drives). Should be a plausible, VARYING RXCMD if RX works.
#
# Decisive:
#   D=11  -> DIR/PHY are fine; the reg-read failure is RX sampling phase or the
#            command-accept (NXT) path -> next step is the phase eye-scan.
#   D=10/01 -> DIR line itself is the fault (wiring/pin/bank) -> no phase helps.
#
import os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Mux, Array,
                      ClockSignal)


class UlpiDirEdge(Elaboratable):
    BAUD_DIV = 521               # 60 MHz sync / 115200

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        # usb domain = the ULPI 60 MHz via the PLL (proven to lock); sync =
        # 60 MHz off the 25 MHz crystal (reliable) for the UART.
        m.submodules.car = platform.clock_domain_generator(ulpi_clk_pin=ulpi_clk.i)

        # Observe only (tristate data, STP low). RST: give the PHY an explicit
        # reset PULSE — assert (active-HIGH) for ~20 ms after config, then
        # release. If the PHY was stuck, a clean reset edge should wake it and
        # DIR should start toggling. sync = 60 MHz off the crystal PLL.
        rst_done = Signal(); rst_cnt = Signal(21)
        with m.If(~rst_done):
            m.d.sync += rst_cnt.eq(rst_cnt + 1)
            with m.If(rst_cnt == 1_200_000):        # ~20 ms at 60 MHz
                m.d.sync += rst_done.eq(1)
        m.d.comb += [ulpi.rst.o.eq(~rst_done),      # HIGH during pulse, then LOW
                     ulpi.data.oe.eq(0),
                     ulpi.stp.o.eq(0)]

        # --- watch DIR / NXT / data in the ULPI clock domain ---
        dh = Signal(); dl = Signal(); nh = Signal(); nl = Signal()
        snap = Signal(8)
        wcnt = Signal(24)
        m.d.usb += wcnt.eq(wcnt + 1)
        with m.If(wcnt == 0):
            m.d.usb += [dh.eq(0), dl.eq(0), nh.eq(0), nl.eq(0)]
        with m.Else():
            m.d.usb += [dh.eq(dh | ulpi.dir.i), dl.eq(dl | ~ulpi.dir.i),
                        nh.eq(nh | ulpi.nxt.i), nl.eq(nl | ~ulpi.nxt.i)]
        with m.If(ulpi.dir.i):
            m.d.usb += snap.eq(ulpi.data.i)

        # ---- IS DIR REAL, OR A FLOATING PIN READING NOISE? ---------------
        # A genuine USB3300 RxCmd is a handful of 2-3 cycle DIR assertions per
        # window. A floating CMOS input sits high ~50% of the time and toggles
        # millions of times. So COUNT, don't just OR:
        #   DH  = cycles DIR was high in the window (max 0xFFFFFF = 16.7M)
        #   DE  = DIR rising edges in the window
        # 16.7M cycles/window, so  DH ~ 800000+ => noise/floating (OPEN)
        #                          DH ~ tens    => real RxCmd pulses (CONNECTED)
        dhi = Signal(24); dedge = Signal(24)
        nhi = Signal(24)
        dir_q = Signal()
        m.d.usb += dir_q.eq(ulpi.dir.i)
        with m.If(wcnt == 0):
            m.d.usb += [dhi.eq(0), dedge.eq(0), nhi.eq(0)]
        with m.Else():
            with m.If(ulpi.dir.i):
                m.d.usb += dhi.eq(dhi + 1)
            with m.If(ulpi.dir.i & ~dir_q):
                m.d.usb += dedge.eq(dedge + 1)
            with m.If(ulpi.nxt.i):
                m.d.usb += nhi.eq(nhi + 1)

        # latch the window result at wrap (values stable just before reset)
        dstat = Signal(2); nstat = Signal(2); rsnap = Signal(8)
        rdhi = Signal(24); rdedge = Signal(24); rnhi = Signal(24)
        with m.If(wcnt == 0):
            m.d.usb += [dstat.eq(Cat(dl, dh)), nstat.eq(Cat(nl, nh)),
                        rsnap.eq(snap), rdhi.eq(dhi), rdedge.eq(dedge),
                        rnhi.eq(nhi)]

        # --- CDC usb->sync (2-FF) ---
        def sync2(src, n):
            a = Signal(n); b = Signal(n)
            m.d.sync += [a.eq(src), b.eq(a)]
            return b
        ds = sync2(dstat, 2); ns = sync2(nstat, 2); rs = sync2(rsnap, 8)
        os_ = sync2(rdhi, 24); an = sync2(rdedge, 24); al = sync2(rnhi, 24)

        # --- UART TX (8N1) in sync domain ---
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
        chars = ([ord('D'), ord('='), hexd(ds[1:2]), hexd(ds[0:1]),
                  ord(' '), ord('N'), ord('='), hexd(ns[1:2]), hexd(ns[0:1]),
                  ord(' '), ord('R'), ord('='), hexd(rs[4:8]), hexd(rs[0:4]),
                  ord(' '), ord('D'), ord('H'), ord('='),
                  hexd(os_[20:24]), hexd(os_[16:20]), hexd(os_[12:16]),
                  hexd(os_[8:12]), hexd(os_[4:8]), hexd(os_[0:4]),
                  ord(' '), ord('D'), ord('E'), ord('='),
                  hexd(an[20:24]), hexd(an[16:20]), hexd(an[12:16]),
                  hexd(an[8:12]), hexd(an[4:8]), hexd(an[0:4]),
                  ord(' '), ord('N'), ord('H'), ord('='),
                  hexd(al[20:24]), hexd(al[16:20]), hexd(al[12:16]),
                  hexd(al[8:12]), hexd(al[4:8]), hexd(al[0:4]),
                  ord('\r'), ord('\n')])
        N = len(chars)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
        throttle = Signal(24); latched = Signal()
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

        # heartbeat LED off the sync (crystal-derived) domain
        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
