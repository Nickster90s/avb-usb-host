from ulpi_busacc_top import UlpiBusAcc
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiBusAcc(), name="busacc", do_build=False)
plan.execute_local("build_busacc_usb1")
print("Build complete in build_busacc_usb1")
