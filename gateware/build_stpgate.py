from ulpi_stpgate_top import UlpiStpGate
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiStpGate(), name="stpgate", do_build=False)
plan.execute_local("build_stpgate")
print("Build complete in build_stpgate")
