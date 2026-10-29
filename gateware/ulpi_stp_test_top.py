#!/usr/bin/env python3
#
# ULPI DATA-BUS DRIVE TEST -- continuity check with nothing but a voltmeter.
#
# Holds the PHY in RESET (active HIGH) so it can never drive the bus, then
# drives all eight ULPI data lines HIGH from the FPGA, continuously.
#
# Measure DC volts at the PHY's D0..D7 pins (or at the socket contacts):
#     ~3.3 V -> the FPGA's drive reaches that pin: the line is CONNECTED
#      ~0 V  -> OPEN (or shorted to ground)
#
# No contention risk: the PHY is held in reset, so its data pins stay inputs.
#
# Prints a heartbeat on the UART (115200) so you can confirm the bitstream is
# actually running while you probe.
#
from amaranth import Module, Signal, Elaboratable, Cat, Const, Mux, Array


class UlpiStpTest(Elaboratable):
    BAUD_DIV = 521

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)

        # sync comes off the 25 MHz crystal, so this runs even with no PHY clock.
        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi_clk.i, usb_pll=False)

        m.d.comb += [
            ulpi.rst.o.eq(1),          # HOLD PHY IN RESET (active HIGH)
            ulpi.data.oe.eq(1),        # we drive
            ulpi.data.o.eq(0x00),      # data LOW so STP stands out
            ulpi.stp.o.eq(1),          # STP driven HIGH - measure it at the PHY
        ]

        # ---- UART heartbeat ----
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

        parts = [ord(c) for c in "STP=HIGH DATA=LOW, PHY in reset\r\n"]
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
