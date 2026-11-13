#!/usr/bin/env python3
"""Bare-bones receiver: prints every packet the SX1262 hears. No queue, no database.

Run from the ground-station repo root (with the venv active), and STOP index.py first:
only one program can own the radio GPIO pins.

    python3 rx_dump.py
    RADIO_ARGS="--sf 9" python3 rx_dump.py      # same overrides as index.py
"""

import os
import shlex
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from radio.radio_rx import SX1262, build_parser          # same radio code index.py uses
from src.protobuf_decoder import decode_radio_frame, parse_packet


def load_env(path: str = ".env") -> None:
    """Minimal .env reader so RADIO_ARGS etc. match what index.py uses. Existing variables win."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().removeprefix("export ").strip()
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
        print(f"Loaded {path}")
    except FileNotFoundError:
        print(f"No {path} found; using defaults (index.py may use different radio settings)")


def main() -> None:
    load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    print("RADIO_ARGS =", os.environ.get("RADIO_ARGS", "(not set)"))
    args = build_parser().parse_args(shlex.split(os.environ.get("RADIO_ARGS", "")))
    radio = SX1262(args)
    radio.configure()
    print(f"Listening: {args.freq / 1e6:.3f} MHz SF{args.sf} BW{args.bw} sync=0x{args.sync:02X}")
    print("Ctrl-C to stop.\n")

    count = 0
    last_seq = None
    try:
        while True:
            pkt = radio.poll()
            if pkt is None:
                time.sleep(0.002)
                continue

            count += 1
            frame = bytes.fromhex(pkt["hex"])
            print("=" * 72)
            print(f"#{count}  {datetime.now().strftime('%H:%M:%S.%f')[:-3]}  "
                  f"{len(frame)} bytes  rssi={pkt.get('rssi')} snr={pkt.get('snr')} "
                  f"crc_ok={pkt.get('crc_ok', True)}")
            print("raw:", frame.hex())

            if not pkt.get("crc_ok", True):
                print("(bad CRC, skipping decode)")
                continue

            # 1) The protobuf exactly as the radio sent it.
            try:
                packet = parse_packet(frame)
                which = packet.WhichOneof("payload")
                if which == "telemetry":
                    which = f"telemetry/{packet.telemetry.WhichOneof('data')}"
                print(f"callsign={packet.callsign!r} seq={packet.sequence} "
                      f"timestamp_ms={packet.timestamp_ms} -> {which}")
                if last_seq is not None and packet.sequence != last_seq + 1:
                    print(f"  !! sequence jumped {last_seq} -> {packet.sequence}")
                last_seq = packet.sequence
                print(str(packet).rstrip())
            except Exception as exc:
                print(f"protobuf parse FAILED: {exc}")

            # 2) What index.py would put in the queue (this is what reaches the database).
            try:
                message_type, payload = decode_radio_frame(frame, datetime.now(timezone.utc))
                print(f"-> would queue: type={message_type!r}")
                print(f"   payload={payload!r}")
            except Exception as exc:
                print(f"decode_radio_frame FAILED: {exc}")
    except KeyboardInterrupt:
        print(f"\nStopped after {count} packets.")


if __name__ == "__main__":
    main()
