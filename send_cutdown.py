#!/usr/bin/env python3
"""Send a cutdown command. DRY RUN by default: nothing is transmitted unless you pass --transmit.

    CUTDOWN_AUTH_KEY=0x1234ABCD python3 send_cutdown.py --callsign MYCALL            # dry run: shows the packet
    CUTDOWN_AUTH_KEY=0x1234ABCD python3 send_cutdown.py --callsign MYCALL --transmit  # asks, then sends

How it sends: the receiver (index.py) owns the radio, so this script does not touch the radio.
It writes a request file into TX_SPOOL_DIR (default ~/leos-state/tx) and waits for the receiver
to transmit it and report back. The receiver must be running with RADIO_TX_ENABLED=true.
A request expires after --expires seconds, so it can never fire late. After it is sent, the script waits
(--ack-wait) for the payload's ACK: the same packet echoed back with the ACK key (cutdown_command.ACK_KEY).
With --retry-for N the receiver sends the same packet again every ~2 s until the ACK arrives, for up to N seconds.

The key is read from the CUTDOWN_AUTH_KEY environment variable (decimal or 0x hex), or typed at a
hidden prompt. It is never accepted as a command-line argument, so it does not end up in the
shell history or in `ps` output.
"""

import argparse
import getpass
import os
import sys

import cutdown_command

CONFIRM_WORD = "CUTDOWN"


def read_key() -> int:
    raw = os.environ.get("CUTDOWN_AUTH_KEY") or getpass.getpass("Authorization key (hidden): ")
    try:
        return int(raw.strip(), 0)
    except ValueError:
        sys.exit("The authorization key must be a number (decimal or 0x hex).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build (and optionally send) a cutdown command.")
    ap.add_argument("--callsign", required=True, help="your callsign, at most 16 ASCII characters")
    ap.add_argument("--transmit", action="store_true", help="actually send it (default is a dry run)")
    ap.add_argument("--ack-wait", type=float, default=5.0, help="seconds to wait for the payload's ACK (default 5)")
    ap.add_argument("--expires", type=float, default=30.0, help="seconds before the request is void (default 30)")
    ap.add_argument("--retry-for", type=float, default=0.0, metavar="SECONDS",
                    help="keep retransmitting every ~2 s until the ACK arrives, for up to this long (default: send once)")
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

    tx_dir = cutdown_command.tx_spool_dir()
    request_id, request_path = cutdown_command.write_request(
        data, tx_dir, args.expires, retry_until_ack=args.retry_for > 0, retry_max_s=args.retry_for or None)
    print(f"Request {request_id} queued, waiting for the receiver to transmit it...")
    result = cutdown_command.wait_for_result(tx_dir, request_id, args.expires + 5)
    if result is None:
        request_path.unlink(missing_ok=True)    # make sure it cannot be sent after we give up
        sys.exit("No answer from the receiver. The request was withdrawn and nothing was sent.\n"
                 "Is index.py running in pi mode with RADIO_TX_ENABLED=true?")

    print(f"Result: {result['status']}: {result['detail']}")
    if result["status"] != "sent":
        sys.exit(1)
    sequence = cutdown_command.packet_sequence(data)
    wait_s = args.retry_for + 2 if args.retry_for > 0 else args.ack_wait
    if args.retry_for > 0:
        print(f"Transmitted. Retransmitting about every 2 s until the ACK arrives, for up to {args.retry_for:g} s "
              f"(sequence {sequence})...")
    else:
        print(f"Transmitted. Waiting up to {args.ack_wait:g} s for the payload's ACK (sequence {sequence})...")
    ack = cutdown_command.wait_for_ack(tx_dir, sequence, wait_s)
    if ack is None:
        print("NO ACK received. Either the payload did not hear the command, it rejected it (wrong key), or its "
              "reply was lost.\nThat does not prove it was ignored: check the payload's telemetry and its own log.")
        sys.exit(2)
    print(f"ACK received from {ack['callsign']} (rssi={ack.get('rssi')} snr={ack.get('snr')}): the payload's radio "
          "accepted the command.\nThis does not confirm the cutdown itself happened: check telemetry and the payload.")


if __name__ == "__main__":
    main()