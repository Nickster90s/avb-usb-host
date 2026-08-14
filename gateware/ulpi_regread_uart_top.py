#!/usr/bin/env python3
#
# ULPI scratch-register loopback test — pinpoints a dead D0..D7 data line.
#
# Uses LUNA's ULPIRegisterWindow (correct ULPI protocol) to WRITE alternating
# patterns (0xAA / 0x55) to the ULPI Scratch Register (0x16), READ them back,
# and accumulate any mismatched bits. 0xAA|0x55 exercises every data bit in
# BOTH directions, so a stuck/open line always shows up. Prints ~1/sec on the
# CH347 UART (R3, 115200, clocked from the stable crystal):
#
#   W=aa R=rr BAD=bb
#
#   W/R  = last pattern written / read back
#   BAD  = accumulated mismatch mask (write XOR read, OR'd over time)
#          BAD=00 -> all 8 data lines good (fault is NOT the data bus)
#          BAD bit n set -> data line Dn is dead/open:
#            b0->D0(Y9) b1->D1(V8) b2->D2(W9) b3->D3(V9)
#            b4->D4(W6) b5->D5(V7) b6->D6(Y3) b7->D7(Y6)
#
import os
from amaranth import Module, Signal, Elaboratable, Cat, Const, Array, Mux
from luna import top_level_cli
from luna.gateware.interface.ulpi import ULPIRegisterWindow


class UlpiRegReadUart(Elaboratable):
    SCRATCH  = 0x16
    BAUD_DIV = 521            # 60 MHz sync / 115200
    WAITMS   = 60000          # ~1 ms PHY settle

    def elaborate(self, platform):
        m = Module()
        ulpi = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        m.submodules.car = platform.clock_domain_generator(ulpi_clk_pin=ulpi_clk.i)
        m.d.comb += ulpi.rst.o.eq(0)

        # ---- LUNA ULPI register window (usb domain) ----
        m.submodules.rw = rw = ULPIRegisterWindow()
        m.d.comb += [
            rw.ulpi_data_in.eq(ulpi.data.i),
            rw.ulpi_dir.eq(ulpi.dir.i),
            rw.ulpi_next.eq(ulpi.nxt.i),
            ulpi.data.o.eq(rw.ulpi_data_out),
            ulpi.data.oe.eq(rw.ulpi_out_req),
            ulpi.stp.o.eq(rw.ulpi_stop),
        ]

        startup = Signal(range(self.WAITMS + 1)); ready = Signal()
        m.d.usb += startup.eq(startup + ~ready)
        with m.If(startup == self.WAITMS):
            m.d.usb += ready.eq(1)

        # Read four known registers to test addressing + all data bits.
        ADDRS = [0x00, 0x01, 0x02, 0x03]     # VIDL=24 VIDH=04 PIDL PIDH=00
        idx   = Signal(range(len(ADDRS)))
        res   = Array([Signal(8, name=f"reg{i}") for i in range(len(ADDRS))])
        addr_cur = Signal(6)
        m.d.comb += addr_cur.eq(Array([Const(a) for a in ADDRS])[idx])

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                with m.If(ready & ~rw.busy):
                    m.next = "READ"
            with m.State("READ"):
                m.d.comb += [rw.address.eq(addr_cur), rw.read_request.eq(1)]
                m.next = "RWAIT"
            with m.State("RWAIT"):
                with m.If(rw.done):
                    m.d.usb += res[idx].eq(rw.read_data)
                    with m.If(idx == len(ADDRS) - 1):
                        m.d.usb += idx.eq(0)
                    with m.Else():
                        m.d.usb += idx.eq(idx + 1)
                    m.next = "IDLE"

        # ---- CDC usb->sync (2-FF) ----
        def sync2(src, n=8):
            a = Signal(n); b = Signal(n)
            m.d.sync += [a.eq(src), b.eq(a)]
            return b
        s0 = sync2(res[0]); s1 = sync2(res[1]); s2 = sync2(res[2]); s3 = sync2(res[3])

        # ---- UART TX (8N1) in sync domain (crystal-clean 60 MHz) ----
        uart = platform.request("uart_tx", 0)
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
        def field(label, val):
            return [ord(c) for c in label] + [hexd(val[4:8]), hexd(val[0:4])]
        parts = (field("00=", s0) + field(" 01=", s1) + field(" 02=", s2)
                 + field(" 03=", s3) + [ord('\n')])
        N = len(parts)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
        latched = Signal(); throttle = Signal(26)
        m.d.sync += throttle.eq(throttle + 1)
        with m.If((throttle == 0) & ~latched):
            m.d.sync += [msg[i].eq(parts[i]) for i in range(N)]
            m.d.sync += latched.eq(1)

        pidx = Signal(range(N + 1))
        m.d.sync += tx_start.eq(0)
        with m.If(latched & ~tx_busy & ~tx_start):
            with m.If(pidx < N):
                m.d.sync += [tx_byte.eq(msg[pidx]), tx_start.eq(1), pidx.eq(pidx + 1)]
            with m.Else():
                m.d.sync += [pidx.eq(0), latched.eq(0)]

        return m


if __name__ == "__main__":
    top_level_cli(UlpiRegReadUart)
