"""Build the cutdown command packet (leos.Packet -> CommandPacket -> CutdownCommand).

This only builds bytes. It never touches the radio, so it is safe to import and test anywhere.
Transmitting is done by send_cutdown.py.

Packet layout (leos-protobufs, branch luke, commit c2fd0a0 or newer):
    Packet { callsign, sequence, timestamp_ms,
             command = CommandPacket { cutdown = CutdownCommand { authorization_key } } }

There is no separate command id or "fire" flag in this schema: a packet that carries a cutdown
command IS the request to cut down, so the radio must check the key before acting. The
``sequence`` field doubles as the anti-replay counter (see new_sequence()).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

CALLSIGN_MAX = 16  # packet.options: callsign max_size 17 including the terminating NUL


def _generated_dir() -> Path:
    root = os.environ.get("LEOS_PROTOBUF_PATH")
    if root:
        return Path(root).expanduser() / "generated" / "python"
    return Path(__file__).resolve().parent.parent / "leos-protobufs" / "generated" / "python"


def _load_packet_module():
    gen_dir = _generated_dir()
    if not gen_dir.is_dir():
        raise RuntimeError(
            f"leos-protobufs generated Python not found at {gen_dir}. "
            "Clone leos-protobufs (branch 'luke') next to this repo or set LEOS_PROTOBUF_PATH."
        )
    if str(gen_dir) not in sys.path:
        sys.path.insert(0, str(gen_dir))
    import packet_pb2

    return packet_pb2


def new_sequence() -> int:
    """A sequence number that always increases between runs: whole seconds since the Unix epoch.

    The radio should accept a command only if its sequence is higher than the last one it
    accepted from this callsign, so a recorded packet cannot simply be replayed. Fits in
    uint32 until the year 2106.
    """
    return int(time.time())


def build_cutdown_packet(*, authorization_key: int, callsign: str, sequence: int | None = None) -> bytes:
    """Serialize a cutdown command into the bytes that go over the radio."""
    if not 0 < authorization_key <= 0xFFFFFFFF:
        # 0 is rejected on purpose: proto3 leaves a zero out, so it is indistinguishable from "no key".
        raise ValueError("authorization_key must be a non-zero 32-bit number")
    if not callsign or len(callsign) > CALLSIGN_MAX or not callsign.isascii():
        raise ValueError(f"callsign must be 1 to {CALLSIGN_MAX} ASCII characters")
    sequence = new_sequence() if sequence is None else sequence
    if not 0 < sequence <= 0xFFFFFFFF:
        raise ValueError("sequence must be a positive 32-bit number")

    packet_pb2 = _load_packet_module()
    packet = packet_pb2.Packet()
    packet.callsign = callsign
    packet.sequence = sequence
    packet.timestamp_ms = int(time.monotonic() * 1000)  # sender uptime, not UTC (per packet.proto)
    packet.command.cutdown.authorization_key = authorization_key
    return packet.SerializeToString()


def describe(data: bytes) -> str:
    """Human-readable summary of a command packet, for the confirmation prompt and tests."""
    packet = _load_packet_module().Packet()
    packet.ParseFromString(data)
    if packet.WhichOneof("payload") != "command":
        return f"not a command packet (payload={packet.WhichOneof('payload')})"
    which = packet.command.WhichOneof("data")
    if which != "cutdown":
        return f"command from {packet.callsign}: {which}"
    key = packet.command.cutdown.authorization_key
    return f"CUTDOWN from {packet.callsign} | sequence={packet.sequence} | key={'set' if key else 'MISSING'}"