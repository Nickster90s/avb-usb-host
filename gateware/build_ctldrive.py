from ulpi_ctl_drive_top import UlpiCtlDrive
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiCtlDrive(), name="ctldrive", do_build=False)
plan.execute_local("build_ctldrive")
print("Build complete in build_ctldrive")
