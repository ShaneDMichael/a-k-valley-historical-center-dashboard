import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import requests
from fastapi import FastAPI, Header, HTTPException


APP_VERSION = "0.1.0"

CACHE_DIR = Path(os.getenv("CACHE_DIR", "/var/data")).resolve()
CACHE_PATH = CACHE_DIR / "basement_forecast_latest.json"

FORECAST_LAT = float(os.getenv("FORECAST_LAT", "40.602722"))
FORECAST_LON = float(os.getenv("FORECAST_LON", "-79.754920"))
MOLD_RH_THRESHOLD = float(os.getenv("MOLD_RH_THRESHOLD", "45"))

SWITCHBOT_DEVICE_ID = os.getenv("SWITCHBOT_DEVICE_ID")
SWITCHBOT_TOKEN = os.getenv("SWITCHBOT_TOKEN")
SWITCHBOT_SECRET = os.getenv("SWITCHBOT_SECRET")

FORECAST_API_KEY = os.getenv("FORECAST_API_KEY")

ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", str(Path(__file__).parent / "artifacts"))).resolve()
FEATURE_SCHEMA_PATH = ARTIFACT_DIR / "feature_schema.json"
MODEL_PATH = ARTIFACT_DIR / "linear_model.json"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "akv-forecast-service/0.1"})

app = FastAPI(title="AKVHC Forecast Service", version=APP_VERSION)

_coef: Optional[np.ndarray] = None
_intercept: Optional[float] = None
_feature_cols: Optional[List[str]] = None


def _require_api_key(provided: Optional[str]) -> None:
    expected = FORECAST_API_KEY
    if expected and provided != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def _load_artifacts() -> None:
    global _coef, _intercept, _feature_cols

    if _coef is not None and _intercept is not None and _feature_cols is not None:
        return

    if not MODEL_PATH.exists() or not FEATURE_SCHEMA_PATH.exists():
        raise RuntimeError(
            f"Missing artifacts. Expected {MODEL_PATH.name} and {FEATURE_SCHEMA_PATH.name} in {ARTIFACT_DIR}"
        )

    with MODEL_PATH.open("r", encoding="utf-8") as f:
        model_payload = json.load(f)

    coef = model_payload.get("coef")
    intercept = model_payload.get("intercept")

    if not isinstance(coef, list) or not all(isinstance(v, (int, float)) for v in coef):
        raise RuntimeError("Invalid linear_model.json: missing coef")
    if not isinstance(intercept, (int, float)):
        raise RuntimeError("Invalid linear_model.json: missing intercept")

    _coef = np.asarray(coef, dtype=float)
    _intercept = float(intercept)
    with FEATURE_SCHEMA_PATH.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    cols = payload.get("feature_cols")
    if not isinstance(cols, list) or not all(isinstance(x, str) for x in cols):
        raise RuntimeError("Invalid feature_schema.json: missing feature_cols")

    _feature_cols = cols


def _switchbot_headers() -> Dict[str, str]:
    # SwitchBot API signing is already implemented in your Node service.
    # For this first pass we keep the forecast service independent by calling SwitchBot directly.
    # If you prefer, we can switch to calling the Node /api/temperature endpoint instead.
    import base64
    import hmac
    import hashlib
    import uuid

    token = SWITCHBOT_TOKEN.strip() if isinstance(SWITCHBOT_TOKEN, str) else SWITCHBOT_TOKEN
    secret = SWITCHBOT_SECRET.strip() if isinstance(SWITCHBOT_SECRET, str) else SWITCHBOT_SECRET

    if not token or not secret:
        raise RuntimeError("Missing SWITCHBOT_TOKEN and/or SWITCHBOT_SECRET")

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


def fetch_switchbot_basement() -> Dict[str, Any]:
    if not SWITCHBOT_DEVICE_ID:
        raise RuntimeError("Missing SWITCHBOT_DEVICE_ID")

    url = f"https://api.switch-bot.com/v1.1/devices/{requests.utils.quote(SWITCHBOT_DEVICE_ID)}/status"
    resp = SESSION.get(url, headers=_switchbot_headers(), timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("statusCode") and payload.get("statusCode") != 100:
        raise RuntimeError(f"SwitchBot error: {payload.get('message') or payload.get('statusCode')}")

    body = payload.get("body") or {}
    temperature_c = body.get("temperature") if body.get("temperature") is not None else body.get("temp")
    humidity = body.get("humidity") if body.get("humidity") is not None else body.get("humid")

    return {
        "temperature_c": float(temperature_c) if temperature_c is not None else None,
        "humidity_percent": float(humidity) if humidity is not None else None,
        "battery": body.get("battery"),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


def _get_nws_points(lat: float, lon: float) -> Dict[str, Any]:
    url = f"https://api.weather.gov/points/{lat:.6f},{lon:.6f}"
    resp = SESSION.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_noaa_hourly(lat: float, lon: float, hours: int = 48) -> List[Dict[str, Any]]:
    points = _get_nws_points(lat, lon)
    forecast_url = points.get("properties", {}).get("forecastHourly")
    if not forecast_url:
        raise RuntimeError("NOAA points response missing forecastHourly")

    resp = SESSION.get(forecast_url, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    periods = payload.get("properties", {}).get("periods")
    if not isinstance(periods, list) or not periods:
        raise RuntimeError("NOAA hourly forecast missing periods")

    rows: List[Dict[str, Any]] = []
    for p in periods[:hours]:
        start_time = p.get("startTime")
        temp_f = p.get("temperature")
        dewpoint = p.get("dewpoint", {})
        dewpoint_c = dewpoint.get("value")
        if not start_time:
            continue
        try:
            ts = datetime.fromisoformat(str(start_time).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue

        out_temp_f = float(temp_f) if isinstance(temp_f, (int, float)) else np.nan
        out_dp_c = float(dewpoint_c) if isinstance(dewpoint_c, (int, float)) else np.nan
        out_dp_f = out_dp_c * 9.0 / 5.0 + 32.0 if not np.isnan(out_dp_c) else np.nan

        rows.append(
            {
                "timestamp": ts,
                "outside_temp_f": out_temp_f,
                "outside_dew_point_f": out_dp_f,
            }
        )

    if not rows:
        raise RuntimeError("NOAA hourly forecast produced no rows")

    return rows


def _hour_sin_cos(hours: np.ndarray) -> Dict[str, np.ndarray]:
    h = hours.astype(float)
    angle = 2.0 * np.pi * h / 24.0
    return {"hour_sin": np.sin(angle), "hour_cos": np.cos(angle)}


def build_feature_frame(
    now_utc: datetime,
    basement_temp_c: Optional[float],
    basement_rh: Optional[float],
    noaa_rows: List[Dict[str, Any]],
) -> Dict[str, np.ndarray]:
    # This is a minimal inference-time feature set. Your exported feature schema defines what the
    # model expects; any missing features are filled with NaN.
    timestamps = [r["timestamp"] for r in noaa_rows]
    outside_temp_f = np.asarray([r.get("outside_temp_f", np.nan) for r in noaa_rows], dtype=float)
    outside_dew_point_f = np.asarray([r.get("outside_dew_point_f", np.nan) for r in noaa_rows], dtype=float)

    basement_temp_f = np.nan
    if basement_temp_c is not None:
        basement_temp_f = float(basement_temp_c) * 9.0 / 5.0 + 32.0

    basement_temp_f_arr = np.full(len(timestamps), basement_temp_f, dtype=float)
    basement_rh_lag_1h = np.full(len(timestamps), float(basement_rh) if basement_rh is not None else np.nan, dtype=float)
    basement_rh_lag_6h = np.full(len(timestamps), np.nan, dtype=float)
    basement_rh_lag_24h = np.full(len(timestamps), np.nan, dtype=float)

    hours = np.asarray([t.hour for t in timestamps], dtype=float)
    cyc = _hour_sin_cos(hours)

    dewpoint_minus_basement_temp = outside_dew_point_f - basement_temp_f_arr

    return {
        "timestamps": np.asarray(timestamps, dtype=object),
        "outside_temp_f": outside_temp_f,
        "outside_dew_point_f": outside_dew_point_f,
        "basement_temp_f": basement_temp_f_arr,
        "basement_rh_lag_1h": basement_rh_lag_1h,
        "basement_rh_lag_6h": basement_rh_lag_6h,
        "basement_rh_lag_24h": basement_rh_lag_24h,
        "hour_sin": cyc["hour_sin"],
        "hour_cos": cyc["hour_cos"],
        "outside_dewpoint_minus_basement_temp_f": dewpoint_minus_basement_temp,
    }


def predict_48h(feature_arrays: Dict[str, np.ndarray]) -> np.ndarray:
    _load_artifacts()
    assert _feature_cols is not None
    assert _coef is not None and _intercept is not None

    n = len(feature_arrays.get("outside_temp_f", []))
    Xv = np.full((n, len(_feature_cols)), np.nan, dtype=float)
    for j, col in enumerate(_feature_cols):
        arr = feature_arrays.get(col)
        if arr is None:
            continue
        Xv[:, j] = np.asarray(arr, dtype=float)
    if Xv.shape[1] != _coef.shape[0]:
        raise RuntimeError(
            f"Feature mismatch: X has {Xv.shape[1]} cols but model has {_coef.shape[0]} coefs"
        )
    return (Xv @ _coef) + _intercept


def risk_summary(pred_rh: np.ndarray) -> Dict[str, Any]:
    above = pred_rh >= MOLD_RH_THRESHOLD
    hours_above = int(np.sum(above))

    if hours_above >= 24:
        label = "High"
    elif hours_above >= 8:
        label = "Moderate"
    else:
        label = "Low"

    return {
        "threshold": round(float(MOLD_RH_THRESHOLD), 1),
        "hours_above_threshold_next_48h": hours_above,
        "label": label,
    }


def write_cache(payload: Dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(CACHE_PATH)


def read_cache() -> Dict[str, Any]:
    if not CACHE_PATH.exists():
        raise FileNotFoundError(str(CACHE_PATH))
    with CACHE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "cachePath": str(CACHE_PATH),
        "artifactsPresent": bool(MODEL_PATH.exists() and FEATURE_SCHEMA_PATH.exists()),
    }


@app.get("/latest")
def latest(x_forecast_api_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_api_key(x_forecast_api_key)
    try:
        return read_cache()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no_cached_forecast")


@app.post("/refresh")
def refresh(x_forecast_api_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_api_key(x_forecast_api_key)

    try:
        basement = fetch_switchbot_basement()
        noaa = fetch_noaa_hourly(FORECAST_LAT, FORECAST_LON, hours=48)

        now = datetime.now(timezone.utc)
        feat = build_feature_frame(
            now_utc=now,
            basement_temp_c=basement.get("temperature_c"),
            basement_rh=basement.get("humidity_percent"),
            noaa_rows=noaa,
        )

        preds = predict_48h(feat)
        preds_rounded = np.round(preds.astype(float), 1)

        rows = []
        for row, prh in zip(noaa, preds_rounded.tolist()):
            ts = row.get("timestamp")
            out_temp = row.get("outside_temp_f")
            out_dp = row.get("outside_dew_point_f")
            rows.append(
                {
                    "timestamp": (ts.astimezone(timezone.utc).isoformat() if isinstance(ts, datetime) else None),
                    "outside_temp_f": round(float(out_temp), 1) if isinstance(out_temp, (int, float)) and not np.isnan(float(out_temp)) else None,
                    "outside_dew_point_f": round(float(out_dp), 1) if isinstance(out_dp, (int, float)) and not np.isnan(float(out_dp)) else None,
                    "predicted_basement_rh_percent": float(prh),
                    "mold_risk_flag": bool(float(prh) >= MOLD_RH_THRESHOLD),
                }
            )

        summary = risk_summary(preds_rounded)
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "location": {"lat": FORECAST_LAT, "lon": FORECAST_LON},
            "basement_current": {
                "temperature_c": basement.get("temperature_c"),
                "humidity_percent": basement.get("humidity_percent"),
                "fetchedAt": basement.get("fetchedAt"),
            },
            "risk": summary,
            "forecast": rows,
        }

        write_cache(report)
        return {"status": "ok", "generated_at": report["generated_at"], "cachePath": str(CACHE_PATH)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
