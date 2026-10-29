import os
from ulpi_loopback_top import UlpiLoopback
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
d = os.environ.get("BUILD_DIR", "build_loopback")
plan = ColorlightI9PlusPlatform().build(UlpiLoopback(), name="loopback", do_build=False)
plan.execute_local(d)
print("Build complete in", d)
