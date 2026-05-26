# Phase 3 — USB UAC2 → AVB-Milan Bridge (design)

Status: design, 2026-05-26. Prereqs DONE:
- avb-usb-host: USB HS UAC2 sink works (tag `usb-hs-uac2-working`).
  Robust playback; 8ch S32_LE/24-bit @ 48 kHz on `out2ch.channel_stream_out`.
- avb-aes3: full AVB-Milan stack (AAF talker, CRF/MCR, gPTP, SRP,
  AVDECC) running on LiteX + VexRiscv + LiteEth, openXC7 toolchain.

## Goal

A standalone box: **DAW → USB → FPGA → AVB-Milan**, the FPGA's AVB
output locked to the network media clock (CRF) from the mixer. The
USB side is an async UAC2 sink; rate adaptation is handled by USB
async feedback driven by the recovered media clock (no SRC needed).

## Data path

```
host ─USB EP1 OUT─► USBStreamToChannels ─► AsyncFIFO ─► AAF talker ─► LiteEth ─► AVB
                    (cd_usb, 60 MHz)        (CDC)        (media-clock dom)
                                                              ▲ rate
host ◄USB EP1 IN──  feedback value ◄──────────────────  MCR (from CRF listener)
```

- EP1 OUT: host delivers audio (already working).
- AsyncFIFO: crosses cd_usb → media-clock domain. Depth sized for
  USB 125 µs microframe jitter + CRF servo wander (~256–1024 samples).
- AAF talker (avb-aes3): packetizes channel samples into AVTP-AAF,
  stamps presentation_time, sends at the CRF-locked rate.
- MCR (avb-aes3): recovers media clock from incoming CRF stream; its
  rate (samples consumed/sec) drives the USB EP1 IN feedback so the
  host's OUT delivery tracks the AVB clock. This closes the loop and
  removes the need for a sample-rate converter.

## Decision 1 — framework merge (do this first)

avb-usb-host is Amaranth/LUNA; avb-aes3 is LiteX/Migen. They must meet
at one Verilog top. **Recommendation: host the USB block inside the
LiteX SoC** (avb-aes3 is the bigger codebase — CPU, firmware, LiteEth,
AVB IP — far easier to add a USB leaf than to re-host all of that
under Amaranth).

Mechanism:
1. From avb-usb-host, emit the USB subsystem as a standalone Verilog
   module with a clean port list: ULPI pins in/out, plus a streaming
   interface (channel data + valid/ready + channel index) and the
   feedback input (rate value). Amaranth can emit Verilog via
   `amaranth.back.verilog.convert(elaboratable, ports=[...])`.
   - Wrap `USBLoopbackUTMI` minus the loopback: expose
     `out2ch.channel_stream_out` and `ep1_in` (feedback) as top ports.
   - Include the ultraembedded `ulpi_wrapper.v` as a sub-file.
2. In avb-aes3 (Migen), instantiate that Verilog with `Instance(...)`,
   the same way `ulpi_wrapper.v` is instantiated in usb_utmi_top.
3. Clock domains: the USB block carries its own `cd_usb` (from the
   ULPI clock). Add it to the avb-aes3 CRG as an extra domain. Keep
   the winning config: phase-0 PLL OR raw BUFG, raw ULPI input pins,
   wrapper startup reset pulse, `interface.claim` in handlers.
4. Pins: ULPI on P2 (T4 clk, etc. — see avb-usb-host README); the
   AVB Ethernet PHY pins come from avb-aes3's platform. Confirm no
   bank/pin conflicts on the i9plus.

## Decision 2 — async sample bridge

- `stream.AsyncFIFO` (LiteX) write=cd_usb, read=media-clock domain.
- USB delivers 8 × 24-bit samples/frame; pack to the AAF talker's
  per-channel sample width.
- Underflow/overflow handling: on USB underrun, AAF sends last/zero
  sample (don't stall the AVB stream — it must stay isochronous).

## Decision 3 — USB async feedback (EP1 IN)

- LUNA `USBIsochronousInMemoryEndpoint` (ep1_in, 4 bytes) already
  present. Feed it the 10.14 (HS) format feedback value =
  samples-per-microframe in 16.16-ish fixed point, derived from the
  MCR rate (media-clock samples/sec ÷ 8000 microframes/sec).
- This makes the host speed up / slow down delivery to match the AVB
  media clock. Without it, the FIFO drifts and eventually over/underflows.

## Milestones / tasks

P3.1  Emit USB subsystem as Verilog module w/ stream + feedback ports.
P3.2  Add cd_usb domain + ULPI pins to avb-aes3 CRG/platform; build a
      do-nothing instance (just enumerate inside the AVB bitstream).
P3.3  AsyncFIFO USB→media-clock; wire USB channels → AAF talker input.
P3.4  Drive EP1 IN feedback from MCR rate; verify host rate tracks.
P3.5  End-to-end: DAW plays → Hive/AVB listener receives the stream,
      locked to mixer CRF. Bit-accuracy + no over/underrun over time.
P3.6  AVDECC: expose the USB-fed talker as a Milan stream (descriptors).

## Risks / notes

- USB capture (device→host) isoc IN currently flow-control-broken in
  the loopback placeholder — not needed for playback→AVB; revisit only
  if AVB→USB return audio is wanted.
- Keep the eye-scan tool (`ulpi_phasescan_top.py`) handy: if the merged
  build shifts ULPI routing/timing, re-confirm phase 0 is clean.
- pin/bank conflicts: ULPI on P2 (bank 34) vs LiteEth RGMII (bank 14/35
  per avb-aes3) — verify in the merged platform.
