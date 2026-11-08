"""FastAPI ingest API for telemetry data."""

import logging
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from src import db
from src.models import EfmPayload, GeigerPayload, SensorsAndGpsPayload

logger = logging.getLogger(__name__)


app = FastAPI(title="LEOS Ingest API")


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok"}


def _validate_time(time_str: str) -> JSONResponse | None:
    try:
        datetime.fromisoformat(time_str)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid time format: {time_str}"},
        )
    return None


def _validate_optional_time(time_str: str | None) -> JSONResponse | None:
    if time_str is None:
        return None
    return _validate_time(time_str)


def _result(inserted: bool, time_str: str, message_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=201 if inserted else 200,
        content={
            "status": "inserted" if inserted else "duplicate, skipped",
            "time": time_str,
            "message_type": message_type,
        },
    )


@app.post("/ingest/sensors-and-gps")
def ingest_sensors_and_gps(payload: SensorsAndGpsPayload):
    error = _validate_time(payload.time)
    if error is not None:
        return error
    error = _validate_optional_time(payload.gps_utc)
    if error is not None:
        return error

    try:
        inserted = db.insert_sensors_and_gps_row(payload.model_dump())
    except Exception as exc:
        logger.error("Sensors/GPS insert failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return _result(inserted, payload.time, "sensors_and_gps")


@app.post("/ingest/efm")
def ingest_efm(payload: EfmPayload):
    error = _validate_time(payload.time)
    if error is not None:
        return error

    try:
        inserted = db.insert_efm_row(payload.model_dump())
    except Exception as exc:
        logger.error("EFM insert failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return _result(inserted, payload.time, "efm")


@app.post("/ingest/geiger")
def ingest_geiger(payload: GeigerPayload):
    error = _validate_time(payload.time)
    if error is not None:
        return error

    try:
        inserted = db.insert_geiger_row(payload.model_dump())
    except Exception as exc:
        logger.error("Geiger insert failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return _result(inserted, payload.time, "geiger")