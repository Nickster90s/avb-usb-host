#!/usr/bin/env python3
#
# V4 CLOCK-PRESENCE TEST — is a real 60 MHz clock arriving at the ULPI clock pin?
# Host-free, PLL-free, PHY-register-free. The LED is driven from the board's
# 25 MHz crystal (always running) so it ALWAYS blinks; the BLINK RATE reports
# whether the ULPI clock pin is toggling:
#
#   LED FAST blink (~6 Hz)  = a clock IS present at the ULPI clock pin (V4 good)
#   LED SLOW blink (~0.7 Hz)= NO clock at the pin (V4 wire / breakout CLKOUT / RST)
#   LED SOLID / no blink    = crystal/board/config problem (should never happen)
#
# Uses EXPLICIT BUFG instances (not tool-inferred) on both clk25 and the ULPI
# clock, and does NOT instantiate the USB PLL — so the ULPI clock pin fans out
# only to our test BUFG (no dual-fanout ambiguity).
#
from amaranth import Module, Signal, Elaboratable, Instance, ClockDomain, Mux
from luna import top_level_cli


class UlpiClockTest(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        clk25    = platform.request("clk25", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        ulpi     = platform.request("ulpi", 0)          # to release the PHY reset
        led      = platform.request("user_led", 0)

        # Release the PHY (RST active-HIGH -> drive 0) so it outputs CLKOUT.
        m.d.comb += ulpi.rst.o.eq(0)

        # Reference domain: board 25 MHz crystal via explicit BUFG (always runs).
        cd_ref = ClockDomain("ref"); m.domains += cd_ref
        refbuf = Signal()
        m.submodules += Instance("BUFG", i_I=clk25.i, o_O=refbuf)
        m.d.comb += cd_ref.clk.eq(refbuf)

        # Test domain: the ULPI clock pin via explicit BUFG.
        cd_v4 = ClockDomain("v4"); m.domains += cd_v4
        v4buf = Signal()
        m.submodules += Instance("BUFG", i_I=ulpi_clk.i, o_O=v4buf)
        m.d.comb += cd_v4.clk.eq(v4buf)

        v4tog = Signal()
        m.d.v4 += v4tog.eq(~v4tog)                      # toggles iff V4 clock runs

        # Detect V4 toggling from the crystal domain.
        s1 = Signal(); s2 = Signal(); sp = Signal()
        m.d.ref += [s1.eq(v4tog), s2.eq(s1), sp.eq(s2)]
        wd = Signal(21)
        with m.If(s2 != sp):
            m.d.ref += wd.eq(0)
        with m.Elif(wd != (2**21 - 1)):
            m.d.ref += wd.eq(wd + 1)
        v4_present = Signal()
        m.d.comb += v4_present.eq(wd != (2**21 - 1))

        # LED: always blinks (crystal). FAST = V4 clock present, SLOW = absent.
        ctr = Signal(26)
        m.d.ref += ctr.eq(ctr + 1)
        m.d.comb += led.o.eq(Mux(v4_present, ctr[21], ctr[24]))
        return m


if __name__ == "__main__":
    top_level_cli(UlpiClockTest)
