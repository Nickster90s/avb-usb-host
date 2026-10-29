#!/usr/bin/env python3
# Build the UART per-bit ULPI test for USB1/PHY-A (T4). Do NOT set USB_BACKUP., with injected PLL clock constraints
# (the Amaranth Xray XDC template emits none -- see build_dac.py).
import sys
from ulpi_bittest_uart_top import UlpiBitTestUart
from colorlight_i9plus_platform import ColorlightI9PlusPlatform

NAME = "bittestuart"
BUILD_DIR = "build_bittest_uart_usb1"

CLOCK_XDC = """
# ---- injected clock constraints (Amaranth Xray template omits these) ----
create_clock -name sys_clk   -period 16.667 [get_nets sys_clk]
create_clock -name usb_clk   -period 16.667 [get_nets usb_clk]
create_clock -name fast_clk  -period  5.000 [get_nets fast_clk]
create_clock -name audio_clk -period 81.660 [get_nets audio_clk]
"""

plan = ColorlightI9PlusPlatform().build(UlpiBitTestUart(), name=NAME, do_build=False)
xdc = f"{NAME}.xdc"
if xdc not in plan.files:
    print(f"ERROR: {xdc} not in plan ({list(plan.files)})", file=sys.stderr); sys.exit(1)
plan.files[xdc] = plan.files[xdc] + CLOCK_XDC
print("injected create_clock into", xdc)
plan.execute_local(BUILD_DIR)
print("Build complete in", BUILD_DIR)
