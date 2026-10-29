#!/usr/bin/env python3
#
# DIR vs NXT RELEASE-DECAY — chase the one asymmetry left standing.
#
# State of play (2026-10-28, clock hard-rewired to SODIMM 87 and PROVEN to be
# PHY-B's own CLKOUT, 72 ppm off PHY-A's):
#   * the FPGA->PHY direction WORKS: the PHY asserts NXT for our TXCMD, and
#     `34C0` (nxt captured = 1) appears cleanly at all 8 clock phases;
#   * DIR never comes back cleanly at ANY phase -- it only ever shows up as the
#     impossible word `28C0`, oe=0 together with dir=0, where `oe = ~dir_i`
#     combinationally in the SAME captured word.
# NXT and DIR are both PHY outputs on identical PCB nets (178 mil, no via,
# SODIMM 103 and 97).  So the fault is specific to DIR.
#
# Method: drive the pin HIGH, release to high-Z, and time how many cycles until
# the input buffer reads LOW.  That measures how hard the FAR END pulls the net
# back -- unlike a contention readback, which is useless here because our own
# driver always wins at our own pad.  DIR and NXT are measured in the SAME pass
# under identical conditions, so each is the other's control.  The 8 data pins
# are measured alongside as a second reference group whose decay is already
# known (109-123 came out 138-159 at 5 ns/count at BOTH positions).
#
# Counts in the CAR's 200 MHz "fast" domain = 5 ns per count.
#
# UART (R3, 115200 8N1):
#     DIR=xxxx NXT=xxxx D0=xxxx D7=xxxx
#
# DIR >> NXT  -> DIR's net is not being pulled back: open/resistive far end, or
#                the PHY's DIR driver is not driving it.  That is the fault.
# DIR ~= NXT  -> both ends behave alike; DIR's problem is not static drive and
#                the next suspect is edge RATE under load, which needs a scope.
# Build the position-A control (no USB_BACKUP) for absolute scale.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Mux, Array,
                      ClockSignal)


class UlpiCtlDecay(Elaboratable):
    CHARGE   = 4096                 # fast-domain cycles driven high
    CAP      = 0xFFFF
    BAUD_DIV = 521                  # 60 MHz sync / 115200
    NAMES    = ["DIR", "NXT", "D0", "D7"]

    def elaborate(self, platform):
        m = Module()
        ctl      = platform.request("ulpi_ctl_io", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi_clk.i,
            usb_phase=float(_os.environ.get("USB_PHASE", "0")))

        # PHY RELEASED (reset is active HIGH) so its outputs are live and we
        # measure the real driver, not a tri-stated pin.  STP parked low.
        m.d.comb += [ctl.rst.o.eq(0), ctl.stp.o.eq(0)]

        charging = Signal()
        # all four pins under test are driven high together, then released
        m.d.comb += [
            ctl.dir.o.eq(1),  ctl.dir.oe.eq(charging),
            ctl.nxt.o.eq(1),  ctl.nxt.oe.eq(charging),
            ctl.data.o.eq(0xFF),
            ctl.data.oe.eq(Cat(charging, Const(0, 6), charging)),  # d0 and d7
        ]
        pins = [ctl.dir.i, ctl.nxt.i, ctl.data.i[0], ctl.data.i[7]]
        N = len(pins)

        cnt  = Signal(range(self.CHARGE + 1))
        dec  = Signal(16)
        done = Array([Signal(name=f"dn{i}") for i in range(N)])
        res  = Array([Signal(16, name=f"rs{i}") for i in range(N)])
        rep  = Array([Signal(16, name=f"rp{i}") for i in range(N)])

        with m.FSM(domain="fast"):
            with m.State("CHARGE"):
                m.d.comb += charging.eq(1)
                m.d.fast += cnt.eq(cnt + 1)
                with m.If(cnt == self.CHARGE - 1):
                    m.d.fast += [cnt.eq(0), dec.eq(0)]
                    for i in range(N):
                        m.d.fast += [done[i].eq(0), res[i].eq(self.CAP)]
                    m.next = "DECAY"
            with m.State("DECAY"):
                m.d.fast += dec.eq(dec + 1)
                for i in range(N):
                    with m.If(~done[i] & ~pins[i]):
                        m.d.fast += [done[i].eq(1), res[i].eq(dec)]
                allz = Cat(*[done[i] for i in range(N)]).all()
                with m.If(allz | (dec == self.CAP)):
                    for i in range(N):
                        m.d.fast += rep[i].eq(res[i])
                    m.next = "CHARGE"

        # snapshot into the sync domain on a slow strobe, then 2-FF sync
        hold = Array([Signal(16, name=f"hd{i}") for i in range(N)])
        snap = Signal(20)
        m.d.fast += snap.eq(snap + 1)
        with m.If(snap.all()):
            for i in range(N):
                m.d.fast += hold[i].eq(rep[i])
        s1 = Array([Signal(16, name=f"s1_{i}") for i in range(N)])
        s2 = Array([Signal(16, name=f"s2_{i}") for i in range(N)])
        for i in range(N):
            m.d.sync += [s1[i].eq(hold[i]), s2[i].eq(s1[i])]

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

        chars = []
        for i, nm in enumerate(self.NAMES):
            chars += [Const(ord(c), 8) for c in nm] + [Const(ord('='), 8)]
            chars += [hexd(s2[i][4 * (3 - k):4 * (3 - k) + 4]) for k in range(4)]
            chars += [Const(ord(' '), 8)]
        chars += [Const(ord('\r'), 8), Const(ord('\n'), 8)]
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
