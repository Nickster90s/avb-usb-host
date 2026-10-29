from ulpi_freqtest_rsthigh_top import UlpiFreqTestRstHigh
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiFreqTestRstHigh(), name="freqrsthigh", do_build=False)
plan.execute_local("build_freqrsthigh_usb1")
print("Build complete in build_freqrsthigh_usb1")
