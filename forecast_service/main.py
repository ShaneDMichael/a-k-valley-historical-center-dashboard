import os
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import requests
from fastapi import FastAPI


APP_VERSION = "0.1.0"

CACHE_DIR = Path(os.getenv("CACHE_DIR", "/var/data")).resolve()

MOLD_RH_THRESHOLD = float(os.getenv("MOLD_RH_THRESHOLD", "50"))

RISK_DAY_TZ = os.getenv("RISK_DAY_TZ", "America/New_York")
RISK_DAY_START_HOUR_LOCAL = int(os.getenv("RISK_DAY_START_HOUR_LOCAL", "18"))

SWITCHBOT_TOKEN = os.getenv("SWITCHBOT_TOKEN")
SWITCHBOT_SECRET = os.getenv("SWITCHBOT_SECRET")
SWITCHBOT_BASEMENT_FAR_DEVICE_ID = os.getenv("SWITCHBOT_BASEMENT_FAR_DEVICE_ID")
SWITCHBOT_BASEMENT_NEAR_DEVICE_ID = os.getenv("SWITCHBOT_BASEMENT_NEAR_DEVICE_ID")

SWITCHBOT_OUTSIDE_TOKEN = os.getenv("SWITCHBOT_OUTSIDE_TOKEN")
SWITCHBOT_OUTSIDE_SECRET = os.getenv("SWITCHBOT_OUTSIDE_SECRET")
SWITCHBOT_OUTSIDE_DEVICE_ID = os.getenv("SWITCHBOT_OUTSIDE_DEVICE_ID")

DATABASE_URL = os.getenv("DATABASE_URL")

MODELS_DIR = Path(os.getenv("MODELS_DIR", str(Path(__file__).parent / "models"))).resolve()
MODEL_24H_PATH = Path(os.getenv("MODEL_24H_PATH", str(MODELS_DIR / "basement_rh_lin_24h.joblib"))).resolve()
MODEL_48H_PATH = Path(os.getenv("MODEL_48H_PATH", str(MODELS_DIR / "basement_rh_lin_48h.joblib"))).resolve()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "akv-forecast-service/0.1"})

app = FastAPI(title="AKVHC Forecast Service", version=APP_VERSION)

_model_24h = None
_model_48h = None


def _load_models() -> None:
    global _model_24h, _model_48h
    if _model_24h is not None and _model_48h is not None:
        return

    if not MODEL_24H_PATH.exists() or not MODEL_48H_PATH.exists():
        raise RuntimeError(
            "Missing model artifacts. Expected: "
            f"{MODEL_24H_PATH.name} and {MODEL_48H_PATH.name} in {MODELS_DIR}"
        )

    _model_24h = joblib.load(MODEL_24H_PATH)
    _model_48h = joblib.load(MODEL_48H_PATH)


def _switchbot_headers(token: str, secret: str) -> Dict[str, str]:
    import base64
    import hmac
    import hashlib
    import uuid

    token = token.strip()
    secret = secret.strip()

    t = int(time.time() * 1000)
    nonce = str(uuid.uuid4())
    data = f"{token}{t}{nonce}".encode("utf-8")
    sign = base64.b64encode(hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest()).decode("utf-8")

    return {
        "Authorization": token,
        "t": str(t),
        "nonce": nonce,
        "sign": sign,
        "Content-Type": "application/json",
    }


def fetch_switchbot_device_status(*, device_id: str, token: str, secret: str) -> Dict[str, Any]:
    url = f"https://api.switch-bot.com/v1.1/devices/{requests.utils.quote(device_id)}/status"
    resp = SESSION.get(url, headers=_switchbot_headers(token, secret), timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("statusCode") and payload.get("statusCode") != 100:
        raise RuntimeError(f"SwitchBot error: {payload.get('message') or payload.get('statusCode')}")

    body = payload.get("body") or {}
    temperature = body.get("temperature") if body.get("temperature") is not None else body.get("temp")
    humidity = body.get("humidity") if body.get("humidity") is not None else body.get("humid")
    dew_point = body.get("dewPoint") if body.get("dewPoint") is not None else body.get("dew_point")

    return {
        "temperature_f": float(temperature) if temperature is not None else None,
        "humidity_percent": float(humidity) if humidity is not None else None,
        "dew_point_f": float(dew_point) if dew_point is not None else None,
        "battery": body.get("battery"),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }
def _db_connect():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg2.connect(DATABASE_URL)


def _ensure_tables() -> None:
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists sensor_readings (
                  ts timestamptz not null,
                  role text not null,
                  temp_f double precision,
                  rh_percent double precision,
                  dew_point_f double precision,
                  primary key (ts, role)
                );
                """
            )
            cur.execute(
                """
                create table if not exists weather_forecasts (
                  ts timestamptz not null,
                  source text not null,
                  temp_f double precision,
                  rh_percent double precision,
                  dew_point_f double precision,
                  primary key (ts, source)
                );
                """
            )
        conn.commit()


def _round_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _insert_reading(role: str, temp_f: Optional[float], rh: Optional[float], dew_point_f: Optional[float], ts: datetime) -> None:
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into sensor_readings (ts, role, temp_f, rh_percent, dew_point_f)
                values (%s, %s, %s, %s, %s)
                on conflict (ts, role) do update set
                  temp_f = excluded.temp_f,
                  rh_percent = excluded.rh_percent,
                  dew_point_f = excluded.dew_point_f;
                """,
                (ts, role, temp_f, rh, dew_point_f),
            )
        conn.commit()


def _fetch_history(start_ts: datetime, end_ts: datetime) -> pd.DataFrame:
    with _db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select ts, role, temp_f, rh_percent, dew_point_f
                from sensor_readings
                where ts >= %s and ts <= %s
                order by ts asc;
                """,
                (start_ts, end_ts),
            )
            rows = cur.fetchall()

    if not rows:
        return pd.DataFrame(columns=["ts", "role", "temp_f", "rh_percent", "dew_point_f"])

    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def _count_risk_days() -> int:
    """Count distinct 'risk days' where max basement RH >= threshold.

    A 'risk day' is defined as the local time interval [6pm, 6pm) in RISK_DAY_TZ.
    """

    hour = int(RISK_DAY_START_HOUR_LOCAL)
    if hour < 0 or hour > 23:
        hour = 18

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                with per_ts as (
                  select ts, max(rh_percent) as rh_max
                  from sensor_readings
                  where role in ('basement_far', 'basement_near')
                  group by ts
                ),
                risk_days as (
                  select date_trunc('day', (ts at time zone %s) - make_interval(hours => %s)) as risk_day
                  from per_ts
                  where rh_max is not null and rh_max >= %s
                  group by 1
                )
                select count(*)::int from risk_days;
                """,
                (RISK_DAY_TZ, hour, float(MOLD_RH_THRESHOLD)),
            )
            row = cur.fetchone()

    return int(row[0] or 0)


def _build_feature_row(now_utc: datetime) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    # We store and model at 1-minute resolution.
    now_utc = _round_to_minute(now_utc)
    start = now_utc - pd.Timedelta(hours=4)

    raw = _fetch_history(start, now_utc)
    debug: Dict[str, Any] = {"history_rows": int(len(raw))}
    if raw.empty:
        return pd.DataFrame(), debug

    # Pivot into wide format and resample to 1min.
    wide_parts = {}
    for role in raw["role"].unique():
        part = raw.loc[raw["role"] == role, ["ts", "temp_f", "rh_percent", "dew_point_f"]].copy()
        part = part.set_index("ts").sort_index()
        # Ensure numeric dtypes so resampling doesn't drop all-null columns.
        for c in ("temp_f", "rh_percent", "dew_point_f"):
            part[c] = pd.to_numeric(part[c], errors="coerce")
        # Mean in case of duplicates; then resample and forward-fill.
        part = part.resample("1min").mean(numeric_only=True).ffill()
        if "dew_point_f" in part.columns and "temp_f" in part.columns and "rh_percent" in part.columns:
            missing_dp = part["dew_point_f"].isna()
            if missing_dp.any():
                part.loc[missing_dp, "dew_point_f"] = [
                    _dew_point_f_from_temp_rh(t, rh)
                    for t, rh in zip(part.loc[missing_dp, "temp_f"], part.loc[missing_dp, "rh_percent"], strict=False)
                ]
        part = part.reindex(columns=["temp_f", "rh_percent", "dew_point_f"])
        wide_parts[role] = part

    far = wide_parts.get("basement_far")
    out = wide_parts.get("outside")

    if far is None or out is None or far.empty or out.empty:
        debug["missing_roles"] = [r for r in ["basement_far", "outside"] if r not in wide_parts]
        return pd.DataFrame(), debug

    # Guard: avoid making predictions until we have enough history to populate lag features.
    # We use 1-3 hour lags (plus minute lags), so require at least ~3 hours of 1-minute coverage.
    required_minutes = 3 * 60
    required_start = now_utc - pd.Timedelta(minutes=required_minutes)
    far_coverage_ok = far.index.min() <= required_start and far.index.max() >= now_utc
    out_coverage_ok = out.index.min() <= required_start and out.index.max() >= now_utc
    far_points_ok = int(far.loc[required_start:now_utc].shape[0]) >= required_minutes
    out_points_ok = int(out.loc[required_start:now_utc].shape[0]) >= required_minutes
    if not (far_coverage_ok and out_coverage_ok and far_points_ok and out_points_ok):
        debug["insufficient_history"] = {
            "required_minutes": int(required_minutes),
            "far_points": int(far.loc[required_start:now_utc].shape[0]) if not far.empty else 0,
            "out_points": int(out.loc[required_start:now_utc].shape[0]) if not out.empty else 0,
        }
        return pd.DataFrame(), debug

    idx = far.index.union(out.index).sort_values()
    df = pd.DataFrame(index=idx)
    df["basement_temp_mid_f"] = far.reindex(idx)["temp_f"]
    df["basement_mid_rh_percent"] = far.reindex(idx)["rh_percent"]
    df["basement_mid_dew_point_f"] = far.reindex(idx)["dew_point_f"]
    df["outside_temp_f"] = out.reindex(idx)["temp_f"]
    df["outside_dew_point_f"] = out.reindex(idx)["dew_point_f"]

    # Time features
    hour = df.index.hour
    df["sin_hour"] = np.sin(2 * np.pi * hour / 24.0)
    df["cos_hour"] = np.cos(2 * np.pi * hour / 24.0)
    dow = df.index.dayofweek
    df["sin_dow"] = np.sin(2 * np.pi * dow / 7.0)
    df["cos_dow"] = np.cos(2 * np.pi * dow / 7.0)

    # Derived features
    df["dew_point_diff_f"] = df["outside_dew_point_f"] - df["basement_mid_dew_point_f"]
    df["temp_diff_f"] = df["outside_temp_f"] - df["basement_temp_mid_f"]

    # Lag features
    steps_per_hour = 60
    for h in (1, 2, 3):
        shift = h * steps_per_hour
        for c in (
            "basement_temp_mid_f",
            "basement_mid_dew_point_f",
            "basement_mid_rh_percent",
            "outside_temp_f",
            "outside_dew_point_f",
        ):
            df[f"{c}_lag_{h}h"] = df[c].shift(shift)

    for m in (1, 5, 15, 30, 60):
        df[f"basement_mid_rh_percent_lag_{m}m"] = df["basement_mid_rh_percent"].shift(m)

    # Select feature row at now.
    if now_utc not in df.index:
        debug["now_missing_in_index"] = True
        return pd.DataFrame(), debug

    X = df.loc[[now_utc]].copy()
    X.index.name = "timestamp"
    debug["feature_row_ts"] = str(now_utc)
    return X, debug


def _risk_from_rh(rh: Optional[float]) -> str:
    if rh is None or not isinstance(rh, (int, float)) or np.isnan(float(rh)):
        return "unknown"
    return "elevated" if float(rh) >= MOLD_RH_THRESHOLD else "ok"


def _dew_point_f_from_temp_rh(temp_f: Optional[float], rh_percent: Optional[float]) -> Optional[float]:
    if temp_f is None or rh_percent is None:
        return None
    try:
        t_c = (float(temp_f) - 32.0) * (5.0 / 9.0)
        rh = float(rh_percent)
        if np.isnan(t_c) or np.isnan(rh) or rh <= 0.0 or rh > 100.0:
            return None
        a = 17.625
        b = 243.04
        gamma = (a * t_c) / (b + t_c) + math.log(rh / 100.0)
        dp_c = (b * gamma) / (a - gamma)
        dp_f = dp_c * (9.0 / 5.0) + 32.0
        if np.isnan(dp_f) or np.isinf(dp_f):
            return None
        return float(dp_f)
    except Exception:
        return None


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        fv = float(v)
    except Exception:
        return None
    if np.isnan(fv) or np.isinf(fv):
        return None
    return fv


@app.get("/health")
def health() -> Dict[str, Any]:
    models_present = bool(MODEL_24H_PATH.exists() and MODEL_48H_PATH.exists())
    db_configured = bool(DATABASE_URL)
    return {
        "status": "ok",
        "version": APP_VERSION,
        "cacheDir": str(CACHE_DIR),
        "modelsDir": str(MODELS_DIR),
        "modelsPresent": models_present,
        "dbConfigured": db_configured,
    }


@app.get("/status")
def status() -> Dict[str, Any]:
    # Minimal contract for the dashboard.
    warnings = []

    # Validate required env vars early.
    missing_env = []
    for k in [
        "SWITCHBOT_TOKEN",
        "SWITCHBOT_SECRET",
        "SWITCHBOT_BASEMENT_FAR_DEVICE_ID",
        "SWITCHBOT_BASEMENT_NEAR_DEVICE_ID",
        "SWITCHBOT_OUTSIDE_TOKEN",
        "SWITCHBOT_OUTSIDE_SECRET",
        "SWITCHBOT_OUTSIDE_DEVICE_ID",
        "DATABASE_URL",
    ]:
        if not os.getenv(k):
            missing_env.append(k)
    if missing_env:
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "current": {"rh_max_percent": None, "risk_status": "unknown"},
            "forecast_24h": {"rh_max_percent": None, "risk_status": "unknown"},
            "forecast_48h": {"rh_max_percent": None, "risk_status": "unknown"},
            "warnings": [f"missing_env:{','.join(missing_env)}"],
        }

    try:
        _ensure_tables()
    except Exception as e:
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "current": {"rh_max_percent": None, "risk_status": "unknown"},
            "forecast_24h": {"rh_max_percent": None, "risk_status": "unknown"},
            "forecast_48h": {"rh_max_percent": None, "risk_status": "unknown"},
            "warnings": [f"db_error:{str(e)}"],
        }

    now = datetime.now(timezone.utc)
    now_min = _round_to_minute(now)

    # Fetch live sensor readings.
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

    far_rh = _safe_float(far.get("humidity_percent"))
    near_rh = _safe_float(near.get("humidity_percent"))
    out_temp = _safe_float(outside.get("temperature_f"))
    out_dp = _safe_float(outside.get("dew_point_f"))

    far_temp = _safe_float(far.get("temperature_f"))
    near_temp = _safe_float(near.get("temperature_f"))
    far_dp = _safe_float(far.get("dew_point_f"))
    near_dp = _safe_float(near.get("dew_point_f"))

    if far_dp is None:
        far_dp = _dew_point_f_from_temp_rh(far_temp, far_rh)
    if near_dp is None:
        near_dp = _dew_point_f_from_temp_rh(near_temp, near_rh)
    if out_dp is None:
        out_dp = _dew_point_f_from_temp_rh(out_temp, _safe_float(outside.get("humidity_percent")))

    # Insert readings into Postgres.
    try:
        _insert_reading(
            "basement_far",
            far_temp,
            far_rh,
            far_dp,
            now_min,
        )
        _insert_reading(
            "basement_near",
            near_temp,
            near_rh,
            near_dp,
            now_min,
        )
        _insert_reading(
            "outside",
            out_temp,
            _safe_float(outside.get("humidity_percent")),
            out_dp,
            now_min,
        )
    except Exception as e:
        warnings.append(f"db_insert_failed:{str(e)}")

    current_rh_max = None
    if far_rh is not None and near_rh is not None:
        current_rh_max = float(max(far_rh, near_rh))

    risk_days = None
    try:
        risk_days = _count_risk_days()
    except Exception as e:
        warnings.append(f"risk_days_failed:{str(e)}")

    # Build feature row (from stored history).
    try:
        _load_models()
        X_row, debug = _build_feature_row(now)
    except Exception as e:
        warnings.append(f"feature_build_failed:{str(e)}")
        X_row = pd.DataFrame()
        debug = {"error": str(e)}

    pred_far_24 = None
    pred_far_48 = None
    if not X_row.empty:
        try:
            pred_far_24 = _safe_float(float(_model_24h.predict(X_row)[0]))
            pred_far_48 = _safe_float(float(_model_48h.predict(X_row)[0]))
        except Exception as e:
            warnings.append(f"predict_failed:{str(e)}")

    # Option A: estimate near-side based on far-side delta.
    pred_near_24 = None
    pred_near_48 = None
    if near_rh is not None and far_rh is not None:
        if pred_far_24 is not None:
            pred_near_24 = float(near_rh + (pred_far_24 - far_rh))
        if pred_far_48 is not None:
            pred_near_48 = float(near_rh + (pred_far_48 - far_rh))

    # Clamp predictions to [0, 100].
    def _clamp(v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        return float(min(100.0, max(0.0, v)))

    pred_far_24 = _clamp(pred_far_24)
    pred_far_48 = _clamp(pred_far_48)
    pred_near_24 = _clamp(pred_near_24)
    pred_near_48 = _clamp(pred_near_48)

    f24_max = None
    f48_max = None
    if pred_far_24 is not None and pred_near_24 is not None:
        f24_max = float(max(pred_far_24, pred_near_24))
    if pred_far_48 is not None and pred_near_48 is not None:
        f48_max = float(max(pred_far_48, pred_near_48))

    resp = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "risk_days": risk_days,
        "current": {
            "rh_max_percent": round(current_rh_max, 1) if current_rh_max is not None else None,
            "risk_status": _risk_from_rh(current_rh_max),
        },
        "forecast_24h": {
            "rh_max_percent": round(f24_max, 1) if f24_max is not None else None,
            "risk_status": _risk_from_rh(f24_max),
        },
        "forecast_48h": {
            "rh_max_percent": round(f48_max, 1) if f48_max is not None else None,
            "risk_status": _risk_from_rh(f48_max),
        },
        "warnings": warnings,
        "debug": debug,
    }

    return resp
