import os
from ulpi_dircensus_top import UlpiDirCensus
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
d = os.environ.get("BUILD_DIR", "build_dircensus")
plan = ColorlightI9PlusPlatform().build(UlpiDirCensus(), name="dircensus", do_build=False)
plan.execute_local(d)
print("Build complete in", d)
