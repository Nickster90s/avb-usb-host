from ulpi_pindecay_top import UlpiPinDecay
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiPinDecay(), name="pindecay", do_build=False)
plan.execute_local("build_pindecay_usb1")
print("Build complete in build_pindecay_usb1")
