#!/usr/bin/env python3
#
# ULPI PIN-STATE monitor -> UART (115200 8N1 on uart_tx / R3).
#
# Open-vs-short test for the ULPI data bus. Drives NOTHING (data oe=0, STP=0,
# RST released after a pulse) and simply reports the raw pin levels. Build with
# USB_PULLUP_DATA=1 to enable internal pull-ups on D0-7:
#
#   line reads 1 with pull-up  -> FLOATING: not connected (open wire/joint)
#   line reads 0 with pull-up  -> held LOW: shorted to GND, or driven by the PHY
#
# Run it twice -- once with USB_PULLUP_DATA=1 and once without -- and compare.
# A healthy, connected, idle ULPI bus reads the PHY's drive in both cases.
#
# Output (~4/s):
#   DIR=1 NXT=0 DATA=FF  D0-7:11111111
#
# D0-7 is one char per line, D0 leftmost, so a mixed pattern names exactly
# which data lines are open and which are not.
#
import os
from amaranth import Module, Signal, Elaboratable, Cat, Const, Mux, Array


class UlpiPinMon(Elaboratable):
    BAUD_DIV = 521                      # 60 MHz sync / 115200

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)

        # sync (crystal) carries the UART so this keeps printing even if the
        # PHY clock is absent. usb rides the ULPI clock.
        _ph = os.environ.get("USB_PHASE", "raw")
        if _ph == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        elif _ph == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(_ph))

        # Reset PULSE then release (USB3300 RESET is active HIGH).
        rst_done = Signal(); rst_cnt = Signal(21)
        with m.If(~rst_done):
            m.d.sync += rst_cnt.eq(rst_cnt + 1)
            with m.If(rst_cnt == 1_200_000):          # ~20 ms
                m.d.sync += rst_done.eq(1)
        m.d.comb += [ulpi.rst.o.eq(~rst_done),
                     ulpi.data.oe.eq(0),              # never drive the bus
                     ulpi.stp.o.eq(0)]

        # Sample raw levels in the sync domain via 2-FF -- deliberately NOT the
        # usb domain, so a dead/absent PHY clock cannot stop the readout.
        def sync2(src, n):
            a = Signal(n); b = Signal(n)
            m.d.sync += [a.eq(src), b.eq(a)]
            return b
        d_s   = sync2(ulpi.data.i, 8)
        dir_s = sync2(ulpi.dir.i, 1)
        nxt_s = sync2(ulpi.nxt.i, 1)

        # ---- UART TX (8N1) ----
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
        def lit(s):
            return [ord(c) for c in s]
        bitchars = [Mux(d_s[i], ord('1'), ord('0')) for i in range(8)]

        parts = (lit("DIR=") + [Mux(dir_s, ord('1'), ord('0'))]
               + lit(" NXT=") + [Mux(nxt_s, ord('1'), ord('0'))]
               + lit(" DATA=") + [hexd(d_s[4:8]), hexd(d_s[0:4])]
               + lit(" D0-7:") + bitchars
               + lit("\r\n"))
        N = len(parts)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])

        latched = Signal(); throttle = Signal(24)
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
