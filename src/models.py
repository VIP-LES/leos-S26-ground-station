"""Shared payload models and routing constants."""

from pydantic import BaseModel

SENSORS_AND_GPS = "sensors_and_gps"
EFM = "efm"
GEIGER = "geiger"

INGEST_PATHS = {
    SENSORS_AND_GPS: "/ingest/sensors-and-gps",
    EFM: "/ingest/efm",
    GEIGER: "/ingest/geiger",
}


class SensorsAndGpsPayload(BaseModel):
    """Structured slow telemetry payload."""

    time: str
    gps_utc: str | None = None
    temperature: float | None = None
    pressure: float | None = None
    humidity: float | None = None
    altitude: float | None = None
    gas_resistance: float | None = None
    light_lux: float | None = None
    raw_visible: int | None = None
    raw_infrared: int | None = None
    raw_full_spectrum: int | None = None
    uvs: float | None = None
    pm10_env: float | None = None
    pm25_env: float | None = None
    pm100_env: float | None = None
    aqi_pm25_us: float | None = None
    aqi_pm100_us: float | None = None
    particles_03um: float | None = None
    particles_05um: float | None = None
    particles_10um: float | None = None
    particles_25um: float | None = None
    particles_50um: float | None = None
    particles_100um: float | None = None
    gps_fix_ok: bool | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    gps_alt_m: float | None = None
    gps_speed_mps: float | None = None
    gps_track_deg: float | None = None
    gps_sats_used: int | None = None
    gps_sats_visible: int | None = None


class EfmPayload(BaseModel):
    """Structured EFM telemetry payload.

    efm0_motor_frequency / efm1_motor_frequency are the current format (one motor frequency per
    field mill). The adc* and avg/min/max/hall fields are older formats, kept so rows queued
    before the change are still accepted.
    """

    time: str
    adc1_ch1_diff: float | None = None
    adc1_ch4_breakbeam: float | None = None
    adc2_ch1_diff: float | None = None
    adc2_ch4_breakbeam: float | None = None
    efm0_motor_frequency: float | None = None
    efm1_motor_frequency: float | None = None
    efm0_avg_sample: float | None = None
    efm0_min_sample: float | None = None
    efm0_max_sample: float | None = None
    efm0_hall_events: int | None = None
    efm1_avg_sample: float | None = None
    efm1_min_sample: float | None = None
    efm1_max_sample: float | None = None
    efm1_hall_events: int | None = None


class GeigerPayload(BaseModel):
    """Geiger counter telemetry payload."""

    time: str
    hits_in_interval: int | None = None