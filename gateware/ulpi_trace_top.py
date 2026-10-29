#!/usr/bin/env python3
#
# 64-CYCLE ULPI BUS TRACE — stop inferring the waveform from counters.
#
# Every diagnostic so far reported RATES (ACC/ABRT/TO/CAP), which forced us to
# infer what the bus actually does. This records the real thing: 64 consecutive
# ULPI clock cycles starting the moment we begin driving a register-read
# command, then prints the buffer over the UART forever so it can be read back
# reliably at 115200.
#
# Word layout, one per ULPI cycle, printed as 4 hex digits:
#     bit 15..13  FSM state (0=IDLE 1=CMD 2=TURN 3=DATA)
#     bit 12      our data-bus output enable (1 = the FPGA is driving D[7:0])
#     bit 11      NXT as sampled
#     bit 10      DIR as sampled
#     bit 9..8    (zero)
#     bit 7..0    D[7:0] as sampled
#
# A HEALTHY register read looks like:
#     state=CMD  oe=1 data=C0            we drive the REGR command
#     state=CMD  oe=1 nxt=1              PHY accepts it
#     state=TURN dir=1                   PHY takes the bus (turnaround cycle)
#     state=DATA dir=1 oe=0 data=24      the register value arrives
# Anything else tells us exactly which of those four steps is missing.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Cat, Const, Mux, Array,
                      ClockSignal)
from amaranth.hdl.mem import Memory


class UlpiTrace(Elaboratable):
    DEPTH       = 64
    # 0xFF = REGR of register 0x3F. PERMUTATION-PROOF: under ANY reordering of
    # the 8 data wires, 0xFF still arrives at the PHY as 0xFF, i.e. still a
    # valid REGR opcode. 0xC0 does NOT have that property -- permute it and it
    # can arrive as 0x03 (reserved/IDLE range), which is not a command at all,
    # so the PHY would never assert NXT. That is exactly position B's symptom.
    # Also SAFE: 0xFF is not in the REGW range 0x80-0xBF, so it cannot corrupt
    # the PHY's configuration the way a blind byte sweep did.
    #   NXT asserts on 0xFF -> wires all deliver, fault is ORDERING (gateware)
    #   NXT still dead      -> data genuinely not getting through (physical)
    CMD_REGR    = int(_os.environ.get("ULPI_CMD", "0xFF"), 0)
    BAUD_DIV    = 521                  # 60 MHz sync / 115200
    CYCLES_1MS  = 60_000

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        # CLOCKING MODE MATTERS AND WAS NEVER CONTROLLED FOR (2026-10-28).
        # usb_utmi_top.py -- the design that actually enumerates on PHY-A --
        # uses usb_pll=False, i.e. the RAW ULPI clock straight into cd_usb via
        # BUFG, exactly as LUNA/usbsniffer do.  Every diagnostic in this tree
        # used the CAR default usb_pll=True instead, so the whole position-B
        # campaign ran in a clocking mode the known-good configuration does not
        # use.  A PLL fed from a 60 MHz hand-wired lead can lock and still
        # deliver a badly jittered output, which would produce exactly the
        # observed symptom: inputs that read self-contradictorily at EVERY
        # phase, while the same pins read cleanly in the crystal-derived
        # 200 MHz domain.
        #   USB_RAW=1  -> usb_pll=False, raw ULPI clock (matches usb_utmi_top)
        #   USB_INV=1  -> additionally sample on the FALLING edge
        _raw = _os.environ.get("USB_RAW", "") == "1"
        _inv = _os.environ.get("USB_INV", "") == "1"
        if _raw:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=_inv)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i,
                usb_phase=float(_os.environ.get("USB_PHASE", "0")))

        m.d.comb += ulpi.rst.o.eq(0)          # release PHY (reset is active HIGH)

        # ULPI REQUIRES THE LINK TO DRIVE THE BUS WHENEVER DIR IS LOW, parked
        # at 0x00 (NOP) when idle. The first version of this trace tri-stated in
        # IDLE/TURN, which left the bus floating: on the KNOWN-GOOD position A
        # the PHY then never asserted DIR, no read ever completed, and NXT read
        # stuck HIGH (a floating input, not a PHY state). That made the
        # instrument fail its own control and its position-B result worthless.
        # Drive unconditionally while DIR is low, exactly as the working
        # phasescan does.
        drive = Signal(8)
        oe    = Signal()
        m.d.comb += [ulpi.data.o.eq(drive),
                     oe.eq(~ulpi.dir.i),
                     ulpi.data.oe.eq(oe),
                     ulpi.stp.o.eq(0)]
        dir_i, nxt_i, data_i = ulpi.dir.i, ulpi.nxt.i, ulpi.data.i

        # let the PHY settle after reset release before we trigger
        startup = Signal(range(self.CYCLES_1MS + 1)); ready = Signal()
        m.d.usb += startup.eq(Mux(ready, startup, startup + 1))
        with m.If(startup == self.CYCLES_1MS):
            m.d.usb += ready.eq(1)

        st      = Signal(3)
        wptr    = Signal(range(self.DEPTH + 1))
        done    = Signal()
        rec     = Signal()

        mem   = Memory(width=16, depth=self.DEPTH)
        m.submodules.wr = wr = mem.write_port(domain="usb")
        m.submodules.rd = rd = mem.read_port(domain="sync")

        word = Cat(data_i, Const(0, 2), dir_i, nxt_i, oe, st)
        m.d.comb += [wr.addr.eq(wptr), wr.data.eq(word),
                     wr.en.eq(rec & ~done)]
        with m.If(rec & ~done):
            m.d.usb += wptr.eq(wptr + 1)
            with m.If(wptr == self.DEPTH - 1):
                m.d.usb += done.eq(1)

        to_cnt = Signal(12)
        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                m.d.comb += [drive.eq(0x00), st.eq(0)]
                m.d.usb += to_cnt.eq(0)
                with m.If(ready & ~dir_i & ~done):
                    m.d.usb += rec.eq(1)        # trigger: start recording
                    m.next = "CMD"
            with m.State("CMD"):
                m.d.comb += [drive.eq(self.CMD_REGR), st.eq(1)]
                m.d.usb += to_cnt.eq(to_cnt + 1)
                with m.If(nxt_i):
                    m.next = "TURN"
                with m.Elif(to_cnt.all()):
                    m.next = "IDLE"
            with m.State("TURN"):
                m.d.comb += [drive.eq(0x00), st.eq(2)]
                m.d.usb += to_cnt.eq(to_cnt + 1)
                with m.If(dir_i):
                    m.next = "DATA"
                with m.Elif(to_cnt.all()):
                    m.next = "IDLE"
            with m.State("DATA"):
                m.d.comb += [drive.eq(0x00), st.eq(3)]
                m.d.usb += to_cnt.eq(to_cnt + 1)
                with m.If(~dir_i | to_cnt.all()):
                    m.next = "IDLE"

        # ---------------- UART dump of the frozen buffer -------------------
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

        done_s = Signal(); d1 = Signal()
        m.d.sync += [d1.eq(done), done_s.eq(d1)]

        idx   = Signal(range(self.DEPTH))
        sub   = Signal(3)
        gap   = Signal(22)
        m.d.comb += rd.addr.eq(idx)
        w     = rd.data
        chars = Array([hexd(w[12:16]), hexd(w[8:12]), hexd(w[4:8]),
                       hexd(w[0:4]), Const(ord(' '), 8)])

        m.d.sync += tx_start.eq(0)
        with m.FSM(domain="sync"):
            with m.State("WAIT"):
                with m.If(done_s):
                    m.d.sync += [idx.eq(0), sub.eq(0)]
                    m.next = "EMIT"
            with m.State("EMIT"):
                with m.If(~tx_busy & ~tx_start):
                    with m.If(sub < 5):
                        m.d.sync += [tx_byte.eq(chars[sub]), tx_start.eq(1),
                                     sub.eq(sub + 1)]
                    with m.Else():
                        m.d.sync += sub.eq(0)
                        with m.If(idx == self.DEPTH - 1):
                            m.next = "NL"
                        with m.Else():
                            m.d.sync += idx.eq(idx + 1)
                            # newline every 8 words for readability
                            with m.If(idx[0:3] == 7):
                                m.next = "NL8"
            with m.State("NL8"):
                with m.If(~tx_busy & ~tx_start):
                    m.d.sync += [tx_byte.eq(ord('\n')), tx_start.eq(1)]
                    m.next = "EMIT"
            with m.State("NL"):
                with m.If(~tx_busy & ~tx_start):
                    m.d.sync += [tx_byte.eq(ord('\n')), tx_start.eq(1),
                                 gap.eq(0)]
                    m.next = "GAP"
            with m.State("GAP"):
                m.d.sync += gap.eq(gap + 1)
                with m.If(gap.all()):
                    m.d.sync += [idx.eq(0), sub.eq(0)]
                    m.next = "EMIT"

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
