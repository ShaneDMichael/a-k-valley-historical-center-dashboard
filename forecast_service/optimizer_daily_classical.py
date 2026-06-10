import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

from zoneinfo import ZoneInfo

from main import _db_connect


psycopg2.extras.register_uuid()


LOCAL_TZ = os.getenv("LOCAL_TZ", "America/New_York")
OPTIMIZER_HORIZON_HOURS = int(os.getenv("OPTIMIZER_HORIZON_HOURS", "24"))
APP_VERSION = os.getenv("APP_VERSION", "dev")
OPTIMIZER_BUILD_ID = os.getenv("OPTIMIZER_BUILD_ID", "2026-06-09a")


def _heartbeat(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}", flush=True)


def _ensure_optimizer_tables() -> None:
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists room_metadata (
                  room text primary key,
                  room_display_name text,
                  volume_ft3 double precision,
                  floor_area_ft2 double precision,
                  dehumidifier_id text,
                  control_type text,
                  lat double precision,
                  lon double precision,
                  altitude_ft double precision,
                  priority_weight double precision,
                  space_type text,
                  dh_max_db double precision,
                  dh_rh_setpoint_percent double precision,
                  glb_path text,
                  glb_file text,
                  notes text
                );
                """
            )
            cur.execute(
                """
                create table if not exists optimizer_runs (
                  run_id uuid primary key,
                  run_ts timestamptz not null,
                  horizon_hours int not null,
                  solver text not null,
                  rh_target_percent double precision not null,
                  app_version text,
                  warnings text,
                  created_at timestamptz not null default now()
                );
                """
            )
            cur.execute(
                """
                create index if not exists optimizer_runs_run_ts_idx
                on optimizer_runs (run_ts desc);
                """
            )
            cur.execute(
                """
                create table if not exists dehumidifier_schedule_slots (
                  run_id uuid not null references optimizer_runs(run_id) on delete cascade,
                  channel_id text not null,
                  slot_start_ts timestamptz not null,
                  slot_end_ts timestamptz not null,
                  is_on boolean not null,
                  created_at timestamptz not null default now(),
                  primary key (run_id, channel_id, slot_start_ts)
                );
                """
            )

            # Backward-compatible migration: rename device_group -> channel_id.
            cur.execute(
                """
                do $$
                begin
                  if exists (
                    select 1
                    from information_schema.columns
                    where table_schema = 'public'
                      and table_name = 'dehumidifier_schedule_slots'
                      and column_name = 'device_group'
                  ) and not exists (
                    select 1
                    from information_schema.columns
                    where table_schema = 'public'
                      and table_name = 'dehumidifier_schedule_slots'
                      and column_name = 'channel_id'
                  ) then
                    alter table public.dehumidifier_schedule_slots rename column device_group to channel_id;
                  end if;
                end $$;
                """
            )
            cur.execute(
                """
                create index if not exists dehumidifier_schedule_slots_device_slot_idx
                on dehumidifier_schedule_slots (channel_id, slot_start_ts desc);
                """
            )
            cur.execute(
                """
                create table if not exists predicted_rh_points (
                  run_id uuid not null references optimizer_runs(run_id) on delete cascade,
                  series text not null,
                  ts timestamptz not null,
                  rh_percent double precision,
                  created_at timestamptz not null default now(),
                  primary key (run_id, series, ts)
                );
                """
            )
            cur.execute(
                """
                create index if not exists predicted_rh_points_series_ts_idx
                on predicted_rh_points (series, ts desc);
                """
            )
        conn.commit()


def _latest_sensor_values(now_utc: datetime) -> Dict[str, Dict[str, Optional[float]]]:
    """Return latest (temp_f, rh_percent, dew_point_f) per role at or before now."""

    with _db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                with ranked as (
                  select
                    role,
                    ts,
                    temp_f,
                    rh_percent,
                    dew_point_f,
                    row_number() over (partition by role order by ts desc) as rn
                  from sensor_readings
                  where ts <= %s
                )
                select role, ts, temp_f, rh_percent, dew_point_f
                from ranked
                where rn = 1;
                """,
                (now_utc,),
            )
            rows = cur.fetchall() or []

    out: Dict[str, Dict[str, Optional[float]]] = {}
    for r in rows:
        out[str(r["role"])] = {
            "ts": r.get("ts"),
            "temp_f": r.get("temp_f"),
            "rh_percent": r.get("rh_percent"),
            "dew_point_f": r.get("dew_point_f"),
        }
    return out


def _weather_forecast_points_colnames() -> Dict[str, str]:
    """Return canonical column name mapping for weather_forecast_points.

    The project has existed in multiple schemas:
    - v1: run_ts, target_ts, rh_percent
    - v0/vlegacy: run_ts_utc, target_ts_utc, rh_pct
    """

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select column_name
                from information_schema.columns
                where table_schema = 'public'
                  and table_name = 'weather_forecast_points';
                """
            )
            cols = {str(r[0]) for r in (cur.fetchall() or [])}

    run_col = "run_ts" if "run_ts" in cols else "run_ts_utc"
    target_col = "target_ts" if "target_ts" in cols else "target_ts_utc"
    rh_col = "rh_percent" if "rh_percent" in cols else "rh_pct"
    dp_col = "dew_point_f" if "dew_point_f" in cols else "dew_point_f"
    temp_col = "temp_f" if "temp_f" in cols else "temp_f"

    return {
        "run_ts": run_col,
        "target_ts": target_col,
        "rh": rh_col,
        "dew_point_f": dp_col,
        "temp_f": temp_col,
    }


def _fetch_latest_open_meteo_run_ts(now_utc: datetime) -> Optional[datetime]:
    start = now_utc - timedelta(days=2)
    cols = _weather_forecast_points_colnames()
    run_col = cols["run_ts"]
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select max({run_col})
                from weather_forecast_points
                where source in ('open_meteo', 'open-meteo')
                  and {run_col} >= %s
                  and {run_col} <= %s;
                """ ,
                (start, now_utc),
            )
            row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _fetch_forecast_points(run_ts: datetime, start_ts: datetime, end_ts: datetime) -> List[Dict[str, Any]]:
    cols = _weather_forecast_points_colnames()
    run_col = cols["run_ts"]
    target_col = cols["target_ts"]
    rh_col = cols["rh"]
    temp_col = cols["temp_f"]
    dp_col = cols["dew_point_f"]
    with _db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                select
                  {target_col} as target_ts,
                  {temp_col} as temp_f,
                  {rh_col} as rh_percent,
                  {dp_col} as dew_point_f
                from weather_forecast_points
                where {run_col}=%s and source in ('open_meteo', 'open-meteo')
                  and {target_col} >= %s and {target_col} < %s
                order by {target_col} asc;
                """ ,
                (run_ts, start_ts, end_ts),
            )
            return cur.fetchall() or []


def _write_optimizer_outputs(
    *,
    run_ts: datetime,
    horizon_hours: int,
    rh_target_percent: float,
    solver: str,
    warnings: str,
    schedule_rows: List[Tuple[str, datetime, datetime, bool]],
    rh_rows: List[Tuple[str, datetime, Optional[float]]],
) -> str:
    run_id = uuid.uuid4()
    run_id_s = str(run_id)
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into optimizer_runs (run_id, run_ts, horizon_hours, solver, rh_target_percent, app_version, warnings)
                values (%s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    run_id_s,
                    run_ts,
                    int(horizon_hours),
                    str(solver),
                    float(rh_target_percent),
                    str(APP_VERSION),
                    str(warnings),
                ),
            )

            psycopg2.extras.execute_values(
                cur,
                """
                insert into dehumidifier_schedule_slots (run_id, channel_id, slot_start_ts, slot_end_ts, is_on)
                values %s;
                """,
                [(run_id_s, dg, s, e, bool(on)) for (dg, s, e, on) in schedule_rows],
            )

            psycopg2.extras.execute_values(
                cur,
                """
                insert into predicted_rh_points (run_id, series, ts, rh_percent)
                values %s;
                """,
                [(run_id_s, series, ts, (None if rh is None else float(rh))) for (series, ts, rh) in rh_rows],
            )
        conn.commit()

    return run_id_s


def main() -> None:
    _heartbeat(f"optimizer_start build_id={OPTIMIZER_BUILD_ID} app_version={APP_VERSION} file={__file__}")
    _ensure_optimizer_tables()

    tz = ZoneInfo(LOCAL_TZ)
    now_local = datetime.now(tz)
    run_local = now_local.replace(hour=8, minute=0, second=0, microsecond=0)
    # If invoked after 8am local, run immediately for the current day; if before, use today at 8.
    # Render cron will run at 8am, so this is just a guard.
    run_ts = run_local.astimezone(timezone.utc)

    horizon_hours = int(max(1, OPTIMIZER_HORIZON_HOURS))
    rh_target = 55.0

    warnings: List[str] = []

    # Required sensor roles for the summary-only UI.
    roles_needed = [
        "basement_far",
        "basement_near",
        "outside",
        "big_room_far",
        "big_room_near",
        "entrance",
        "upstairs",
    ]

    sensors = _latest_sensor_values(datetime.now(timezone.utc))
    missing_roles = [r for r in roles_needed if r not in sensors]
    if missing_roles:
        warnings.append(f"missing_sensor_roles:{','.join(missing_roles)}")

    latest_run = _fetch_latest_open_meteo_run_ts(datetime.now(timezone.utc))
    if latest_run is None:
        warnings.append("missing_open_meteo_run")

    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def _rh(role: str) -> Optional[float]:
        v = sensors.get(role, {}).get("rh_percent")
        try:
            return None if v is None else float(v)
        except Exception:
            return None

    bfar = _rh("basement_far")
    bnear = _rh("basement_near")

    basement_max = None if (bfar is None and bnear is None) else max(v for v in [bfar, bnear] if v is not None)

    channels = [
        "basement",
        "big_room_far",
        "big_room_near",
        "entrance",
        "upstairs",
    ]

    initial_rh: Dict[str, Optional[float]] = {
        "basement": basement_max,
        "big_room_far": _rh("big_room_far"),
        "big_room_near": _rh("big_room_near"),
        "entrance": _rh("entrance"),
        "upstairs": _rh("upstairs"),
    }

    # Heuristic v1 (manual execution): simple hourly on/off with hysteresis.
    # We simulate RH with a crude drift model and choose ON when above target.
    rh_target = float(rh_target)
    hysteresis = 1.0
    params = {
        "basement": {"drift_off": 0.4, "delta_on": -1.5, "max_on_hours": 12},
        "big_room_far": {"drift_off": 0.3, "delta_on": -1.2, "max_on_hours": 10},
        "big_room_near": {"drift_off": 0.3, "delta_on": -1.2, "max_on_hours": 10},
        "entrance": {"drift_off": 0.25, "delta_on": -1.0, "max_on_hours": 8},
        "upstairs": {"drift_off": 0.25, "delta_on": -1.0, "max_on_hours": 8},
    }

    schedule_rows: List[Tuple[str, datetime, datetime, bool]] = []
    rh_rows: List[Tuple[str, datetime, Optional[float]]] = []

    for channel_id in channels:
        rh = initial_rh.get(channel_id)
        if rh is None:
            warnings.append(f"missing_rh_for_channel:{channel_id}")

        # Store initial point
        rh_rows.append((channel_id, start, rh))

        on_prev = False
        on_hours = 0
        p = params[channel_id]

        for h in range(horizon_hours):
            slot_start = start + timedelta(hours=h)
            slot_end = slot_start + timedelta(hours=1)

            if rh is None:
                on = False
            else:
                if rh >= (rh_target + hysteresis):
                    on = True
                elif rh <= (rh_target - hysteresis):
                    on = False
                else:
                    on = bool(on_prev)

                if on and on_hours >= int(p["max_on_hours"]):
                    on = False

            schedule_rows.append((channel_id, slot_start, slot_end, bool(on)))

            # Update simulated RH for next hour.
            if rh is not None:
                if on:
                    rh = float(max(0.0, min(100.0, float(rh) + float(p["delta_on"]))))
                    on_hours += 1
                else:
                    rh = float(max(0.0, min(100.0, float(rh) + float(p["drift_off"]))))

            rh_rows.append((channel_id, slot_end, rh))
            on_prev = bool(on)

    run_id = _write_optimizer_outputs(
        run_ts=run_ts,
        horizon_hours=horizon_hours,
        rh_target_percent=rh_target,
        solver="heuristic_v1",
        warnings=";".join(warnings),
        schedule_rows=schedule_rows,
        rh_rows=rh_rows,
    )

    _heartbeat(f"optimizer_run_ok run_id={run_id} horizon_hours={horizon_hours} warnings={';'.join(warnings)}")


if __name__ == "__main__":
    main()
