#!/usr/bin/env python3
#
# Build usb_only_top (UAC2 loopback, no ADAT/AVB) WITH proper PLL clock
# constraints, for either PHY on either board revision.
#
#   BOARD=breakout            -> custom SODIMM baseboard pin map
#   BOARD=ext (default)       -> Colorlight ext board
#   USB_BACKUP=1              -> 2nd PHY's pins instead of the 1st
#
# Why this script exists rather than `python3 usb_only_top.py`:
#   1. LUNA's top_level_cli calls get_appropriate_platform(), which probes for
#      an Apollo debugger and dies with UnboundLocalError when none is present.
#      The repo's build_*.py pattern (platform.build(..., do_build=False) then
#      plan.execute_local) bypasses it entirely.
#   2. The Amaranth "Xray" toolchain's XDC template only emits IOSTANDARD/LOC --
#      it never iterates iter_clock_constraints(), so nextpnr-xilinx gets ZERO
#      create_clock and times every domain (and the usb<->audio CDC) blind.
#      That is the documented openXC7 "missing PLL clock constraints" failure.
#      Same injection as build_dac.py.
#
# Output goes to a per-(board,phy) directory so a USB1 bitstream can never be
# mistaken for a USB2 one -- the two differ only in pin LOCs, and flashing the
# wrong one fails silently.
#
import os
import sys

from usb_only_top import USBLoopback
from colorlight_i9plus_platform import ColorlightI9PlusPlatform

BOARD = os.environ.get("BOARD", "ext").strip().lower()
PHY = "usb2" if os.environ.get("USB_BACKUP", "") == "1" else "usb1"

NAME = "usb_only"
BUILD_DIR = f"build_usbonly_{BOARD}_{PHY}"

# Net names are the top-level Amaranth clock wires emitted by the CAR
# (sync/usb/fast/audio -> sys_clk/usb_clk/fast_clk/audio_clk). Values match
# build_dac.py, which is the recipe proven to produce clean USB audio.
# nextpnr-xilinx honours create_clock on get_nets and harmlessly ignores
# set_clock_groups (it logs "ignoring unsupported XDC command").
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

plan = ColorlightI9PlusPlatform().build(USBLoopback(), name=NAME, do_build=False)

xdc = f"{NAME}.xdc"
if xdc not in plan.files:
    print(f"ERROR: {xdc} not in build plan ({list(plan.files)})", file=sys.stderr)
    sys.exit(1)
plan.files[xdc] = plan.files[xdc] + CLOCK_XDC
print(f"BOARD={BOARD} PHY={PHY}: injected create_clock into {xdc}")

plan.execute_local(BUILD_DIR)
print("Build complete in", BUILD_DIR)
