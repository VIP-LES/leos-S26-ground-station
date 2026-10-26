"""Pi-side radio source: SX1262 over SPI (radio/radio_rx.py) -> protobuf decode -> SQLite queue.

Enabled with RADIO_SOURCE=python. Radio settings default to the ground-station PCB and the f26
Pico transmitter (see radio/radio_rx.py). Override any radio_rx flag with RADIO_ARGS, e.g.
RADIO_ARGS="--sf 9 --freq 915000000".
"""

from __future__ import annotations

import logging
import os
import shlex
import threading
import time
from datetime import datetime, timezone

from src import queue_writer
from src.protobuf_decoder import decode_radio_frame, parse_packet

logger = logging.getLogger(__name__)

POLL_SLEEP_S = 0.002
STATS_INTERVAL_S = 10.0


def _open_radio():
    # Imported here so machines without spidev/lgpio (Mac, CI, lab host) can still import this module.
    from radio.radio_rx import SX1262, build_parser

    args = build_parser().parse_args(shlex.split(os.environ.get("RADIO_ARGS", "")))
    radio = SX1262(args)
    radio.configure()
    logger.info(
        "SX1262 listening: %.3f MHz SF%d BW%d CR4/%d sync=0x%02X",
        args.freq / 1e6, args.sf, args.bw, args.cr + 4, args.sync,
    )
    return radio


def run_python_radio(
    queue_path: str,
    *,
    radio=None,
    stop: threading.Event | None = None,
) -> None:
    """Poll the radio forever, decode each packet, and enqueue it for the forwarder.

    ``radio`` (anything with ``poll() -> dict | None``) and ``stop`` exist so tests can drive the
    loop without hardware.
    """
    stop = stop or threading.Event()
    try:
        radio = radio or _open_radio()
    except Exception:
        logger.exception("Could not start the SX1262 radio")
        raise

    received = decoded = queued = crc_bad = 0
    last_stats = time.monotonic()

    while not stop.is_set():
        pkt = radio.poll()
        if pkt is None:
            time.sleep(POLL_SLEEP_S)
        else:
            received += 1
            if not pkt.get("crc_ok", True):
                crc_bad += 1
                logger.warning("Dropping packet with bad CRC (rssi=%s snr=%s)", pkt.get("rssi"), pkt.get("snr"))
            else:
                frame = bytes.fromhex(pkt["hex"])
                try:
                    message_type, payload = decode_radio_frame(frame, datetime.now(timezone.utc))
                except Exception as exc:
                    logger.warning("Decode failed (%s) len=%d data=%s", exc, len(frame), frame.hex())
                else:
                    decoded += 1
                    queue_writer.enqueue(queue_path, message_type, payload)
                    queued += 1
                    try:
                        info = parse_packet(frame)
                        who = f"{info.callsign} seq={info.sequence}"
                    except Exception:
                        who = "legacy frame"
                    logger.info(
                        "Queued %s from %s (rssi=%s snr=%s)", message_type, who, pkt.get("rssi"), pkt.get("snr")
                    )

        now = time.monotonic()
        if now - last_stats >= STATS_INTERVAL_S:
            logger.info("python radio stats: received=%d decoded=%d queued=%d bad_crc=%d",
                        received, decoded, queued, crc_bad)
            last_stats = now