# Phase-parameterised trace build. BUILD_DIR picks the output directory so a
# USB_PHASE sweep does not clobber itself.
import os
from ulpi_trace_top import UlpiTrace
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
d = os.environ.get("BUILD_DIR", "build_trace")
plan = ColorlightI9PlusPlatform().build(UlpiTrace(), name="trace", do_build=False)
plan.execute_local(d)
print("Build complete in", d)
