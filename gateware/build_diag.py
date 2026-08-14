import sys
from ulpi_regread_uart_top import UlpiRegReadUart
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
NAME="diag"; BUILD_DIR="build_diag"
CLOCK_XDC="""
create_clock -name sys_clk   -period 16.667 [get_nets sys_clk]
create_clock -name usb_clk   -period 16.667 [get_nets usb_clk]
create_clock -name fast_clk  -period  5.000 [get_nets fast_clk]
create_clock -name audio_clk -period 81.660 [get_nets audio_clk]
set_clock_groups -asynchronous -group [get_clocks sys_clk] -group [get_clocks usb_clk] -group [get_clocks fast_clk] -group [get_clocks audio_clk]
"""
plan = ColorlightI9PlusPlatform().build(UlpiRegReadUart(), name=NAME, do_build=False)
x=f"{NAME}.xdc"
plan.files[x]=plan.files[x]+CLOCK_XDC
print("injected clocks")
plan.execute_local(BUILD_DIR)
print("Build complete in", BUILD_DIR)
