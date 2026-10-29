#!/usr/bin/env python3
#
# ULPI PER-BIT data-line test -> UART (R3, 115200, 8N1).
#
# Writes walking patterns to the PHY Scratch register (0x16) and reads them
# back at max register rate, per bit, accumulating any mismatch over a window.
# Prints once per window:
#
#   D0=OK D1=OK D2=BAD D3=OK D4=OK D5=BAD D6=OK D7=OK
#
#   OK  = that data line read every pattern back correctly
#   BAD = that data line mis-read at least once (mis-wired / stuck / marginal)
#
# (This is register-rate: it catches gross/marginal wiring, not HS-only skew.
#  If all OK, wiring is good and the HS problem is pure eye margin.)
# Sweep USB_PHASE (build arg) to see which bits clean up at which phase.
#
import os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Array, Mux)
from luna.gateware.interface.ulpi import ULPIRegisterWindow


class UlpiBitUart(Elaboratable):
    SCRATCH  = 0x16
    BAUD_DIV = 521            # 60 MHz sync / 115200
    WAITMS   = 60000

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)

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

        PATS = [0xAA, 0x55, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80,
                0xFE, 0xFD, 0xFB, 0xF7, 0xEF, 0xDF, 0xBF, 0x7F]
        pidx = Signal(range(len(PATS))); pat = Signal(8)
        m.d.comb += pat.eq(Array([Const(p) for p in PATS])[pidx])
        m.d.comb += [rw.address.eq(self.SCRATCH), rw.write_data.eq(pat)]   # HELD

        err_acc = Signal(8); win = Signal(20); bad = Signal(8)
        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                with m.If(ready & ~rw.busy):
                    m.next = "WR"
            with m.State("WR"):
                m.d.comb += rw.write_request.eq(1); m.next = "WRW"
            with m.State("WRW"):
                with m.If(rw.done):
                    m.next = "RD"
            with m.State("RD"):
                m.d.comb += rw.read_request.eq(1); m.next = "RDW"
            with m.State("RDW"):
                with m.If(rw.done):
                    m.d.usb += [err_acc.eq(err_acc | (rw.read_data ^ pat)),
                                pidx.eq(Mux(pidx == len(PATS)-1, 0, pidx + 1)),
                                win.eq(win + 1)]
                    with m.If(win == 0):
                        m.d.usb += [bad.eq(err_acc), err_acc.eq(0)]
                    m.next = "IDLE"

        # CDC usb->sync
        bad_s = Signal(8); _b = Signal(8)
        m.d.sync += [_b.eq(bad), bad_s.eq(_b)]

        # UART TX (8N1) in sync domain
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

        # message: "D0=OK D1=BAD ... D7=OK\r\n"  (OK->"OK " so fields align)
        chars = []
        for i in range(8):
            b = bad_s[i]
            chars += [ord('D'), ord('0') + i, ord('='),
                      Mux(b, ord('B'), ord('O')),
                      Mux(b, ord('A'), ord('K')),
                      Mux(b, ord('D'), ord(' ')),
                      ord(' ')]
        chars += [ord('\r'), ord('\n')]
        N = len(chars)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
        thr = Signal(24); latched = Signal()
        m.d.sync += thr.eq(thr + 1)
        with m.If((thr == 0) & ~latched):
            m.d.sync += [msg[i].eq(chars[i]) for i in range(N)]
            m.d.sync += latched.eq(1)
        pidx2 = Signal(range(N + 1)); m.d.sync += tx_start.eq(0)
        with m.If(latched & ~tx_busy & ~tx_start):
            with m.If(pidx2 == N):
                m.d.sync += [pidx2.eq(0), latched.eq(0)]
            with m.Else():
                m.d.sync += [tx_byte.eq(msg[pidx2]), tx_start.eq(1),
                             pidx2.eq(pidx2 + 1)]
        return m
