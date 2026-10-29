# Build the ultraembedded-core backup ULPI probe, same flow as Main
# (build_mainrecipe.py): no create_clock injection, fixed output dir.
# Run with USB_BACKUP=1 to target the backup pins.
from ulpi_utmi_probe_top import UlpiUtmiProbe
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiUtmiProbe(), name="utmiprobe", do_build=False)
plan.execute_local("build_utmiprobe")
print("Build complete in build_utmiprobe")
