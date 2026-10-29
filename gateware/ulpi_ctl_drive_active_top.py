#!/usr/bin/env python3
#
# DIR / NXT / STP CONTINUITY TEST -- the drive-contention method, applied to the
# control lines.
#
# DIR and NXT are PHY OUTPUTS in normal operation, so they cannot be checked
# with a voltmeter any other way: an open line and a connected line both read
# the same when the FPGA merely listens. This build requests the drivable
# "ulpi_ctl_drive" resource instead of "ulpi" and drives them HIGH, with the
# PHY held in RESET so its pins are in a defined state.
#
# Measure DC volts AT THE PHY's pins. Calibrated against the data lines, which
# are known-connected on both boards:
#
#   ~2.6 V  -> CONNECTED. The FPGA's drive is contending with the PHY pin.
#   ~3.3 V  -> OPEN. Nothing on the other end loading the FPGA output.
#
# RST is driven HIGH here too (it is active-HIGH), which both holds the PHY in
# reset and lets you check RST's own continuity by the same rule.
#
# NOTE this drives pins the PHY may also drive. Both ends are 3.3 V CMOS and
# current-limited, and this is the identical condition the data-line test
# already ran safely -- but do not leave it loaded longer than the measurement.
#
from amaranth import Module, Signal, Elaboratable, Cat, Const, Array


class UlpiCtlDriveActive(Elaboratable):
    BAUD_DIV = 521                      # 60 MHz sync / 115200

    def elaborate(self, platform):
        m = Module()
        ctl      = platform.request("ulpi_ctl_drive", 0)
        ulpi_clk = platform.request("ulpi_clock", 0)
        uart     = platform.request("uart_tx", 0)

        # sync comes off the 25 MHz crystal, so this runs with or without a
        # PHY clock. usb rides the ULPI clock but nothing here depends on it.
        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi_clk.i, usb_pll=False)

        # PHY ACTIVE (rst LOW) -- this is the whole point of this variant.
        # The earlier ctl_drive held the PHY in RESET, where the USB3300 does
        # NOT drive DIR, so DIR read 3.3 V whether connected or open: no
        # contention, no information. Out of reset the PHY actively drives DIR
        # and NXT low when idle, so both become CONTESTED outputs and the
        # drive-contention rule applies:
        #     ~2.6 V at the PHY pin -> CONNECTED (FPGA fighting the PHY)
        #     ~3.3 V at the PHY pin -> OPEN (nothing loading the FPGA)
        # STP and RST stay PHY INPUTS and remain untestable this way; STP is
        # driven LOW here so it cannot abort anything while you measure.
        m.d.comb += [
            ctl.rst.o.eq(0),            # PHY OUT of reset -> it drives DIR/NXT
            ctl.dir.o.eq(1),            # contest DIR -> measure at PHY
            ctl.nxt.o.eq(1),            # contest NXT -> measure at PHY (recheck)
            ctl.stp.o.eq(0),            # keep STP deasserted
        ]

        # ---- UART heartbeat so you can confirm it is running ----
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

        parts = [ord(c) for c in "PHY ACTIVE (rst=0); DIR+NXT driven HIGH - measure at PHY\r\n"]
        N = len(parts)
        msg = Array([Signal(8, name=f"m{i}") for i in range(N)])
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
        return m
