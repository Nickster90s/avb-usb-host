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
# All 13 ULPI signals are Bank-34 pins on P2. CLK MUST be on a clock-
# capable input — on FGG484, T4 is IO_L13N_T2_MRCC_34, while T3 is just
# IO_0_34 (NOT clock-capable). Verified against prjxray-db's
# package_pins.csv. CLK on T3 silently builds but never clocks the
# ulpi domain because there's no MRCC/SRCC there to feed a BUFG.
import os as _os
# ---- Board revision + ULPI pin maps (SODIMM pin numbers) --------------------
# BOARD=ext      (default) Colorlight ext-board + jumpered ULPI breakouts.
# BOARD=breakout Custom SODIMM baseboard (2 magjacks, 2x USB3300, JTAG/UART USB).
# USB_BACKUP=1   select the 2nd PHY's pins instead of the 1st.
#
# On the custom board BOTH ULPI buses are in BANK 34, entirely on ODD SODIMM
# pins, and SODIMM 88 (V4) / 90 (R4) are left NC as the differential partners of
# the two clocks W4 / T4.  The two BREAKOUT maps are verified identical to
# avb-aes3/avb_soc.py _ULPI_PINS -- keep those in sync.  The ext/backup maps
# intentionally DIFFER between the projects: this one is the 2026-09-10 bank-34
# cluster, while avb-aes3 still carries the older bank-14 map whose D6/D7 sit on
# config-control pins (stuck 0xC0).  Don't "fix" that by copying either way.
# See memory project-custom-sodimm-breakout-pinout.
_BOARD = _os.environ.get("BOARD", "ext").strip().lower()
_BACKUP = _os.environ.get("USB_BACKUP", "") == "1"

_ULPI_MAPS = {
    # ---- Main ("USB1") ------------------------------------------------------
    # Same FPGA balls on both boards, but the custom board wires D[7:0] in
    # ascending SODIMM order, which ROTATES four bits:
    #   ext       61..75 -> D0 D1 D2 D4 D5 D6 D3 D7   (D3 sits on 73)
    #   breakout  61..75 -> D0 D1 D2 D3 D4 D5 D6 D7
    # Picking the wrong one is SILENT -- the bus toggles, the PHY clocks, and
    # you get corrupt descriptors / error-71.
    ("ext", "main"): {
        "clk": 51, "dir": 49, "nxt": 57, "stp": 59, "rst": 41,
        "d0": 61, "d1": 63, "d2": 65, "d3": 73,
        "d4": 67, "d5": 69, "d6": 71, "d7": 75,
    },
    ("breakout", "main"): {
        "clk": 51, "dir": 49, "nxt": 57, "stp": 59, "rst": 41,
        "d0": 61, "d1": 63, "d2": 65, "d3": 67,
        "d4": 69, "d5": 71, "d6": 73, "d7": 75,
    },

    # ---- Backup ("USB2") ----------------------------------------------------
    # ext: bank-34 HDMI-safe cluster on the 2nd jumpered breakout.
    #   clk V4=88  dir R4=90  stp T5=92
    #   d0 Y9=117  d1 V8=119  d2 W9=121  d3 V9=123  d4 W6=104  d5 V7=110
    # Two historical hardware workarounds are preserved as env switches:
    #   USB_SWAP_D67=1 swaps D6/D7 (Y3=83 <-> Y6=95) to cancel a physical cross
    #                  on that breakout's wiring.
    #   NXT_W4=1       restores the OLD nxt=W4(85)/rst=Y4(89) mapping. Default is
    #                  nxt=Y4(89)/rst=W4(85): W4 is the differential PARTNER of
    #                  the clock V4, so the STATIC rst lives there and cannot
    #                  couple into the 60 MHz clock.
    ("ext", "backup"): {
        "clk": 88, "dir": 90, "stp": 92,
        "nxt": 85 if _os.environ.get("NXT_W4", "") == "1" else 89,
        "rst": 89 if _os.environ.get("NXT_W4", "") == "1" else 85,
        "d0": 117, "d1": 119, "d2": 121, "d3": 123, "d4": 104, "d5": 110,
        "d6": 95 if _os.environ.get("USB_SWAP_D67", "") == "1" else 83,
        "d7": 83 if _os.environ.get("USB_SWAP_D67", "") == "1" else 95,
    },
    # breakout: bank 34, ALL-ODD SODIMM, CLK=W4 (85, MRCC L12N_T1_34).
    # V4 (88) is W4's partner and T4 (51) is the Main clock -> both NC on that
    # board, so nothing switching sits next to either clock.
    # ---- 2026-10-28, THE CLOCK PIN: MEASURED, not assumed -------------------
    # The EasyEDA PCB netlist (FPGA/easyeda/PCB_PCB_fpga_breakout_*.json) says
    # USB2_CLK is routed CN1.85 -> J5.1 (178.2 mil, no via, no series R) and
    # that SODIMM 87 carries net "CN1_87" with ZERO COPPER.  So on the PCB, 85
    # is the clock pin and 87 does not exist.
    # The BOARD disagrees with the PCB.  ulpi_freq2_top.py counts BOTH pins in
    # one bitstream (PLL-free, 25 MHz crystal gate):
    #     W(85/W4) = 00000000 Hz      A(87/AA4) = 60,002,311 Hz
    # and with a FASM-patched 4.7k PULLDOWN on AA4 (openXC7 drops PULLDOWN from
    # the XDC -- patch RIOB33_X43Y27.IOB_Y1.PULLTYPE) it still reads
    #     A = 60,002,301 Hz           (unchanged, 0.17 ppm)
    # A pulldown crushes a capacitively coupled signal and does nothing to a
    # driven one, so AA4 is GENUINELY DC-DRIVEN -- it is not crosstalk from 85.
    # => The 60 MHz CLKOUT physically reaches SODIMM 87, and SODIMM 85 (the
    #    PCB's engineered, length-matched clock route) is dead: the present
    #    clock path is an off-PCB bench wire.
    # Default stays 87 because that is where the clock actually is.  Set
    # USB2_CLK_DIMM=85 once the clock wire is moved to J5.1, which is the
    # better home: W4 is IO_L12N_T1_MRCC_34 while AA4 is only SRCC, and 85 has
    # 178 mil of matched PCB copper instead of a flying lead.
    # NC CONSTRAINT: with the clock on 87 (AA4 = L11N) its differential partner
    #   Y4 = SODIMM 89 (L11P) must stay unused/quiet.  On 85 (W4 = L12N) the
    #   partner is V4 = SODIMM 88.  The PCB leaves 88 and 89 both unrouted.
    # *** ROOT CAUSE OF THE WHOLE USB2 BRING-UP SAGA, FOUND 2026-10-28 ***
    # DIR and NXT are PHYSICALLY SWAPPED in the hand-wiring at position B, so
    # the gateware swaps them back.  DEFAULT IS NOW SWAPPED (=1) because that
    # matches the board as wired; set USB_SWAP_DIRNXT=0 after physically
    # exchanging the two leads at J5 (J5.2=USB2_DIR, J5.5=USB2_NXT -- note J5's
    # pin order differs from J4's, which is how the pair got transposed).
    # PROOF: with the swap, trace64 returns a textbook register read, and 0x24
    # (USB3300 vendor-ID low) 1836 times in one capture:
    #   10C0 IDLE -> 30C0 CMD(0xC0) -> 38C0 NXT=1 accept -> 34C0 DIR=1 turn
    #        -> 4424 oe=0 D=24  -> 6000/7000 DATA
    # Without it, 0x24 never appears at any of 8 clock phases or any of 3
    # clocking modes.
    # USB_SWAP_DIRNXT=1 exchanges DIR and NXT (2026-10-28).
    #   Why: at position B the PHY accepts every TXCMD (our nxt_i asserts) and
    #   then nxt_i STAYS high for 3+ cycles while dir_i shows only a 1-cycle
    #   blip that `oe = ~dir_i` catches but the trace's own dir bit misses.
    #   Read with DIR and NXT exchanged, that is a textbook register read:
    #   PHY NXT pulses once to accept, then PHY DIR asserts for several cycles
    #   to hand back the data.  Because `oe` is driven from the WRONG pin it
    #   never releases the bus during the turnaround, so we drive against the
    #   PHY's data and read back our own C0/00 -- which is exactly why 0x24
    #   never appears, at ANY clock phase and in ANY clocking mode (a wiring
    #   permutation is phase-independent, which nothing else explained).
    ("breakout", "backup"): {
        "clk": int(_os.environ.get("USB2_CLK_DIMM", "87")),
        "dir": 97 if _os.environ.get("USB_SWAP_DIRNXT", "1") == "0" else 103,
        "nxt": 103 if _os.environ.get("USB_SWAP_DIRNXT", "1") == "0" else 97,
        "stp": 101, "rst": 99,
        "d0": 109, "d1": 111, "d2": 113, "d3": 115,
        "d4": 117, "d5": 119, "d6": 121, "d7": 123,
    },
}

_key = (_BOARD, "backup" if _BACKUP else "main")
try:
    _ULPI_DIMM = _ULPI_MAPS[_key]
except KeyError:
    raise ValueError(
        "BOARD=%r has no %s ULPI map; expected one of %s"
        % (_BOARD, _key[1], sorted({b for b, _ in _ULPI_MAPS})))

# The OTHER map, for a design that drives BOTH USB3300s from one bitstream.
# `ulpi`/`ulpi_clock` stay exactly as they are -- env-selected, so every
# archived build reproduces byte-for-byte -- and the second bus is exposed
# ADDITIVELY as `ulpi_b`/`ulpi_clock_b`. A dual design therefore leaves
# USB_BACKUP unset and requests ulpi (= Main) plus ulpi_b (= Backup).
# None when the board has only one map, so requesting ulpi_b then fails loudly
# at build time rather than silently landing on the wrong pins.
_ULPI_DIMM_B = _ULPI_MAPS.get((_BOARD, "main" if _BACKUP else "backup"))


def _ulpi_b_resources():
    """The second ULPI bus as Resources, or [] when the board has no 2nd map.

    Mirrors the `ulpi` / `ulpi_clock` resources exactly, minus the diagnostic
    pull attributes (USB_PULLDN / USB_PULLUP_DATA), which are single-bus
    bring-up aids and would be ambiguous applied to two buses at once.

    NOTE on IDELAY: both ULPI buses live in BANK 34 on the breakout, so a dual
    design needs exactly ONE IDELAYCTRL shared by all 20 IDELAYE2s. Two will
    fail with "more than one IDELAYCTRL for the default IODELAY_GROUP" -- this
    is why avb-aes3 shares the CRG's single S7IDELAYCTRL between its two ULPIs.
    """
    if _ULPI_DIMM_B is None:
        return []
    d = _ULPI_DIMM_B
    return [
        Resource("ulpi_clock_b", 0, Pins(_dimm(d["clk"]), dir="i"),
                 Clock(60e6), Attrs(IOSTANDARD="LVCMOS33")),
        Resource("ulpi_b", 0,
            Subsignal("dir",  Pins(_dimm(d["dir"]), dir="i"), Attrs(**_IN_IOB)),
            Subsignal("nxt",  Pins(_dimm(d["nxt"]), dir="i"), Attrs(**_IN_IOB)),
            Subsignal("stp",  Pins(_dimm(d["stp"]), dir="o"), Attrs(IOB="TRUE")),
            Subsignal("rst",  Pins(_dimm(d["rst"]), dir="o")),
            Subsignal("data", Pins(" ".join(_dimm(d[f"d{i}"]) for i in range(8)),
                                   dir="io"), Attrs(**_IN_IOB)),
            Attrs(IOSTANDARD="LVCMOS33", SLEW="FAST")),
    ]

# When USB_IDELAY_TAP is set, an IDELAYE2 sits in each ULPI INPUT path
# (data/dir/nxt), and IOB is dropped from those pins.
#
# CORRECTION 2026-10-28: the original note here claimed IOB="TRUE" was what
# "fights the IDELAY for the ILOGIC/routing (3 stuck overused wires)". That is
# UNSUPPORTED. With IOB verified absent from every input in the generated XDC
# (only `stp` keeps it), nextpnr-xilinx still parks at exactly `overused=3` and
# thrashes indefinitely -- 4700 router iterations with the count never moving --
# in VAR_LOAD mode and in VARIABLE mode with a single shared CE net. So the
# IOB attribute is not the cause, and dropping it does not fix the convergence
# failure. **No IDELAY build has ever routed on this design, in any mode**: no
# archived bitstream in bitstreams/ has USB_IDELAY_TAP set, so the IDELAY tap
# axis has never actually been exercised and must not be cited as explored.
# CAUSE FOUND 2026-10-29, in the SISTER PROJECT: ../../avb-aes3/avb_soc.py:675
# runs this exact IDELAY-on-ULPI mechanism and ROUTES, on both ULPI ports, as a
# shipping design. Its comment names the failure mode outright: adding a
# boot-time LD pulse "STALLED nextpnr's HeAP placer (41 s -> 15+ min, because
# the pulse fans out to 10 IO-site-locked IDELAYE2s). Do not do that." That is
# precisely what ulpi_tapscan_top.py does in both VAR_LOAD and VARIABLE mode,
# so the `overused=3` thrash is the GENERATED SWEEP LOGIC, not IDELAY itself
# and not the IOB attribute. The earlier "IOI clock resources will not take
# both" guess is withdrawn.
#
# So: a RUNTIME tap sweep needs a CPU/CSR to pulse LD (avb-aes3 has one, driven
# by its firmware 'u' command); a FIXED tap costs ZERO extra nets (LD/CE/INC/
# CNTVALUEIN all tied to constants) and is the form to use here. Sweep by
# rebuilding at each tap, not by generating the sweep in fabric.
#
# avb-aes3 pins tap 8 as the CSR reset AND as IDELAY_VALUE for both ULPI ports
# (VAR_LOAD powers up at IDELAY_VALUE and only takes CNTVALUEIN on an LD pulse,
# so the two must match or every build silently runs at IDELAY_VALUE). Tap 8 is
# a STARTING POINT here, not a proven value: the eye centre depends on the
# IOB->first-flop routing delay, which differs per design and per rebuild.
#
# Note also that IOB=TRUE is itself a no-op on openXC7 (zero input-flop
# features appear in the FASM), so these pins have no flop at the pad either
# way.
_IN_IOB = {} if _os.environ.get("USB_IDELAY_TAP") is not None else {"IOB": "TRUE"}


class ColorlightI9PlusPlatform(XilinxPlatform):
    """Colorlight i9plus v6.1 (XC7A50T-FGG484) with external ULPI PHY."""
    name        = "colorlight_i9plus"           # used by LUNA logging
    device      = "xc7a50t"
    package     = "fgg484"
    speed       = "1"
    default_clk = "clk25"

    # LUNA platform attributes — without ignore_phy_vbus, the reset
    # sequencer waits for VBUS detection via PHY RxCmd messages, but
    # those never arrive on first power-up of a USB3300 → controller
    # stalls in IDLE, no register writes, no D[7:0] activity.
    # default_usb_connection tells LUNA which platform resource is the
    # USB PHY.
    default_usb_connection = "ulpi"
    ignore_phy_vbus        = True

    # Required by LUNA's top_level_cli — gives the generated SoC its
    # own sync/usb/fast clock domains.
    clock_domain_generator = ColorlightI9PlusCAR

    def __init__(self):
        # "Xray" = yosys + nextpnr-xilinx + prjxray-db (the openXC7 stack
        # we already use for avb-aes3). Amaranth's default is Vivado.
        super().__init__(toolchain="Xray")

    @property
    def _xray_device(self):
        # Amaranth's default returns just "xc7a50t" (family). Our
        # prjxray chipdb is per-package: "xc7a50tfgg484.bin".
        return f"{self.device}{self.package}"

    @property
    def vendor_toolchain(self):
        # Amaranth's XilinxPlatform claims vendor_toolchain only for
        # Vivado/ISE; Xray gets the generic Platform.get_input_output
        # path which rejects IOSTANDARD attrs. We DO want IOSTANDARD on
        # all pins (it ends up in the generated XDC, which nextpnr-
        # xilinx reads), so override to claim vendor support for IO
        # construction. The vendor-specific tooling commands are still
        # selected by `self.toolchain == "Xray"` elsewhere.
        return True

    resources = [
        Resource("clk25", 0, Pins("K4", dir="i"),
                 Clock(25e6), Attrs(IOSTANDARD="LVCMOS33")),

        Resource("user_led", 0, Pins("A18", dir="o"),
                 Attrs(IOSTANDARD="LVCMOS33")),

        # UART TX to the CH347 (FPGA -> CH347 RXD1 on R3). Lets a headless
        # gateware diagnostic print to the user's picocom without a CPU.
        Resource("uart_tx", 0, Pins("R3", dir="o"),
                 Attrs(IOSTANDARD="LVCMOS33")),

        # ----- USB HS via external ULPI breakout -----
        # IMPORTANT: separate the ULPI CLK into its own Resource.
        # When clk is a Subsignal of `ulpi`, yosys+nextpnr-xilinx does
        # not always produce the global-clock-buffer insertion that
        # LUNA's ULPI controller needs. Matching usbsniffer (the only
        # listed-as-fully-supported Artix-7 + USB3300 LUNA platform).
        Resource("ulpi_clock", 0, Pins(_dimm(_ULPI_DIMM["clk"]), dir="i"),
                 Clock(60e6), Attrs(IOSTANDARD="LVCMOS33")),

        # Diagnostic-only: a SECOND clock-pin input so one bitstream can
        # measure the real ULPI clock pin and a neighbour side by side
        # (built-in differential control).  Set USB_CLK_ALT to a SODIMM pin
        # number; default 87 (AA4), the pin the PCB leaves unrouted and which
        # therefore reads pure crosstalk from 85 (W4).
        Resource("ulpi_clock_alt", 0,
                 Pins(_dimm(int(_os.environ.get("USB_CLK_ALT", "87"))), dir="i"),
                 Attrs(IOSTANDARD="LVCMOS33")),

        Resource("ulpi", 0,
            # IOB=TRUE on the inputs: forces the first register driven
            # by each input pin to be packed into the IOB block,
            # eliminating fabric-routing delay. Without this on
            # yosys+nextpnr-xilinx the input flops land deep in the
            # fabric and the ULPI 60 MHz setup time can't be met.
            # Quartus does this automatically; the open Xilinx toolchain
            # needs an explicit attr.
            # USB_PULLDN=1: add internal pull-downs on DIR/NXT for the
            # disconnected-line diagnostic. If DIR/NXT then read LOW they were
            # floating (not wired); if NXT still reads HIGH it is really driven.
            Subsignal("dir",  Pins(_dimm(_ULPI_DIMM["dir"]), dir="i"),
                      Attrs(**_IN_IOB,
                            **({"PULLDOWN": "TRUE"} if _os.environ.get("USB_PULLDN") == "1" else {}))),
            Subsignal("nxt",  Pins(_dimm(_ULPI_DIMM["nxt"]), dir="i"),
                      Attrs(**_IN_IOB,
                            **({"PULLDOWN": "TRUE"} if _os.environ.get("USB_PULLDN") == "1" else {}))),
            Subsignal("stp",  Pins(_dimm(_ULPI_DIMM["stp"]), dir="o"),
                      Attrs(IOB="TRUE")),
            Subsignal("rst",  Pins(_dimm(_ULPI_DIMM["rst"]), dir="o")),
            # USB_PULLUP_DATA=1: internal pull-UPs on D0-7 for the
            # open-vs-short diagnostic. With our output enable off, a line that
            # then reads 1 is FLOATING (not connected); one that still reads 0
            # is held LOW (shorted to GND, or actively driven).
            Subsignal("data", Pins(" ".join(_dimm(_ULPI_DIMM[f"d{i}"]) for i in range(8)),
                                   dir="io"),
                      Attrs(**_IN_IOB,
                            **({"PULLUP": "TRUE"}
                               if _os.environ.get("USB_PULLUP_DATA") == "1" else {}))),
            Attrs(IOSTANDARD="LVCMOS33", SLEW="FAST")),

        # The SECOND ULPI bus, for driving both USB3300s from one bitstream.
        # Empty on a board with only one map, so requesting it fails loudly.
        *_ulpi_b_resources(),

        # ----- DIAGNOSTIC: DIR/NXT as OUTPUTS -----------------------------
        # Same pins as the "ulpi" resource's dir/nxt, but drivable, so the
        # drive-contention continuity test (validated on the data lines:
        # ~2.6 V = connected/contending, ~3.3 V = open) can be applied to DIR
        # and NXT, which are inputs in normal operation and therefore cannot
        # be checked any other way. Request this INSTEAD of "ulpi", never
        # alongside it -- they claim the same pins.
        Resource("ulpi_ctl_drive", 0,
            Subsignal("dir", Pins(_dimm(_ULPI_DIMM["dir"]), dir="o")),
            Subsignal("nxt", Pins(_dimm(_ULPI_DIMM["nxt"]), dir="o")),
            Subsignal("stp", Pins(_dimm(_ULPI_DIMM["stp"]), dir="o")),
            Subsignal("rst", Pins(_dimm(_ULPI_DIMM["rst"]), dir="o")),
            Attrs(IOSTANDARD="LVCMOS33")),

        # ----- DIAGNOSTIC: DIR/NXT as BIDIRECTIONAL (contention readback) --
        # Same pins again, but dir="io" so the gateware can DRIVE the pin and
        # READ THE SAME PAD BACK. DIR is driven by the PHY at all times, so:
        #   line CONNECTED -> our HIGH fights the PHY's LOW, the pad sits below
        #                     Vih and the readback DISAGREES with what we drive
        #   line OPEN       -> nothing fights us, readback always AGREES
        # This settles "is DIR real or is it a floating pin reading noise?"
        # with no meter, no pull-ups (openXC7 drops PULLUP/PULLDOWN silently)
        # and no dependence on the PHY answering a command. Request this
        # INSTEAD of "ulpi" / "ulpi_ctl_drive" -- they claim the same pins.
        # DIR and NXT as BIDIRECTIONAL, plus stp/rst/data so this resource can
        # stand in for "ulpi" entirely (they claim the same pins -- request one
        # or the other, never both).  Needed for the drive-and-RELEASE decay
        # timing on DIR vs NXT: contention readback is useless (our driver wins
        # at our own pad) but release-and-time measures how hard the far end
        # pulls the net back, which is exactly the DIR-vs-NXT asymmetry to
        # chase.  stp/data are here only so they can be parked legally.
        Resource("ulpi_ctl_io", 0,
            Subsignal("dir", Pins(_dimm(_ULPI_DIMM["dir"]), dir="io")),
            Subsignal("nxt", Pins(_dimm(_ULPI_DIMM["nxt"]), dir="io")),
            Subsignal("rst", Pins(_dimm(_ULPI_DIMM["rst"]), dir="o")),
            Subsignal("stp", Pins(_dimm(_ULPI_DIMM["stp"]), dir="o")),
            Subsignal("data", Pins(" ".join(_dimm(_ULPI_DIMM[f"d{i}"])
                                            for i in range(8)), dir="io")),
            Attrs(IOSTANDARD="LVCMOS33")),

        # ----- I2S DAC (PCM5102A) — same balls as avb-aes3 i2s_io() -----
        # SODIMM 46/48/50 → balls U7/U6/U5. DAC board: SCK=GND (no-MCLK,
        # internal PLL), FMT=I2S, XSMT=unmute.
        Resource("i2s_dac", 0,
            Subsignal("bck",  Pins("U7", dir="o")),
            Subsignal("lrck", Pins("U6", dir="o")),
            Subsignal("din",  Pins("U5", dir="o")),
            Attrs(IOSTANDARD="LVCMOS33")),

        # The AVB-side Ethernet PHYs (PHY0 + PHY1) will be added in Phase 3
        # when we bring over the LiteEth + AVB stack from avb-aes3.
    ]

    connectors = [Connector("dimm", 0, " ".join(_DIMM_PINS))]
