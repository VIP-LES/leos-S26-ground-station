#!/usr/bin/env python3
"""Send a cutdown command. DRY RUN by default: nothing is transmitted unless you pass --transmit.

    CUTDOWN_AUTH_KEY=0x1234ABCD python3 send_cutdown.py --callsign MYCALL            # dry run: shows the packet
    CUTDOWN_AUTH_KEY=0x1234ABCD python3 send_cutdown.py --callsign MYCALL --transmit  # asks, then sends

How it sends: the receiver (index.py) owns the radio, so this script does not touch the radio.
It writes a request file into TX_SPOOL_DIR (default ~/leos-state/tx) and waits for the receiver
to transmit it and report back. The receiver must be running with RADIO_TX_ENABLED=true.
A request expires after --expires seconds, so it can never fire late.

The key is read from the CUTDOWN_AUTH_KEY environment variable (decimal or 0x hex), or typed at a
hidden prompt. It is never accepted as a command-line argument, so it does not end up in the
shell history or in `ps` output.
"""

import argparse
import getpass
import json
import os
import sys
import time
import uuid
from pathlib import Path

import cutdown_command

CONFIRM_WORD = "CUTDOWN"


def tx_spool_dir() -> Path:
    return Path(os.environ.get("TX_SPOOL_DIR") or "~/leos-state/tx").expanduser()


def read_key() -> int:
    raw = os.environ.get("CUTDOWN_AUTH_KEY") or getpass.getpass("Authorization key (hidden): ")
    try:
        return int(raw.strip(), 0)
    except ValueError:
        sys.exit("The authorization key must be a number (decimal or 0x hex).")


def write_request(data: bytes, tx_dir: Path, expires_s: float) -> tuple[str, Path]:
    """Queue a packet for the receiver to transmit. Returns (request id, request file path)."""
    tx_dir.mkdir(parents=True, exist_ok=True)
    request_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    tmp = tx_dir / f"{request_id}.tmp"
    tmp.write_text(json.dumps({"hex": data.hex(), "expires_at": time.time() + expires_s}))
    final = tx_dir / f"{request_id}.request.json"
    os.replace(tmp, final)   # atomic: the receiver never sees a half-written request
    return request_id, final


def wait_for_result(tx_dir: Path, request_id: str, wait_s: float) -> dict | None:
    result_path = tx_dir / f"{request_id}.result.json"
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if result_path.exists():
            result = json.loads(result_path.read_text())
            result_path.unlink(missing_ok=True)
            return result
        time.sleep(0.2)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Build (and optionally send) a cutdown command.")
    ap.add_argument("--callsign", required=True, help="your callsign, at most 16 ASCII characters")
    ap.add_argument("--transmit", action="store_true", help="actually send it (default is a dry run)")
    ap.add_argument("--expires", type=float, default=30.0, help="seconds before the request is void (default 30)")
    args = ap.parse_args()

    data = cutdown_command.build_cutdown_packet(authorization_key=read_key(), callsign=args.callsign)
    print(cutdown_command.describe(data))
    print(f"{len(data)} bytes: {data.hex()}")

    if not args.transmit:
        print("\nDRY RUN: nothing was transmitted. Add --transmit to send it.")
        return

    print("\nThis will command the payload to CUT DOWN.")
    if input(f"Type {CONFIRM_WORD} to send, anything else to cancel: ").strip() != CONFIRM_WORD:
        sys.exit("Cancelled. Nothing was sent.")

    tx_dir = tx_spool_dir()
    request_id, request_path = write_request(data, tx_dir, args.expires)
    print(f"Request {request_id} queued, waiting for the receiver to transmit it...")
    result = wait_for_result(tx_dir, request_id, args.expires + 5)
    if result is None:
        request_path.unlink(missing_ok=True)    # make sure it cannot be sent after we give up
        sys.exit("No answer from the receiver. The request was withdrawn and nothing was sent.\n"
                 "Is index.py running in pi mode with RADIO_TX_ENABLED=true?")

    print(f"Result: {result['status']}: {result['detail']}")
    if result["status"] != "sent":
        sys.exit(1)
    print("Transmitted. The radio gives no acknowledgement; check the payload's telemetry before assuming it worked.")


if __name__ == "__main__":
    main()