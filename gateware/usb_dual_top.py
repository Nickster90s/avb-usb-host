#!/usr/bin/env python3
#
# BOTH USB3300 PHYs from ONE bitstream.
#
# Milestone 1: dual ENUMERATION. Each PHY gets its own ULPI->UTMI->LUNA
# USBDevice with a control endpoint and the full UAC2 descriptor set, so both
# ports come up as distinct USB audio devices. The audio datapath (iso streams,
# channel converters, DAC, feedback servo) is NOT wired yet -- the ISO
# endpoints are declared so the descriptors stay honest, and tied inert. This
# is the same shape as usb_utmi_top.py's proven USB_MINIMAL path, which is why
# it is the right first step: it answers every hard question about running two
# PHYs at once (two asynchronous clock domains, 20 IDELAYE2s off ONE
# IDELAYCTRL, pin pressure, usb-domain Fmax) for a fraction of the logic.
#
# WHY THE TWO DOMAINS ARE GENUINELY ASYNCHRONOUS: each USB3300 runs its own
# crystal. Measured on this board, PHY-A's CLKOUT is 60,006,601 Hz and PHY-B's
# is 60,002,287 Hz -- 72 ppm apart. cd_usb and cd_usb_b therefore drift
# relative to each other forever, and ANY signal crossing between them needs a
# real CDC. There is no "close enough" here.
#
# Pins: leave USB_BACKUP unset. `ulpi`/`ulpi_clock` are PHY-A (Main) and
# `ulpi_b`/`ulpi_clock_b` are PHY-B (Backup) -- see colorlight_i9plus_platform.
#
# Build:
#   BOARD=breakout USB_PHASE=0 USB_IDELAY_TAP=8 \
#     ../.venv/bin/python3 build_dual.py
#
# UART (R3, 115200 8N1) -- both PHYs on one line, A then B:
#   A SF=xxxx SU=xxxx AK=xxxx RE=xxxx L=x S=80 06 00 01 00 00 40 00 | B SF=...
# Read it exactly like the single-PHY probe: SF climbing means that PHY's RX
# path is decoding real 480 Mbps packets, AK non-zero means the host ACKed what
# we sent, RE must stay 0000, and S= is GET_DESCRIPTOR(device) whose expected
# value 80 06 00 01 00 00 40 00 is a built-in control.
#
import os
from amaranth import (Module, Signal, Elaboratable, Instance, ClockSignal,
                      Array, C, Cat, Mux, DomainRenamer)

from luna.gateware.usb.usb2.device import USBDevice
from luna.gateware.interface.utmi import UTMIInterface
from luna.usb2 import (USBIsochronousOutStreamEndpoint,
                       USBIsochronousInMemoryEndpoint,
                       USBIsochronousInStreamEndpoint)
from usb_protocol.types import USBRequestType, USBStandardRequests

from usb_descriptors import USBDescriptors
from requesthandlers import UAC2RequestHandlers


class UsbLink(Elaboratable):
    """One PHY's ULPI->UTMI->USBDevice stack, in the ambient "usb" domain.

    Domain-agnostic on purpose: instance B is wrapped in
    DomainRenamer({"usb": "usb_b"}) by the parent, which rewrites both the
    m.d.usb statements and the ClockSignal("usb") feeding the ULPI wrapper.

    Does NOT instantiate IDELAYCTRL -- there must be exactly one for the whole
    device (both buses are in bank 34), so the parent owns it.
    """

    MAX_PACKET_SIZE = 224
    CHANNELS        = 8

    def __init__(self, *, ulpi, product_name, product_id, serial,
                 idelay_tap=None):
        self.ulpi         = ulpi
        self.product_name = product_name
        self.product_id   = product_id
        self.serial       = serial
        self.idelay_tap   = idelay_tap

        # Probe counters, read by the parent for the UART report.
        self.p_sf = Signal(16)
        self.p_su = Signal(16)
        self.p_ak = Signal(16)
        self.p_re = Signal(16)
        self.p_ls = Signal(2)
        self.p_sb = Array([Signal(8, name=f"p_sb{i}") for i in range(8)])

    def elaborate(self, platform):
        m    = Module()
        ulpi = self.ulpi
        utmi = UTMIInterface()

        data_s, dir_s, nxt_s = ulpi.data.i, ulpi.dir.i, ulpi.nxt.i

        # ---- optional IDELAYE2 on the inputs (tap set by the parent) -------
        # FIXED mode: LD/CE/INC/CNTVALUEIN/C all tied to constants, so this
        # costs ZERO routed nets. C is UNUSED in FIXED mode (UG471) -- do not
        # feed it a clock, it only puts another clock into the IOI.
        if self.idelay_tap is not None:
            def _idelay(sig_in, nm):
                o = Signal(name="idly_" + nm)
                m.submodules["idelay_" + nm] = Instance("IDELAYE2",
                    p_IDELAY_TYPE="FIXED", p_DELAY_SRC="IDATAIN",
                    p_HIGH_PERFORMANCE_MODE="TRUE", p_SIGNAL_PATTERN="DATA",
                    p_REFCLK_FREQUENCY=200.0, p_CINVCTRL_SEL="FALSE",
                    p_PIPE_SEL="FALSE", p_IDELAY_VALUE=self.idelay_tap,
                    i_C=0, i_LD=0, i_CNTVALUEIN=0, i_CE=0, i_INC=0,
                    i_LDPIPEEN=0, i_REGRST=0,
                    i_IDATAIN=sig_in, o_DATAOUT=o)
                return o
            data_s = Cat(*[_idelay(data_s[i], f"d{i}") for i in range(8)])
            dir_s  = _idelay(dir_s, "dir")
            nxt_s  = _idelay(nxt_s, "nxt")

        # USB3300 RESET is active-HIGH; tie low to keep the transceiver on.
        m.d.comb += ulpi.rst.o.eq(0)

        # Data-bus tristate. OE comes from the POST-DELAY dir, never the raw
        # pin: one pad cannot supply both a delayed and an undelayed copy of
        # itself (PAD -> IBUF -> (IDELAY | direct) is a one-of-two mux), and
        # asking for both is what made every earlier IDELAY build unroutable
        # at a constant overused=3.
        ulpi_data_drive = Signal(8)
        m.d.comb += [ulpi.data.o.eq(ulpi_data_drive),
                     ulpi.data.oe.eq(~dir_s)]

        # Startup reset pulse: ResetSignal("usb") is never asserted, so the
        # wrapper needs an explicit one to run its init sequence.
        wrap_rst   = Signal(reset=1)
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
        m.d.comb += [
            utmi.vbus_valid.eq(1), utmi.session_valid.eq(1),
            utmi.session_end.eq(0), utmi.host_disconnect.eq(0),
            utmi.id_digital.eq(0),
        ]

        m.submodules.usb = usb = USBDevice(bus=utmi)
        usb.always_fs  = False
        usb.data_clock = 60e6

        descriptors = USBDescriptors(product_name=self.product_name,
                                     serial=self.serial,
                                     product_id=self.product_id)
        desc_ch = int(os.environ.get("DESC_CH", self.CHANNELS))
        usb_descriptors = descriptors.create_usb2_descriptors(
                              desc_ch, self.MAX_PACKET_SIZE)

        control_ep = usb.add_control_endpoint()
        control_ep.add_standard_request_handlers(usb_descriptors, blacklist=[
            lambda setup: (setup.type    == USBRequestType.STANDARD)
                        & (setup.request == USBStandardRequests.SET_INTERFACE)
        ])
        control_ep.add_request_handler(UAC2RequestHandlers())

        ep1_out = USBIsochronousOutStreamEndpoint(
            endpoint_number=1, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep1_out)
        ep1_in = USBIsochronousInMemoryEndpoint(
            endpoint_number=1, max_packet_size=4)
        usb.add_endpoint(ep1_in)
        ep2_in = USBIsochronousInStreamEndpoint(
            endpoint_number=2, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep2_in)

        # Endpoints declared (descriptors must match) but inert: milestone 1 is
        # enumeration only.
        m.d.comb += [
            ep1_out.stream.ready.eq(1),
            ep2_in.stream.valid.eq(0),
            ep1_in.bytes_in_frame.eq(4),
            ep1_in.value.eq(0),
            usb.connect.eq(1),
        ]

        # ---- probe counters (see the module docstring) ---------------------
        def bump(sig, cond):
            with m.If(cond & (sig != 0xFFFF)):
                m.d.usb += sig.eq(sig + 1)

        ra_d = Signal()
        m.d.usb += ra_d.eq(utmi.rx_active)
        ra_fall = Signal()
        m.d.comb += ra_fall.eq(~utmi.rx_active & ra_d)

        byte_ok = Signal()
        m.d.comb += byte_ok.eq(utmi.rx_active & utmi.rx_valid)
        bidx  = Signal(4)
        first = Signal()
        m.d.comb += first.eq(byte_ok & (bidx == 0))
        with m.If(byte_ok & (bidx != 15)):
            m.d.usb += bidx.eq(bidx + 1)
        with m.If(ra_fall):
            m.d.usb += bidx.eq(0)

        pid = Signal(8)
        with m.If(first):
            m.d.usb += pid.eq(utmi.rx_data)

        bump(self.p_sf, first & (utmi.rx_data == 0xA5))   # SOF
        bump(self.p_su, first & (utmi.rx_data == 0x2D))   # SETUP
        bump(self.p_ak, first & (utmi.rx_data == 0xD2))   # ACK from host
        bump(self.p_re, utmi.rx_active & utmi.rx_error)
        m.d.comb += self.p_ls.eq(utmi.line_state)

        armed    = Signal()
        captured = Signal()
        with m.If(ra_fall & (pid == 0x2D) & ~captured):
            m.d.usb += armed.eq(1)
        with m.If(armed & ~captured):
            with m.If(byte_ok & (bidx >= 1) & (bidx <= 8)):
                m.d.usb += self.p_sb[bidx - 1].eq(utmi.rx_data)
            with m.If(ra_fall & (bidx >= 9)):
                m.d.usb += [captured.eq(1), armed.eq(0)]

        return m


class UsbDual(Elaboratable):
    """Both PHYs, each in its own clock domain, reporting on one UART."""

    BAUD_DIV = 521              # 60 MHz sync / 115200

    def elaborate(self, platform):
        m = Module()

        with open(os.path.join(os.path.dirname(__file__),
                  "../rtl/ulpi_ultraembedded/ulpi_wrapper.v")) as f:
            platform.add_file("ulpi_wrapper.v", f.read())

        ulpi_a   = platform.request("ulpi", 0)
        ulpi_b   = platform.request("ulpi_b", 0)
        clk_a    = platform.request("ulpi_clock", 0)
        clk_b    = platform.request("ulpi_clock_b", 0)
        uart     = platform.request("uart_tx", 0)
        led      = platform.request("user_led", 0)

        phase = float(os.environ.get("USB_PHASE", "0"))
        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=clk_a.i, usb_pll=True, usb_phase=phase,
            ulpi_clk_pin_b=clk_b.i)

        # ---- exactly ONE IDELAYCTRL for all 20 IDELAYE2s -------------------
        # Both ULPI buses are in bank 34. A second IDELAYCTRL fails with "more
        # than one IDELAYCTRL for the default IODELAY_GROUP"; this is why
        # avb-aes3 shares its CRG's single S7IDELAYCTRL between its two ULPIs.
        # REFCLK goes through an explicit BUFG: the CAR drives cd_fast straight
        # off PLL CLKOUT1 with no buffer.
        # PER-PHY TAPS. The eye centre is a property of the PHY's own wiring
        # and of ITS paths' placement, not of the chip type, so the two buses
        # need independent taps -- which is precisely why avb-aes3 carries
        # separate ulpi_idelay_tap and ulpi2_idelay_tap CSRs rather than one.
        #
        # Measured here: with tap 8 on BOTH, PHY-B enumerated and PHY-A decoded
        # SOFs cleanly (SF saturated, RE=0000) but never asserted its D+
        # pull-up -- i.e. its RX was fine and its ULPI register WRITES were not.
        # Reads and SOF decoding tolerate ~0.6 ns of extra nxt/data delay; the
        # write handshake, which times our driven data against the PHY's NXT,
        # does not. PHY-A's proven single-PHY build uses NO IDELAY, so that is
        # its default here: unset = no IDELAYE2 on that bus at all.
        #   USB_IDELAY_TAP_A / USB_IDELAY_TAP_B  per bus
        #   USB_IDELAY_TAP                       fallback for both
        def _tap(which, default):
            v = os.environ.get("USB_IDELAY_TAP_" + which,
                               os.environ.get("USB_IDELAY_TAP", default))
            return None if v in (None, "", "none") else int(v)
        tap_a = _tap("A", None)
        tap_b = _tap("B", "8")

        if tap_a is not None or tap_b is not None:
            idc_rst = Signal(reset=1)
            idc_cnt = Signal(8, reset=0)
            with m.If(~idc_cnt.all()):
                m.d.sync += idc_cnt.eq(idc_cnt + 1)
            with m.Else():
                m.d.sync += idc_rst.eq(0)
            refclk_g = Signal()
            m.submodules.idelay_bufg = Instance("BUFG",
                i_I=ClockSignal("fast"), o_O=refclk_g)
            m.submodules.idelayctrl = Instance("IDELAYCTRL",
                i_REFCLK=refclk_g, i_RST=idc_rst)

        link_a = UsbLink(ulpi=ulpi_a, product_name="N-Series AVB Switchover Main",
                         product_id=0xEAB1, serial="0", idelay_tap=tap_a)
        link_b = UsbLink(ulpi=ulpi_b, product_name="N-Series AVB Switchover Backup",
                         product_id=0xEAB2, serial="1", idelay_tap=tap_b)
        m.submodules.link_a = link_a
        m.submodules.link_b = DomainRenamer({"usb": "usb_b"})(link_b)

        # ---- snapshot each link's counters into sync -----------------------
        def snapshot(link, dom):
            srcs = [link.p_sf, link.p_su, link.p_ak, link.p_re,
                    link.p_ls] + [link.p_sb[i] for i in range(8)]
            hold = [Signal(len(s), name=f"h_{dom}_{i}")
                    for i, s in enumerate(srcs)]
            snap = Signal(22)
            m.d[dom] += snap.eq(snap + 1)
            with m.If(snap == 0):
                m.d[dom] += [h.eq(s) for h, s in zip(hold, srcs)]
            out = []
            for h in hold:
                a = Signal(len(h)); b = Signal(len(h))
                m.d.sync += [a.eq(h), b.eq(a)]
                out.append(b)
            return out

        shown_a = snapshot(link_a, "usb")
        shown_b = snapshot(link_b, "usb_b")

        # ---- 115200 8N1 UART in the crystal-clean sync domain --------------
        baud_cnt = Signal(range(self.BAUD_DIV)); baud_tick = Signal()
        m.d.sync += baud_cnt.eq(baud_cnt - 1)
        with m.If(baud_cnt == 0):
            m.d.sync += baud_cnt.eq(self.BAUD_DIV - 1)
        m.d.comb += baud_tick.eq(baud_cnt == 0)

        tx_shift = Signal(10, reset=0x3FF); tx_bits = Signal(range(11))
        tx_busy = Signal(); tx_byte = Signal(8); tx_start = Signal()
        m.d.comb += uart.o.eq(tx_shift[0])
        with m.If(tx_start & ~tx_busy):
            m.d.sync += [tx_shift.eq(Cat(C(0, 1), tx_byte, C(1, 1))),
                         tx_bits.eq(10), tx_busy.eq(1)]
        with m.Elif(tx_busy & baud_tick):
            m.d.sync += [tx_shift.eq(Cat(tx_shift[1:], C(1, 1))),
                         tx_bits.eq(tx_bits - 1)]
            with m.If(tx_bits == 1):
                m.d.sync += tx_busy.eq(0)

        def hexd(nib):
            return Mux(nib < 10, nib + ord('0'), nib + (ord('A') - 10))

        def fields(label, shown):
            p = [C(ord(label), 8), C(ord(' '), 8)]
            for nm, v in zip(("SF", "SU", "AK", "RE"), shown[:4]):
                p += [C(ord(nm[0]), 8), C(ord(nm[1]), 8), C(ord('='), 8),
                      hexd(v[12:16]), hexd(v[8:12]),
                      hexd(v[4:8]), hexd(v[0:4]), C(ord(' '), 8)]
            p += [C(ord('L'), 8), C(ord('='), 8), hexd(shown[4]),
                  C(ord(' '), 8), C(ord('S'), 8), C(ord('='), 8)]
            for v in shown[5:13]:
                p += [hexd(v[4:8]), hexd(v[0:4]), C(ord(' '), 8)]
            return p

        parts = (fields('A', shown_a)
                 + [C(ord('|'), 8), C(ord(' '), 8)]
                 + fields('B', shown_b)
                 + [C(ord('\r'), 8), C(ord('\n'), 8)])

        msg = Array(parts)
        N = len(parts)
        sending = Signal(); throttle = Signal(26); pos = Signal(range(N + 1))
        m.d.sync += throttle.eq(throttle + 1)
        with m.If((throttle == 0) & ~sending):
            m.d.sync += [sending.eq(1), pos.eq(0)]
        m.d.sync += tx_start.eq(0)
        with m.If(sending & ~tx_busy & ~tx_start):
            with m.If(pos == N):
                m.d.sync += sending.eq(0)
            with m.Else():
                m.d.sync += [tx_byte.eq(msg[pos]), tx_start.eq(1),
                             pos.eq(pos + 1)]

        hb = Signal(24)
        m.d.sync += hb.eq(hb + 1)
        m.d.comb += led.o.eq(hb[23])
        return m
