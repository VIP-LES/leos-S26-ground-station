#!/usr/bin/env python3
"""LoRa receiver for SX1262 on a Raspberry Pi (Python port of the C version).

Prints one JSON line per received packet:
  {"t": 1760000000, "len": 12, "rssi": -92.5, "snr": 7.25, "crc_ok": true, "hex": "..."}

Needs:  sudo apt install -y python3-spidev python3-lgpio   (and SPI enabled)
Run:    python3 radio_rx.py
Defaults match the LEOS ground-station PCB (E22-900M30S on the Pi header) and the
f26 Pico test transmitter: 915 MHz, SF7, BW125, CR4/5, sync 0x12, TCXO 2.2 V via DIO3,
DIO2 as RF switch, LDO regulator. Radio settings MUST match the transmitter.
Pins are BCM numbers: RESET=25, BUSY=22, DIO1=17, NSS=CE0.
"""
import argparse
import json
import sys
import time

import lgpio
import spidev

# SX126x opcodes (datasheet section 13)
SET_STANDBY, SET_RX, SET_PKT_TYPE = 0x80, 0x82, 0x8A
SET_RF_FREQ, SET_MOD, SET_PKT, SET_BUF_BASE = 0x86, 0x8B, 0x8C, 0x8F
SET_DIO_IRQ, CLR_IRQ, GET_IRQ = 0x08, 0x02, 0x12
GET_RX_BUF, GET_PKT_STATUS, READ_BUF = 0x13, 0x14, 0x1E
WRITE_REG, SET_REG_MODE, CALIBRATE, CAL_IMAGE = 0x0D, 0x96, 0x89, 0x98
SET_DIO2_RFSW, SET_DIO3_TCXO = 0x9D, 0x97

IRQ_RX_DONE, IRQ_HEADER_ERR, IRQ_CRC_ERR, IRQ_TIMEOUT = 1 << 1, 1 << 5, 1 << 6, 1 << 9
BW_CODE = {62: 3, 125: 4, 250: 5, 500: 6}  # 62 = 62.5 kHz
BW_KHZ = {62: 62.5, 125: 125.0, 250: 250.0, 500: 500.0}


class SX1262:
    def __init__(self, a):
        self.a = a
        self.h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(self.h, a.rst, 1)
        lgpio.gpio_claim_input(self.h, a.busy)
        lgpio.gpio_claim_input(self.h, a.dio1)
        if a.rxen >= 0:
            lgpio.gpio_claim_output(self.h, a.rxen, 1)  # RX path on
        if a.txen >= 0:
            lgpio.gpio_claim_output(self.h, a.txen, 0)  # TX path off
        self.spi = spidev.SpiDev()
        self.spi.open(a.spi_bus, a.spi_dev)  # kernel drives NSS (CE0/CE1)
        self.spi.max_speed_hz = 2_000_000
        self.spi.mode = 0

    def wait_busy(self, timeout=0.2):
        end = time.time() + timeout
        while lgpio.gpio_read(self.h, self.a.busy):
            if time.time() > end:
                raise TimeoutError("BUSY stuck high - check wiring/power/reset pin")
            time.sleep(0.0001)

    def write(self, opcode, args=()):
        self.wait_busy()
        self.spi.xfer2([opcode, *args])  # one xfer2 => NSS held low for whole command

    def read(self, opcode, n, extra=()):
        """Returns n data bytes (skips the status byte the chip returns)."""
        self.wait_busy()
        resp = self.spi.xfer2([opcode, *extra] + [0] * (1 + n))
        return resp[len(extra) + 2:]

    def reset(self):
        lgpio.gpio_write(self.h, self.a.rst, 0)
        time.sleep(0.002)
        lgpio.gpio_write(self.h, self.a.rst, 1)
        time.sleep(0.01)
        self.wait_busy()

    def configure(self):
        a = self.a
        self.reset()
        self.write(SET_STANDBY, [0x00])  # STDBY_RC
        if a.tcxo:  # TCXO powered from DIO3 (1.8 V, ~5 ms startup), then recalibrate
            self.write(SET_DIO3_TCXO, [a.tcxo_volt, 0x00, 0x01, 0x40])
            self.write(CALIBRATE, [0x7F])
            time.sleep(0.01)
            self.wait_busy(1.0)
            self.write(0x07, [0x00, 0x00])  # ClearDeviceErrors: drop stale XOSC_START_ERR from before the TCXO was powered
        self.write(SET_STANDBY, [0x00])
        self.write(SET_REG_MODE, [0x00 if a.ldo else 0x01])  # DC-DC unless --ldo
        if a.dio2_rfsw:
            self.write(SET_DIO2_RFSW, [0x01])
        self.write(SET_PKT_TYPE, [0x01])  # LoRa
        self.write(CAL_IMAGE, [0xE1, 0xE9])  # 902-928 MHz image calibration; change for other bands
        frf = (a.freq << 25) // 32_000_000
        self.write(SET_RF_FREQ, [(frf >> 24) & 0xFF, (frf >> 16) & 0xFF, (frf >> 8) & 0xFF, frf & 0xFF])
        sym_ms = (1 << a.sf) / BW_KHZ[a.bw]
        ldro = 1 if sym_ms > 16.0 else 0  # low data rate optimisation
        self.write(SET_MOD, [a.sf, BW_CODE[a.bw], a.cr, ldro])
        # preamble, explicit header (0), max payload 255, CRC on, standard IQ
        self.write(SET_PKT, [a.preamble >> 8, a.preamble & 0xFF, 0x00, 0xFF, 0x01, 0x00])
        # sync word register 0x0740: 0x12 -> 0x1424 (private), 0x34 -> 0x3444 (public)
        sw = a.sync
        self.write(WRITE_REG, [0x07, 0x40, (sw & 0xF0) | 0x04, ((sw & 0x0F) << 4) | 0x04])
        self.write(WRITE_REG, [0x08, 0xAC, 0x96])  # RX boosted gain
        self.write(SET_BUF_BASE, [0x00, 0x00])
        irq = IRQ_RX_DONE | IRQ_HEADER_ERR | IRQ_CRC_ERR | IRQ_TIMEOUT
        self.write(SET_DIO_IRQ, [irq >> 8, irq & 0xFF, irq >> 8, irq & 0xFF, 0, 0, 0, 0])
        self.write(CLR_IRQ, [0xFF, 0xFF])
        self.write(SET_RX, [0xFF, 0xFF, 0xFF])  # continuous RX

    def poll(self):
        """Return a packet dict if one arrived, else None."""
        if not lgpio.gpio_read(self.h, self.a.dio1):
            return None
        hi, lo = self.read(GET_IRQ, 2)
        irq = (hi << 8) | lo
        self.write(CLR_IRQ, [hi, lo])
        if irq & IRQ_HEADER_ERR:
            print("header error", file=sys.stderr)
        if not irq & IRQ_RX_DONE:
            return None
        length, start = self.read(GET_RX_BUF, 2)
        rssi_raw, snr_raw, _ = self.read(GET_PKT_STATUS, 3)
        snr_raw = snr_raw - 256 if snr_raw > 127 else snr_raw
        data = bytes(self.read(READ_BUF, length, extra=[start]))
        return {"t": int(time.time()), "len": length, "rssi": -rssi_raw / 2.0, "snr": snr_raw / 4.0,
                "crc_ok": not (irq & IRQ_CRC_ERR), "hex": data.hex()}


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--freq", type=int, default=915_000_000)
    p.add_argument("--sf", type=int, default=7)
    p.add_argument("--bw", type=int, default=125, choices=[62, 125, 250, 500])
    p.add_argument("--cr", type=int, default=1, help="1..4 => 4/5..4/8")
    p.add_argument("--sync", type=lambda x: int(x, 0), default=0x12, help="0x12 private, 0x34 public")
    p.add_argument("--preamble", type=int, default=8)
    p.add_argument("--rst", type=int, default=25)
    p.add_argument("--busy", type=int, default=22)
    p.add_argument("--dio1", type=int, default=17)
    p.add_argument("--rxen", type=int, default=-1)
    p.add_argument("--txen", type=int, default=-1)
    p.add_argument("--spi-bus", type=int, default=0)
    p.add_argument("--spi-dev", type=int, default=0)
    p.add_argument("--tcxo", action=argparse.BooleanOptionalAction, default=True,
                   help="TCXO powered from DIO3 (default on; --no-tcxo for a plain crystal)")
    p.add_argument("--tcxo-volt", type=int, default=3, choices=range(8),
                   help="0=1.6V 1=1.7V 2=1.8V 3=2.2V 4=2.4V 5=2.7V 6=3.0V 7=3.3V (default 3 = 2.2V)")
    p.add_argument("--dio2-rfsw", action=argparse.BooleanOptionalAction, default=True,
                   help="DIO2 controls the RF switch (default on)")
    p.add_argument("--ldo", action=argparse.BooleanOptionalAction, default=True,
                   help="use LDO regulator (default on; --no-ldo for DC-DC)")
    return p


def main():
    a = build_parser().parse_args()

    radio = SX1262(a)
    radio.configure()
    print(f"listening: {a.freq/1e6:.3f} MHz SF{a.sf} BW{a.bw} CR4/{a.cr+4} sync=0x{a.sync:02X}", file=sys.stderr)
    last = time.time()
    try:
        while True:
            pkt = radio.poll()
            if pkt:
                print(json.dumps(pkt), flush=True)
            elif time.time() - last > 30:
                print("alive", file=sys.stderr)
                last = time.time()
            time.sleep(0.002)
    except KeyboardInterrupt:
        radio.write(SET_STANDBY, [0x00])


if __name__ == "__main__":
    main()