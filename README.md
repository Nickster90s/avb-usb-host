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

## Status

**Phase 1 — scaffolding (in progress)**

- Project structure laid out: `gateware/` (Amaranth + LUNA), `firmware/`,
  `rtl/` (any custom Verilog), `docs/`.
- USB UAC2 gateware copied from
  [adat-usb2-audio-interface@artix7](https://github.com/hansfbaier/adat-usb2-audio-interface/tree/artix7)
  as the starting point. ADAT-specific bits parked under `gateware/_ref/`.
- Colorlight i9plus Amaranth platform stub (`colorlight_i9plus_platform.py`)
  with placeholder ULPI pinout — needs real pins once the breakout PCB is
  designed.

**Phase 2 — USB enumeration (not started)**

- Hardware: external USB3340 (or USB3300) ULPI breakout wired to i9plus
  SODIMM pins. ULPI_CLK must land on a clock-capable input.
- Goal: device enumerates as a UAC2 sound card on the host.

**Phase 3 — AVB integration (not started)**

- Port LiteEth + AVB stack from `../avb-aes3/` (different HDL framework —
  Migen — so the integration is at the Verilog top-level).
- Async SRC between USB clock and MCR-locked media clock.
- AAF talker emits at media-clock rate using gPTP `presentation_time`,
  not a physical media clock.

## Layout

| Path | Contents |
|------|----------|
| `gateware/` | LUNA / Amaranth USB stack: descriptors, UAC2 endpoints, channel routing |
| `gateware/colorlight_i9plus_platform.py` | i9plus + ULPI placeholder pinout |
| `gateware/_ref/` | Reference files (ADAT board / Cyclone platform / original top-level) kept for porting reference |
| `gateware/_bench/` | Testbenches and GTKWave configs |
| `rtl/` | Custom Verilog (will host AVB modules in Phase 3) |
| `firmware/` | RISC-V firmware (Phase 3) |
| `docs/` | Design notes |

## Dependencies (TODO — Phase 1.5)

The Amaranth / LUNA stack is NOT yet installed system-wide. To make
gateware build, install:

```bash
pip install \
  git+https://github.com/amaranth-community-unofficial/python-usb-descriptors.git \
  git+https://github.com/amaranth-community-unofficial/amaranth-boards.git \
  git+https://github.com/amaranth-community-unofficial/amlib.git \
  git+https://github.com/amaranth-community-unofficial/usb2-highspeed-core.git
```

(See `gateware/requirements.txt` for the reference set.)

## Why a different HDL framework than avb-aes3?

`avb-aes3/` uses LiteX/Migen. `avb-usb-host/` uses Amaranth + LUNA
because that's where UAC2 implementations live. We'll bridge the
two at the Verilog top-level in Phase 3.
