#!/usr/bin/env python3
#
# v3 ULPI test — maximally dumb. No FSM. No phy_ready wait. No DIR check.
# Just continuously drive D[7:0] = 0x8A and pulse STP high every ~21 ms.
#
# Watch on scope:
#   D0=0  D1=1  D2=0  D3=1  D4=0  D5=0  D6=0  D7=1   (= 0x8A)
#   STP pulses high periodically (period of ~21 ms when CTR_BITS=21,
#   pulse width ~17 µs).
#   NXT: if PHY accepts our TXCMD pattern, it should pulse high.
#
# LED in usb domain: fast blink once NXT has ever been seen, slow otherwise.

from amaranth import Module, Signal, Elaboratable, Mux, ClockSignal
from luna     import top_level_cli


class UlpiForce(Elaboratable):
    CTR_BITS = 21       # ~21 ms period at 60 MHz

    def elaborate(self, platform):
        m = Module()

        ulpi = platform.request("ulpi", 0)
        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi.clk.i)
        m.d.comb += ClockSignal("usb").eq(ulpi.clk.i)

        # Release PHY reset.
        m.d.comb += ulpi.rst.o.eq(0)

        # NXT latch.
        nxt_seen = Signal()
        m.d.usb += nxt_seen.eq(nxt_seen | ulpi.nxt.i)

        # Periodic STP pulse. STP high for top 1024 ticks of the cycle,
        # low otherwise. Period 21-bit ~ 17.5 ms, pulse ~ 17 µs.
        ctr     = Signal(self.CTR_BITS)
        m.d.usb += ctr.eq(ctr + 1)
        stp_now = ctr[-10:] == 0    # high for 1 of every 1024 ticks

        # Unconditional drive of data and STP. OE always asserted.
        m.d.comb += [
            ulpi.data.oe.eq(1),
            ulpi.data.o .eq(0x8A),
            ulpi.stp.o  .eq(stp_now),
        ]

        # LED: fast blink once NXT was ever observed.
        led = platform.request("user_led", 0)
        m.d.comb += led.o.eq(Mux(nxt_seen, ctr[-3], ctr[-1]))

        return m


if __name__ == "__main__":
    top_level_cli(UlpiForce)
