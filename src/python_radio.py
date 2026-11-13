"""Pi-side radio source: SX1262 over SPI (radio/radio_rx.py) -> protobuf decode -> SQLite queue.

Enabled with RADIO_SOURCE=python. Radio settings default to the ground-station PCB and the f26
Pico transmitter (see radio/radio_rx.py). Override any radio_rx flag with RADIO_ARGS, e.g.
RADIO_ARGS="--sf 9 --freq 915000000".

Loss detection: LoRa has no acknowledgements, so a packet that never arrives is invisible to the
radio. Every protobuf Packet carries a sequence number, so a jump in that number (seq 7 then 10)
means packets 8 and 9 were missed. Those gaps are logged and counted in the periodic stats line.

One radio packet can carry several kinds of telemetry (sensors, EFM, geiger), so each decoded
packet is queued as one row per kind.

Front panel: with FRONT_PANEL_ENABLED=true, a button and LEDs on the Pi (see src/front_panel.py) let
you send the cutdown locally with no network at all. It only queues a request; the transmit below
does the sending.

Uplink: this process owns the radio, so commands are transmitted from here. send_cutdown.py drops
a request file into TX_SPOOL_DIR; the loop below picks it up between received packets, checks it,
transmits it, and writes a result file. Transmitting is OFF unless RADIO_TX_ENABLED=true.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src import queue_writer
from src.protobuf_decoder import decode_radio_messages, parse_packet

logger = logging.getLogger(__name__)

POLL_SLEEP_S = 0.002
STATS_INTERVAL_S = 10.0
TX_CHECK_S = 0.2
DEFAULT_TX_POWER_DBM = 10


def tx_spool_dir() -> Path:
    return Path(os.environ.get("TX_SPOOL_DIR") or "~/leos-state/tx").expanduser()


def _write_result(tx_dir: Path, request_id: str, status: str, detail: str = "") -> None:
    """Write <id>.result.json atomically so send_cutdown.py never reads a half-written file."""
    result = {"status": status, "detail": detail, "t": time.time()}
    tmp = tx_dir / f"{request_id}.result.tmp"
    tmp.write_text(json.dumps(result))
    os.replace(tmp, tx_dir / f"{request_id}.result.json")


def service_tx_requests(radio, tx_dir: Path, stats: dict) -> None:
    """Handle at most one pending transmit request. Every outcome is written to a result file.

    A request is sent only if ALL of these hold: RADIO_TX_ENABLED=true, it has not expired, and
    the bytes are a LEOS command packet. Anything else is refused, so a stale or malformed file
    can never cause a transmission.
    """
    try:
        requests_pending = sorted(tx_dir.glob("*.request.json"))
    except OSError:
        return
    if not requests_pending:
        return

    path = requests_pending[0]
    request_id = path.name[: -len(".request.json")]
    try:
        request = json.loads(path.read_text())
        data = bytes.fromhex(request["hex"])
        expires_at = float(request["expires_at"])
    except Exception as exc:
        _write_result(tx_dir, request_id, "rejected", f"unreadable request: {exc}")
        path.unlink(missing_ok=True)
        return

    path.unlink(missing_ok=True)   # consumed: it can never be sent twice

    if os.environ.get("RADIO_TX_ENABLED", "").strip().lower() != "true":
        status, detail = "rejected", "transmitting is disabled (set RADIO_TX_ENABLED=true)"
    elif time.time() > expires_at:
        status, detail = "expired", "request was too old, not sent"
    else:
        try:
            packet = parse_packet(data)
            is_command = packet.WhichOneof("payload") == "command"
        except Exception as exc:
            is_command, detail = False, f"not a LEOS packet: {exc}"
        else:
            detail = "only command packets can be transmitted"
        if not is_command:
            status = "rejected"
        else:
            from radio.radio_tx import TransmitError, transmit

            power = int(os.environ.get("RADIO_TX_POWER_DBM", DEFAULT_TX_POWER_DBM))
            try:
                transmit(radio, data, power_dbm=power)
            except TransmitError as exc:
                status, detail = "failed", str(exc)
            else:
                status, detail = "sent", f"{len(data)} bytes at {power} dBm"
                stats["tx_sent"] = stats.get("tx_sent", 0) + 1

    log = logger.warning if status == "sent" else logger.error
    log("Uplink request %s: %s (%s)", request_id, status, detail)
    _write_result(tx_dir, request_id, status, detail)


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
    last_tx_check = 0.0
    tx_dir = tx_spool_dir()
    tx_dir.mkdir(parents=True, exist_ok=True)

    status = {"last_rx": None}          # read by the front panel for the "hearing the payload" LED
    try:
        from src.front_panel import start_front_panel

        start_front_panel(stop, status)
    except Exception:
        logger.exception("Front panel did not start; the receiver carries on without it")

    while not stop.is_set():
        pkt = radio.poll()
        if pkt is None:
            if time.monotonic() - last_tx_check >= TX_CHECK_S:
                last_tx_check = time.monotonic()
                service_tx_requests(radio, tx_dir, stats)
            time.sleep(POLL_SLEEP_S)
        else:
            stats["received"] += 1
            if not pkt.get("crc_ok", True):
                stats["bad_crc"] += 1
                logger.warning("Dropping packet with bad CRC (rssi=%s snr=%s)", pkt.get("rssi"), pkt.get("snr"))
            else:
                frame = bytes.fromhex(pkt["hex"])
                try:
                    messages = decode_radio_messages(frame, datetime.now(timezone.utc))
                except Exception as exc:
                    logger.warning("Decode failed (%s) len=%d data=%s", exc, len(frame), frame.hex())
                else:
                    status["last_rx"] = time.monotonic()
                    stats["decoded"] += 1
                    for message_type, payload in messages:
                        queue_writer.enqueue(queue_path, message_type, payload)
                        stats["queued"] += 1

                    callsign, sequence = _packet_identity(frame)
                    who = f"{callsign} seq={sequence}" if callsign is not None else "legacy frame"
                    logger.info(
                        "Queued %s from %s (rssi=%s snr=%s)",
                        "+".join(message_type for message_type, _ in messages),
                        who, pkt.get("rssi"), pkt.get("snr"),
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