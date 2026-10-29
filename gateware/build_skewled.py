from ulpi_skewled_top import UlpiSkewLed
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiSkewLed(), name="skewled", do_build=False)
plan.execute_local("build_skewled")
print("Build complete in build_skewled")
