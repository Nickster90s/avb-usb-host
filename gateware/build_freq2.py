from ulpi_freq2_top import UlpiFreq2
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiFreq2(), name="freq2", do_build=False)
plan.execute_local("build_freq2")
print("Build complete in build_freq2")
