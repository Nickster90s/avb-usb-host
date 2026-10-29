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
import os as _os
from amaranth import (Array, Cat, Const, Instance, Module, Signal, Elaboratable, Mux, ClockSignal,
                      ResetSignal)
from luna import top_level_cli


class UlpiPhaseScanDirFilt(Elaboratable):
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
            ulpi.data.oe.eq(~ulpi.dir.i),     # RAW dir: never fight the PHY
        ]

        # ---- optional IDELAYE2 on the INPUTS (USB_IDELAY_TAP=0..31) ----------
        # Clock PHASE moves our launch edge AND our capture edge together, so a
        # phase sweep cannot separate TX from RX timing -- which is why 18 phase
        # points on PHY-B failed IDENTICALLY. IDELAY delays ONLY the inputs, so
        # phase x tap spans the 2-D space: phase sets TX launch, tap trims RX
        # capture. FIXED mode (not VAR_LOAD) so the tap is live with no LD pulse.
        _tap = _os.environ.get("USB_IDELAY_TAP")
        if _tap is not None:
            idc_rst = Signal(reset=1); idc_cnt = Signal(8)
            with m.If(~idc_cnt.all()):
                m.d.sync += idc_cnt.eq(idc_cnt + 1)
            with m.Else():
                m.d.sync += idc_rst.eq(0)
            m.submodules.idelayctrl = Instance("IDELAYCTRL",
                i_REFCLK=ClockSignal("fast"), i_RST=idc_rst)

            def _idly(sig, nm):
                o = Signal(name="idly_" + nm)
                m.submodules["idelay_" + nm] = Instance("IDELAYE2",
                    p_IDELAY_TYPE="FIXED", p_DELAY_SRC="IDATAIN",
                    p_HIGH_PERFORMANCE_MODE="TRUE", p_SIGNAL_PATTERN="DATA",
                    p_REFCLK_FREQUENCY=200.0, p_CINVCTRL_SEL="FALSE",
                    p_PIPE_SEL="FALSE", p_IDELAY_VALUE=int(_tap),
                    i_C=ClockSignal("sync"), i_LD=0, i_CNTVALUEIN=0,
                    i_CE=0, i_INC=0, i_LDPIPEEN=0, i_REGRST=0,
                    i_IDATAIN=sig, o_DATAOUT=o)
                return o
            dir_i  = _idly(ulpi.dir.i, "dir")
            nxt_i  = _idly(ulpi.nxt.i, "nxt")
            data_i = Cat(*[_idly(ulpi.data.i[i], f"d{i}") for i in range(8)])
        else:
            dir_i  = ulpi.dir.i
            nxt_i  = ulpi.nxt.i
            data_i = ulpi.data.i

        # ---- DIR GLITCH FILTER -------------------------------------------
        # 2026-10-28: PHY B drives a continuous train of EXACTLY ONE CYCLE
        # DIR pulses at ULPI_CLK/40 = 1.5 MHz (rstgate proved they stop dead
        # when the PHY is held in reset, so they really come from the PHY).
        # The FSM checks `if dir_i` first in CMD and aborts, so a pulse lands
        # inside nearly every command and NO register access can ever finish.
        # That -- not the sampling phase -- is why 18 clock configurations and
        # 32 IDELAY taps all failed identically.
        #
        # A real ULPI turnaround holds DIR high for >= 2 cycles, so require two
        # consecutive highs. The 1-cycle delay is FREE for a register read: the
        # cycle DIR first goes high is the turnaround cycle (bus not yet valid)
        # and the register byte lands on the NEXT cycle -- exactly where the
        # filtered DIR now points.
        dir_raw = Signal()
        m.d.comb += dir_raw.eq(dir_i)
        dir_d   = Signal()
        m.d.usb += dir_d.eq(dir_raw)
        dir_f   = Signal()
        m.d.comb += dir_f.eq(dir_raw & dir_d)
        # count the glitches we suppressed, so the UART proves the filter fired
        n_glitch = Signal(16)
        with m.If(dir_raw & ~dir_d):
            m.d.usb += n_glitch.eq(n_glitch + 1)
        dir_i = dir_f

        startup   = Signal(range(self.CYCLES_1MS + 1))
        ready     = Signal()
        match_seen = Signal()
        err_seen  = Signal()
        captured  = Signal(8)

        m.d.usb += startup.eq(Mux(ready, startup, startup + 1))
        with m.If(startup == self.CYCLES_1MS):
            m.d.usb += ready.eq(1)

        # Transition counters -- these say WHERE the read sequence dies:
        #   ACC=0000            the PHY never asserts NXT for our TXCMD
        #                       => our command byte is not reaching it = TX side
        #   ACC>0 DAT=0000      accepted, but the PHY never turned the bus around
        #   ACC>0 DAT>0         we reach DATA; then CAP/MATCH/ERR are meaningful
        #   ABRT                times DIR pre-empted us while driving the command
        n_acc   = Signal(16)
        n_data  = Signal(16)
        n_abort = Signal(16)
        # WATCHDOG. Without this the FSM deadlocks on the first anomaly: one
        # NXT (real or glitched) moves it to TURN, the PHY never asserts DIR,
        # and it waits there forever -- every counter freezes at 1 and the
        # instrument stops sampling. With a timeout it retries, so the counters
        # become RATES and the thing is actually sweepable across phases.
        TO_BITS = 12                     # ~68 us at 60 MHz
        to_cnt  = Signal(TO_BITS)
        n_to    = Signal(16)
        timeout = Signal()
        m.d.comb += timeout.eq(to_cnt.all())

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                m.d.comb += [drive.eq(0x00), ulpi.stp.o.eq(0)]
                m.d.usb += to_cnt.eq(0)
                with m.If(ready & ~dir_i):
                    m.next = "CMD"

            with m.State("CMD"):
                m.d.usb += to_cnt.eq(to_cnt + 1)
                # Drive the read command until the PHY accepts it (NXT).
                m.d.comb += [drive.eq(self.CMD_REGR), ulpi.stp.o.eq(0)]
                # DO NOT ABORT ON DIR. PHY B emits a 1.5 MHz train of
                # 1-cycle DIR pulses; jitter makes some of them read as 2
                # cycles, so even the 2-cycle filter trips and every command
                # died here (ACC == ABRT exactly). `data.oe` is gated on RAW
                # dir, so a glitch already stops us driving for that cycle --
                # we just hold the command and keep waiting for NXT instead of
                # throwing the transaction away. Still COUNT them as ABRT so
                # the rate stays visible.
                with m.If(dir_i):
                    m.d.usb += n_abort.eq(n_abort + 1)
                with m.If(nxt_i):            # command accepted
                    m.d.usb += n_acc.eq(n_acc + 1)
                    m.next = "TURN"
                with m.Elif(timeout):
                    m.d.usb += n_to.eq(n_to + 1)
                    m.next = "IDLE"

            with m.State("TURN"):
                m.d.usb += to_cnt.eq(to_cnt + 1)
                # Stop driving; wait for PHY to assert DIR (it owns bus now).
                m.d.comb += [drive.eq(0x00), ulpi.stp.o.eq(0)]
                with m.If(dir_i):
                    m.d.usb += n_data.eq(n_data + 1)
                    m.next = "DATA"
                with m.Elif(timeout):
                    m.d.usb += n_to.eq(n_to + 1)
                    m.next = "IDLE"

            with m.State("DATA"):
                m.d.usb += to_cnt.eq(to_cnt + 1)
                # DIR high, NXT low → register data is on the bus.
                m.d.comb += [drive.eq(0x00), ulpi.stp.o.eq(0)]
                m.d.usb += captured.eq(data_i)
                # Compare the LIVE bus value. The original compared `captured`
                # on the cycle DIR drops, which is one cycle stale, so MATCH
                # could never assert -- on any PHY, including a working one.
                with m.If(data_i == self.EXPECT):
                    m.d.usb += match_seen.eq(1)
                with m.Elif(data_i != 0x00):
                    m.d.usb += err_seen.eq(1)
                with m.If(~dir_i):
                    m.next = "IDLE"
                with m.Elif(timeout):
                    m.d.usb += n_to.eq(n_to + 1)
                    m.next = "IDLE"

        # ROBUST eye finder: fast blink ONLY if many reads succeeded AND
        # not a single one ever returned wrong data. A marginal phase
        # produces occasional bad reads → err_seen latches → slow blink.
        # Sweep USB_PHASE and pick the CENTRE of the fast-blink window.
        #   fast → zero errors (solid eye at this phase)
        #   slow → at least one bad read (marginal/closed eye)
        # ---- UART report (115200 8N1): the byte we ACTUALLY captured ----------
        # The LED can only say pass/fail, which cannot distinguish:
        #   CAP=00 / CAP=FF -> we are sampling the bus TURNAROUND (no one driving)
        #   CAP=<rotated 24> -> data lines sample fine but are mis-ORDERED
        #   CAP=24          -> RX eye is OPEN at this phase
        # NXT also matters: if the PHY never asserts NXT our TXCMD never landed,
        # so the read never even started and CAP is meaningless.
        uart = platform.request("uart_tx", 0)
        BAUD_DIV = 521                      # 60 MHz sync / 115200
        baud_cnt = Signal(range(BAUD_DIV)); baud_tick = Signal()
        m.d.sync += baud_cnt.eq(baud_cnt - 1)
        with m.If(baud_cnt == 0):
            m.d.sync += baud_cnt.eq(BAUD_DIV - 1)
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

        # count completed reads + NXT acceptances so a dead link is obvious
        nxt_seen = Signal()
        with m.If(ulpi.nxt.i):
            m.d.usb += nxt_seen.eq(1)

        # Multi-bit CDC done properly. A plain 2-FF sync on a 16-bit counter
        # samples it mid-increment and returns TORN values -- that is why these
        # counters appeared to DECREASE and why their readings were worthless.
        # Fix: snapshot into hold registers in the source (usb) domain on a slow
        # strobe; the held value is then stable for millions of cycles, so a
        # 2-FF sync of it is safe.
        snap      = Signal(24)
        hold_acc  = Signal(16); hold_dat = Signal(16)
        hold_abrt = Signal(16); hold_to  = Signal(16); hold_cap = Signal(8)
        hold_gl   = Signal(16)
        m.d.usb += snap.eq(snap + 1)
        with m.If(snap == 0):
            m.d.usb += [hold_acc.eq(n_acc),   hold_dat.eq(n_data),
                        hold_abrt.eq(n_abort), hold_to.eq(n_to),
                        hold_cap.eq(captured), hold_gl.eq(n_glitch)]

        def s2(src_sig, n):
            a = Signal(n); b = Signal(n)
            m.d.sync += [a.eq(src_sig), b.eq(a)]
            return b
        cap_s   = s2(hold_cap, 8)
        mat_s   = s2(match_seen, 1)
        err_s   = s2(err_seen, 1)
        nxt_s   = s2(nxt_seen, 1)

        def hexd(nib):
            return Mux(nib < 10, nib + ord('0'), nib + (ord('A') - 10))
        def lit(s):
            return [ord(c) for c in s]
        acc_s  = s2(hold_acc, 16)
        dat_s  = s2(hold_dat, 16)
        abrt_s = s2(hold_abrt, 16)
        def hexw(v):
            return [hexd(v[12:16]), hexd(v[8:12]), hexd(v[4:8]), hexd(v[0:4])]
        to_s = s2(hold_to, 16)
        gl   = s2(hold_gl, 16)
        parts = (lit("ACC=") + hexw(acc_s)
               + lit(" DAT=") + hexw(dat_s)
               + lit(" ABRT=") + hexw(abrt_s)
               + lit(" GL=") + hexw(gl)
               + lit(" TO=") + hexw(to_s)
               + lit(" CAP=") + [hexd(cap_s[4:8]), hexd(cap_s[0:4])]
               + lit(" M=") + [Mux(mat_s, ord('1'), ord('0'))]
               + lit(" E=") + [Mux(err_s, ord('1'), ord('0'))]
               + lit("\r\n"))
        N = len(parts)
        msg = Array([Signal(8, name=f"pm{i}") for i in range(N)])
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

        led     = platform.request("user_led", 0)
        counter = Signal(26)
        m.d.usb += counter.eq(counter + 1)
        good = match_seen & ~err_seen
        m.d.comb += led.o.eq(Mux(good, counter[21], counter[25]))

        return m


if __name__ == "__main__":
    top_level_cli(UlpiPhaseScan)
