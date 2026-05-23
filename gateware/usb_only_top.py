#!/usr/bin/env python3
#
# Phase 2b: USB-only top-level. Brings up a single UAC2 device on the
# i9plus + USB3300 breakout, with NO ADAT and NO AVB. The audio data
# path is short-circuited: USB OUT → channel splitter → channel
# combiner → USB IN (computer-to-computer loopback through the FPGA).
#
# Once this enumerates on the host and round-trips audio cleanly, we
# integrate with the AVB stack in Phase 3.

from amaranth import Module, Signal, Elaboratable, Shape, Mux

from luna                                 import top_level_cli
from luna.gateware.usb.usb2.device        import USBDevice
from luna.usb2                            import (
    USBIsochronousOutStreamEndpoint,
    USBIsochronousInMemoryEndpoint,
    USBIsochronousInStreamEndpoint,
)
from usb_protocol.types                   import USBRequestType, USBStandardRequests

from usb_descriptors                      import USBDescriptors
from requesthandlers                      import UAC2RequestHandlers
from usb_stream_to_channels               import USBStreamToChannels
from channels_to_usb_stream               import ChannelsToUSBStream


class USBLoopback(Elaboratable):
    """USB UAC2 device with internal OUT→IN loopback.

    Confirms the descriptor + endpoint + channel-pipeline chain is
    functional before we hook real audio data in.
    """

    NUMBER_OF_CHANNELS = 8                    # 8 ch out + 8 ch in
    AUDIO_BITS         = 24
    # Class A 48 kHz × 8 ch × 4 bytes (32-bit slots) × 7 samples worst case
    MAX_PACKET_SIZE    = 224

    def elaborate(self, platform):
        m = Module()

        # ULPI on P2 header — clk is its OWN resource (usbsniffer pattern,
        # see platform.py comment). Request both before the CAR.
        ulpi_clk = platform.request("ulpi_clock", 0)
        ulpi     = platform.request("ulpi", 0)

        # Bring up sync/usb/fast clock domains. usb domain rides the
        # 60 MHz CLK that the USB3300 sources back into the FPGA,
        # phase-shifted through a PLL for proper sampling alignment.
        # Phase can be overridden via USB_PHASE env var for sweeping.
        import os
        usb_phase = float(os.environ.get("USB_PHASE", "-120"))
        m.submodules.car = platform.clock_domain_generator(
            ulpi_clk_pin=ulpi_clk.i, usb_phase=usb_phase)

        # Baseline LUNA setup: handle_clocking=True is the default;
        # we drive cd_usb.clk from ulpi.clk.i in our CAR and LUNA's
        # ULPI controller does the same. In Amaranth those two comb
        # drives of the same signal collapse to one wire — confirmed
        # by inspecting top.v.
        m.submodules.usb = usb = USBDevice(bus=ulpi, handle_clocking=False)

        descriptors      = USBDescriptors(use_ila=False, ila_max_packet_size=512)
        usb_descriptors  = descriptors.create_usb2_descriptors(
                              self.NUMBER_OF_CHANNELS, self.MAX_PACKET_SIZE)

        control_ep = usb.add_control_endpoint()
        control_ep.add_standard_request_handlers(usb_descriptors, blacklist=[
            lambda setup: (setup.type    == USBRequestType.STANDARD)
                        & (setup.request == USBStandardRequests.SET_INTERFACE)
        ])
        control_ep.add_request_handler(UAC2RequestHandlers())

        # Host → FPGA audio (host writes EP1 OUT).
        ep1_out = USBIsochronousOutStreamEndpoint(
            endpoint_number=1, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep1_out)

        # Sample-rate feedback EP (host reads to learn our actual drain rate).
        ep1_in = USBIsochronousInMemoryEndpoint(
            endpoint_number=1, max_packet_size=4)
        usb.add_endpoint(ep1_in)

        # FPGA → host audio (host reads EP2 IN).
        ep2_in = USBIsochronousInStreamEndpoint(
            endpoint_number=2, max_packet_size=self.MAX_PACKET_SIZE)
        usb.add_endpoint(ep2_in)

        # USB OUT byte-stream → per-channel sample stream.
        m.submodules.out2ch = out2ch = USBStreamToChannels(
            max_no_channels=self.NUMBER_OF_CHANNELS)
        m.d.comb += [
            out2ch.usb_stream_in.stream_eq(ep1_out.stream),
        ]

        # Internal loopback: dump OUT's channel stream straight back to IN.
        m.submodules.ch2in = ch2in = ChannelsToUSBStream(
            max_nr_channels=self.NUMBER_OF_CHANNELS)
        m.d.comb += [
            ch2in.channel_stream_in.stream_eq(out2ch.channel_stream_out),
            ep2_in.stream.stream_eq(ch2in.usb_stream_out),
        ]

        # Connect — tell USB host we're ready.
        m.d.comb += usb.connect.eq(1)

        # STP+NXT-both-seen latch. Slow blink → no successful ULPI
        # write completed. Fast blink → PHY accepted at least one
        # write from LUNA = phase shift is in the right window.
        led               = platform.request("user_led", 0)
        counter           = Signal(26)
        stp_seen          = Signal()
        nxt_seen          = Signal()
        m.d.usb          += [
            counter.eq(counter + 1),
            stp_seen.eq(stp_seen | ulpi.stp.o),
            nxt_seen.eq(nxt_seen | ulpi.nxt.i),
        ]
        both = stp_seen & nxt_seen
        m.d.comb += led.o.eq(Mux(both, counter[21], counter[25]))
        return m


if __name__ == "__main__":
    # `top_level_cli` parses --action build/program/etc. and invokes the
    # platform's toolchain. Build with `python3 usb_only_top.py --action build`.
    top_level_cli(USBLoopback)
