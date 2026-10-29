#!/usr/bin/env python3
#
# ULPI CLK-vs-DATA SKEW meter -> onboard LED (no UART).
#
# The backup enumerates HS but corrupts descriptors (error -71): its 8 data
# bits are on scattered pins with non-uniform clk-to-pin delay, so some are
# sampled off-centre. This counts HOW MANY data bits are timing-marginal at
# the built phase. It drives continuous ULPI register reads for bus traffic
# and samples the data bus TWICE per cycle -- on the ULPI clock RISING edge
# (d_r) and FALLING edge (d_f). A well-aligned bit is stable across the cycle
# (d_r==d_f); a SKEWED bit disagrees. A sticky per-bit mask records any
# disagreement while the PHY drives the bus (dir=1).
#
# LED = the mask's popcount, blinked out:  N blinks, pause, repeat.
#   0 blinks (LED dark)  = ALL 8 data bits clean at this phase  <- goal
#   N blinks             = N data bits are skew/marginal
# Sweep USB_PHASE (build arg) and watch the blink count drop; the phase with
# 0 blinks is the timing centre. If NO phase reaches 0, the worst pins need
# to move to a better-matched location.
#
import os
from amaranth import (Module, Signal, Elaboratable, Const, Array, Mux,
                      ClockDomain, ClockSignal)
from luna.gateware.interface.ulpi import ULPIRegisterWindow


class UlpiSkewLed(Elaboratable):
    WAITMS = 60000

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        led      = platform.request("user_led", 0)

        _ph = os.environ.get("USB_PHASE", "0")
        if _ph == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        elif _ph == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(_ph))
        m.d.comb += ulpi.rst.o.eq(0)

        # continuous ULPI register reads -> bus traffic (dir/data toggling)
        m.submodules.rw = rw = ULPIRegisterWindow()
        m.d.comb += [
            rw.ulpi_data_in.eq(ulpi.data.i), rw.ulpi_dir.eq(ulpi.dir.i),
            rw.ulpi_next.eq(ulpi.nxt.i), ulpi.data.o.eq(rw.ulpi_data_out),
            ulpi.data.oe.eq(rw.ulpi_out_req), ulpi.stp.o.eq(rw.ulpi_stop),
        ]
        startup = Signal(range(self.WAITMS + 1)); ready = Signal()
        m.d.usb += startup.eq(startup + ~ready)
        with m.If(startup == self.WAITMS):
            m.d.usb += ready.eq(1)
        ADDRS = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05]
        idx = Signal(range(len(ADDRS)))
        m.d.comb += rw.address.eq(Array([Const(a) for a in ADDRS])[idx])
        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                with m.If(ready & ~rw.busy):
                    m.next = "READ"
            with m.State("READ"):
                m.d.comb += rw.read_request.eq(1)
                m.next = "WAIT"
            with m.State("WAIT"):
                with m.If(rw.done):
                    m.d.usb += idx.eq(Mux(idx == len(ADDRS)-1, 0, idx+1))
                    m.next = "IDLE"

        # double-edge sample; compare while the PHY drives (dir=1)
        m.domains += ClockDomain("usbn", local=True)
        m.d.comb += ClockSignal("usbn").eq(~ClockSignal("usb"))
        d_r = Signal(8); d_f = Signal(8); d_f_u = Signal(8); dir_r = Signal()
        m.d.usb  += d_r.eq(ulpi.data.i)
        m.d.usbn += d_f.eq(ulpi.data.i)
        m.d.usb  += [d_f_u.eq(d_f), dir_r.eq(ulpi.dir.i)]
        skew = Signal(8)
        with m.If(dir_r & ready):
            m.d.usb += skew.eq(skew | (d_r ^ d_f_u))
        winr = Signal(26); disp = Signal(8)
        m.d.usb += winr.eq(winr + 1)
        with m.If(winr == 0):
            m.d.usb += [disp.eq(skew), skew.eq(0)]

        # popcount of disp (0..8)
        pc = Signal(4)
        m.d.comb += pc.eq(sum((disp[i] for i in range(8)), Const(0, 4)))
        pc_s = Signal(4); _p = Signal(4)                # CDC usb->sync
        m.d.sync += [_p.eq(pc), pc_s.eq(_p)]

        # timebase ~0.18 s tick in the crystal-clean sync domain
        TICK = 11_000_000
        tick = Signal(range(TICK)); t = Signal()
        m.d.sync += tick.eq(tick + 1)
        with m.If(tick == TICK - 1):
            m.d.sync += [tick.eq(0), t.eq(1)]
        with m.Else():
            m.d.sync += t.eq(0)

        # frame = 2*pc blink-ticks (on/off) + 6-tick dark gap; repeat
        pos = Signal(6); flen = Signal(6); blinkn = Signal(6)
        with m.If(t):
            with m.If(pos + 1 >= flen):
                m.d.sync += [pos.eq(0), blinkn.eq(pc_s << 1),
                             flen.eq((pc_s << 1) + 6)]
            with m.Else():
                m.d.sync += pos.eq(pos + 1)
        m.d.comb += led.o.eq((pos < blinkn) & ~pos[0])   # pc pulses, then gap
        return m
