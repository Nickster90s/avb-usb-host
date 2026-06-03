#!/usr/bin/env python3
# Multi-packet-with-gaps sim of the REAL ingress chain:
#   EP (USBIsochronousOutStreamEndpoint + its 2-byte boundary detector) -> decoder
# Each sample's 4 bytes encode its global index N: [pad=0, N&ff, N>>8, N>>16], so
# decoder payload == N. We feed many packets WITH GAPS (the real µframe structure
# the old sim never modelled) and check the decoded sequence is 0,1,2,3,... with no
# drop/dup/corruption — especially at packet boundaries (every frames*n_ch samples).
# Run: avb-usb-host/.venv/bin/python gateware/sim_boundaries.py
from amaranth import *
from amaranth.sim import Simulator
from luna.gateware.usb.usb2.endpoints.isochronous import USBIsochronousOutStreamEndpoint
from usb_stream_to_channels import USBStreamToChannels

MPS = 224


def run(packets=30, frames=6, n_ch=2, gap=20, vary=True):
    m = Module()
    m.submodules.ep  = ep  = USBIsochronousOutStreamEndpoint(endpoint_number=1, max_packet_size=MPS)
    dec = USBStreamToChannels(max_no_channels=8)
    m.submodules.dec = DomainRenamer("usb")(dec)   # decoder runs in cd_usb, exactly as the wrapper
    m.d.comb += [
        dec.no_channels_in.eq(n_ch),
        dec.channel_stream_out.ready.eq(1),            # consumer always ready (wrapper ties cs.ready=1)
        dec.usb_stream_in.valid.eq(ep.stream.valid),
        dec.usb_stream_in.payload.eq(ep.stream.payload),
        dec.usb_stream_in.first.eq(0),                 # free-run, exactly as the wrapper
        ep.stream.ready.eq(dec.usb_stream_in.ready),
    ]
    it = ep.interface
    got = []
    stats = {"ep_emit": 0, "dec_emit": 0}

    def collect():
        if (yield ep.stream.valid) and (yield ep.stream.ready):
            stats["ep_emit"] += 1
        if (yield dec.channel_stream_out.valid):
            stats["dec_emit"] += 1
            got.append((yield dec.channel_stream_out.payload))

    def bench():
        yield it.tokenizer.endpoint.eq(1)
        yield it.tokenizer.is_out.eq(1)
        yield it.rx.valid.eq(0); yield it.rx.next.eq(0)
        yield it.rx_complete.eq(0); yield it.rx_invalid.eq(0)
        yield; yield
        idx = 0
        for p in range(packets):
            # vary frames/packet 5/6/7 like a real feedback-controlled host
            f = frames + ((p % 3) - 1) if vary else frames
            nsmp = f * n_ch
            for s in range(nsmp):
                sidx = idx + s
                for b in (0x00, sidx & 0xff, (sidx >> 8) & 0xff, (sidx >> 16) & 0xff):
                    yield it.rx.valid.eq(1); yield it.rx.next.eq(1); yield it.rx.payload.eq(b)
                    yield from collect()
                    yield
            idx += nsmp
            yield it.rx.valid.eq(0); yield it.rx.next.eq(0)
            yield it.rx_complete.eq(1)
            yield from collect(); yield
            yield it.rx_complete.eq(0)
            for _ in range(gap):
                yield from collect(); yield
        for _ in range(3000):
            yield from collect(); yield

    sim = Simulator(m)
    sim.add_clock(1/60e6, domain="usb")
    sim.add_sync_process(bench, domain="usb")
    sim.run()

    print(f"  [dbg] EP emitted {stats['ep_emit']} bytes, decoder emitted {stats['dec_emit']} samples")
    exp = list(range(idx_total(packets, frames, n_ch, vary)))
    n = min(len(exp), len(got))
    mism = [(i, exp[i], got[i]) for i in range(n) if exp[i] != got[i]]
    print(f"fed {len(exp)} samples -> decoder emitted {len(got)}")
    if len(got) == len(exp) and not mism:
        print("CLEAN: contiguous 0..N, no drop/dup/corruption across packet boundaries")
    else:
        if len(got) != len(exp):
            print(f"COUNT MISMATCH: expected {len(exp)}, got {len(got)} "
                  f"({len(got)-len(exp):+d})")
        print(f"VALUE MISMATCHES: {len(mism)} of {n} compared")
        for i, e, g in mism[:12]:
            print(f"  out#{i}: expected {e}, got {g}")


def idx_total(packets, frames, n_ch, vary):
    t = 0
    for p in range(packets):
        f = frames + ((p % 3) - 1) if vary else frames
        t += f * n_ch
    return t


if __name__ == "__main__":
    print("=== constant 6 frames/packet, with gaps ===")
    run(vary=False)
    print("\n=== varying 5/6/7 frames/packet (feedback-controlled host), with gaps ===")
    run(vary=True)
