"""Transmit one packet on an already-configured SX1262 (radio.radio_rx.SX1262).

The receiver (index.py -> python_radio.py) owns the radio and keeps it in continuous receive.
This module briefly switches it to transmit, sends one packet, and puts it back in receive by
calling radio.configure() again. It imports no hardware libraries itself: everything goes
through radio.write() / radio.read(), so it can be tested with a fake radio.

SX126x opcodes are from the datasheet (section 13). The power-amplifier settings match the Pico
transmitter in module-radio (SX1262, paDutyCycle 0x04, hpMax 0x07, 200 us ramp).
"""

from __future__ import annotations

import time

SET_STANDBY, SET_TX, SET_PKT = 0x80, 0x83, 0x8C
SET_PA_CONFIG, SET_TX_PARAMS, WRITE_BUFFER = 0x95, 0x8E, 0x0E
SET_DIO_IRQ, CLR_IRQ, GET_IRQ = 0x08, 0x02, 0x12

IRQ_TX_DONE, IRQ_TIMEOUT = 1 << 0, 1 << 9
MAX_PAYLOAD = 255
MIN_DBM, MAX_DBM = -9, 22
RAMP_200_US = 0x04
TX_TIMEOUT_STEPS = (0x00, 0xFA, 0x00)   # 64000 * 15.625 us = 1.0 s hardware timeout


class TransmitError(RuntimeError):
    """The packet was not sent."""


def _rf_path(radio, *, tx: bool) -> None:
    """Switch the external RF path if the board uses RXEN/TXEN pins (DIO2 handles it otherwise)."""
    a = radio.a
    if a.rxen < 0 and a.txen < 0:
        return
    import lgpio  # only on the Pi, only when those pins are wired

    if a.rxen >= 0:
        lgpio.gpio_write(radio.h, a.rxen, 0 if tx else 1)
    if a.txen >= 0:
        lgpio.gpio_write(radio.h, a.txen, 1 if tx else 0)


def transmit(radio, data: bytes, *, power_dbm: int = 10, timeout_s: float = 2.0) -> None:
    """Send ``data`` as one LoRa packet, then return the radio to continuous receive.

    Uses the modulation, frequency and sync word that radio.configure() already set, so the
    uplink and downlink share the same settings. Raises TransmitError if it was not sent.
    The antenna must be connected before calling this.
    """
    if not 1 <= len(data) <= MAX_PAYLOAD:
        raise TransmitError(f"packet must be 1 to {MAX_PAYLOAD} bytes, got {len(data)}")
    if not MIN_DBM <= power_dbm <= MAX_DBM:
        raise TransmitError(f"power must be {MIN_DBM} to {MAX_DBM} dBm, got {power_dbm}")

    preamble = radio.a.preamble
    irq_mask = IRQ_TX_DONE | IRQ_TIMEOUT
    failure: str | None = None
    try:
        _rf_path(radio, tx=True)
        radio.write(SET_STANDBY, [0x00])                                   # STDBY_RC
        radio.write(SET_PA_CONFIG, [0x04, 0x07, 0x00, 0x01])               # SX1262 high-power PA
        radio.write(SET_TX_PARAMS, [power_dbm & 0xFF, RAMP_200_US])
        # preamble, explicit header (0), payload length, CRC on, standard IQ
        radio.write(SET_PKT, [preamble >> 8, preamble & 0xFF, 0x00, len(data), 0x01, 0x00])
        radio.write(WRITE_BUFFER, [0x00, *data])                           # buffer offset 0
        radio.write(SET_DIO_IRQ, [irq_mask >> 8, irq_mask & 0xFF, irq_mask >> 8, irq_mask & 0xFF, 0, 0, 0, 0])
        radio.write(CLR_IRQ, [0xFF, 0xFF])
        radio.write(SET_TX, list(TX_TIMEOUT_STEPS))

        deadline = time.monotonic() + timeout_s
        while True:
            hi, lo = radio.read(GET_IRQ, 2)
            irq = (hi << 8) | lo
            if irq & IRQ_TX_DONE:
                break
            if irq & IRQ_TIMEOUT:
                failure = "radio TX timeout"
                break
            if time.monotonic() > deadline:
                failure = "no TX_DONE before the host deadline"
                break
            time.sleep(0.002)
    except Exception as exc:   # SPI trouble, BUSY stuck high, and so on
        failure = f"radio error: {exc}"
    finally:
        try:
            radio.write(CLR_IRQ, [0xFF, 0xFF])
            radio.write(SET_STANDBY, [0x00])
            _rf_path(radio, tx=False)
        finally:
            radio.configure()                                             # back to continuous receive

    if failure:
        raise TransmitError(failure)