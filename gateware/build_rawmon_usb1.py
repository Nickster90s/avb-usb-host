from ulpi_rawmon_top import UlpiRawMon
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiRawMon(), name="rawmon", do_build=False)
plan.execute_local("build_rawmon_usb1")
print("Build complete in build_rawmon_usb1")
