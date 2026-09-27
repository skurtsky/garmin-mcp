"""PostgreSQL access layer for the Garmin dashboard cache.

When DATABASE_URL is set, the dashboard reads pre-synced data from PostgreSQL
instead of calling Garmin live. The schema is auto-created on first connect.
"""
import logging
import os
import threading
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or DATABASE_URL


def _date_prefix(value) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else None


def _km_to_meters(value) -> int | None:
    if value is None:
        return None
    return round(float(value) * 1000)


def _meters_to_km(value) -> float | None:
    if value is None:
        return None
    return round(float(value) / 1000, 3)


def is_configured() -> bool:
    return bool(_database_url())


# A single dashboard page load makes 6-9 separate queries (today's metrics,
# trends, recent activities, weekly summaries, records, profile, goals, sync
# state). Each used to open — and TLS-handshake, and authenticate — a brand
# new connection, which on a remote, low-tier instance (e.g. an Azure B1
# burstable Postgres) can easily cost more than the query itself; that
# per-query handshake, not query execution, was the dominant cost of a page
# load. A small pool keeps a handful of connections warm and reuses them
# instead. Created lazily (not at import time) so importing this module
# without DATABASE_URL set — e.g. in tests — stays a no-op.
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    global _pool
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set; PostgreSQL sync requires a database connection string.")
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(database_url, min_size=1, max_size=5, open=True)
    return _pool


@contextmanager
def get_conn():
    with _get_pool().connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# Training plans (Claude Coach). Each plan is stored as its whole JSON
# document, keyed by meta.id; at most one is active, the rest are archived.
# Every change writes a full snapshot to training_plan_revisions so any edit
# can be undone. Completion and Garmin links live in their own table, keyed by
# workout id, so replacing or restoring the plan content never loses them.
TRAINING_PLAN_SCHEMA = """
    CREATE TABLE IF NOT EXISTS training_plans (
        id           TEXT PRIMARY KEY,
        status       TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'archived')),
        plan         JSONB NOT NULL,
        version      INTEGER NOT NULL DEFAULT 1,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        archived_at  TIMESTAMPTZ
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_training_plans_one_active
        ON training_plans ((true)) WHERE status = 'active';

    CREATE TABLE IF NOT EXISTS training_plan_revisions (
        plan_id     TEXT NOT NULL REFERENCES training_plans(id) ON DELETE CASCADE,
        version     INTEGER NOT NULL,
        plan        JSONB NOT NULL,
        source      TEXT NOT NULL,
        summary     TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (plan_id, version)
    );

    CREATE TABLE IF NOT EXISTS training_plan_workout_state (
        plan_id                TEXT NOT NULL REFERENCES training_plans(id) ON DELETE CASCADE,
        workout_id             TEXT NOT NULL,
        completed              BOOLEAN NOT NULL DEFAULT FALSE,
        completed_at           TIMESTAMPTZ,
        activity_id            BIGINT,
        notes                  TEXT,
        garmin_workout_id      BIGINT,
        garmin_scheduled_date  DATE,
        updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (plan_id, workout_id)
    );
"""


# The redesigned Activity / Trends / Fitness tabs. Garmin only exposes the
# current thresholds, race predictions and records, so the sync job keeps a
# daily copy of each to chart how they move. Metric values are stored in one
# unit per metric: watts, bpm, seconds per km (threshold pace), seconds per
# 100 m (CSS), ml/kg/min (VO2max).
DASHBOARD_HISTORY_SCHEMA = """
    CREATE TABLE IF NOT EXISTS threshold_snapshots (
        snapshot_date  DATE NOT NULL,
        source         TEXT NOT NULL CHECK (source IN ('garmin', 'plan')),
        metric         TEXT NOT NULL,
        value          REAL NOT NULL,
        synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (snapshot_date, source, metric)
    );

    CREATE TABLE IF NOT EXISTS race_prediction_snapshots (
        snapshot_date  DATE NOT NULL,
        distance       TEXT NOT NULL,
        seconds        INTEGER NOT NULL,
        synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (snapshot_date, distance)
    );

    -- One row each time a personal record changes, with the value it beat.
    CREATE TABLE IF NOT EXISTS personal_record_history (
        id                     SERIAL PRIMARY KEY,
        sport                  TEXT NOT NULL,
        record_type            TEXT NOT NULL,
        value_raw              REAL,
        value_formatted        TEXT,
        record_date            DATE,
        activity_id            BIGINT,
        previous_value_raw     REAL,
        previous_formatted     TEXT,
        previous_record_date   DATE,
        detected_at            TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_personal_record_history_date
        ON personal_record_history (record_date DESC);

    -- Best efforts inside each activity (fastest 5K within a run, best
    -- 20 minutes of a ride, ...), keyed like personal_records, so recent
    -- efforts close to a record can be found. value: seconds, or watts.
    CREATE TABLE IF NOT EXISTS activity_best_efforts (
        garmin_id      BIGINT NOT NULL,
        sport          TEXT NOT NULL,
        record_type    TEXT NOT NULL,
        value          REAL NOT NULL,
        activity_date  DATE,
        PRIMARY KEY (garmin_id, record_type)
    );
    CREATE INDEX IF NOT EXISTS idx_activity_best_efforts_type
        ON activity_best_efforts (sport, record_type, activity_date DESC);

    -- Manual corrections to automatic per-activity flags (commute detection).
    CREATE TABLE IF NOT EXISTS activity_overrides (
        garmin_id   BIGINT PRIMARY KEY,
        is_commute  BOOLEAN,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- App-wide settings as one JSON value per key (e.g. 'goal_race').
    CREATE TABLE IF NOT EXISTS app_settings (
        key         TEXT PRIMARY KEY,
        value       JSONB NOT NULL,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    INSERT INTO sync_state (data_type) VALUES
        ('threshold_snapshots'), ('race_predictions')
    ON CONFLICT DO NOTHING;
"""


def ensure_schema():
    """Create tables if they don't exist. Safe to call on every startup."""
    if not is_configured():
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_metrics (
                    metric_date      DATE PRIMARY KEY,
                    resting_hr       SMALLINT,
                    hrv              SMALLINT,
                    sleep_score      SMALLINT,
                    stress           SMALLINT,
                    steps            INTEGER,
                    training_load    REAL,
                    body_battery_wake  SMALLINT,
                    body_battery_drain SMALLINT,
                    sleep_data       JSONB,
                    health_data      JSONB,
                    readiness_data   JSONB,
                    training_data    JSONB,
                    training_status_data JSONB,
                    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS activities (
                    garmin_id        BIGINT PRIMARY KEY,
                    activity_date    TIMESTAMPTZ NOT NULL,
                    activity_type    TEXT NOT NULL,
                    name             TEXT,
                    distance_km      REAL,
                    duration_min     REAL,
                    avg_hr           SMALLINT,
                    training_load    REAL,
                    summary          JSONB,
                    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_activities_date
                    ON activities (activity_date DESC);

                CREATE TABLE IF NOT EXISTS activity_details (
                    garmin_id   BIGINT PRIMARY KEY,
                    detail      JSONB,
                    route       JSONB,
                    synced_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS personal_records (
                    sport           TEXT NOT NULL,
                    record_type     TEXT NOT NULL,
                    value_raw       REAL,
                    value_formatted TEXT,
                    record_date     DATE,
                    activity_id     BIGINT,
                    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (sport, record_type)
                );

                CREATE TABLE IF NOT EXISTS athlete_profile (
                    id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    profile_data JSONB,
                    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS active_goals (
                    id         SERIAL PRIMARY KEY,
                    goals_data JSONB,
                    synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS gear (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    type            TEXT,
                    status          TEXT,
                    usage_meters    BIGINT,
                    lifespan_meters BIGINT,
                    created_date    DATE,
                    retired_date    DATE,
                    source          TEXT NOT NULL DEFAULT 'garmin',
                    raw_data        JSONB,
                    last_synced     TIMESTAMPTZ NOT NULL DEFAULT now(),
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS bike_components (
                    id                      TEXT PRIMARY KEY,
                    gear_id                 TEXT NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
                    parent_gear_id          TEXT REFERENCES gear(id) ON DELETE SET NULL,
                    component_type          TEXT NOT NULL,
                    service_interval_meters BIGINT,
                    notify                  BOOLEAN NOT NULL DEFAULT TRUE,
                    install_date            DATE NOT NULL,
                    install_usage_meters    BIGINT NOT NULL DEFAULT 0,
                    notes                   TEXT,
                    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bike_components_parent_gear
                    ON bike_components (parent_gear_id, gear_id);

                CREATE TABLE IF NOT EXISTS bike_component_services (
                    id                  TEXT PRIMARY KEY,
                    bike_component_id   TEXT NOT NULL REFERENCES bike_components(id) ON DELETE CASCADE,
                    service_type        TEXT NOT NULL,
                    service_interval_meters BIGINT,
                    notify              BOOLEAN NOT NULL DEFAULT TRUE,
                    notes               TEXT,
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bike_component_services_type
                    ON bike_component_services (bike_component_id, lower(service_type));

                CREATE TABLE IF NOT EXISTS bike_component_service_logs (
                    id                         TEXT PRIMARY KEY,
                    bike_component_service_id  TEXT NOT NULL REFERENCES bike_component_services(id) ON DELETE CASCADE,
                    service_date               DATE NOT NULL,
                    service_datetime           TIMESTAMPTZ,
                    service_type               TEXT NOT NULL,
                    usage_meters               BIGINT NOT NULL,
                    notes                      TEXT,
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_bike_component_service_logs_date
                    ON bike_component_service_logs (service_date DESC, created_at DESC);

                CREATE TABLE IF NOT EXISTS sync_state (
                    data_type         TEXT PRIMARY KEY,
                    last_synced_date  DATE,
                    last_sync_time    TIMESTAMPTZ,
                    status            TEXT DEFAULT 'ok',
                    error_message     TEXT
                );

                INSERT INTO sync_state (data_type) VALUES
                    ('daily_metrics'), ('activities'), ('activity_details'),
                    ('personal_records'), ('athlete_profile'), ('active_goals'),
                    ('gear')
                ON CONFLICT DO NOTHING;
            """)
            cur.execute(TRAINING_PLAN_SCHEMA)
            cur.execute(DASHBOARD_HISTORY_SCHEMA)
    logger.info("Database schema verified")


# ── READ OPERATIONS (used by the dashboard) ──────────────────────────────────

def get_today_metrics(date_str: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM daily_metrics WHERE metric_date = %s", (date_str,)
            )
            return cur.fetchone()


def get_trend_metrics(start_date: str, end_date: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT metric_date, resting_hr, hrv, sleep_score, stress,
                          steps, training_load, body_battery_wake, body_battery_drain,
                          training_status_data
                   FROM daily_metrics
                   WHERE metric_date BETWEEN %s AND %s
                   ORDER BY metric_date""",
                (start_date, end_date),
            )
            return cur.fetchall()


def get_trend_series_rows(start_date: str, end_date: str) -> list[dict]:
    """Per-day trend values for the get_trends MCP tool, one row per synced
    day. HRV and body battery come out of the stored tool payloads rather than
    the scalar columns, since those JSON fields are what the live get_trends
    reads (the hrv column holds sleep's avgOvernightHrv, and the
    body_battery_* columns aren't populated by the sync job)."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT metric_date, resting_hr, sleep_score, stress, steps,
                          training_load,
                          COALESCE((readiness_data -> 'hrv' ->> 'last_night_avg')::numeric,
                                   hrv) AS hrv,
                          COALESCE((readiness_data -> 'body_battery' ->> 'highest')::numeric,
                                   body_battery_wake) AS body_battery_wake,
                          COALESCE((readiness_data -> 'body_battery' ->> 'drained')::numeric,
                                   body_battery_drain) AS body_battery_drain,
                          synced_at
                   FROM daily_metrics
                   WHERE metric_date BETWEEN %s AND %s
                   ORDER BY metric_date""",
                (start_date, end_date),
            )
            return cur.fetchall()


def get_recent_activities(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT garmin_id, activity_date, activity_type, name,
                          distance_km, duration_min, avg_hr, training_load, summary
                   FROM activities ORDER BY activity_date DESC LIMIT %s""",
                (limit,),
            )
            return cur.fetchall()


def get_weekly_activities(week_start: str, week_end: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT summary FROM activities
                   WHERE activity_date >= %s AND activity_date < %s
                   ORDER BY activity_date DESC""",
                (week_start, week_end),
            )
            return cur.fetchall()


def get_personal_records_from_db() -> dict:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM personal_records ORDER BY sport, record_type")
            rows = cur.fetchall()
    result: dict[str, list] = {}
    for row in rows:
        sport = row["sport"]
        result.setdefault(sport, []).append({
            "label": row["record_type"],
            "value_raw": row["value_raw"],
            "value_formatted": row["value_formatted"],
            "date": str(row["record_date"]) if row["record_date"] else None,
            "activity_id": row["activity_id"],
        })
    return result


def get_athlete_profile_from_db() -> dict | None:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT profile_data FROM athlete_profile WHERE id = 1")
            row = cur.fetchone()
            return row["profile_data"] if row else None


def get_active_goals_from_db() -> list | None:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT goals_data FROM active_goals ORDER BY synced_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            return row["goals_data"] if row else None


def get_activity_detail_from_db(garmin_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT detail, route FROM activity_details WHERE garmin_id = %s",
                (garmin_id,),
            )
            return cur.fetchone()


_ACTIVITY_BRIEF_COLUMNS = """garmin_id, activity_date, activity_type, name,
                              distance_km, duration_min, summary"""


def get_activities_in_range(start_date: str, end_date: str) -> list[dict]:
    """Synced activities from start_date up to (not including) end_date,
    oldest first — for matching them to a training plan's workouts."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""SELECT {_ACTIVITY_BRIEF_COLUMNS} FROM activities
                    WHERE activity_date >= %s AND activity_date < %s
                    ORDER BY activity_date""",
                (start_date, end_date),
            )
            return cur.fetchall()


def get_activities_by_ids(garmin_ids: list[int]) -> dict[int, dict]:
    """{garmin_id: row} for the synced activities among ``garmin_ids``."""
    if not garmin_ids:
        return {}
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_ACTIVITY_BRIEF_COLUMNS} FROM activities WHERE garmin_id = ANY(%s)",
                (list(garmin_ids),),
            )
            return {row["garmin_id"]: row for row in cur.fetchall()}


def get_activity_with_detail(garmin_id: int) -> dict | None:
    """The activity-detail page's full payload: the `activities` row's own
    columns (name, date, distance, duration, avg HR, training load, sport
    type) plus the matching `activity_details` row's `detail`/`route` JSONB.

    None when the activity hasn't been synced yet, or its detail row hasn't
    been populated yet (sync_garmin.py backfills `activity_details`
    separately from `activities`, so there's a lag after a brand new
    activity first appears).
    """
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT a.garmin_id, a.activity_date, a.activity_type, a.name,
                          a.distance_km, a.duration_min, a.avg_hr, a.training_load,
                          a.summary ->> 'date' AS local_start_iso,
                          d.detail, d.route
                   FROM activities a
                   JOIN activity_details d ON d.garmin_id = a.garmin_id
                   WHERE a.garmin_id = %s""",
                (garmin_id,),
            )
            return cur.fetchone()


def get_sync_state() -> dict:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM sync_state")
            return {row["data_type"]: dict(row) for row in cur.fetchall()}


def get_gear_items_from_db() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, name, type, status, usage_meters, lifespan_meters,
                          created_date, retired_date, source, raw_data
                   FROM gear
                   ORDER BY lower(name)"""
            )
            rows = cur.fetchall()

    items = []
    for row in rows:
        raw = dict(row["raw_data"] or {})
        raw.update({
            "uuid": row["id"],
            "name": row["name"],
            "activity_type": row["type"],
            "status": row["status"],
            "distance_km": _meters_to_km(row["usage_meters"]),
            "max_distance_km": _meters_to_km(row["lifespan_meters"]),
            "date_begin": row["created_date"].isoformat() if row["created_date"] else None,
            "date_end": row["retired_date"].isoformat() if row["retired_date"] else None,
            "source": row["source"],
        })
        items.append(raw)
    return items


def read_gear_tracker_data() -> dict:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT bc.*, component.name AS component_name,
                          component.source AS component_source,
                          parent.name AS parent_gear_name
                   FROM bike_components bc
                   JOIN gear component ON component.id = bc.gear_id
                   LEFT JOIN gear parent ON parent.id = bc.parent_gear_id
                   ORDER BY lower(coalesce(parent.name, '')), lower(component.name)"""
            )
            components = [
                {
                    "id": row["id"],
                    "bike_uuid": row["parent_gear_id"],
                    "bike_name": row["parent_gear_name"],
                    "name": row["component_name"],
                    "component_type": row["component_type"],
                    "install_date": row["install_date"].isoformat(),
                    "install_distance_km": _meters_to_km(row["install_usage_meters"]),
                    "maintenance_interval_km": _meters_to_km(row["service_interval_meters"]),
                    "linked_gear_uuid": row["gear_id"] if row["gear_id"] != row["id"] else None,
                    "notify": row["notify"],
                }
                for row in cur.fetchall()
            ]

            cur.execute(
                """SELECT *
                   FROM bike_component_services
                   ORDER BY lower(service_type)"""
            )
            component_services = [
                {
                    "id": row["id"],
                    "component_id": row["bike_component_id"],
                    "service_type": row["service_type"],
                    "service_interval_km": _meters_to_km(row["service_interval_meters"]),
                    "notify": row["notify"],
                    "notes": row["notes"],
                }
                for row in cur.fetchall()
            ]

            cur.execute(
                """SELECT bcsl.*
                   FROM bike_component_service_logs bcsl
                   JOIN bike_component_services bcs ON bcs.id = bcsl.bike_component_service_id
                   JOIN bike_components bc ON bc.id = bcs.bike_component_id
                   ORDER BY bcsl.service_date DESC, bcsl.created_at DESC
                   LIMIT 1000"""
            )
            maintenance_log = [
                {
                    "id": row["id"],
                    "service_id": row["bike_component_service_id"],
                    "date": row["service_date"].isoformat(),
                    "service_datetime": row["service_datetime"].isoformat() if row["service_datetime"] else None,
                    "action": row["service_type"],
                    "distance_at_service_km": _meters_to_km(row["usage_meters"]),
                    "notes": row["notes"],
                }
                for row in cur.fetchall()
            ]

    services_by_id = {service["id"]: service for service in component_services}
    components_by_service = {service["id"]: service["component_id"] for service in component_services}
    for entry in maintenance_log:
        entry["component_id"] = components_by_service.get(entry["service_id"])
        service = services_by_id.get(entry["service_id"])
        if service:
            entry["service_type"] = service["service_type"]

    return {
        "components": components,
        "component_services": component_services,
        "maintenance_log": maintenance_log,
    }


def write_gear_tracker_data(data: dict) -> None:
    components = data.get("components") or []
    component_services = data.get("component_services") or []
    maintenance_log = data.get("maintenance_log") or []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for component in components:
                parent_gear_id = component["bike_uuid"]
                cur.execute(
                    """INSERT INTO gear (id, name, source)
                       VALUES (%s, %s, 'manual')
                       ON CONFLICT (id) DO UPDATE SET
                           name = coalesce(gear.name, EXCLUDED.name),
                           updated_at = now()""",
                    (parent_gear_id, component.get("bike_name") or parent_gear_id),
                )
                component_gear_id = component.get("linked_gear_uuid") or component["id"]
                cur.execute(
                    """INSERT INTO gear (id, name, source)
                       VALUES (%s, %s, 'manual')
                       ON CONFLICT (id) DO UPDATE SET
                           name = EXCLUDED.name,
                           updated_at = now()""",
                    (component_gear_id, component["name"]),
                )

            cur.execute("DELETE FROM bike_component_service_logs")
            cur.execute("DELETE FROM bike_component_services")
            cur.execute("DELETE FROM bike_components")

            component_ids = {component["id"] for component in components}
            for component in components:
                component_type = component.get("component_type") or component.get("name") or "component"
                component_gear_id = component.get("linked_gear_uuid") or component["id"]
                cur.execute(
                    """INSERT INTO bike_components
                           (id, gear_id, parent_gear_id, component_type,
                            service_interval_meters, notify,
                            install_date, install_usage_meters, notes)
                              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        component["id"],
                        component_gear_id,
                        component["bike_uuid"],
                        component_type,
                        _km_to_meters(component.get("maintenance_interval_km")),
                        component.get("notify", True),
                        component["install_date"],
                        _km_to_meters(component.get("install_distance_km") or 0),
                        component.get("notes"),
                    ),
                )

            for service in component_services:
                if service.get("component_id") not in component_ids:
                    continue
                cur.execute(
                    """INSERT INTO bike_component_services
                           (id, bike_component_id, service_type,
                            service_interval_meters, notify, notes)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        service["id"],
                        service["component_id"],
                        service["service_type"],
                        _km_to_meters(service.get("service_interval_km")),
                        service.get("notify", True),
                        service.get("notes"),
                    ),
                )

            service_ids = {service["id"] for service in component_services}
            for entry in maintenance_log:
                service_id = entry.get("service_id")
                if service_id not in service_ids:
                    continue
                cur.execute(
                    """INSERT INTO bike_component_service_logs
                           (id, bike_component_service_id, service_date,
                            service_datetime, service_type, usage_meters, notes)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        entry["id"],
                        service_id,
                        entry["date"],
                        entry.get("service_datetime"),
                        entry["action"],
                        _km_to_meters(entry.get("distance_at_service_km") or 0),
                        entry.get("notes"),
                    ),
                )


# ── WRITE OPERATIONS (used by the sync job) ──────────────────────────────────

def upsert_daily_metric(date_str: str, **kwargs):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO daily_metrics (metric_date, resting_hr, hrv,
                       sleep_score, stress, steps, training_load,
                       body_battery_wake, body_battery_drain,
                       sleep_data, health_data, readiness_data,
                       training_data, training_status_data)
                   VALUES (%(date)s, %(rhr)s, %(hrv)s, %(sleep_score)s,
                       %(stress)s, %(steps)s, %(training_load)s,
                       %(bb_wake)s, %(bb_drain)s,
                       %(sleep_data)s, %(health_data)s,
                       %(readiness_data)s, %(training_data)s,
                       %(training_status_data)s)
                   ON CONFLICT (metric_date) DO UPDATE SET
                       resting_hr = EXCLUDED.resting_hr,
                       hrv = EXCLUDED.hrv,
                       sleep_score = EXCLUDED.sleep_score,
                       stress = EXCLUDED.stress,
                       steps = EXCLUDED.steps,
                       training_load = EXCLUDED.training_load,
                       body_battery_wake = EXCLUDED.body_battery_wake,
                       body_battery_drain = EXCLUDED.body_battery_drain,
                       sleep_data = EXCLUDED.sleep_data,
                       health_data = EXCLUDED.health_data,
                       readiness_data = EXCLUDED.readiness_data,
                       training_data = EXCLUDED.training_data,
                       training_status_data = EXCLUDED.training_status_data,
                       synced_at = now()""",
                {
                    "date": date_str,
                    "rhr": kwargs.get("rhr"),
                    "hrv": kwargs.get("hrv"),
                    "sleep_score": kwargs.get("sleep_score"),
                    "stress": kwargs.get("stress"),
                    "steps": kwargs.get("steps"),
                    "training_load": kwargs.get("training_load"),
                    "bb_wake": kwargs.get("body_battery_wake"),
                    "bb_drain": kwargs.get("body_battery_drain"),
                    "sleep_data": Jsonb(kwargs.get("sleep_data")),
                    "health_data": Jsonb(kwargs.get("health_data")),
                    "readiness_data": Jsonb(kwargs.get("readiness_data")),
                    "training_data": Jsonb(kwargs.get("training_data")),
                    "training_status_data": Jsonb(kwargs.get("training_status_data")),
                },
            )


def upsert_activity(garmin_id: int, **kwargs) -> bool:
    """Insert or refresh a synced activity. True when it's new — the first
    time this activity has been synced (xmax is 0 only on a fresh insert)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                       (garmin_id, activity_date, activity_type, name,
                        distance_km, duration_min, avg_hr, training_load, summary)
                   VALUES (%(id)s, %(date)s, %(type)s, %(name)s,
                       %(dist)s, %(dur)s, %(hr)s, %(load)s, %(summary)s)
                   ON CONFLICT (garmin_id) DO UPDATE SET
                       name = EXCLUDED.name,
                       summary = EXCLUDED.summary,
                       synced_at = now()
                   RETURNING (xmax = 0) AS inserted""",
                {
                    "id": garmin_id,
                    "date": kwargs.get("activity_date"),
                    "type": kwargs.get("activity_type"),
                    "name": kwargs.get("name"),
                    "dist": kwargs.get("distance_km"),
                    "dur": kwargs.get("duration_min"),
                    "hr": kwargs.get("avg_hr"),
                    "load": kwargs.get("training_load"),
                    "summary": Jsonb(kwargs.get("summary")),
                },
            )
            row = cur.fetchone()
            return bool(row and row[0])


def get_activity_ids_needing_detail(limit: int = 10) -> list[int]:
    """Activity ids with no detail row yet, or whose detail predates the
    parent activity's last sync, most recent activity first."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.garmin_id
                   FROM activities a
                   LEFT JOIN activity_details d ON d.garmin_id = a.garmin_id
                   WHERE d.garmin_id IS NULL OR d.synced_at < a.synced_at
                   ORDER BY a.activity_date DESC
                   LIMIT %s""",
                (limit,),
            )
            return [row[0] for row in cur.fetchall()]


def get_recent_activity_ids(limit: int = 10) -> list[int]:
    """The N most recent activity ids, regardless of whether their detail row
    already exists or is current — used by sync_garmin.py's --overwrite to
    force a re-fetch (e.g. after get_activity_detail_row starts extracting a
    field that a previous sync predates, like hr_zones or sub_activities)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT garmin_id FROM activities ORDER BY activity_date DESC LIMIT %s",
                (limit,),
            )
            return [row[0] for row in cur.fetchall()]


def upsert_activity_detail(garmin_id: int, detail: dict, route: list | None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activity_details (garmin_id, detail, route)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (garmin_id) DO UPDATE SET
                       detail = EXCLUDED.detail,
                       route = EXCLUDED.route,
                       synced_at = now()""",
                (garmin_id, Jsonb(detail), Jsonb(route) if route is not None else None),
            )
            _replace_best_efforts(cur, garmin_id, (detail or {}).get("best_efforts") or [])


def _replace_best_efforts(cur, garmin_id: int, efforts: list[dict]):
    """Index an activity's best efforts (from its detail row), dated by the
    activity's local start date."""
    cur.execute("DELETE FROM activity_best_efforts WHERE garmin_id = %s", (garmin_id,))
    for effort in efforts:
        cur.execute(
            """INSERT INTO activity_best_efforts
                   (garmin_id, sport, record_type, value, activity_date)
               SELECT %(id)s, %(sport)s, %(type)s, %(value)s,
                      (SELECT COALESCE(substring(summary ->> 'date', 1, 10)::date,
                                       activity_date::date)
                       FROM activities WHERE garmin_id = %(id)s)
               ON CONFLICT (garmin_id, record_type) DO UPDATE SET
                   value = EXCLUDED.value, activity_date = EXCLUDED.activity_date""",
            {"id": garmin_id, "sport": effort["sport"], "type": effort["record_type"],
             "value": effort["value"]},
        )


def _record_changed(old: dict, rec: dict) -> bool:
    old_value, new_value = old.get("value_raw"), rec.get("value_raw")
    if old_value is None or new_value is None:
        return old_value != new_value
    return abs(float(old_value) - float(new_value)) > 1e-6 or old.get("activity_id") != rec.get("activity_id")


def upsert_personal_records(records: dict):
    """Store the current records. When one changes, the value it replaced is
    logged to personal_record_history first — Garmin only ever returns the
    current best, so this log is the only record of the improvement."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT sport, record_type, value_raw, value_formatted, record_date, activity_id "
                        "FROM personal_records")
            existing = {(r["sport"], r["record_type"]): r for r in cur.fetchall()}
            for sport, recs in records.items():
                for rec in recs:
                    old = existing.get((sport, rec.get("label")))
                    if old is not None and _record_changed(old, rec):
                        cur.execute(
                            """INSERT INTO personal_record_history
                                   (sport, record_type, value_raw, value_formatted, record_date,
                                    activity_id, previous_value_raw, previous_formatted,
                                    previous_record_date)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (sport, rec.get("label"), rec.get("value_raw"), rec.get("value_formatted"),
                             _date_prefix(rec.get("date")), rec.get("activity_id"), old["value_raw"],
                             old["value_formatted"], old["record_date"]),
                        )
                    cur.execute(
                        """INSERT INTO personal_records
                               (sport, record_type, value_raw, value_formatted,
                                record_date, activity_id)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (sport, record_type) DO UPDATE SET
                               value_raw = EXCLUDED.value_raw,
                               value_formatted = EXCLUDED.value_formatted,
                               record_date = EXCLUDED.record_date,
                               activity_id = EXCLUDED.activity_id,
                               synced_at = now()""",
                        (sport, rec.get("label"), rec.get("value_raw"),
                         rec.get("value_formatted"), rec.get("date"),
                         rec.get("activity_id")),
                    )


def upsert_athlete_profile(profile_data: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO athlete_profile (id, profile_data)
                   VALUES (1, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       profile_data = EXCLUDED.profile_data,
                       synced_at = now()""",
                (Jsonb(profile_data),),
            )


def upsert_active_goals(goals_data: list):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO active_goals (goals_data) VALUES (%s)",
                (Jsonb(goals_data),),
            )


def upsert_gear_items(gear_items: list[dict]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            for item in gear_items:
                gear_id = item.get("uuid")
                if not gear_id:
                    continue
                cur.execute(
                    """INSERT INTO gear
                           (id, name, type, status, usage_meters,
                            lifespan_meters, created_date, retired_date,
                            source, raw_data, last_synced)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                               'garmin', %s, now())
                       ON CONFLICT (id) DO UPDATE SET
                           name = EXCLUDED.name,
                           type = EXCLUDED.type,
                           status = EXCLUDED.status,
                           usage_meters = EXCLUDED.usage_meters,
                           lifespan_meters = EXCLUDED.lifespan_meters,
                           created_date = EXCLUDED.created_date,
                           retired_date = EXCLUDED.retired_date,
                           source = EXCLUDED.source,
                           raw_data = EXCLUDED.raw_data,
                           last_synced = now(),
                           updated_at = now()""",
                    (
                        gear_id,
                        item.get("name") or gear_id,
                        item.get("activity_type"),
                        item.get("status"),
                        _km_to_meters(item.get("distance_km")),
                        _km_to_meters(item.get("max_distance_km")),
                        _date_prefix(item.get("date_begin")),
                        _date_prefix(item.get("date_end")),
                        Jsonb(item),
                    ),
                )


def update_sync_state(data_type: str, last_date: str, status: str = "ok",
                      error: str | None = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sync_state
                   SET last_synced_date = %s, last_sync_time = now(),
                       status = %s, error_message = %s
                   WHERE data_type = %s""",
                (last_date, status, error, data_type),
            )


# ── TRAINING PLANS ────────────────────────────────────────────────────────────

_PLAN_COLUMNS = "id, status, plan, version, created_at, updated_at, archived_at"


def get_training_plan(plan_id: str | None = None) -> dict | None:
    """One plan row (with its JSON), or the active plan when no id is given."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if plan_id is None:
                cur.execute(f"SELECT {_PLAN_COLUMNS} FROM training_plans WHERE status = 'active'")
            else:
                cur.execute(f"SELECT {_PLAN_COLUMNS} FROM training_plans WHERE id = %s", (plan_id,))
            return cur.fetchone()


def list_training_plans() -> list[dict]:
    """Every plan without its JSON body: active first, then newest."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, status, version, created_at, updated_at, archived_at,
                          plan->'meta'->>'event' AS event,
                          plan->'meta'->>'athlete' AS athlete,
                          plan->'meta'->>'planStartDate' AS start_date,
                          plan->'meta'->>'planEndDate' AS end_date
                   FROM training_plans
                   ORDER BY status = 'active' DESC, updated_at DESC"""
            )
            return cur.fetchall()


def _archive_other_active(cur, plan_id: str) -> None:
    cur.execute(
        """UPDATE training_plans SET status = 'archived', archived_at = now()
           WHERE status = 'active' AND id <> %s""",
        (plan_id,),
    )


def save_uploaded_training_plan(plan_id: str, plan: dict, summary: str) -> dict:
    """Insert a new plan, or replace an existing one's content, and make it
    the active plan (archiving whichever was active). Writes a revision."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _archive_other_active(cur, plan_id)
            cur.execute(
                f"""INSERT INTO training_plans (id, status, plan, version)
                    VALUES (%s, 'active', %s, 1)
                    ON CONFLICT (id) DO UPDATE SET
                        plan = EXCLUDED.plan,
                        status = 'active',
                        archived_at = NULL,
                        version = training_plans.version + 1,
                        updated_at = now()
                    RETURNING {_PLAN_COLUMNS}""",
                (plan_id, Jsonb(plan)),
            )
            row = cur.fetchone()
            _insert_revision(cur, row, "upload", summary)
            return row


def _insert_revision(cur, row: dict, source: str, summary: str) -> None:
    cur.execute(
        """INSERT INTO training_plan_revisions (plan_id, version, plan, source, summary)
           VALUES (%s, %s, %s, %s, %s)""",
        (row["id"], row["version"], Jsonb(row["plan"]), source, summary),
    )


def update_training_plan(plan_id: str, mutate, source: str) -> dict:
    """Read-modify-write one plan under a row lock.

    ``mutate(plan) -> (new_plan, summary)`` gets the stored JSON and may raise
    to abort (nothing is written). Bumps the version and writes a revision.
    Raises LookupError when the plan doesn't exist.
    """
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT plan FROM training_plans WHERE id = %s FOR UPDATE", (plan_id,))
            current = cur.fetchone()
            if current is None:
                raise LookupError(plan_id)
            new_plan, summary = mutate(current["plan"])
            cur.execute(
                f"""UPDATE training_plans
                    SET plan = %s, version = version + 1, updated_at = now()
                    WHERE id = %s
                    RETURNING {_PLAN_COLUMNS}""",
                (Jsonb(new_plan), plan_id),
            )
            row = cur.fetchone()
            _insert_revision(cur, row, source, summary)
            return row


def set_training_plan_status(plan_id: str, status: str) -> bool:
    """Activate (archiving the current active plan) or archive a plan."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM training_plans WHERE id = %s FOR UPDATE", (plan_id,))
            if cur.fetchone() is None:
                return False
            if status == "active":
                _archive_other_active(cur, plan_id)
                cur.execute(
                    """UPDATE training_plans SET status = 'active', archived_at = NULL
                       WHERE id = %s""",
                    (plan_id,),
                )
            else:
                cur.execute(
                    """UPDATE training_plans
                       SET status = 'archived', archived_at = coalesce(archived_at, now())
                       WHERE id = %s""",
                    (plan_id,),
                )
            return cur.rowcount > 0


def delete_training_plan(plan_id: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM training_plans WHERE id = %s", (plan_id,))
            return cur.rowcount > 0


def list_training_plan_revisions(plan_id: str, limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT version, source, summary, created_at
                   FROM training_plan_revisions
                   WHERE plan_id = %s
                   ORDER BY version DESC
                   LIMIT %s""",
                (plan_id, limit),
            )
            return cur.fetchall()


def last_upload_version(plan_id: str) -> int | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT max(version) FROM training_plan_revisions
                   WHERE plan_id = %s AND source = 'upload'""",
                (plan_id,),
            )
            return cur.fetchone()[0]


def get_training_plan_revision(plan_id: str, version: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT version, plan, source, summary, created_at
                   FROM training_plan_revisions
                   WHERE plan_id = %s AND version = %s""",
                (plan_id, version),
            )
            return cur.fetchone()


def get_workout_states(plan_id: str) -> dict[str, dict]:
    """{workout_id: state row} for every workout with tracked state."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT workout_id, completed, completed_at, activity_id, notes,
                          garmin_workout_id, garmin_scheduled_date, updated_at
                   FROM training_plan_workout_state WHERE plan_id = %s""",
                (plan_id,),
            )
            return {row["workout_id"]: row for row in cur.fetchall()}


_STATE_FIELDS = ("completed", "completed_at", "activity_id", "notes",
                 "garmin_workout_id", "garmin_scheduled_date")


def upsert_workout_state(plan_id: str, workout_id: str, **fields) -> dict:
    """Set some of a workout's state fields, leaving the others untouched."""
    unknown = set(fields) - set(_STATE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown workout state fields: {sorted(unknown)}")
    columns = list(fields)
    values = [fields[c] for c in columns]
    insert_cols = ", ".join(["plan_id", "workout_id", *columns])
    placeholders = ", ".join(["%s"] * (2 + len(columns)))
    updates = ", ".join([*(f"{c} = EXCLUDED.{c}" for c in columns), "updated_at = now()"])
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""INSERT INTO training_plan_workout_state ({insert_cols})
                    VALUES ({placeholders})
                    ON CONFLICT (plan_id, workout_id) DO UPDATE SET {updates}
                    RETURNING workout_id, completed, completed_at, activity_id, notes,
                              garmin_workout_id, garmin_scheduled_date, updated_at""",
                (plan_id, workout_id, *values),
            )
            return cur.fetchone()


def get_activity_brief(garmin_id: int) -> dict | None:
    """Date, type and name of a synced activity (for matching it to a plan)."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT garmin_id, activity_date, activity_type, name
                   FROM activities WHERE garmin_id = %s""",
                (garmin_id,),
            )
            return cur.fetchone()


# ── DASHBOARD HISTORY (threshold / prediction snapshots, records, efforts) ────

def upsert_threshold_snapshot(snapshot_date: str, source: str, values: dict):
    """Store one day's thresholds for a source ('garmin' or 'plan');
    ``values`` is {metric: value}, None values skipped."""
    rows = [(snapshot_date, source, metric, float(value))
            for metric, value in values.items() if value is not None]
    if not rows:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO threshold_snapshots (snapshot_date, source, metric, value)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (snapshot_date, source, metric) DO UPDATE SET
                       value = EXCLUDED.value, synced_at = now()""",
                rows,
            )


def get_threshold_snapshots(start_date: str, end_date: str) -> list[dict]:
    """Snapshots from start_date through end_date, oldest first."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT snapshot_date, source, metric, value FROM threshold_snapshots
                   WHERE snapshot_date BETWEEN %s AND %s
                   ORDER BY snapshot_date, source, metric""",
                (start_date, end_date),
            )
            return cur.fetchall()


def upsert_race_predictions(snapshot_date: str, predictions: dict):
    """Store one day's race predictions: {distance: seconds}."""
    rows = [(snapshot_date, distance, int(secs))
            for distance, secs in predictions.items() if secs]
    if not rows:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO race_prediction_snapshots (snapshot_date, distance, seconds)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (snapshot_date, distance) DO UPDATE SET
                       seconds = EXCLUDED.seconds, synced_at = now()""",
                rows,
            )


def get_race_prediction_snapshots(start_date: str, end_date: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT snapshot_date, distance, seconds FROM race_prediction_snapshots
                   WHERE snapshot_date BETWEEN %s AND %s
                   ORDER BY snapshot_date, distance""",
                (start_date, end_date),
            )
            return cur.fetchall()


def get_personal_record_history(since_date: str) -> list[dict]:
    """Record changes whose record date is on or after since_date, newest first."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT sport, record_type, value_raw, value_formatted, record_date,
                          activity_id, previous_value_raw, previous_formatted,
                          previous_record_date, detected_at
                   FROM personal_record_history
                   WHERE record_date >= %s
                   ORDER BY record_date DESC, detected_at DESC""",
                (since_date,),
            )
            return cur.fetchall()


def get_best_efforts(since_date: str) -> list[dict]:
    """Best efforts from activities on or after since_date, newest first."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT e.garmin_id, e.sport, e.record_type, e.value, e.activity_date, a.name
                   FROM activity_best_efforts e
                   LEFT JOIN activities a ON a.garmin_id = e.garmin_id
                   WHERE e.activity_date >= %s
                   ORDER BY e.activity_date DESC""",
                (since_date,),
            )
            return cur.fetchall()


def get_daily_activity_loads(start_date: str, end_date: str) -> dict[str, float]:
    """{local date: summed Garmin training load} for activities from
    start_date through end_date (inclusive), by each activity's local
    start date."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT day, SUM(load) FROM (
                       SELECT COALESCE(substring(summary ->> 'date', 1, 10),
                                       to_char(activity_date, 'YYYY-MM-DD')) AS day,
                              COALESCE(training_load, (summary ->> 'training_load')::real, 0) AS load
                       FROM activities
                       WHERE activity_date >= %s::date - 1 AND activity_date < %s::date + 2
                   ) t
                   WHERE day BETWEEN %s AND %s
                   GROUP BY day""",
                (start_date, end_date, start_date, end_date),
            )
            return {row[0]: float(row[1] or 0) for row in cur.fetchall()}


def get_activity_overrides(garmin_ids: list[int]) -> dict[int, dict]:
    if not garmin_ids:
        return {}
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT garmin_id, is_commute FROM activity_overrides WHERE garmin_id = ANY(%s)",
                (list(garmin_ids),),
            )
            return {row["garmin_id"]: row for row in cur.fetchall()}


def set_commute_override(garmin_id: int, is_commute: bool | None):
    """Mark an activity as a commute (True) or not (False), overriding the
    automatic detection; None removes the override."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if is_commute is None:
                cur.execute("DELETE FROM activity_overrides WHERE garmin_id = %s", (garmin_id,))
                return
            cur.execute(
                """INSERT INTO activity_overrides (garmin_id, is_commute)
                   VALUES (%s, %s)
                   ON CONFLICT (garmin_id) DO UPDATE SET
                       is_commute = EXCLUDED.is_commute, updated_at = now()""",
                (garmin_id, is_commute),
            )


def get_rides_in_range(start_date: str, end_date: str) -> list[dict]:
    """Rides (any cycling type) from start_date up to (not including)
    end_date, with the summary JSON commute detection reads."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""SELECT {_ACTIVITY_BRIEF_COLUMNS} FROM activities
                    WHERE activity_date >= %s AND activity_date < %s
                      AND (activity_type ILIKE '%%bik%%' OR activity_type ILIKE '%%cycl%%'
                           OR activity_type ILIKE '%%ride%%')
                    ORDER BY activity_date""",
                (start_date, end_date),
            )
            return cur.fetchall()


def get_setting(key: str):
    """An app setting's JSON value, or None when unset."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None


def set_setting(key: str, value):
    """Store (value) or clear (None) an app setting."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if value is None:
                cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))
                return
            cur.execute(
                """INSERT INTO app_settings (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (key, Jsonb(value)),
            )


def list_plan_revisions_for_thresholds() -> list[dict]:
    """Every revision of every plan (oldest first) — the plan-side threshold
    history backfill replays them."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT plan_id, version, plan, created_at FROM training_plan_revisions
                   ORDER BY created_at, plan_id, version"""
            )
            return cur.fetchall()


def get_vo2max_history_from_daily_metrics(start_date: str, end_date: str) -> list[dict]:
    """The VO2max values the daily-metrics sync already stored (in
    training_status_data), per day."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT metric_date,
                          (training_status_data -> 'vo2max' ->> 'running')::real AS running,
                          (training_status_data -> 'vo2max' ->> 'cycling')::real AS cycling
                   FROM daily_metrics
                   WHERE metric_date BETWEEN %s AND %s
                   ORDER BY metric_date""",
                (start_date, end_date),
            )
            return cur.fetchall()


def get_ride_ftp_history(start_date: str, end_date: str) -> list[dict]:
    """The FTP Garmin recorded on each synced ride (activity_details.detail
    ftp), one value per day — the fallback FTP history."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT DISTINCT ON (day) day, ftp FROM (
                       SELECT COALESCE(substring(a.summary ->> 'date', 1, 10),
                                       to_char(a.activity_date, 'YYYY-MM-DD')) AS day,
                              (d.detail ->> 'ftp')::real AS ftp, a.activity_date
                       FROM activities a JOIN activity_details d ON d.garmin_id = a.garmin_id
                       WHERE d.detail ->> 'ftp' IS NOT NULL
                   ) t
                   WHERE day BETWEEN %s AND %s
                   ORDER BY day, activity_date DESC""",
                (start_date, end_date),
            )
            return cur.fetchall()
