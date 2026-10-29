from ulpi_ctl_drive_active_top import UlpiCtlDriveActive
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiCtlDriveActive(), name="ctldriveactive", do_build=False)
plan.execute_local("build_ctldrive_active")
print("Build complete in build_ctldrive_active")
