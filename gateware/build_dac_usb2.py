#!/usr/bin/env python3
#
# Build usb_utmi_top WITH proper PLL clock constraints.
#
# The Amaranth "Xray" toolchain's XDC template only emits IOSTANDARD/LOC — it
# NEVER iterates iter_clock_constraints(), so add_clock_constraint() is silently
# dropped and nextpnr-xilinx gets ZERO create_clock. nextpnr then times every
# domain (and the usb<->audio CDC) blind -> placement-variant setup violations
# that corrupt the USB audio data path (gritty/harsh DAC output), while the slow
# audio-only path survives. This is the documented openXC7 "missing PLL clock
# constraints" failure (see avb-aes3 avb_soc.py:1056, which injects the same
# create_clock lines on the PLL output nets; nextpnr-xilinx honours create_clock
# on get_nets and harmlessly ignores set_clock_groups).
#
# Fix: build the plan (do_build=False), append create_clock for the four PLL
# output nets to the generated XDC, then execute. Net names are the top-level
# Amaranth clock wires (verified in the emitted .v): sys_clk / usb_clk /
# audio_clk / fast_clk.
#
import sys
from usb_utmi_top import USBLoopbackUTMI
from colorlight_i9plus_platform import ColorlightI9PlusPlatform

NAME = "usb_dac_utmi"
BUILD_DIR = "build_dac_utmi_usb2"

CLOCK_XDC = """
# ---- injected PLL clock constraints (Amaranth Xray template omits these) ----
create_clock -name sys_clk   -period 16.667 [get_nets sys_clk]
create_clock -name usb_clk   -period 16.667 [get_nets usb_clk]
create_clock -name fast_clk  -period  5.000 [get_nets fast_clk]
create_clock -name audio_clk -period 81.660 [get_nets audio_clk]
set_clock_groups -asynchronous \\
    -group [get_clocks sys_clk] \\
    -group [get_clocks usb_clk] \\
    -group [get_clocks fast_clk] \\
    -group [get_clocks audio_clk]
"""

plan = ColorlightI9PlusPlatform().build(
    USBLoopbackUTMI(), name=NAME, do_build=False)

xdc = f"{NAME}.xdc"
if xdc not in plan.files:
    print(f"ERROR: {xdc} not in build plan ({list(plan.files)})", file=sys.stderr)
    sys.exit(1)
plan.files[xdc] = plan.files[xdc] + CLOCK_XDC
print("Injected create_clock into", xdc)

plan.execute_local(BUILD_DIR)
print("Build complete in", BUILD_DIR)
