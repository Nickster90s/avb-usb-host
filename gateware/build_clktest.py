from ulpi_clocktest_top import UlpiClockTest
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiClockTest(), name="clktest", do_build=False)
plan.execute_local("build_clktest")
print("Build complete in build_clktest")
