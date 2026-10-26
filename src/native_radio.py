"""Native Pi-side radio source backed by the SX126x receiver process."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass

from src import queue_writer
from src.protobuf_decoder import decode_radio_frame

logger = logging.getLogger(__name__)

IPC_MSG_RX_PACKET = 0x01
IPC_MSG_BUTTON_EVENT = 0x02
IPC_MSG_TX_PACKET = 0x03
IPC_MSG_TX_RESULT = 0x04
HEARTBEAT_INTERVAL_S = 1.0


@dataclass
class NativeRadioStats:
    ipc_rx_packets: int = 0
    ipc_rx_bytes: int = 0
    decode_ok: int = 0
    decode_fail: int = 0
    queue_ok: int = 0
    button_events: int = 0
    tx_results: int = 0
    short_messages: int = 0
    unknown_messages: int = 0


def _log_heartbeat(stats: NativeRadioStats, previous: NativeRadioStats) -> NativeRadioStats:
    logger.info(
        "native heartbeat delta{ipc_rx=%d ipc_bytes=%d decode_ok=%d decode_fail=%d queue_ok=%d tx_results=%d button=%d short=%d unknown=%d} "
        "total{ipc_rx=%d ipc_bytes=%d decode_ok=%d decode_fail=%d queue_ok=%d tx_results=%d button=%d short=%d unknown=%d}",
        stats.ipc_rx_packets - previous.ipc_rx_packets,
        stats.ipc_rx_bytes - previous.ipc_rx_bytes,
        stats.decode_ok - previous.decode_ok,
        stats.decode_fail - previous.decode_fail,
        stats.queue_ok - previous.queue_ok,
        stats.tx_results - previous.tx_results,
        stats.button_events - previous.button_events,
        stats.short_messages - previous.short_messages,
        stats.unknown_messages - previous.unknown_messages,
        stats.ipc_rx_packets,
        stats.ipc_rx_bytes,
        stats.decode_ok,
        stats.decode_fail,
        stats.queue_ok,
        stats.tx_results,
        stats.button_events,
        stats.short_messages,
        stats.unknown_messages,
    )
    return NativeRadioStats(**stats.__dict__)


def _maybe_log_heartbeat(
    stats: NativeRadioStats,
    previous: NativeRadioStats,
    last_heartbeat: float,
) -> tuple[NativeRadioStats, float]:
    now = time.monotonic()
    if (now - last_heartbeat) >= HEARTBEAT_INTERVAL_S:
        return _log_heartbeat(stats, previous), now
    return previous, last_heartbeat


@dataclass
class RadioReceiverClient:
    sock: socket.socket
    process: subprocess.Popen[str] | None = None

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            if self.process is not None:
                self.process.terminate()
                self.process.wait(timeout=2)

    def send_tx_packet(self, radio_id: int, frame: bytes) -> None:
        self.sock.send(bytes((IPC_MSG_TX_PACKET, radio_id)) + frame)


def _connect_seqpacket(socket_path: str, timeout_s: float = 5.0) -> socket.socket:
    deadline = time.monotonic() + timeout_s

    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            sock.connect(socket_path)
            sock.settimeout(HEARTBEAT_INTERVAL_S)
            return sock
        except OSError:
            sock.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def start_receiver_client(
    socket_path: str,
    *,
    receiver_bin: str | None = None,
    autostart: bool = True,
) -> RadioReceiverClient:
    process = None

    logger.info(
        "starting native receiver client socket=%s autostart=%s receiver_bin=%s",
        socket_path,
        autostart,
        receiver_bin,
    )

    if autostart:
        if receiver_bin is None:
            raise ValueError("receiver_bin is required when autostart is enabled")

        process = subprocess.Popen(
            [receiver_bin],
            stdout=subprocess.DEVNULL,
            stderr=None,
            env=os.environ.copy(),
        )

    sock = _connect_seqpacket(socket_path)
    logger.info("connected to native receiver socket=%s", socket_path)
    return RadioReceiverClient(sock=sock, process=process)


def run_native_radio(
    queue_path: str,
    socket_path: str,
    *,
    receiver_bin: str | None = None,
    autostart: bool = True,
) -> None:
    stats = NativeRadioStats()
    last_heartbeat = time.monotonic()
    last_stats = NativeRadioStats()
    client = start_receiver_client(
        socket_path,
        receiver_bin=receiver_bin,
        autostart=autostart,
    )

    try:
        while True:
            try:
                message = client.sock.recv(512)
            except socket.timeout:
                last_stats, last_heartbeat = _maybe_log_heartbeat(stats, last_stats, last_heartbeat)
                continue

            if not message:
                raise RuntimeError("radio receiver socket closed")

            stats.ipc_rx_packets += 1
            stats.ipc_rx_bytes += len(message)
            if len(message) < 2:
                stats.short_messages += 1
                logger.warning("Ignoring short IPC message of %d bytes", len(message))
                last_stats, last_heartbeat = _maybe_log_heartbeat(stats, last_stats, last_heartbeat)
                continue

            message_kind = message[0]
            radio_id = message[1]

            if message_kind == IPC_MSG_RX_PACKET:
                frame = message[2:]
                logger.debug("RX frame from radio %d len=%d", radio_id, len(frame))
                try:
                    message_type, payload = decode_radio_frame(frame)
                except Exception:
                    stats.decode_fail += 1
                    logger.exception(
                        "Failed to decode frame from radio %d len=%d data=%s",
                        radio_id,
                        len(frame),
                        frame.hex(),
                    )
                    last_stats, last_heartbeat = _maybe_log_heartbeat(stats, last_stats, last_heartbeat)
                    continue

                stats.decode_ok += 1
                queue_writer.enqueue(queue_path, message_type, payload)
                stats.queue_ok += 1
                logger.info("Queued radio %d %s payload at %s", radio_id, message_type, payload["time"])
                last_stats, last_heartbeat = _maybe_log_heartbeat(stats, last_stats, last_heartbeat)
                continue

            if message_kind == IPC_MSG_BUTTON_EVENT:
                stats.button_events += 1
                logger.info("Ignoring button event %d", radio_id)
                last_stats, last_heartbeat = _maybe_log_heartbeat(stats, last_stats, last_heartbeat)
                continue

            if message_kind == IPC_MSG_TX_RESULT:
                stats.tx_results += 1
                logger.info(
                    "TX result from radio %d status=%d",
                    radio_id,
                    message[2] if len(message) > 2 else -1,
                )
                last_stats, last_heartbeat = _maybe_log_heartbeat(stats, last_stats, last_heartbeat)
                continue

            stats.unknown_messages += 1
            logger.warning("Ignoring unknown IPC message kind 0x%02x", message_kind)
            last_stats, last_heartbeat = _maybe_log_heartbeat(stats, last_stats, last_heartbeat)
    finally:
        logger.info("closing native receiver client")
        client.close()
