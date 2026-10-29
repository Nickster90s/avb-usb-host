#!/usr/bin/env python3
#
# ULPI probe using the REAL core — the ultraembedded ulpi_wrapper.v — instead
# of LUNA's ULPIRegisterWindow. The whole point: every earlier reg-read/bit
# diag used LUNA's ULPI translator, which the real USB top abandoned because it
# "never completes a register write on the yosys+nextpnr-xilinx toolchain"
# (see usb_utmi_top.py header). So those diags tested a DIFFERENT, known-broken
# core — their C0/C1/C2/C3 command echo and "D0..D7 all OK" are unreliable.
#
# This probe wires up the ultraembedded wrapper EXACTLY like usb_utmi_top.py
# (same CAR, USB_PHASE=inv default, same startup reset pulse, raw sampling),
# then drives the UTMI mode pins to a FS-device config. That config differs
# from the wrapper's reset defaults, so the wrapper auto-issues its FUNC_CTRL
# register WRITE over ULPI (TX + bus-turnaround) — the exact thing the real USB
# does at init. We then stream the wrapper's OWN outputs over UART (R3 115200):
#
#   LS=n TX=xx RA=n RV=n RE=n RD=xx
#
#   LS = live utmi_linestate (D+/D- as decoded from the PHY's RX_CMD status
#        bytes). This is the RX (PHY->FPGA) health:
#          0 = SE0 (both low: unconfigured / reset / bus reset)
#          1 = J   (FS idle: our FS term pull-up reached the PHY, no host)
#          2 = K
#          3 = SE1 (illegal)
#        Host-free tell: unconfigured stays LS=0. If the FUNC_CTRL write
#        actually reaches the PHY (TX+turnaround good) AND the status path
#        works (RX good), FS term enables the D+ pull-up -> LS reads 1.
#        Plug a host into the backup port and LS moves through chirp.
#   TX = last non-zero byte the wrapper DROVE onto the ULPI bus. 0x84 =
#        REG_FUNC_CTRL command -> confirms the wrapper's TX path is emitting.
#   RA/RV/RE = rxactive / rxvalid / rxerror ever seen (sticky).
#   RD = last received UTMI data byte (on rxvalid).
#
# Interpreting on the backup pins:
#   TX=84 + LS=1        -> full round trip works on backup (TX write reached
#                          PHY, RX status came back). Backup ULPI link is good;
#                          any HS failure is pure eye-margin/skew, not the core.
#   TX=84 + LS=0 stuck  -> TX emits but RX status never returns -> backup RX
#                          (dir/nxt/data sampling) is the fault.
#   TX=00               -> wrapper never drove a command -> clock/reset problem.
#
import os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockSignal,
                      Cat, Const, Array, Mux)
from luna import top_level_cli
from luna.gateware.interface.utmi import UTMIInterface


class UlpiUtmiProbe(Elaboratable):
    BAUD_DIV = 521            # 60 MHz sync / 115200

    def elaborate(self, platform):
        m = Module()

        # ---- Bring in the SAME Verilog core the real USB uses ----
        with open(os.path.join(os.path.dirname(__file__),
                  "../rtl/ulpi_ultraembedded/ulpi_wrapper.v")) as f:
            platform.add_file("ulpi_wrapper.v", f.read())

        ulpi_clk = platform.request("ulpi_clock", 0)
        ulpi     = platform.request("ulpi", 0)

        # Clock/reset exactly like usb_utmi_top.py (USB_PHASE=inv default).
        usb_phase_env = os.environ.get("USB_PHASE", "inv")
        if usb_phase_env == "raw":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False)
        elif usb_phase_env == "inv":
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=False, usb_invert=True)
        else:
            m.submodules.car = platform.clock_domain_generator(
                ulpi_clk_pin=ulpi_clk.i, usb_pll=True,
                usb_phase=float(usb_phase_env))

        m.d.comb += ulpi.rst.o.eq(0)          # USB3300 RESET active-high; keep low

        # UTMI bus record (same as the real top).
        utmi = UTMIInterface()

        # Data-bus tristate: drive when PHY's DIR is low (we own the bus).
        ulpi_data_drive = Signal(8)
        m.d.comb += [
            ulpi.data.o .eq(ulpi_data_drive),
            ulpi.data.oe.eq(~ulpi.dir.i),
        ]

        # Raw sampling — identical to usb_utmi_top's SAMPLE_EDGE=off path.
        data_s, dir_s, nxt_s = ulpi.data.i, ulpi.dir.i, ulpi.nxt.i

        # Startup reset pulse for the wrapper (same as real top).
        wrap_rst    = Signal(reset=1)
        wrap_rstcnt = Signal(7, reset=0)
        with m.If(~wrap_rstcnt.all()):
            m.d.usb += wrap_rstcnt.eq(wrap_rstcnt + 1)
        with m.Else():
            m.d.usb += wrap_rst.eq(0)

        m.submodules.ulpi_wrap = Instance("ulpi_wrapper",
            i_ulpi_clk60_i      = ClockSignal("usb"),
            i_ulpi_rst_i        = wrap_rst,
            i_ulpi_data_out_i   = data_s,
            i_ulpi_dir_i        = dir_s,
            i_ulpi_nxt_i        = nxt_s,
            o_ulpi_data_in_o    = ulpi_data_drive,
            o_ulpi_stp_o        = ulpi.stp.o,

            i_utmi_data_out_i   = utmi.tx_data,
            i_utmi_txvalid_i    = utmi.tx_valid,
            i_utmi_op_mode_i    = utmi.op_mode,
            i_utmi_xcvrselect_i = utmi.xcvr_select,
            i_utmi_termselect_i = utmi.term_select,
            i_utmi_dppulldown_i = utmi.dp_pulldown,
            i_utmi_dmpulldown_i = utmi.dm_pulldown,
            o_utmi_data_in_o    = utmi.rx_data,
            o_utmi_txready_o    = utmi.tx_ready,
            o_utmi_rxvalid_o    = utmi.rx_valid,
            o_utmi_rxactive_o   = utmi.rx_active,
            o_utmi_rxerror_o    = utmi.rx_error,
            o_utmi_linestate_o  = utmi.line_state,
        )

        # Drive the UTMI mode pins to a FS-device config. These differ from the
        # wrapper's reset defaults (xcvrselect=0, termselect=0, opmode=11), so
        # the wrapper's mode_update fires and it issues the FUNC_CTRL register
        # write over ULPI — the real TX + bus-turnaround path, on the backup
        # pins, through the real core. termselect=1 enables the FS pull-up so a
        # successful write shows up host-free as LS=1 (J).
        m.d.comb += [
            utmi.op_mode    .eq(0),   # normal operation
            utmi.xcvr_select.eq(1),   # FS transceiver
            utmi.term_select.eq(1),   # enable FS termination (device pull-up)
            utmi.dp_pulldown.eq(0),
            utmi.dm_pulldown.eq(0),
            utmi.tx_valid   .eq(0),
            utmi.tx_data    .eq(0),
            utmi.vbus_valid    .eq(1),
            utmi.session_valid .eq(1),
            utmi.session_end   .eq(0),
            utmi.host_disconnect.eq(0),
            utmi.id_digital    .eq(0),
        ]

        # ---- Observe the wrapper's outputs (usb domain) ----
        ls_live = Signal(2)
        tx_last = Signal(8)
        ra_seen = Signal(); rv_seen = Signal(); re_seen = Signal()
        rd_last = Signal(8)
        m.d.usb += ls_live.eq(utmi.line_state)
        with m.If(ulpi_data_drive != 0):
            m.d.usb += tx_last.eq(ulpi_data_drive)
        with m.If(utmi.rx_active):
            m.d.usb += ra_seen.eq(1)
        with m.If(utmi.rx_error):
            m.d.usb += re_seen.eq(1)
        with m.If(utmi.rx_valid):
            m.d.usb += [rv_seen.eq(1), rd_last.eq(utmi.rx_data)]

        # ---- CDC usb -> sync (2-FF) ----
        def sync2(src, n):
            a = Signal(n); b = Signal(n)
            m.d.sync += [a.eq(src), b.eq(a)]
            return b
        ls_s = sync2(ls_live, 2)
        tx_s = sync2(tx_last, 8)
        ra_s = sync2(ra_seen, 1); rv_s = sync2(rv_seen, 1); re_s = sync2(re_seen, 1)
        rd_s = sync2(rd_last, 8)

        # ---- UART TX (8N1) in the crystal-clean sync domain ----
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
        def hexbyte(label, val):
            return [ord(c) for c in label] + [hexd(val[4:8]), hexd(val[0:4])]
        def bit1(label, val):
            return [ord(c) for c in label] + [Mux(val, ord('1'), ord('0'))]
        # LS is 2 bits -> print as a single hex nibble 0..3.
        parts = ([ord('L'), ord('S'), ord('='), hexd(ls_s)]
                 + hexbyte(" TX=", tx_s)
                 + bit1(" RA=", ra_s) + bit1(" RV=", rv_s) + bit1(" RE=", re_s)
                 + hexbyte(" RD=", rd_s)
                 + [ord('\r'), ord('\n')])
        N = len(parts)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
        latched = Signal(); throttle = Signal(25)
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
    top_level_cli(UlpiUtmiProbe)
