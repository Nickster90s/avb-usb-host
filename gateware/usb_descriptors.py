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


# ---------------------------------------------------------------------------
# HID (KVM): one interface, one interrupt IN endpoint, keyboard + mouse.
#
# WHY HERE AND NOT IN usb_protocol: there is no hid.py in
# usb_protocol/emitters/descriptors/ -- it ships standard, cdc, uac1/2/3,
# midi1/2 and microsoft10. The standard interface and endpoint descriptors come
# from InterfaceDescriptorEmitter/EndpointDescriptorEmitter as usual; the HID
# class descriptor (type 0x21) and the report descriptor (type 0x22) are raw
# bytes. add_subordinate_descriptor() takes bytes-like objects and keys its type
# count off byte 1, so a raw 0x21 does not disturb the endpoint count.
#
# REPORT IDs, NOT BOOT PROTOCOL, AND THAT IS A TRADE WORTH KNOWING. Report IDs
# let keyboard and mouse share one interface and one endpoint, which is what
# "one HID device per PHY" asks for. The cost is that bInterfaceSubClass must be
# 0, so this is NOT a boot-protocol device: a BIOS or UEFI setup screen that
# only speaks boot protocol will not see it. If that is needed later it is a
# SECOND interface with subclass 1, protocol 1, no report IDs and a fixed 8-byte
# keyboard report -- not a change to this descriptor.
HID_REPORT_DESCRIPTOR = bytes([
    # ---- Report ID 1: keyboard, boot-protocol-compatible layout -----------
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,        #   Usage Minimum (LeftControl)
    0x29, 0xE7,        #   Usage Maximum (Right GUI)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data,Var,Abs)      -- modifier byte
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x01,        #   Input (Cst,Ary,Abs)       -- reserved byte
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x65,        #   Logical Maximum (101)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0x00,        #   Usage Minimum (0)
    0x29, 0x65,        #   Usage Maximum (101)
    0x81, 0x00,        #   Input (Data,Ary,Abs)      -- 6 key codes
    0xC0,              # End Collection
    # ---- Report ID 2: mouse, 3 buttons + X/Y + wheel ----------------------
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (1)
    0x29, 0x03,        #     Usage Maximum (3)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x95, 0x03,        #     Report Count (3)
    0x75, 0x01,        #     Report Size (1)
    0x81, 0x02,        #     Input (Data,Var,Abs)
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x05,        #     Report Size (5)
    0x81, 0x01,        #     Input (Cst,Ary,Abs)     -- padding
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x09, 0x38,        #     Usage (Wheel)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x03,        #     Report Count (3)
    0x81, 0x06,        #     Input (Data,Var,Rel)
    # PAD THE MOUSE TO THE SAME LENGTH AS THE KEYBOARD. Both reports are now
    # 1 ID byte + 8 data bytes = 9. That is not cosmetic: the endpoint is a
    # USBSignalInEndpoint, which relays a FIXED-width signal on every poll, so
    # two different report lengths cannot share it. Padding here is legal and
    # declared; sending 9 bytes for a report the descriptor calls 5 would not
    # be.
    0x95, 0x04,        #     Report Count (4)
    0x75, 0x08,        #     Report Size (8)
    0x81, 0x01,        #     Input (Cst,Ary,Abs)  -- padding to a fixed 8 bytes
    0xC0,              #   End Collection
    0xC0,              # End Collection
])

# EVERY report is this long, including its ID byte: 1 + 8. Keyboard is
# ID + modifiers + reserved + 6 keycodes; the mouse is padded to match.
HID_REPORT_BYTES = 9
# EP 0x01 iso OUT (audio), 0x81 iso IN (feedback), 0x82 iso IN (capture stub)
# are taken. 0x83 is the first free IN address.
HID_ENDPOINT_NUMBER   = 3
HID_DESCRIPTOR_TYPE   = 0x21
HID_REPORT_DESC_TYPE  = 0x22

class USBDescriptors():
    # Referenced by requesthandlers.py (clock-frequency control target).
    CLOCK_ID = 1

    # 24 valid bits inside a 4-byte (S32_LE) subslot, MSB-aligned = Milan
    # AAF 32-bit INT. Bump BIT_RESOLUTION to 32 if/when the FIFO carries
    # full 32-bit samples.
    SUBSLOT_SIZE   = 4
    BIT_RESOLUTION = 32      # true 32-bit subslot (48ch/48K/32B)
    SAMPLE_RATE    = 48000   # used to size wMaxPacketSize / high-bandwidth iso

    # OUT ISO packet sizing -> (endpoint_base, descriptor_wMaxPacketSize).
    # bytes/uframe = (n*SUBSLOT_SIZE) * (fs/8000 + 1)  (+1 frame = async-feedback
    # overshoot). Fits one 1024-byte transaction -> single. Otherwise HIGH-
    # BANDWIDTH iso (2-3 transactions/uframe via the mult bits 11-12 of
    # wMaxPacketSize). The per-transaction base MUST be a whole multiple of the
    # sample-frame size (n*SUBSLOT_SIZE) so every transaction starts on a frame
    # boundary (the decoder keys framing off the per-transaction `first`). For
    # 48ch@48K@32B: frame=192, total=1344 -> base 768 x 2 transactions = 1536.
    def _out_packet(self, n: int):
        # Was een @classmethod op cls.SAMPLE_RATE. Nu een instantiemethode,
        # zodat een per-build rate (96k) de maat bepaalt zonder de klasse-
        # attribuut globaal te muteren -- die mutatie zou elke andere instantie
        # in hetzelfde proces meenemen.
        frame = n * self.SUBSLOT_SIZE
        total = frame * (self.SAMPLE_RATE // 8000 + 1)
        if total <= 1024:
            return total, total
        for k in (2, 3):
            base = ((512 // frame) + 1) * frame   # smallest frame-multiple > 512
            while base <= 1024:
                if k * base >= total:
                    return base, base | ((k - 1) << 11)
                base += frame
        raise ValueError(f"{total} bytes/uframe exceeds 3x1024 high-bandwidth iso")

    # UAC2 bmChannelConfig spatial masks. FL+FR for the stereo
    # compatibility alt-setting; 0 (discrete / "raw" channels) for the
    # N-ch alt-setting — our N channels are arbitrary AVB streams, not a
    # surround-speaker layout, so declaring no spatial location is the
    # honest choice and lets the host treat them as discrete.
    CHCFG_STEREO   = 0x00000003   # Front-Left | Front-Right
    CHCFG_DISCRETE = 0x00000000

    def __init__(self, *, product_name="N-Series AVB Switchover Main",
                 serial="0", product_id=0xEAB1, sample_rate=None, **_ignored):
        # **_ignored keeps the historical call sites working
        # (USBDescriptors(use_ila=..., ila_max_packet_size=...)); this
        # device has no ILA. product_name/serial/product_id let a 2nd
        # instance enumerate as a distinct "Backup" interface.
        #
        # A SEPARATE PID for the 2nd port is the upstream pattern this
        # project descends from -- adat-usb2-audio-interface's descriptor
        # does `idProduct = 0xADA1 if "USB1" in product_id else 0xADA2`.
        # Two devices sharing one PID force every udev rule, ALSA config and
        # DAW device list to disambiguate by serial string instead.
        # Overschrijft het klasse-attribuut alleen voor DEZE instantie; bij
        # None blijft de 48000-default gelden.
        if sample_rate is not None:
            self.SAMPLE_RATE = sample_rate
        self.product_name = product_name
        self.serial       = serial
        self.product_id   = product_id

    # ---- public entry point (unchanged signature) ------------------------
    @staticmethod
    def playback_only() -> bool:
        """Whether the capture (IN) streaming interface is emitted.

        Capture is DROPPED by default and the reason is measured: macOS opens
        the device full-duplex and runs capture alongside playback on one IO
        work loop, and EP 0x82 delivers no real audio yet, so it starves the
        loop -> Audio IO Overload -> silence at ~2 s. Linux never hit it because
        aplay opens playback-only. HW-VERIFIED 2026-06-05.
        """
        import os
        return os.environ.get("PLAYBACK_ONLY", "1") == "1"

    @classmethod
    def n_audio_interfaces(cls) -> int:
        """How many interfaces the audio function occupies, starting at 0.

        THE SINGLE PLACE THIS IS DECIDED, because it is not a constant: it
        follows PLAYBACK_ONLY. 2 in the shipping device (IF0 AudioControl, IF1
        playback) and 3 with PLAYBACK_ONLY=0, which inserts IF2 capture. The
        number appears in three places that must agree -- the HID interface's
        own bInterfaceNumber, the Interface Association's bInterfaceCount, and
        UAC2RequestHandlers' claim boundary -- so all three read it from here.
        """
        return 2 if cls.playback_only() else 3

    @staticmethod
    def hid_enabled() -> bool:
        import os
        return os.environ.get("KVM_HID", "1") == "1"

    @classmethod
    def hid_interface_number(cls) -> int:
        """The HID interface's number: the first one after the audio function."""
        return cls.n_audio_interfaces()

    def create_usb2_descriptors(self, no_channels: int, max_packet_size: int):
        # Capture (IN) streaming interface is DROPPED by default (playback-only
        # device). ROOT CAUSE of the macOS "~2 s then silence" failure: macOS
        # opens the device FULL-DUPLEX and runs the capture stream alongside
        # playback on one IO work loop. Our capture endpoint (0x82) delivers no
        # real audio yet (it's a stub for the future AVB->USB return path), so
        # it starves the shared loop -> Audio IO Overload -> stop at ~2 s. Linux
        # (aplay) opens playback-only and never hit this, which is why it always
        # worked. HW-VERIFIED 2026-06-05: playback-only = clean continuous audio
        # on BOTH macOS and Linux. Re-enable capture with PLAYBACK_ONLY=0 once
        # EP 0x82 carries a real stream.
        playback_only = self.playback_only()
        descriptors = DeviceDescriptorCollection()

        with descriptors.DeviceDescriptor() as d:
            d.bcdUSB             = 2.00
            d.bDeviceClass       = 0xEF      # Misc (Interface Association)
            d.bDeviceSubclass    = 0x02
            d.bDeviceProtocol    = 0x01
            d.idVendor           = 0x1209    # pid.codes hobbyist VID
            d.idProduct          = self.product_id  # EAB1 Main / EAB2 Backup
            d.iManufacturer      = "EventsLight"
            d.iProduct           = self.product_name
            d.iSerialNumber      = self.serial
            d.bcdDevice          = 0.01
            d.bNumConfigurations = 1

        with descriptors.ConfigurationDescriptor() as cfg:
            # Interface Association: the audio function spans IF0..IF2.
            iad = uac2.InterfaceAssociationDescriptorEmitter()
            iad.bFirstInterface = 0
            iad.bInterfaceCount = 2 if playback_only else 3
            cfg.add_subordinate_descriptor(iad)

            # IF0 standard AudioControl interface
            ac_std = uac2.StandardAudioControlInterfaceDescriptorEmitter()
            ac_std.bInterfaceNumber = 0
            cfg.add_subordinate_descriptor(ac_std)

            # IF0 class-specific AudioControl (clock + terminals)
            cfg.add_subordinate_descriptor(
                self._audio_control_interface(no_channels, playback_only))

            # IF1 playback (OUT); IF2 capture (IN) unless playback-only.
            self._streaming_out(cfg, no_channels, max_packet_size)
            if not playback_only:
                self._streaming_in(cfg, no_channels, max_packet_size)

            # KVM_HID=0 leaves the HID interface out entirely, which is the
            # ONLY way to get a clean "before" netlist from the current source.
            # It exists because the committed .v pair predates this source and
            # cannot be regenerated from it, so "it placed before" is a
            # measurement against a stale artifact -- see KVM_HANDOFF.md. If
            # placement fails, build both ways from the SAME source before
            # blaming HID.
            #
            # HID (KVM), on the first interface after the audio function. It is
            # deliberately OUTSIDE the Interface Association above: the IAD
            # covers the audio function only, and HID is a separate function on
            # the same device.
            if self.hid_enabled():
                self._hid_interface(cfg, descriptors)

        return descriptors

    # ---- HID: keyboard + mouse on one interrupt IN endpoint ---------------
    def _hid_interface(self, cfg, descriptors):
        hid_if = self.hid_interface_number()

        std = standard.InterfaceDescriptorEmitter()
        std.bInterfaceNumber   = hid_if
        std.bAlternateSetting  = 0
        std.bInterfaceClass    = 0x03      # HID
        std.bInterfaceSubclass = 0x00      # not boot protocol -- see the
        std.bInterfaceProtocol = 0x00      # note at HID_REPORT_DESCRIPTOR
        # An index, NOT a string: this emitter is built standalone and added
        # with add_subordinate_descriptor(), so InterfaceDescriptorEmitter's
        # _pre_emit never runs ensure_string_field_is_index() (it only does that
        # when it has a collection). Register the string here instead.
        std.iInterface         = descriptors.ensure_string_field_is_index(
                                     "KVM Control")

        # HID class descriptor, raw: usb_protocol has no emitter for type 0x21.
        rl = len(HID_REPORT_DESCRIPTOR)
        std.add_subordinate_descriptor(bytes([
            9,                          # bLength
            HID_DESCRIPTOR_TYPE,        # bDescriptorType = 0x21
            0x11, 0x01,                 # bcdHID 1.11
            0x00,                       # bCountryCode = not localised
            0x01,                       # bNumDescriptors
            HID_REPORT_DESC_TYPE,       # bDescriptorType = 0x22
            rl & 0xFF, (rl >> 8) & 0xFF # wDescriptorLength
        ]))

        with std.EndpointDescriptor() as ep:
            ep.bEndpointAddress = USBDirection.IN.to_endpoint_address(
                                      HID_ENDPOINT_NUMBER)          # 0x83
            ep.bmAttributes     = 0x03                              # interrupt
            ep.wMaxPacketSize   = 16                                # >= 9
            # HIGH SPEED: bInterval is 2^(n-1) microframes, NOT milliseconds.
            # 4 -> 8 uframes -> 1 ms, which is what a KVM wants. A value copied
            # from a full-speed example (where it IS milliseconds) would ask for
            # 2^(n-1) uframes and be wrong by orders of magnitude.
            ep.bInterval        = 4

        cfg.add_subordinate_descriptor(std)

        # Register the report descriptor so GET_DESCRIPTOR(0x22) serves it.
        # LUNA's standard handler ignores the recipient and wIndex, so this
        # needs no handler of its own.
        descriptors.add_descriptor(HID_REPORT_DESCRIPTOR, index=0,
                                   descriptor_type=HID_REPORT_DESC_TYPE)

    # ---- AudioControl: clock + 4 terminals -------------------------------
    def _audio_control_interface(self, n: int, playback_only: bool = False):
        ac = uac2.ClassSpecificAudioControlInterfaceDescriptorEmitter()

        clk = uac2.ClockSourceDescriptorEmitter()
        clk.bClockID     = self.CLOCK_ID
        clk.bmAttributes = uac2.ClockAttributes.INTERNAL_FIXED_CLOCK
        # bmControls bitmap: bits[1:0]=freq control, bits[3:2]=validity control.
        # Advertise BOTH as host-readable: freq read-only (01) + clock-valid
        # read-only (01<<2) = 0x05. macOS queries CLOCK_VALID and needs it present
        # + answered (see requesthandlers) or it stops the stream after ~2 s.
        clk.bmControls   = uac2.ClockFrequencyControl.HOST_READ_ONLY | (0b01 << 2)
        ac.add_subordinate_descriptor(clk)

        # Playback: USB-streaming IN terminal → Speaker OUT terminal.
        # FULL channel count here (the fix) so the host exposes an N-ch
        # output, not stereo-only.
        pb_in = uac2.InputTerminalDescriptorEmitter()
        pb_in.bTerminalID     = 2
        pb_in.wTerminalType   = uac2.USBTerminalTypes.USB_STREAMING
        pb_in.bNrChannels     = n
        # Defined channel positions for 2ch (FL+FR); DISCRETE only for >2ch.
        # bmChannelConfig=0 (discrete) made macOS log "starting channel number
        # should not be zero" and fail to fully set up the stream/clock.
        pb_in.bmChannelConfig = self.CHCFG_STEREO if n == 2 else self.CHCFG_DISCRETE
        pb_in.bCSourceID      = self.CLOCK_ID
        ac.add_subordinate_descriptor(pb_in)

        pb_out = uac2.OutputTerminalDescriptorEmitter()
        pb_out.bTerminalID   = 3
        pb_out.wTerminalType = uac2.OutputTerminalTypes.SPEAKER
        pb_out.bSourceID     = 2
        pb_out.bCSourceID    = self.CLOCK_ID
        ac.add_subordinate_descriptor(pb_out)

        # Capture: Microphone IN terminal → USB-streaming OUT terminal.
        # Omitted entirely for a playback-only device (see create_usb2_descriptors).
        if not playback_only:
            cap_in = uac2.InputTerminalDescriptorEmitter()
            cap_in.bTerminalID     = 4
            cap_in.wTerminalType   = uac2.InputTerminalTypes.MICROPHONE
            cap_in.bNrChannels     = n
            cap_in.bmChannelConfig = self.CHCFG_STEREO if n == 2 else self.CHCFG_DISCRETE
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
        # HIGH-BANDWIDTH iso: descriptor wMaxPacketSize encodes the per-transaction
        # base in bits 0..10 and (transactions-1) in bits 11..12. The EP buffer
        # (subsystem USBIsochronous*Endpoint max_packet_size) uses the base only.
        ep.wMaxPacketSize   = self._out_packet(ch)[1]
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
            fb.bInterval        = 4   # HS feedback = once per 1ms frame (spec); 1 is rejected by macOS
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
        # SINGLE operational altset: altset1 = N-ch (8ch), no stereo fallback.
        # The host therefore has exactly one streaming choice — the 8ch bridge.
        self._streaming_altset(cfg, iface=1, terminal_link=2, ep_addr=out_ep,
                               alt=1, ch=n, chcfg=self.CHCFG_DISCRETE,
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
