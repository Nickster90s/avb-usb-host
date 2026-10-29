from ulpi_nxtprobe_top import UlpiNxtProbe
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiNxtProbe(), name="nxtprobe", do_build=False)
plan.execute_local("build_nxtprobe")
print("Build complete in build_nxtprobe")
