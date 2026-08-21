# Making the Dashboard Snappier — Implementation Guide

Two phases: **Phase 1** makes the existing in-memory design faster with no new
infrastructure; **Phase 2** adds a PostgreSQL database and a scheduled sync job
so pages never wait on Garmin at all.

---

## Current Bottlenecks

| Bottleneck | Impact |
|---|---|
| A cache miss fires **13 concurrent Garmin tasks**, including trends which itself fires up to **70–150 per-day API calls** (14d × 5 metrics, or 30d × 5) | First load after cache expiry takes 5–15 s |
| The dashboard cache lock is held for the **entire fetch** — concurrent requests queue behind the first slow one | Stacked latency when multiple tabs/users hit a cold cache |
| **All five tabs** (Today, Trends, Activity, Fitness, Gear) are rendered on every request | Larger HTML, more server CPU, even though only one tab is visible |
| No **response compression** | 200–400 KB HTML transferred uncompressed |
| The cache is **process-local** — a container restart or new revision starts cold | First request after every deploy or restart is slow |
| **Garmin tokens** live on the Azure File Share, but the dashboard cache does not survive restarts | Every restart → cold Garmin fetch |

---

## Phase 1 — Code Changes (No New Infrastructure)

### 1.1  Split the dashboard cache into freshness tiers

Replace the single `_dashboard_cache` dict with per-section caches that have
different TTLs.

**File: `tools/dashboard.py`**

```python
# Replace the single cache with a per-section cache.
import time, threading

_section_cache: dict[str, tuple[float, object]] = {}
_section_cache_lock = threading.Lock()
_refresh_in_progress: set[str] = set()

# Freshness tiers (seconds)
CACHE_TTLS = {
    # Today's metrics — change throughout the day
    "readiness":       120,
    "health":          120,
    "sleep":           300,      # sleep data only updates once per day
    "training":        120,
    "training_status": 300,

    # Activities — new ones appear after a sync
    "activities":      300,
    "week":            300,

    # Slow-changing — expensive to fetch, rarely stale
    "trends":          900,      # 15 min — the big one
    "personal_records": 3600,    # 1 hour
    "active_goals":    1800,     # 30 min
    "athlete":         3600,     # 1 hour
    "last_sync":       120,

    # Gear — local JSON, almost free
    "gear_status":     60,
}


def _get_cached(key: str):
    """Return (value, is_fresh). Value is None when nothing is cached."""
    with _section_cache_lock:
        entry = _section_cache.get(key)
        if entry is None:
            return None, False
        ts, value = entry
        ttl = CACHE_TTLS.get(key, 60)
        return value, (time.monotonic() - ts) < ttl


def _set_cached(key: str, value):
    with _section_cache_lock:
        _section_cache[key] = (time.monotonic(), value)
```

Then change `build_dashboard_data()` so it:
1. Returns stale cached values immediately for sections that have *any* cached
   data (even expired).
2. Kicks off a background refresh for expired sections (see 1.2).

This means the first page load ever is still slow (nothing cached), but every
subsequent load returns in **< 100 ms** — serving the previous snapshot while
fresh data is fetched in the background.

### 1.2  Background refresh (stale-while-revalidate)

The key pattern: **return stale data immediately, refresh in the background.**

```python
_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cache-refresh")

def _refresh_section(key: str, fn, args, kwargs):
    """Background-refresh a single section. Skips if already in progress."""
    with _section_cache_lock:
        if key in _refresh_in_progress:
            return
        _refresh_in_progress.add(key)
    try:
        value, err = _safe(fn, *args, **kwargs)
        if err is None:          # only update cache on success
            _set_cached(key, value)
    finally:
        with _section_cache_lock:
            _refresh_in_progress.discard(key)


def build_dashboard_data() -> dict:
    # ... (same lazy imports as today) ...

    now = _local_now()
    today = now.date().isoformat()

    tasks = { ... }  # same task dict as today

    data = {
        "date": today,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "tz_offset_hours": _tz_offset_hours(),
    }

    any_missing = False
    for key, (fn, args, kwargs) in tasks.items():
        cached_value, is_fresh = _get_cached(key)

        if cached_value is not None:
            # Serve cached (possibly stale) value
            data[key] = cached_value
            data[f"{key}_err"] = None

            # Schedule background refresh if stale
            if not is_fresh:
                _bg_executor.submit(_refresh_section, key, fn, args, kwargs)
        else:
            any_missing = True

    if any_missing:
        # First load — must fetch missing sections synchronously
        missing_tasks = {
            k: v for k, v in tasks.items()
            if _get_cached(k)[0] is None
        }
        results = _fetch_parallel(missing_tasks)
        for key, (value, err) in results.items():
            data[key] = value
            data[f"{key}_err"] = err
            if err is None:
                _set_cached(key, value)

    return data
```

**Result:** After the first cold load, every subsequent `/dashboard` request
returns in tens of milliseconds regardless of Garmin latency. The data shown is
at most one TTL period old.

### 1.3  Add response compression

**File: `server.py`**, in `build_asgi_app()`:

```python
# Add GZipMiddleware from Starlette (already in requirements)
from starlette.middleware.gzip import GZipMiddleware

def build_asgi_app():
    # ... existing code ...
    # Wrap the final app with gzip
    from starlette.middleware.gzip import GZipMiddleware
    gzip_app = GZipMiddleware(auth_app, minimum_size=500)
    return gzip_app
```

> **Note:** `GZipMiddleware` wraps an ASGI app, and `auth_app` is already an
> ASGI callable, so this works directly — no Starlette `app` instance needed.

This typically reduces the ~200–400 KB dashboard HTML to 30–60 KB.

### 1.4  Remove the blocking cache lock

The current code holds `_dashboard_cache_lock` during the entire
`build_dashboard_data()` call. With the per-section cache above this is no
longer needed — the per-key lock granularity means concurrent requests never
block each other.

### 1.5  Add a `Last-Synced` indicator

Show the user when data was last refreshed so stale data is transparent:

```python
# In render_dashboard_html, add to the header:
sync_age = ""
if data.get("generated_at"):
    sync_age = f'<span class="muted" style="font-size:11px">Data as of {data["generated_at"]}</span>'
```

### 1.6  Summary of Phase 1 changes

| File | Change |
|---|---|
| `tools/dashboard.py` | Per-section cache with tiered TTLs, stale-while-revalidate background refresh, remove blocking lock |
| `server.py` | Add `GZipMiddleware` wrapping `auth_app` |
| `tools/dashboard.py` | Show "Data as of" timestamp in the rendered HTML |
| `requirements.txt` | No changes needed — `starlette` already includes `GZipMiddleware` |

**Expected improvement:** Dashboard loads drop from 5–15 s (cold) to < 100 ms
(warm), with data at most 2–15 minutes old depending on the section.

---

## Phase 2 — PostgreSQL + Scheduled Sync

### 2.1  Why PostgreSQL

| Option | Fit |
|---|---|
| **SQLite on Azure File Share** | Won't work — SMB doesn't support POSIX file locking (you already hit this with gear tracker, issue #60) |
| **SQLite on container filesystem** | Lost on every restart or revision swap |
| **Azure Database for PostgreSQL Flexible Server** | Durable, supports multiple replicas, managed backups, ~$15/mo for Burstable B1ms |
| **Azure Cosmos DB** | Overkill for a single-user dashboard |
| **Azure SQL** | Works but more expensive at the low end |

### 2.2  Create the PostgreSQL server

```powershell
# Create the PostgreSQL Flexible Server (Burstable B1ms — cheapest tier)
az postgres flexible-server create `
  --name garmin-mcp-db `
  --resource-group garmin-mcp-rg `
  --location canadacentral `
  --sku-name Standard_B1ms `
  --tier Burstable `
  --storage-size 32 `
  --version 16 `
  --admin-user garminadmin `
  --admin-password '<STRONG_PASSWORD>' `
  --public-access 0.0.0.0     # allows Azure services; lock down further with VNet later

# Create the application database
az postgres flexible-server db create `
--resource-group garmin-mcp-rg `
--server-name garmin-mcp-db `
--name garmin

# Allow the Container App's outbound IPs (or use VNet integration later)
az postgres flexible-server firewall-rule create `
  --resource-group garmin-mcp-rg `
  --name garmin-mcp-db `
  --rule-name AllowContainerApp `
  --start-ip-address 0.0.0.0 `
  --end-ip-address 255.255.255.255
```

> **Production hardening:** Use VNet integration between the Container App
> Environment and the PostgreSQL server so the database is not exposed to the
> public internet. The firewall rule above is a starting point.

### 2.3  Database schema

Create a migration file or run directly:

```sql
-- Daily health metrics (one row per date)
CREATE TABLE daily_metrics (
    metric_date    DATE PRIMARY KEY,
    resting_hr     SMALLINT,
    hrv            SMALLINT,
    sleep_score    SMALLINT,
    stress         SMALLINT,
    steps          INTEGER,
    training_load  REAL,
    body_battery_wake  SMALLINT,
    body_battery_drain SMALLINT,
    sleep_data     JSONB,          -- full sleep payload
    health_data    JSONB,          -- full daily health payload
    readiness_data JSONB,          -- body battery / readiness
    training_data  JSONB,          -- training readiness / status
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Activities (one row per Garmin activity)
CREATE TABLE activities (
    garmin_id      BIGINT PRIMARY KEY,
    activity_date  TIMESTAMPTZ NOT NULL,
    activity_type  TEXT NOT NULL,
    name           TEXT,
    distance_km    REAL,
    duration_min   REAL,
    avg_hr         SMALLINT,
    training_load  REAL,
    summary        JSONB,          -- the full extracted summary dict
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_activities_date ON activities (activity_date DESC);

-- Activity details (lazy-loaded, one row per activity)
CREATE TABLE activity_details (
    garmin_id      BIGINT PRIMARY KEY REFERENCES activities(garmin_id),
    detail         JSONB,          -- full detail payload
    route          JSONB,          -- parsed GPX route
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Personal records
CREATE TABLE personal_records (
    sport          TEXT NOT NULL,
    record_type    TEXT NOT NULL,
    value_raw      REAL,
    value_formatted TEXT,
    record_date    DATE,
    activity_id    BIGINT,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sport, record_type)
);

-- Sync state — tracks what was last synced and when
CREATE TABLE sync_state (
    data_type         TEXT PRIMARY KEY,   -- 'daily_metrics', 'activities', 'records', ...
    last_synced_date  DATE,               -- the most recent date we've synced
    last_sync_time    TIMESTAMPTZ,        -- when the sync job last ran
    status            TEXT DEFAULT 'ok',  -- 'ok', 'error', 'running'
    error_message     TEXT
);

-- Seed sync state rows
INSERT INTO sync_state (data_type) VALUES
    ('daily_metrics'), ('activities'), ('personal_records'),
    ('athlete_profile'), ('active_goals');

-- Athlete profile (single row, refreshed daily)
CREATE TABLE athlete_profile (
    id             INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    profile_data   JSONB,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Active goals (refreshed periodically)
CREATE TABLE active_goals (
    id             SERIAL PRIMARY KEY,
    goal_data      JSONB,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2.4  Add a database access layer

**New file: `db.py`**

```python
import os
import logging
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor, Json

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")  # postgres://user:pass@host/garmin


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_today_metrics(date_str: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM daily_metrics WHERE metric_date = %s",
                (date_str,),
            )
            return cur.fetchone()


def get_trend_metrics(start_date: str, end_date: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT garmin_id, activity_date, activity_type, name,
                          distance_km, duration_min, avg_hr, training_load, summary
                   FROM activities ORDER BY activity_date DESC LIMIT %s""",
                (limit,),
            )
            return cur.fetchall()


def upsert_daily_metric(date_str: str, **kwargs):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO daily_metrics (metric_date, resting_hr, hrv,
                       sleep_score, stress, steps, training_load,
                       body_battery_wake, body_battery_drain,
                       sleep_data, health_data, readiness_data, training_data)
                   VALUES (%(date)s, %(rhr)s, %(hrv)s, %(sleep_score)s,
                       %(stress)s, %(steps)s, %(training_load)s,
                       %(bb_wake)s, %(bb_drain)s,
                       %(sleep_data)s, %(health_data)s,
                       %(readiness_data)s, %(training_data)s)
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
                    "sleep_data": Json(kwargs.get("sleep_data")),
                    "health_data": Json(kwargs.get("health_data")),
                    "readiness_data": Json(kwargs.get("readiness_data")),
                    "training_data": Json(kwargs.get("training_data")),
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
                    "summary": Json(kwargs.get("summary")),
                },
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


def get_sync_state() -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sync_state")
            return {row["data_type"]: dict(row) for row in cur.fetchall()}
```

### 2.5  Sync job

**New file: `sync_garmin.py`** — standalone script run by a Container Apps Job.

```python
"""Scheduled Garmin → PostgreSQL sync.

Fetches new/updated data from Garmin Connect and upserts it into PostgreSQL.
Designed to run as a Container Apps Job on a schedule (every 5–15 minutes).
"""
import logging
import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def sync_daily_metrics():
    """Sync the last 3 days of daily metrics (overlap catches corrections)."""
    from garmin_client import get_client
    from tools.health import get_sleep, get_daily_health, get_daily_readiness, get_training_readiness
    from tools.trends import _PER_DAY_FETCHERS
    import db

    client = get_client()
    today = date.today()

    # Always re-sync the last 3 days (Garmin can retroactively correct data)
    for offset in range(3):
        d = (today - timedelta(days=offset)).isoformat()
        logger.info(f"Syncing daily metrics for {d}")

        try:
            sleep_data = get_sleep(d)
        except Exception:
            sleep_data = None
        try:
            health_data = get_daily_health(d)
        except Exception:
            health_data = None
        try:
            readiness_data = get_daily_readiness(d)
        except Exception:
            readiness_data = None
        try:
            training_data = get_training_readiness(d)
        except Exception:
            training_data = None

        # Extract scalar values for indexed columns
        rhr = (health_data or {}).get("heart_rate", {}).get("resting_hr")
        hrv = (sleep_data or {}).get("avg_hrv")
        sleep_score = (sleep_data or {}).get("sleep_score")
        stress = (health_data or {}).get("stress", {}).get("avg_stress")

        # Steps and training load via per-day fetchers
        try:
            steps = _PER_DAY_FETCHERS["steps"](client, d) if "steps" in _PER_DAY_FETCHERS else None
        except Exception:
            steps = None
        try:
            training_load = _PER_DAY_FETCHERS["training_load"](client, d) if "training_load" in _PER_DAY_FETCHERS else None
        except Exception:
            training_load = None

        db.upsert_daily_metric(
            d, rhr=rhr, hrv=hrv, sleep_score=sleep_score,
            stress=stress, steps=steps, training_load=training_load,
            sleep_data=sleep_data, health_data=health_data,
            readiness_data=readiness_data, training_data=training_data,
        )

    db.update_sync_state("daily_metrics", today.isoformat())
    logger.info("Daily metrics sync complete")


def sync_activities():
    """Sync recent activities. Fetches the latest 20 and upserts."""
    from tools.activities import get_activities
    import db

    logger.info("Syncing activities")
    activities = get_activities(limit=20)
    for act in activities:
        db.upsert_activity(
            garmin_id=act["id"],
            activity_date=act.get("date"),
            activity_type=act.get("type"),
            name=act.get("name"),
            distance_km=act.get("distance_km"),
            duration_min=act.get("duration_min"),
            avg_hr=act.get("avg_hr"),
            training_load=act.get("training_load"),
            summary=act,
        )
    db.update_sync_state("activities", date.today().isoformat())
    logger.info(f"Synced {len(activities)} activities")


def sync_personal_records():
    """Sync personal records (cheap call, run less often)."""
    from tools.performance import get_personal_records
    import db

    logger.info("Syncing personal records")
    records = get_personal_records()
    with db.get_conn() as conn:
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
    db.update_sync_state("personal_records", date.today().isoformat())
    logger.info("Personal records sync complete")


def main():
    logger.info("Starting Garmin sync job")
    try:
        sync_daily_metrics()
        sync_activities()
        sync_personal_records()
        logger.info("Sync job completed successfully")
    except Exception:
        logger.exception("Sync job failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

### 2.6  Update the dashboard to read from PostgreSQL

**File: `tools/dashboard.py`** — replace `build_dashboard_data()`:

```python
def build_dashboard_data() -> dict:
    """Build dashboard data from PostgreSQL (fast) with Garmin fallback."""
    import db

    now = _local_now()
    today = now.date().isoformat()

    data = {
        "date": today,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "tz_offset_hours": _tz_offset_hours(),
    }

    # ── Read from database (fast — no Garmin calls) ──────────────────
    today_metrics = db.get_today_metrics(today)
    if today_metrics:
        data["readiness"] = today_metrics.get("readiness_data")
        data["health"]    = today_metrics.get("health_data")
        data["sleep"]     = today_metrics.get("sleep_data")
        data["training"]  = today_metrics.get("training_data")
    # ... fill readiness_err, health_err, etc. as None ...

    # Activities from DB
    data["activities"] = [
        row["summary"] for row in db.get_recent_activities(limit=20)
    ]
    data["activities_err"] = None

    # Trends from DB — query daily_metrics for the trend window
    from datetime import date as date_cls, timedelta
    trend_start = (date_cls.fromisoformat(today) - timedelta(days=29)).isoformat()
    trend_rows = db.get_trend_metrics(trend_start, today)
    # ... reshape trend_rows into the same format render_dashboard_html expects ...

    # Sync status for the "last synced" indicator
    data["sync_state"] = db.get_sync_state()

    # Gear status still comes from the JSON file (local, fast)
    from tools.gear_tracker import build_gear_status
    data["gear_status"], data["gear_status_err"] = _safe(build_gear_status)

    return data
```

The key insight: **trends are no longer 150 API calls** — they are a single
`SELECT ... WHERE metric_date BETWEEN x AND y` query that returns in < 10 ms.

### 2.7  Create the Container Apps Job

```powershell
# Create a scheduled job that runs sync_garmin.py every 10 minutes
az containerapp job create `
  --name garmin-sync `
  --resource-group garmin-mcp-rg `
  --environment $(az containerapp show `
      --name garmin-mcp `
      --resource-group garmin-mcp-rg `
      --query "properties.environmentId" -o tsv) `
  --image ghcr.io/skurtsky/garmin-mcp:latest `
  --cpu 0.25 --memory 0.5Gi `
  --trigger-type Schedule `
  --cron-expression "*/10 * * * *" `
  --replica-timeout 300 `
  --replica-retry-limit 1 `
  --command "python" "sync_garmin.py" `
  --env-vars `
      GARMIN_EMAIL=secretref:garmin-email `
      GARMIN_PASSWORD=secretref:garmin-password `
      DATABASE_URL=secretref:database-url `
  --secrets `
      garmin-email="<EMAIL>" `
      garmin-password="<PASSWORD>" `
      database-url="postgresql://garminadmin:<PASS>@garmin-mcp-db.postgres.database.azure.com/garmin?sslmode=require"

# The job shares the same container image — sync_garmin.py is already in /app.
# It just runs a different entrypoint command.
```

### 2.8  Add DATABASE_URL to the Container App

```powershell
az containerapp update `
  --name garmin-mcp `
  --resource-group garmin-mcp-rg `
  --set-env-vars `
      DATABASE_URL=secretref:database-url `
  --secrets `
      database-url="postgresql://garminadmin:<PASS>@garmin-mcp-db.postgres.database.azure.com/garmin?sslmode=require"
```

### 2.9  Mount the token share for the sync job

The sync job needs access to the Garmin session tokens on the same Azure File
Share already mounted for the web container:

```powershell
$STORAGE_KEY = az storage account keys list `
  --account-name garminmcpkurt `
  --resource-group garmin-mcp-rg `
  --query "[0].value" -o tsv

az containerapp job update `
  --name garmin-sync `
  --resource-group garmin-mcp-rg `
  --set-env-vars GARMIN_TOKEN_DIR="/mnt/garminconnect"

# Note: volume mounts for Container Apps Jobs use the same
# --volumes and --container-volume-mounts flags as regular Container Apps.
# Mount the 'garminconnect' share at /mnt/garminconnect.
```

### 2.10  Update `requirements.txt`

```
psycopg2-binary==2.9.10
```

### 2.11  Update `Dockerfile`

```dockerfile
# No structural changes needed — sync_garmin.py and db.py are already
# copied by the COPY line. Just make sure they are in the root:
COPY db.py .
COPY sync_garmin.py .
```

### 2.12  Update the deploy workflow

Add the sync job image update to `.github/workflows/deploy.yml`:

```yaml
  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - name: Azure login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - name: Update Container App with new image
        uses: azure/CLI@v2
        with:
          inlineScript: |
            az containerapp update \
              --name ${{ env.CONTAINER_APP_NAME }} \
              --resource-group ${{ env.RESOURCE_GROUP }} \
              --image ${{ needs.build-and-push.outputs.image }} \
              --set-env-vars DEPLOY_TIME="$(date +%Y%m%d%H%M%S)"

      # Keep the sync job on the same image as the web app
      - name: Update Sync Job with new image
        uses: azure/CLI@v2
        with:
          inlineScript: |
            az containerapp job update \
              --name garmin-sync \
              --resource-group ${{ env.RESOURCE_GROUP }} \
              --image ${{ needs.build-and-push.outputs.image }}
```

---

## Phase 2 Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                  Azure Container App Env                │
│                                                         │
│  ┌──────────────────┐      ┌─────────────────────────┐  │
│  │  garmin-mcp       │      │  garmin-sync (Job)      │  │
│  │  (Web + MCP)      │      │  cron: */10 * * * *     │  │
│  │                   │      │                         │  │
│  │  GET /dashboard   │      │  sync_garmin.py         │  │
│  │    → read from DB │      │    → Garmin API         │  │
│  │    → render HTML  │      │    → upsert into DB     │  │
│  │    → < 100 ms     │      │    → update sync_state  │  │
│  └────────┬──────────┘      └────────────┬────────────┘  │
│           │                              │               │
│           │         ┌────────────┐       │               │
│           └────────►│ PostgreSQL │◄──────┘               │
│                     │ Flexible   │                       │
│                     │ Server     │                       │
│                     └────────────┘                       │
│                                                         │
│  ┌────────────────────────────────────┐                  │
│  │  Azure File Share (garminmcpkurt)  │                  │
│  │  ├── garmin_tokens.json            │                  │
│  │  ├── gear-tracker/                 │                  │
│  │  ├── weekly-summaries/             │                  │
│  │  └── training-plan/                │                  │
│  └────────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────┘
```

---

## Cost Estimate (Monthly)

| Resource | SKU | Est. Cost |
|---|---|---|
| Container App (web) | 0.5 vCPU / 1 GiB, always-on | ~$15 |
| Container App Job (sync) | 0.25 vCPU / 0.5 GiB, 144 runs/day × ~30 s each | ~$1 |
| PostgreSQL Flexible Server | Burstable B1ms, 32 GB storage | ~$15 |
| Azure File Share | < 1 GB | ~$0.05 |
| **Total** | | **~$31/mo** |

---

## Implementation Order

1. **Phase 1 first** — purely code changes, no infrastructure, deploy in one PR:
   - Per-section cache with tiered TTLs
   - Stale-while-revalidate background refresh
   - GZip compression
   - "Data as of" indicator
   
2. **Phase 2 in stages:**
   - a. Create PostgreSQL server and run the schema migration
   - b. Add `db.py` and `psycopg2-binary` to the project
   - c. Create `sync_garmin.py` and test locally against the database
   - d. Update `build_dashboard_data()` to read from DB with Garmin fallback
   - e. Update the Dockerfile, deploy workflow, and Container App env vars
   - f. Create the Container Apps Job
   - g. Verify the sync job runs and the dashboard reads from DB
   - h. Remove the direct-Garmin code path from the dashboard once stable

---

## Does This Work on Azure Container Apps?

Yes — every component here is a first-class Azure Container Apps feature:

| Capability | Azure Container Apps support |
|---|---|
| Scheduled background jobs | Container Apps Jobs with `--trigger-type Schedule` |
| PostgreSQL connectivity | Outbound TCP to Flexible Server (or VNet-integrated) |
| Shared secrets | `--secrets` and `secretref:` env vars |
| File share mounts | Azure Files volume mounts (already used for tokens) |
| Multiple containers, same image | Web app + Job can share the GHCR image, different entrypoints |
| Zero-downtime deploys | Built-in rolling revision updates |

The sync job uses the **same Docker image** as the web app — it just runs
`python sync_garmin.py` instead of `python server.py`. No separate build
pipeline needed.
