from ulpi_trace_top import UlpiTrace
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiTrace(), name="trace", do_build=False)
plan.execute_local("build_trace_usb1")
print("Build complete in build_trace_usb1")
