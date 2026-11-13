"""Forward structured queued payloads to the lab-hosted ingest API."""

from __future__ import annotations

import json
import logging
import time

import requests

from src import queue_writer
from src.models import INGEST_PATHS

logger = logging.getLogger(__name__)
_session = requests.Session() 

def _post_row(message_type: str, payload: dict, ingest_url: str) -> str:
    endpoint = INGEST_PATHS[message_type]
    try:
        resp = _session.post(f"{ingest_url.rstrip('/')}{endpoint}", json=payload, timeout=5)
    except requests.RequestException as exc:
        logger.error("Forwarder POST failed: %s", exc)
        return "retry"

    if resp.status_code in (200, 201):
        return "sent"
    if 400 <= resp.status_code < 500:
        logger.warning("Dropping malformed %s payload: %s", message_type, resp.text)
        return "drop"

    logger.warning("Ingest API error %d: %s", resp.status_code, resp.text)
    return "retry"


def run_forwarder(queue_path: str, ingest_url: str, poll_interval_s: float = 0.5) -> None:
    while True:
        try:
            rows = queue_writer.fetch_unsent(queue_path, limit=100)
            if not rows:
                time.sleep(poll_interval_s)
                continue

            for row in rows:
                payload = json.loads(row["payload"])
                result = _post_row(row["message_type"], payload, ingest_url)
                if result in {"sent", "drop"}:
                    queue_writer.mark_sent(queue_path, row["id"])
                elif result == "retry":
                    time.sleep(poll_interval_s)
                    break
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.error("Forwarder loop failed: %s", exc)
            time.sleep(poll_interval_s)
