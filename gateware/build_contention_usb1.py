from ulpi_contention_top import UlpiContention
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiContention(), name="contention", do_build=False)
plan.execute_local("build_contention_usb1")
print("Build complete in build_contention_usb1")
