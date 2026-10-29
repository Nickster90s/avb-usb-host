#!/usr/bin/env python3
#
# ULPI PER-BIT data-line test -> UART (115200 8N1 on uart_tx / R3).
#
# Same walking-pattern scratch-register test as ulpi_bittest_top.py, but prints
# the result over UART instead of blinking an 8-symbol LED frame, because
# counting blink lengths is error-prone.
#
# Output line (~1/s):
#
#   N=01A3 BAD=0C WR=AA RD=A6 D0-7:..XX....
#
#   N      completed read transactions (16-bit, wraps). *** READ THIS FIRST ***
#   BAD    OR-accumulated per-bit mismatch over the last window
#   WR/RD  last pattern written / value read back
#   D0-7   one char per data line, D0 leftmost: '.' = clean, 'X' = failed
#
# CRITICAL: N=0000 means NO register cycle ever completed, so BAD is meaningless
# and will read 00 -- which in the LED version looks identical to "all 8 lines
# clean". N stuck at 0000 => the link never gets its DIR/NXT turnaround, i.e.
# the fault is on DIR / NXT / STP (or the PHY isn't responding), NOT on D0-7.
# Only when N is counting up does the D0-7 field mean anything.
#
# USB_PHASE: "raw" (posedge BUFG off the ULPI pin, no PLL -- what the breakout
# board's PHY-A needs), "inv" (falling edge), or a number (PLL phase shift).
#
import os
from amaranth import (Module, Signal, Elaboratable, Const, Array, Mux, Cat)
from luna.gateware.interface.ulpi import ULPIRegisterWindow


class UlpiBitTestUart(Elaboratable):
    SCRATCH  = 0x16
    WAITMS   = 60000          # usb-domain cycles to let the PHY settle
    BAUD_DIV = 521            # 60 MHz sync / 115200
    WIN      = 16             # transactions per accumulate window = 2**WIN

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)

        _ph = os.environ.get("USB_PHASE", "raw")
        if _ph == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        elif _ph == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(_ph))
        m.d.comb += ulpi.rst.o.eq(0)          # USB3300 RESET is active HIGH

        m.submodules.rw = rw = ULPIRegisterWindow()
        m.d.comb += [
            rw.ulpi_data_in.eq(ulpi.data.i), rw.ulpi_dir.eq(ulpi.dir.i),
            rw.ulpi_next.eq(ulpi.nxt.i),     ulpi.data.o.eq(rw.ulpi_data_out),
            ulpi.data.oe.eq(rw.ulpi_out_req), ulpi.stp.o.eq(rw.ulpi_stop),
        ]

        startup = Signal(range(self.WAITMS + 1)); ready = Signal()
        m.d.usb += startup.eq(startup + ~ready)
        with m.If(startup == self.WAITMS):
            m.d.usb += ready.eq(1)

        # Walking patterns: every bit driven both high and low.
        PATS = [0xAA, 0x55, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80,
                0xFE, 0xFD, 0xFB, 0xF7, 0xEF, 0xDF, 0xBF, 0x7F]
        pidx = Signal(range(len(PATS)))
        pat  = Signal(8)
        m.d.comb += pat.eq(Array([Const(p) for p in PATS])[pidx])
        m.d.comb += [rw.address.eq(self.SCRATCH), rw.write_data.eq(pat)]

        err_acc = Signal(8)
        bad     = Signal(8)
        win     = Signal(self.WIN)
        nxfer   = Signal(16)      # completed read transactions -- the liveness proof
        last_rd = Signal(8)
        last_wr = Signal(8)

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                with m.If(ready & ~rw.busy):
                    m.next = "WR"
            with m.State("WR"):
                m.d.comb += rw.write_request.eq(1)
                m.next = "WRW"
            with m.State("WRW"):
                with m.If(rw.done):
                    m.next = "RD"
            with m.State("RD"):
                m.d.comb += rw.read_request.eq(1)
                m.next = "RDW"
            with m.State("RDW"):
                with m.If(rw.done):
                    m.d.usb += [
                        err_acc.eq(err_acc | (rw.read_data ^ pat)),
                        last_rd.eq(rw.read_data),
                        last_wr.eq(pat),
                        nxfer.eq(nxfer + 1),
                        pidx.eq(Mux(pidx == len(PATS) - 1, 0, pidx + 1)),
                        win.eq(win + 1),
                    ]
                    with m.If(win == 0):
                        m.d.usb += [bad.eq(err_acc), err_acc.eq(0)]
                    m.next = "IDLE"

        # ---- CDC usb -> sync (2-FF; all fields are slow-changing diagnostics) ----
        def sync2(src, n):
            a = Signal(n); b = Signal(n)
            m.d.sync += [a.eq(src), b.eq(a)]
            return b
        bad_s = sync2(bad, 8)
        wr_s  = sync2(last_wr, 8)
        rd_s  = sync2(last_rd, 8)
        n_s   = sync2(nxfer, 16)

        # ---- UART TX (8N1) in the crystal-clean sync domain ----
        # sync is driven from clk25, NOT from the ULPI clock, so this keeps
        # printing even when the PHY is dead -- which is the whole point.
        uart = platform.request("uart_tx", 0)
        baud_cnt = Signal(range(self.BAUD_DIV)); baud_tick = Signal()
        m.d.sync += baud_cnt.eq(baud_cnt - 1)
        with m.If(baud_cnt == 0):
            m.d.sync += baud_cnt.eq(self.BAUD_DIV - 1)
        m.d.comb += baud_tick.eq(baud_cnt == 0)

        tx_shift = Signal(10, reset=0x3FF); tx_bits = Signal(range(11))
        tx_busy  = Signal(); tx_byte = Signal(8); tx_start = Signal()
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
        def hexbyte(v):
            return [hexd(v[4:8]), hexd(v[0:4])]
        def hexword(v):
            return [hexd(v[12:16]), hexd(v[8:12]), hexd(v[4:8]), hexd(v[0:4])]
        def lit(s):
            return [ord(c) for c in s]

        # D0 leftmost: '.' clean, 'X' failed.
        bits = [Mux(bad_s[i], ord('X'), ord('.')) for i in range(8)]

        parts = (lit("N=")      + hexword(n_s)
               + lit(" BAD=")   + hexbyte(bad_s)
               + lit(" WR=")    + hexbyte(wr_s)
               + lit(" RD=")    + hexbyte(rd_s)
               + lit(" D0-7:")  + bits
               + lit("\r\n"))
        N = len(parts)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])

        latched = Signal(); throttle = Signal(26)
        m.d.sync += throttle.eq(throttle + 1)
        with m.If((throttle == 0) & ~latched):
            m.d.sync += [msg[i].eq(parts[i]) for i in range(N)]
            m.d.sync += latched.eq(1)

        pos = Signal(range(N + 1))
        m.d.sync += tx_start.eq(0)
        with m.If(latched & ~tx_busy & ~tx_start):
            with m.If(pos < N):
                m.d.sync += [tx_byte.eq(msg[pos]), tx_start.eq(1), pos.eq(pos + 1)]
            with m.Else():
                m.d.sync += [pos.eq(0), latched.eq(0)]

        return m
