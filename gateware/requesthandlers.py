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
    def __init__(self):
        super().__init__()

        self.output_interface_altsetting_nr = Signal(3)
        self.input_interface_altsetting_nr  = Signal(3)
        self.interface_settings_changed     = Signal()
        # Multi-rate clock source: 0 = 96 kHz (default), 1 = 48 kHz. Host selects
        # via SET CUR(SAM_FREQ); the gateware adapts framing + feedback. Exposed
        # to the top level.
        self.current_rate_48k               = Signal()

    def elaborate(self, platform):
        m = Module()

        interface         = self.interface
        setup             = self.interface.setup

        m.submodules.transmitter = transmitter = \
            StreamSerializer(data_length=14, domain="usb", stream_type=USBInStreamInterface, max_length_width=14)

        m.d.usb += self.interface_settings_changed.eq(0)

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

                m.d.usb += [
                    self.output_interface_altsetting_nr.eq(0),
                    self.input_interface_altsetting_nr.eq(0),
                    self.interface_settings_changed.eq(1),
                ]

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
        # The active rate is driven from OUTSIDE (AVB/AVDECC side, or a build
        # default on the DAC top). The clock source reports just this one rate,
        # read-only; the host follows it. Plain Python expression (not an m.d
        # statement) so it doesn't break the If/Elif chain.
        cur_rate    = Mux(self.current_rate_48k, SRATE_48k, SRATE_96k)

        with m.Elif(setup.type == USBRequestType.CLASS):
            # Claim ALL class requests — this device owns the audio class
            # space; unknown ones fall to the Default stall below.
            m.d.comb += interface.claim.eq(1)
            with m.Switch(setup.request):
                with m.Case(AudioClassSpecificRequestCodes.RANGE):
                    m.d.comb += transmitter.stream.attach(self.interface.tx)

                    with m.If(request_clock_freq):
                        m.d.comb += [
                            Cat(transmitter.data).eq(
                                Cat(Const(0x1, 16),            # wNumSubRanges = 1
                                    cur_rate, cur_rate, ZERO)),  # the one active rate
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
                        # GET CUR(SAM_FREQ) -> the currently selected rate.
                        m.d.comb += [
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
                        # Read-only clock: nothing to SET. Stall anything else.
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
