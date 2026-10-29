from ulpi_freqtest_top import UlpiFreqTest
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiFreqTest(), name="freqtest", do_build=False)
plan.execute_local("build_freqtest")
print("Build complete in build_freqtest")
