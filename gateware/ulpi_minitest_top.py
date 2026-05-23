#!/usr/bin/env python3
#
# Minimal manual ULPI test — bypasses LUNA entirely.
#
# Behavior on `usb` domain (60 MHz from ULPI CLK):
#   • Wait 1 ms after boot (post-RST settle)
#   • Drive ulpi.data[7:0] = 0x8A continuously when DIR is low
#     (TXCMD = REG_WRITE | addr 0x0A = OTG control register)
#   • When NXT goes high, advance: hold the write-value byte (0x80) for
#     one cycle, then pulse STP for one cycle, then loop back.
#   • Latch any-NXT-high so we can tell on an LED if the PHY ever
#     responded.
#
# LED encoding (on `usb` domain):
#   • Slow blink (~0.5 Hz)  → NXT never high → PHY isn't seeing valid
#     TXCMD on D[7:0]. Data line wiring or STP wiring.
#   • Fast blink (~4 Hz)    → NXT was observed → wiring works, the PHY
#     IS responding to TXCMDs, the LUNA stack is the problem.

from amaranth import Module, Signal, Elaboratable, Const, Mux, ClockSignal
from luna     import top_level_cli


class UlpiMiniTest(Elaboratable):
    CLOCKS_PER_MS  = 60_000

    def elaborate(self, platform):
        m = Module()

        ulpi = platform.request("ulpi", 0)
        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi.clk.i)

        # Without LUNA, no one drives ClockSignal("usb"). Drive it here
        # from the ULPI clock pin directly (T4, MRCC → BUFG).
        m.d.comb += ClockSignal("usb").eq(ulpi.clk.i)

        # PHY reset: pin is PinsN so .o=0 means physical pin HIGH = released.
        # We deassert reset at boot (= drive .o low so PinsN inverts to high).
        m.d.comb += ulpi.rst.o.eq(0)

        startup    = Signal(range(self.CLOCKS_PER_MS + 1))
        phy_ready  = Signal()

        m.d.usb += startup.eq(Mux(phy_ready, startup, startup + 1))
        with m.If(startup == self.CLOCKS_PER_MS):
            m.d.usb += phy_ready.eq(1)

        # We drive the data bus whenever DIR is low (FPGA owns the bus).
        m.d.comb += ulpi.data.oe.eq(~ulpi.dir.i)

        nxt_seen   = Signal()
        m.d.usb   += nxt_seen.eq(nxt_seen | ulpi.nxt.i)

        # 3-state mini FSM that writes register 0x0A (TXCMD = 0x8A,
        # data byte 0x80 = disable dp/dm pulldowns and ext_vbus indicator)
        with m.FSM(domain="usb") as fsm:

            with m.State('IDLE'):
                m.d.comb += [
                    ulpi.data.o.eq(0x00),       # NOP
                    ulpi.stp.o .eq(0),
                ]
                with m.If(phy_ready & ~ulpi.dir.i):
                    m.next = 'CMD'

            with m.State('CMD'):
                m.d.comb += [
                    ulpi.data.o.eq(0x8A),       # REG_WRITE | 0x0A
                    ulpi.stp.o .eq(0),
                ]
                with m.If(ulpi.dir.i):          # PHY claimed bus
                    m.next = 'IDLE'
                with m.Elif(ulpi.nxt.i):        # PHY accepted TXCMD
                    m.next = 'DATA'

            with m.State('DATA'):
                m.d.comb += [
                    ulpi.data.o.eq(0x80),       # write value
                    ulpi.stp.o .eq(0),
                ]
                with m.If(ulpi.dir.i):
                    m.next = 'IDLE'
                with m.Elif(ulpi.nxt.i):
                    m.next = 'STOP'

            with m.State('STOP'):
                m.d.comb += [
                    ulpi.data.o.eq(0x00),
                    ulpi.stp.o .eq(1),          # end transaction
                ]
                m.next = 'WAIT'

            with m.State('WAIT'):
                # Brief pause before re-issuing the write — keeps STP
                # visible on the scope as a periodic pulse if it works.
                wait_cnt = Signal(20)
                m.d.usb += wait_cnt.eq(wait_cnt + 1)
                with m.If(wait_cnt == 0):       # rolled over
                    m.next = 'IDLE'

        # LED: slow blink while no NXT, fast once NXT was seen.
        led    = platform.request("user_led", 0)
        counter = Signal(26)
        m.d.usb += counter.eq(counter + 1)
        m.d.comb += led.o.eq(Mux(nxt_seen, counter[22], counter[25]))

        return m


if __name__ == "__main__":
    top_level_cli(UlpiMiniTest)
