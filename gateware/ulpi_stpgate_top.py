#!/usr/bin/env python3
#
# "DOES THE 1.5 MHz PULSE TRAIN ON DIR COME FROM THE PHY?"  +  "DOES RST WORK?"
#
# diredge measured DIR at position B as 419430 high cycles AND 419430 rising
# edges per 2^24 ULPI cycles -- i.e. ~419k isolated ONE-CYCLE pulses, one every
# 40 cycles = a clean periodic 1.5 MHz train at 2.5% duty. Position A's DIR is
# a quiet DC low. DH==DE rules out floating noise (noise gives multi-cycle
# highs) and rules out a real RxCmd (those are 2+ cycles and aperiodic).
#
# So something DRIVES that line. This test says whether it is the PHY: toggle
# the PHY's RESET and count DIR activity separately in each state.
#
#   H1/E1 ~ 0  while H0/E0 stay high  -> the train STOPS when the PHY is reset
#                                        => it comes from the PHY, and RST works
#   H1 ~ H0 and E1 ~ E0               -> the train is INDEPENDENT of the PHY
#                                        => DIR is not driven by the PHY
#                                           (open / crossed), or RST never
#                                           reaches the PHY
#
# EVERYTHING is in the 25 MHz crystal domain on an explicit BUFG. That is
# deliberate: if asserting RESET stops the PHY's CLKOUT, an ULPI-clocked
# counter would freeze and fake an "H1 ~ 0" result. The crystal cannot stop.
# Sampling a 16.7 ns pulse at 40 ns catches only ~40% of them, which is fine --
# we are separating "active" from "quiet", not calibrating a rate.
#
# USB3300 RESET is active HIGH. UART R3, 115200 8N1:
#     H0=xxxxxx E0=xxxxxx H1=xxxxxx E1=xxxxxx
#   *0 = PHY RUNNING (rst driven 0)      *1 = PHY IN RESET (rst driven 1)
#
from amaranth import (Module, Signal, Elaboratable, Instance, ClockDomain,
                      ClockSignal, Cat, Const, Mux, Array)


class UlpiStpGate(Elaboratable):
    REFHZ    = 25_000_000
    BAUD_DIV = REFHZ // 115200

    def elaborate(self, platform):
        m = Module()
        clk25 = platform.request("clk25", 0)
        ulpi  = platform.request("ulpi", 0)
        uart  = platform.request("uart_tx", 0)
        led   = platform.request("user_led", 0)

        m.domains += ClockDomain("sync")
        refbuf = Signal()
        m.submodules += Instance("BUFG", i_I=clk25.i, o_O=refbuf)
        m.d.comb += ClockSignal("sync").eq(refbuf)

        # never drive the bus; STP low; DIR/NXT are inputs we only observe
        m.d.comb += ulpi.data.oe.eq(0)

        # ---- 2x2 SWEEP: STP in {0,1} x RESET in {0,1} ---------------------
        # STP (SODIMM 101 / Y7) is the ONE ULPI signal never tested at position
        # B. In ULPI the link asserts STP to terminate a transfer, so an STP
        # stuck HIGH at the PHY (a floating CMOS input usually reads high) makes
        # the PHY refuse every TXCMD -- while it still clocks, still answers
        # RESET and still emits RxCmds. A 64-cycle trace showed NXT never
        # asserting, which is exactly that.
        #
        # The PHY's own DIR glitch train is the probe: if STP is CONNECTED,
        # driving it must change the PHY's behaviour, so DIR activity differs
        # between stp=0 and stp=1. If STP is OPEN, the two are identical.
        # The rst=1 quadrants are the BUILT-IN POSITIVE CONTROL -- rstgate
        # already proved DIR activity must collapse to ~0 there, so if it does,
        # the method is validated and the STP answer can be trusted.
        tick = Signal(25)
        m.d.sync += tick.eq(tick + 1)
        in_rst = Signal(); in_stp = Signal()
        m.d.comb += in_rst.eq(tick[24])          # active HIGH = PHY in reset
        m.d.comb += in_stp.eq(tick[23])
        m.d.comb += [ulpi.rst.o.eq(in_rst), ulpi.stp.o.eq(in_stp)]

        # Ignore the first 2^18 cycles (~10 ms) after each state change so the
        # PHY's reset entry/exit transient is not counted as activity.
        settled = Signal()
        m.d.comb += settled.eq(tick[18:23] != 0)

        dir_q = Signal()
        m.d.sync += dir_q.eq(ulpi.dir.i)
        rise = Signal()
        m.d.comb += rise.eq(ulpi.dir.i & ~dir_q)

        h0 = Signal(24); e0 = Signal(24); h1 = Signal(24); e1 = Signal(24)
        win = Signal(27)
        m.d.sync += win.eq(win + 1)
        with m.If(win == 0):
            m.d.sync += [h0.eq(0), e0.eq(0), h1.eq(0), e1.eq(0)]
        with m.Elif(settled):
            with m.If(~in_rst & ~in_stp):
                with m.If(ulpi.dir.i): m.d.sync += h0.eq(h0 + 1)
            with m.Elif(~in_rst & in_stp):
                with m.If(ulpi.dir.i): m.d.sync += e0.eq(e0 + 1)
            with m.Elif(in_rst & ~in_stp):
                with m.If(ulpi.dir.i): m.d.sync += h1.eq(h1 + 1)
            with m.Else():
                with m.If(ulpi.dir.i): m.d.sync += e1.eq(e1 + 1)

        rh0 = Signal(24); re0 = Signal(24); rh1 = Signal(24); re1 = Signal(24)
        with m.If(win == 0):
            m.d.sync += [rh0.eq(h0), re0.eq(e0), rh1.eq(h1), re1.eq(e1)]

        # ---- UART TX (8N1) ------------------------------------------------
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

        def field(name, s):
            return ([ord(c) for c in name] + [ord('=')]
                    + [hexd(s[20:24]), hexd(s[16:20]), hexd(s[12:16]),
                       hexd(s[8:12]), hexd(s[4:8]), hexd(s[0:4]), ord(' ')])

        chars = (field("RUNstp0", rh0) + field("RUNstp1", re0)
                 + field("RSTstp0", rh1) + field("RSTstp1", re1)
                 + [ord('\r'), ord('\n')])
        N = len(chars)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
        throttle = Signal(23); latched = Signal()
        m.d.sync += throttle.eq(throttle + 1)
        with m.If((throttle == 0) & ~latched):
            m.d.sync += [msg[i].eq(chars[i]) for i in range(N)]
            m.d.sync += latched.eq(1)

        pidx = Signal(range(N + 1))
        m.d.sync += tx_start.eq(0)
        with m.If(latched & ~tx_busy & ~tx_start):
            with m.If(pidx == N):
                m.d.sync += [pidx.eq(0), latched.eq(0)]
            with m.Else():
                m.d.sync += [tx_byte.eq(msg[pidx]), tx_start.eq(1),
                             pidx.eq(pidx + 1)]

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
