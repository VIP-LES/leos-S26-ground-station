"""Entry point for Pi-mode forwarding and lab-mode ingest."""

import os
import threading
import logging
import time
from pathlib import Path

from dotenv import load_dotenv


def load_environment() -> list[str]:
    repo_root = Path(__file__).resolve().parent
    loaded: list[str] = []
    env_files = (
        repo_root / ".env",
        repo_root / ".env.pi-local",
    )

    for env_file in env_files:
        if env_file.exists():
            load_dotenv(env_file, override=True)
            loaded.append(env_file.name)

    return loaded


LOADED_ENV_FILES = load_environment()

import uvicorn  # noqa: E402 — must import after load_dotenv

from src import db  # noqa: E402
from src.native_radio import run_native_radio  # noqa: E402
from src.forwarder import run_forwarder  # noqa: E402
from src.ingest_api import app  # noqa: E402
from src.queue_writer import init_queue  # noqa: E402
from src.python_radio import run_python_radio  # noqa: E402
from src.replay_radio import run_replay_radio  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    if LOADED_ENV_FILES:
        logger.info("Loaded environment file(s): %s", ", ".join(LOADED_ENV_FILES))
    else:
        logger.info("No environment file found; using process environment only")

    mode = os.environ.get("GROUNDSTATION_MODE", "lab").strip().lower()
    if mode == "lab":
        run_lab_mode()
        return
    if mode == "pi":
        run_pi_mode()
        return
    raise ValueError(f"Unsupported GROUNDSTATION_MODE: {mode}")


def run_lab_mode() -> None:
    port = int(os.environ.get("INGEST_PORT", "4000"))
    db.connect()
    db.ensure_schema()
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    finally:
        db.close()


def run_pi_mode() -> None:
    queue_path = os.environ.get("SPOOL_DB_PATH", "/home/pi/leos-state/spool.db")
    ingest_url = os.environ.get("INGEST_URL", "http://localhost:4000")
    pi_local_only = os.environ.get("GROUNDSTATION_PI_LOCAL_ONLY", "false").lower() == "true"
    radio_source = os.environ.get("RADIO_SOURCE", "replay").strip().lower()
    replay_input_path = os.environ.get("REPLAY_RADIO_INPUT_PATH")
    replay_repeat = os.environ.get("REPLAY_RADIO_REPEAT", "false").lower() == "true"
    replay_interval_s = float(os.environ.get("REPLAY_RADIO_INTERVAL_S", "0"))
    radio_socket_path = os.environ.get("RADIO_SOCKET_PATH", "/tmp/leos-radio.sock")
    radio_receiver_bin = os.environ.get(
        "RADIO_RECEIVER_BIN",
        "/home/pi/leos-S26-ground-station/native/build/radio_receiver",
    )
    radio_autostart = os.environ.get("RADIO_RECEIVER_AUTOSTART", "true").lower() == "true"

    producer_thread = None
    init_queue(queue_path)

    if radio_source == "replay" and replay_input_path:
        producer_thread = threading.Thread(
            target=run_replay_radio,
            kwargs={
                "queue_path": queue_path,
                "input_path": replay_input_path,
                "repeat": replay_repeat,
                "interval_s": replay_interval_s,
            },
            daemon=True,
        )
        producer_thread.start()
        logger.info("Replay radio source started from %s", replay_input_path)
    elif radio_source == "native":
        producer_thread = threading.Thread(
            target=run_native_radio,
            kwargs={
                "queue_path": queue_path,
                "socket_path": radio_socket_path,
                "receiver_bin": radio_receiver_bin,
                "autostart": radio_autostart,
            },
            daemon=True,
        )
        producer_thread.start()
        logger.info("Native radio receiver started via %s", radio_socket_path)
    elif radio_source == "python":
        producer_thread = threading.Thread(
            target=run_python_radio,
            kwargs={"queue_path": queue_path},
            daemon=True,
        )
        producer_thread.start()
        logger.info("Python SX1262 radio source started (protobuf decode)")
    else:
        logger.info("Pi mode started without an active radio source; forwarder will idle")

    if pi_local_only:
        logger.info("Pi local-only mode enabled; telemetry will remain in local SQLite at %s", queue_path)
        if producer_thread is not None:
            producer_thread.join()
            return

        while True:
            time.sleep(1.0)

    run_forwarder(queue_path, ingest_url)

if __name__ == "__main__":
    main()