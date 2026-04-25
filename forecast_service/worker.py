import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg2
import requests

from main import (
    SWITCHBOT_BASEMENT_FAR_DEVICE_ID,
    SWITCHBOT_BASEMENT_NEAR_DEVICE_ID,
    SWITCHBOT_OUTSIDE_DEVICE_ID,
    SWITCHBOT_OUTSIDE_SECRET,
    SWITCHBOT_OUTSIDE_TOKEN,
    SWITCHBOT_SECRET,
    SWITCHBOT_TOKEN,
    _ensure_tables,
    _insert_reading,
    _round_to_minute,
    _safe_float,
    fetch_switchbot_device_status,
)


POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "60"))
WEATHER_POLL_SECONDS = int(os.getenv("WEATHER_POLL_SECONDS", "900"))
OPEN_METEO_LAT = float(os.getenv("OPEN_METEO_LAT", "40.602722"))
OPEN_METEO_LON = float(os.getenv("OPEN_METEO_LON", "-79.75420"))
OPEN_METEO_TZ = os.getenv("OPEN_METEO_TZ", "America/New_York")

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "akv-forecast-worker/0.1"})


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def _heartbeat(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}", flush=True)


def _f_from_c(c: Optional[float]) -> Optional[float]:
    if c is None:
        return None
    return float(c) * 9.0 / 5.0 + 32.0


def _insert_weather_forecast(ts_utc: datetime, temp_f: Optional[float], rh: Optional[float], dew_point_f: Optional[float]) -> None:
    from main import _db_connect

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into weather_forecasts (ts, source, temp_f, rh_percent, dew_point_f)
                values (%s, %s, %s, %s, %s)
                on conflict (ts, source) do update set
                  temp_f = excluded.temp_f,
                  rh_percent = excluded.rh_percent,
                  dew_point_f = excluded.dew_point_f;
                """,
                (ts_utc, "open_meteo", temp_f, rh, dew_point_f),
            )
        conn.commit()


def _refresh_open_meteo_hourly() -> Dict[str, Any]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": OPEN_METEO_LAT,
        "longitude": OPEN_METEO_LON,
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m",
        "timezone": OPEN_METEO_TZ,
        "forecast_days": 3,
    }
    resp = _SESSION.get(url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json() or {}

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temps_c = hourly.get("temperature_2m") or []
    rhs = hourly.get("relative_humidity_2m") or []
    dps_c = hourly.get("dew_point_2m") or []

    n = min(len(times), len(temps_c), len(rhs), len(dps_c))
    if n == 0:
        return {"inserted": 0}

    inserted = 0
    for i in range(n):
        # Open-Meteo returns local-time strings; we store as UTC.
        t_local = times[i]
        ts = datetime.fromisoformat(t_local)
        ts_utc = ts.astimezone(timezone.utc)

        temp_f = _f_from_c(_safe_float(temps_c[i]))
        rh = _safe_float(rhs[i])
        dp_f = _f_from_c(_safe_float(dps_c[i]))
        _insert_weather_forecast(ts_utc, temp_f, rh, dp_f)
        inserted += 1

    return {"inserted": int(inserted), "hours": int(n)}


def _poll_once() -> None:
    _ensure_tables()

    now = datetime.now(timezone.utc)
    now_min = _round_to_minute(now)

    far = fetch_switchbot_device_status(
        device_id=str(SWITCHBOT_BASEMENT_FAR_DEVICE_ID),
        token=str(SWITCHBOT_TOKEN),
        secret=str(SWITCHBOT_SECRET),
    )
    near = fetch_switchbot_device_status(
        device_id=str(SWITCHBOT_BASEMENT_NEAR_DEVICE_ID),
        token=str(SWITCHBOT_TOKEN),
        secret=str(SWITCHBOT_SECRET),
    )
    outside = fetch_switchbot_device_status(
        device_id=str(SWITCHBOT_OUTSIDE_DEVICE_ID),
        token=str(SWITCHBOT_OUTSIDE_TOKEN),
        secret=str(SWITCHBOT_OUTSIDE_SECRET),
    )

    _insert_reading(
        "basement_far",
        _safe_float(far.get("temperature_f")),
        _safe_float(far.get("humidity_percent")),
        _safe_float(far.get("dew_point_f")),
        now_min,
    )
    _insert_reading(
        "basement_near",
        _safe_float(near.get("temperature_f")),
        _safe_float(near.get("humidity_percent")),
        _safe_float(near.get("dew_point_f")),
        now_min,
    )
    _insert_reading(
        "outside",
        _safe_float(outside.get("temperature_f")),
        _safe_float(outside.get("humidity_percent")),
        _safe_float(outside.get("dew_point_f")),
        now_min,
    )

    far_rh = _safe_float(far.get("humidity_percent"))
    near_rh = _safe_float(near.get("humidity_percent"))
    out_t = _safe_float(outside.get("temperature_f"))
    out_dp = _safe_float(outside.get("dew_point_f"))

    _heartbeat(
        "ok "
        f"far_rh={far_rh} near_rh={near_rh} "
        f"out_temp_f={out_t} out_dp_f={out_dp}"
    )


def main() -> None:
    missing = []
    for k in [
        "DATABASE_URL",
        "SWITCHBOT_TOKEN",
        "SWITCHBOT_SECRET",
        "SWITCHBOT_BASEMENT_FAR_DEVICE_ID",
        "SWITCHBOT_BASEMENT_NEAR_DEVICE_ID",
        "SWITCHBOT_OUTSIDE_TOKEN",
        "SWITCHBOT_OUTSIDE_SECRET",
        "SWITCHBOT_OUTSIDE_DEVICE_ID",
    ]:
        if not os.getenv(k):
            missing.append(k)

    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    _heartbeat(f"worker_start poll_seconds={POLL_SECONDS}")

    next_weather = time.time()

    while True:
        try:
            _poll_once()
        except psycopg2.OperationalError as e:
            _heartbeat(f"db_operational_error: {e}")
        except Exception as e:
            _heartbeat(f"poll_failed: {e}")

        # Weather refresh on a slower cadence.
        now_s = time.time()
        if now_s >= next_weather:
            try:
                info = _refresh_open_meteo_hourly()
                _heartbeat(f"open_meteo_ok inserted={info.get('inserted')}")
            except Exception as e:
                _heartbeat(f"open_meteo_failed: {e}")
            next_weather = now_s + WEATHER_POLL_SECONDS

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
