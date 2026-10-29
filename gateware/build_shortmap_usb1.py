from ulpi_shortmap_top import UlpiShortMap
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiShortMap(), name="shortmap", do_build=False)
plan.execute_local("build_shortmap_usb1")
print("Build complete in build_shortmap_usb1")
