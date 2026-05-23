#!/usr/bin/env python3
"""Rigol DHO924S USBTMC helper for ULPI debugging on the i9plus."""
import sys, time
import usbtmc

VID, PID = 0x1ab1, 0x044c

def open_scope():
    inst = usbtmc.Instrument(VID, PID)
    inst.timeout = 5
    return inst

def setup_ulpi(s):
    """Set channels: Ch1=CLK Ch2=STP Ch3=NXT Ch4=DIR."""
    s.write('*RST')
    time.sleep(2)
    for ch in (1, 2, 3, 4):
        s.write(f':CHAN{ch}:DISP ON')
        s.write(f':CHAN{ch}:SCAL 1')        # 1 V/div
        s.write(f':CHAN{ch}:OFFS -1.5')     # baseline lower
        s.write(f':CHAN{ch}:COUP DC')
        s.write(f':CHAN{ch}:PROB 1')        # 1× (set to 10 if you use 10× probes)
    s.write(':TIM:MAIN:SCAL 1e-6')          # 1 µs/div
    s.write(':TIM:MAIN:OFFS 0')

def trigger_on(s, ch=2, level=1.5):
    s.write(':TRIG:MODE EDGE')
    s.write(f':TRIG:EDGE:SOUR CHAN{ch}')
    s.write(':TRIG:EDGE:SLOP POS')
    s.write(f':TRIG:EDGE:LEV {level}')
    s.write(':TRIG:SWE NORM')               # normal — wait for trigger

def status(s):
    return {
        'trig_status': s.ask(':TRIG:STAT?').strip(),
        'tim_scale':   s.ask(':TIM:MAIN:SCAL?').strip(),
        'ch1':         s.ask(':CHAN1:DISP?').strip(),
    }

def screenshot(s, path='scope.png'):
    """Grab a PNG screenshot."""
    s.write(':DISP:DATA? ON,OFF,PNG')
    raw = s.read_raw(2_000_000)
    # IEEE 488.2 block: "#<len_digits><len><data>"
    assert raw[:1] == b'#', f'unexpected header: {raw[:10]}'
    ndigits = int(raw[1:2])
    nbytes  = int(raw[2:2+ndigits])
    png = raw[2+ndigits:2+ndigits+nbytes]
    with open(path, 'wb') as f:
        f.write(png)
    return path, len(png)

def measure_dc(s, ch):
    s.write(f':MEAS:VAVG:SOUR CHAN{ch}')
    return float(s.ask(':MEAS:ITEM? VAVG').strip())

def measure_freq(s, ch):
    try:
        s.write(f':MEAS:FREQ:SOUR CHAN{ch}')
        return float(s.ask(':MEAS:ITEM? FREQ').strip())
    except Exception:
        return None

if __name__ == '__main__':
    s = open_scope()
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'setup':
        setup_ulpi(s); print('configured')
    elif cmd == 'trigger':
        trigger_on(s); print('armed')
    elif cmd == 'shot':
        p, n = screenshot(s, sys.argv[2] if len(sys.argv) > 2 else 'scope.png')
        print(f'wrote {p} ({n} bytes)')
    elif cmd == 'voltages':
        for ch, name in [(1,'CLK'), (2,'STP'), (3,'NXT'), (4,'DIR')]:
            v = measure_dc(s, ch)
            f = measure_freq(s, ch)
            fs = f'{f/1e6:.2f} MHz' if f and 100 < f < 200e6 else '—'
            print(f'  Ch{ch} {name:3s}: avg={v*1000:+7.1f} mV  freq={fs}')
    else:
        print('Status:', status(s))
