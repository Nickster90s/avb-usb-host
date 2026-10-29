#!/usr/bin/env python3
#
# DUAL-DOMAIN DIR CENSUS — settle a flat contradiction in the evidence.
#
#   ctldecay, 200 MHz crystal-derived "fast" domain: DIR observed HIGH for
#     0xBFFF x 5 ns = 246 us, i.e. ~14,700 usb cycles.
#   trace, "usb" domain (PHY clock): DIR reads 0 for 60 CONSECUTIVE cycles in
#     the turnaround, at all 8 clock phases, in PLL and raw clocking modes.
#
# Both cannot be true.  This instrument observes the SAME DIR pin PASSIVELY
# (drives nothing) in BOTH domains over the SAME gate window and reports each
# domain's view, so the disagreement is measured rather than argued:
#
#     FH=xxxxxxxx FT=xxxxxxxx FE=xxxxxxxx UH=xxxxxxxx UT=xxxxxxxx UE=xxxxxxxx
#     `- fast(200MHz): high cycles / total / rising edges
#                                  `- usb(PHY clk): high cycles / total / edges
#
# FH/FT ~= UH/UT and FE ~= UE  -> both domains agree; DIR genuinely behaves the
#     same way, so the trace's 60-cycle DIR=0 is a PROTOCOL/timing effect, not
#     a capture failure, and the PHY simply is not turning the bus around.
# FE >> UE with FH/FT small    -> DIR pulses are NARROWER than one usb clock
#     (16.7 ns); the PHY emits sub-cycle DIR glitches and the usb domain
#     legitimately cannot see them.
# FH/FT large but UH/UT ~= 0   -> the usb-domain CAPTURE of DIR is broken while
#     the identical pin reads fine in fast; a gateware/placement fault on that
#     one input path, not a wire.
#
# Edge counts are the sharper test: a real DIR rising edge is a real event, so
# FE and UE must agree regardless of clock rate -- unless pulses are narrower
# than a usb period, which the duty figures then reveal.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Mux, Array,
                      ClockSignal)


class UlpiDirCensus(Elaboratable):
    BAUD_DIV = 521
    GATE     = 26                  # 2**26 fast cycles = ~335 ms

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        # match the known-good design's clocking mode by default
        if _os.environ.get("USB_PLL", "") == "1":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i,
                usb_phase=float(_os.environ.get("USB_PHASE", "0")))
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)

        # Observe only.  Park the bus the way ULPI requires (link drives 0x00
        # whenever DIR is low) so the PHY sees a legal idle link.
        m.d.comb += [ulpi.rst.o.eq(0), ulpi.stp.o.eq(0),
                     ulpi.data.o.eq(0x00), ulpi.data.oe.eq(~ulpi.dir.i)]

        # ---- gate generated in fast, synced into usb ------------------------
        gcnt = Signal(self.GATE + 1)
        m.d.fast += gcnt.eq(gcnt + 1)
        gate_f = Signal()
        m.d.comb += gate_f.eq(~gcnt[self.GATE])      # high for the first half
        g1 = Signal(); gate_u = Signal()
        m.d.usb += [g1.eq(gate_f), gate_u.eq(g1)]

        def census(dom, gate, tag):
            hi = Signal(32, name="hi_" + tag)
            to = Signal(32, name="to_" + tag)
            ed = Signal(32, name="ed_" + tag)
            prev = Signal(name="pv_" + tag)
            m.d[dom] += prev.eq(ulpi.dir.i)
            with m.If(gate):
                m.d[dom] += to.eq(to + 1)
                with m.If(ulpi.dir.i):
                    m.d[dom] += hi.eq(hi + 1)
                with m.If(ulpi.dir.i & ~prev):
                    m.d[dom] += ed.eq(ed + 1)
            return hi, to, ed

        fh, ft, fe = census("fast", gate_f, "f")
        uh, ut, ue = census("usb", gate_u, "u")

        # counters freeze when the gate drops, so a plain 2-FF read is safe
        vals = [fh, ft, fe, uh, ut, ue]
        s1 = [Signal(32, name=f"a{i}") for i in range(6)]
        s2 = [Signal(32, name=f"b{i}") for i in range(6)]
        for i in range(6):
            m.d.sync += [s1[i].eq(vals[i]), s2[i].eq(s1[i])]

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

        chars = []
        for tag, sig in zip(("FH", "FT", "FE", "UH", "UT", "UE"), s2):
            chars += [Const(ord(tag[0]), 8), Const(ord(tag[1]), 8),
                      Const(ord('='), 8)] + hexw(sig) + [Const(ord(' '), 8)]
        chars += [Const(ord('\r'), 8), Const(ord('\n'), 8)]
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
