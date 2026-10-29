#!/usr/bin/env python3
#
# ULPI PER-BIT data-line test -> onboard LED (no UART).
#
# Tests each ULPI data line D0..D7 individually by writing walking patterns to
# the PHY Scratch register (0x16) and reading them back, at max register rate,
# accumulating any per-bit mismatch over a long window. This catches a
# mis-wired / stuck / grossly-skewed data line (it does NOT see HS-only timing
# skew, which needs real HS traffic -- if every bit is clean here, the wiring is
# good and the remaining problem is pure HS eye margin -> matched-pin cluster).
#
# The LED blinks an 8-symbol frame, D0 first, then a long dark gap, repeat:
#   SHORT pulse (~0.1 s) = that data line is CLEAN
#   LONG  pulse (~0.5 s) = that data line FAILED (mis-read)
# Count the symbols 1..8 = D0..D7; the long ones are the bad lines.
# All-short = all 8 lines read correctly at register rate.
#
import os
from amaranth import (Module, Signal, Elaboratable, Const, Array, Mux)
from luna.gateware.interface.ulpi import ULPIRegisterWindow


class UlpiBitTest(Elaboratable):
    SCRATCH = 0x16
    WAITMS  = 60000

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        led      = platform.request("user_led", 0)

        _ph = os.environ.get("USB_PHASE", "0")
        if _ph == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        elif _ph == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(_ph))
        m.d.comb += ulpi.rst.o.eq(0)

        m.submodules.rw = rw = ULPIRegisterWindow()
        m.d.comb += [
            rw.ulpi_data_in.eq(ulpi.data.i), rw.ulpi_dir.eq(ulpi.dir.i),
            rw.ulpi_next.eq(ulpi.nxt.i), ulpi.data.o.eq(rw.ulpi_data_out),
            ulpi.data.oe.eq(rw.ulpi_out_req), ulpi.stp.o.eq(rw.ulpi_stop),
        ]
        startup = Signal(range(self.WAITMS + 1)); ready = Signal()
        m.d.usb += startup.eq(startup + ~ready)
        with m.If(startup == self.WAITMS):
            m.d.usb += ready.eq(1)

        # walking patterns exercise every bit high AND low
        PATS = [0xAA, 0x55, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80,
                0xFE, 0xFD, 0xFB, 0xF7, 0xEF, 0xDF, 0xBF, 0x7F]
        pidx = Signal(range(len(PATS)))
        pat = Signal(8)
        m.d.comb += pat.eq(Array([Const(p) for p in PATS])[pidx])
        m.d.comb += [rw.address.eq(self.SCRATCH), rw.write_data.eq(pat)]  # HELD

        err_acc = Signal(8); win = Signal(22); bad = Signal(8)
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
                    m.d.usb += err_acc.eq(err_acc | (rw.read_data ^ pat))
                    m.d.usb += pidx.eq(Mux(pidx == len(PATS)-1, 0, pidx + 1))
                    m.d.usb += win.eq(win + 1)
                    with m.If(win == 0):
                        m.d.usb += [bad.eq(err_acc), err_acc.eq(0)]
                    m.next = "IDLE"

        # CDC usb->sync
        bad_s = Signal(8); _b = Signal(8)
        m.d.sync += [_b.eq(bad), bad_s.eq(_b)]

        # ~0.1 s tick in the crystal-clean sync domain
        TICK = 6_000_000
        tick = Signal(range(TICK)); t = Signal()
        m.d.sync += tick.eq(tick + 1)
        with m.If(tick == TICK - 1):
            m.d.sync += [tick.eq(0), t.eq(1)]
        with m.Else():
            m.d.sync += t.eq(0)

        # frame: 8 symbols x 8 ticks + 8-tick gap = 72 ticks. Each symbol on for
        # 5 ticks (bad) or 1 tick (clean) at its start; D0 is symbol 0.
        fc = Signal(range(72)); frame_bad = Signal(8)
        with m.If(t):
            with m.If(fc == 71):
                m.d.sync += [fc.eq(0), frame_bad.eq(bad_s)]   # latch for the frame
            with m.Else():
                m.d.sync += fc.eq(fc + 1)
        sym = Signal(4); wit = Signal(3)
        m.d.comb += [sym.eq(fc[3:7]), wit.eq(fc[0:3])]        # fc//8, fc%8
        cur_bad = Signal()
        m.d.comb += cur_bad.eq(Array([frame_bad[i] for i in range(8)]
                                     + [Const(0)] * 8)[sym])
        m.d.comb += led.o.eq((sym < 8) & (wit < Mux(cur_bad, 5, 1)))
        return m
