"""PostgreSQL access layer for the Garmin dashboard cache.

When DATABASE_URL is set, the dashboard reads pre-synced data from PostgreSQL
instead of calling Garmin live. The schema is auto-created on first connect.
"""
import logging
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


def is_configured() -> bool:
    return bool(DATABASE_URL)


@contextmanager
def get_conn():
    conn = psycopg.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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

                CREATE TABLE IF NOT EXISTS sync_state (
                    data_type         TEXT PRIMARY KEY,
                    last_synced_date  DATE,
                    last_sync_time    TIMESTAMPTZ,
                    status            TEXT DEFAULT 'ok',
                    error_message     TEXT
                );

                INSERT INTO sync_state (data_type) VALUES
                    ('daily_metrics'), ('activities'), ('activity_details'),
                    ('personal_records'), ('athlete_profile'), ('active_goals')
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
                          steps, training_load, body_battery_wake, body_battery_drain
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
