from ulpi_diredge_top import UlpiDirEdge
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiDirEdge(), name="diredge", do_build=False)
plan.execute_local("build_diredge")
print("Build complete in build_diredge")
