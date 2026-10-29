from ulpi_drive_test_top import UlpiDriveTest
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiDriveTest(), name="drivetest", do_build=False)
plan.execute_local("build_drivetest")
print("Build complete in build_drivetest")
