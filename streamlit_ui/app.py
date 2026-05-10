import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st


st.set_page_config(page_title="AKV Basement Forecast", layout="wide")

FORECAST_STATUS_URL = os.getenv(
    "FORECAST_STATUS_URL",
    "https://a-k-valley-heritage-center-forecast.onrender.com/status",
)
OPEN_METEO_LAT = os.getenv("OPEN_METEO_LAT", "").strip()
OPEN_METEO_LON = os.getenv("OPEN_METEO_LON", "").strip()
OPEN_METEO_TZ = os.getenv("OPEN_METEO_TZ", "America/New_York").strip() or "America/New_York"
MOLD_RH_THRESHOLD = float(os.getenv("MOLD_RH_THRESHOLD", "50"))
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "60"))


def fetch_status() -> dict:
    resp = requests.get(FORECAST_STATUS_URL, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("Unexpected response")
    return payload


def risk_color(risk: str) -> str:
    r = (risk or "").lower()
    if r in {"high"}:
        return "#b91c1c"
    if r in {"medium"}:
        return "#b45309"
    if r in {"low"}:
        return "#facc15"
    if r in {"no", "ok"}:
        return "#15803d"
    return "#334155"


def risk_from_rh(rh: float | None) -> str:
    if rh is None:
        return "unknown"
    try:
        v = float(rh)
        if v < 50.0:
            return "no"
        if v <= 60.0:
            return "low"
        if v <= 65.0:
            return "medium"
        return "high"
    except Exception:
        return "unknown"


def fetch_open_meteo_rh_max(lat: str, lon: str, tz_name: str) -> tuple[float | None, float | None]:
    """Return (max_rh_next_24h, max_rh_next_48h) from Open-Meteo hourly RH forecast."""

    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "relative_humidity_2m",
        "timezone": tz_name,
        "forecast_days": 3,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json() if resp is not None else {}
    hourly = (payload or {}).get("hourly") or {}
    times = hourly.get("time") or []
    rhs = hourly.get("relative_humidity_2m") or []
    if not times or not rhs or len(times) != len(rhs):
        return (None, None)

    vals_24: list[float] = []
    vals_48: list[float] = []
    end_24 = now_local.timestamp() + 24 * 3600
    end_48 = now_local.timestamp() + 48 * 3600

    for t_str, rh in zip(times, rhs):
        try:
            dt = datetime.fromisoformat(str(t_str)).replace(tzinfo=tz)
            ts = dt.timestamp()
            if ts < now_local.timestamp():
                continue
            v = float(rh)
            if ts <= end_48:
                vals_48.append(v)
                if ts <= end_24:
                    vals_24.append(v)
        except Exception:
            continue

    max24 = max(vals_24) if vals_24 else None
    max48 = max(vals_48) if vals_48 else None
    return (max24, max48)


def format_updated_at(value) -> str:
    if not value:
        return "—"
    tz = ZoneInfo("America/New_York")

    def _fmt(dt: datetime) -> str:
        # Windows-friendly: avoid %-d / %-I.
        s = dt.strftime("%b %d, %Y %I:%M %p %Z")
        # Strip leading zeros from day and hour.
        s = s.replace(" 0", " ")
        return s

    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value))
            dt = dt.replace(tzinfo=tz)
            return _fmt(dt)
        except Exception:
            return str(value)
    if isinstance(value, str):
        s = value.strip()
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            dt = dt.astimezone(tz)
            return _fmt(dt)
        except Exception:
            return value
    return str(value)


st.title("A-K Valley Heritage Center")
st.header("Basement Humidity and Mold Risk Forecast")

st.markdown(
    "<style>h1{font-size:3rem !important;}div[data-testid='stMetricLabel']{font-size:1.15rem !important;}div[data-testid='stMetricValue']{font-size:2.4rem !important;}</style>",
    unsafe_allow_html=True,
)

if "last_refresh" not in st.session_state:
    st.session_state["last_refresh"] = time.time()

bar_left, bar_right = st.columns([1, 1])
with bar_right:
    if st.button("Refresh now"):
        st.session_state["last_refresh"] = time.time()
        st.rerun()

try:
    status = fetch_status()
except Exception as e:
    st.error(f"Failed to load forecast status: {e}")
    st.stop()

updated_at = status.get("updated_at")
risk_days = status.get("risk_days")

c = status.get("current") or {}
f24 = status.get("forecast_24h") or {}
f48 = status.get("forecast_48h") or {}

def fmt_percent(v):
    if v is None:
        return "—"
    try:
        return f"{float(v):.0f}%"
    except Exception:
        return f"{v}%"

f24_rh = f24.get("rh_max_percent")
f48_rh = f48.get("rh_max_percent")
model_used_24h = bool(status.get("model_used_24h"))
model_used_48h = bool(status.get("model_used_48h"))
f24_is_estimate = not model_used_24h
f48_is_estimate = not model_used_48h

if f24_rh is None:
    try:
        f24_rh = (status.get("debug") or {}).get("pred_far_24_raw")
    except Exception:
        f24_rh = None
if f48_rh is None:
    try:
        f48_rh = (status.get("debug") or {}).get("pred_far_48_raw")
    except Exception:
        f48_rh = None

if (f24_rh is None or f48_rh is None) and OPEN_METEO_LAT and OPEN_METEO_LON:
    try:
        om24, om48 = fetch_open_meteo_rh_max(OPEN_METEO_LAT, OPEN_METEO_LON, OPEN_METEO_TZ)
        current_rh = c.get("rh_max_percent")
        try:
            current_rh_f = float(current_rh) if current_rh is not None else None
        except Exception:
            current_rh_f = None

        # Open-Meteo is OUTDOOR RH; use it only as a weak signal so we don't
        # show unrealistic basement jumps while the database is still populating.
        def _blend(outdoor_max: float | None) -> float | None:
            if outdoor_max is None:
                return None
            if current_rh_f is None:
                return float(outdoor_max)
            v = 0.8 * float(current_rh_f) + 0.2 * float(outdoor_max)
            # Clamp to a conservative band around current conditions.
            v = max(float(current_rh_f) - 5.0, min(float(current_rh_f) + 10.0, v))
            return float(max(0.0, min(100.0, v)))

        if f24_rh is None and om24 is not None:
            f24_rh = _blend(om24)
            f24_is_estimate = True
        if f48_rh is None and om48 is not None:
            f48_rh = _blend(om48)
            f48_is_estimate = True
    except Exception:
        pass

f24_risk = f24.get("risk_status")
f48_risk = f48.get("risk_status")
if not f24_risk or str(f24_risk).lower() == "unknown":
    f24_risk = risk_from_rh(None if f24_rh is None else float(f24_rh))
if not f48_risk or str(f48_risk).lower() == "unknown":
    f48_risk = risk_from_rh(None if f48_rh is None else float(f48_rh))

left, right = st.columns([1, 2])
with left:
    st.metric("Current Relative Humidity", fmt_percent(c.get("rh_max_percent")))
    st.markdown(
        f"<div style='padding:8px;border-radius:8px;background:{risk_color(c.get('risk_status'))};color:white'>Current Mold Risk Level: {c.get('risk_status')}</div>",
        unsafe_allow_html=True,
    )

with right:
    fcol1, fcol2 = st.columns(2)
    with fcol1:
        st.metric(
            "Forecast Relative Humidity +24h" + (" (estimate)" if f24_is_estimate else ""),
            fmt_percent(f24_rh),
        )
    with fcol2:
        st.metric(
            "Forecast Relative Humidity +48h" + (" (estimate)" if f48_is_estimate else ""),
            fmt_percent(f48_rh),
        )

    rcol1, rcol2 = st.columns(2)
    with rcol1:
        st.markdown(
            f"<div style='padding:8px;border-radius:8px;background:{risk_color(f24_risk)};color:white'>+24h Mold Risk Level: {f24_risk}</div>",
            unsafe_allow_html=True,
        )
    with rcol2:
        st.markdown(
            f"<div style='padding:8px;border-radius:8px;background:{risk_color(f48_risk)};color:white'>+48h Mold Risk Level: {f48_risk}</div>",
            unsafe_allow_html=True,
        )

st.caption(
    f"Updated: {format_updated_at(updated_at)} | Past number of Mold Risk days: {risk_days}"
)


if REFRESH_SECONDS > 0:
    now_s = time.time()
    last = float(st.session_state.get("last_refresh") or 0.0)
    if now_s - last >= float(REFRESH_SECONDS):
        st.session_state["last_refresh"] = now_s
        st.rerun()
