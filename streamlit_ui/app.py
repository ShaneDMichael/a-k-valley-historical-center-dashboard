import os
import time

import requests
import streamlit as st
import streamlit.components.v1 as components


st.set_page_config(page_title="AKV Basement Forecast", layout="wide")

FORECAST_STATUS_URL = os.getenv(
    "FORECAST_STATUS_URL",
    "https://a-k-valley-heritage-center-forecast.onrender.com/status",
)
GLB_VIEWER_URL = os.getenv("GLB_VIEWER_URL", "").strip()
GLB_MODEL = os.getenv("GLB_MODEL", "Basement.glb")
GLB_DEVICE_ID = os.getenv("GLB_DEVICE_ID", "")
GLB_TITLE = os.getenv("GLB_TITLE", "Basement")
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


st.title("AKV Basement Humidity Forecast")

if "last_refresh" not in st.session_state:
    st.session_state["last_refresh"] = time.time()

with st.sidebar:
    st.subheader("Config")
    st.write(f"Status URL: `{FORECAST_STATUS_URL}`")
    st.write(f"Viewer URL: `{GLB_VIEWER_URL}`")
    st.write(f"Model: `{GLB_MODEL}`")
    st.write(f"Refresh: {REFRESH_SECONDS}s")

    if st.button("Refresh now"):
        st.session_state["last_refresh"] = time.time()
        st.rerun()

col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("Forecast")

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
    m1.metric("Current RH Max", c.get("rh_max_percent"))
    m2.metric("Forecast +24h", f24_rh)
    m3.metric("Forecast +48h", f48_rh)

    r1, r2, r3 = st.columns(3)
    r1.markdown(
        f"<div style='padding:8px;border-radius:8px;background:{risk_color(c.get('risk_status'))};color:white'>Current: {c.get('risk_status')}</div>",
        unsafe_allow_html=True,
    )
    r2.markdown(
        f"<div style='padding:8px;border-radius:8px;background:{risk_color(f24.get('risk_status'))};color:white'>+24h: {f24.get('risk_status')}</div>",
        unsafe_allow_html=True,
    )
    r3.markdown(
        f"<div style='padding:8px;border-radius:8px;background:{risk_color(f48.get('risk_status'))};color:white'>+48h: {f48.get('risk_status')}</div>",
        unsafe_allow_html=True,
    )

    st.caption(f"Updated: {updated_at} | Risk days: {risk_days}")

    debug = status.get("debug") or {}
    cal = debug.get("calibration") or {}
    with st.expander("Debug / Calibration"):
        st.json(
            {
                "calibration": cal,
                "warnings": status.get("warnings"),
            }
        )

with col_right:
    st.subheader("3D Model")

    if not GLB_VIEWER_URL:
        st.info("Set GLB_VIEWER_URL in the Streamlit service environment to enable the embedded 3D viewer.")
        st.stop()

    qs = []
    if GLB_MODEL:
        qs.append(f"model={requests.utils.quote(GLB_MODEL)}")
    if GLB_DEVICE_ID:
        qs.append(f"deviceId={requests.utils.quote(GLB_DEVICE_ID)}")
    if GLB_TITLE:
        qs.append(f"title={requests.utils.quote(GLB_TITLE)}")
    viewer_src = GLB_VIEWER_URL.rstrip("/") + "/" + ("?" + "&".join(qs) if qs else "")

    components.iframe(viewer_src, height=700)


if REFRESH_SECONDS > 0:
    now_s = time.time()
    last = float(st.session_state.get("last_refresh") or 0.0)
    if now_s - last >= float(REFRESH_SECONDS):
        st.session_state["last_refresh"] = now_s
        st.rerun()
