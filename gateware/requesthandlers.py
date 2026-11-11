from enum import IntEnum
from amaranth import *
from luna.gateware.usb.usb2.request   import USBRequestHandler
from luna.gateware.stream.generator   import StreamSerializer

from usb_protocol.types                       import USBRequestType, USBRequestRecipient, USBStandardRequests
from usb_protocol.types.descriptors.uac2      import AudioClassSpecificRequestCodes, ClockSourceControlSelectors
from luna.gateware.usb.stream                 import USBInStreamInterface

from usb_descriptors import USBDescriptors

class VendorRequests(IntEnum):
    ILA_STOP_CAPTURE = 0

class UAC2RequestHandlers(USBRequestHandler):
    """ request handlers to implement UAC2 functionality. """
    def __init__(self, n_audio_interfaces: int = 2):
        """n_audio_interfaces: how many interfaces the audio function occupies,
        starting at 0. This handler claims class requests ONLY for those, so a
        second class on a later interface (HID) can be served by its own
        handler. It is 2 in the shipping playback-only device (IF0
        AudioControl + IF1 playback) and 3 with PLAYBACK_ONLY=0, which adds the
        capture interface -- so it is NOT a constant, it follows an environment
        variable, and usb_descriptors.USBDescriptors.n_audio_interfaces() is the
        one place that decides. Passing it in rather than hardcoding is the
        point: the number appears in the descriptors too and the two must agree.
        """
        super().__init__()

        self.n_audio_interfaces = n_audio_interfaces

        self.output_interface_altsetting_nr = Signal(3)
        self.input_interface_altsetting_nr  = Signal(3)
        self.interface_settings_changed     = Signal()
        # RATE-FOLLOW (port van branch usb-dac-96K32B, commit 66cf1df, daar
        # HW-geverifieerd op 48k EN 96k). 0 = 96 kHz, 1 = 48 kHz. Gedreven van
        # BUITEN: bij de DAC-top een build-default, in de bridge een firmware-CSR
        # die de Dante/AVDECC-gekozen rate doorgeeft.
        #
        # LET OP -- NIET HOST-PROGRAMMEERBAAR MAKEN. Een eerdere poging liet de
        # host de rate zetten via SET CUR(SAM_FREQ) en dat BRAK het streamen: de
        # host kon de programmeerbare clock niet engageren en je kreeg stilte.
        # De clock source meldt daarom precies EEN rate, read-only, en de host
        # volgt. Zie 66cf1df.
        self.current_rate_48k               = Signal()

    def elaborate(self, platform):
        m = Module()

        interface         = self.interface
        setup             = self.interface.setup

        m.submodules.transmitter = transmitter = \
            StreamSerializer(data_length=14, domain="usb", stream_type=USBInStreamInterface, max_length_width=14)

        m.d.usb += self.interface_settings_changed.eq(0)

        # Computed BEFORE the If/Elif chain on purpose: an m.d statement inside
        # one breaks it with "Elif without preceding If" (see cur_rate below),
        # and a Signal assigned mid-chain is the same trap in a different coat.
        audio_class_request = Signal()
        # THREE BITS, AND A `!=` RATHER THAN A `<`. The obvious form,
        # `setup.index < self.n_audio_interfaces`, compares all SIXTEEN bits of
        # wIndex against a constant, and yosys builds that as a CARRY4 chain.
        # Measured 2026-11-11: that chain landed on cd_usb's critical path --
        #     request_mux_index -> LUT -> $lt$...v:9596 CARRY4 -> LUT ->
        #     endpoint_mux.tx_mux -> tx_data
        # -- and the usb domain fell to 45.77 MHz against the 60 the ULPI bus
        # needs (the known-good build measures 67.71). The claim used to be a
        # constant 1 under CLASS, so narrowing it by interface number is what
        # introduced a comparator into a path that had none.
        #
        # Only THREE low bits are compared, and against the HID interface
        # number, because that is all the discrimination the device can need:
        # the interfaces that exist are 0..2 (0..3 with PLAYBACK_ONLY=0). A
        # class request aimed at a nonexistent interface is then claimed by
        # audio and STALLed by the Default below -- exactly what happened before
        # this handler learned to discriminate at all, so nothing regresses.
        m.d.comb += audio_class_request.eq(
            ((setup.recipient == USBRequestRecipient.INTERFACE)
             & (setup.index[0:3] != self.n_audio_interfaces))
            | (setup.recipient == USBRequestRecipient.ENDPOINT)
            | (setup.recipient == USBRequestRecipient.DEVICE))

        #
        # Class request handlers.
        #
        with m.If(setup.type == USBRequestType.STANDARD):
            with m.If((setup.recipient == USBRequestRecipient.INTERFACE) &
                      (setup.request == USBStandardRequests.SET_INTERFACE)):

                # Claim this request so the mux routes to us (newer
                # usb2-highspeed-core requires interface.claim; the older
                # adat-usb2 handler this was copied from predates it).
                m.d.comb += interface.claim.eq(1)

                interface_nr   = setup.index
                alt_setting_nr = setup.value

                # TOUCH ONLY THE INTERFACE THAT WAS ADDRESSED.
                #
                # This used to zero BOTH altsetting registers here, before the
                # Switch, and then set one of them inside it. So a SET_INTERFACE
                # to any interface other than 1 or 2 -- IF0, the AudioControl
                # interface, which a host may legitimately address -- cleared
                # both and restored neither. Zeroing was also redundant for the
                # interface being addressed: SET_INTERFACE to altsetting 0 is
                # already `eq(alt_setting_nr)` with alt_setting_nr == 0.
                #
                # It has not been causing silence, and the reason is worth
                # writing down because it is also a warning: NOTHING IN
                # usb_avb_subsystem.py READS THESE SIGNALS. The only reader in
                # the tree is usb_utmi_top.py:309, a bench top; LUNA's
                # isochronous endpoints gate themselves. So the bug was latent
                # and would have surfaced the day someone wired them up --
                # which is exactly when it would have been hardest to find.
                m.d.usb += self.interface_settings_changed.eq(1)

                with m.Switch(interface_nr):
                    with m.Case(1):
                        m.d.usb += self.output_interface_altsetting_nr.eq(alt_setting_nr)
                    with m.Case(2):
                        m.d.usb += self.input_interface_altsetting_nr.eq(alt_setting_nr)

                # Always ACK the data out...
                with m.If(interface.rx_ready_for_response):
                    m.d.comb += interface.handshakes_out.ack.eq(1)

                # ... and accept whatever the request was.
                with m.If(interface.status_requested):
                    m.d.comb += self.send_zlp()

        clock_freq =   (setup.value == Const(ClockSourceControlSelectors.CS_SAM_FREQ_CONTROL << 8, 16)) \
                     & (setup.index == Const(USBDescriptors.CLOCK_ID << 8, 16))

        request_clock_freq     = clock_freq     & setup.is_in_request
        set_clock_freq         = clock_freq     & ~setup.is_in_request

        # macOS/CoreAudio polls the Clock Source's CLOCK_VALID control during
        # streaming and STOPS the stream (~2 s) if that GET is stalled. Linux
        # doesn't poll it, so this only bit macOS. Answer it = 0x01 (valid).
        clock_valid =   (setup.value == Const(ClockSourceControlSelectors.CS_CLOCK_VALID_CONTROL << 8, 16)) \
                      & (setup.index == Const(USBDescriptors.CLOCK_ID << 8, 16))
        request_clock_valid    = clock_valid    & setup.is_in_request

        SRATE_48k   = Const(48000, 32)
        SRATE_96k   = Const(96000, 32)
        ZERO        = Const(0, 32)
        # Combinatorische Mux, en dat is OPZETTELIJK upstream: een m.d-statement
        # hier breekt de If/Elif-keten hieronder ("Elif without preceding If").
        # Ik heb geprobeerd dit te registreren om de 32-bits mux van het
        # kritieke pad in cd_usb te halen, en dat liep op twee fouten uit --
        # eerst m.d.sync, wat in deze module NIET usb is, en daarna de
        # kapotte keten. Beide stonden al in het commentaar hier.
        #
        # En het was een AANNAME: dat deze mux het kritieke pad is, is niet
        # gemeten. nextpnr rapporteert het kritieke pad in de build-log; zoek
        # het daar op voordat je hier iets herschrijft. Wil het alsnog, zet de
        # m.d.usb-toekenning dan VOOR het begin van de If/Elif-keten.
        cur_rate    = Mux(self.current_rate_48k, SRATE_48k, SRATE_96k)

        # NOTE the parentheses: `&` binds tighter than `==` in Python, so
        # `setup.type == USBRequestType.CLASS & audio_class_request` parses as
        # `setup.type == (CLASS & audio_class_request)` and silently compares
        # against nonsense. Written without them first.
        with m.Elif((setup.type == USBRequestType.CLASS) & audio_class_request):
            # CLAIM ONLY THE AUDIO FUNCTION'S CLASS REQUESTS.
            #
            # This used to claim ALL of them, with the comment "this device owns
            # the audio class space; unknown ones fall to the Default stall
            # below" -- true while audio was the only class here, and a wall the
            # moment it is not. Every class request was claimed and anything
            # this Switch did not recognise was STALLed, so a second class on a
            # later interface never saw its own requests at all.
            #
            # THE REQUEST CODE CANNOT TELL THEM APART. HID's GET_REPORT is
            # request 0x01 and so is Audio's CUR; SET_REPORT is 0x09. The
            # discriminator is setup.index -- the interface number for an
            # INTERFACE-recipient request, which is what the HID spec uses for
            # every one of its class requests. Hence audio_class_request above.
            #
            # Endpoint-recipient class requests stay ours: the audio endpoints
            # are the only ones that take them (HID speaks to its interface),
            # and wIndex is an endpoint address there, not an interface number,
            # so the comparison below would be meaningless.
            m.d.comb += interface.claim.eq(1)
            with m.Switch(setup.request):
                with m.Case(AudioClassSpecificRequestCodes.RANGE):
                    m.d.comb += transmitter.stream.attach(self.interface.tx)

                    with m.If(request_clock_freq):
                        m.d.comb += [
                            Cat(transmitter.data).eq(
                                Cat(Const(0x1, 16),              # wNumSubRanges = 1
                                    cur_rate, cur_rate, ZERO)),  # de ene actieve rate
                            transmitter.max_length.eq(setup.length)
                        ]
                    with m.Else():
                        m.d.comb += interface.handshakes_out.stall.eq(1)

                    # ... trigger it to respond when data's requested...
                    with m.If(interface.data_requested):
                        m.d.comb += transmitter.start.eq(1)

                    # ... and ACK our status stage.
                    with m.If(interface.status_requested):
                        m.d.comb += interface.handshakes_out.ack.eq(1)

                with m.Case(AudioClassSpecificRequestCodes.CUR):
                    m.d.comb += transmitter.stream.attach(self.interface.tx)
                    with m.If(request_clock_freq & (setup.length == 4)):
                        m.d.comb += [
                            # GET CUR(SAM_FREQ) -> de nu geselecteerde rate.
                            Cat(transmitter.data[0:4]).eq(cur_rate),
                            transmitter.max_length.eq(4)
                        ]
                    with m.Elif(request_clock_valid & (setup.length == 1)):
                        # CLOCK_VALID CUR -> 1 byte 0x01 (clock is valid/locked).
                        m.d.comb += [
                            transmitter.data[0].eq(Const(1, 8)),
                            transmitter.max_length.eq(1)
                        ]
                    with m.Else():
                        m.d.comb += interface.handshakes_out.stall.eq(1)

                    # ... trigger it to respond when data's requested...
                    with m.If(interface.data_requested):
                        m.d.comb += transmitter.start.eq(1)

                    # ... and ACK our status stage.
                    with m.If(interface.status_requested):
                        m.d.comb += interface.handshakes_out.ack.eq(1)

                with m.Default():
                    #
                    # Stall unhandled requests.
                    #
                    with m.If(interface.status_requested | interface.data_requested):
                        m.d.comb += interface.handshakes_out.stall.eq(1)

        with m.Elif(setup.type == USBRequestType.VENDOR):
            m.d.comb += interface.claim.eq(1)
            with m.Switch(setup.request):
                with m.Case(VendorRequests.ILA_STOP_CAPTURE):
                    # TODO - will be implemented when needed
                    pass

                with m.Default():
                    m.d.comb += self.interface.handshakes_out.stall.eq(1)

        with m.Else():
            m.d.comb += self.interface.handshakes_out.stall.eq(1)

        return m
