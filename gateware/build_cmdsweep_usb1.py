from ulpi_cmdsweep_top import UlpiCmdSweep
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiCmdSweep(), name="cmdsweep", do_build=False)
plan.execute_local("build_cmdsweep_usb1")
print("Build complete in build_cmdsweep_usb1")
