#!/usr/bin/env python3
#
# RUNTIME IDELAY TAP SWEEP — all 32 input-delay taps in ONE bitstream.
#
# Why this instrument exists (2026-10-28):
#   trace64 on the reworked board (clock hard-wired to SODIMM 87/AA4) showed
#   for the FIRST time that the PHY ACCEPTS the command: NXT asserts in
#   response to TXCMD 0xC0.  So the FPGA->PHY direction works and the failure
#   is now on the INPUT side.  The same trace showed two self-contradictory
#   captures in one cycle -- `oe` (= ~dir_i, combinational) read 0 while the
#   recorded dir_i read 0, and the FSM's copy of nxt_i read 0 while the trace's
#   copy read 1.  One input resolving differently on two fanout paths in the
#   same cycle is a setup/hold violation, not a connectivity fault.
#   openXC7 drops IOB=TRUE (no input flops in the IOB), so every input is
#   captured in fabric with its own skew.  IDELAYE2 delays INPUTS ONLY, which
#   is exactly the direction that is now broken.
#
# The tap advances at RUNTIME, so one bitstream sweeps all 32 instead of
# needing 32 builds.  Mode is **VARIABLE**, not VAR_LOAD: VAR_LOAD needs
# CNTVALUEIN[4:0] + LD routed into every IDELAY, which for 10 of them is 60
# extra fabric nets into the IO column and makes nextpnr thrash forever at
# "overused=3" (it ran 4700 router iterations without converging).  VARIABLE
# needs one shared CE net with INC tied high, so the tap steps up by one per CE
# pulse and the sweep costs 1 routed net instead of 60.  Per tap it runs TRIALS register reads
# of ULPI reg 0x00 (expect 0x24, the USB3300 vendor-ID low byte) and prints:
#
#     P=00 T=00 OK=0000 D=xx H=xx
#      |     |     |      `- last data byte captured in the turnaround
#      |     |     `-------- how many of TRIALS returned 0x24
#      |     `-------------- IDELAY tap 0..31 (~78 ps each at 200 MHz refclk)
#      `-------------------- USB_PHASE this bitstream was built at
#
# PASS = any tap with OK != 0000.  The centre of the OK window is the tap to
# lock in.  All 32 taps reading OK=0000 means the input eye is closed at this
# clock phase -- rebuild with another USB_PHASE and sweep again.
#
import os as _os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockDomain,
                      ClockSignal, Cat, Const, Mux, Array, Memory)


class UlpiTapScan(Elaboratable):
    REFHZ      = 25_000_000
    BAUD_DIV   = 521                   # 60 MHz sync / 115200
    CYCLES_1MS = 60_000
    TAPS       = 32
    TRIALS     = 256
    SETTLE     = 1024                  # cycles to wait after loading a tap
    TIMEOUT    = 64                    # per-attempt timeout, usb cycles

    def __init__(self):
        self.phase = float(_os.environ.get("USB_PHASE", "0"))

    def elaborate(self, platform):
        m = Module()
        ulpi     = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=self.phase)

        m.d.comb += [ulpi.rst.o.eq(0), ulpi.stp.o.eq(0)]

        drive = Signal(8)
        m.d.comb += [ulpi.data.o.eq(drive),
                     ulpi.data.oe.eq(~ulpi.dir.i)]   # RAW dir gates the driver

        # ---------------- IDELAYE2 on every input, VAR_LOAD ------------------
        idc_rst = Signal(reset=1); idc_cnt = Signal(8)
        with m.If(~idc_cnt.all()):
            m.d.sync += idc_cnt.eq(idc_cnt + 1)
        with m.Else():
            m.d.sync += idc_rst.eq(0)
        m.submodules.idelayctrl = Instance("IDELAYCTRL",
            i_REFCLK=ClockSignal("fast"), i_RST=idc_rst)

        tap = Signal(5)          # display/bookkeeping only
        ld  = Signal()           # one-cycle CE pulse: step the tap up by one

        def _idly(sig, nm):
            o = Signal(name="idly_" + nm)
            m.submodules["idelay_" + nm] = Instance("IDELAYE2",
                p_IDELAY_TYPE="VARIABLE", p_DELAY_SRC="IDATAIN",
                p_HIGH_PERFORMANCE_MODE="TRUE", p_SIGNAL_PATTERN="DATA",
                p_REFCLK_FREQUENCY=200.0, p_CINVCTRL_SEL="FALSE",
                p_PIPE_SEL="FALSE", p_IDELAY_VALUE=0,
                i_C=ClockSignal("usb"), i_LD=0, i_CNTVALUEIN=0,
                i_CE=ld, i_INC=1, i_LDPIPEEN=0, i_REGRST=0,
                i_IDATAIN=sig, o_DATAOUT=o)
            return o

        dir_i  = _idly(ulpi.dir.i, "dir")
        nxt_i  = _idly(ulpi.nxt.i, "nxt")
        data_i = Cat(*[_idly(ulpi.data.i[i], f"d{i}") for i in range(8)])

        # ---------------- results store: one 16-bit word per tap -------------
        res = Memory(width=24, depth=self.TAPS)
        m.submodules.res_wr = res_wr = res.write_port(domain="usb")
        m.submodules.res_rd = res_rd = res.read_port(domain="sync")

        startup = Signal(range(self.CYCLES_1MS + 1)); ready = Signal()
        m.d.usb += startup.eq(Mux(ready, startup, startup + 1))
        with m.If(startup == self.CYCLES_1MS):
            m.d.usb += ready.eq(1)

        ok      = Signal(8)             # successes this tap (saturating)
        trial   = Signal(range(self.TRIALS + 1))
        settle  = Signal(range(self.SETTLE + 1))
        to      = Signal(range(self.TIMEOUT + 1))
        lastd   = Signal(8)
        dirh    = Signal(8)            # saturating census of dir_i == 1
        done    = Signal()

        m.d.comb += [res_wr.addr.eq(tap), res_wr.data.eq(Cat(lastd, ok, dirh))]

        # BUILT-IN CONTROL FOR THE IDELAY ITSELF.  If prjxray mis-models the
        # VARIABLE mode bits the tap may never actually move, and then "all 32
        # taps fail" is indistinguishable from "the eye is closed everywhere".
        # DIR has real activity, so count how many cycles the DELAYED dir_i
        # reads high per tap: if H varies across taps the delay line is live.
        with m.If(dir_i & ~dirh.all()):
            m.d.usb += dirh.eq(dirh + 1)

        def sat_inc():
            return ok.eq(Mux(ok.all(), ok, ok + 1))

        with m.FSM(domain="usb"):
            with m.State("BOOT"):
                m.d.comb += drive.eq(0x00)
                with m.If(ready):
                    # tap 0 is IDELAY_VALUE, already loaded -- do NOT pulse CE
                    m.d.usb += [tap.eq(0), ld.eq(0), settle.eq(self.SETTLE)]
                    m.next = "SETTLE"
            with m.State("SETTLE"):
                m.d.comb += drive.eq(0x00)
                m.d.usb += ld.eq(0)
                m.d.usb += settle.eq(settle - 1)
                with m.If(settle == 0):
                    m.d.usb += [ok.eq(0), trial.eq(0), lastd.eq(0),
                                dirh.eq(0)]
                    m.next = "IDLE"
            with m.State("IDLE"):
                m.d.comb += drive.eq(0x00)
                m.d.usb += to.eq(0)
                with m.If(~dir_i):
                    m.next = "CMD"
            with m.State("CMD"):
                m.d.comb += drive.eq(0xC0)
                m.d.usb += to.eq(to + 1)
                with m.If(nxt_i):
                    m.next = "TURN"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "NEXT"
            with m.State("TURN"):
                m.d.comb += drive.eq(0x00)
                m.d.usb += to.eq(to + 1)
                with m.If(dir_i):
                    m.d.usb += lastd.eq(data_i)
                    with m.If(data_i == 0x24):
                        m.d.usb += sat_inc()
                    m.next = "DATA"
                with m.Elif(to == self.TIMEOUT):
                    m.next = "NEXT"
            with m.State("DATA"):
                m.d.comb += drive.eq(0x00)
                m.d.usb += to.eq(to + 1)
                with m.If(data_i == 0x24):
                    m.d.usb += [lastd.eq(data_i), sat_inc()]
                with m.If(~dir_i | (to == self.TIMEOUT)):
                    m.next = "NEXT"
            with m.State("NEXT"):
                m.d.comb += drive.eq(0x00)
                m.d.comb += res_wr.en.eq(1)       # store running result
                m.d.usb += trial.eq(trial + 1)
                with m.If(trial == self.TRIALS - 1):
                    with m.If(tap == self.TAPS - 1):
                        m.d.usb += done.eq(1)
                        m.next = "HOLD"
                    with m.Else():
                        m.d.usb += [tap.eq(tap + 1), ld.eq(1),
                                    settle.eq(self.SETTLE)]
                        m.next = "SETTLE"
                with m.Else():
                    m.next = "IDLE"
            with m.State("HOLD"):
                m.d.comb += drive.eq(0x00)

        # ---------------- UART dump (sync / 60 MHz domain) -------------------
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

        ph = int(round(self.phase)) & 0xFF
        d1 = Signal(); done_s = Signal()
        m.d.sync += [d1.eq(done), done_s.eq(d1)]

        idx = Signal(range(self.TAPS)); sub = Signal(5); gap = Signal(23)
        m.d.comb += res_rd.addr.eq(idx)
        w = res_rd.data
        chars = [
            Const(ord('P'), 8), Const(ord('='), 8),
            Const(ord("0123456789ABCDEF"[(ph >> 4) & 0xF]), 8),
            Const(ord("0123456789ABCDEF"[ph & 0xF]), 8),
            Const(ord(' '), 8),
            Const(ord('T'), 8), Const(ord('='), 8),
            hexd(idx[4:5]), hexd(idx[0:4]),
            Const(ord(' '), 8),
            Const(ord('O'), 8), Const(ord('K'), 8), Const(ord('='), 8),
            hexd(w[12:16]), hexd(w[8:12]),
            Const(ord(' '), 8),
            Const(ord('D'), 8), Const(ord('='), 8),
            hexd(w[4:8]), hexd(w[0:4]),
            Const(ord(' '), 8),
            Const(ord('H'), 8), Const(ord('='), 8),
            hexd(w[20:24]), hexd(w[16:20]),
            Const(ord('\r'), 8), Const(ord('\n'), 8),
        ]
        NCH = len(chars)
        sel = Array(chars)

        m.d.sync += tx_start.eq(0)
        with m.FSM(domain="sync"):
            with m.State("WAIT"):
                with m.If(done_s):
                    m.d.sync += [idx.eq(0), sub.eq(0)]
                    m.next = "EMIT"
            with m.State("EMIT"):
                with m.If(~tx_busy & ~tx_start):
                    m.d.sync += [tx_byte.eq(sel[sub]), tx_start.eq(1),
                                 sub.eq(sub + 1)]
                    with m.If(sub == NCH - 1):
                        m.d.sync += sub.eq(0)
                        with m.If(idx == self.TAPS - 1):
                            m.d.sync += gap.eq(0)
                            m.next = "GAP"
                        with m.Else():
                            m.d.sync += idx.eq(idx + 1)
            with m.State("GAP"):
                m.d.sync += gap.eq(gap + 1)
                with m.If(gap.all()):
                    m.d.sync += [idx.eq(0), sub.eq(0)]
                    m.next = "EMIT"

        hb = Signal(24); m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
