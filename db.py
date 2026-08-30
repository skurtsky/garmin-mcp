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


def upsert_activity(garmin_id: int, **kwargs):
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
                       synced_at = now()""",
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


def upsert_personal_records(records: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            for sport, recs in records.items():
                for rec in recs:
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
