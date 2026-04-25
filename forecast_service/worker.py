import os
import time
from datetime import datetime, timezone
from typing import Optional

import psycopg2

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


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def _heartbeat(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}", flush=True)


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

    while True:
        try:
            _poll_once()
        except psycopg2.OperationalError as e:
            _heartbeat(f"db_operational_error: {e}")
        except Exception as e:
            _heartbeat(f"poll_failed: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
