#!/usr/bin/env python3
#
# USB UAC2 descriptors for the EventsLight N-Series AVB bridge.
# Copyright 2025-2026 Nick (nick.eventslight@gmail.com)
# SPDX-License-Identifier: Apache-2.0
#
# Clean reimplementation against the public `usb_protocol` UAC2 emitter
# API + the USB Audio Class 2.0 spec. Originally bootstrapped from
# hansfbaier/adat-usb2-audio-interface (CERN-OHL-W-2.0), but rewritten
# from the topology up for THIS product: a symmetric N-channel
# (default 8) USB Audio Class 2.0 device that bridges to AVB-Milan.
# No ADAT, no MIDI, no ILA — just the audio function.
#
# Topology (one audio function, 3 interfaces):
#   IF0  AudioControl
#        ClockSource id=1 (internal, host-readable)
#        Playback : INPUT_TERMINAL id=2 (USB streaming, N ch)
#                 → OUTPUT_TERMINAL id=3 (Speaker)        [host → device → AVB]
#        Capture  : INPUT_TERMINAL id=4 (Microphone, N ch)
#                 → OUTPUT_TERMINAL id=5 (USB streaming)  [AVB → device → host]
#   IF1  AudioStreaming OUT (playback): altset0 zero-bw,
#        altset1 stereo (host-default), altset2 N-ch.
#        EP 0x01 iso async (audio) + EP 0x81 iso feedback.
#   IF2  AudioStreaming IN  (capture):  altset0 zero-bw,
#        altset1 stereo, altset2 N-ch.  EP 0x82 iso async (audio).
#
# Key correctness points vs the original ADAT descriptors:
#   * The playback terminals (id=2/id=3) declare the FULL channel count
#     (N), not 2 — the ADAT version pinned the playback input terminal
#     to 2ch, so hosts only ever exposed a stereo "Analog Stereo" output
#     even though an 8ch alt-setting existed. Now an N-ch playback device
#     is exposed (see memory uac2-playback-terminal-8ch-fix).
#   * S32_LE container, 24 valid bits (bSubslotSize=4, bBitResolution=24)
#     — MSB-aligned, bit-compatible with Milan AAF 32-bit INT.
#   * A stereo alt-setting (1) is kept first so Windows' generic UAC2
#     driver picks a sane default; the N-ch alt-setting (2) carries the
#     full bridge.

from usb_protocol.types                  import (USBTransferType,
                                                 USBSynchronizationType,
                                                 USBUsageType, USBDirection)
from usb_protocol.emitters                import DeviceDescriptorCollection
from usb_protocol.emitters.descriptors    import uac2, standard


class USBDescriptors():
    # Referenced by requesthandlers.py (clock-frequency control target).
    CLOCK_ID = 1

    # 24 valid bits inside a 4-byte (S32_LE) subslot, MSB-aligned = Milan
    # AAF 32-bit INT. Bump BIT_RESOLUTION to 32 if/when the FIFO carries
    # full 32-bit samples.
    SUBSLOT_SIZE   = 4
    BIT_RESOLUTION = 24

    # UAC2 bmChannelConfig spatial masks. FL+FR for the stereo
    # compatibility alt-setting; 0 (discrete / "raw" channels) for the
    # N-ch alt-setting — our N channels are arbitrary AVB streams, not a
    # surround-speaker layout, so declaring no spatial location is the
    # honest choice and lets the host treat them as discrete.
    CHCFG_STEREO   = 0x00000003   # Front-Left | Front-Right
    CHCFG_DISCRETE = 0x00000000

    def __init__(self, **_ignored):
        # **_ignored keeps the historical call sites working
        # (USBDescriptors(use_ila=..., ila_max_packet_size=...)); this
        # device has no ILA.
        pass

    # ---- public entry point (unchanged signature) ------------------------
    def create_usb2_descriptors(self, no_channels: int, max_packet_size: int):
        descriptors = DeviceDescriptorCollection()

        with descriptors.DeviceDescriptor() as d:
            d.bcdUSB             = 2.00
            d.bDeviceClass       = 0xEF      # Misc (Interface Association)
            d.bDeviceSubclass    = 0x02
            d.bDeviceProtocol    = 0x01
            d.idVendor           = 0x1209    # pid.codes hobbyist VID
            d.idProduct          = 0xEAB1    # EventsLight AVB bridge
            d.iManufacturer      = "EventsLight"
            d.iProduct           = "N-Series AVB Switchover"
            d.iSerialNumber      = "0"
            d.bcdDevice          = 0.01
            d.bNumConfigurations = 1

        with descriptors.ConfigurationDescriptor() as cfg:
            # Interface Association: the audio function spans IF0..IF2.
            iad = uac2.InterfaceAssociationDescriptorEmitter()
            iad.bFirstInterface = 0
            iad.bInterfaceCount = 3
            cfg.add_subordinate_descriptor(iad)

            # IF0 standard AudioControl interface
            ac_std = uac2.StandardAudioControlInterfaceDescriptorEmitter()
            ac_std.bInterfaceNumber = 0
            cfg.add_subordinate_descriptor(ac_std)

            # IF0 class-specific AudioControl (clock + terminals)
            cfg.add_subordinate_descriptor(
                self._audio_control_interface(no_channels))

            # IF1 playback (OUT) + IF2 capture (IN)
            self._streaming_out(cfg, no_channels, max_packet_size)
            self._streaming_in(cfg, no_channels, max_packet_size)

        return descriptors

    # ---- AudioControl: clock + 4 terminals -------------------------------
    def _audio_control_interface(self, n: int):
        ac = uac2.ClassSpecificAudioControlInterfaceDescriptorEmitter()

        clk = uac2.ClockSourceDescriptorEmitter()
        clk.bClockID     = self.CLOCK_ID
        clk.bmAttributes = uac2.ClockAttributes.INTERNAL_FIXED_CLOCK
        clk.bmControls   = uac2.ClockFrequencyControl.HOST_READ_ONLY
        ac.add_subordinate_descriptor(clk)

        # Playback: USB-streaming IN terminal → Speaker OUT terminal.
        # FULL channel count here (the fix) so the host exposes an N-ch
        # output, not stereo-only.
        pb_in = uac2.InputTerminalDescriptorEmitter()
        pb_in.bTerminalID     = 2
        pb_in.wTerminalType   = uac2.USBTerminalTypes.USB_STREAMING
        pb_in.bNrChannels     = n
        pb_in.bmChannelConfig = self.CHCFG_DISCRETE
        pb_in.bCSourceID      = self.CLOCK_ID
        ac.add_subordinate_descriptor(pb_in)

        pb_out = uac2.OutputTerminalDescriptorEmitter()
        pb_out.bTerminalID   = 3
        pb_out.wTerminalType = uac2.OutputTerminalTypes.SPEAKER
        pb_out.bSourceID     = 2
        pb_out.bCSourceID    = self.CLOCK_ID
        ac.add_subordinate_descriptor(pb_out)

        # Capture: Microphone IN terminal → USB-streaming OUT terminal.
        cap_in = uac2.InputTerminalDescriptorEmitter()
        cap_in.bTerminalID     = 4
        cap_in.wTerminalType   = uac2.InputTerminalTypes.MICROPHONE
        cap_in.bNrChannels     = n
        cap_in.bmChannelConfig = self.CHCFG_DISCRETE
        cap_in.bCSourceID      = self.CLOCK_ID
        ac.add_subordinate_descriptor(cap_in)

        cap_out = uac2.OutputTerminalDescriptorEmitter()
        cap_out.bTerminalID   = 5
        cap_out.wTerminalType = uac2.USBTerminalTypes.USB_STREAMING
        cap_out.bSourceID     = 4
        cap_out.bCSourceID    = self.CLOCK_ID
        ac.add_subordinate_descriptor(cap_out)

        return ac

    # ---- one streaming alt-setting (shared OUT/IN body) ------------------
    def _streaming_altset(self, cfg, *, iface, terminal_link, ep_addr, alt,
                          ch, chcfg, max_packet_size, feedback_ep=None):
        std = uac2.AudioStreamingInterfaceDescriptorEmitter()
        std.bInterfaceNumber  = iface
        std.bAlternateSetting = alt
        std.bNumEndpoints     = 2 if feedback_ep is not None else 1
        cfg.add_subordinate_descriptor(std)

        cs = uac2.ClassSpecificAudioStreamingInterfaceDescriptorEmitter()
        cs.bTerminalLink   = terminal_link
        cs.bFormatType     = uac2.FormatTypes.FORMAT_TYPE_I
        cs.bmFormats       = uac2.TypeIFormats.PCM
        cs.bNrChannels     = ch
        cs.bmChannelConfig = chcfg
        cfg.add_subordinate_descriptor(cs)

        fmt = uac2.TypeIFormatTypeDescriptorEmitter()
        fmt.bSubslotSize   = self.SUBSLOT_SIZE
        fmt.bBitResolution = self.BIT_RESOLUTION
        cfg.add_subordinate_descriptor(fmt)

        ep = standard.EndpointDescriptorEmitter()
        ep.bEndpointAddress = ep_addr
        ep.bmAttributes     = (USBTransferType.ISOCHRONOUS
                               | (USBSynchronizationType.ASYNC << 2)
                               | (USBUsageType.DATA << 4))
        ep.wMaxPacketSize   = max_packet_size
        ep.bInterval        = 1
        cfg.add_subordinate_descriptor(ep)

        cfg.add_subordinate_descriptor(
            uac2.ClassSpecificAudioStreamingIsochronousAudioDataEndpointDescriptorEmitter())

        if feedback_ep is not None:
            fb = standard.EndpointDescriptorEmitter()
            fb.bEndpointAddress = feedback_ep
            fb.bmAttributes     = (USBTransferType.ISOCHRONOUS
                                   | (USBSynchronizationType.NONE << 2)
                                   | (USBUsageType.FEEDBACK << 4))
            fb.wMaxPacketSize   = 4
            fb.bInterval        = 4
            cfg.add_subordinate_descriptor(fb)

    # ---- IF1 playback (host → device) ------------------------------------
    def _streaming_out(self, cfg, n: int, max_packet_size: int):
        # altset0: zero-bandwidth (no endpoints) — required default.
        z = uac2.AudioStreamingInterfaceDescriptorEmitter()
        z.bInterfaceNumber  = 1
        z.bAlternateSetting = 0
        cfg.add_subordinate_descriptor(z)

        out_ep = USBDirection.OUT.to_endpoint_address(1)   # EP 0x01
        fb_ep  = USBDirection.IN.to_endpoint_address(1)    # EP 0x81 feedback
        # altset1 stereo (host default), altset2 N-ch (the bridge).
        self._streaming_altset(cfg, iface=1, terminal_link=2, ep_addr=out_ep,
                               alt=1, ch=2, chcfg=self.CHCFG_STEREO,
                               max_packet_size=max_packet_size, feedback_ep=fb_ep)
        if n > 2:
            self._streaming_altset(cfg, iface=1, terminal_link=2, ep_addr=out_ep,
                                   alt=2, ch=n, chcfg=self.CHCFG_DISCRETE,
                                   max_packet_size=max_packet_size, feedback_ep=fb_ep)

    # ---- IF2 capture (device → host) -------------------------------------
    def _streaming_in(self, cfg, n: int, max_packet_size: int):
        z = uac2.AudioStreamingInterfaceDescriptorEmitter()
        z.bInterfaceNumber  = 2
        z.bAlternateSetting = 0
        cfg.add_subordinate_descriptor(z)

        in_ep = USBDirection.IN.to_endpoint_address(2)     # EP 0x82
        self._streaming_altset(cfg, iface=2, terminal_link=5, ep_addr=in_ep,
                               alt=1, ch=2, chcfg=self.CHCFG_STEREO,
                               max_packet_size=max_packet_size)
        if n > 2:
            self._streaming_altset(cfg, iface=2, terminal_link=5, ep_addr=in_ep,
                                   alt=2, ch=n, chcfg=self.CHCFG_DISCRETE,
                                   max_packet_size=max_packet_size)
