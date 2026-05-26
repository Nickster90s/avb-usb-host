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

- **Phase 1** ✅ — Project scaffolding, USB UAC2 stack copied from `hansfbaier/adat-usb2-audio-interface@artix7`, ADAT-specific bits parked under `gateware/_ref/`.
- **Phase 2a** ✅ — Hardware spec locked: USB3300 ULPI breakout on P2.
- **Phase 2b** 🟡 in progress — `gateware/usb_only_top.py` elaborates cleanly (Fragment.get smoke test passes). Next: actual yosys+nextpnr-xilinx build, real-hardware enumeration test.
- **Phase 3** ⬜ — port LiteEth + AVB stack from `../avb-aes3/`, async SRC, AAF talker with presentation_time, AVDECC Milan talker descriptors.

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
because that's where mature UAC2 implementations live. The two will
meet at the Verilog top-level when we integrate the AVB stack in
Phase 3.
