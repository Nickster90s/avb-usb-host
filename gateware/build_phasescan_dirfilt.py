from ulpi_phasescan_dirfilt_top import UlpiPhaseScanDirFilt
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiPhaseScanDirFilt(), name="psdirfilt", do_build=False)
plan.execute_local("build_psdirfilt")
print("Build complete in build_psdirfilt")
