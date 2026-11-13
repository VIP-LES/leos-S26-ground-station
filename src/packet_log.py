"""Local logs on the Pi's SD card, so there is a record even with no network.

Two files, both under LOG_DIR (default ~/leos-state/logs):

  groundstation.log     every message the program logs (receiver, front panel, uplink, errors), as text.
                        Rotates at 5 MB and keeps 10 files, so at most about 55 MB.
  rx-YYYY-MM-DD.jsonl   one JSON line per radio packet heard: time, RSSI, SNR, size, the raw bytes (hex), and what
                        the decoder made of it. One file per UTC day; the oldest files are deleted once the
                        total passes PACKET_LOG_MAX_MB (default 500).

Command packets heard on the air are logged WITHOUT their raw bytes, because those bytes contain the cutdown
authorization key in the clear.

The Pi has no battery clock, so with no network its wall-clock time can be wrong or jump. Every packet line
therefore also carries "mono_s" (seconds since boot, which never jumps) so events can always be put in order.

Disable both with LOCAL_LOG_ENABLED=false.
"""

from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

TEXT_LOG_NAME = "groundstation.log"
TEXT_LOG_BYTES = 5 * 1024 * 1024
TEXT_LOG_BACKUPS = 10
DEFAULT_PACKET_LOG_MB = 500
FSYNC_EVERY_S = 5.0          # flush to the card at most this often: durable enough, gentle on the SD card
PRUNE_EVERY_S = 3600.0


def enabled() -> bool:
    return os.environ.get("LOCAL_LOG_ENABLED", "true").strip().lower() != "false"


def log_dir() -> Path:
    return Path(os.environ.get("LOG_DIR") or "~/leos-state/logs").expanduser()


def install_text_log(directory: Path | None = None) -> Path | None:
    """Also write everything the program logs to a rotating file. Safe to call more than once."""
    directory = directory or log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / TEXT_LOG_NAME
        root = logging.getLogger()
        for h in root.handlers:
            if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path.resolve():
                return path
        handler = RotatingFileHandler(path, maxBytes=TEXT_LOG_BYTES, backupCount=TEXT_LOG_BACKUPS)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        return path
    except OSError as exc:
        logger.error("Could not start the text log in %s: %s", directory, exc)
        return None


class PacketLog:
    """Append-only, daily-rotating JSON-lines record of every radio packet heard."""

    def __init__(self, directory: Path | None = None, *, max_total_bytes: int | None = None,
                 clock=time.time, mono=time.monotonic):
        self.directory = directory or log_dir()
        mb = float(os.environ.get("PACKET_LOG_MAX_MB", DEFAULT_PACKET_LOG_MB))
        self.max_total_bytes = int(mb * 1024 * 1024) if max_total_bytes is None else max_total_bytes
        self._clock, self._mono = clock, mono
        self._file = None
        self._day = None
        self._last_fsync = 0.0
        self._last_prune = float("-inf")
        self._warned_at = float("-inf")

    def _path_for(self, day: str) -> Path:
        return self.directory / f"rx-{day}.jsonl"

    def _open(self, day: str) -> None:
        self.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path_for(day), "a", encoding="utf-8")
        self._day = day

    def prune(self) -> None:
        """Delete the oldest daily files until the total is under the cap (the newest file always stays)."""
        files = sorted(self.directory.glob("rx-*.jsonl"))
        total = sum(f.stat().st_size for f in files)
        while len(files) > 1 and total > self.max_total_bytes:
            oldest = files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)

    def record(self, entry: dict) -> None:
        """Write one line. Never raises: a full or failing card must not stop the receiver."""
        try:
            now = self._clock()
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            if day != self._day or self._file is None:
                self._open(day)
            line = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "mono_s": round(self._mono(), 3), **entry}
            self._file.write(json.dumps(line, separators=(",", ":")) + "\n")
            self._file.flush()
            mono = self._mono()
            if mono - self._last_fsync >= FSYNC_EVERY_S:
                os.fsync(self._file.fileno())
                self._last_fsync = mono
            if mono - self._last_prune >= PRUNE_EVERY_S:
                self.prune()
                self._last_prune = mono
        except OSError as exc:
            mono = self._mono()
            if mono - self._warned_at > 60:
                logger.error("Packet log write failed (%s); continuing without it", exc)
                self._warned_at = mono

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
            self._day = None


def open_logs() -> PacketLog | None:
    """Start the text log and return the packet log, or None if local logging is switched off."""
    if not enabled():
        return None
    install_text_log()
    return PacketLog()