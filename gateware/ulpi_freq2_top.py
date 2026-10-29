#!/usr/bin/env python3
#
# DUAL frequency counter -- measures the REAL ULPI clock pin and a NEIGHBOUR
# pin in ONE bitstream, so the comparison is a built-in differential control
# and not two separate builds.
#
#   W = ulpi_clock      (SODIMM 85 = W4 by default: the pin the PCB routes
#                        USB2_CLK to -- CN1.85 -> J5.1, 178.2 mil, no via)
#   A = ulpi_clock_alt  (SODIMM 87 = AA4 by default: net "CN1_87", ZERO copper
#                        on the PCB -- a floating pin, so whatever it reads is
#                        crosstalk)
#
# Both counters are PLL-FREE: the gate and the UART run off the 25 MHz board
# crystal through an explicit BUFG, each measured clock gets its own BUFG, and
# each count is frozen before it is read.  Printed once per ~2 s:
#
#     W=03938700 A=03938700
#
# Read each as a plain integer = Hz.  0x03938700 = 60 000 000.
# W=00000000 means NO CLOCK reaches the pin the board actually routes.
#
import os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockDomain,
                      ClockSignal, Cat, Const, Mux, Array)


class UlpiFreq2(Elaboratable):
    REFHZ    = 25_000_000
    BAUD_DIV = REFHZ // 115200

    def elaborate(self, platform):
        m = Module()
        clk25 = platform.request("clk25", 0)
        ulpi  = platform.request("ulpi", 0)
        uart  = platform.request("uart_tx", 0)
        led   = platform.request("user_led", 0)
        cw    = platform.request("ulpi_clock", 0)
        ca    = platform.request("ulpi_clock_alt", 0)

        m.domains += ClockDomain("sync")
        refbuf = Signal()
        m.submodules += Instance("BUFG", i_I=clk25.i, o_O=refbuf)
        m.d.comb += ClockSignal("sync").eq(refbuf)

        # release the PHY (reset is active HIGH) so CLKOUT runs
        m.d.comb += ulpi.rst.o.eq(0)
        m.d.comb += ulpi.stp.o.eq(0)
        # ULPI requires the LINK to drive the bus whenever DIR is low
        m.d.comb += [ulpi.data.o.eq(0x00), ulpi.data.oe.eq(~ulpi.dir.i)]

        # 1 s gate in the crystal domain
        gate = Signal(reset=1)
        gcnt = Signal(range(self.REFHZ))
        with m.If(gcnt == self.REFHZ - 1):
            m.d.sync += [gcnt.eq(0), gate.eq(~gate)]
        with m.Else():
            m.d.sync += gcnt.eq(gcnt + 1)

        freqs = []
        for name, pin in (("w", cw.i), ("a", ca.i)):
            m.domains += ClockDomain("m_" + name)
            buf = Signal(name="buf_" + name)
            m.submodules["bufg_" + name] = Instance("BUFG", i_I=pin, o_O=buf)
            m.d.comb += ClockSignal("m_" + name).eq(buf)
            g1 = Signal(name="g1_" + name)
            gm = Signal(name="gm_" + name)
            gd = Signal(name="gd_" + name)
            m.d["m_" + name] += [g1.eq(gate), gm.eq(g1), gd.eq(gm)]
            cnt = Signal(27, name="cnt_" + name)
            with m.If(gm & ~gd):
                m.d["m_" + name] += cnt.eq(1)
            with m.Elif(gm):
                m.d["m_" + name] += cnt.eq(cnt + 1)
            freqs.append((Signal(27, name="freq_" + name), cnt))

        gate_d = Signal(); settle = Signal(range(2048)); pending = Signal()
        m.d.sync += gate_d.eq(gate)
        with m.If(gate_d & ~gate):
            m.d.sync += [settle.eq(2047), pending.eq(1)]
        with m.Elif(settle != 0):
            m.d.sync += settle.eq(settle - 1)
            with m.If(settle == 1):
                m.d.sync += [pending.eq(0)]
                m.d.sync += [f.eq(c) for f, c in freqs]

        # --- UART TX (8N1), crystal domain ---
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
            nibs = [sig[4 * i:4 * i + 4] for i in range(7)] + [Const(0, 4)]
            return [hexd(nibs[7 - i]) for i in range(8)]

        chars = ([Const(ord('W'), 8), Const(ord('='), 8)] + hexw(freqs[0][0])
                 + [Const(ord(' '), 8), Const(ord('A'), 8), Const(ord('='), 8)]
                 + hexw(freqs[1][0])
                 + [Const(ord('\r'), 8), Const(ord('\n'), 8)])
        N = len(chars)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
        pend_d = Signal(); m.d.sync += pend_d.eq(pending)
        arm = Signal()
        with m.If(pend_d & ~pending):
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

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
