import os
from ulpi_scratch_top import UlpiScratch
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
d = os.environ.get("BUILD_DIR", "build_scratch")
plan = ColorlightI9PlusPlatform().build(UlpiScratch(), name="scratch", do_build=False)
plan.execute_local(d)
print("Build complete in", d)
