from usb_utmi_top import USBLoopbackUTMI
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(USBLoopbackUTMI(), name="top", do_build=False)
plan.execute_local("build_usb2_180")
print("Build complete in build_usb2_180")
