#!/usr/bin/env python3
#
# USB-DOMAIN CAPTURE SELF-TEST — take the PHY out of the question.
#
# Everything so far has tested the FPGA and the PHY together, so every result
# has been ambiguous between "the PHY misbehaves" and "we cannot capture what
# it sends".  This instrument tests ONLY our own capture path: the FPGA drives
# a known pattern onto the 8 ULPI data lines (legal while DIR is low -- the
# link owns the bus) and reads it back through the real pads, the real 178 mil
# PCB nets and the real fabric capture flops, clocked by the REAL PHY clock.
# The PHY contributes nothing but the clock.
#
# If our own driven data cannot be read back reliably, the fault is in the usb
# clock domain -- clock quality / reference -- and NOT in the PHY, not in the
# data nets (proven good by the walking-one short map) and not in DIR.
# If it reads back perfectly, the capture path is sound at 60 MHz and the
# remaining suspect goes back to what the PHY actually drives and when.
#
# *** USB_LB_RST=1 IS MANDATORY WITH USB_LB_FAST=1 ***
# While DIR is low the LINK owns the ULPI bus and the PHY DECODES whatever we
# drive as commands: 01xxxxxx = TRANSMIT, 10xxxxxx = REGISTER WRITE (random
# address AND data), 11xxxxxx = REGISTER READ.  An LFSR therefore walks the
# whole opcode space and writes random registers -- the "never sweep arbitrary
# bytes at a ULPI PHY" rule.  Run without USB_LB_RST and PHY-B latches DIR
# high, which gates drive_ok and silently yields TOT=0: a vacuous result, not a
# pass.  (PHY-A tolerated it and returned ERR=0 over 1.34e9 samples, so the two
# positions could not even be compared.)
# USB_LB_RST=1 holds the PHY in RESET for the whole test: it cannot decode our
# bytes and cannot drive the bus, so the loopback is purely FPGA pad -> net ->
# FPGA pad.  This is only possible because the USB3300's CLKOUT FREE-RUNS while
# RESET is asserted (measured: L=664,408,229 vs H=664,403,320 cycles), so the
# usb domain still has its 60 MHz clock.
#
# USB_LB_FAST=1 changes the pattern EVERY CYCLE via an 8-bit LFSR instead of
# every 8 cycles.  This matters: the slow version proved only SETTLED-VALUE
# capture, and a real USB packet is back-to-back changing bytes.  Single-byte
# register reads already work at position B while packet transfers fail with
# EPROTO (error -71), so per-cycle transition capture is precisely the untested
# gap.  The LFSR flips several bits per cycle, which is the stressing case.
#
# Slow mode changes the pattern every 8 usb cycles and excludes the 3 cycles
# after each change, because the readback can lag the drive (the trace saw
# `50C0`: reading back C0 while already driving 00).
#
#     ERR=xxxxxxxx TOT=xxxxxxxx LAG=x
#      |            |            `- which delay matched best (0/1/2 cycles)
#      |            `- compared cycles
#      `- mismatches.  ERR=0 => capture is clean at 60 MHz.
#
# Build the position-A control (drop USB_BACKUP=1) for the known-good number.
# USB_RAW=1 selects the raw-ULPI-clock mode that the working design uses.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Mux, Array,
                      ClockSignal)


class UlpiLoopback(Elaboratable):
    BAUD_DIV = 521
    WINDOW   = 26                 # compare over 2**26 usb cycles (~1.1 s)

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        _raw = _os.environ.get("USB_RAW", "") == "1"
        if _raw:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i,
                usb_phase=float(_os.environ.get("USB_PHASE", "0")))

        _hold_rst = _os.environ.get("USB_LB_RST", "") == "1"
        m.d.comb += [ulpi.rst.o.eq(1 if _hold_rst else 0), ulpi.stp.o.eq(0)]

        # ---- drive a known pattern --------------------------------------
        _fast = _os.environ.get("USB_LB_FAST", "") == "1"
        phase8 = Signal(3)
        m.d.usb += phase8.eq(phase8 + 1)
        pat = Signal(8, reset=0x01)
        if _fast:
            # 8-bit LFSR, x^8+x^6+x^5+x^4+1 -> a new byte EVERY cycle
            fb = Signal()
            m.d.comb += fb.eq(pat[7] ^ pat[5] ^ pat[4] ^ pat[3])
            m.d.usb += pat.eq(Cat(fb, pat[0:7]))
        else:
            with m.If(phase8 == 0):
                # 55 -> AA -> 0F -> F0 -> 33 -> CC -> 00 -> FF
                seq = Array([Const(v, 8) for v in
                             (0x55, 0xAA, 0x0F, 0xF0, 0x33, 0xCC, 0x00, 0xFF)])
                step = Signal(3)
                m.d.usb += [step.eq(step + 1), pat.eq(seq[step])]

        drive_ok = Signal()
        if _hold_rst:
            # PHY is in reset: it cannot drive the bus, so there is no
            # contention and nothing to arbitrate -- drive unconditionally.
            m.d.comb += drive_ok.eq(1)
        else:
            m.d.comb += drive_ok.eq(~ulpi.dir.i)  # only when we own the bus
        m.d.comb += [ulpi.data.o.eq(pat), ulpi.data.oe.eq(drive_ok)]

        # ---- compare the readback against delayed copies of the drive -------
        d1 = Signal(8); d2 = Signal(8)
        m.d.usb += [d1.eq(pat), d2.eq(d1)]
        rb = ulpi.data.i

        settled = Signal()
        if _fast:
            # every cycle counts; there is no settling window to skip
            m.d.comb += settled.eq(drive_ok)
        else:
            m.d.comb += settled.eq((phase8 >= 3) & drive_ok)

        errs = [Signal(32, name=f"e{k}") for k in range(3)]
        tot  = Signal(32)
        cands = [pat, d1, d2]
        with m.If(settled):
            m.d.usb += tot.eq(tot + 1)
            for k in range(3):
                with m.If(rb != cands[k]):
                    m.d.usb += errs[k].eq(errs[k] + 1)

        # ---- snapshot on a slow strobe, then 2-FF into sync -----------------
        tick = Signal(self.WINDOW + 1)
        m.d.usb += tick.eq(tick + 1)
        he = [Signal(32, name=f"he{k}") for k in range(3)]
        ht = Signal(32)
        with m.If(tick.all()):
            m.d.usb += [he[k].eq(errs[k]) for k in range(3)]
            m.d.usb += ht.eq(tot)
        s1e = [Signal(32, name=f"s1e{k}") for k in range(3)]
        s2e = [Signal(32, name=f"s2e{k}") for k in range(3)]
        s1t = Signal(32); s2t = Signal(32)
        for k in range(3):
            m.d.sync += [s1e[k].eq(he[k]), s2e[k].eq(s1e[k])]
        m.d.sync += [s1t.eq(ht), s2t.eq(s1t)]

        # pick the delay with the fewest errors
        best = Signal(2); bval = Signal(32)
        m.d.comb += [
            best.eq(Mux(s2e[0] <= s2e[1],
                        Mux(s2e[0] <= s2e[2], 0, 2),
                        Mux(s2e[1] <= s2e[2], 1, 2))),
            bval.eq(Mux(best == 0, s2e[0], Mux(best == 1, s2e[1], s2e[2]))),
        ]

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

        # Print ALL THREE lag counters, not just the minimum.  ERR=0 is a NULL
        # result, and if the comparator were broken (always "equal") all three
        # would read 0 and `best` would still pick lag 0 -- indistinguishable
        # from a genuine pass.  E1/E2 large with E0=0 is the POSITIVE CONTROL
        # that the comparison machinery actually fires.
        chars = ([Const(ord(c), 8) for c in "E0="] + hexw(s2e[0])
                 + [Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "E1="] + hexw(s2e[1])
                 + [Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "E2="] + hexw(s2e[2])
                 + [Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "ERR="] + hexw(bval)
                 + [Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "TOT="] + hexw(s2t)
                 + [Const(ord(' '), 8)]
                 + [Const(ord(c), 8) for c in "LAG="] + [hexd(best)]
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
