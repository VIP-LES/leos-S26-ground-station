"""Database connection and operations for TimescaleDB."""

import os
import logging

import psycopg2

logger = logging.getLogger(__name__)

_conn: psycopg2.extensions.connection | None = None


def _get_conn() -> psycopg2.extensions.connection:
    """Return the active connection or raise if not connected."""
    if _conn is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _conn


def connect():
    """Create a connection to TimescaleDB using environment variables.

    Required env vars: PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE.
    Optional: PGSSL (set to "true" to enable SSL).
    """
    global _conn

    _conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=int(os.environ["PGPORT"]),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        database=os.environ["PGDATABASE"],
        sslmode="require" if os.environ.get("PGSSL") == "true" else "prefer",
    )
    _conn.autocommit = True
    logger.info(
        "Connected to TimescaleDB at %s:%s", os.environ["PGHOST"], os.environ["PGPORT"]
    )


def ensure_schema():
    """Create the telemetry hypertables if they don't exist."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sensors_and_gps_data (
                time         timestamptz PRIMARY KEY,
                gps_utc      timestamptz,
                temperature  double precision,
                pressure     double precision,
                humidity     double precision,
                altitude     double precision,
                gas_resistance double precision,
                light_lux    double precision,
                raw_visible  integer,
                raw_infrared integer,
                raw_full_spectrum bigint,
                uvs          double precision,
                pm10_env     double precision,
                pm25_env     double precision,
                pm100_env    double precision,
                aqi_pm25_us  double precision,
                aqi_pm100_us double precision,
                particles_03um double precision,
                particles_05um double precision,
                particles_10um double precision,
                particles_25um double precision,
                particles_50um double precision,
                particles_100um double precision,
                gps_fix_ok   boolean,
                gps_lat      double precision,
                gps_lon      double precision,
                gps_alt_m    double precision,
                gps_speed_mps double precision,
                gps_track_deg double precision,
                gps_sats_used integer,
                gps_sats_visible integer
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS efm_data (
                time                timestamptz PRIMARY KEY,
                adc1_ch1_diff       double precision,
                adc1_ch4_breakbeam  double precision,
                adc2_ch1_diff       double precision,
                adc2_ch4_breakbeam  double precision
            )
            """
        )
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_utc timestamptz")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS humidity double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS altitude double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gas_resistance double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS light_lux double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS raw_visible integer")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS raw_infrared integer")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS raw_full_spectrum bigint")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS uvs double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS particles_03um double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS particles_05um double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS particles_10um double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS particles_25um double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS particles_50um double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS particles_100um double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_fix_ok boolean")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_lat double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_lon double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_alt_m double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_speed_mps double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_track_deg double precision")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_sats_used integer")
        cur.execute("ALTER TABLE sensors_and_gps_data ADD COLUMN IF NOT EXISTS gps_sats_visible integer")
        cur.execute("ALTER TABLE efm_data DROP COLUMN IF EXISTS adc1_ch2_sensing")
        cur.execute("ALTER TABLE efm_data DROP COLUMN IF EXISTS adc1_ch3_reference")
        cur.execute("ALTER TABLE efm_data DROP COLUMN IF EXISTS adc2_ch2_sensing")
        cur.execute("ALTER TABLE efm_data DROP COLUMN IF EXISTS adc2_ch3_reference")
        cur.execute("ALTER TABLE efm_data ADD COLUMN IF NOT EXISTS adc1_ch1_diff double precision")
        cur.execute("ALTER TABLE efm_data ADD COLUMN IF NOT EXISTS adc1_ch4_breakbeam double precision")
        cur.execute("ALTER TABLE efm_data ADD COLUMN IF NOT EXISTS adc2_ch1_diff double precision")
        cur.execute("ALTER TABLE efm_data ADD COLUMN IF NOT EXISTS adc2_ch4_breakbeam double precision")
        cur.execute(
            "SELECT create_hypertable('sensors_and_gps_data', 'time', "
            "if_not_exists => TRUE, migrate_data => TRUE)"
        )
        cur.execute(
            "SELECT create_hypertable('efm_data', 'time', "
            "if_not_exists => TRUE, migrate_data => TRUE)"
        )
    logger.info("Schema ensured")


def insert_sensors_and_gps_row(row: dict) -> bool:
    """Insert one slow telemetry row."""
    sql = """
        INSERT INTO sensors_and_gps_data
            (time, gps_utc, temperature, pressure, humidity, altitude,
             gas_resistance, light_lux, raw_visible, raw_infrared,
             raw_full_spectrum, uvs, pm10_env, pm25_env, pm100_env,
             aqi_pm25_us, aqi_pm100_us, particles_03um, particles_05um,
             particles_10um, particles_25um, particles_50um, particles_100um,
             gps_fix_ok, gps_lat, gps_lon,
             gps_alt_m, gps_speed_mps, gps_track_deg, gps_sats_used,
             gps_sats_visible)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (time) DO NOTHING
    """
    values = (
        row["time"],
        row.get("gps_utc"),
        row.get("temperature"),
        row.get("pressure"),
        row.get("humidity"),
        row.get("altitude"),
        row.get("gas_resistance"),
        row.get("light_lux"),
        row.get("raw_visible"),
        row.get("raw_infrared"),
        row.get("raw_full_spectrum"),
        row.get("uvs"),
        row.get("pm10_env"),
        row.get("pm25_env"),
        row.get("pm100_env"),
        row.get("aqi_pm25_us"),
        row.get("aqi_pm100_us"),
        row.get("particles_03um"),
        row.get("particles_05um"),
        row.get("particles_10um"),
        row.get("particles_25um"),
        row.get("particles_50um"),
        row.get("particles_100um"),
        row.get("gps_fix_ok"),
        row.get("gps_lat"),
        row.get("gps_lon"),
        row.get("gps_alt_m"),
        row.get("gps_speed_mps"),
        row.get("gps_track_deg"),
        row.get("gps_sats_used"),
        row.get("gps_sats_visible"),
    )
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, values)
        inserted = cur.rowcount > 0

    if inserted:
        logger.info("Inserted row at time %s", row["time"])
    else:
        logger.debug("Duplicate row at time %s, skipped", row["time"])

    return inserted


def insert_efm_row(row: dict) -> bool:
    """Insert one EFM row."""
    sql = """
        INSERT INTO efm_data
            (time, adc1_ch1_diff, adc1_ch4_breakbeam,
             adc2_ch1_diff, adc2_ch4_breakbeam)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (time) DO NOTHING
    """
    values = (
        row["time"],
        row.get("adc1_ch1_diff"),
        row.get("adc1_ch4_breakbeam"),
        row.get("adc2_ch1_diff"),
        row.get("adc2_ch4_breakbeam"),
    )
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, values)
        inserted = cur.rowcount > 0

    if inserted:
        logger.info("Inserted EFM row at time %s", row["time"])
    else:
        logger.debug("Duplicate EFM row at time %s, skipped", row["time"])
    return inserted


def insert_row(row: dict) -> bool:
    """Backward-compatible alias for the original single-stream API."""
    return insert_sensors_and_gps_row(row)


def close():
    """Close the database connection."""
    global _conn
    if _conn:
        _conn.close()
        _conn = None
        logger.info("Database connection closed")
