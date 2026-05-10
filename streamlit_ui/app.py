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
    if r in {"high", "elevated", "risk"}:
        return "#b91c1c"
    if r in {"moderate", "watch"}:
        return "#b45309"
    if r in {"low", "ok"}:
        return "#15803d"
    return "#334155"


def format_updated_at(value) -> str:
    if not value:
        return "—"
    tz = ZoneInfo("America/New_York")
    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value))
            dt = dt.replace(tzinfo=tz)
            return dt.strftime("%b %-d, %Y %-I:%M %p %Z")
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
            return dt.strftime("%b %-d, %Y %-I:%M %p %Z")
        except Exception:
            return value
    return str(value)


st.title("A-K Valley Heritage Center")
st.header("Basement Humidity Forecast")

st.markdown(
    "<style>h1{font-size:3rem !important;}</style>",
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

m1, m2, m3 = st.columns(3)
left, right = st.columns([1, 2])
with left:
    st.subheader("Current")
    st.metric("Current Relative Humidity", fmt_percent(c.get("rh_max_percent")))
    st.markdown(
        f"<div style='padding:8px;border-radius:8px;background:{risk_color(c.get('risk_status'))};color:white'>Current Mold Risk Level: {c.get('risk_status')}</div>",
        unsafe_allow_html=True,
    )

with right:
    st.subheader("Forecast")
    fcol1, fcol2 = st.columns(2)
    with fcol1:
        st.metric("Forecast Relative Humidity +24h", fmt_percent(f24_rh))
    with fcol2:
        st.metric("Forecast Relative Humidity +48h", fmt_percent(f48_rh))

    rcol1, rcol2 = st.columns(2)
    with rcol1:
        st.markdown(
            f"<div style='padding:8px;border-radius:8px;background:{risk_color(f24.get('risk_status'))};color:white'>+24h Mold Risk Level: {f24.get('risk_status')}</div>",
            unsafe_allow_html=True,
        )
    with rcol2:
        st.markdown(
            f"<div style='padding:8px;border-radius:8px;background:{risk_color(f48.get('risk_status'))};color:white'>+48h Mold Risk Level: {f48.get('risk_status')}</div>",
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
