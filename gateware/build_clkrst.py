from ulpi_clkrst_top import UlpiClkRst
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiClkRst(), name="clkrst", do_build=False)
plan.execute_local("build_clkrst")
print("Build complete in build_clkrst")
