import os
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import pandas as pd
import requests
import streamlit as st


st.set_page_config(page_title="AKV Basement Forecast", layout="wide")

FORECAST_STATUS_URL = os.getenv(
    "FORECAST_STATUS_URL",
    "https://a-k-valley-heritage-center-forecast.onrender.com/status",
)
FORECAST_OPTIMIZER_URL = os.getenv(
    "FORECAST_OPTIMIZER_URL",
    "https://a-k-valley-heritage-center-forecast.onrender.com/optimizer/latest",
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


def fetch_optimizer_latest() -> dict:
    resp = requests.get(FORECAST_OPTIMIZER_URL, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("Unexpected response")
    return payload


def _opt_view() -> str:
    v = st.query_params.get("view")
    return str(v) if v is not None else ""


def _go_opt() -> None:
    st.query_params["view"] = "optimizer"


def _go_main() -> None:
    if "view" in st.query_params:
        del st.query_params["view"]


def risk_color(risk: str) -> str:
    r = (risk or "").lower()
    if r in {"high"}:
        return "#b91c1c"
    if r in {"medium"}:
        return "#b45309"
    if r in {"low"}:
        return "#0f766e"
    if r in {"none", "ok"}:
        return "#15803d"
    return "#334155"


def risk_from_rh(rh: float | None) -> str:
    if rh is None:
        return "unknown"
    try:
        v = float(rh)
        if v < 50.0:
            return "none"
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


@st.cache_data(ttl=2 * 60 * 60)
def fetch_open_meteo_rh_max_cached(lat: str, lon: str, tz_name: str) -> tuple[float | None, float | None]:
    return fetch_open_meteo_rh_max(lat, lon, tz_name)


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
    "<style>"
    "h1{font-size:3rem !important;font-weight:800 !important;}"
    "h2{font-size:1.8rem !important;font-weight:800 !important;margin-top:0.25rem !important;}"
    "div[data-testid='stMetricLabel']{font-size:1.35rem !important;font-weight:800 !important;}"
    "div[data-testid='stMetricValue']{font-size:2.6rem !important;font-weight:800 !important;}"
    "div[data-testid='stMetric']{padding:0.25rem 0 0.75rem 0 !important;}"
    "div[data-testid='stMarkdownContainer']{margin-top:0.25rem !important;margin-bottom:0.75rem !important;}"
    "</style>",
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

current_rh = c.get("rh_max_percent")
current_risk = c.get("risk_status")
if not current_risk or str(current_risk).lower() == "unknown":
    try:
        current_risk = risk_from_rh(None if current_rh is None else float(current_rh))
    except Exception:
        current_risk = "unknown"

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
        f"<div style='padding:10px;border-radius:10px;background:{risk_color(current_risk)};color:white;font-weight:700'>Current Mold Risk Level: {current_risk}</div>",
        unsafe_allow_html=True,
    )

with right:
    fcol1, fcol2 = st.columns(2)
    with fcol1:
        st.metric(
            "Forecast Relative Humidity +24h" + (" (estimate)" if f24_is_estimate else ""),
            fmt_percent(f24_rh),
        )
        st.markdown(
            f"<div style='padding:10px;border-radius:10px;background:{risk_color(f24_risk)};color:white;font-weight:700'>+24h Mold Risk Level: {f24_risk}</div>",
            unsafe_allow_html=True,
        )
    with fcol2:
        st.metric(
            "Forecast Relative Humidity +48h" + (" (estimate)" if f48_is_estimate else ""),
            fmt_percent(f48_rh),
        )
        st.markdown(
            f"<div style='padding:10px;border-radius:10px;background:{risk_color(f48_risk)};color:white;font-weight:700'>+48h Mold Risk Level: {f48_risk}</div>",
            unsafe_allow_html=True,
        )

st.caption(f"Updated: {format_updated_at(updated_at)}")
st.caption(f"Past number of Mold Risk days: {risk_days}")


st.header("Dehumidifier Optimization (Daily Schedule)")

if _opt_view() != "optimizer":
    st.button("View optimization schedule", on_click=_go_opt)
else:
    st.button("Back to main dashboard", on_click=_go_main)

try:
    opt = fetch_optimizer_latest()
except Exception as e:
    st.error(f"Failed to load optimizer results: {e}")
    opt = {"run": None, "schedule_slots": [], "predicted_rh_points": []}

run = opt.get("run")
if not run:
    st.info("No optimizer runs found yet.")
elif _opt_view() == "optimizer":
    st.caption(
        "Latest optimizer run: "
        f"{format_updated_at(run.get('run_ts'))} | solver={run.get('solver')}"
    )
    if run.get("created_at"):
        st.caption(f"Optimizer record created: {format_updated_at(run.get('created_at'))}")
    if run.get("app_version"):
        st.caption(f"Optimizer app_version: {run.get('app_version')}")
    if run.get("warnings"):
        st.caption(f"Warnings: {run.get('warnings')}")

    om_db = status.get("open_meteo_db") or {}
    if om_db:
        st.caption(
            "Open-Meteo DB: "
            f"points_last_48h={om_db.get('points_last_48h')} "
            f"latest_run={format_updated_at(om_db.get('latest_run_ts_utc'))}"
        )

    schedule_slots = opt.get("schedule_slots") or []
    rh_points = opt.get("predicted_rh_points") or []

    channel_labels = {
        "basement": "Basement",
        "big_room_far": "Big Room far side",
        "big_room_near": "Big Room near side",
        "entrance": "Entrance Room",
        "upstairs": "Upstairs Office",
    }
    channels = list(channel_labels.keys())

    try:
        tz = ZoneInfo(OPEN_METEO_TZ)
    except Exception:
        tz = ZoneInfo("America/New_York")

    # Build a compact schedule grid: rows=channel, cols=hour start.
    slots_by_channel = {c: [] for c in channels}
    for s in schedule_slots:
        ch = s.get("channel_id")
        if ch in slots_by_channel:
            slots_by_channel[ch].append(s)

    # Determine column labels from the first channel with slots.
    col_ts = []
    for c in channels:
        if slots_by_channel.get(c):
            col_ts = [x.get("slot_start_ts") for x in slots_by_channel[c]]
            break

    if col_ts:
        st.subheader("Schedule periods (next 24 hours)")

        def _parse_dt(v: str | None) -> Optional[datetime]:
            if not v:
                return None
            try:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except Exception:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        def _fmt_hm(dt: datetime) -> str:
            s = dt.strftime("%b %d, %I:%M%p")
            s = s.replace(":00", "")
            if s.startswith("0"):
                s = s[1:]
            s = s.replace("AM", "am").replace("PM", "pm")
            return s

        for c in channels:
            slots = slots_by_channel.get(c) or []
            if not slots:
                continue

            # Sort by real timestamps (string sorting can mis-order) then trim to next 24 slots.
            parsed_slots: List[tuple[datetime, dict]] = []
            for s in slots:
                s_start_dt = _parse_dt(s.get("slot_start_ts"))
                if not s_start_dt:
                    continue
                parsed_slots.append((s_start_dt, s))
            parsed_slots.sort(key=lambda t: t[0])
            slots = [s for _, s in parsed_slots][:24]

            periods: List[tuple[datetime, datetime, bool]] = []
            for s in slots:
                s_start = _parse_dt(s.get("slot_start_ts"))
                s_end = _parse_dt(s.get("slot_end_ts"))
                if not s_start or not s_end:
                    continue
                if s_end <= s_start:
                    continue
                s_on = bool(s.get("is_on"))

                # Convert to local tz for display
                s_start = s_start.astimezone(tz)
                s_end = s_end.astimezone(tz)

                if not periods:
                    periods.append((s_start, s_end, s_on))
                    continue

                last_start, last_end, last_on = periods[-1]
                if s_on == last_on and abs((s_start - last_end).total_seconds()) <= 1:
                    periods[-1] = (last_start, s_end, last_on)
                else:
                    periods.append((s_start, s_end, s_on))

            st.markdown(f"**{channel_labels.get(c, c)}**")
            if not periods:
                st.caption("No schedule periods available.")
                continue

            for p_start, p_end, p_on in periods:
                if p_end <= p_start:
                    continue
                st.caption(f"{_fmt_hm(p_start)} – {_fmt_hm(p_end)}: {'ON' if p_on else 'OFF'}")
    else:
        st.info("No schedule slots available yet.")

    st.subheader("Predicted RH (when schedule is followed)")
    st.caption("These curves are the optimizer’s predicted RH trajectory assuming the ON/OFF schedule is followed.")
    st.caption(
        "Background colors show qualitative mold risk bands by RH: "
        "0–50 none (green), 50–60 low (teal), 60–65 medium (orange), 65+ high (red)."
    )

    pts_by_series = {c: [] for c in channels}
    for p in rh_points:
        series = p.get("series")
        if series in pts_by_series:
            pts_by_series[series].append(p)

    try:
        target_rh = float(run.get("rh_target_percent") or 55.0)
    except Exception:
        target_rh = 55.0

    # Use datetime index so the x-axis renders as time (no 0.0, 1.0, ... decimals).
    for c in channels:
        pts = pts_by_series.get(c) or []
        if not pts:
            continue
        st.markdown(f"**{channel_labels.get(c, c)}**")
        times: List[datetime] = []
        values: List[Optional[float]] = []
        now_local = datetime.now(tz)
        min_ts = now_local - timedelta(days=2)
        max_ts = now_local + timedelta(days=7)
        for item in pts:
            ts_raw = item.get("ts")
            if not ts_raw:
                continue
            try:
                dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except Exception:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(tz)
            if dt_local < min_ts or dt_local > max_ts:
                continue
            times.append(dt_local)
            values.append(item.get("rh_percent"))

        if not times:
            continue

        idx = pd.to_datetime(times)
        try:
            if getattr(idx, "tz", None) is not None:
                idx = idx.tz_convert(tz)
        except Exception:
            pass
        try:
            idx = idx.tz_localize(None)
        except Exception:
            pass

        df = pd.DataFrame({channel_labels.get(c, c): values}, index=pd.DatetimeIndex(idx)).sort_index()
        fig, ax = plt.subplots(figsize=(10, 2.5))
        ax.axhspan(0, 50, facecolor="#15803d", alpha=0.28, zorder=0)
        ax.axhspan(50, 60, facecolor="#0f766e", alpha=0.28, zorder=0)
        ax.axhspan(60, 65, facecolor="#b45309", alpha=0.28, zorder=0)
        ax.axhspan(65, 100, facecolor="#b91c1c", alpha=0.28, zorder=0)
        ax.axhline(target_rh, color="#111827", linewidth=1.0, alpha=0.45)
        try:
            ax.annotate(
                f"{int(round(target_rh))}",
                xy=(-0.002, float(target_rh)),
                xycoords=ax.get_yaxis_transform(),
                xytext=(-2, -3),
                textcoords="offset points",
                va="center",
                ha="right",
                fontsize=7,
                color="#111827",
                alpha=0.60,
            )
        except Exception:
            pass
        ax.plot(df.index, df.iloc[:, 0], linewidth=2)
        ax.set_ylabel("RH %")
        ax.set_xlabel("")
        ax.set_ylim(0, 100)
        try:
            ax.set_xlim(df.index.min(), df.index.max())
        except Exception:
            pass

        def _fmt_tick(x, _pos):
            try:
                dt = mdates.num2date(x)
                s = dt.strftime("%b %d %I%p")
                s = s.replace(" 0", " ")
                s = s.replace("AM", "am").replace("PM", "pm")
                return s
            except Exception:
                return ""

        ax.xaxis.set_major_formatter(FuncFormatter(_fmt_tick))
        ax.grid(True, alpha=0.25)
        fig.autofmt_xdate(rotation=0, ha="center")
        st.pyplot(fig, clear_figure=True)


if REFRESH_SECONDS > 0:
    now_s = time.time()
    last = float(st.session_state.get("last_refresh") or 0.0)
    if now_s - last >= float(REFRESH_SECONDS):
        st.session_state["last_refresh"] = now_s
        st.rerun()
