#!/usr/bin/env python3
#
# STP VERIFICATION BY SCRATCH-REGISTER WRITE + READ-BACK.
#
# Why (2026-10-28): after the DIR<->NXT swap fix, PHY-B reads registers fine
# (TXCMD 0xC0 -> 0x24) and chirps at high speed, but USB enumeration fails with
# error -71 = EPROTO = malformed packets.  **A register READ never uses STP; a
# USB packet does** -- a ULPI transmit is terminated by asserting STP for one
# cycle.  STP is the LAST ULPI signal on this board with no independent test:
# this morning's stpgate only showed that driving SODIMM 101 high silenced bus
# activity, which several wirings would also produce.  And J5 carries STP where
# its twin J4 carries NXT, which is exactly the pattern that produced the
# DIR<->NXT transposition.
#
# A register WRITE is the cheapest transaction that REQUIRES STP:
#   TXCMD 0x96 (REGW addr 0x16) -> NXT -> data byte -> NXT -> STP for 1 cycle
#   TXCMD 0xD6 (REGR addr 0x16) -> NXT -> turnaround -> the byte comes back
# 0x16 is the USB3300 Scratch register: read/write, no side effects.  ONLY this
# address is ever written -- see the "never sweep arbitrary bytes at a ULPI PHY"
# rule, which came from a sweep that wrote ~64 registers and jammed the PHY.
#
# CAPTURE TIMING (learned the hard way): the register value does NOT appear on
# the first cycle DIR is high -- that cycle is the turnaround and the bus still
# carries OUR last driven byte.  The validated trace shows:
#     34C0 CMD  DIR=1 D=C0   <- DIR rises, still our byte
#     44C0 TURN DIR=1 D=C0
#     4424 TURN DIR=1 D=24   <- the value lands HERE, 2 cycles after DIR rose
#     6424 DATA DIR=1 D=24
# Sampling on the first DIR-high cycle returns our own TXCMD (0xC0 / 0xD6) and
# looks exactly like a dead PHY.  Waiting ONE cycle is still too early (it lands
# on 44C0).  Rather than hard-code a latency -- miscounted twice already --
# sample data_i on EVERY cycle DIR is high and keep the LAST value before DIR
# drops.  Over the real sequence that is C0, C0, 24, 24 -> 0x24, and it stays
# correct whatever the turnaround length turns out to be.
#
# Two alternating values (0x5A / 0xA5) so a stuck or never-written register
# cannot pass by luck: BOTH must read back.
#
#     RA=xx RB=xx V0=xx OK=xxxx BAD=xxxx
#
# V0 is the IN-BITSTREAM CONTROL: a plain read of register 0x00, which must
# return 0x24 on a USB3300.  Without it, RA=RB=00 is ambiguous -- 00 is equally
# what a broken READ path returns, and convicting STP on that would repeat the
# mistake this whole session kept making.
#   V0=24 and RA/RB wrong  -> reads fine, WRITES fail -> STP convicted.
#   V0=00 as well          -> this instrument's read path is broken; the test
#                             says nothing about STP.
#      |     |     `- write+readback pairs that matched / mismatched
#      |     `- what came back after writing 0xA5
#      `- what came back after writing 0x5A
#
# RA=5A RB=A5, BAD=0  -> STP works; the enumeration fault is elsewhere.
# RA==RB, or both stuck/wrong while plain reads still return 0x24
#                     -> STP is not reaching the PHY.  Convicted.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Mux, Array)

SCRATCH = 0x16
TXCMD_W = 0x80 | SCRATCH        # 0x96  REGW
TXCMD_R = 0xC0 | SCRATCH        # 0xD6  REGR


class UlpiScratch(Elaboratable):
    BAUD_DIV   = 521
    CYCLES_1MS = 60_000
    TIMEOUT    = 256

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        env = _os.environ.get("USB_PHASE", "0")
        if env == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        elif env == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(env))

        m.d.comb += ulpi.rst.o.eq(0)

        drive = Signal(8)
        stp   = Signal()
        m.d.comb += [ulpi.data.o.eq(drive),
                     ulpi.data.oe.eq(~ulpi.dir.i),
                     ulpi.stp.o.eq(stp)]
        dir_i, nxt_i, data_i = ulpi.dir.i, ulpi.nxt.i, ulpi.data.i

        startup = Signal(range(self.CYCLES_1MS + 1)); ready = Signal()
        m.d.usb += startup.eq(Mux(ready, startup, startup + 1))
        with m.If(startup == self.CYCLES_1MS):
            m.d.usb += ready.eq(1)

        which = Signal()                 # 0 -> 0x5A, 1 -> 0xA5
        val   = Signal(8)
        m.d.comb += val.eq(Mux(which, 0xA5, 0x5A))
        ra    = Signal(8); rb = Signal(8); v0 = Signal(8)
        ok    = Signal(16); bad = Signal(16)
        got   = Signal(8)
        to    = Signal(range(self.TIMEOUT + 1))

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                m.d.comb += drive.eq(0x00)
                m.d.usb += to.eq(0)
                with m.If(ready & ~dir_i):
                    m.next = "WCMD"
            with m.State("WCMD"):
                m.d.comb += drive.eq(TXCMD_W)
                m.d.usb += to.eq(to + 1)
                with m.If(dir_i):
                    m.next = "IDLE"               # PHY grabbed the bus, retry
                with m.Elif(nxt_i):
                    m.next = "WDATA"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "FAIL"
            with m.State("WDATA"):
                m.d.comb += drive.eq(val)
                m.d.usb += to.eq(to + 1)
                with m.If(nxt_i):
                    m.next = "WSTP"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "FAIL"
            with m.State("WSTP"):
                # terminate the write: one cycle of STP with the bus at 0x00
                m.d.comb += [drive.eq(0x00), stp.eq(1)]
                m.next = "RCMD"
            with m.State("RCMD"):
                m.d.comb += drive.eq(TXCMD_R)
                m.d.usb += to.eq(to + 1)
                with m.If(dir_i):
                    m.next = "RCMD"
                with m.Elif(nxt_i):
                    m.next = "RTURN"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "FAIL"
            with m.State("RTURN"):
                m.d.comb += drive.eq(0x00)
                m.d.usb += to.eq(to + 1)
                with m.If(dir_i):
                    m.next = "RHOLD"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "FAIL"
            with m.State("RHOLD"):
                # keep the newest byte for as long as the PHY owns the bus;
                # when DIR drops, `got` holds the last one = the register value
                m.d.comb += drive.eq(0x00)
                # capture ONLY while the PHY still owns the bus: an
                # unconditional capture also fires on the cycle DIR has already
                # dropped and overwrites the value with the idle bus (0x00).
                with m.If(dir_i):
                    m.d.usb += got.eq(data_i)
                m.d.usb += to.eq(to + 1)
                with m.If(~dir_i):
                    m.next = "DONE"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "DONE"
            with m.State("DONE"):
                m.d.comb += drive.eq(0x00)
                with m.If(which):
                    m.d.usb += rb.eq(got)
                with m.Else():
                    m.d.usb += ra.eq(got)
                with m.If(got == val):
                    m.d.usb += ok.eq(ok + 1)
                with m.Else():
                    m.d.usb += bad.eq(bad + 1)
                m.d.usb += which.eq(~which)
                m.next = "CCMD"
            with m.State("CCMD"):                 # control: plain REGR 0x00
                m.d.comb += drive.eq(0xC0)
                m.d.usb += to.eq(to + 1)
                with m.If(dir_i):
                    m.next = "CCMD"
                with m.Elif(nxt_i):
                    m.next = "CTURN"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "IDLE"
            with m.State("CTURN"):
                m.d.comb += drive.eq(0x00)
                m.d.usb += to.eq(to + 1)
                with m.If(dir_i):
                    m.next = "CHOLD"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "IDLE"
            with m.State("CHOLD"):
                m.d.comb += drive.eq(0x00)
                # capture ONLY while the PHY still owns the bus: an
                # unconditional capture also fires on the cycle DIR has already
                # dropped and overwrites the value with the idle bus (0x00).
                with m.If(dir_i):
                    m.d.usb += v0.eq(data_i)
                m.d.usb += to.eq(to + 1)
                with m.If(~dir_i):
                    m.next = "IDLE"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "IDLE"
            with m.State("FAIL"):
                m.d.comb += drive.eq(0x00)
                m.d.usb += bad.eq(bad + 1)
                m.d.usb += which.eq(~which)
                m.next = "IDLE"

        # ---- snapshot into sync, then UART ----------------------------------
        h = [Signal(16, name=f"h{i}") for i in range(5)]
        src = [ra, rb, ok, bad, v0]
        for i in range(5):
            s1 = Signal(16, name=f"q{i}")
            m.d.sync += [s1.eq(src[i]), h[i].eq(s1)]

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

        chars = ([Const(ord(c), 8) for c in "RA="]
                 + [hexd(h[0][4:8]), hexd(h[0][0:4]), Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "RB="]
                 + [hexd(h[1][4:8]), hexd(h[1][0:4]), Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "V0="]
                 + [hexd(h[4][4:8]), hexd(h[4][0:4]), Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "OK="]
                 + [hexd(h[2][4*(3-k):4*(3-k)+4]) for k in range(4)]
                 + [Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "BAD="]
                 + [hexd(h[3][4*(3-k):4*(3-k)+4]) for k in range(4)]
                 + [Const(ord('\r'), 8), Const(ord('\n'), 8)])
        NCH = len(chars)
        sel = Array(chars)
        pidx = Signal(range(NCH + 1)); gap = Signal(22)
        m.d.sync += tx_start.eq(0)
        with m.FSM(domain="sync"):
            with m.State("EMIT"):
                with m.If(~tx_busy & ~tx_start):
                    m.d.sync += [tx_byte.eq(sel[pidx]), tx_start.eq(1),
                                 pidx.eq(pidx + 1)]
                    with m.If(pidx == NCH - 1):
                        m.d.sync += [pidx.eq(0), gap.eq(0)]
                        m.next = "GAP"
            with m.State("GAP"):
                m.d.sync += gap.eq(gap + 1)
                with m.If(gap.all()):
                    m.next = "EMIT"

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
