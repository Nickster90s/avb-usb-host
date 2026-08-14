# Faithful "as Main is built": same top (USBLoopbackUTMI), same flow as
# usb_utmi_top.py's top_level_cli, but write to a fixed dir + no auto-program,
# and NO create_clock injection (exactly like the literal README §8 recipe).
from usb_utmi_top import USBLoopbackUTMI
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(USBLoopbackUTMI(), name="top", do_build=False)
plan.execute_local("build_mainrecipe")
print("Build complete in build_mainrecipe")
