import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

from zoneinfo import ZoneInfo

from main import _db_connect


psycopg2.extras.register_uuid()

LOCAL_TZ = os.getenv("LOCAL_TZ", "America/New_York")
OPTIMIZER_HORIZON_HOURS = int(os.getenv("OPTIMIZER_HORIZON_HOURS", "24"))
OPTIMIZER_SLOT_MINUTES = int(os.getenv("OPTIMIZER_SLOT_MINUTES", "60"))

APP_VERSION = os.getenv("APP_VERSION", "dev")
OPTIMIZER_BUILD_ID = os.getenv("OPTIMIZER_BUILD_ID", "2026-06-10-milp")
OPTIMIZER_PROCESSOR_TYPE = os.getenv("OPTIMIZER_PROCESSOR_TYPE", "cpu")

RH_THRESHOLD = float(os.getenv("OPT_RH_THRESHOLD", "50.0"))
RH_TARGET = float(os.getenv("OPT_RH_TARGET", "48.0"))

ENERGY_PENALTY_PER_HOUR = float(os.getenv("OPT_ENERGY_PENALTY_PER_HOUR", "1.0"))
EXCEEDANCE_PENALTY_PER_RH_HOUR = float(os.getenv("OPT_EXCEEDANCE_PENALTY_PER_RH_HOUR", "50.0"))

DRIFT_OFF_RH_PER_HR_BASEMENT = float(os.getenv("OPT_DRIFT_OFF_BASEMENT", "0.40"))
DRY_ON_RH_PER_HR_BASEMENT = float(os.getenv("OPT_DRY_ON_BASEMENT", "1.50"))

DRIFT_OFF_RH_PER_HR_BIGROOM = float(os.getenv("OPT_DRIFT_OFF_BIGROOM", "0.30"))
DRY_ON_RH_PER_HR_BIGROOM = float(os.getenv("OPT_DRY_ON_BIGROOM", "1.20"))

DRIFT_OFF_RH_PER_HR_ENTRANCE = float(os.getenv("OPT_DRIFT_OFF_ENTRANCE", "0.25"))
DRY_ON_RH_PER_HR_ENTRANCE = float(os.getenv("OPT_DRY_ON_ENTRANCE", "1.00"))

DRIFT_OFF_RH_PER_HR_UPSTAIRS = float(os.getenv("OPT_DRIFT_OFF_UPSTAIRS", "0.25"))
DRY_ON_RH_PER_HR_UPSTAIRS = float(os.getenv("OPT_DRY_ON_UPSTAIRS", "1.00"))


def _heartbeat(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}", flush=True)


def _ensure_optimizer_execution_log_table() -> None:
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists optimizer_execution_logs (
                  execution_id uuid primary key,
                  optimizer_process text not null,
                  processor_type text not null,
                  runtime_seconds double precision,
                  app_version text,
                  optimizer_build_id text,
                  created_at timestamptz not null default now()
                );
                """
            )
        conn.commit()


def _log_optimizer_execution(*, optimizer_process: str, processor_type: str, runtime_seconds: Optional[float]) -> None:
    try:
        _ensure_optimizer_execution_log_table()
    except Exception:
        return

    try:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into optimizer_execution_logs (
                      execution_id,
                      optimizer_process,
                      processor_type,
                      runtime_seconds,
                      app_version,
                      optimizer_build_id
                    )
                    values (%s, %s, %s, %s, %s, %s);
                    """,
                    (
                        str(uuid.uuid4()),
                        str(optimizer_process),
                        str(processor_type),
                        (None if runtime_seconds is None else float(runtime_seconds)),
                        str(APP_VERSION),
                        str(OPTIMIZER_BUILD_ID),
                    ),
                )
            conn.commit()
    except Exception:
        return


def _latest_sensor_values(now_utc: datetime) -> Dict[str, Dict[str, Optional[float]]]:
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


def _ensure_optimizer_tables() -> None:
    with _db_connect() as conn:
        with conn.cursor() as cur:
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
            cur.execute(
                """
                create table if not exists predicted_rh_points (
                  run_id uuid not null references optimizer_runs(run_id) on delete cascade,
                  series text not null,
                  ts timestamptz not null,
                  rh_percent double precision not null,
                  created_at timestamptz not null default now(),
                  primary key (run_id, series, ts)
                );
                """
            )
        conn.commit()


def _write_optimizer_outputs(
    *,
    run_ts: datetime,
    horizon_hours: int,
    rh_target_percent: float,
    solver: str,
    warnings: str,
    schedule_rows: List[Tuple[str, datetime, datetime, bool]],
    predicted_rh_rows: List[Tuple[str, datetime, float]],
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
                [(run_id_s, cid, s, e, bool(on)) for (cid, s, e, on) in schedule_rows],
            )

            if predicted_rh_rows:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    insert into predicted_rh_points (run_id, series, ts, rh_percent)
                    values %s
                    on conflict do nothing;
                    """,
                    [(run_id_s, str(series), ts, float(rh)) for (series, ts, rh) in predicted_rh_rows],
                )
        conn.commit()

    return run_id_s


@dataclass(frozen=True)
class Channel:
    channel_id: str
    initial_rh: Optional[float]
    drift_off_per_hour: float
    dry_on_per_hour: float


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _build_channels(sensors: Dict[str, Dict[str, Optional[float]]]) -> Tuple[List[Channel], List[str]]:
    warnings: List[str] = []

    def _rh(role: str) -> Optional[float]:
        return _safe_float(sensors.get(role, {}).get("rh_percent"))

    bfar = _rh("basement_far")
    bnear = _rh("basement_near")
    basement_max = None if (bfar is None and bnear is None) else max(v for v in [bfar, bnear] if v is not None)

    channels = [
        Channel(
            channel_id="basement",
            initial_rh=basement_max,
            drift_off_per_hour=DRIFT_OFF_RH_PER_HR_BASEMENT,
            dry_on_per_hour=DRY_ON_RH_PER_HR_BASEMENT,
        ),
        Channel(
            channel_id="big_room_far",
            initial_rh=_rh("big_room_far"),
            drift_off_per_hour=DRIFT_OFF_RH_PER_HR_BIGROOM,
            dry_on_per_hour=DRY_ON_RH_PER_HR_BIGROOM,
        ),
        Channel(
            channel_id="big_room_near",
            initial_rh=_rh("big_room_near"),
            drift_off_per_hour=DRIFT_OFF_RH_PER_HR_BIGROOM,
            dry_on_per_hour=DRY_ON_RH_PER_HR_BIGROOM,
        ),
        Channel(
            channel_id="entrance",
            initial_rh=_rh("entrance"),
            drift_off_per_hour=DRIFT_OFF_RH_PER_HR_ENTRANCE,
            dry_on_per_hour=DRY_ON_RH_PER_HR_ENTRANCE,
        ),
        Channel(
            channel_id="upstairs",
            initial_rh=_rh("upstairs"),
            drift_off_per_hour=DRIFT_OFF_RH_PER_HR_UPSTAIRS,
            dry_on_per_hour=DRY_ON_RH_PER_HR_UPSTAIRS,
        ),
    ]

    for ch in channels:
        if ch.initial_rh is None:
            warnings.append(f"missing_rh_for_channel:{ch.channel_id}")

    return channels, warnings


def solve_milp_schedule(
    *,
    channels: List[Channel],
    start_ts: datetime,
    horizon_slots: int,
    slot_hours: float,
) -> Tuple[List[Tuple[str, datetime, datetime, bool]], List[Tuple[str, datetime, float]], List[str]]:
    try:
        from ortools.linear_solver import pywraplp
    except Exception as e:
        return [], [], [f"missing_dependency_ortools:{type(e).__name__}"]

    solver = pywraplp.Solver.CreateSolver("CBC")
    if solver is None:
        return [], [], ["milp_solver_unavailable"]

    u: Dict[Tuple[str, int], Any] = {}
    rh: Dict[Tuple[str, int], Any] = {}
    exc: Dict[Tuple[str, int], Any] = {}

    for ch in channels:
        for t in range(horizon_slots + 1):
            rh[(ch.channel_id, t)] = solver.NumVar(0.0, 100.0, f"rh_{ch.channel_id}_{t}")
        for t in range(horizon_slots):
            u[(ch.channel_id, t)] = solver.BoolVar(f"u_{ch.channel_id}_{t}")
            exc[(ch.channel_id, t)] = solver.NumVar(0.0, 100.0, f"exc_{ch.channel_id}_{t}")

    big_m = 200.0

    for ch in channels:
        if ch.initial_rh is None:
            solver.Add(rh[(ch.channel_id, 0)] == RH_TARGET)
        else:
            solver.Add(rh[(ch.channel_id, 0)] == float(ch.initial_rh))

        drift_off = float(ch.drift_off_per_hour) * float(slot_hours)
        dry_on = float(ch.dry_on_per_hour) * float(slot_hours)

        for t in range(horizon_slots):
            solver.Add(
                rh[(ch.channel_id, t + 1)]
                == rh[(ch.channel_id, t)] + drift_off - dry_on * u[(ch.channel_id, t)]
            )

            solver.Add(rh[(ch.channel_id, t)] - RH_THRESHOLD <= exc[(ch.channel_id, t)])
            solver.Add(exc[(ch.channel_id, t)] >= 0.0)

            solver.Add(rh[(ch.channel_id, t)] >= RH_TARGET - big_m + big_m * u[(ch.channel_id, t)])

    obj_terms = []
    for ch in channels:
        for t in range(horizon_slots):
            obj_terms.append(EXCEEDANCE_PENALTY_PER_RH_HOUR * exc[(ch.channel_id, t)] * slot_hours)
            obj_terms.append(ENERGY_PENALTY_PER_HOUR * u[(ch.channel_id, t)] * slot_hours)

    solver.Minimize(solver.Sum(obj_terms))

    status = solver.Solve()
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        return [], [], [f"milp_infeasible_or_failed:status={status}"]

    schedule_rows: List[Tuple[str, datetime, datetime, bool]] = []
    for ch in channels:
        for t in range(horizon_slots):
            slot_start = start_ts + timedelta(hours=float(t) * float(slot_hours))
            slot_end = slot_start + timedelta(hours=float(slot_hours))
            on = bool(round(u[(ch.channel_id, t)].solution_value()))
            schedule_rows.append((ch.channel_id, slot_start, slot_end, on))

    predicted_rh_rows: List[Tuple[str, datetime, float]] = []
    for ch in channels:
        for t in range(horizon_slots + 1):
            ts = start_ts + timedelta(hours=float(t) * float(slot_hours))
            predicted_rh_rows.append((ch.channel_id, ts, float(rh[(ch.channel_id, t)].solution_value())))

    return schedule_rows, predicted_rh_rows, []


def main() -> None:
    t0 = time.perf_counter()
    _heartbeat(f"optimizer_start build_id={OPTIMIZER_BUILD_ID} app_version={APP_VERSION} file={__file__}")

    _ensure_optimizer_tables()

    tz = ZoneInfo(LOCAL_TZ)
    now_local = datetime.now(tz)
    run_local = now_local.replace(hour=8, minute=0, second=0, microsecond=0)
    run_ts = run_local.astimezone(timezone.utc)

    horizon_hours = int(max(1, OPTIMIZER_HORIZON_HOURS))
    slot_minutes = int(max(5, OPTIMIZER_SLOT_MINUTES))
    slot_hours = float(slot_minutes) / 60.0
    horizon_slots = int(max(1, round(float(horizon_hours) / slot_hours)))

    warnings: List[str] = []

    sensors = _latest_sensor_values(datetime.now(timezone.utc))
    channels, w = _build_channels(sensors)
    warnings.extend(w)

    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    schedule_rows, predicted_rh_rows, w2 = solve_milp_schedule(
        channels=channels,
        start_ts=start,
        horizon_slots=horizon_slots,
        slot_hours=slot_hours,
    )
    warnings.extend(w2)

    if not schedule_rows:
        warnings.append("no_schedule_rows")

    run_id = _write_optimizer_outputs(
        run_ts=run_ts,
        horizon_hours=horizon_hours,
        rh_target_percent=float(RH_TARGET),
        solver="milp_v1",
        warnings=";".join(warnings),
        schedule_rows=schedule_rows,
        predicted_rh_rows=predicted_rh_rows,
    )

    elapsed = time.perf_counter() - t0
    _log_optimizer_execution(
        optimizer_process="optimizer_daily_milp.py",
        processor_type=OPTIMIZER_PROCESSOR_TYPE,
        runtime_seconds=elapsed,
    )

    _heartbeat(
        f"optimizer_run_ok run_id={run_id} horizon_hours={horizon_hours} slot_minutes={slot_minutes} "
        f"runtime_seconds={elapsed:.3f} warnings={';'.join(warnings)}"
    )


if __name__ == "__main__":
    main()
