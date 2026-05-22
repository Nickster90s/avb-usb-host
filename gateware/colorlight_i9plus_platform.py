#
# Amaranth platform definition for the Colorlight i9plus (XC7A50T-FGG484)
# carrying an external ULPI USB HS PHY breakout (e.g. USB3340 or USB3300).
#
# The i9plus SODIMM module exposes ~80 free user IOs. The ULPI pinout
# below is a PLACEHOLDER — the actual pin assignments depend on the
# ULPI breakout board layout. Update the Pins("...") strings once the
# breakout PCB is finalized.
#
# Critical constraint: ULPI clk must be on a clock-capable input pin
# (MRCC or SRCC). On XC7A50T-FGG484, candidate pins include T20, U20,
# U17, P15, P17 — pick one that's reachable from the breakout's CLKOUT.
#
# Why these pin choices: 8 data lines plus dir/nxt/stp/rst/clk = 13
# pins total. The dimm connector exposes contiguous blocks at offsets
# 41–104 and 109–198 that can fit a 13-wire ribbon cleanly.

from amaranth.build import Platform, Resource, Subsignal, Pins, PinsN, Attrs, Clock, Connector
from amaranth.vendor.xilinx import XilinxPlatform

from colorlight_i9plus_car import ColorlightI9PlusCAR

# Reuse the LiteX-Boards i9plus pin map (200-pin SODIMM dictionary).
# Same physical board, different HDL framework — re-encode here so we
# don't take a runtime dependency on litex-boards.
_DIMM_PINS = [
    "GND", "5V", "GND", "5V", "GND", "5V", "GND", "5V", "GND", "5V",        # 1-10
    "GND", "5V", "NC", "NC", "ETH1_1P", "ETH2_1P", "ETH1_1N", "ETH2_1N", "NC", "NC",  # 11-20
    "ETH1_2N", "ETH2_2N", "ETH1_2P", "ETH2_2P", "NC", "NC", "ETH1_3P", "ETH2_3P", "ETH1_3N", "ETH2_3N",  # 21-30
    "NC", "NC", "ETH1_4N", "ETH2_4N", "ETH1_4P", "ETH2_4P", "NC", "NC", "GND", "GND",  # 31-40
    "R2", "P5", "P6", "T6", "R6", "U7", "T1", "U6", "T3", "U5",             # 41-50
    "T4", "V5", "T4", "U1", "GND", "GND", "U2", "H3", "U3", "J1",           # 51-60
    "V2", "K1", "V3", "L1", "W1", "M1", "Y1", "J2", "AA1", "K2",            # 61-70
    "AB1", "K3", "W2", "G3", "Y2", "J4", "AB2", "G4", "AA3", "F4",          # 71-80
    "AB3", "L4", "Y3", "R3", "W4", "M3", "AA4", "V4", "Y4", "R4",           # 81-90
    "AB5", "T5", "AA5", "J5", "Y6", "J6", "AB6", "W5", "AA6", "L5",         # 91-100
    "Y7", "L6", "AB7", "W6", "GND", "GND", "GND", "GND", "AA8", "V7",       # 101-110
    "AB8", "N13", "Y8", "N14", "W7", "P15", "Y9", "P16", "V8", "R16",       # 111-120
    "W9", "N17", "V9", "V17", "R14", "P17", "P14", "U17", "W17", "T18",     # 121-130
    "Y18", "R17", "AA18", "U18", "W19", "R18", "AB18", "N18", "Y19", "R19", # 131-140
    "AA19", "N19", "V18", "N15", "V19", "M16", "AB20", "M15", "AA20", "L15",# 141-150
    "AA21", "L16", "AB21", "K14", "Y21", "N22", "GND", "GND", "AB22", "J14",# 151-160
    "W20", "J15", "Y22", "J19", "W21", "H13", "W22", "H14", "V20", "H17",   # 161-170
    "V22", "H15", "U21", "G18", "U20", "G17", "T20", "G16", "P19", "F16",   # 171-180
    "P20", "F15", "M18", "E17", "L19", "E16", "J17", "D16", "K18", "D15",   # 181-190
    "K19", "C18", "K16", "C17", "H18", "B20", "H19", "B17", "NC", "NC",     # 191-200
]
def _dimm(n):
    """Look up the FPGA pin attached to SODIMM pin number n (1-indexed)."""
    return _DIMM_PINS[n - 1]

# ULPI pinout — physically wired to the P2 header on the i9plus ext board.
# P2 breaks out the SODIMM Top row (odd pins 41..95), giving 20 free
# FPGA signal pins + 3V3 + GND + 5V rails right on the header — the
# USB3300 breakout can be powered straight from P2's 3V3 pin.
#
# All 13 ULPI signals are Bank-35 pins on P2 (no overlap with the
# Ethernet PHYs which live in Bank 14/13). CLK on T3 — most likely
# Bank-35 clock-capable (SRCC/MRCC); if the toolchain rejects it,
# swap with T4 or U3 (also corner-of-bank, likely clock-capable).
# The 60 MHz from the USB3300 drives the `ulpi` clock domain.
_ULPI_DIMM = {
    "clk": 49,   # T3   — Bank 35 clock-capable candidate (verify at build)
    "dir": 51,   # T4
    "nxt": 57,   # U2
    "stp": 59,   # U3
    "rst": 41,   # R2   — active-LOW per USB3300 datasheet
    "d0":  61,   # V2
    "d1":  63,   # V3
    "d2":  65,   # W1
    "d3":  73,   # W2
    "d4":  67,   # Y1
    "d5":  69,   # AA1
    "d6":  71,   # AB1
    "d7":  75,   # Y2
}


class ColorlightI9PlusPlatform(XilinxPlatform):
    """Colorlight i9plus v6.1 (XC7A50T-FGG484) with external ULPI PHY."""
    device      = "xc7a50t"
    package     = "fgg484"
    speed       = "1"
    default_clk = "clk25"

    # Required by LUNA's top_level_cli — gives the generated SoC its
    # own sync/usb/fast clock domains.
    clock_domain_generator = ColorlightI9PlusCAR

    resources = [
        Resource("clk25", 0, Pins("K4", dir="i"),
                 Clock(25e6), Attrs(IOSTANDARD="LVCMOS33")),

        Resource("user_led", 0, Pins("A18", dir="o"),
                 Attrs(IOSTANDARD="LVCMOS33")),

        # ----- USB HS via external ULPI breakout -----
        # PLACEHOLDER pinout — update once breakout PCB exists.
        Resource("ulpi", 0,
            Subsignal("clk",  Pins(_dimm(_ULPI_DIMM["clk"]), dir="i"),
                      Clock(60e6)),
            Subsignal("dir",  Pins(_dimm(_ULPI_DIMM["dir"]), dir="i")),
            Subsignal("nxt",  Pins(_dimm(_ULPI_DIMM["nxt"]), dir="i")),
            Subsignal("stp",  Pins(_dimm(_ULPI_DIMM["stp"]), dir="o")),
            Subsignal("rst",  PinsN(_dimm(_ULPI_DIMM["rst"]), dir="o")),
            Subsignal("data", Pins(" ".join(_dimm(_ULPI_DIMM[f"d{i}"]) for i in range(8)),
                                   dir="io")),
            Attrs(IOSTANDARD="LVCMOS33", SLEW="FAST")),

        # The AVB-side Ethernet PHYs (PHY0 + PHY1) will be added in Phase 3
        # when we bring over the LiteEth + AVB stack from avb-aes3.
    ]

    connectors = [Connector("dimm", 0, " ".join(_DIMM_PINS))]
