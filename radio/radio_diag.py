#!/usr/bin/env python3
"""Diagnostic: prints live RSSI + IRQ + DIO1 level, ignoring DIO1 for packet detection.
Run next to radio_rx.py:  python3 radio_diag.py
Then press 't' on the Pico. If RSSI jumps well above the idle level, the RF link works."""
import argparse
import time
import lgpio
import radio_rx as r

p = argparse.ArgumentParser()
p.add_argument("--freq", type=int, default=915_000_000)
p.add_argument("--tcxo-volt", type=int, default=3)
p.add_argument("--rst", type=int, default=25)
p.add_argument("--busy", type=int, default=22)
p.add_argument("--dio1", type=int, default=17)
x = p.parse_args()
a = argparse.Namespace(rst=x.rst, busy=x.busy, dio1=x.dio1, rxen=-1, txen=-1, spi_bus=0, spi_dev=0,
                       freq=x.freq, sf=7, bw=125, cr=1, sync=0x12, preamble=8,
                       tcxo=True, tcxo_volt=x.tcxo_volt, dio2_rfsw=True, ldo=True)
radio = r.SX1262(a)
radio.configure()
radio.wait_busy()
st = radio.spi.xfer2([0xC0, 0])[1]
errs = radio.read(0x17, 2)
print(f"chip mode={(st >> 4) & 7} (5=RX)  device errors=0x{(errs[0] << 8) | errs[1]:04x} (0 is good)")
print("idle RSSI should be about -100 to -120 dBm. Press 't' on the Pico now.\n")
peak = -200.0
t0 = time.time()
while True:
    best = -200.0
    for _ in range(40):  # sample RSSI fast so a short packet isn't missed
        radio.wait_busy()
        v = -radio.read(0x15, 1)[0] / 2.0
        best = max(best, v)
        time.sleep(0.005)
    peak = max(peak, best)
    hi, lo = radio.read(r.GET_IRQ, 2)
    dio1 = lgpio.gpio_read(radio.h, a.dio1)
    flag = "  <-- SIGNAL" if best > -85 else ""
    print(f"rssi_max={best:6.1f} dBm  peak={peak:6.1f}  irq=0x{(hi << 8) | lo:04x}  dio1={dio1}{flag}")
