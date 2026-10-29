from ulpi_pindecay_hr_top import UlpiPinDecayHR
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiPinDecayHR(), name="pdhr", do_build=False)
plan.execute_local("build_pdhr")
print("Build complete in build_pdhr")
