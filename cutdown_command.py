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

import json
import os
import sys
import time
import uuid
from pathlib import Path

CALLSIGN_MAX = 16  # packet.options: callsign max_size 17 including the terminating NUL

# The payload's radio acknowledges an authorized cutdown command by sending the SAME packet back (same
# callsign and sequence) with this key in place of the real one. It is a command packet on the wire, so the
# only thing that makes it an ACK is this value. It must never equal the key the payload accepts as a command.
ACK_KEY = 0xBEEFDEAD


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


def tx_spool_dir() -> Path:
    """Folder where requests for the receiver to transmit are dropped."""
    return Path(os.environ.get("TX_SPOOL_DIR") or "~/leos-state/tx").expanduser()


def _sequence_file() -> Path:
    return Path(os.environ.get("CUTDOWN_SEQ_FILE") or "~/leos-state/cutdown_seq").expanduser()


def new_sequence(state_file: Path | None = None) -> int:
    """A sequence number that always increases, even if the clock goes backwards.

    The Pi has no battery-backed clock, so with no network its time can restart from an old
    value after a reboot. The sequence is therefore max(Unix time, last used + 1), and the last
    value is kept in a small file. A receiver that rejects old sequences can then never see a
    new command look older than the previous one.
    """
    path = state_file or _sequence_file()
    last = 0
    try:
        last = int(path.read_text().strip())
    except (OSError, ValueError):
        pass
    sequence = max(int(time.time()), last + 1)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(str(sequence))
        os.replace(tmp, path)
    except OSError:
        pass   # never block a cutdown because the counter file could not be written
    return sequence


def write_request(data: bytes, tx_dir: Path, expires_s: float) -> tuple[str, Path]:
    """Queue a packet for the receiver to transmit. Returns (request id, request file path)."""
    tx_dir.mkdir(parents=True, exist_ok=True)
    request_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    tmp = tx_dir / f"{request_id}.tmp"
    tmp.write_text(json.dumps({"hex": data.hex(), "expires_at": time.time() + expires_s}))
    final = tx_dir / f"{request_id}.request.json"
    os.replace(tmp, final)   # atomic: the receiver never sees a half-written request
    return request_id, final


def check_result(tx_dir: Path, request_id: str) -> dict | None:
    """The receiver's answer for a request, or None if it has not answered yet."""
    result_path = tx_dir / f"{request_id}.result.json"
    if not result_path.exists():
        return None
    try:
        result = json.loads(result_path.read_text())
    except (OSError, ValueError):
        return None   # caught mid-write; try again on the next call
    result_path.unlink(missing_ok=True)
    return result


def wait_for_result(tx_dir: Path, request_id: str, wait_s: float) -> dict | None:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        result = check_result(tx_dir, request_id)
        if result is not None:
            return result
        time.sleep(0.2)
    return None


def build_cutdown_packet(*, authorization_key: int, callsign: str, sequence: int | None = None) -> bytes:
    """Serialize a cutdown command into the bytes that go over the radio."""
    if authorization_key == ACK_KEY:
        raise ValueError("that key is reserved for the payload's acknowledgement, not for commands")
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


def packet_sequence(data: bytes) -> int:
    """The sequence number inside a packet (used to match the payload's ACK to the command sent)."""
    packet = _load_packet_module().Packet()
    packet.ParseFromString(data)
    return packet.sequence


def parse_ack(frame: bytes) -> dict | None:
    """If ``frame`` is the payload's acknowledgement, return {callsign, sequence}; otherwise None. Never raises."""
    try:
        packet = _load_packet_module().Packet()
        if packet.ParseFromString(frame) != len(frame):
            return None
        if packet.WhichOneof("payload") != "command" or packet.command.WhichOneof("data") != "cutdown":
            return None
        if packet.command.cutdown.authorization_key != ACK_KEY:
            return None
        return {"callsign": packet.callsign, "sequence": packet.sequence}
    except Exception:
        return None


def write_ack(tx_dir: Path, ack: dict) -> None:
    """Record a matched ACK for send_cutdown.py to pick up. Atomic, like the other spool files."""
    tx_dir.mkdir(parents=True, exist_ok=True)
    tmp = tx_dir / f"{ack['sequence']}.ack.tmp"
    tmp.write_text(json.dumps(ack))
    os.replace(tmp, tx_dir / f"{ack['sequence']}.ack.json")


def check_ack(tx_dir: Path, sequence: int) -> dict | None:
    path = tx_dir / f"{sequence}.ack.json"
    if not path.exists():
        return None
    try:
        ack = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    path.unlink(missing_ok=True)
    return ack


def wait_for_ack(tx_dir: Path, sequence: int, wait_s: float) -> dict | None:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        ack = check_ack(tx_dir, sequence)
        if ack is not None:
            return ack
        time.sleep(0.1)
    return None