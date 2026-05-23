#!/usr/bin/env python3
#
# Dumber than ulpi_minitest_top: just unconditionally drive D=0xAA on
# the ULPI data lines, OE always on. No FSM, no waiting.
#
# Expected on scope (1 µs/div):
#   • D0  (V2)  = LOW    (bit 0 of 0xAA = 0)
#   • D1  (V3)  = HIGH   (bit 1 of 0xAA = 1)
#   • D3  (W2)  = HIGH   (bit 3 of 0xAA = 1)
#   • D7  (Y2)  = HIGH   (bit 7 of 0xAA = 1)
#
# If everything stays at 0V, the data IOBUFs aren't driving — either
# the OE logic is broken, the data pin assignments are wrong, or the
# ulpi.data.oe signal isn't reaching the IOBUFs.

from amaranth import Module, Signal, Elaboratable, Const, Mux, ClockSignal
from luna     import top_level_cli


class UlpiDump(Elaboratable):
    def elaborate(self, platform):
        m = Module()

        ulpi = platform.request("ulpi", 0)
        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi.clk.i)
        m.d.comb += ClockSignal("usb").eq(ulpi.clk.i)

        # Release PHY reset (PinsN inverts).
        m.d.comb += ulpi.rst.o.eq(0)

        # ALWAYS drive 0xAA. STP idle low. OE always asserted.
        m.d.comb += [
            ulpi.data.oe.eq(1),
            ulpi.data.o .eq(0xAA),
            ulpi.stp.o  .eq(0),
        ]

        # Heartbeat LED in usb domain just to confirm bitstream loaded.
        led     = platform.request("user_led", 0)
        counter = Signal(26)
        m.d.usb += counter.eq(counter + 1)
        m.d.comb += led.o.eq(counter[-1])

        return m


if __name__ == "__main__":
    top_level_cli(UlpiDump)
