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

Local logs: everything is also written to the SD card (see src/packet_log.py), so there is a record even
with no network.

Uplink: this process owns the radio, so commands are transmitted from here. send_cutdown.py drops
a request file into TX_SPOOL_DIR; the loop below picks it up between received packets, checks it,
transmits it, and writes a result file. Transmitting is OFF unless RADIO_TX_ENABLED=true.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cutdown_command
from src import packet_log, queue_writer
from src.protobuf_decoder import decode_radio_messages, parse_packet

logger = logging.getLogger(__name__)

POLL_SLEEP_S = 0.002
STATS_INTERVAL_S = 10.0
TX_CHECK_S = 0.2
OPEN_RETRY_S = 5.0
DEFAULT_TX_POWER_DBM = 10
ACK_MATCH_WINDOW_S = 120.0     # an ACK counts only for a command this receiver transmitted this recently
_recent_tx: dict[int, float] = {}   # sequence -> time.monotonic() of each command we sent

# Retransmit-until-ACK (a request with retry_until_ack): the SAME packet is sent again every RETRY_INTERVAL_S
# (randomised a little, so it never stays in step with the payload's own once-a-second telemetry) until the
# payload's ACK arrives or CUTDOWN_RETRY_MAX_S runs out, or it is cancelled from the front panel.
DEFAULT_RETRY_INTERVAL_S = 2.0
DEFAULT_RETRY_MAX_S = 120.0
MAX_RETRY_MAX_S = 600.0
_retx: dict | None = None            # the one cutdown being retried: data, seq, attempts, deadline, next_at


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _next_gap() -> float:
    interval = max(1.0, _env_float("CUTDOWN_RETRY_INTERVAL_S", DEFAULT_RETRY_INTERVAL_S))
    return interval * random.uniform(0.8, 1.2)


def _start_retx(data: bytes, sequence: int, request: dict, panel_status: dict | None) -> None:
    global _retx
    max_s = min(MAX_RETRY_MAX_S, max(1.0, float(request.get("retry_max_s")
                                                or _env_float("CUTDOWN_RETRY_MAX_S", DEFAULT_RETRY_MAX_S))))
    now = time.monotonic()
    if _retx is not None:
        logger.warning("A new cutdown request replaces the one still being retried (sequence %d)", _retx["seq"])
    _retx = {"data": data, "seq": sequence, "attempts": 1, "deadline": now + max_s, "next_at": now + _next_gap()}
    if panel_status is not None:
        panel_status["cutdown_wait"] = sequence
        panel_status.pop("cutdown_outcome", None)
        panel_status["cancel_cutdown"] = False
    logger.warning("Cutdown sequence %d will be retransmitted about every %.0f s until the ACK arrives (up to %.0f s)",
                   sequence, _env_float("CUTDOWN_RETRY_INTERVAL_S", DEFAULT_RETRY_INTERVAL_S), max_s)


def _end_retx(panel_status: dict | None, result: str) -> None:
    """Stop retrying. result is "acked", "gave_up" or "cancelled"."""
    global _retx
    if _retx is None:
        return
    sequence, _retx = _retx["seq"], None
    if panel_status is not None:
        panel_status["cutdown_wait"] = None
        panel_status["cutdown_outcome"] = {"seq": sequence, "result": result, "at": time.monotonic()}


def service_retransmits(radio, stats: dict, panel_status: dict | None = None) -> None:
    """Send the pending cutdown again if it is time. Call it often; it is cheap when nothing is pending."""
    if _retx is None:
        return
    now = time.monotonic()
    if panel_status is not None and panel_status.get("cancel_cutdown"):
        panel_status["cancel_cutdown"] = False
        logger.warning("Cutdown retries cancelled after %d transmission(s), no ACK (sequence %d)",
                       _retx["attempts"], _retx["seq"])
        _end_retx(panel_status, "cancelled")
        return
    if os.environ.get("RADIO_TX_ENABLED", "").strip().lower() != "true":
        logger.error("Cutdown retries stopped: transmitting is disabled (RADIO_TX_ENABLED is not true)")
        _end_retx(panel_status, "gave_up")
        return
    if now >= _retx["deadline"]:
        logger.error("NO ACK for cutdown sequence %d after %d transmission(s): giving up",
                     _retx["seq"], _retx["attempts"])
        _end_retx(panel_status, "gave_up")
        return
    if now < _retx["next_at"]:
        return

    from radio.radio_tx import TransmitError, transmit

    power = int(os.environ.get("RADIO_TX_POWER_DBM", DEFAULT_TX_POWER_DBM))
    try:
        transmit(radio, _retx["data"], power_dbm=power)
    except TransmitError as exc:
        logger.error("Cutdown retransmit failed (%s); will try again", exc)
    else:
        _retx["attempts"] += 1
        stats["tx_sent"] = stats.get("tx_sent", 0) + 1
        _recent_tx[_retx["seq"]] = time.monotonic()        # keeps the ACK match window open
        if panel_status is not None:
            panel_status["retx_at"] = time.monotonic()      # the front panel blinks green on this
        logger.warning("Cutdown retransmit #%d (sequence %d): no ACK yet", _retx["attempts"], _retx["seq"])
    _retx["next_at"] = time.monotonic() + _next_gap()


def tx_spool_dir() -> Path:
    return Path(os.environ.get("TX_SPOOL_DIR") or "~/leos-state/tx").expanduser()


def _write_result(tx_dir: Path, request_id: str, status: str, detail: str = "") -> None:
    """Write <id>.result.json atomically so send_cutdown.py never reads a half-written file."""
    result = {"status": status, "detail": detail, "t": time.time()}
    tmp = tx_dir / f"{request_id}.result.tmp"
    tmp.write_text(json.dumps(result))
    os.replace(tmp, tx_dir / f"{request_id}.result.json")


def service_tx_requests(radio, tx_dir: Path, stats: dict, panel_status: dict | None = None) -> None:
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
                _recent_tx[packet.sequence] = time.monotonic()
                if request.get("retry_until_ack"):
                    _start_retx(data, packet.sequence, request, panel_status)

    log = logger.warning if status == "sent" else logger.error
    log("Uplink request %s: %s (%s)", request_id, status, detail)
    _write_result(tx_dir, request_id, status, detail)


def _handle_ack(ack: dict, rssi, snr, status: dict, tx_dir: Path, stats: dict) -> str:
    """The payload acknowledged a command. Only an ACK for a command WE sent recently counts."""
    now = time.monotonic()
    for seq in [q for q, t in _recent_tx.items() if now - t > ACK_MATCH_WINDOW_S]:
        del _recent_tx[seq]
    if ack["sequence"] not in _recent_tx:
        logger.warning("ACK from %s for sequence %d, which we did not send recently: ignored (rssi=%s snr=%s)",
                       ack["callsign"], ack["sequence"], rssi, snr)
        return "ack_unmatched"
    stats["acks"] = stats.get("acks", 0) + 1
    status["ack_at"] = now
    if _retx is not None and ack["sequence"] == _retx["seq"]:
        logger.warning("ACK arrived after %d transmission(s) of sequence %d: retries stopped",
                       _retx["attempts"], ack["sequence"])
        _end_retx(status, "acked")
    cutdown_command.write_ack(tx_dir, {**ack, "rssi": rssi, "snr": snr, "t": time.time()})
    for old in tx_dir.glob("*.ack.json"):               # tidy up ACK files nobody collected
        try:
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink()
        except OSError:
            pass
    logger.warning("ACK RECEIVED from %s for command sequence %d (rssi=%s snr=%s): the payload's radio "
                   "accepted the cutdown command", ack["callsign"], ack["sequence"], rssi, snr)
    return "ack_received"


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


def _is_command(frame: bytes) -> bool:
    try:
        return parse_packet(frame).WhichOneof("payload") == "command"
    except Exception:
        return False


def _close_radio(radio) -> None:
    """Release the pins and SPI port of a real SX1262 so a fresh one can claim them again."""
    try:
        import lgpio

        spi = getattr(radio, "spi", None)
        if spi is not None:
            spi.close()
        handle = getattr(radio, "h", None)
        if handle is not None:
            lgpio.gpiochip_close(handle)
    except Exception:
        pass   # best effort: a radio that is already broken may fail to close


def _receive_loop(radio, queue_path, stop, stats, last_seq, status, tx_dir, plog=None) -> None:
    """Poll the radio until ``stop`` is set (or until the radio raises)."""
    last_stats = time.monotonic()
    last_tx_check = 0.0
    while not stop.is_set():
        pkt = radio.poll()
        if pkt is None:
            if time.monotonic() - last_tx_check >= TX_CHECK_S:
                last_tx_check = time.monotonic()
                service_tx_requests(radio, tx_dir, stats, status)
            service_retransmits(radio, stats, status)
            time.sleep(POLL_SLEEP_S)
        else:
            stats["received"] += 1
            entry = {"rssi": pkt.get("rssi"), "snr": pkt.get("snr"), "crc_ok": pkt.get("crc_ok", True),
                     "len": pkt.get("len"), "hex": pkt.get("hex")}
            if not pkt.get("crc_ok", True):
                stats["bad_crc"] += 1
                entry["outcome"] = "bad_crc"
                logger.warning("Dropping packet with bad CRC (rssi=%s snr=%s)", pkt.get("rssi"), pkt.get("snr"))
            else:
                frame = bytes.fromhex(pkt["hex"])
                entry["len"] = len(frame)
                received_at = datetime.now(timezone.utc)
                ack = cutdown_command.parse_ack(frame)
                if ack is not None:
                    entry["hex"] = None                       # ACK bytes are not logged, like command bytes
                    entry["callsign"], entry["seq"] = ack["callsign"], ack["sequence"]
                    entry["outcome"] = _handle_ack(ack, pkt.get("rssi"), pkt.get("snr"), status, tx_dir, stats)
                    status["last_rx"] = time.monotonic()
                    if pkt.get("rssi") is not None or pkt.get("snr") is not None:
                        queue_writer.enqueue(queue_path, "radio_link", {      # the ACK's signal strength too
                            "time": received_at.isoformat(), "rssi": pkt.get("rssi"), "snr": pkt.get("snr"),
                            "callsign": ack["callsign"], "sequence": ack["sequence"],
                        })
                    if plog is not None:
                        plog.record(entry)
                    continue
                if _is_command(frame):
                    entry["hex"] = None            # the raw bytes would contain the cutdown key
                    entry["outcome"] = "command_heard_bytes_not_logged"
                try:
                    messages = decode_radio_messages(frame, received_at)
                except Exception as exc:
                    entry.setdefault("outcome", f"decode_failed: {exc}")
                    if entry["hex"] is not None:
                        logger.warning("Decode failed (%s) len=%d data=%s", exc, len(frame), frame.hex())
                    else:
                        logger.warning("Decode failed (%s) len=%d (command packet, bytes not logged)", exc, len(frame))
                else:
                    status["last_rx"] = time.monotonic()
                    stats["decoded"] += 1
                    for message_type, payload in messages:
                        queue_writer.enqueue(queue_path, message_type, payload)
                        stats["queued"] += 1

                    callsign, sequence = _packet_identity(frame)
                    if pkt.get("rssi") is not None or pkt.get("snr") is not None:
                        queue_writer.enqueue(queue_path, "radio_link", {
                            "time": received_at.isoformat(), "rssi": pkt.get("rssi"), "snr": pkt.get("snr"),
                            "callsign": callsign, "sequence": sequence,
                        })
                    entry.setdefault("outcome", "decoded:" + "+".join(m for m, _ in messages))
                    entry["callsign"], entry["seq"] = callsign, sequence
                    who = f"{callsign} seq={sequence}" if callsign is not None else "legacy frame"
                    logger.info(
                        "Queued %s from %s (rssi=%s snr=%s)",
                        "+".join(message_type for message_type, _ in messages),
                        who, pkt.get("rssi"), pkt.get("snr"),
                    )
                    if callsign is not None:
                        _track_sequence(last_seq, stats, callsign, sequence)
            if plog is not None:
                plog.record(entry)

        now = time.monotonic()
        if now - last_stats >= STATS_INTERVAL_S:
            logger.info(
                "python radio stats: received=%d decoded=%d queued=%d bad_crc=%d missed=%d restarts=%d duplicates=%d",
                stats["received"], stats["decoded"], stats["queued"], stats["bad_crc"],
                stats["missed"], stats["restarts"], stats["duplicates"],
            )
            last_stats = now


def run_python_radio(
    queue_path: str,
    *,
    radio=None,
    stop: threading.Event | None = None,
) -> dict:
    """Receive forever, decode each packet, and enqueue it for the forwarder.

    This never gives up. If the radio cannot be opened (for example another program holds its GPIO
    pins) or fails while running, it logs the problem, releases the pins, waits, and tries again, so
    the front panel keeps its LEDs and the receiver recovers by itself once the cause is gone.

    ``radio`` (anything with ``poll() -> dict | None``) and ``stop`` exist so tests can drive the
    loop without hardware: a radio passed in is used as is and is never reopened. Returns the final
    counters when ``stop`` is set.
    """
    stop = stop or threading.Event()
    stats = {"received": 0, "decoded": 0, "queued": 0, "bad_crc": 0, "missed": 0, "restarts": 0,
             "duplicates": 0, "radio_reopened": 0}
    last_seq: dict[str, int] = {}
    tx_dir = tx_spool_dir()
    tx_dir.mkdir(parents=True, exist_ok=True)

    plog = packet_log.open_logs()      # text log + packet log on the SD card (LOCAL_LOG_ENABLED=false turns off)

    # Read by the front panel: "hearing the payload" LED and the "radio is down" warning.
    status = {"last_rx": None, "radio_ok": False}
    # Started BEFORE the radio, so the LEDs work (and show a radio fault) even if the radio cannot open.
    try:
        from src.front_panel import start_front_panel

        start_front_panel(stop, status)
    except Exception:
        logger.exception("Front panel did not start; the receiver carries on without it")

    injected = radio is not None
    while not stop.is_set():
        try:
            current = radio if injected else _open_radio()
        except Exception:
            status["radio_ok"] = False
            logger.exception("Could not start the SX1262 radio; trying again in %.0f s", OPEN_RETRY_S)
            stop.wait(OPEN_RETRY_S)
            continue

        status["radio_ok"] = True
        try:
            _receive_loop(current, queue_path, stop, stats, last_seq, status, tx_dir, plog)
        except Exception:
            status["radio_ok"] = False
            logger.exception("The radio loop failed; reopening the radio in %.0f s", OPEN_RETRY_S)
            if injected:
                break
            _close_radio(current)
            stats["radio_reopened"] += 1
            stop.wait(OPEN_RETRY_S)
        else:
            if not injected:
                _close_radio(current)
    if plog is not None:
        plog.close()
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