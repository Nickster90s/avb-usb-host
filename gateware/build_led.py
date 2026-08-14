from ulpi_led_diag_top import UlpiLedDiag
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiLedDiag(), name="led", do_build=False)
plan.execute_local("build_led")
print("Build complete in build_led")
