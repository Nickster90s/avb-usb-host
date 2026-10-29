from ulpi_ctldecay_top import UlpiCtlDecay
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiCtlDecay(), name="ctldecay", do_build=False)
plan.execute_local("build_ctldecay")
print("Build complete in build_ctldecay")
