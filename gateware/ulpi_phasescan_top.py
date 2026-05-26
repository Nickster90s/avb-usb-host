#!/usr/bin/env python3
#
# ULPI RX timing eye-scan tool.
#
# Reads USB3300 ULPI register 0x00 (Vendor ID Low = 0x24 on SMSC/
# Microchip) over and over and checks the read-back. The usb-domain
# clock phase is set per build via USB_PHASE (PLL). Sweep USB_PHASE
# and watch the LED: the phases that read 0x24 correctly are the
# working window; its centre is the phase to lock in.
#
# Why this and not IDELAY/IDDR: those crash openXC7's prjxray FASM
# backend, so we sweep the PLL output phase instead (proven to build).
#
#   LED FAST blink → register 0x00 read back as 0x24 (RX timing OK here)
#   LED SLOW blink → wrong/never read (RX timing bad at this phase)
#
# Usage: for ph in 0 22.5 45 ... ; do USB_PHASE=$ph build+load; read LED
#
# ULPI register read protocol (ULPI 1.1):
#   1. bus idle (dir=0): link drives TXCMD = 0b11_aaaaaa (REGR|addr).
#      reg 0x00 → 0xC0.
#   2. PHY asserts NXT to accept the command byte.
#   3. link stops driving (turnaround); PHY asserts DIR.
#   4. PHY drives the register data on the bus (dir=1, nxt=0) → capture.
#   5. PHY drops DIR.

import os
from amaranth import (Module, Signal, Elaboratable, Mux, ClockSignal,
                      ResetSignal)
from luna import top_level_cli


class UlpiPhaseScan(Elaboratable):
    EXPECT      = 0x24                 # USB3300 vendor-id-low
    CMD_REGR    = 0xC0                 # REGR | 0x00
    CYCLES_1MS  = 60_000

    def elaborate(self, platform):
        m = Module()

        ulpi_clk = platform.request("ulpi_clock", 0)
        ulpi     = platform.request("ulpi", 0)

        # usb clock: PLL phase (numeric), or raw/inv via BUFG.
        env = os.environ.get("USB_PHASE", "0")
        if env == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        elif env == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(env))

        m.d.comb += ulpi.rst.o.eq(0)         # release transceiver (active-high)

        # ULPI data tristate.
        drive = Signal(8)
        m.d.comb += [
            ulpi.data.o .eq(drive),
            ulpi.data.oe.eq(~ulpi.dir.i),     # we drive only when DIR low
        ]

        dir_i  = ulpi.dir.i
        nxt_i  = ulpi.nxt.i
        data_i = ulpi.data.i

        startup   = Signal(range(self.CYCLES_1MS + 1))
        ready     = Signal()
        match_seen = Signal()
        captured  = Signal(8)

        m.d.usb += startup.eq(Mux(ready, startup, startup + 1))
        with m.If(startup == self.CYCLES_1MS):
            m.d.usb += ready.eq(1)

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                m.d.comb += [drive.eq(0x00), ulpi.stp.o.eq(0)]
                with m.If(ready & ~dir_i):
                    m.next = "CMD"

            with m.State("CMD"):
                # Drive the read command until the PHY accepts it (NXT).
                m.d.comb += [drive.eq(self.CMD_REGR), ulpi.stp.o.eq(0)]
                with m.If(dir_i):            # PHY grabbed bus unexpectedly
                    m.next = "IDLE"
                with m.Elif(nxt_i):          # command accepted
                    m.next = "TURN"

            with m.State("TURN"):
                # Stop driving; wait for PHY to assert DIR (it owns bus now).
                m.d.comb += [drive.eq(0x00), ulpi.stp.o.eq(0)]
                with m.If(dir_i):
                    m.next = "DATA"

            with m.State("DATA"):
                # DIR high, NXT low → register data is on the bus.
                m.d.comb += [drive.eq(0x00), ulpi.stp.o.eq(0)]
                m.d.usb += captured.eq(data_i)
                with m.If(captured == self.EXPECT):
                    m.d.usb += match_seen.eq(1)
                # When DIR drops, the PHY has released the bus.
                with m.If(~dir_i):
                    m.next = "IDLE"

        # LED: fast once we've ever read 0x24 correctly, slow otherwise.
        led     = platform.request("user_led", 0)
        counter = Signal(26)
        m.d.usb += counter.eq(counter + 1)
        m.d.comb += led.o.eq(Mux(match_seen, counter[21], counter[25]))

        return m


if __name__ == "__main__":
    top_level_cli(UlpiPhaseScan)
