from ulpi_pinmon_uart_top import UlpiPinMon
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiPinMon(), name="pinmon", do_build=False)
plan.execute_local("build_pinmon")
print("Build complete in build_pinmon")
