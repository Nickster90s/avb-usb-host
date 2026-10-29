from ulpi_phasescan_top import *
import ulpi_phasescan_top as m, inspect
cls = [c for _, c in inspect.getmembers(m, inspect.isclass)
       if c.__module__ == 'ulpi_phasescan_top'][0]
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(cls(), name="phasescan", do_build=False)
plan.execute_local("build_phasescan_usb2")
print("Build complete in build_phasescan_usb2")
