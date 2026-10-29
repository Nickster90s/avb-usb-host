# Real USB design + the env-gated UTMI enumeration probe (USB_PROBE=1).
#   BOARD=breakout USB_BACKUP=1 USB_PHASE=0 USB_PROBE=1 [USB_IDELAY_TAP=n] \
#     ../.venv/bin/python3 build_probe.py
# BUILD_DIR overrides the output dir so a tap sweep does not clobber itself.
#
# The Amaranth Xray template emits ZERO create_clock (see build_dac.py), so
# nextpnr times every path blind. That matters more here than anywhere else:
# with no input flop at the IOB and no IDELAY, the IOB->first-flop ROUTING
# delay IS the ULPI sampling point, so placement quality decides whether USB
# enumerates. Two unconstrained P&R runs of this same design at position B gave
# "HS detect + error -71" and "no pull-up at all" respectively. Inject the
# constraints -- and note fast_clk (200 MHz) is the IDELAYCTRL REFCLK, which
# nextpnr cannot route on clock resources unless it knows it is a clock.
import os, sys
from usb_utmi_top import USBLoopbackUTMI
from colorlight_i9plus_platform import ColorlightI9PlusPlatform

NAME = "top"
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

d = os.environ.get("BUILD_DIR", "build_probe")
plan = ColorlightI9PlusPlatform().build(USBLoopbackUTMI(), name=NAME, do_build=False)

xdc = f"{NAME}.xdc"
if xdc not in plan.files:
    print(f"ERROR: {xdc} not in build plan ({list(plan.files)})", file=sys.stderr)
    sys.exit(1)
if os.environ.get("NO_CLOCK_XDC") != "1":
    plan.files[xdc] = plan.files[xdc] + CLOCK_XDC
    print("Injected create_clock into", xdc)

if os.environ.get("ELAB_ONLY") == "1":
    os.makedirs(d, exist_ok=True)
    for n, c in plan.files.items():
        with open(os.path.join(d, n), "wb" if isinstance(c, bytes) else "w") as f:
            f.write(c)
    print("Elaborated only -> %s" % d)
else:
    plan.execute_local(d)
    print("Build complete in %s" % d)
