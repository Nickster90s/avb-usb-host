from ulpi_tapscan_top import UlpiTapScan
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiTapScan(), name="tapscan", do_build=False)
plan.execute_local("build_tapscan")
print("Build complete in build_tapscan")
