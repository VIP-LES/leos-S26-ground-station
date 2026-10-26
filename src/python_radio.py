"""Pi-side radio source: SX1262 over SPI (radio/radio_rx.py) -> protobuf decode -> SQLite queue.

Enabled with RADIO_SOURCE=python. Radio settings default to the ground-station PCB and the f26
Pico transmitter (see radio/radio_rx.py). Override any radio_rx flag with RADIO_ARGS, e.g.
RADIO_ARGS="--sf 9 --freq 915000000".

Loss detection: LoRa has no acknowledgements, so a packet that never arrives is invisible to the
radio. Every protobuf Packet carries a sequence number, so a jump in that number (seq 7 then 10)
means packets 8 and 9 were missed. Those gaps are logged and counted in the periodic stats line.
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


def _packet_identity(frame: bytes) -> tuple[str | None, int | None]:
    """(callsign, sequence) for a protobuf packet, or (None, None) for anything else (legacy frames)."""
    try:
        packet = parse_packet(frame)
    except Exception:
        return None, None
    return packet.callsign, packet.sequence


def run_python_radio(
    queue_path: str,
    *,
    radio=None,
    stop: threading.Event | None = None,
) -> dict:
    """Poll the radio forever, decode each packet, and enqueue it for the forwarder.

    ``radio`` (anything with ``poll() -> dict | None``) and ``stop`` exist so tests can drive the
    loop without hardware. Returns the final counters when ``stop`` is set.
    """
    stop = stop or threading.Event()
    try:
        radio = radio or _open_radio()
    except Exception:
        logger.exception("Could not start the SX1262 radio")
        raise

    stats = {"received": 0, "decoded": 0, "queued": 0, "bad_crc": 0, "missed": 0, "restarts": 0, "duplicates": 0}
    last_seq: dict[str, int] = {}
    last_stats = time.monotonic()

    while not stop.is_set():
        pkt = radio.poll()
        if pkt is None:
            time.sleep(POLL_SLEEP_S)
        else:
            stats["received"] += 1
            if not pkt.get("crc_ok", True):
                stats["bad_crc"] += 1
                logger.warning("Dropping packet with bad CRC (rssi=%s snr=%s)", pkt.get("rssi"), pkt.get("snr"))
            else:
                frame = bytes.fromhex(pkt["hex"])
                try:
                    message_type, payload = decode_radio_frame(frame, datetime.now(timezone.utc))
                except Exception as exc:
                    logger.warning("Decode failed (%s) len=%d data=%s", exc, len(frame), frame.hex())
                else:
                    stats["decoded"] += 1
                    queue_writer.enqueue(queue_path, message_type, payload)
                    stats["queued"] += 1

                    callsign, sequence = _packet_identity(frame)
                    who = f"{callsign} seq={sequence}" if callsign is not None else "legacy frame"
                    logger.info(
                        "Queued %s from %s (rssi=%s snr=%s)", message_type, who, pkt.get("rssi"), pkt.get("snr")
                    )
                    if callsign is not None:
                        _track_sequence(last_seq, stats, callsign, sequence)

        now = time.monotonic()
        if now - last_stats >= STATS_INTERVAL_S:
            logger.info(
                "python radio stats: received=%d decoded=%d queued=%d bad_crc=%d missed=%d restarts=%d duplicates=%d",
                stats["received"], stats["decoded"], stats["queued"], stats["bad_crc"],
                stats["missed"], stats["restarts"], stats["duplicates"],
            )
            last_stats = now
    return stats


def _track_sequence(last_seq: dict[str, int], stats: dict, callsign: str, sequence: int) -> None:
    previous = last_seq.get(callsign)
    last_seq[callsign] = sequence
    if previous is None or sequence == previous + 1:
        return
    if sequence == previous:
        stats["duplicates"] += 1
        logger.warning("Repeated sequence %d from %s", sequence, callsign)
    elif sequence > previous:
        gap = sequence - previous - 1
        stats["missed"] += gap
        logger.warning("MISSED %d packet(s) from %s: seq %d -> %d", gap, callsign, previous, sequence)
    else:
        stats["restarts"] += 1
        logger.info("Sequence restarted for %s: %d -> %d (sender rebooted or new stream)", callsign, previous, sequence)