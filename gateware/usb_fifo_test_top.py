#!/usr/bin/env python3
#
# Isolation test (2026-05-28): build the FIFO-inside USBAVBSubsystem
# wrapper STANDALONE on the near-empty avb-usb-host chip (no eth, no
# LiteX SoC, no CRF/AVB) to confirm the wrapper + internal AsyncFIFO
# enumerates as USB HS UAC2.
#
# Purpose: separate "is the new FIFO-in-wrapper good?" from "is the
# avb-aes3 integration congestion/floorplan the problem?". If USB
# enumerates here (minimal congestion → cd_usb closes 60 MHz easily),
# the wrapper is correct and the avb-aes3 failure is purely placement.
#
# Build: USB_PHASE=0 python3 usb_fifo_test_top.py
#
# The subsystem's sample_* drain ports are driven by a slow counter so
# the AsyncFIFO is NOT optimised away (we want its real timing/area
# footprint in the build). sample_readable is ORed onto the LED.

from amaranth import Module, Signal, Elaboratable, Mux

from usb_avb_subsystem import USBAVBSubsystem
from colorlight_i9plus_platform import ColorlightI9PlusPlatform


class USBFifoTestTop(Elaboratable):
    def elaborate(self, platform):
        m = Module()

        # The subsystem requests ULPI pins + CAR itself (standalone=False).
        m.submodules.sub = sub = USBAVBSubsystem(standalone=False)

        # Keep the FIFO live: drain one sample every ~1024 sys cycles so
        # the read side isn't pruned. (Real consumer pops far faster; this
        # is just to retain the logic for the timing test.)
        drain_cnt = Signal(10)
        m.d.sync += drain_cnt.eq(drain_cnt + 1)
        m.d.comb += sub.sample_pop.eq(drain_cnt.all() & sub.sample_readable)

        # Nominal feedback so the device behaves.
        m.d.comb += sub.feedback_value.eq(0x0001_8000)

        # LED: blink rate shows FIFO activity (sample_readable seen).
        led      = platform.request("user_led", 0)
        counter  = Signal(26)
        seen     = Signal()
        m.d.sync += [
            counter.eq(counter + 1),
            seen.eq(seen | sub.sample_readable),
        ]
        m.d.comb += led.o.eq(Mux(seen, counter[21], counter[25]))

        return m


if __name__ == "__main__":
    # Build directly via the Amaranth platform — bypasses LUNA's
    # top_level_cli (which imports apollo_fpga, not installed here).
    plat = ColorlightI9PlusPlatform()
    plat.build(USBFifoTestTop(), name="usb_fifo_test",
               do_program=False, build_dir="build_fifotest")
    print("built build_fifotest/usb_fifo_test.bit")
