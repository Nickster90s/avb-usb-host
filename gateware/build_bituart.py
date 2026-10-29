from ulpi_bituart_top import UlpiBitUart
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiBitUart(), name="bituart", do_build=False)
plan.execute_local("build_bituart")
print("Build complete in build_bituart")
