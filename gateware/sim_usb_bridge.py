#!/usr/bin/env python3
# Cycle-accurate sims of the USB->AVB ingress path, to localise the ~23x sample
# over-production the soft-ILA + usbmon found on hardware (usb_samp ~2.2M/s vs
# real 96k/s for 2ch@48k). Two isolated tests:
#   1) the LUNA USBIsochronousOutStreamEndpoint alone: 48 bytes in -> 48 out?
#   2) USBStreamToChannels alone: a realistic byte stream -> right cs.valid rate?
# Run: avb-usb-host/.venv/bin/python gateware/sim_usb_bridge.py
from amaranth import *
from amaranth.sim import Simulator
from luna.gateware.usb.usb2.endpoints.isochronous import USBIsochronousOutStreamEndpoint
from usb_stream_to_channels import USBStreamToChannels

MPS = 224


def test_ep_alone(packet_len=48):
    """Feed one OUT packet of `packet_len` bytes; count bytes the EP emits with
    ready held high. A clean EP emits exactly packet_len then goes empty."""
    m = Module()
    m.submodules.ep = ep = USBIsochronousOutStreamEndpoint(
        endpoint_number=1, max_packet_size=MPS)
    m.d.comb += ep.stream.ready.eq(1)        # consumer always ready (as the wrapper does)
    it = ep.interface

    out_bytes = []

    def bench():
        yield it.tokenizer.endpoint.eq(1)
        yield it.tokenizer.is_out.eq(1)
        yield it.rx.valid.eq(0); yield it.rx.next.eq(0)
        yield it.rx_complete.eq(0); yield it.rx_invalid.eq(0)
        yield; yield
        # deliver packet_len bytes (rx.next strobes each byte, rx.valid qualifies)
        for i in range(packet_len):
            yield it.rx.valid.eq(1)
            yield it.rx.next.eq(1)
            yield it.rx.payload.eq(i & 0xff)
            yield
        yield it.rx.valid.eq(0); yield it.rx.next.eq(0)
        # commit the transaction (good CRC)
        yield it.rx_complete.eq(1); yield
        yield it.rx_complete.eq(0)
        # drain & count output beats for a long window
        for _ in range(4000):
            v = yield ep.stream.valid
            r = yield ep.stream.ready
            if v and r:
                out_bytes.append((yield ep.stream.payload))
            yield

    sim = Simulator(m)
    sim.add_clock(1/60e6, domain="usb")
    sim.add_sync_process(bench, domain="usb")
    sim.run()
    print(f"[EP] fed {packet_len} bytes -> EP emitted {len(out_bytes)} bytes "
          f"({'CLEAN' if len(out_bytes)==packet_len else 'FLOOD/MISMATCH'})")
    if out_bytes[:8]:
        print(f"     first 8 out: {out_bytes[:8]}  last 4: {out_bytes[-4:]}")
    return len(out_bytes)


def test_decoder_alone(n_frames=6, n_ch=2):
    """Feed USBStreamToChannels a realistic byte stream of n_frames*n_ch samples
    (4 bytes each) with first on byte 0; ready held high; count cs.valid/cs.first.
    Correct: cs.valid = n_frames*n_ch, cs.first = n_frames."""
    m = Module()
    m.submodules.dec = dec = USBStreamToChannels(max_no_channels=8)
    m.d.comb += [
        dec.no_channels_in.eq(n_ch),            # the fix under test
        dec.channel_stream_out.ready.eq(1),      # consumer always ready
    ]
    nbytes = n_frames * n_ch * 4
    valids = []; firsts = []

    def bench():
        yield dec.usb_stream_in.valid.eq(0)
        yield dec.usb_stream_in.first.eq(0)
        yield dec.usb_stream_in.last.eq(0)
        yield; yield
        for i in range(nbytes):
            yield dec.usb_stream_in.valid.eq(1)
            yield dec.usb_stream_in.first.eq(1 if i == 0 else 0)
            yield dec.usb_stream_in.last.eq(1 if i == nbytes-1 else 0)
            yield dec.usb_stream_in.payload.eq(i & 0xff)
            # sample the channel-stream outputs each cycle
            if (yield dec.channel_stream_out.valid):
                valids.append((yield dec.channel_stream_out.channel_nr))
            if (yield dec.channel_stream_out.first):
                firsts.append(1)
            yield
        yield dec.usb_stream_in.valid.eq(0)
        for _ in range(20):
            if (yield dec.channel_stream_out.valid):
                valids.append((yield dec.channel_stream_out.channel_nr))
            if (yield dec.channel_stream_out.first):
                firsts.append(1)
            yield

    sim = Simulator(m)
    sim.add_clock(1/60e6)
    sim.add_sync_process(bench)
    sim.run()
    exp_v = n_frames * n_ch
    print(f"[DEC] fed {nbytes} bytes ({n_frames} frames x {n_ch}ch) -> "
          f"cs.valid={len(valids)} (expect {exp_v}), cs.first={len(firsts)} (expect {n_frames})  "
          f"channels seen={valids}")


def test_ep_decoder_continuous(n_packets=40, pkt_bytes=48, gap=80, n_ch=2):
    """The REALISTIC case: EP fed many back-to-back OUT packets (with inter-packet
    gaps, like microframes) -> decoder, consumer always ready. Counts cs.valid &
    cs.first over the whole run. Clean: cs.valid = n_packets*pkt_bytes/4."""
    from amaranth.hdl.dsl import Module as _M
    m = Module()
    m.submodules.ep = ep = USBIsochronousOutStreamEndpoint(
        endpoint_number=1, max_packet_size=MPS)
    # decoder in the SAME (usb) domain as the EP fifo
    m.submodules.dec = dec = DomainRenamer("usb")(USBStreamToChannels(max_no_channels=8))
    m.d.comb += [
        dec.usb_stream_in.stream_eq(ep.stream),     # EP.stream -> decoder (ready flows back)
        dec.no_channels_in.eq(n_ch),
        dec.channel_stream_out.ready.eq(1),         # bridge accepts always (as wrapper)
    ]
    it = ep.interface
    stats = {"valid": 0, "first": 0, "ep_out": 0}

    def bench():
        yield it.tokenizer.endpoint.eq(1)
        yield it.tokenizer.is_out.eq(1)
        yield
        for p in range(n_packets):
            for i in range(pkt_bytes):
                yield it.rx.valid.eq(1); yield it.rx.next.eq(1)
                yield it.rx.payload.eq((p + i) & 0xff)
                yield
                if (yield dec.channel_stream_out.valid): stats["valid"] += 1
                if (yield dec.channel_stream_out.first): stats["first"] += 1
                if (yield ep.stream.valid) and (yield ep.stream.ready): stats["ep_out"] += 1
            yield it.rx.valid.eq(0); yield it.rx.next.eq(0)
            yield it.rx_complete.eq(1); yield; yield it.rx_complete.eq(0)
            for _ in range(gap):    # inter-packet gap (EP empty)
                yield
                if (yield dec.channel_stream_out.valid): stats["valid"] += 1
                if (yield dec.channel_stream_out.first): stats["first"] += 1
                if (yield ep.stream.valid) and (yield ep.stream.ready): stats["ep_out"] += 1
        for _ in range(200):
            yield
            if (yield dec.channel_stream_out.valid): stats["valid"] += 1
            if (yield dec.channel_stream_out.first): stats["first"] += 1
            if (yield ep.stream.valid) and (yield ep.stream.ready): stats["ep_out"] += 1

    sim = Simulator(m)
    sim.add_clock(1/60e6, domain="usb")
    sim.add_sync_process(bench, domain="usb")
    sim.run()
    exp_v = n_packets * pkt_bytes // 4
    print(f"[CHAIN] {n_packets} pkts x {pkt_bytes}B -> EP_out={stats['ep_out']}B "
          f"(expect {n_packets*pkt_bytes}), cs.valid={stats['valid']} (expect {exp_v}), "
          f"cs.first={stats['first']} (expect {exp_v//n_ch})  "
          f"{'CLEAN' if stats['valid']==exp_v else 'FLOOD x%.1f' % (stats['valid']/exp_v)}")


def test_full_bridge(n_packets=40, pkt_bytes=48, gap=80, n_ch=2):
    """Full ingress as on hardware: EP -> decoder -> cd_usb->sys AsyncFIFO bridge
    -> sys-side do_pop=usb_readable + the usb_samp/first counters (exactly the
    aaf_packetizer read logic). Reproduces what the hardware 'a' print measures.
    Clean: usb_samp == cs.valid == n_packets*pkt_bytes/4."""
    from amaranth.lib.fifo import AsyncFIFO
    m = Module()
    m.submodules.ep = ep = USBIsochronousOutStreamEndpoint(
        endpoint_number=1, max_packet_size=MPS)
    m.submodules.dec = dec = DomainRenamer("usb")(USBStreamToChannels(max_no_channels=8))
    cs = dec.channel_stream_out
    m.submodules.bridge = bridge = AsyncFIFO(width=64, depth=64, r_domain="sync", w_domain="usb")
    w_word = Cat(Const(0,8), cs.payload[:24], cs.channel_nr, cs.first, Const(0,28))
    m.d.comb += [
        dec.usb_stream_in.stream_eq(ep.stream),
        dec.no_channels_in.eq(n_ch),
        cs.ready.eq(1),
        bridge.w_data.eq(w_word),
        bridge.w_en.eq(cs.valid & bridge.w_rdy),     # wrapper write side
    ]
    # sys-side read EXACTLY as aaf_packetizer: do_pop = usb_readable (always drain)
    usb_readable = Signal();  do_pop = Signal();  first_bit = Signal()
    m.d.comb += [
        usb_readable.eq(bridge.r_rdy),
        do_pop.eq(usb_readable),
        bridge.r_en.eq(do_pop & bridge.r_rdy),
        first_bit.eq(bridge.r_data[35]),
    ]
    usb_samp = Signal(32); first_cnt = Signal(32); wr_cnt = Signal(32)
    with m.If(usb_readable & do_pop):
        m.d.sync += usb_samp.eq(usb_samp + 1)
    with m.If(do_pop & first_bit):
        m.d.sync += first_cnt.eq(first_cnt + 1)
    with m.If(bridge.w_en):
        m.d.usb += wr_cnt.eq(wr_cnt + 1)               # frames actually written (cd_usb)

    it = ep.interface
    def bench():
        yield it.tokenizer.endpoint.eq(1); yield it.tokenizer.is_out.eq(1); yield
        for p in range(n_packets):
            for i in range(pkt_bytes):
                yield it.rx.valid.eq(1); yield it.rx.next.eq(1)
                yield it.rx.payload.eq((p+i) & 0xff); yield
            yield it.rx.valid.eq(0); yield it.rx.next.eq(0)
            yield it.rx_complete.eq(1); yield; yield it.rx_complete.eq(0)
            for _ in range(gap): yield
        for _ in range(400): yield

    sim = Simulator(m)
    sim.add_clock(1/60e6, domain="usb")
    sim.add_clock(1/60e6, domain="sync")
    sim.add_sync_process(bench, domain="usb")
    def collect():
        for _ in range(n_packets*(pkt_bytes+gap+4)+800): yield
        print(f"[BRIDGE] cd_usb writes={ (yield wr_cnt) } (expect {n_packets*pkt_bytes//4}), "
              f"sys usb_samp={ (yield usb_samp) }, first_cnt={ (yield first_cnt) }")
    sim.add_sync_process(collect, domain="sync")
    sim.run()


if __name__ == "__main__":
    print("=== Test 1: USBIsochronousOutStreamEndpoint alone ===")
    test_ep_alone(48)
    print("\n=== Test 2: USBStreamToChannels alone (no_channels_in=2) ===")
    test_decoder_alone(6, 2)
    print("\n=== Test 3: EP -> decoder, CONTINUOUS multi-packet (realistic) ===")
    test_ep_decoder_continuous(40, 48, 80, 2)
    print("\n=== Test 4: FULL ingress + cd_usb->sys bridge + sys do_pop counters ===")
    test_full_bridge(40, 48, 80, 2)


def test_first_cadence(n_frames=24, n_ch=2, firsts_per_packet=1, pkt_frames=6):
    """Measure cs.first cadence for 2ch with usb_first asserted `firsts_per_packet`
    times per `pkt_frames`-frame packet. Correct: cs.first == n_frames (1/frame).
    If the real EP asserts stream.first multiple times per packet, each forces
    out_channel_nr=0 -> extra firsts -> first > n_frames (on-HW: 1.5x)."""
    m = Module()
    m.submodules.dec = dec = USBStreamToChannels(max_no_channels=8)
    m.d.comb += [dec.no_channels_in.eq(n_ch), dec.channel_stream_out.ready.eq(1)]
    nbytes = n_frames * n_ch * 4
    res = {"valid":0, "first":0}
    def bench():
        yield dec.usb_stream_in.valid.eq(0); yield
        byte_i = 0
        for f in range(n_frames):
            for c in range(n_ch):
                for b in range(4):
                    # assert usb_first on the very first byte of the packet, and
                    # (to model EP chunking) `firsts_per_packet` evenly within it
                    in_pkt = f % pkt_frames
                    first = 1 if (b==0 and c==0 and (in_pkt * firsts_per_packet // pkt_frames) !=
                                  ((in_pkt-1) * firsts_per_packet // pkt_frames) ) else 0
                    if f % pkt_frames == 0 and b==0 and c==0:
                        first = 1
                    yield dec.usb_stream_in.valid.eq(1)
                    yield dec.usb_stream_in.first.eq(first)
                    yield dec.usb_stream_in.payload.eq(byte_i & 0xff); byte_i += 1
                    yield
                    if (yield dec.channel_stream_out.valid): res["valid"] += 1
                    if (yield dec.channel_stream_out.first): res["first"] += 1
        yield dec.usb_stream_in.valid.eq(0)
        for _ in range(20):
            yield
            if (yield dec.channel_stream_out.valid): res["valid"] += 1
            if (yield dec.channel_stream_out.first): res["first"] += 1
    sim = Simulator(m); sim.add_clock(1/60e6); sim.add_sync_process(bench); sim.run()
    print(f"[FIRST] {n_frames} frames x{n_ch}ch, firsts/pkt={firsts_per_packet}: "
          f"cs.valid={res['valid']} (exp {n_frames*n_ch}), cs.first={res['first']} (exp {n_frames})")

if __name__ == "__main__":
    print("\n=== Test 5: cs.first cadence vs usb_first frequency ===")
    test_first_cadence(24, 2, 1)
    test_first_cadence(24, 2, 2)
    test_first_cadence(24, 2, 3)
