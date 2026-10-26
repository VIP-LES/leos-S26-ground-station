"""Protobuf (leos-protobufs) radio packet decoding for the ground station.

The Pico transmits a ``leos.Packet`` protobuf message (see leos-protobufs/proto/packet.proto).
This module turns it into the same ``(message_type, ingest_payload)`` pair that
``telemetry_decoder.decode_rf_frame_to_ingest_payload`` returns for the legacy 0xA5 frame, so
everything downstream (queue, forwarder, ingest API, TimescaleDB, Grafana) is unchanged.

Setup: clone leos-protobufs (branch ``luke``) next to this repo, or point LEOS_PROTOBUF_PATH at it.
The generated Python modules need ``protobuf>=7.35.1`` (they check the runtime version on import).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.models import EFM, SENSORS_AND_GPS
from src.telemetry_decoder import SYNC_BYTE, decode_rf_frame_to_ingest_payload

logger = logging.getLogger(__name__)

# gps.proto: fix is 1=no fix, 2=2D, 3=3D
MIN_FIX_FOR_GPS_OK = 2
# efm.proto ADC.id -> columns in efm_data. The firmware's id numbering (1-based assumed) is
# not documented in the .proto, so ids outside this map are skipped with a warning.
EFM_ADC_ID_TO_PREFIX = {1: "adc1", 2: "adc2"}

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


def _low_rate_to_ingest(low_rate, received_at: datetime) -> dict:
    payload: dict = {"time": _utc_iso(received_at)}

    if low_rate.HasField("sensors"):
        s = low_rate.sensors
        payload.update(
            temperature=s.temperature,
            pressure=s.pressure,
            humidity=s.humidity,
            light_lux=s.light_lux,
            gas_resistance=s.gas_resistance,
            # s.uvi (UV index) has no column yet; ltr390 "uvs" is a different quantity.
        )

    if low_rate.HasField("beacon") and low_rate.beacon.HasField("gps"):
        g = low_rate.beacon.gps
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


def _efm_to_ingest(efm, received_at: datetime) -> dict:
    payload: dict = {"time": _utc_iso(received_at)}
    for adc in efm.adc:
        prefix = EFM_ADC_ID_TO_PREFIX.get(adc.id)
        if prefix is None:
            logger.warning("Skipping EFM ADC with unexpected id %d", adc.id)
            continue
        payload[f"{prefix}_ch1_diff"] = adc.ch1_diff
        payload[f"{prefix}_ch4_breakbeam"] = adc.ch4_breakbeam
        # ch2_phase1 / ch3_phase2 have no columns in efm_data.
    return payload


def decode_protobuf_packet(data: bytes, received_at: datetime | None = None) -> tuple[str, dict]:
    """Decode a ``leos.Packet`` into ``(message_type, ingest_payload)``.

    ``time`` is the ground-station receive time: the packet's timestamp_ms is the sender's
    uptime, not UTC. Raises ValueError for undecodable packets and for payload kinds the
    ground station does not ingest (commands, empty payloads).
    """
    received_at = received_at or datetime.now(timezone.utc)
    packet = parse_packet(data)

    kind = packet.WhichOneof("payload")
    if kind != "telemetry":
        raise ValueError(f"Unsupported packet payload: {kind!r}")

    telemetry = packet.telemetry
    data_kind = telemetry.WhichOneof("data")
    if data_kind == "low_rate":
        return SENSORS_AND_GPS, _low_rate_to_ingest(telemetry.low_rate, received_at)
    if data_kind == "efm":
        return EFM, _efm_to_ingest(telemetry.efm, received_at)
    raise ValueError(f"Unsupported telemetry data: {data_kind!r}")


def decode_radio_frame(frame: bytes, received_at: datetime | None = None) -> tuple[str, dict]:
    """Decode any radio frame: legacy 0xA5 frames keep their old path, everything else is protobuf.

    A protobuf Packet always starts with the callsign tag (0x0A), never 0xA5, so the first byte
    is an unambiguous discriminator.
    """
    if frame[:1] == bytes((SYNC_BYTE,)):
        return decode_rf_frame_to_ingest_payload(frame)
    return decode_protobuf_packet(frame, received_at)


def decode_radio_hex(frame_hex: str, received_at: datetime | None = None) -> tuple[str, dict]:
    return decode_radio_frame(bytes.fromhex(frame_hex.strip()), received_at)