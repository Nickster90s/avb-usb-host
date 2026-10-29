# BOTH USB3300 PHYs from one bitstream. Leave USB_BACKUP UNSET: the dual design
# requests ulpi (Main) and ulpi_b (Backup) explicitly.
#   BOARD=breakout USB_PHASE=0 USB_IDELAY_TAP=8 ../.venv/bin/python3 build_dual.py
import os, sys
from usb_dual_top import UsbDual
from colorlight_i9plus_platform import ColorlightI9PlusPlatform

NAME = "top"
CLOCK_XDC = """
# ---- injected PLL clock constraints (Amaranth Xray template omits these) ----
create_clock -name sys_clk    -period 16.667 [get_nets sys_clk]
create_clock -name usb_clk    -period 16.667 [get_nets usb_clk]
create_clock -name usb_b_clk  -period 16.667 [get_nets usb_b_clk]
create_clock -name fast_clk   -period  5.000 [get_nets fast_clk]
create_clock -name audio_clk  -period 81.660 [get_nets audio_clk]
set_clock_groups -asynchronous \\
    -group [get_clocks sys_clk] \\
    -group [get_clocks usb_clk] \\
    -group [get_clocks usb_b_clk] \\
    -group [get_clocks fast_clk] \\
    -group [get_clocks audio_clk]
"""
d = os.environ.get("BUILD_DIR", "build_dual")
plan = ColorlightI9PlusPlatform().build(UsbDual(), name=NAME, do_build=False)
xdc = f"{NAME}.xdc"
if xdc not in plan.files:
    print(f"ERROR: {xdc} not in plan ({list(plan.files)})", file=sys.stderr)
    sys.exit(1)
plan.files[xdc] = plan.files[xdc] + CLOCK_XDC
print("Injected create_clock into", xdc)
if os.environ.get("ELAB_ONLY") == "1":
    os.makedirs(d, exist_ok=True)
    for n, c in plan.files.items():
        with open(os.path.join(d, n), "wb" if isinstance(c, bytes) else "w") as f:
            f.write(c)
    print("Elaborated only ->", d)
else:
    plan.execute_local(d)
    print("Build complete in", d)
