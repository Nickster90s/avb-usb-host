from ulpi_phasescan_uart_top import UlpiPhaseScanUart
from colorlight_i9plus_platform import ColorlightI9PlusPlatform
plan = ColorlightI9PlusPlatform().build(UlpiPhaseScanUart(), name="phasescanuart", do_build=False)
plan.execute_local("build_phasescan_uart_usb1")
print("Build complete in build_phasescan_uart_usb1")
