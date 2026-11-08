"""Protobuf (leos-protobufs) radio packet decoding for the ground station.

The Pico transmits a ``leos.Packet`` protobuf message (see leos-protobufs/proto/packet.proto,
branch ``luke``). This module turns it into the same ``(message_type, ingest_payload)`` pair that
``telemetry_decoder.decode_rf_frame_to_ingest_payload`` returns for the legacy 0xA5 frame, so
everything downstream (queue, forwarder, ingest API, TimescaleDB, Grafana) is unchanged.

Schema (current): Packet.telemetry is a TelemetryPacket with optional sub-messages
``purple`` (PurpleboardData, including ``air``), ``beacon`` (GPS + health), ``efm0``/``efm1``
(interval summaries) and ``geiger``. One packet can carry several of these, so decoding returns a
list of ``(message_type, payload)`` pairs: ``purple`` + ``beacon`` become ``sensors_and_gps``,
``efm0`` / ``efm1`` become one ``efm`` row, and ``geiger`` becomes a ``geiger`` row.

Setup: clone leos-protobufs (branch ``luke``) next to this repo, or point LEOS_PROTOBUF_PATH at it.
The generated Python modules need ``protobuf>=7.35.1`` (they check the runtime version on import).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.models import EFM, GEIGER, SENSORS_AND_GPS
from src.telemetry_decoder import SYNC_BYTE, decode_rf_frame_to_ingest_payload

logger = logging.getLogger(__name__)

# gps.proto: fix is 1=no fix, 2=2D, 3=3D
MIN_FIX_FOR_GPS_OK = 2

_packet_pb2 = None


def _protobuf_generated_dir() -> Path:
    root = os.environ.get("LEOS_PROTOBUF_PATH")
    if root:
        return Path(root).expanduser() / "generated" / "python"
    return Path(__file__).resolve().parent.parent.parent / "leos-protobufs" / "generated" / "python"


def _load_packet_module():
    """Import the generated packet_pb2 lazily so the rest of the app runs without it."""
    global _packet_pb2
    if _packet_pb2 is not None:
        return _packet_pb2

    gen_dir = _protobuf_generated_dir()
    if not gen_dir.is_dir():
        raise RuntimeError(
            f"leos-protobufs generated Python not found at {gen_dir}. Clone leos-protobufs "
            "(branch 'luke') next to this repo or set LEOS_PROTOBUF_PATH."
        )
    # The generated modules import each other as top-level modules (e.g. "import telemetry_pb2").
    if str(gen_dir) not in sys.path:
        sys.path.insert(0, str(gen_dir))
    import packet_pb2  # noqa: E402

    _packet_pb2 = packet_pb2
    return _packet_pb2


def protobuf_available() -> bool:
    try:
        _load_packet_module()
    except Exception:  # missing dir, missing protobuf package, runtime version too old
        return False
    return True


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _unix_ms_to_iso(unix_ms: int) -> str | None:
    if unix_ms == 0:
        return None
    return datetime.fromtimestamp(unix_ms / 1000.0, tz=timezone.utc).isoformat()


def parse_packet(data: bytes):
    """Parse raw bytes into a ``leos.Packet`` message. Raises ValueError if it isn't one."""
    packet_pb2 = _load_packet_module()
    from google.protobuf.message import DecodeError

    packet = packet_pb2.Packet()
    try:
        consumed = packet.ParseFromString(data)
    except DecodeError as exc:
        raise ValueError(f"Not a valid protobuf Packet: {exc}") from exc
    if consumed != len(data):
        raise ValueError("Trailing bytes after protobuf Packet")
    if not packet.callsign:
        raise ValueError("Protobuf Packet has no callsign (likely not a LEOS packet)")
    return packet


def _telemetry_to_ingest(telemetry, received_at: datetime) -> dict:
    """Build a sensors_and_gps ingest payload from purple + beacon."""
    payload: dict = {"time": _utc_iso(received_at)}

    if telemetry.HasField("purple"):
        p = telemetry.purple
        payload.update(
            temperature=p.temperature,
            pressure=p.pressure,
            humidity=p.humidity,
            light_lux=p.light_lux,
            gas_resistance=p.gas_resistance,
            # The radio currently sends the raw LTR390 count in the "uvi" field, which matches
            # the "uvs" column. If the firmware later sends a real UV index, add a uvi column.
            uvs=p.uvi,
        )
        if p.HasField("air"):
            a = p.air
            payload.update(
                pm10_env=a.pm10_env,      # PM1.0, ug/m3
                pm25_env=a.pm25_env,      # PM2.5, ug/m3
                pm100_env=a.pm100_env,    # PM10, ug/m3
                aqi_pm25_us=a.aqi_pm25_us,
                aqi_pm100_us=a.aqi_pm100_us,
            )

    if telemetry.HasField("beacon") and telemetry.beacon.HasField("gps"):
        g = telemetry.beacon.gps
        payload.update(
            gps_utc=_unix_ms_to_iso(g.utc_unix_ms),
            gps_fix_ok=g.fix >= MIN_FIX_FOR_GPS_OK,
            gps_lat=g.lat,
            gps_lon=g.lon,
            gps_alt_m=g.alt_m,
            gps_sats_used=g.satellites,
            # g.climb_mps is vertical speed and g.hdop has no column; gps_speed_mps stays unset.
        )
    return payload


def _efm_to_ingest(telemetry, received_at: datetime) -> dict | None:
    """efm0 / efm1 interval summaries -> one efm row. None if neither is present."""
    payload: dict = {"time": _utc_iso(received_at)}
    found = False
    for n, name in ((0, "efm0"), (1, "efm1")):
        if telemetry.HasField(name):
            e = getattr(telemetry, name)
            payload[f"efm{n}_avg_sample"] = e.avg_sample
            payload[f"efm{n}_min_sample"] = e.min_sample
            payload[f"efm{n}_max_sample"] = e.max_sample
            payload[f"efm{n}_hall_events"] = e.hall_events_in_interval
            found = True
    return payload if found else None


def decode_protobuf_messages(data: bytes, received_at: datetime | None = None) -> list[tuple[str, dict]]:
    """Decode a ``leos.Packet`` into a list of ``(message_type, ingest_payload)`` pairs.

    ``time`` is the ground-station receive time: the packet's timestamp_ms is the sender's
    uptime, not UTC. Raises ValueError for undecodable packets and for packets that carry
    nothing the ground station ingests (commands, empty telemetry).
    """
    received_at = received_at or datetime.now(timezone.utc)
    packet = parse_packet(data)

    kind = packet.WhichOneof("payload")
    if kind != "telemetry":
        raise ValueError(f"Unsupported packet payload: {kind!r}")

    telemetry = packet.telemetry
    messages: list[tuple[str, dict]] = []

    if telemetry.HasField("purple") or telemetry.HasField("beacon"):
        messages.append((SENSORS_AND_GPS, _telemetry_to_ingest(telemetry, received_at)))

    efm = _efm_to_ingest(telemetry, received_at)
    if efm is not None:
        messages.append((EFM, efm))

    if telemetry.HasField("geiger"):
        messages.append((GEIGER, {
            "time": _utc_iso(received_at),
            "hits_in_interval": telemetry.geiger.hits_in_interval,
        }))

    if not messages:
        raise ValueError("Telemetry packet carries no purple, beacon, efm or geiger data")
    return messages


def decode_protobuf_packet(data: bytes, received_at: datetime | None = None) -> tuple[str, dict]:
    """First message of a packet (kept for callers that expect one result per packet)."""
    return decode_protobuf_messages(data, received_at)[0]


def decode_radio_frame(frame: bytes, received_at: datetime | None = None) -> tuple[str, dict]:
    """Decode any radio frame: legacy 0xA5 frames keep their old path, everything else is protobuf.

    A protobuf Packet always starts with the callsign tag (0x0A), never 0xA5, so the first byte
    is an unambiguous discriminator.
    """
    if frame[:1] == bytes((SYNC_BYTE,)):
        return decode_rf_frame_to_ingest_payload(frame)
    return decode_protobuf_packet(frame, received_at)


def decode_radio_messages(frame: bytes, received_at: datetime | None = None) -> list[tuple[str, dict]]:
    """Like ``decode_radio_frame`` but returns every message the frame carries."""
    if frame[:1] == bytes((SYNC_BYTE,)):
        return [decode_rf_frame_to_ingest_payload(frame)]
    return decode_protobuf_messages(frame, received_at)


def decode_radio_hex(frame_hex: str, received_at: datetime | None = None) -> tuple[str, dict]:
    return decode_radio_frame(bytes.fromhex(frame_hex.strip()), received_at)