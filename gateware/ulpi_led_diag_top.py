#!/usr/bin/env python3
#
# ULPI RX-path health on the ONBOARD LED — no host, no UART, no replug.
# Continuously reads USB3300 ULPI register 0x00 (Vendor ID Low, must = 0x24)
# via LUNA's robust ULPIRegisterWindow and shows the result on user_led (A18):
#
#   LED FAST blink  -> reg 0x00 reads 0x24 reliably  = RX DATA PATH GOOD
#                      (so an error-71 is then host-side: USB cable/VBUS, not FPGA<->PHY)
#   LED SLOW blink  -> reads complete but WRONG value = RX data corrupt (data-bus wiring/SI)
#   LED ~OFF/steady -> reads never complete          = DIR/NXT handshake dead / PHY held reset
#
# Host-independent: watch the LED while poking wires or sweeping USB_PHASE.
# Build:  USB_BACKUP=1 USB_PHASE=0 SAMPLE_EDGE=off  (via build_dac-style wrapper)
#
import os
from amaranth import Module, Signal, Elaboratable, Mux, ClockDomain
from luna import top_level_cli
from luna.gateware.interface.ulpi import ULPIRegisterWindow


class UlpiLedDiag(Elaboratable):
    WAITMS = 60000            # ~1 ms PHY settle at 60 MHz

    def elaborate(self, platform):
        m = Module()
        ulpi = platform.request("ulpi", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        # Mirror usb_utmi_top's clock selection so the LED reflects the SAME
        # clock path the enumerating build uses. USB_PHASE: "raw" = ULPI clock
        # straight through a BUFG (NO PLL, no lock needed — proves whether a
        # clean 60 MHz is even present at V4); "inv" = inverted BUFG; numeric =
        # phase-shift PLL.
        ph = os.environ.get("USB_PHASE", "0")
        if ph == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        elif ph == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True, usb_phase=float(ph))
        m.d.comb += ulpi.rst.o.eq(0)

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

        # DECAYING error indicator (tuning meter): err_recent goes high on any
        # wrong read and auto-clears after ~0.1 s with no errors. So a config
        # with a CLEAN eye settles to err_recent=0 (LED slow); a marginal one
        # keeps re-triggering (LED fast). Sweep USB_PHASE and watch for the
        # config that stays STEADY SLOW = clean RX = will enumerate.
        toggle  = Signal()               # alternate reg 0x00 / 0x01
        addr    = Signal(6)
        expect  = Signal(8)
        err_pulse = Signal()
        m.d.comb += addr.eq(Mux(toggle, 0x01, 0x00))
        m.d.comb += expect.eq(Mux(toggle, 0x04, 0x24))
        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                with m.If(ready & ~rw.busy):
                    m.next = "READ"
            with m.State("READ"):
                m.d.comb += [rw.address.eq(addr), rw.read_request.eq(1)]
                m.next = "WAIT"
            with m.State("WAIT"):
                with m.If(rw.done):
                    with m.If(rw.read_data != expect):
                        m.d.comb += err_pulse.eq(1)
                    m.d.usb += toggle.eq(~toggle)
                    m.next = "IDLE"

        err_recent = Signal()
        err_timer  = Signal(23)          # ~0.14 s @60 MHz
        with m.If(err_pulse):
            m.d.usb += [err_recent.eq(1), err_timer.eq(0)]
        with m.Elif(err_timer != (2**23 - 1)):
            m.d.usb += err_timer.eq(err_timer + 1)
        with m.Else():
            m.d.usb += err_recent.eq(0)
        err_latched = err_recent          # keep downstream name

        # ---- (A) USB-clock alive: does cd_usb (PLL OUTPUT) run? ----
        hb = Signal()
        m.d.usb += hb.eq(~hb)                     # toggles iff PLL locked + usb runs
        hb_s = Signal(); hb_s1 = Signal(); hb_prev = Signal()
        m.d.sync += [hb_s1.eq(hb), hb_s.eq(hb_s1), hb_prev.eq(hb_s)]
        wd = Signal(20)
        with m.If(hb_s != hb_prev):
            m.d.sync += wd.eq(0)
        with m.Elif(wd != (2**20 - 1)):
            m.d.sync += wd.eq(wd + 1)
        clk_alive = Signal()
        m.d.comb += clk_alive.eq(wd != (2**20 - 1))

        err_s = Signal(); err_s1 = Signal()
        m.d.sync += [err_s1.eq(err_latched), err_s.eq(err_s1)]

        led = platform.request("user_led", 0)
        sctr = Signal(27)
        m.d.sync += sctr.eq(sctr + 1)
        # By BLINK RATE (polarity-independent):
        #   SOLID / no blink = USB CLOCK DEAD (PLL not locked)
        #   SLOW ~0.9 Hz     = clock OK + RX reads CLEAN
        #   FAST ~7 Hz       = clock OK + RX reads have ERRORS (marginal)
        with m.If(~clk_alive):
            m.d.comb += led.o.eq(0)
        with m.Else():
            m.d.comb += led.o.eq(Mux(err_s, sctr[22], sctr[25]))
        return m


if __name__ == "__main__":
    top_level_cli(UlpiLedDiag)
