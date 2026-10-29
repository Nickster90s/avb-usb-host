from ulpi_rstgate_top import UlpiRstGate
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiRstGate(), name="rstgate", do_build=False)
plan.execute_local("build_rstgate")
print("Build complete in build_rstgate")
