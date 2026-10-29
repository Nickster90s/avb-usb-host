from ulpi_bittest_top import UlpiBitTest
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiBitTest(), name="bittest", do_build=False)
plan.execute_local("build_bittest")
print("Build complete in build_bittest")
