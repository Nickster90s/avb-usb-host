# avb-usb-host — USB UAC2 to AVB-Milan audio interface

Bridges a USB UAC2 device (host = computer/DAW) to an AVB-Milan
network endpoint, locked to the network's media clock via CRF.

```
DAW ── UAC2 OUT ──► [USB device]
                       │
                       ▼  async SRC, ratio from MCR
                    [AAF talker] ── Ethernet ──► AVB mixer
                       ▲
                    [CRF listener] ◄── Ethernet ──── (CRF from mixer)
```

## Hardware

- **FPGA board**: Colorlight i9plus v6.1 (XC7A50T-FGG484)
- **USB HS PHY**: external USB3300 ULPI breakout, wired to the **P2** header
- **Ethernet**: two on-board RGMII PHYs (B50612D) for AVB
- **I2S DAC** (optional local monitor): PCM5102A on the **P6** header — same pins as in `avb-aes3`

## Pin assignments

### USB3300 ULPI → P2 header

| ULPI signal | i9plus pin | Notes |
|---|---|---|
| 3V3, GND | P2 power rails | Power the breakout from these |
| **CLK** (60 MHz IN from PHY) | **T4** | IO_L13N_T2_MRCC_34 (clock-capable) — required for global clock buffer |
| DIR | T3 | plain IO_0_34, not clock-capable — fine for non-clock signal |
| NXT | U2 | |
| STP | U3 | |
| **RST (ACTIVE-HIGH)** | R2 | USB3300 pin 9 is active-HIGH; platform uses `Pins` (not `PinsN`) so driving 0 releases the transceiver. Static — does not toggle during operation. |
| DATA0 | V2 | |
| DATA1 | V3 | |
| DATA2 | W1 | |
| DATA3 | W2 | |
| DATA4 | Y1 | |
| DATA5 | AA1 | |
| DATA6 | AB1 | |
| DATA7 | Y2 | |

### ⚠️ ULPI WIRING SPEC — signal integrity is critical (60 MHz source-synchronous bus)

ULPI is a 60 MHz bidirectional source-synchronous bus. With loose / mis-paired
jumper wires the CLK↔data skew is uncontrolled and **per-pin**, so no gateware
clock-phase setting can compensate — the link cannot complete a single ULPI
register write even though everything builds, clocks, and the manual ULPI
driver (`ulpi_force_top.py`) gets a PHY NXT response. This was the wall hit
in the 2026-05 bring-up.

**Rule: each timing-critical signal's twisted-pair partner must be GROUND,
never another active signal.** Pairing CLK with any switching signal injects
jitter into CLK and corrupts the sampling of every line. Do NOT pair by
physical header adjacency — the breakout and i9plus P2 pin orders differ, so
adjacency pairing scrambles the groupings.

**Twisted-pair grouping (Cat5e, ~7 cm, all same length):**

Cable 1 — timing-critical, each signal twisted with its OWN ground:
| Pair | Signal (FPGA pin) | Partner |
|---|---|---|
| 1 | **CLK (T4)** | **GND** ← #1 priority, never share |
| 2 | NXT (U2) | GND ← sampled every cycle |
| 3 | DIR (T3) | GND ← sampled every cycle |
| 4 | STP (U3) | GND |

Cable 2 — data bus (less timing-critical than CLK/NXT/DIR; pair with GND if
you have spare pairs, otherwise data+data is acceptable):
| Pair | Signals |
|---|---|
| 1 | D0 (V2) + GND  *(or D0+D1)* |
| 2 | D2 (W1) + GND  *(or D2+D3)* |
| 3 | D4 (Y1) + GND  *(or D4+D5)* |
| 4 | D6 (AB1) + GND *(or D6+D7)* |

**RST (R2)** is static — a loose single wire is fine.

GND is available on the P2 header power rail. On the 200-pin SODIMM, GND sits
at pin numbers 39, 40, 55, 56, 105, 106, 107, 108 (near the P2 ULPI block) —
use a *separate* GND wire for each critical pair, not a shared daisy-chain.

**The single highest-impact fix: CLK must be twisted with GND alone.**

The proper long-term solution is a custom breakout PCB with length-matched
ULPI routing (CLK and all 11 signals within ~5–10 mm of each other). This is
also required for a production-grade product.

If T3 turns out not to be MRCC/SRCC on this package, swap CLK to T4 or U3
(also Bank 34 corner — likely clock-capable).

### I2S DAC (PCM5102A) → P6 header

| I2S signal | i9plus pin |
|---|---|
| BCK (bit clock) | **U7** |
| LRCK (word clock) | **U6** |
| DIN (serial data) | **U5** |

These three pins are on the **P6** header (dimm pins 46, 48, 50 — Bot row). The DAC stays as a local monitor output. Audio playback comes from gateware FIFOs paced by MCR (Phase 3 work).

### Ethernet — on-board PHYs (no jumper wires)

PHY0 (U5): RGMII on H4/A1/H2/G2/G1/F1/E3/E2/E1/F3/D1/B2/B1/C2/D2
PHY1 (U9): RGMII on L3/M6/H2/G2/G1/R1/N2/N3/P1/P2/N5/M5/M2/N4/P4
(H2/G2/G1 are MDIO/MDC shared.)

### What's free for future expansion

After USB (13 pins on P2) and I2S DAC (3 pins on P6), the rest of the user-IO is **untouched**:

- **P2 remaining**: Y3, Y4, Y6, W4, AA3, AB2, AB3 (7 pins)
- **P3 header**: ~20 pins, Bank 14/15 (V18/V19/V8/V9/W17/W19/Y18/Y19/AA19/AA20/AA21/AB18/AB20/P14/W9/Y9/AA6/R14/AB8 plus rails)
- **P5 header**: ~16 pins, mix of Bank 14/35 (P15/P16/P17/N13/N14/U17/L5/L6/W5/W6/J5/J6/T5/R4/M3/R3/U4)
- **P6 remaining** (after I2S): F4, L4, J4, G4, K3, G3, J2, K2, L1, M1, J1, K1, U1, H3, T6, P5 (16 pins)

That's **~60 free signal pins** for AES3, AES67, MADI, additional I2S channels, GPIO LEDs, debug headers, etc.

### Adding AES3 later (room exists)

AES3 needs very little:
- **AES3 TX**: 1 differential pair (or 1 single-ended pin into an SN65MLVD or RS422 transmitter chip)
- **AES3 RX**: 1 single-ended pin from an RS422/differential receiver

A stereo AES3 link with 2-channel multiplexing fits in 2 IOs (1 TX + 1 RX) plus the receiver/transmitter chips. Any pair from the ~60 free pins above works. The `avb-aes3` codebase had AES3 RX/TX modules in `rtl/_aes3_backup/` — port them back if needed.

## Status

- **Phase 1** ✅ — Scaffolding, USB UAC2 stack adapted from
  `hansfbaier/adat-usb2-audio-interface@artix7`. ADAT-specific bits parked
  under `gateware/_ref/`.
- **Phase 2a** ✅ — Hardware spec: USB3300 ULPI breakout on P2.
- **Phase 2b** ✅ — `usb_utmi_top.py` builds + enumerates. Device shows up
  as `lsusb 1209:eab1 EventsLight N-Series AVB Switchover`, HS (480 Mbit/s),
  Linux binds snd-usb-audio, `pcm0p` + `pcm0c` present. Robust 8 ch /
  S32_LE / 48 kHz playback. The proven-working `top.bit` is archived
  under LFS at `bitstreams/7895049_*_usb-hs-uac2-working.bit`.
- **Phase 3.1** ✅ — USB block emits as a clean Verilog leaf
  (`rtl/generated/usb_avb_subsystem.v`) that LiteX/Migen can instance.
- **Phase 3.2** ✅ — Block integrated into the avb-aes3 LiteX SoC; both USB
  and gigabit AVB coexist on one bitstream
  (see `github.com/Nickster90s/avb-aes3`).
- **Phase 3.3** 🟡 — DAW → USB → AAF on-wire bridge. First gateware
  AsyncFIFO attempt regressed USB HS chirp (any new cd_usb consumer
  breaks the marginal ULPI timing). Reverted; next attempt will move the
  FIFO inside the wrapper so it stays inside the proven placement
  domain. See `avb-aes3` README §8 for the post-mortem.

## Layout

| Path | Contents |
|------|----------|
| `gateware/colorlight_i9plus_platform.py` | Amaranth platform — pin map for i9plus + ULPI on P2 |
| `gateware/colorlight_i9plus_car.py` | Clock-and-reset generator (clk25 → sync/fast PLL; usb ← ULPI CLK) |
| `gateware/usb_only_top.py` | Phase 2b USB-only top-level (loopback) |
| `gateware/usb_descriptors.py` | UAC2 USB descriptors |
| `gateware/requesthandlers.py` | UAC2 control endpoint handlers |
| `gateware/usb_stream_to_channels.py`, `channels_to_usb_stream.py` | USB ↔ per-channel sample stream |
| `gateware/_ref/` | Reference files from the upstream project (kept for porting) |
| `gateware/_bench/` | Testbenches and GTKWave configs |
| `rtl/` | Custom Verilog (will host AVB modules in Phase 3) |
| `firmware/` | RISC-V firmware (Phase 3) |
| `docs/` | Design notes |

## Building

```bash
# One-time: create venv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r gateware/requirements.txt    # pins amaranth==0.4.5

# Phase 2b build (will invoke yosys + nextpnr-xilinx)
cd gateware
python3 usb_only_top.py --action build
```

## Why a different HDL framework than avb-aes3?

`avb-aes3/` uses LiteX/Migen. `avb-usb-host/` uses Amaranth + LUNA
because that's where mature UAC2 implementations live. The two meet
at the Verilog top-level: `usb_utmi_top.py` emits
`rtl/generated/usb_avb_subsystem.v` which LiteX instances as a
blackbox in `avb-aes3/avb_soc.py`.

---

## Amaranth + LUNA on openXC7 — what we learned (community notes)

This combo (**Amaranth** + **LUNA** + **yosys** + **nextpnr-xilinx**
+ **prjxray-db**, no Vivado anywhere) was not a publicly solved
configuration when we started. Most "fully supported" LUNA boards
ship with a USB3320 PHY where the FPGA drives the ULPI clock
(`clk_dir="o"`) AND a closed toolchain (Vivado or Quartus) that
auto-inserts IDDR / IDELAY on the input flops. We had the OPPOSITE:
a USB3300 (PHY-driven clock, `clk_dir="i"`) on an XC7A50T with
yosys+nextpnr-xilinx, which doesn't auto-insert IDDR / IDELAY.

It works now. Here's the entire set of moving parts that had to be
right — pulled out so you don't have to re-discover them:

### 1. The four `XilinxPlatform` overrides for the open toolchain

Stock Amaranth's `XilinxPlatform` assumes Vivado is in PATH and
fails in several different ways without it. See
`gateware/colorlight_i9plus_platform.py:79..120`:

```python
class ColorlightI9PlusPlatform(XilinxPlatform):
    device      = "xc7a50t"
    package     = "fgg484"
    speed       = "1"
    default_clk = "clk25"

    def __init__(self):
        # 1. Use the open toolchain (yosys + nextpnr-xilinx + fasm).
        #    Amaranth's default toolchain="Vivado" errors with
        #    "tool vivado not in PATH".
        super().__init__(toolchain="Xray")

    @property
    def _xray_device(self):
        # 2. The prjxray chipdb is per-PACKAGE, not per-family.
        #    Amaranth's default returns "xc7a50t" → looks for
        #    xc7a50t.bin (missing). We need "xc7a50tfgg484.bin".
        return f"{self.device}{self.package}"

    @property
    def vendor_toolchain(self):
        # 3. Amaranth gates IOSTANDARD attr handling behind
        #    "is this Vivado/ISE?". The generic Platform.get_input_output
        #    rejects Attrs(IOSTANDARD=...) on bidirectional pins
        #    like the ULPI data lines. We DO want IOSTANDARD —
        #    it ends up in the XDC that nextpnr-xilinx reads — so
        #    override this to claim vendor-tool status.
        return True
```

And in the CAR (`colorlight_i9plus_car.py:74` + `:124`):

```python
m.submodules.mainpll = Instance("PLLE2_ADV",
    # 4. nextpnr-xilinx's fasm backend supports "INTERNAL" only.
    #    Vivado's default "ZHOLD" crashes legalisation at fasm.cc:1572.
    p_COMPENSATION = "INTERNAL",
    ...
)
```

Without all four, the build either won't elaborate, won't accept
ULPI pin attributes, won't find the chipdb, or won't post-route legalise.

### 2. PLLE2_ADV VCO must be ≥ 800 MHz on Artix-7 −1 parts (UG472)

Easy to violate when scaling down from a low CLKIN like 25 MHz.

```python
# From clk25 to 60 MHz sync + 240 MHz fast:
p_CLKFBOUT_MULT  = 48,      # VCO = 25 × 48 = 1200 MHz  ✓
p_CLKOUT0_DIVIDE = 20,      # → 60 MHz
p_CLKOUT1_DIVIDE = 5,       # → 240 MHz
```

We had `MULT=24 → VCO=600 MHz` (out of spec, PLL output silently
broken) for two days before catching it. **Always compute
`VCO = CLKIN × CLKFBOUT_MULT / DIVCLK_DIVIDE` and confirm
`800 ≤ VCO ≤ 1600 MHz`.**

### 3. USB3300 RESET is **active HIGH**

Per the USB3300 datasheet pin 9. Use `Pins(...)` not `PinsN(...)` in
the Amaranth resource. `PinsN` inverts → with `ResetSignal("usb")=0`
the physical pin goes HIGH → transceiver held in reset forever:

- 60 MHz CLKOUT still appears (the PHY's internal PLL keeps running, misleading)
- Manual ULPI TXCMD gets a NXT response (the digital part still ticks)
- **but the device never enumerates** (D+ pull-up is part of the analog
  transceiver, which is held in reset)

```python
Subsignal("rst", Pins(_dimm(_ULPI_DIMM["rst"]), dir="o"))    # ✅
# Subsignal("rst", PinsN(_dimm(_ULPI_DIMM["rst"]), dir="o")) # ❌ holds the PHY in reset
```

### 4. The ULPI wrapper choice — `ultraembedded` over LUNA's own

LUNA ships its own ULPI translator. We tried it; it never completes
a register write on this toolchain. The trail eventually pointed at
the fact that **LUNA's translator assumes the toolchain auto-inserts
IDDR / IDELAY on the input flops**, which yosys+nextpnr-xilinx doesn't.

Solution: use `ultraembedded/core_ulpi_wrapper` (Verilog, proven on
Spartan-6 + USB3300, vendored at
`rtl/ulpi_ultraembedded/ulpi_wrapper.v`) and feed its UTMI to
`luna.gateware.usb.usb2.device.USBDevice(bus=utmi)` with
`always_fs=False, data_clock=60e6`. See
`gateware/usb_utmi_top.py:117..156`.

### 5. The wrapper startup reset pulse — **the final missing piece**

LUNA's `ResetSignal("usb")` is never asserted at boot, so the
ultraembedded wrapper never sees its reset go high-then-low and
never starts its register-config sequence. The whole bus sits
quiet — no STP, no NXT, no enumeration — even with everything
above correct. Fix (`usb_utmi_top.py:108..114`):

```python
# Hold wrapper reset HIGH for the first ~128 usb cycles after boot,
# then release. Without this the wrapper never inits.
wrap_rst    = Signal(reset=1)
wrap_rstcnt = Signal(7, reset=0)
with m.If(~wrap_rstcnt.all()):
    m.d.usb += wrap_rstcnt.eq(wrap_rstcnt + 1)
with m.Else():
    m.d.usb += wrap_rst.eq(0)
```

We spent a long time staring at scope traces before realising this.

### 6. `interface.claim` for any custom UAC2 request handler

The `usb2-highspeed-core` fork we depend on routes each control
request to whichever handler asserts `interface.claim`. None set →
the multiplexer falls back to a stall-only handler → every UAC2
class request is stalled → `dmesg "unable to retrieve number of
sample rates (clock 1)"` → no PCM substreams visible. Fix
(`gateware/requesthandlers.py`, idiom from
`luna/gateware/usb/devices/acm.py` + `request/standard.py`):

```python
# In the STANDARD SET_INTERFACE branch:
m.d.comb += interface.claim.eq(1)

# In the all-CLASS branch:
m.d.comb += interface.claim.eq(1)

# In the all-VENDOR branch:
m.d.comb += interface.claim.eq(1)
```

This is mandatory for ANY custom handler on this fork. Without it,
enumeration succeeds (it's all standard requests) but `pcm0p`/`pcm0c`
never appear because the host can't query rates.

### 7. ULPI bus physical signal integrity (re-stated, it's that important)

See the big block above on twisted-pair wiring. Twenty gateware
variations all failed identically until the user re-wired CLK with
its **own** ground. No clock-phase fiddling rescues a noisy CLK.

### 8. The working build recipe

```sh
export CHIPDB=/home/lisp/FPGA/demo-projects/chipdb
export PRJXRAY_DB_DIR=/home/lisp/openxc7/openxc7/opt/nextpnr-xilinx/external/prjxray-db
cd gateware
USB_PHASE=0 SAMPLE_EDGE=off python3 usb_utmi_top.py
```

- `USB_PHASE=0`: PLL with no shift. Validated by the
  `ulpi_phasescan_top.py` eye-scan that reads PHY register 0x00 back
  correctly at this phase.
- `SAMPLE_EDGE=off`: feed the wrapper raw ULPI pins, no extra
  resampling layer. Earlier `clk_edge="neg"` re-sampler mis-timed
  the wrapper.

The output `gateware/build/top.bit` is the same one archived under
`bitstreams/`.

---

## i9plus platform template — reusable starting point

Two files form a self-contained Amaranth + openXC7 starting point
for the Colorlight i9plus v6.1 (XC7A50T-FGG484):

- **`gateware/colorlight_i9plus_platform.py`** — `XilinxPlatform`
  subclass with all four openXC7 overrides (above), the 200-pin
  SODIMM pin dictionary, `clk25`, `user_led`, and a ULPI resource
  template you can adapt or delete.

- **`gateware/colorlight_i9plus_car.py`** — `ColorlightI9PlusCAR`
  Elaboratable: main PLL (`clk25 → 60 MHz sync + 240 MHz fast`,
  VCO = 1200 MHz), USB CAR with three modes (`usb_pll=True`
  phase-shifted PLL, `usb_pll=False` raw BUFG, `usb_invert=True`
  inverted-clock falling-edge sampling). Drop the USB bits if you
  don't need them — the main PLL is reusable as-is for any i9plus
  project on openXC7.

If you're starting a new Amaranth + openXC7 project on the i9plus,
fork these two files; they save you from re-discovering every
gotcha in section 1.

---

## Related repos

- **avb-aes3** — https://github.com/Nickster90s/avb-aes3
  The LiteX SoC that instances this project's USB block + handles
  AVB-Milan / gPTP / AVDECC / AAF / CRF / MCR. Its `TOOLCHAIN.md`
  records the exact openXC7 + LiteX versions used.
- **openXC7** — https://github.com/openXC7/toolchain-installer
  The yosys + nextpnr-xilinx + prjxray-db distribution.
- **LUNA** — https://github.com/greatscottgadgets/luna
  Amaranth USB device framework.
- **ultraembedded ULPI wrapper** —
  https://github.com/ultraembedded/core_ulpi_wrapper
  The Verilog ULPI↔UTMI bridge we use instead of LUNA's own.

---

## License

(TBD — pick one before this gets serious community attention.)
