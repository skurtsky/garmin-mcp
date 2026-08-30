# tools/dashboard.py
"""Server-rendered health dashboard — "Nocturne" design.

Gathers a live overview from the Garmin client and renders it as a single,
self-contained HTML page (inline CSS, no build step, no external requests —
the Phosphor icon font is embedded as a subsetted base64 woff2, and the small
amount of interactivity below is a handful of small inline `<script>` tags,
no CDN/library). Each request pulls fresh data server-side.

The four tabs (Today / Trends / Activity / Fitness) are all rendered into the
page on every load; switching tabs, the trend range (7d/14d/30d), and the
personal-records sport filter are pure CSS (`:checked` radio inputs driving
sibling visibility) — no JS is involved there.

Line/area trend charts and bar charts are touch/click-interactive: a small
inline JS module (`_CHART_JS`) reads each chart's data points off its markup
(a `data-points` JSON attribute on line charts, `data-date`/`data-value`
attributes on bar segments) and drives a shared tooltip + crosshair overlay
on mousemove/click/touch. The data itself stays server-rendered; JS only
handles pointer interaction.

The data-gathering entrypoint (`build_dashboard_data`) lazily imports the
underlying tool functions so that `render_dashboard_html` — a pure function of
a data dict — can be imported and exercised without a live Garmin session.
"""
import base64
import html
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

# Auto-refresh the browser page this often (seconds). 0 disables refresh.
REFRESH_SECONDS = int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "300"))

# The Trends tab's range toggle (7d/14d/30d) is backed by one get_trends() call
# fetched at this period; get_trends fetches its per-day metrics concurrently,
# but rhr/hrv/sleep_score/stress/training_load have no batch endpoint in the
# Garmin client, so each extra day still costs one more round-trip per metric.
# Defaults to 14d (2/3 of the toggle) rather than the full 30d/1m so a cold
# page load stays fast; bump to "1m" for the full 30d option if you don't mind
# the extra latency. Any of get_trends' periods works; only ranges <= the
# fetched window actually show a toggle button.
TREND_PERIOD = os.environ.get("DASHBOARD_TREND_PERIOD", "").strip() or "6m"

# Fallback daily step goal when the athlete has no active Garmin step goal.
DEFAULT_STEP_GOAL = int(os.environ.get("DASHBOARD_STEP_GOAL", "10000"))

_TREND_METRICS = ["rhr", "hrv", "sleep_score", "stress", "steps", "training_load"]

logger = logging.getLogger(__name__)

# ── PER-SECTION CACHE WITH TIERED TTLS ──────────────────────────────────────
# Each dashboard section has its own TTL based on how often its underlying
# Garmin data actually changes.  Stale entries are served immediately while a
# background thread refreshes them (stale-while-revalidate).

_SECTION_CACHE_TTLS: dict[str, int] = {
    "readiness":        120,
    "health":           120,
    "sleep":            300,
    "training":         120,
    "training_status":  300,
    "training_status_daily_history": 300,
    "activities":       300,
    "week":             300,
    "trends":           900,
    "personal_records": 3600,
    "active_goals":     1800,
    "athlete":          3600,
    "last_sync":        120,
    "gear_status":      60,
}

# A past week's activities are effectively immutable (issue 85 — Activity
# tab week navigation), so once fetched they're cached generously — a
# separate key per week_offset (see _activity_week_cache_key), never
# confused with "week" (always the *current*, still-accumulating week).
_ACTIVITY_WEEK_TTL = 3600

# (timestamp, value, error_message) per section key
_section_cache: dict[str, tuple[float, object, str | None]] = {}
_section_cache_lock = threading.Lock()
_refresh_in_progress: set[str] = set()
_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cache-refresh")


def _activity_week_cache_key(week_offset: int) -> str:
    return "week" if week_offset == 0 else f"week@offset={week_offset}"


def _clear_section_cache():
    """Drop all cached sections. Used by tests."""
    with _section_cache_lock:
        _section_cache.clear()
        _refresh_in_progress.clear()


def _tz_offset_hours() -> float:
    """Local-time offset from UTC, from DASHBOARD_TZ_OFFSET_HOURS (default 0)."""
    try:
        return float(os.environ.get("DASHBOARD_TZ_OFFSET_HOURS", "0"))
    except ValueError:
        return 0.0


def _local_now() -> datetime:
    """Current time in the configured local zone."""
    return datetime.now(timezone.utc) + timedelta(hours=_tz_offset_hours())


def _safe(fn, *args, **kwargs):
    """Run a data fetch, returning (result, error_message). Never raises."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 — a failed section must not break the page
        return None, f"{type(e).__name__}: {e}"


@contextmanager
def _timed(label: str):
    """Log how long one dashboard-data section took. Diagnostic only — logs
    at INFO so it shows up in normal server logs without extra config,
    letting a slow page load be traced to a specific section (DB query vs.
    a live Garmin call vs. something else) instead of guessed at."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        logger.info("dashboard timing: %-24s %6.0fms", label, (time.monotonic() - t0) * 1000)


def _fetch_last_sync() -> dict:
    """Last device-sync info from Garmin (upload time + device name).

    Kept as a module-level helper so it can be patched in tests and so the
    live client import stays lazy. Only used by the live-Garmin fallback
    path (build_dashboard_data) — the DB path uses _fetch_last_sync_from_db.
    """
    from garmin_client import get_client

    info = get_client().get_device_last_used() or {}
    return {
        "device_name": info.get("lastUsedDeviceName"),
        "upload_time": info.get("lastUsedDeviceUploadTime"),
    }


def _fetch_last_sync_from_db() -> dict | None:
    """The most recent sync_garmin.py run, from Postgres's own sync_state
    table — not a live Garmin lookup. Returns None if the sync job has never
    recorded a run, same as the live version returning nothing to show.

    ``upload_time`` is epoch milliseconds, matching the shape the live
    Garmin device-upload lookup returns, so the same rendering helpers
    (_fmt_sync_time / _sync_time_utc_iso) handle either source unchanged.
    """
    import db

    rows = [r for r in db.get_sync_state().values() if r.get("last_sync_time")]
    if not rows:
        return None
    latest = max(r["last_sync_time"] for r in rows)
    return {"device_name": None, "upload_time": int(latest.timestamp() * 1000)}


def _fetch_parallel(tasks: dict) -> dict:
    """Run independent ``_safe()`` fetches concurrently.

    Each dashboard section is its own blocking network call (or, for trends/
    gear, a batch of them) with no dependency on any other section — running
    them one after another just adds up their latencies instead of
    overlapping them. ``tasks`` maps a result key to ``(fn, args, kwargs)``;
    returns a dict of the same keys to ``(result, error_message)`` pairs.
    """
    with ThreadPoolExecutor(max_workers=len(tasks) or 1) as pool:
        futures = {
            key: pool.submit(_safe, fn, *args, **kwargs)
            for key, (fn, args, kwargs) in tasks.items()
        }
        return {key: future.result() for key, future in futures.items()}


def _get_cached(key: str, ttl: int | None = None) -> tuple[object | None, str | None, bool]:
    """Return (value, error, is_fresh). value is None when nothing is cached.

    ``ttl`` overrides the section's usual TTL — used for the Activity tab's
    per-week-offset cache entries (see ``_activity_week_cache_key``), which
    aren't in ``_SECTION_CACHE_TTLS`` since their key is dynamic.
    """
    with _section_cache_lock:
        entry = _section_cache.get(key)
        if entry is None:
            return None, None, False
        ts, value, err = entry
        ttl = ttl if ttl is not None else _SECTION_CACHE_TTLS.get(key, 120)
        return value, err, (time.monotonic() - ts) < ttl


def _set_cached(key: str, value, err: str | None = None):
    with _section_cache_lock:
        _section_cache[key] = (time.monotonic(), value, err)


def _refresh_section(key: str, fn, args, kwargs):
    """Background-refresh one section. Skips if already refreshing."""
    with _section_cache_lock:
        if key in _refresh_in_progress:
            return
        _refresh_in_progress.add(key)
    try:
        value, err = _safe(fn, *args, **kwargs)
        _set_cached(key, value, err)
    finally:
        with _section_cache_lock:
            _refresh_in_progress.discard(key)


def _build_trends_from_db(rows: list[dict], days: int) -> dict:
    """Reshape daily_metrics DB rows into the format _panel_trends expects."""
    metrics: dict[str, dict] = {}
    key_map = {
        "rhr": ("resting_hr", "bpm"),
        "hrv": ("hrv", "ms"),
        "sleep_score": ("sleep_score", "score"),
        "stress": ("stress", "level"),
        "steps": ("steps", "steps"),
        "training_load": ("training_load", "load"),
    }
    for key, (col, unit) in key_map.items():
        daily = [
            {"date": str(r["metric_date"]), "value": r.get(col)}
            for r in rows
        ]
        metrics[key] = {"unit": unit, "daily": daily}
    return {"period": TREND_PERIOD, "days": days, "metrics": metrics}


def _build_dashboard_data_from_db(week_offset: int = 0) -> dict | None:
    """Try to build dashboard data from PostgreSQL. Returns None if DB is
    empty or unavailable, signalling the caller to fall back to Garmin."""
    import db
    if not db.is_configured():
        return None

    _page_t0 = time.monotonic()
    try:
        now = _local_now()
        today = now.date().isoformat()

        with _timed("today_metrics"):
            today_metrics = db.get_today_metrics(today)
        if today_metrics is None:
            return None  # DB has no data yet, fall back to Garmin

        # Trends: fetch 180 days from DB
        trend_start = (now.date() - timedelta(days=179)).isoformat()
        with _timed("trend_metrics"):
            trend_rows = db.get_trend_metrics(trend_start, today)

        # Activities
        with _timed("recent_activities"):
            activities_rows = db.get_recent_activities(limit=20)
        activities = [r["summary"] for r in activities_rows if r.get("summary")]

        # Weekly summary: computed entirely from the activities table — no
        # live Garmin call. "week" is always the current week (feeds the
        # Today tab's load widget) and always freshly queried, since it's
        # still accumulating today. "activity_week" is whichever week the
        # Activity tab is browsing — the same object when week_offset is 0,
        # otherwise served from the same per-offset cache the live-Garmin
        # path uses (a closed week's activities don't change), so browsing
        # back to a week already viewed this session costs no DB round trip
        # at all rather than just a cheaper one (issue 85).
        from tools.activities import get_weekly_summary_from_db
        with _timed("weekly_summary(current)"):
            week_data, _ = _safe(get_weekly_summary_from_db)
        if week_offset == 0:
            activity_week_data, activity_week_err = week_data, None
        else:
            cache_key = _activity_week_cache_key(week_offset)
            cached_value, cached_err, is_fresh = _get_cached(cache_key, _ACTIVITY_WEEK_TTL)
            if is_fresh and (cached_value is not None or cached_err is not None):
                activity_week_data, activity_week_err = cached_value, cached_err
                logger.info("dashboard timing: weekly_summary(offset=%s) served from cache", week_offset)
            else:
                with _timed(f"weekly_summary(offset={week_offset})"):
                    activity_week_data, activity_week_err = _safe(get_weekly_summary_from_db, week_offset)
                _set_cached(cache_key, activity_week_data, activity_week_err)

        # Profile, records, goals from DB
        with _timed("personal_records"):
            personal_records = db.get_personal_records_from_db()
        with _timed("athlete_profile"):
            athlete = db.get_athlete_profile_from_db()
        with _timed("active_goals"):
            active_goals = db.get_active_goals_from_db()

        # last_sync: the sync job's own record of when it last ran (from
        # sync_state), not a live device-upload lookup.
        with _timed("last_sync"):
            last_sync, sync_err = _safe(_fetch_last_sync_from_db)

        # Gear is DB-backed when DATABASE_URL is configured; build_gear_status
        # falls back to Garmin only when the synced gear catalog is empty or
        # the app is running without PostgreSQL.
        from tools.gear_tracker import build_gear_status
        with _timed("gear_status"):
            gear_status, gear_err = _safe(build_gear_status)

        logger.info("dashboard timing: TOTAL %6.0fms (week_offset=%s)",
                    (time.monotonic() - _page_t0) * 1000, week_offset)

        data = {
            "date": today,
            "generated_at": now.strftime("%Y-%m-%d %H:%M"),
            "tz_offset_hours": _tz_offset_hours(),
            "readiness": today_metrics.get("readiness_data"),
            "readiness_err": None,
            "health": today_metrics.get("health_data"),
            "health_err": None,
            "sleep": today_metrics.get("sleep_data"),
            "sleep_err": None,
            "training": today_metrics.get("training_data"),
            "training_err": None,
            "training_status": today_metrics.get("training_status_data"),
            "training_status_err": None,
            "training_status_daily_history": _daily_training_status_history(trend_rows, now.date()),
            "training_status_daily_history_err": None,
            "activities": activities,
            "activities_err": None,
            "week": week_data,
            "week_err": None,
            "activity_week": activity_week_data,
            "activity_week_err": activity_week_err,
            "activity_week_offset": week_offset,
            "trends": _build_trends_from_db(trend_rows, len(trend_rows)),
            "trends_err": None,
            "personal_records": personal_records or {},
            "personal_records_err": None,
            "active_goals": active_goals or [],
            "active_goals_err": None,
            "athlete": athlete,
            "athlete_err": None,
            "last_sync": last_sync,
            "last_sync_err": sync_err,
            "gear_status": gear_status,
            "gear_status_err": gear_err,
        }
        return data
    except Exception as e:
        logger.warning(f"DB read failed, falling back to Garmin: {e}")
        return None


def build_dashboard_data(week_offset: int = 0) -> dict:
    """Fetch every dashboard section from the Garmin client, concurrently.

    Each section is fetched independently and its failure is captured rather
    than raised, so one unavailable metric never blanks the whole page.

    ``week_offset`` (0 = current week, 1 = last week, …) selects which week
    the Activity tab shows (issue 85 — week navigation); it never affects the
    Today tab's own "this week" widget, which always reflects the current
    week via the "week" key.
    """
    # Try PostgreSQL first (fast — no Garmin API calls for cached data)
    db_data = _build_dashboard_data_from_db(week_offset)
    if db_data is not None:
        return db_data

    # Fall back to Garmin live calls with per-section caching
    # Imported lazily so render_dashboard_html stays importable without a
    # configured Garmin client.
    from tools.health import (
        get_daily_health,
        get_daily_readiness,
        get_sleep,
        get_training_readiness,
        get_training_status,
        get_training_status_daily_history,
    )
    from tools.activities import get_activities, get_weekly_summary
    from tools.trends import get_trends
    from tools.performance import get_personal_records
    from tools.profile import get_athlete_profile
    from tools.challenges import get_active_goals
    from tools.gear_tracker import build_gear_status

    now = _local_now()
    today = now.date().isoformat()

    tasks = {
        "readiness":        (get_daily_readiness, (today,), {}),
        "health":           (get_daily_health, (today,), {}),
        "sleep":            (get_sleep, (today,), {}),
        "training":         (get_training_readiness, (today,), {}),
        "training_status":  (get_training_status, (today,), {}),
        "training_status_daily_history": (get_training_status_daily_history, (), {}),
        "activities":       (get_activities, (), {"limit": 20}),
        "week":             (get_weekly_summary, (), {}),
        "trends":           (get_trends, (), {"period": TREND_PERIOD, "metrics": _TREND_METRICS}),
        "personal_records": (get_personal_records, (), {}),
        "active_goals":     (get_active_goals, (), {}),
        "athlete":          (get_athlete_profile, (), {}),
        "last_sync":        (_fetch_last_sync, (), {}),
        "gear_status":      (build_gear_status, (), {}),
    }
    # cache key per task — same as the data key for every section above,
    # except "activity_week" below, whose cache key is offset-specific (see
    # _activity_week_cache_key) so a different past week never serves stale
    # data cached under a different offset.
    cache_keys = {key: key for key in tasks}

    # The Activity tab's own week when browsing away from the current week
    # (issue 85). Folded into the same fetch batch below — not bolted on
    # afterward — so it overlaps with every other section's fetch instead of
    # adding its own serial round-trip on top of an already-parallel page
    # load (that stacking is what made the nav arrows feel slow).
    if week_offset:
        tasks["activity_week"] = (get_weekly_summary, (), {"week_offset": week_offset})
        cache_keys["activity_week"] = _activity_week_cache_key(week_offset)

    data = {
        "date": today,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "tz_offset_hours": _tz_offset_hours(),
    }

    # Serve cached values where available; schedule background refreshes for
    # stale ones.  Only sections with no cached value at all are fetched
    # synchronously (first load after a container restart).
    missing_tasks: dict = {}
    for key, (fn, args, kwargs) in tasks.items():
        cache_key = cache_keys[key]
        ttl = _ACTIVITY_WEEK_TTL if key == "activity_week" else None
        cached_value, cached_err, is_fresh = _get_cached(cache_key, ttl)
        if cached_value is not None or cached_err is not None:
            data[key] = cached_value
            data[f"{key}_err"] = cached_err
            if not is_fresh:
                _bg_executor.submit(_refresh_section, cache_key, fn, args, kwargs)
        else:
            missing_tasks[key] = (fn, args, kwargs)

    if missing_tasks:
        results = _fetch_parallel(missing_tasks)
        for key, (value, err) in results.items():
            data[key] = value
            data[f"{key}_err"] = err
            _set_cached(cache_keys[key], value, err)

    if not week_offset:
        data["activity_week"] = data.get("week")
        data["activity_week_err"] = data.get("week_err")
    data["activity_week_offset"] = week_offset

    return data


def get_dashboard_data(week_offset: int = 0) -> dict:
    """Public entry point — returns a snapshot, warm or cold."""
    return build_dashboard_data(week_offset)


# ── FORMATTING HELPERS ──────────────────────────────────────────────────────

_ENUM_LABELS = {
    "GOOD_SLEEP_LAST_NIGHT": "Good sleep last night",
    "DAY_STRESSFUL_AND_INTENSIVE_EXERCISE": "Stressful day with intensive exercise",
}


def _humanize(value):
    """Map a raw Garmin enum string to human-readable text.

    Known values use a lookup table; anything that looks enum-like
    (ALL_CAPS and/or underscore_separated) is converted to sentence case.
    Already-readable phrases are returned unchanged.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    if text in _ENUM_LABELS:
        return _ENUM_LABELS[text]
    letters = text.replace("_", "")
    if "_" in text or " " not in text or (letters.isalpha() and letters.isupper()):
        spaced = text.replace("_", " ").strip()
        return spaced[:1].upper() + spaced[1:].lower()
    return text


def _e(value) -> str:
    """HTML-escape a value for safe interpolation, mapping None to an em dash."""
    if value is None:
        return "&mdash;"
    return html.escape(str(value))


def _label(value) -> str:
    """Human-readable, HTML-escaped enum label ('—' when missing)."""
    human = _humanize(value)
    return html.escape(human) if human else "&mdash;"


def _num(value, suffix: str = "") -> str:
    """Render a numeric value with an optional suffix, or an em dash if missing."""
    if value is None:
        return "&mdash;"
    return f"{_e(value)}{html.escape(suffix)}"


def _trim(value: float, decimals: int = 2) -> str:
    """Fixed-point formatting with trailing zeros trimmed (19.20 -> 19.2)."""
    s = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_km(v):
    if v is None:
        return "&mdash;"
    return f"{_trim(v)} km"


def _fmt_dur(minutes):
    """Minutes as 'Xh YY' (>=60) or 'YY min'."""
    if minutes is None:
        return "&mdash;"
    total = round(minutes)
    h, m = divmod(total, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _fmt_hm_clock(hours: float):
    """Decimal hours as '<span>H<sub>h</sub>MM</span>' style pieces -> (h, mm)."""
    if hours is None:
        return None, None
    total_min = round(hours * 60)
    h, m = divmod(total_min, 60)
    return h, m


def _short_date(iso_date):
    """'2026-08-13...' -> '13 Aug'."""
    if not iso_date:
        return None
    try:
        d = date.fromisoformat(str(iso_date)[:10])
        return f"{d.day} {d.strftime('%b')}"
    except ValueError:
        return None


def _month_year(value):
    """A date-ish string -> 'Aug 2026'; passthrough on parse failure."""
    if not value:
        return "&mdash;"
    text = str(value)[:10]
    try:
        d = date.fromisoformat(text)
        return d.strftime("%b %Y")
    except ValueError:
        return _e(text)


def _format_week_range(start: str, end: str) -> str:
    """'2026-08-17', '2026-08-23' -> 'Aug 17 – Aug 23, 2026' (the Activity
    tab's date-range indicator — issue 85). Spans a year boundary or a
    truncated/missing date gracefully."""
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except (ValueError, TypeError):
        return ""
    if s.year != e.year:
        return f"{s.strftime('%b')} {s.day}, {s.year} – {e.strftime('%b')} {e.day}, {e.year}"
    if s.month == e.month:
        return f"{s.strftime('%b')} {s.day}–{e.day}, {e.year}"
    return f"{s.strftime('%b')} {s.day} – {e.strftime('%b')} {e.day}, {e.year}"


# ── SPORT ICON / TINT ───────────────────────────────────────────────────────
# (Phosphor glyph, tint colour) per Garmin activity typeKey. The glyph is a
# PUA codepoint into the embedded subsetted Phosphor font (see _ICON_CSS).

_RUN = ""
_BIKE = ""
_SWIM = ""
_WALK = ""
_MOUNTAIN = ""
_BARBELL = ""
_LOTUS = ""
_BOAT = ""
_MEDAL = ""
_PULSE = ""
_WRENCH = ""
_HEARTBEAT = ""

# Icons the embedded Phosphor subset doesn't carry (More menu + its
# off-dashboard links, and FTP's "Lightning" glyph) — plain inline SVG, same
# approach as tools/navbar.py.
_MORE_ICON_PATH = ('<path d="M40,72H216a8,8,0,0,0,0-16H40a8,8,0,0,0,0,16Zm176,32H40a8,8,0,0,0,0,16H216a8,'
                   '8,0,0,0,0-16Zm0,48H40a8,8,0,0,0,0,16H216a8,8,0,0,0,0-16Z"/>')
_CALENDAR_ICON_PATH = ('<path d="M208 32h-24v-8a8 8 0 0 0-16 0v8H88v-8a8 8 0 0 0-16 0v8H48a16 16 0 0 0-16 16v160'
                       'a16 16 0 0 0 16 16h160a16 16 0 0 0 16-16V48a16 16 0 0 0-16-16Zm0 176H48V96h160v112Zm0-128H48'
                       'V48h24v8a8 8 0 0 0 16 0v-8h80v8a8 8 0 0 0 16 0v-8h24Z"/>')
_CHART_ICON_PATH = ('<path d="M40 216a8 8 0 0 1-8-8V48a8 8 0 0 1 16 0v152h168a8 8 0 0 1 0 16Zm40-40a8 8 0 0 1-8-8v-40a8'
                    ' 8 0 0 1 16 0v40a8 8 0 0 1-8 8Zm48 0a8 8 0 0 1-8-8V96a8 8 0 0 1 16 0v72a8 8 0 0 1-8 8Zm48 0a8 8'
                    ' 0 0 1-8-8V64a8 8 0 0 1 16 0v104a8 8 0 0 1-8 8Z"/>')
_LIGHTNING_ICON_PATH = ('<path d="M215.79,118.17a8,8,0,0,0-5-5.66L153.18,90.9l14.66-73.33a8,8,0,0,0-13.69-7l-112,120a8,'
                        '8,0,0,0,3,13l57.63,21.61L88.16,238.43a8,8,0,0,0,13.69,7l112-120A8,8,0,0,0,215.79,118.17Z'
                        'M109.37,214l10.47-52.38a8,8,0,0,0-5-9.06L62,132.71l84.62-90.66L136.16,94.43a8,8,0,0,0,5,9.06'
                        'l52.8,19.8Z"/>')


def _svg_icon(path: str, size: int = 19) -> str:
    return f'<svg viewBox="0 0 256 256" width="{size}" height="{size}" fill="currentColor" aria-hidden="true">{path}</svg>'


_SPORT_STYLE = {
    "running": (_RUN, "#e2734a"), "trail_running": (_RUN, "#e2734a"),
    "treadmill_running": (_RUN, "#e2734a"), "track_running": (_RUN, "#e2734a"),
    "indoor_running": (_RUN, "#e2734a"),
    "road_biking": (_BIKE, "#4fae72"), "cycling": (_BIKE, "#4fae72"),
    "indoor_cycling": (_BIKE, "#4fae72"), "mountain_biking": (_BIKE, "#4fae72"),
    "gravel_cycling": (_BIKE, "#4fae72"), "virtual_ride": (_BIKE, "#4fae72"),
    "lap_swimming": (_SWIM, "#4aa7d8"), "open_water_swimming": (_SWIM, "#4aa7d8"),
    "swimming": (_SWIM, "#4aa7d8"),
    "walking": (_WALK, "#9397ab"), "hiking": (_MOUNTAIN, "#9397ab"),
    "strength_training": (_BARBELL, "#a07fe0"), "yoga": (_LOTUS, "#7fc9b0"),
    "cardio": (_PULSE, "#d9a441"), "rowing": (_BOAT, "#4aa7d8"),
    "indoor_rowing": (_BOAT, "#4aa7d8"), "elliptical": (_PULSE, "#d9a441"),
    "multi_sport": (_MEDAL, "var(--color-accent)"),
}
_DEFAULT_SPORT_STYLE = (_PULSE, "var(--color-neutral-500)")


def _sport_style(sport):
    return _SPORT_STYLE.get(str(sport or "").lower(), _DEFAULT_SPORT_STYLE)


def _sport_label(sport):
    return html.escape(str(sport or "activity").replace("_", " ").title())


def _activity_big_stat(a: dict) -> tuple[str, str]:
    """(headline, sub) for an activity row — distance for most sports, duration
    for sports Garmin doesn't report distance for (strength, yoga, ...)."""
    if a.get("distance_km"):
        return _fmt_km(a["distance_km"]), _fmt_dur(a.get("duration_min"))
    return _fmt_dur(a.get("duration_min")), _sport_label(a.get("type"))


# ── SPARKLINE / CHART GEOMETRY ──────────────────────────────────────────────

def _fill_gaps(vals: list):
    """Forward/back-fill None gaps in a numeric series so a path stays continuous.
    Returns None if every value is missing."""
    present = [v for v in vals if v is not None]
    if not present:
        return None
    filled, last = [], present[0]
    for v in vals:
        if v is not None:
            last = v
        filled.append(last)
    return filled


def _spark(vals: list, w: int = 300, h: int = 78, p: int = 9):
    """Line/area SVG path geometry for a numeric series. Mirrors the design's
    JS spark() helper. Returns None if there's nothing to draw."""
    filled = _fill_gaps(vals)
    if filled is None or len(filled) < 2:
        return None
    vmin, vmax = min(filled), max(filled)
    span = (vmax - vmin) or 1
    n = len(filled)

    def sx(i):
        return w / 2 if n == 1 else p + i * (w - 2 * p) / (n - 1)

    def sy(v):
        return h - p - ((v - vmin) / span) * (h - 2 * p)

    pts = [(sx(i), sy(v)) for i, v in enumerate(filled)]
    line = " ".join(f"{'L' if i else 'M'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(pts))
    area = f"{line} L{pts[-1][0]:.1f} {h} L{pts[0][0]:.1f} {h} Z"
    avg = sum(filled) / n
    return {
        "line": line, "area": area, "min": vmin, "max": vmax, "avg": avg,
        "avgY": round(sy(avg), 1), "last": filled[-1], "first": filled[0],
        "points": [(round(x, 2), round(y, 2)) for x, y in pts],
    }


def _chart(series: dict, label: str, unit: str, lower_better: bool, days: int,
           big: bool = False, stroke: str | None = None, fill: str | None = None,
           chart_id: str | None = None):
    """A Trends-tab metric card's display values, sliced to the trailing `days`."""
    daily = (series or {}).get("daily") or []
    sliced = daily[-days:]
    vals = [p.get("value") for p in sliced]
    s = _spark(vals)
    if s is None:
        return None

    # Compare this period's average to the previous period's average.
    prev_sliced = daily[-(2 * days):-days] if len(daily) >= 2 * days else []
    prev_vals = [p.get("value") for p in prev_sliced if p.get("value") is not None]
    if prev_vals:
        prev_avg = sum(prev_vals) / len(prev_vals)
        delta = s["avg"] - prev_avg
        good = delta <= 0 if lower_better else delta >= 0
    else:
        delta = None
        good = True

    f = (lambda v: f"{round(v):,}") if big else (lambda v: str(round(v)))
    filled = _fill_gaps(vals)
    unit_suffix = f" {unit}" if unit else ""
    points = [
        {"x": x, "y": y, "d": _short_date(sliced[i].get("date")) or "",
         "v": f"{f(filled[i])}{unit_suffix}"}
        for i, (x, y) in enumerate(s["points"])
    ]
    delta_str = f"{'+' if delta > 0 else ''}{f(delta)}" if delta is not None else ""
    return {
        "label": label, "unit": unit,
        "value": f(s["avg"]), "current": f(s["last"]), "avg": f(s["avg"]),
        "lo": f(s["min"]), "hi": f(s["max"]),
        "delta": delta_str,
        "avgY": s["avgY"], "line": s["line"], "area": s["area"],
        "stroke": stroke or "var(--color-accent)", "fill": fill or "url(#gArea)",
        "deltaColor": "#7fc9b0" if good else "#cf8a80",
        "id": chart_id or "",
        "points_json": html.escape(json.dumps(points), quote=True),
    }


# ── ICON FONT (Phosphor, subsetted to the glyphs this page uses) ───────────

_PHOSPHOR_WOFF2_B64 = (
    "d09GMgABAAAAAA5wAA0AAAAAGhgAAA4fAAIZmgAAAAAAAAAAAAAAAAAAAAAAAAAAGxAcGgZgAIFMEQgKrWCmRQE2AiQDLAssAAQgBQYHIBtuFVGUjlYN4IuEbIpi9nJXPtQRvxpI47J5i7gU7C4bPBkhyaxR/Jy/96K8IMFTTUgqQawvDoWHaBALYknNkYonFVE6BWpGzUnFlFT54lT0B76X3Z7IBJOz2Q+i8IsisCmu3g3lWU3b02QNBF2A31PyTb5akAdw4Jt7VY9KVybbGbQDlbFkw7S4xzcvzVqDoJ8OANw7+39zpU1270qgurJARldW6Cox9+dmNzPZLHByROyKKJHkIubKJE/IGtHny0LJ2jYkcrpG0ZbeacD/Q5zWJYKH/gsEAoCGK9xxIWFh/SABGdpr5HAIUSfncJxkMXgAtYa6DmDR862R38EQF9R7E6M/68CfFo7daV+U8xFsFJiHLENnY1QgB+oTiWTcyMVROQiy/m+JdsAR41joOPyq4PWu12ffyN5Pfn+9tVtrRKuqVdNqaGVbZ7bu+W32nxanE0j/L0lGpOelo6RZnfd1Ku2U1PGXdvPaxUjeSZ5LnkgeSx5I7kvmSvr4zvU6AIILatuI0wkPrS8fxGWsEM5eS0vN3BGuW3ggoCgv18CHABxPeZ1yGCrkn4Za04SwKIvZTJtT4odi7FN8HHxtioAD3ert6u3a+9jYODg7uFxCd4NOSGqeqHzrXs5nW3na+wkzmc2OwAEEuvi7JPrY2XsHPBR2GGBRFJaIUdIs2gPQE3sP2hfoFl2XqIbaeEWtYZc87K2BxHK/ptVBChnsHQALNZdCU5Oin3UlZxZ2QMrcYViAXa07DHRV6FPV9D0AJb5ini6WXhDDO6l336F2BXr5ywvk16A21DH2oT1+TbSRBXfoPl9hFAVo1Q5MNOOiHV2Edq0RuBnHhANM30AcQx60eYIxZOALFYk4sSxNDN80D6hgQILRV0w7bQ9DxgaCwAIQZxNxjyYiQkaRGVWiO5wzSBcBAgNh1Y+gbMQQx1xzshrjIVSDt9U/UfjAOUO5zhPi1FGKmWLDEfCwP7vF3tA3ZYYQMKOotljL+8Dwzkx4G+YsLODvAfFqdipNA32fkfUJS5z+ja8v+wLj5lKBcmnjApo76455NqPyKgJK+U2o96xtxMQVMX6nU/4VxS8Mxc1h6heANDrhhCXVHP1qFBWGukWikCjm5MzT//AZ4dw/kIz2T/zoyV/9gcYkIwRpYzIbPzo5G4MJtBOQCHrxBIccyWIQhDvMUWDqOvNEwme+IhOC/RSPSv+EkKPzhxA1hro9cMYRh00pKFtW+GNWfV6iP9S2LGtXbImnddy++OwLpjAbX5TuCBsg3FxwA0N071s3K2nsGx/NwIAj9b54dEpBJSkzpKbKRWRC7HYqIsQ3p4u7isZFUV7vd/NrV56BSCAC6jR8195i4hvH2CO3Ldk0PHvot4dIb7a+dTNd+n8xeZOSb4sEj1wz4QMUUDHkV4fKvlNy+3t3ARyALKKCnTdKLKvkkYQ7G3YGFmx86aKvSbibPw46BPUPJQZCt9kQi5CDjv3UI3IYNwX0jeBf3CC89cyKz/VvX7/IbfYFdaoYAGsg+lRx2DSCPyq6k78ZZ/dhFq146RyVfK05Uyya+M7NOrKW7qsAumaTscbw+m/orm8gmP4emRBh2hAUGlN+Sb1gvwLiazmVCwlZuACV7vJnoP2JDJ4D23mUbUha0XHaHqrWQJJUbVnRyuO3RQaI8K+J9A6Bi2mnh46ICe1u4lqiJ6dA2sUCoGhlCrSgCvp2g/SP6D9D/1af7h/6imc3a7jQVzHhjFr+9JCrKE7T6CEOQwo7iulLwni4RDt+6GX/O/aaI/+Xfkbph3QPwbRgPG+kMloFglhhs2zTY3tFLyM2bh6V28s4Ys0WSIL//N6LogTpLXMBo1PsFhuGWBNVVUMZK9JvS8IxWc2ySr4fEtIgnD+wZQvq9j+UTi/Bcm+kiFSru+aCPiamCse//x6X9eKyjQMsYLOV5cj53+3xovmO7b3H0TOP2xq8jBowY38w9Qaq9BPOMBhT9PhAGA7b1KX48m7O7mFCfdWOgglDwEG90mWLe0mmWZlm2cI/sDTHxz+JJSaBnY/EyOrkFN2dYFwXrF2CEWO/OuxUiXancAf1RT32rWF5M2aJ6QJ7PlVC9YudvZa8fJ2UXwZ1LIeFh4ngDqQlix5OxSd3xL+N0AmOldgG63RYxbPmnRJx/mBaRVEbbciqv9bXA+dg1Y28h1i67/OjNGd29ANYKEe6+QWEv+trW2kNcZFoTi/aO4SQZc6CNy82igghzB9Yo21oaOfqWg2m9Hm7tHdYzKTdw7PB4U3X6Cp0ZtHE7iZ3w9CVodqvLgAol9+15G2RtZ3uV9qWhVkesiqDGUB/SyPvyib/XwuRUQCmepBUmrBJINiUACCr0gUvvJf88mttZqF2oBocZU1XHYGA6SaaBCCE+oshDpmtQ2IHsLInZunKHHbHu03GNk6bDOJspS/XwM5uHevNFljqnUFFMwhuq1WWKYYBTEKrl8MbjLu6l9nI5y1lyG97/7jJHOS7e7oLKvLhbImqeHVhiWPUQGptmHwvUucmPRIKHyWh76oUVAPAEr3H7PZb62MG9OjY0ceLYc7djY8PjWZA8Si3gBxA4O4Uh8oYMiSjsUwfqfumEQsioWgIX1I5kekR6VPH/vp19uXu61T1lGotc0jg7uXGN6jwWqkvjL5tlwVUc7nVAeidEJgYuH4uW86WDWKiQ+Pj755jAM1dt9jYsePc3MaNjW84a5UbADmUOWyOd6sN0mXVLITMt0eI9kSy3RK7bYaGYuEtrbnJbP7+LS3t2/crQFNzWl7ivMUkEjDacXtFkH3Fe7WsFxehXRDZlDg/D6m36D4FmZlJmuhoTVK8oqAPCRNgOnVWVn5Fs2hJ6mkAhMAr3JPneIeE5PpEUvXFYVNs14WSsDCJ8LptSliIhL4XbPEVFfF3EX2jgbJbWAoKa2wlIhsbzuP5lhXJhsbGD6S3viQ8B7L0/tC4IVm/cvNzcXJsVe+hpUU5RgnWYhNLeyo0MVmJSW/vq9Vv7iUl9YxRw4H6+jRLnxIQeqIFPauflIYdIG2MN1fIJcfpsrp8iS/XwsHnGRu06rj2GKURiTSj0E4d141UzXov5li4vpI8Mo8u28ERcr0LfMTnwkqImnXhCjlYnjG5UlehXecVGupRjHAC4+RNHCFPyGrIrxcILl//oXYEWU8ndSUUgc677IMp97unugIL9ovWZYezAyafO+vhgioy06+R7E7IsuLL4HqxIOppQZIy0eFg2b9vy24m09OnJpPl9t/cCodjaFLB0x07zNrDFBKvBPuxQF3h2tuEDxpFRyjSlU5u5Hy4nhw7oGc5uehShC0hcx1Z7FPLscDF49xrCjI0oNAoM0qrqqSPtbBwCkajRltQGEdhqFtHjAiQ/DeSA9b1lekiSiT9KLYqo/2+VdTTMyU/p1Hn2QhVzt8tKkzLnBteV9cla+mGH2Seqyg1t6ZPGgL/TxzV/4dbj2R/yQQyZnXnkO2ru6Xt2eNPUr/w3AWRJ66HZDr/6afYqs2dsyLbsM4AoDSKpeLE0VcGdtR3SRJ98/s4OzYLAMxmdXq6+s7QVNs7JHYYKORRXZeKJ3QJGi/NDD8vZUO9DI4D4jrDuHQLerbYwUPidDnZDwlD7tF0imLgoEAQ4iKzUXsp16IcCQVkCmZGaRLKI/tfGpkwR0OYjfzk5xEOLYC4qaHCgxWxdKZ6VtkqGrJeHHcvN7p+GrDIGhYXF3Zn6KsbfE74DBSCiqYoA8XKMcoW3o4k1tl2ksY2AHDPKQYOysubM1uVLgAAO3clB1aucNoOq9WNl4wavSz+TNAsldzij3IVBp6wPAGGCnmQrnfQZaNysbwyK345/1+hPB/7srPNLhs39stQLpKbs5ilfMGFRPyxMr9c4B7gxl/WA+asxXKj8nLvIG1wudyQ2yh0kz8SA7eiVPHDhyd5/PTOY2cFg6AXYX8lvQASavJLEI8anZenuDBs5pEji7MUK/5NkKUWF/d7ZgLMVsptztfAHmXwGNOa2c5+9YTlg/KHCQHAgWb47QlFM1Xdv9G4qOXmBKCmiebwvfDon3WSeIb8xFEwG78k6vhGBsyUJaOi4oXxAut20/a1lfGFYxdo4/kRV75lEgvZxjqOxisR3Wy+daxB5dFxPeNWFLSx3rlUBOUsyIVCG4kSKJll+SAAvlXdSO0THPyYK2Y5HFYM8Ojma8aaIU0FhYo/csOKFSvihhjiawMHxQ72wSP4rBnaVc8hI55WTbJ7+hm1XnsXYMDeA+GT7V5e9kmky99eEq6b3M1GovD85aTuVkX9ye3SaS1C5zc9rmrczVOxUMHhqBsjovPzo+8IQVCDNDvgrwB3OYgbrPWJxndfNduqiItaZTx/jhrYdHq/6qIQwFUAgouJ2NfcRA08d964lY1SFDTvoE0mXt72bZSyv0jUX0lt284gVqHf1WfZJLygXrEqrlrBRm0FrGvel3j4INIROQ0AE0F/p2uNvtVQqgOA0R/2yqqqbDCuaikD1a/bLnu4r5x98SJzkA8gQnuxMgB4h37uOF1x8mT3urqs3d3r759HX768fqxX5qhBWTQpBmDMHNsirtb1FdUphjzb6s0B/DN9HQW6qf9EnfjM1NWleHik3Dkz1hMHshVNigkTpE2K7LZb2oFG48DaW208J0XjB5vXXLvMu622dkyLk2ljYts4LWOYqM8Cq3HImPDv06wnGP8RDDPCnzlhnXZEMeBnyQRUu7WrwtinZo/oj0IXblTl6xDXNc+695t+PgjrdHDB2QVACCon/snRiBwSdx9/EUnsFhZ22KkFtbehATLIMBlpoyiA9QlD2OaLVxAeksJl3/ngAAAA"
)

_ICON_CSS = (
    "@font-face{font-family:'PhosphorSub';font-weight:400;font-style:normal;"
    f"src:url(data:font/woff2;base64,{_PHOSPHOR_WOFF2_B64}) format('woff2')}}"
    ".ph{font-family:'PhosphorSub';font-style:normal;speak:never;font-variant:normal;"
    "text-transform:none;line-height:1;-webkit-font-smoothing:antialiased}"
)


# ── DASHBOARD DATA SHAPING (build_dashboard_data() -> display-ready values) ─

_READINESS_COLORS = {
    "PRIME": "#4fae72", "HIGH": "#4fae72", "GOOD": "#4fae72",
    "MODERATE": "#d9a441", "FAIR": "#d9a441", "LOW": "#cf5a4e", "POOR": "#cf5a4e",
}


def _readiness_color(level):
    return _READINESS_COLORS.get(str(level or "").upper(), "var(--color-accent)")


# Garmin's trainingStatusFeedbackPhrase (e.g. "PRODUCTIVE_2") — (label, colour)
# per status. Colours follow the Garmin Connect app's own palette (issue 92).
_TRAINING_STATUS_INFO = {
    "PEAKING":      ("Peaking", "#9184d9"),        # purple
    "PRODUCTIVE":   ("Productive", "#4fae72"),     # green
    "MAINTAINING":  ("Maintaining", "#d9c23e"),    # yellow
    "STRAINED":     ("Strained", "#d9689a"),       # pink
    "UNPRODUCTIVE": ("Unproductive", "#e2734a"),   # orange
    "OVERREACHING": ("Overreaching", "#cf5a4e"),   # red
    "RECOVERY":     ("Recovery", "#4aa7d8"),       # blue
    "DETRAINING":   ("Detraining", "#9397ab"),     # gray
    "NO_STATUS":    ("No Status", "var(--color-neutral-500)"),
    "PAUSED":       ("Paused", "var(--color-neutral-600)"),
}
_DEFAULT_TRAINING_STATUS = _TRAINING_STATUS_INFO["NO_STATUS"]


def _training_status_key(raw) -> str | None:
    """Normalize a raw trainingStatusFeedbackPhrase ('PRODUCTIVE_2', 'no status')
    to its lookup key ('PRODUCTIVE', 'NO_STATUS')."""
    if not raw:
        return None
    parts = str(raw).strip().upper().replace("-", "_").replace(" ", "_").split("_")
    if len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    key = "_".join(p for p in parts if p)
    return key or None


def _training_status_info(raw) -> tuple[str, str]:
    """(label, colour) for a raw training-status phrase."""
    return _TRAINING_STATUS_INFO.get(_training_status_key(raw), _DEFAULT_TRAINING_STATUS)


def _daily_training_status_history(rows: list[dict], today: date, days: int = 28) -> list[dict]:
    """Daily training-status snapshots for the trailing `days` days ending on
    `today` (oldest first), sliced from the trend rows already fetched for
    the Trends tab's wide date window — no second DB query. Powers the Today
    tab's training-status history bar (issue 92)."""
    by_date: dict[date, str] = {}
    for r in rows:
        status = (r.get("training_status_data") or {}).get("status")
        if not status:
            continue
        d = r.get("metric_date")
        if not isinstance(d, date):
            try:
                d = date.fromisoformat(str(d)[:10])
            except ValueError:
                continue
        by_date[d] = status

    return [
        {"date": (today - timedelta(days=i)).isoformat(), "status": by_date.get(today - timedelta(days=i))}
        for i in range(days - 1, -1, -1)
    ]


def _training_status_day_segments(days_slice: list[dict], gap: bool) -> str:
    """One `.js-bar` colour segment per day. With `gap`, each segment is a
    small rounded, gapped block (7d view); without, segments sit flush
    against each other inside a rounded, clipped strip so same-coloured runs
    blend seamlessly (28d view)."""
    segments = []
    for day in days_slice:
        seg_label, seg_color = _training_status_info(day.get("status"))
        tooltip_date = _short_date(day.get("date")) or day.get("date") or ""
        radius = "border-radius:4px;" if gap else ""
        segments.append(
            f'<div class="js-bar" data-date="{_e(tooltip_date)}" data-value="{_e(seg_label)}" '
            f'style="flex:1;height:22px;{radius}background:{seg_color}"></div>'
        )
    return "".join(segments)


def _training_status_card(data: dict) -> str:
    ts = data.get("training_status") or {}
    history = data.get("training_status_daily_history") or []
    if not ts and not history and data.get("training_status_err"):
        return f'<div class="card err">Training status unavailable — {_e(data.get("training_status_err"))}</div>'

    label, color = _training_status_info(ts.get("status"))

    bar_7 = _training_status_day_segments(history[-7:], gap=True)
    bar_28 = _training_status_day_segments(history, gap=False)
    no_history = '<div class="muted" style="font-size:12px">No recent history.</div>'

    range_radios = (
        '<input class="hide" type="radio" name="ts-range" id="ts-range-7">'
        '<input class="hide" type="radio" name="ts-range" id="ts-range-28" checked>'
    )
    range_toggle = ('<div class="ts-toggle"><label for="ts-range-7">7d</label>'
                     '<label for="ts-range-28">28d</label></div>')

    return f"""
    <div>
      <div class="section-title">Training status</div>
      {range_radios}
      <div class="card ts-card" style="padding:16px;gap:14px">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap">
          <div style="font-family:var(--font-heading);font-size:24px;color:{color}">{_e(label)}</div>
          {range_toggle}
        </div>
        <div class="ts-bar-7" style="gap:3px">{bar_7 or no_history}</div>
        <div class="ts-bar-28" style="border-radius:5px;overflow:hidden">{bar_28 or no_history}</div>
      </div>
    </div>"""


def _step_goal(data: dict) -> int:
    for g in (data.get("active_goals") or []):
        name = f"{g.get('goal_category') or ''} {g.get('goal_type_name') or ''}".lower()
        if "step" in name and g.get("target_value"):
            return int(g["target_value"])
    return DEFAULT_STEP_GOAL


def _acwr_gauge_pct(acwr):
    if acwr is None:
        return None
    lo, hi = 0.5, 1.8
    return round(max(0.0, min(1.0, (acwr - lo) / (hi - lo))) * 100, 1)


def _load_ratio_card(data: dict) -> str:
    ts = data.get("training_status") or {}
    acwr = ts.get("acwr")
    if acwr is None:
        return ""
    acwr_pct = _acwr_gauge_pct(acwr)
    acwr_color = "#4fae72" if acwr_pct is not None and 26 <= acwr_pct <= 66 else ("#d9a441" if acwr_pct is not None else "var(--color-neutral-500)")
    return f"""
    <div class="card" style="padding:16px;gap:12px">
      <div style="display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div><div class="kicker">Load ratio</div>
          <div style="display:flex;align-items:baseline;gap:8px;margin-top:3px">
            <div style="font-family:var(--font-heading);font-size:30px;line-height:1;color:{acwr_color}">{acwr:.2f}</div>
            <div style="font-size:12px;color:{acwr_color}">{_label(ts.get("acwr_status"))}</div>
          </div>
        </div>
      </div>
      <div style="position:relative;height:30px">
        <div style="position:absolute;inset:12px 0 auto 0;height:7px;border-radius:999px;display:flex;overflow:hidden">
          <div style="width:26%;background:#5b8fd8"></div><div style="width:14%;background:#7fc9b0"></div>
          <div style="width:26%;background:#4fae72"></div><div style="width:14%;background:#d9a441"></div>
          <div style="width:20%;background:#cf5a4e"></div>
        </div>
        <div style="position:absolute;left:{acwr_pct:.1f}%;top:4px;width:3px;height:23px;border-radius:2px;
            background:var(--color-neutral-100);box-shadow:0 0 0 2px var(--color-surface)"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
        <span>Detraining</span><span>0.8</span><span>Optimal</span><span>1.3</span><span>High risk</span>
      </div>
    </div>"""


# ── CSS ──────────────────────────────────────────────────────────────────────

_STYLE = """
:root {
  color-scheme: dark;
  --color-bg: #161826; --color-surface: #232532; --color-text: #e9e9ed;
  --color-accent: #9184d9; --color-accent-2: #a7a1db;
  --color-divider: color-mix(in srgb, #e9e9ed 16%, transparent);
  --color-neutral-100: #f3f5fe; --color-neutral-200: #e4e7f5; --color-neutral-300: #cfd3e5;
  --color-neutral-400: #b2b6ca; --color-neutral-500: #9397ab; --color-neutral-600: #75798c;
  --color-neutral-700: #595d6c; --color-neutral-800: #3f424d; --color-neutral-900: #292b31;
  --color-accent-100: #f5f4ff; --color-accent-200: #e7e5fe; --color-accent-300: #d2cefd;
  --color-accent-400: #b5abfc; --color-accent-500: #968ae0; --color-accent-600: #796cbf;
  --color-accent-700: #5d5294; --color-accent-800: #423a6a; --color-accent-900: #2b2741;
  --font-heading: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-body: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --radius-md: 8px;
  --shadow-sm: 0 0 0 1px #3f424d;
  --shadow-md: 0 0 0 1px #595d6c, 0 6px 18px rgba(0,0,0,0.55);
}
* { box-sizing: border-box; }
body { margin:0; background:var(--color-bg); color:var(--color-text); font-family:var(--font-body);
       font-size:15px; line-height:1.5; }
h1,h2,h3,h4 { font-family:var(--font-heading); font-weight:500; margin:0; }
.card { display:flex; flex-direction:column; gap:8px; padding:12px; border-radius:var(--radius-md);
        background:var(--color-surface); }
.muted { color:var(--color-neutral-500); }
.kicker { font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--color-neutral-500); }
.section-title { font-size:11px; letter-spacing:.12em; text-transform:uppercase;
                  color:var(--color-neutral-500); margin-bottom:8px; }
.err { color:#cf8a80; font-size:12px; padding:12px; }

/* ── icon-only radio → pill toggle (tabs / range / PR filter) ── */
input.hide { position:absolute; opacity:0; width:0; height:0; pointer-events:none; }
.pillbar { display:inline-flex; gap:4px; padding:3px; border-radius:999px;
           border:1px solid var(--color-divider); }
.pillbar label { border:0; cursor:pointer; font:inherit; font-size:11px; padding:5px 13px;
                 border-radius:999px; color:var(--color-neutral-500); display:inline-flex;
                 align-items:center; gap:5px; }
.tabpanel, .range-set, .pr-group, .activity-filter-section { display:none; }
#tab-today:checked ~ .tabpanels .tp-today,
#tab-trends:checked ~ .tabpanels .tp-trends,
#tab-activity:checked ~ .tabpanels .tp-activity,
#tab-you:checked ~ .tabpanels .tp-you,
#tab-gear:checked ~ .tabpanels .tp-gear { display:flex; }
#range-7:checked ~ .range-body .rs-7,
#range-14:checked ~ .range-body .rs-14,
#range-30:checked ~ .range-body .rs-30,
#range-42:checked ~ .range-body .rs-42,
#range-90:checked ~ .range-body .rs-90 { display:grid; }
#activity-filter-all:checked ~ .tabpanels .activity-filter-all,
#activity-filter-triathlon:checked ~ .tabpanels .activity-filter-triathlon,
#activity-filter-bike:checked ~ .tabpanels .activity-filter-bike,
#activity-filter-run:checked ~ .tabpanels .activity-filter-run,
#activity-filter-strength:checked ~ .tabpanels .activity-filter-strength,
#activity-filter-other:checked ~ .tabpanels .activity-filter-other { display:flex; }
#prf-all:checked ~ .pr-body .pr-group,
#prf-run:checked ~ .pr-body .pr-group.pr-running,
#prf-bike:checked ~ .pr-body .pr-group.pr-cycling,
#prf-swim:checked ~ .pr-body .pr-group.pr-swimming { display:block; }
#range-7:checked ~ .rangebar label[for=range-7],
#range-14:checked ~ .rangebar label[for=range-14],
#range-30:checked ~ .rangebar label[for=range-30],
#range-42:checked ~ .rangebar label[for=range-42],
#range-90:checked ~ .rangebar label[for=range-90],
#activity-filter-all:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-all],
#activity-filter-triathlon:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-triathlon],
#activity-filter-bike:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-bike],
#activity-filter-run:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-run],
#activity-filter-strength:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-strength],
#activity-filter-other:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-other],
#prf-all:checked ~ .prbar label[for=prf-all],
#prf-run:checked ~ .prbar label[for=prf-run],
#prf-bike:checked ~ .prbar label[for=prf-bike],
#prf-swim:checked ~ .prbar label[for=prf-swim] {
  background: color-mix(in srgb, var(--color-accent) 20%, transparent);
  color: var(--color-accent-200);
}
.botnav label { flex:1; border:0; cursor:pointer; font:inherit; color:var(--color-neutral-500);
                border-radius:999px; padding:8px 0; display:flex; flex-direction:column;
                align-items:center; gap:2px; }
.botnav label i { font-size:19px; }
.botnav label svg { width:19px; height:19px; }
.botnav label[for=more-menu] svg { width:23px; height:23px; margin-bottom:-4px; }
.botnav label span { font-size:9px; letter-spacing:.06em; text-transform:uppercase; }
#tab-today:checked ~ .botnav label[for=tab-today],
#tab-trends:checked ~ .botnav label[for=tab-trends],
#tab-activity:checked ~ .botnav label[for=tab-activity],
#more-menu:checked ~ .botnav label[for=more-menu] {
  background: color-mix(in srgb, var(--color-accent) 20%, transparent);
  color: var(--color-accent-200);
}

/* ── bottom-nav overflow ("More") popup — Gear / Fitness / Weekly Summary /
   Training Plan, stacked. A checkbox (not a tab radio) so it layers over
   whichever tab is open rather than replacing it. Floats above the navbar
   pill rather than covering it, matching that pill's own surface/blur/shadow. ── */
.more-menu-backdrop, .more-menu-sheet { display:none; }
#more-menu:checked ~ .more-menu-backdrop { display:block; position:fixed; inset:0; z-index:39;
  background:rgba(10,11,16,.6); }
#more-menu:checked ~ .more-menu-sheet { display:flex; }
.more-menu-sheet { position:fixed; left:16px; right:16px; bottom:calc(84px + env(safe-area-inset-bottom, 0px));
  z-index:40; flex-direction:column; max-width:420px; margin:0 auto;
  padding:6px; background:color-mix(in srgb, var(--color-surface) 92%, transparent);
  backdrop-filter:blur(16px); border-radius:20px; box-shadow:var(--shadow-md); }
.more-menu-item { display:flex; align-items:center; gap:12px; padding:12px 10px; border-radius:12px;
  color:var(--color-text); text-decoration:none; font:inherit; font-size:14px; cursor:pointer;
  border:0; background:transparent; width:100%; text-align:left; }
.more-menu-item + .more-menu-item { border-top:1px solid var(--color-divider); }
.more-menu-item.more-menu-group-start { margin-top:6px; }
.more-menu-item:hover { background:color-mix(in srgb, var(--color-accent) 10%, transparent); }
.more-menu-item i, .more-menu-item svg { flex:0 0 auto; font-size:19px; width:19px; height:19px;
  color:var(--color-accent-300); }

/* ── FTP unit toggle (Fitness tab's Thresholds cards, issue 96) ── */
.ftp-toggle { display:flex; gap:2px; padding:2px; border-radius:999px; border:1px solid var(--color-divider); }
.ftp-toggle label { border:0; cursor:pointer; font:inherit; font-size:9px; padding:3px 7px;
                     border-radius:999px; color:var(--color-neutral-500); }
.ftp-val-w, .ftp-val-wkg { display:none; }
#ftp-w:checked ~ .ftp-card .ftp-val-w,
#ftp-wkg:checked ~ .ftp-card .ftp-val-wkg { display:inline; }
#ftp-w:checked ~ .ftp-card .ftp-toggle label[for=ftp-w],
#ftp-wkg:checked ~ .ftp-card .ftp-toggle label[for=ftp-wkg] {
  background: color-mix(in srgb, var(--color-accent) 20%, transparent);
  color: var(--color-accent-200);
}

/* ── training-status history bar's 7d/28d toggle (Today tab, issue 92) ── */
.ts-toggle { display:flex; gap:2px; padding:2px; border-radius:999px; border:1px solid var(--color-divider); }
.ts-toggle label { border:0; cursor:pointer; font:inherit; font-size:9px; padding:3px 7px;
                    border-radius:999px; color:var(--color-neutral-500); }
.ts-bar-7, .ts-bar-28 { display:none; }
#ts-range-7:checked ~ .ts-card .ts-bar-7,
#ts-range-28:checked ~ .ts-card .ts-bar-28 { display:flex; }
#ts-range-7:checked ~ .ts-card .ts-toggle label[for=ts-range-7],
#ts-range-28:checked ~ .ts-card .ts-toggle label[for=ts-range-28] {
  background: color-mix(in srgb, var(--color-accent) 20%, transparent);
  color: var(--color-accent-200);
}

/* ── gear tracker forms (issue 53's tab) ── */
.gear-form { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-end; margin-top:10px;
             padding:10px; background:var(--color-neutral-900); border-radius:var(--radius-md); }
.gear-form label { font-size:10px; color:var(--color-neutral-500); display:flex;
                    flex-direction:column; gap:3px; }
.gear-form input, .gear-form select {
  font:inherit; font-size:12px; color:var(--color-text); background:var(--color-bg);
  border:1px solid var(--color-divider); border-radius:6px; padding:.35rem .5rem;
}
.gear-form button { font:inherit; font-size:12px; cursor:pointer; border-radius:6px;
                     border:1px solid var(--color-accent-700); background:transparent;
                     color:var(--color-accent-200); padding:.4rem .8rem; align-self:flex-end; }
.gear-form button:hover { background:color-mix(in srgb, var(--color-accent) 18%, transparent); }
details.gear-actions summary { cursor:pointer; list-style:none; color:var(--color-accent-300);
                                font-size:11px; display:inline-block; margin-right:14px; }
details.gear-actions summary::-webkit-details-marker { display:none; }
.gear-table { width:100%; border-collapse:collapse; }
.gear-table th, .gear-table td { text-align:left; padding:8px 10px; font-size:12px;
                                  border-bottom:1px solid var(--color-divider); }
.gear-table th { color:var(--color-neutral-500); font-weight:500; font-size:10px;
                  text-transform:uppercase; letter-spacing:.05em; }
.gear-table tr:last-child td { border-bottom:none; }
.gear-log-list { max-height:320px; overflow-y:auto; display:flex; flex-direction:column; gap:8px; }

/* ── gear tab (Nocturne mockup match, issue 63) ── */
.gt-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; }
.gt-bike-card { text-decoration:none; color:inherit; }
.gt-bike-card:hover { box-shadow:0 0 0 1px var(--color-accent-700); }
.gt-wearbar { height:5px; border-radius:999px; background:var(--color-neutral-800); overflow:hidden; }
.gt-wearbar > div { height:100%; border-radius:999px; }
.gt-badge { display:inline-flex; align-items:center; gap:5px; font-size:11px; font-weight:500;
            padding:3px 9px; border-radius:999px; border:1px solid; white-space:nowrap; }
details.gt-bike { background:var(--color-surface); border-radius:var(--radius-md);
                   margin-bottom:12px; scroll-margin-top:60px; }
details.gt-bike > summary { display:flex; align-items:center; justify-content:space-between;
                             padding:14px; cursor:pointer; list-style:none; }
details.gt-bike > summary::-webkit-details-marker { display:none; }
details.gt-bike > summary::after { content:'\\25BE'; color:var(--color-neutral-500);
                                    font-size:13px; transition:transform .15s; }
details.gt-bike[open] > summary::after { transform:rotate(180deg); }
.gt-comp-grid { display:grid; grid-template-columns:minmax(150px,1fr) 100px 100px 80px 140px; gap:8px; align-items:center; }
.gt-comp-row { padding:8px 10px; border-radius:6px; text-decoration:none; color:inherit; cursor:pointer; }
.gt-comp-row:hover { filter:brightness(1.1); }
.gt-hist-extra { display:none; }
.gt-history label[for=gt-hist-lim] { display:none; }
.gt-hist-toggle { font-size:12px; color:var(--color-accent-300); cursor:pointer; }
#gt-hist-all:checked ~ .gt-history .gt-hist-extra { display:table-row; }
#gt-hist-all:checked ~ .gt-history label[for=gt-hist-all] { display:none; }
#gt-hist-all:checked ~ .gt-history label[for=gt-hist-lim] { display:inline; }
.gear-modal { display:none; position:fixed; inset:0; z-index:1000; align-items:center;
              justify-content:center; padding:16px; }
.gear-modal:target { display:flex; }
.gear-modal-backdrop { position:absolute; inset:0; background:rgba(10,11,16,.65); }
.gear-modal-dialog { position:relative; z-index:1; width:100%; max-width:420px; max-height:85vh;
                      overflow-y:auto; background:var(--color-surface); border-radius:var(--radius-md);
                      padding:18px; display:flex; flex-direction:column; gap:12px; box-shadow:var(--shadow-md); }
.gear-modal-dialog-small { max-width:360px; }
.gt-service-list { display:flex; flex-direction:column; gap:5px; }
.gt-service-head, .gt-service-row { display:grid; grid-template-columns:minmax(82px,1fr) 72px 72px 28px 28px; gap:6px; align-items:center; }
.gt-service-head { padding:0 9px 3px; font-size:10px; text-transform:uppercase; letter-spacing:.05em; color:var(--color-neutral-500); }
.gt-service-row { padding:8px 9px; border-radius:6px; font-size:12px; }
.gt-service-log { color:var(--color-accent-200); text-decoration:none; border:1px solid var(--color-accent-700); border-radius:6px; width:24px; height:24px; display:grid; place-items:center; text-align:center; }
.gt-service-log svg { width:14px; height:14px; stroke:currentColor; fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
.gt-service-log:hover { background:color-mix(in srgb, var(--color-accent) 18%, transparent); }

/* ── activity row (opens the activity-detail modal, issue 74) ── */
.actcard { padding:12px; border-radius:var(--radius-md); background:var(--color-surface);
           box-shadow:var(--shadow-sm); }

/* ── interactive charts (crosshair line/area + tappable bars) ── */
.js-bar { cursor:pointer; }
.chart-tooltip { display:none; position:fixed; z-index:100; pointer-events:none;
  background:var(--color-neutral-900); border:1px solid var(--color-divider); border-radius:6px;
  padding:6px 10px; box-shadow:var(--shadow-md); white-space:nowrap; }
.chart-tooltip .tt-date { font-size:10px; color:var(--color-neutral-500); }
.chart-tooltip .tt-val { font-family:var(--font-heading); font-size:13px; margin-top:1px; }

/* ── activity rows (open the activity-detail modal, issue 74) ── */
.actcard-click { cursor:pointer; }
.actcard-click:hover { box-shadow:0 0 0 1px var(--color-accent-700); }

/* ── activity-detail modal (issue 74) ── */
.activity-modal { display:none; position:fixed; inset:0; z-index:1000; }
.activity-modal.open { display:block; }
.activity-modal-backdrop { position:absolute; inset:0; background:rgba(10,11,16,.65); }
.activity-modal-sheet { position:absolute; left:0; right:0; bottom:0; margin:0 auto; width:100%; max-width:520px;
  max-height:92vh; background:var(--color-surface); border-radius:16px 16px 0 0; box-shadow:var(--shadow-md);
  display:flex; flex-direction:column; transform:translateY(100%); transition:transform .25s ease; }
.activity-modal.open .activity-modal-sheet { transform:translateY(0); }
.activity-modal-draghandle { flex:0 0 auto; padding:10px 0 6px; touch-action:none; cursor:grab; }
.activity-modal-handle { width:36px; height:4px; border-radius:999px; background:var(--color-neutral-700);
  margin:0 auto; }
.activity-modal-close { position:absolute; top:8px; right:10px; width:28px; height:28px; border-radius:50%;
  border:none; background:var(--color-neutral-900); color:var(--color-neutral-400); font-size:15px;
  line-height:1; cursor:pointer; z-index:1; }
.activity-modal-body { overflow-y:auto; padding:8px 16px 28px; -webkit-overflow-scrolling:touch; }
.ad-stat-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }
.ad-zone-row { display:grid; grid-template-columns:48px 64px 1fr 44px; align-items:center; gap:8px;
               font-size:11px; padding:4px 0; }
.ad-zone-row-head { font-size:9px; letter-spacing:.05em; text-transform:uppercase; }
.ad-zone-bar { height:6px; border-radius:999px; background:var(--color-neutral-800); overflow:hidden; }
.ad-zone-bar > div { height:100%; }
.ad-te-track { position:relative; height:6px; border-radius:999px; margin:8px 0 2px; }
.ad-splits-table { width:100%; border-collapse:collapse; font-size:12px; white-space:nowrap; }
.ad-splits-table th, .ad-splits-table td { text-align:right; padding:6px 10px; border-bottom:1px solid var(--color-divider); }
.ad-splits-table th:first-child, .ad-splits-table td:first-child { text-align:left; }
.ad-splits-table th { color:var(--color-neutral-500); font-weight:500; font-size:10px; text-transform:uppercase; letter-spacing:.05em; }
.ad-splits-table tr:last-child td { border-bottom:none; }
.ad-gear-row { display:grid; grid-template-columns:1fr 76px 100px; align-items:center; gap:8px;
               font-size:12px; padding:7px 0; border-bottom:1px solid var(--color-divider); }
.ad-gear-row-head { font-size:9px; letter-spacing:.05em; text-transform:uppercase; border-bottom:none; padding-bottom:2px; }
.ad-gear-row:last-of-type { border-bottom:none; }

/* ── HR/power chart card (issue 74 feedback) ── */
.ad-chart-zone { font-family:var(--font-heading); font-size:14px; flex:0 0 auto; }
.ad-bounds-bar { position:relative; display:flex; height:8px; border-radius:999px; overflow:visible; margin-top:6px; }
.ad-bounds-bar > div:not(.ad-chart-marker) { height:100%; }
.ad-bounds-bar > div:first-child { border-radius:999px 0 0 999px; }
.ad-bounds-bar > div:not(.ad-chart-marker):last-child { border-radius:0 999px 999px 0; }
.ad-chart-marker { position:absolute; top:50%; width:12px; height:12px; border-radius:50%;
  transform:translate(-50%,-50%); border:2px solid var(--color-bg); box-shadow:0 0 0 1px var(--color-neutral-700);
  transition:left .08s linear; }

/* ── route map (issue 74 feedback) ── */
#activity-map { height:220px; background:var(--color-neutral-900); }
.ad-map-weather { position:absolute; top:10px; right:10px; z-index:400; display:flex; flex-direction:column; gap:4px;
  background:rgba(10,11,16,.6); backdrop-filter:blur(6px); border-radius:8px; padding:6px 9px;
  font-size:11px; color:var(--color-neutral-200); }
@media (max-width:600px) {
  #activity-map .leaflet-control-zoom, #activity-map .leaflet-control-attribution { display:none; }
}
""" + _ICON_CSS


# ── PANEL: TODAY ─────────────────────────────────────────────────────────────

_STRESS_ZONES = [
    ("Rest", "rest_stress_mins", "#4fae72"),
    ("Low", "low_stress_mins", "#7fc9b0"),
    ("Medium", "medium_stress_mins", "#d9a441"),
    ("High", "high_stress_mins", "#cf5a4e"),
]


def _stress_breakdown_card(health: dict) -> str:
    """Today's stress-zone minutes as a tappable bar chart, or '' if unavailable."""
    stress = (health or {}).get("stress") or {}
    zones = [(label, stress.get(key), color) for label, key, color in _STRESS_ZONES]
    if not any(v is not None for _, v, _ in zones):
        return ""
    vmax = max((v or 0) for _, v, _ in zones) or 1
    bars = "".join(
        f'<div class="js-bar" data-date="{html.escape(label)} stress" '
        f'data-value="{_fmt_dur(v) if v is not None else "No data"}" '
        'style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;'
        'align-items:center;gap:6px;height:100%">'
        f'<div style="width:100%;border-radius:5px 5px 2px 2px;height:{max(round((v or 0) / vmax * 100), 3)}px;'
        f'background:{color};min-height:3px"></div>'
        f'<div style="font-size:10px;color:var(--color-neutral-600)">{html.escape(label)}</div></div>'
        for label, v, color in zones
    )
    return f"""
    <div>
      <div class="section-title">Today's stress breakdown</div>
      <div class="card" style="padding:14px">
        <div style="display:flex;align-items:flex-end;gap:6px;height:92px">{bars}</div>
      </div>
    </div>"""


def _factor_bar(label, pct):
    pct = 0 if pct is None else pct
    color = "#4fae72" if pct >= 80 else ("#d9a441" if pct >= 60 else "#cf5a4e")
    return (
        '<div style="display:grid;grid-template-columns:78px 1fr 26px;align-items:center;gap:9px">'
        f'<div style="font-size:11px;color:var(--color-neutral-400)">{html.escape(label)}</div>'
        '<div style="height:5px;border-radius:999px;background:var(--color-neutral-800);overflow:hidden">'
        f'<div style="height:100%;border-radius:999px;width:{pct}%;background:{color}"></div></div>'
        f'<div style="font-size:11px;text-align:right;color:var(--color-neutral-500)">{_num(round(pct) if pct else None, "%")}</div>'
        "</div>"
    )


def _panel_today(data: dict) -> str:
    training = (data.get("training") or {}).get("readiness") or {}
    readiness = data.get("readiness") or {}
    health = data.get("health") or {}
    sleep = data.get("sleep") or {}
    week = data.get("week") or {}
    activities = data.get("activities") or []
    today = data.get("date")

    if not training and not readiness and data.get("training_err") and data.get("readiness_err"):
        hero = f'<div class="card err">Training readiness unavailable — {_e(data.get("training_err"))}</div>'
    else:
        score = training.get("score")
        level = training.get("level")
        color = _readiness_color(level)
        pct = (score / 100 * 326.7) if score is not None else 0
        factors = [
            ("Sleep", training.get("sleep_score_factor_percent")),
            ("Recovery", training.get("recovery_time_factor_percent")),
            ("Load balance", training.get("acwr_factor_percent")),
            ("HRV", training.get("hrv_factor_percent")),
            ("Stress history", training.get("stress_history_factor_percent")),
        ]
        factor_rows = "".join(_factor_bar(l, v) for l, v in factors if v is not None)
        hero = f"""
        <div class="card" style="padding:16px;box-shadow:var(--shadow-sm);
            background:linear-gradient(160deg, color-mix(in srgb, var(--color-accent) 10%, var(--color-surface)), var(--color-surface) 62%);
            display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px;align-items:center">
          <div style="display:flex;align-items:center;gap:16px">
            <div style="position:relative;flex:0 0 auto">
              <svg width="124" height="124" viewBox="0 0 124 124" style="display:block;transform:rotate(-90deg)">
                <circle cx="62" cy="62" r="52" fill="none" stroke="var(--color-neutral-800)" stroke-width="9"></circle>
                <circle cx="62" cy="62" r="52" fill="none" stroke="{color}" stroke-width="9" stroke-linecap="round" stroke-dasharray="{pct:.1f} 326.7"></circle>
              </svg>
              <div style="position:absolute;inset:0;display:grid;place-items:center;text-align:center">
                <div><div style="font-family:var(--font-heading);font-size:38px;line-height:1">{_num(score)}</div>
                <div style="font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--color-neutral-500)">ready</div></div>
              </div>
            </div>
            <div style="min-width:0">
              <div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--color-accent-300)">Training readiness</div>
              <div style="font-family:var(--font-heading);font-size:22px;margin:3px 0 5px;color:{color}">{_label(level)}</div>
              <div style="font-size:12px;color:var(--color-neutral-400);line-height:1.4">{_label(training.get("feedback_short")) or "&mdash;"}</div>
            </div>
          </div>
          <div style="display:flex;flex-direction:column;gap:9px">{factor_rows or '<div class="muted" style="font-size:12px">No factor data.</div>'}</div>
        </div>"""

    training_status_card = _training_status_card(data)
    load_ratio_card = _load_ratio_card(data)

    bb = readiness.get("body_battery") or {}
    daily_stats = readiness.get("daily_stats") or {}
    hrv = readiness.get("hrv") or {}
    hr = health.get("heart_rate") or {}

    bb_pct = 0
    if bb.get("current_level") is not None and bb.get("highest"):
        bb_pct = max(2, round(bb["current_level"] / max(bb["highest"], 1) * 100))
    step_goal = _step_goal(data)
    steps = daily_stats.get("total_steps")
    step_frac = min(1, (steps or 0) / step_goal) if step_goal else 0
    step_dash = round(step_frac * 169.6, 1)
    step_color = "#4fae72" if steps and steps >= step_goal else "var(--color-accent)"
    step_over = f'{"+" if steps and steps >= step_goal else ""}{round((steps / step_goal - 1) * 100)}% of goal' if steps and step_goal else "&mdash;"
    active_min = round(daily_stats["active_seconds"] / 60) if daily_stats.get("active_seconds") else None

    rhr_series = ((data.get("trends") or {}).get("metrics") or {}).get("rhr") or {}
    rhr_spark = _spark([p.get("value") for p in (rhr_series.get("daily") or [])[-14:]], 300, 60, 7)
    rhr_svg = ""
    if rhr_spark:
        rhr_svg = (f'<svg viewBox="0 0 300 60" preserveAspectRatio="none" style="width:100%;height:44px;display:block">'
                   f'<path d="{rhr_spark["area"]}" fill="url(#gRhr)"></path>'
                   f'<path d="{rhr_spark["line"]}" fill="none" stroke="#cf5a4e" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"></path></svg>')

    hrv_val = hrv.get("weekly_avg")
    hrv_lo, hrv_hi = hrv.get("baseline_low"), hrv.get("baseline_high")
    hrv_marker = 50.0
    if hrv_val is not None and hrv_lo is not None and hrv_hi is not None and hrv_hi > hrv_lo:
        hrv_marker = max(4, min(96, (hrv_val - hrv_lo) / (hrv_hi - hrv_lo) * 100))

    quick_cards = f"""
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px">
      <div class="card" style="padding:14px;gap:10px">
        <div class="kicker">Body battery</div>
        <div style="display:flex;align-items:flex-end;gap:12px">
          <div style="width:26px;height:64px;border-radius:8px;background:var(--color-neutral-800);
              display:flex;flex-direction:column;justify-content:flex-end;overflow:hidden">
            <div style="height:{bb_pct}%;background:linear-gradient(#5b8fd8,#3b5bb5)"></div></div>
          <div><div style="font-family:var(--font-heading);font-size:30px;line-height:1">{_num(bb.get("current_level"))}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">peak {_num(bb.get("highest"))}</div></div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:4px 10px;font-size:10px;color:var(--color-neutral-500)">
          <span style="white-space:nowrap"><i class="ph">&#xe08e;</i> {_num(bb.get("charged"))} charged</span>
          <span style="white-space:nowrap"><i class="ph">&#xe03e;</i> {_num(bb.get("drained"))} drained</span>
        </div>
      </div>
      <div class="card" style="padding:14px;gap:10px">
        <div class="kicker">Steps</div>
        <div style="display:flex;align-items:center;gap:12px">
          <div style="position:relative;flex:0 0 auto">
            <svg width="64" height="64" viewBox="0 0 64 64" style="display:block;transform:rotate(-90deg)">
              <circle cx="32" cy="32" r="27" fill="none" stroke="var(--color-neutral-800)" stroke-width="6"></circle>
              <circle cx="32" cy="32" r="27" fill="none" stroke="{step_color}" stroke-width="6" stroke-linecap="round" stroke-dasharray="{step_dash} 169.6"></circle>
            </svg>
            <div style="position:absolute;inset:0;display:grid;place-items:center;color:{step_color};font-size:18px"><i class="ph">&#xea88;</i></div>
          </div>
          <div><div style="font-family:var(--font-heading);font-size:24px;line-height:1">{_num(steps)}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">goal {step_goal:,}</div></div>
        </div>
        <div style="font-size:10px;color:{step_color}">{step_over}{f" · {active_min} active minutes" if active_min is not None else ""}</div>
      </div>
      <div class="card" style="padding:14px;gap:8px">
        <div class="kicker">Resting HR</div>
        <div style="display:flex;align-items:baseline;gap:6px">
          <div style="font-family:var(--font-heading);font-size:30px;line-height:1">{_num(hr.get("resting_hr") or daily_stats.get("resting_hr"))}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">bpm · 7d {_num(hr.get("seven_day_avg_resting_hr") or daily_stats.get("resting_hr_7day_avg"))}</div>
        </div>
        {rhr_svg}
      </div>
      <div class="card" style="padding:14px;gap:8px">
        <div class="kicker">HRV status</div>
        <div style="display:flex;align-items:baseline;gap:6px">
          <div style="font-family:var(--font-heading);font-size:30px;line-height:1">{_num(hrv_val)}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">ms</div>
        </div>
        <div style="position:relative;height:26px;margin-top:2px">
          <div style="position:absolute;left:0;right:0;top:11px;height:4px;border-radius:999px;background:var(--color-neutral-800)"></div>
          <div style="position:absolute;left:20%;width:55%;top:11px;height:4px;border-radius:999px;background:color-mix(in srgb, #4fae72 55%, transparent)"></div>
          <div style="position:absolute;left:{hrv_marker:.0f}%;top:5px;width:2px;height:16px;border-radius:2px;background:#7fc9b0"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
          <span>{_num(hrv_lo)}</span><span style="color:#7fc9b0">{_label(hrv.get("status"))}</span><span>{_num(hrv_hi)}</span>
        </div>
      </div>
    </div>"""

    total_sleep_h, total_sleep_m = _fmt_hm_clock(sleep.get("total_sleep_hrs"))
    need_h, need_m = _fmt_hm_clock(sleep.get("sleep_need_hrs"))
    need_txt = f"of {need_h}h{need_m:02d} need" if need_h is not None else ""
    stages = [
        ("Deep", sleep.get("deep_sleep_hrs"), sleep.get("deep_pct"), "#2f4a9e"),
        ("Light", sleep.get("light_sleep_hrs"), sleep.get("light_pct"), "#6f9ce8"),
        ("REM", sleep.get("rem_sleep_hrs"), sleep.get("rem_pct"), "#a07fe0"),
        ("Awake", sleep.get("awake_hrs"), sleep.get("awake_pct"), "#d9a441"),
    ]
    stage_bars, stage_legend = "", ""
    for label, hrs, pct, color in stages:
        if hrs is None:
            continue
        pct = pct if pct is not None else 0
        h, m = _fmt_hm_clock(hrs)
        stage_bars += (f'<div style="width:{max(pct,3)}%;background:{color};position:relative">'
                       f'<div style="position:absolute;inset:auto 0 4px 0;text-align:center;font-size:9px;color:#e9e9ed">{round(pct)}%</div></div>')
        stage_legend += (f'<span><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{color};margin-right:4px"></span>'
                         f'{label} {h}h{m:02d}</span>')
    sleep_stats = [("Avg HR", _num(sleep.get("avg_hr"))), ("HRV", _num(sleep.get("avg_hrv"), " ms")),
                   ("Respiration", _num(sleep.get("avg_respiration"))), ("Awakenings", _num(sleep.get("awake_count")))]
    sleep_stats_html = "".join(
        f'<div><div style="font-size:10px;color:var(--color-neutral-500)">{k}</div>'
        f'<div style="font-family:var(--font-heading);font-size:17px">{v}</div></div>'
        for k, v in sleep_stats
    )
    sleep_card = f"""
    <div>
      <div class="section-title">Last night</div>
      <div class="card" style="padding:16px;gap:14px">
        <div style="display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap">
          <div style="display:flex;align-items:baseline;gap:8px">
            <div style="font-family:var(--font-heading);font-size:34px;line-height:1">{total_sleep_h if total_sleep_h is not None else "&mdash;"}<span style="font-size:17px;color:var(--color-neutral-500)">h</span>{f"{total_sleep_m:02d}" if total_sleep_m is not None else ""}</div>
            <div style="font-size:11px;color:var(--color-neutral-500)">{need_txt}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <div style="font-size:11px;color:var(--color-neutral-400)">Score</div>
            <div style="font-family:var(--font-heading);font-size:20px;color:#6f9ce8">{_num(sleep.get("sleep_score"))}</div>
          </div>
        </div>
        <div style="display:flex;height:34px;gap:2px">{stage_bars or '<div class="muted" style="font-size:12px">No sleep data.</div>'}</div>
        <div style="display:flex;flex-wrap:wrap;gap:12px;font-size:10px;color:var(--color-neutral-500)">{stage_legend}</div>
        <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;
            border-top:1px solid var(--color-divider);padding-top:12px">{sleep_stats_html}</div>
      </div>
    </div>"""

    week_activities = week.get("activities") or []
    load_by_wd = [0.0] * 7
    for a in week_activities:
        try:
            wd = date.fromisoformat(str(a.get("date"))[:10]).weekday()
            load_by_wd[wd] += a.get("training_load") or 0
        except ValueError:
            continue
    max_load = max(load_by_wd) or 1
    today_wd = date.fromisoformat(today).weekday() if today else -1
    wd_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    week_bars = "".join(
        f'<div class="js-bar" data-date="{html.escape(wd)}" data-value="{f"{round(v):,} load" if v else "No activity"}" '
        f'style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:6px;height:100%">'
        f'<div style="width:100%;border-radius:5px 5px 2px 2px;height:{max(round(v/max_load*100),3) if v else 3}px;'
        f'background:{"var(--color-accent)" if i==today_wd else ("var(--color-accent-700)" if v else "var(--color-neutral-800)")};min-height:3px"></div>'
        f'<div style="font-size:10px;color:{"var(--color-accent-200)" if i==today_wd else "var(--color-neutral-600)"}">{wd}</div></div>'
        for i, (wd, v) in enumerate(zip(wd_labels, load_by_wd))
    )
    week_card = f"""
    <div>
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
        <div class="section-title" style="margin-bottom:0">This week's load</div>
        <div style="font-size:11px;color:var(--color-neutral-500)">{_num(week.get("total_training_load"))} · {_num(week.get("total_activities"))} activities</div>
      </div>
      <div class="card" style="padding:14px">
        <div style="display:flex;align-items:flex-end;gap:6px;height:92px">{week_bars or '<div class="muted" style="font-size:12px">No activity this week.</div>'}</div>
      </div>
    </div>""" if week else ""

    today_acts = [a for a in activities if str(a.get("date") or "")[:10] == today][:3]
    act_rows = "".join(_activity_row_compact(a) for a in today_acts)
    today_acts_card = f"""
    <div>
      <div class="section-title">Today &middot; {len(today_acts)} {'activity' if len(today_acts)==1 else 'activities'}</div>
      <div style="display:flex;flex-direction:column;gap:8px">{act_rows or '<div class="muted" style="font-size:13px">No activities logged yet today.</div>'}</div>
    </div>""" if today_acts or activities is not None else ""

    stress_card = _stress_breakdown_card(health)

    return (
        '<section class="panel tabpanel tp-today" style="flex-direction:column;gap:22px">'
        f"{hero}{training_status_card}{load_ratio_card}{quick_cards}{stress_card}{sleep_card}{week_card}{today_acts_card}"
        "</section>"
    )


def _activity_row_compact(a: dict) -> str:
    icon, tint = _sport_style(a.get("type"))
    big, sub = _activity_big_stat(a)
    activity_id = a.get("id")
    click_attrs = (
        f' onclick="openActivityModal({activity_id})" role="button" tabindex="0"'
        f' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){{event.preventDefault();openActivityModal({activity_id})}}"'
        if activity_id is not None else ""
    )
    click_class = " actcard-click" if activity_id is not None else ""
    return f"""
    <div class="card{click_class}"{click_attrs} style="padding:12px;flex-direction:row;align-items:center;gap:12px">
      <div style="width:34px;height:34px;flex:0 0 auto;border-radius:9px;display:grid;place-items:center;
          background:color-mix(in srgb, {tint} 18%, transparent);color:{tint};font-size:18px"><i class="ph">{icon}</i></div>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_e(a.get("name"))}</div>
        <div style="font-size:11px;color:var(--color-neutral-500)">{_e(_short_date(a.get("date")))}</div>
      </div>
      <div style="text-align:right">
        <div style="font-family:var(--font-heading);font-size:15px">{big}</div>
        <div style="font-size:10px;color:var(--color-neutral-500)">{sub}</div>
      </div>
    </div>"""


# ── PANEL: TRENDS ────────────────────────────────────────────────────────────

_CHART_SPECS = [
    ("hrv", "HRV", "ms", False, False, "#7fc9b0", "url(#gHrv)"),
    ("rhr", "Resting HR", "bpm", True, False, "#cf5a4e", "url(#gRhr)"),
    ("sleep_score", "Sleep score", "/100", False, False, "#6f9ce8", "url(#gSleep)"),
    ("training_load", "Acute load", "", False, True, "var(--color-accent)", "url(#gArea)"),
    ("stress", "Stress", "avg", True, False, "#d9a441", "url(#gStress)"),
    ("steps", "Steps", "", False, True, "#4fae72", "url(#gSteps)"),
]


def _chart_card_html(c: dict) -> str:
    return f"""
    <div class="card" style="padding:13px;gap:7px">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
        <div class="kicker">{html.escape(c["label"])}</div>
        <div style="font-size:11px;color:{c["deltaColor"]}">{c["delta"]}</div>
      </div>
      <div style="display:flex;align-items:baseline;gap:5px">
        <div style="font-family:var(--font-heading);font-size:26px;line-height:1">{c["value"]}</div>
        <div style="font-size:11px;color:var(--color-neutral-500)">{html.escape(c["unit"])}</div>
      </div>
      <svg class="js-linechart" id="{c["id"]}" data-points="{c["points_json"]}"
          viewBox="0 0 300 78" preserveAspectRatio="none" style="width:100%;height:66px;display:block">
        <path d="{c["area"]}" fill="{c["fill"]}"></path>
        <line x1="0" x2="300" y1="{c["avgY"]}" y2="{c["avgY"]}" stroke="var(--color-neutral-700)" stroke-width="1" stroke-dasharray="3 5" vector-effect="non-scaling-stroke"></line>
        <path d="{c["line"]}" fill="none" stroke="{c["stroke"]}" stroke-width="1.7" vector-effect="non-scaling-stroke" stroke-linejoin="round"></path>
        <line class="chart-crosshair" x1="0" x2="0" y1="0" y2="78" stroke="var(--color-neutral-400)" stroke-width="1" vector-effect="non-scaling-stroke" style="opacity:0;pointer-events:none"></line>
        <circle class="chart-dot" cx="0" cy="0" r="3" fill="{c["stroke"]}" stroke="var(--color-bg)" stroke-width="1.5" style="opacity:0;pointer-events:none"></circle>
        <rect class="chart-hit" x="0" y="0" width="300" height="78" style="fill:transparent;pointer-events:all;cursor:crosshair"></rect>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
        <span>{c["lo"]}</span><span>today {c["current"]}</span><span>{c["hi"]}</span>
      </div>
    </div>"""


def _panel_trends(data: dict) -> str:
    trends = data.get("trends")
    if not trends:
        err = data.get("trends_err") or "no data"
        return f'<section class="panel tabpanel tp-trends"><div class="err">Trends unavailable — {_e(err)}</div></section>'

    metrics = trends.get("metrics") or {}
    available_days = trends.get("days") or 30
    ranges = [r for r in (7, 14, 30, 42, 90) if r <= available_days] or [available_days]
    default_range = max(ranges)

    _RANGE_LABELS = {30: "1 month", 90: "3 months"}
    range_pills = "".join(
        f'<label for="range-{r}">{_RANGE_LABELS.get(r, f"{r}d")}</label>' for r in ranges
    )
    range_inputs = "".join(
        f'<input class="hide" type="radio" name="range" id="range-{r}"{" checked" if r == default_range else ""}>'
        for r in ranges
    )
    range_sets = ""
    for r in ranges:
        cards = "".join(
            _chart_card_html(chart) for chart in (
                _chart(metrics.get(key), label, unit, lower_better, r, big=big, stroke=stroke, fill=fill,
                       chart_id=f"lc-{key}-{r}")
                for key, label, unit, lower_better, big, stroke, fill in _CHART_SPECS
            ) if chart is not None
        )
        range_sets += (
            f'<div class="range-set rs-{r}" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">'
            f"{cards}</div>"
        )

    step_series = metrics.get("steps") or {}
    step_daily = (step_series.get("daily") or [])[-14:]
    step_goal = _step_goal(data)
    step_vals = [p.get("value") or 0 for p in step_daily]
    smax = max(step_vals) if step_vals else 1
    step_bars = "".join(
        f'<div class="js-bar" data-date="{_e(_short_date(p.get("date")))}" data-value="{_e(p.get("value"))} steps" '
        f'style="flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end">'
        f'<div style="width:100%;border-radius:3px;'
        f'height:{max(4, (p.get("value") or 0) / (smax or 1) * 100):.1f}%;'
        f'background:{"#4fae72" if (p.get("value") or 0) >= step_goal else "var(--color-neutral-700)"}"></div></div>'
        for p in step_daily
    )
    steps_card = ""
    if step_daily:
        steps_card = f"""
        <div class="card" style="padding:14px;gap:12px">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <div class="kicker">Daily steps</div><div style="font-size:11px;color:var(--color-neutral-500)">goal {step_goal:,}</div>
          </div>
          <div style="display:flex;align-items:flex-end;gap:3px;height:110px">{step_bars}</div>
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
            <span>{_e(_short_date(step_daily[0].get("date")))}</span><span>{_e(_short_date(step_daily[-1].get("date")))}</span>
          </div>
        </div>"""

    return f"""
    <section class="panel tabpanel tp-trends" style="flex-direction:column;gap:16px">
      {range_inputs}
      <div class="rangebar" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div style="font-family:var(--font-heading);font-size:20px">Trends</div>
        <div class="pillbar">{range_pills}</div>
      </div>
      <div class="range-body">{range_sets}</div>
      {steps_card}
    </section>"""


# ── PANEL: ACTIVITY ──────────────────────────────────────────────────────────

_ACTIVITY_FILTERS = ("all", "triathlon", "bike", "run", "strength", "other")
_BIKE_TYPES = {"cycling", "road_biking", "virtual_ride", "indoor_cycling"}
_RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "indoor_running"}
_SWIM_TYPES = {"lap_swimming", "open_water_swimming", "swimming"}
_TRIATHLON_TYPES = {"multi_sport", "triathlon", "duathlon"}
_STRENGTH_TYPES = {"strength", "strength_training"}


def _activity_filter_key(activity: dict) -> str:
  activity_type = str(activity.get("type") or "").lower()
  if activity_type in _STRENGTH_TYPES:
    return "strength"
  if activity_type in _TRIATHLON_TYPES:
    return "triathlon"
  if activity_type in _BIKE_TYPES:
    return "bike"
  if activity_type in _RUN_TYPES:
    return "run"
  if activity_type in _SWIM_TYPES:
    return "other"
  return "other"


def _activity_filter_matches(activity: dict, filter_key: str) -> bool:
  activity_type = str(activity.get("type") or "").lower()
  if filter_key == "all":
    return True
  if filter_key == "triathlon":
    return activity_type in (_TRIATHLON_TYPES | _BIKE_TYPES | _RUN_TYPES | _SWIM_TYPES)
  return _activity_filter_key(activity) == filter_key


def _activity_summary_card(activities: list[dict], split: list[tuple], summary: dict | None = None) -> str:
  summary = summary or {}
  total_distance = summary.get("total_distance_km", sum(a.get("distance_km") or 0 for a in activities))
  total_duration = summary.get("total_duration_min", sum(a.get("duration_min") or 0 for a in activities))
  total_load = summary.get("total_training_load", sum(a.get("training_load") or 0 for a in activities))
  count = summary.get("total_activities", len(activities))
  avg_load = total_load / count if count else None
  total_dur = total_duration or 0
  palette = ["#4fae72", "#d9a441", "#e2734a", "#4aa7d8", "#a07fe0", "#9397ab"]
  split_items = []
  for i, (label, duration) in enumerate(sorted(split, key=lambda item: -item[1])):
    pct = duration / total_dur * 100 if total_dur else 0
    color = palette[i % len(palette)]
    split_items.append((label, pct, color))
  split_bars = "".join(
    f'<div title="{html.escape(label)}" style="width:{pct:.1f}%;background:{color}"></div>'
    for label, pct, color in split_items
  )
  split_legend = "".join(
    f'<span style="display:flex;align-items:center;gap:5px"><span style="width:7px;height:7px;border-radius:2px;background:{color}"></span>{html.escape(label)}</span>'
    for label, pct, color in split_items
  )
  return f"""
  <div class="card" style="padding:16px;gap:14px">
    <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
    <div><div class="kicker">Distance</div><div style="font-family:var(--font-heading);font-size:24px">{_trim(total_distance)}<span style="font-size:12px;color:var(--color-neutral-500)"> km</span></div></div>
    <div><div class="kicker">Time</div><div style="font-family:var(--font-heading);font-size:24px">{_fmt_dur(total_duration)}</div></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px">
    <div><div class="kicker">Load</div><div style="font-family:var(--font-heading);font-size:24px">{_num(round(total_load))}</div></div>
    <div><div class="kicker">Sessions</div><div style="font-family:var(--font-heading);font-size:24px">{_num(count)}</div></div>
    <div><div class="kicker">Avg load/session</div><div style="font-family:var(--font-heading);font-size:20px">{_num(round(avg_load, 1) if avg_load is not None else None)}</div></div>
    </div>
    <div style="display:flex;height:10px;gap:2px;border-radius:999px;overflow:hidden">{split_bars}</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px 14px;font-size:10px;color:var(--color-neutral-500)">{split_legend}</div>
  </div>"""


def _activity_week_url(token: str | None, offset: int) -> str:
  """/dashboard link that opens the Activity tab on the given week offset
  (0 = current week, 1 = last week, …), carrying the bearer token along."""
  params = {"tab": "activity", "week": str(max(0, offset))}
  if token:
    params["token"] = token
  return f"/dashboard?{urlencode(params)}"


# A drawn chevron rather than a "‹"/"›" text glyph — those two characters
# sit high in most fonts' em-box (they're metrically designed as quote
# marks, not arrows), so flex-centering the *character* still left them
# visibly off-center inside the circle. An SVG path has no such baseline
# quirk: with `display:block` it centers exactly inside its flex parent.
_CHEVRON_LEFT = ('<svg width="8" height="13" viewBox="0 0 8 13" fill="none" style="display:block">'
                 '<path d="M7 1L1.5 6.5L7 12" stroke="currentColor" stroke-width="2" '
                 'stroke-linecap="round" stroke-linejoin="round"/></svg>')
_CHEVRON_RIGHT = ('<svg width="8" height="13" viewBox="0 0 8 13" fill="none" style="display:block">'
                  '<path d="M1 1L6.5 6.5L1 12" stroke="currentColor" stroke-width="2" '
                  'stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _activity_nav_button(direction: str, token: str | None, offset: int, disabled: bool, size: int = 28) -> str:
  """One prev/next week-navigation control. Rendered as an inert <span> when
  disabled (used for the right/next arrow on the current week — issue 85:
  can't navigate into future weeks that have no data) or a link otherwise."""
  arrow = _CHEVRON_LEFT if direction == "prev" else _CHEVRON_RIGHT
  label = "Previous week" if direction == "prev" else "Next week"
  base_style = (
    f"display:inline-flex;align-items:center;justify-content:center;width:{size}px;height:{size}px;"
    "border-radius:999px;border:1px solid var(--color-divider);flex:0 0 auto"
  )
  if disabled:
    return f'<span aria-hidden="true" style="{base_style};color:var(--color-neutral-700)">{arrow}</span>'
  url = _e(_activity_week_url(token, offset))
  return (
    f'<a href="{url}" aria-label="{label}" style="{base_style};color:var(--color-text);text-decoration:none">'
    f'{arrow}</a>'
  )


def _activity_current_week_link(token: str | None, offset: int) -> str:
  """"This week" quick-jump back to week_offset=0 — only shown once you've
  actually navigated away from the current week (issue 85 follow-up)."""
  if offset <= 0:
    return ""
  url = _e(_activity_week_url(token, 0))
  return (
    f'<a href="{url}" style="font-size:11px;color:var(--color-accent);text-decoration:none;'
    'white-space:nowrap;padding:6px 2px">This week</a>'
  )


def _panel_activity(data: dict, token: str | None = None) -> str:
    week = data.get("activity_week")
    if week is None:
        week = data.get("week")
    offset = data.get("activity_week_offset") or 0
    activities = data.get("activities")
    if not week and not activities:
        err = data.get("activity_week_err") or data.get("week_err") or data.get("activities_err") or "no data"
        return f'<section class="panel tabpanel tp-activity"><div class="err">Activity data unavailable — {_e(err)}</div></section>'

    week = week or {}

    week_start = str(week.get("week_start") or "")[:10]
    week_end = str(week.get("week_end") or "")[:10]
    date_range_label = _format_week_range(week_start, week_end)
    filter_labels = "".join(
      f'<label for="activity-filter-{key}">{key.title()}</label>'
      for key in _ACTIVITY_FILTERS
    )
    # The requested week's own activities, straight from get_weekly_summary
    # (already scoped to week_start..week_end server-side) rather than the
    # separately-fetched "recent 20 activities" list, which only covers the
    # current/most-recent week — using it here left older weeks' cards empty
    # while their summary totals (computed from this same source) were correct.
    summary_source = week.get("activities") or []
    current_week_link = _activity_current_week_link(token, offset)
    prev_btn = _activity_nav_button("prev", token, offset + 1, False)
    next_btn = _activity_nav_button("next", token, offset - 1, offset <= 0)
    sections = []
    for key in _ACTIVITY_FILTERS:
      selected_summary = summary_source if key == "all" else [a for a in summary_source if _activity_filter_matches(a, key)]
      split = []
      if key == "all":
        split = [
          (f"{_sport_label(activity_type)} {_fmt_dur(values.get('duration_min'))}", values.get("duration_min") or 0)
          for activity_type, values in (week.get("by_type") or {}).items()
        ]
      else:
        for activity in selected_summary:
          label = _sport_label(activity.get("type"))
          existing = next((index for index, item in enumerate(split) if item[0] == label), None)
          duration = activity.get("duration_min") or 0
          if existing is None:
            split.append((label, duration))
          else:
            split[existing] = (label, split[existing][1] + duration)
      max_load = max((a.get("training_load") or 0) for a in selected_summary) or 1 if selected_summary else 1
      cards = "".join(_activity_row_expandable(a, max_load) for a in selected_summary)
      empty = '<div class="muted" style="font-size:13px">No activities for this filter this week.</div>'
      view_more = '<button type="button" class="btn btn-secondary" style="align-self:center" disabled>View More</button>' if selected_summary else ""
      bottom_nav = (
        '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:4px">'
        f'{prev_btn}<div style="flex:1;display:flex;justify-content:center">{view_more}</div>{next_btn}</div>'
      )
      sections.append(
        f'<div class="activity-filter-section activity-filter-{key}" style="flex-direction:column;gap:8px">'
        f'{_activity_summary_card(selected_summary, split, week if key == "all" else None)}{cards or empty}{bottom_nav}</div>'
      )

    return f"""
    <section class="panel tabpanel tp-activity" style="flex-direction:column;gap:16px">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div>
          <div style="font-family:var(--font-heading);font-size:20px">Activity</div>
          <div style="font-size:12px;color:var(--color-neutral-500);margin-top:2px">{_e(date_range_label)}</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">{current_week_link}{prev_btn}{next_btn}</div>
      </div>
      <div class="activity-filterbar pillbar">{filter_labels}</div>
      {"".join(sections)}
    </section>"""


def _activity_row_expandable(a: dict, max_load: float) -> str:
    icon, tint = _sport_style(a.get("type"))
    big, sub = _activity_big_stat(a)
    load = a.get("training_load") or 0
    load_w = max(2, round(load / max_load * 100))
    activity_id = a.get("id")
    click_attrs = (
        f' onclick="openActivityModal({activity_id})" role="button" tabindex="0"'
        f' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){{event.preventDefault();openActivityModal({activity_id})}}"'
        if activity_id is not None else ""
    )
    click_class = " actcard-click" if activity_id is not None else ""
    return f"""
    <div class="actcard{click_class}"{click_attrs}>
      <div style="display:flex;align-items:center;gap:12px">
        <div style="width:34px;height:34px;flex:0 0 auto;border-radius:9px;display:grid;place-items:center;
            background:color-mix(in srgb, {tint} 18%, transparent);color:{tint};font-size:18px"><i class="ph">{icon}</i></div>
        <div style="flex:1;min-width:0">
          <div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_e(a.get("name"))}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">{_e(_short_date(a.get("date")))}</div>
        </div>
        <div style="text-align:right;flex:0 0 auto">
          <div style="font-family:var(--font-heading);font-size:15px">{big}</div>
          <div style="font-size:10px;color:var(--color-neutral-500)">{sub}</div>
        </div>
      </div>
      <div style="margin-top:10px;height:3px;border-radius:999px;background:var(--color-neutral-800);overflow:hidden">
        <div style="height:100%;width:{load_w}%;background:{tint};border-radius:999px"></div>
      </div>
    </div>"""


# ── PANEL: FITNESS ───────────────────────────────────────────────────────────
# VO2max gauge bands (ml/kg/min) and their ratings — issue #96's Nocturne
# redesign of the VO2max/Thresholds section: a 5-band circular gauge per
# sport (rather than the old dual dot-on-a-bar) plus individual threshold
# cards, one of which (FTP) offers a W / W-per-kg unit toggle.
_VO2_SEGS = (
    (25, 41.7, "#cf5a4e"), (41.7, 45.4, "#d9a441"), (45.4, 51.1, "#4fae72"),
    (51.1, 55.4, "#4aa7d8"), (55.4, 65, "#a07fe0"),
)
_VO2_MIN, _VO2_MAX = 25, 65
_VO2_START_ANGLE, _VO2_SWEEP = 135, 300


def _vo2_rating(value):
    if value is None:
        return None
    if value >= 55.4:
        return "Superior"
    if value >= 51.1:
        return "Excellent"
    if value >= 45.4:
        return "Good"
    if value >= 41.7:
        return "Fair"
    return "Poor"


def _vo2_gauge_svg(value, size: int = 132) -> str:
    """A 5-band circular VO2max gauge (arc from _VO2_MIN to _VO2_MAX). The
    current value gets a highlighted tail + dot; with no reading, just the
    band track renders."""
    cx = cy = size / 2
    r = size / 2 - 15

    def angle_for(v):
        frac = max(0.0, min(1.0, (v - _VO2_MIN) / (_VO2_MAX - _VO2_MIN)))
        return _VO2_START_ANGLE + frac * _VO2_SWEEP

    def pt(a):
        rad = math.radians(a)
        return cx + r * math.cos(rad), cy + r * math.sin(rad)

    def arc(a0, a1):
        x0, y0 = pt(a0)
        x1, y1 = pt(a1)
        large = 1 if (a1 - a0) > 180 else 0
        return f"M{x0:.1f} {y0:.1f} A{r:.1f} {r:.1f} 0 {large} 1 {x1:.1f} {y1:.1f}"

    segs_svg = "".join(
        f'<path d="{arc(angle_for(a), angle_for(b))}" fill="none" stroke="{c}" '
        f'stroke-width="11" stroke-linecap="round"></path>'
        for a, b, c in _VO2_SEGS
    )

    dot_svg = ""
    if value is not None:
        v = max(_VO2_MIN, min(_VO2_MAX, value))
        v_angle = angle_for(v)
        cur = next((s for s in _VO2_SEGS if s[0] <= v <= s[1]), _VO2_SEGS[-1])
        tail = arc(max(angle_for(cur[0]), v_angle - 14), v_angle)
        dot_x, dot_y = pt(v_angle)
        dot_svg = (
            f'<path d="{tail}" fill="none" stroke="{cur[2]}" stroke-width="11" stroke-linecap="round"></path>'
            f'<circle cx="{dot_x:.1f}" cy="{dot_y:.1f}" r="6.5" fill="{cur[2]}" '
            f'stroke="var(--color-surface)" stroke-width="2"></circle>'
        )

    return f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">{segs_svg}{dot_svg}</svg>'


def _vo2_gauge_block(value, label: str, icon: str) -> str:
    rating = _vo2_rating(value)
    size = 132
    return f"""
    <div style="display:flex;flex-direction:column;align-items:center;gap:8px">
      <div style="font-size:11px;color:var(--color-neutral-500);display:flex;align-items:center;gap:5px">
        <i class="ph">{icon}</i>{label} VO&#8322;
      </div>
      <div style="position:relative;width:{size}px;height:{size}px">
        {_vo2_gauge_svg(value, size)}
        <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center">
          <div style="font-family:var(--font-heading);font-size:30px;line-height:1">{_num(value)}</div>
          <div style="font-size:11px;color:var(--color-neutral-500);margin-top:2px">{rating or "&mdash;"}</div>
        </div>
      </div>
    </div>"""


def _threshold_card(icon_html: str, color: str, value: str, label: str, extra: str = "", classes: str = "") -> str:
    return f"""
    <div class="card {classes}" style="padding:13px;gap:9px">
      <div style="display:flex;align-items:center;justify-content:space-between">
        <div style="width:30px;height:30px;border-radius:9px;display:grid;place-items:center;
            background:color-mix(in srgb, {color} 18%, transparent);color:{color};font-size:15px">{icon_html}</div>
        {extra}
      </div>
      <div style="font-family:var(--font-heading);font-size:19px;line-height:1.1">{value}</div>
      <div style="font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--color-neutral-500)">{label}</div>
    </div>"""


def _panel_fitness(data: dict) -> str:
    ts = data.get("training_status") or {}
    athlete = data.get("athlete") or {}
    records = data.get("personal_records")

    vo2 = ts.get("vo2max") or {}
    run_v2, bike_v2 = vo2.get("running"), vo2.get("cycling")
    vo2_card = f"""
    <div class="card" style="padding:18px;gap:16px">
      <div class="kicker">VO&#8322; max</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:16px;justify-content:center">
        {_vo2_gauge_block(run_v2, "Running", _RUN)}
        {_vo2_gauge_block(bike_v2, "Cycling", _BIKE)}
      </div>
    </div>"""

    lthr_card = _threshold_card(f'<i class="ph">{_HEARTBEAT}</i>', "#cf5a4e",
                                 _num(athlete.get("lactate_threshold_hr"), " bpm"), "LTHR")
    lt_pace_card = _threshold_card(
        f'<i class="ph">{_RUN}</i>', "#d9a441",
        f"{athlete['lactate_threshold_pace']:.2f} /km" if athlete.get("lactate_threshold_pace") else "&mdash;",
        "LT pace",
    )

    ftp = athlete.get("ftp")
    weight_kg = athlete.get("weight_kg")
    lightning = _svg_icon(_LIGHTNING_ICON_PATH, size=15)
    if ftp and weight_kg:
        ftp_radios = (
            '<input class="hide" type="radio" name="ftp-unit" id="ftp-w" checked>'
            '<input class="hide" type="radio" name="ftp-unit" id="ftp-wkg">'
        )
        ftp_toggle = '<div class="ftp-toggle"><label for="ftp-w">W</label><label for="ftp-wkg">W/kg</label></div>'
        ftp_value = (
            f'<span class="ftp-val-w">{_num(ftp, " W")}</span>'
            f'<span class="ftp-val-wkg">{ftp / weight_kg:.2f} W/kg</span>'
        )
        ftp_card = (
            f"{ftp_radios}"
            f"{_threshold_card(lightning, '#4fae72', ftp_value, 'FTP', extra=ftp_toggle, classes='ftp-card')}"
        )
    else:
        ftp_card = _threshold_card(lightning, "#4fae72", _num(ftp, " W"), "FTP")

    thresholds_card = f"""
    <div>
      <div class="section-title">Thresholds</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px">
        {lthr_card}{lt_pace_card}{ftp_card}
      </div>
    </div>"""

    lthr = athlete.get("lactate_threshold_hr")
    zones_card = ""
    if lthr:
        bounds = [0, 0.80, 0.90, 0.95, 1.00, 1.10]
        widths = [30, 48, 66, 84, 100]
        zone_rows = ""
        for i in range(5):
            lo_bpm = round(lthr * bounds[i]) if i > 0 else None
            hi_bpm = round(lthr * bounds[i + 1]) if i < 4 else None
            rng = f"< {round(lthr * bounds[1])}" if i == 0 else (f"{lo_bpm}+" if i == 4 else f"{lo_bpm}–{hi_bpm}")
            color = ["#9397ab", "#5b8fd8", "#4fae72", "#d9a441", "#cf5a4e"][i]
            zone_rows += (
                f'<div style="display:grid;grid-template-columns:56px 1fr 62px;align-items:center;gap:10px">'
                f'<div style="font-size:11px;color:var(--color-neutral-400)">Z{i+1}</div>'
                f'<div style="height:8px;border-radius:999px;background:var(--color-neutral-800);overflow:hidden">'
                f'<div style="height:100%;width:{widths[i]}%;background:{color};border-radius:999px"></div></div>'
                f'<div style="font-size:11px;text-align:right;color:var(--color-neutral-500)">{rng}</div></div>'
            )
        zones_card = f"""
        <div>
          <div class="section-title">Heart-rate zones</div>
          <div class="card" style="padding:14px;gap:8px">{zone_rows}</div>
        </div>"""

    pr_html = ""
    if records:
        sport_meta = {"running": ("Running", _RUN), "cycling": ("Cycling", _BIKE), "swimming": ("Swimming", _SWIM)}
        groups_html = ""
        for cat, (label, icon) in sport_meta.items():
            items = records.get(cat) or []
            if not items:
                continue
            item_cards = "".join(
                f'<div class="card" style="padding:12px;gap:4px">'
                f'<div style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--color-neutral-500)">{_e(p.get("label"))}</div>'
                f'<div style="font-family:var(--font-heading);font-size:21px;line-height:1.1">{_e(p.get("value_formatted"))}</div>'
                f'<div style="font-size:10px;color:var(--color-neutral-600)">{_month_year(p.get("date"))}</div></div>'
                for p in items
            )
            groups_html += f"""
            <div class="pr-group pr-{cat}">
              <div style="display:flex;align-items:center;gap:7px;margin-bottom:7px;color:var(--color-accent-300)">
                <i class="ph" style="font-size:15px">{icon}</i>
                <span style="font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--color-neutral-400)">{label}</span>
                <span style="flex:1;height:1px;background:linear-gradient(90deg,var(--color-divider),transparent)"></span>
              </div>
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">{item_cards}</div>
            </div>"""
        pr_html = f"""
        <div>
          <input class="hide" type="radio" name="prf" id="prf-all" checked><input class="hide" type="radio" name="prf" id="prf-run">
          <input class="hide" type="radio" name="prf" id="prf-bike"><input class="hide" type="radio" name="prf" id="prf-swim">
          <div class="prbar" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px">
            <div class="section-title" style="margin-bottom:0">Personal records</div>
            <div class="pillbar">
              <label for="prf-all"><i class="ph" style="font-size:13px">&#xe67e;</i>All</label>
              <label for="prf-run"><i class="ph" style="font-size:13px">{_RUN}</i>Run</label>
              <label for="prf-bike"><i class="ph" style="font-size:13px">{_BIKE}</i>Bike</label>
              <label for="prf-swim"><i class="ph" style="font-size:13px">{_SWIM}</i>Swim</label>
            </div>
          </div>
          <div class="pr-body" style="display:flex;flex-direction:column;gap:14px">{groups_html}</div>
        </div>"""

    return f"""
    <section class="panel tabpanel tp-you" style="flex-direction:column;gap:16px">
      <div style="font-family:var(--font-heading);font-size:20px">Fitness</div>
      {vo2_card}
      {thresholds_card}
      {zones_card}
      {pr_html}
    </section>"""


# ── PANEL: GEAR ──────────────────────────────────────────────────────────────
# Bike component maintenance tracking (issue 53) — Garmin's own gear distance
# joined with the local gear-tracker database (tools/gear_tracker.py). The
# API routes it posts to (/api/gear/maintenance, /api/gear/components) are
# served by that module's Starlette sub-app, mounted alongside /dashboard.

_GEAR_ACTIONS = ("lubed", "replaced", "serviced", "adjusted", "other")


def _status_color(status: str) -> str:
    from tools.gear_tracker import _STATUS_COLOR
    return _STATUS_COLOR.get(status, _STATUS_COLOR["unknown"])


def _dot_style(color: str) -> str:
    return f"display:inline-block;width:8px;height:8px;border-radius:50%;background:{color};flex:0 0 auto"


def _tint(color: str, pct: int = 14) -> str:
    """A status color mixed into the surface color, for a subtle status-tinted background."""
    return f"color-mix(in srgb, {color} {pct}%, var(--color-surface))"


def _border_tint(color: str, pct: int = 40) -> str:
    return f"color-mix(in srgb, {color} {pct}%, transparent)"


def _gear_icon(name: str) -> str:
    if name == "logbook":
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4h11a2 2 0 0 1 2 2v14H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z"/><path d="M7 4v16"/><path d="M10 8h6"/><path d="M10 12h6"/></svg>'
    if name == "pencil":
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l11-11a2.8 2.8 0 0 0-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/></svg>'
    if name == "plus":
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg>'
    return ""


def _gear_bike_overview_card(g: dict) -> str:
    """A clickable bike card (Bikes overview) that jumps to its entry in the
    Bike Component Tracker section below via a same-page anchor link — no JS
    needed."""
    color = _status_color(g["status_indicator"])
    n = len(g["components"])
    badge = ""
    if g["status_indicator"] == "red":
        badge = (
            f'<span class="gt-badge" style="color:{color};background:{_tint(color)};'
            f'border-color:{_border_tint(color)}"><span style="{_dot_style(color)}"></span>Service due</span>'
        )
    stats = [("Distance", _fmt_km(g.get("distance_km")))]
    if g.get("duration_min"):
        stats.append(("Time", _fmt_dur(g.get("duration_min"))))
    if g.get("total_activities") is not None:
        stats.append(("Rides", str(g["total_activities"])))
    stats_html = "".join(
        f'<div style="font-size:12px;white-space:nowrap"><span style="color:var(--color-text)">{_e(v)}</span> '
        f'<span style="color:var(--color-neutral-500)">{html.escape(k)}</span></div>'
        for k, v in stats
    )
    return f"""
    <a class="card gt-bike-card" href="#bike-{_e(g.get('uuid') or '')}" style="padding:14px;gap:10px">
      <div style="font-size:15px;font-weight:600;font-family:var(--font-heading)">{_e(g.get("name"))}</div>
      <div style="display:flex;gap:14px;flex-wrap:wrap">{stats_html}</div>
      <div style="display:flex;align-items:center;justify-content:space-between;padding-top:10px;border-top:1px solid var(--color-divider)">
        <span style="font-size:12px;color:var(--color-neutral-500)">{n} component{"" if n == 1 else "s"} tracked</span>
        {badge}
      </div>
    </a>"""


def _gear_shoe_card(g: dict) -> str:
    color = _status_color(g["status_indicator"])
    max_km = g.get("max_distance_km") or 750
    pct = min(((g.get("distance_km") or 0) / max_km) * 100, 100) if max_km else 0
    distance_line = _fmt_km(g.get("distance_km"))
    if g.get("max_distance_km"):
        distance_line += f" of {_fmt_km(g['max_distance_km'])}"
    return f"""
    <div class="card" style="padding:14px;gap:10px">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:19px;line-height:1">\U0001F3C3</span>
        <div style="min-width:0">
          <div style="font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_e(g.get("name"))}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">{_e(g.get("model") or "Shoes")}</div>
        </div>
      </div>
      <div style="font-size:12px;color:var(--color-neutral-400)">{distance_line}</div>
      <div class="gt-wearbar"><div style="width:{pct:.0f}%;background:{color}"></div></div>
    </div>"""


def _gear_component_row(c: dict) -> str:
    """One row of the Bike Component Tracker table — a CSS-grid `<a>` (not a
    real `<table>` row: browsers foster-parent stray children out of a real
    `<table>`/`<tbody>`, so an anchor can't stand in for a `<tr>` there) that
    opens the component's detail/log-maintenance modal via a same-page
    `#component-<id>` anchor, shown with `:target` (see the .gear-modal CSS)
    — no JS needed."""
    color = _status_color(c["status"])
    lifespan = c.get("lifespan_km")
    usage = c.get("component_usage_km") or c.get("distance_since_km") or 0
    pct = min((usage / lifespan) * 100, 100) if lifespan else 8
    lifespan_txt = _fmt_km(lifespan) if lifespan else "N/A"
    status_label = "N/A" if c["status"] == "unknown" else c["status"].title()
    return f"""
    <a class="gt-comp-row gt-comp-grid" href="#component-{_e(c['id'])}"
       style="background:{_tint(color)};border-left:3px solid {color}">
      <div>{_e(c["name"])}</div>
      <div style="color:var(--color-neutral-400)">{_e(c["last_serviced"])}</div>
      <div>{_fmt_km(c.get("component_usage_km") or c.get("distance_since_km"))}</div>
      <div style="color:var(--color-neutral-400)">{lifespan_txt}</div>
      <div style="display:flex;align-items:center;gap:8px">
        <div style="width:52px;height:6px;border-radius:99px;background:var(--color-neutral-800);overflow:hidden;flex:none">
          <div style="height:100%;border-radius:99px;background:{color};width:{pct:.0f}%"></div>
        </div>
        <span style="font-size:11px;color:{color}">{_e(status_label)}</span>
      </div>
    </a>"""


def _gear_service_row(component: dict, service: dict) -> str:
    color = _status_color(service["status"])
    km_until = service.get("km_until_next_service")
    if km_until is None:
        until_txt = "N/A"
    elif km_until <= 0:
        until_txt = "Due now"
    else:
        until_txt = _fmt_km(km_until)
    return f"""
      <div class="gt-service-row" style="border-left:3px solid {color};background:{_tint(color)}">
        <div style="font-weight:600">{_e(service.get("service_type"))}</div>
        <div style="color:var(--color-neutral-400)">{_e(service.get("last_serviced"))}</div>
        <div>{until_txt}</div>
        <a href="#service-{_e(component['id'])}-{_e(service['id'])}" class="gt-service-log" title="Log service" aria-label="Log service">{_gear_icon("logbook")}</a>
        <a href="#service-edit-{_e(component['id'])}-{_e(service['id'])}" class="gt-service-log" title="Edit service" aria-label="Edit service">{_gear_icon("pencil")}</a>
      </div>"""


def _gear_service_log_modal(component: dict, service: dict, token: str | None) -> str:
    now_value = datetime.now().strftime("%Y-%m-%dT%H:%M")
    service_type = service.get("service_type") or component.get("name") or "service"
    return f"""
    <div id="service-{_e(component['id'])}-{_e(service['id'])}" class="gear-modal">
      <a href="#component-{_e(component['id'])}" class="gear-modal-backdrop" aria-label="Close"></a>
      <div class="gear-modal-dialog gear-modal-dialog-small">
        <div>
          <div style="font-size:16px;font-weight:600;font-family:var(--font-heading)">Log {_e(service_type)}</div>
          <div style="font-size:12px;color:var(--color-neutral-500);margin-top:2px">{_e(component.get("name"))}</div>
        </div>
        <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/maintenance', token))}">
          <input type="hidden" name="component_id" value="{_e(component['id'])}">
          <input type="hidden" name="service_id" value="{_e(service['id'])}">
          <input type="hidden" name="action" value="{_e(service_type)}">
          <label>Date/time<input type="datetime-local" name="service_datetime" value="{_e(now_value)}"></label>
          <label style="flex:1 1 100%">Notes<input type="text" name="notes" placeholder="optional"></label>
          <button type="submit">Log</button>
        </form>
        <a href="#component-{_e(component['id'])}" style="align-self:flex-end;font-size:12px;color:var(--color-neutral-500);text-decoration:none">Back</a>
      </div>
    </div>"""


def _gear_service_edit_modal(component: dict, service: dict, token: str | None) -> str:
    interval = service.get("service_interval_km")
    interval_value = "" if interval is None else f"{interval:g}"
    return f"""
    <div id="service-edit-{_e(component['id'])}-{_e(service['id'])}" class="gear-modal">
      <a href="#component-{_e(component['id'])}" class="gear-modal-backdrop" aria-label="Close"></a>
      <div class="gear-modal-dialog gear-modal-dialog-small">
        <div>
          <div style="font-size:16px;font-weight:600;font-family:var(--font-heading)">Edit service</div>
          <div style="font-size:12px;color:var(--color-neutral-500);margin-top:2px">{_e(component.get("name"))}</div>
        </div>
        <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/services', token))}">
          <input type="hidden" name="component_id" value="{_e(component['id'])}">
          <input type="hidden" name="service_id" value="{_e(service['id'])}">
          <label>Service<input type="text" name="service_type" value="{_e(service.get('service_type'))}" required></label>
          <label>Interval (km)<input type="number" step="1" min="0" name="service_interval_km" value="{interval_value}" placeholder="optional"></label>
          <label style="flex:1 1 100%">Notes<input type="text" name="notes" value="{_e(service.get('notes') or '')}" placeholder="optional"></label>
          <button type="submit">Save</button>
        </form>
        <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/services', token))}">
          <input type="hidden" name="component_id" value="{_e(component['id'])}">
          <input type="hidden" name="service_id" value="{_e(service['id'])}">
          <input type="hidden" name="delete" value="1">
          <button type="submit">Delete service</button>
        </form>
        <a href="#component-{_e(component['id'])}" style="align-self:flex-end;font-size:12px;color:var(--color-neutral-500);text-decoration:none">Back</a>
      </div>
    </div>"""


def _gear_add_service_modal(component: dict, token: str | None) -> str:
    return f"""
    <div id="service-add-{_e(component['id'])}" class="gear-modal">
      <a href="#component-{_e(component['id'])}" class="gear-modal-backdrop" aria-label="Close"></a>
      <div class="gear-modal-dialog gear-modal-dialog-small">
        <div>
          <div style="font-size:16px;font-weight:600;font-family:var(--font-heading)">Add service</div>
          <div style="font-size:12px;color:var(--color-neutral-500);margin-top:2px">{_e(component.get("name"))}</div>
        </div>
        <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/services', token))}">
          <input type="hidden" name="component_id" value="{_e(component['id'])}">
          <label>Service<input type="text" name="service_type" placeholder="Lube" required></label>
          <label>Interval (km)<input type="number" step="1" min="0" name="service_interval_km" placeholder="optional"></label>
          <label style="flex:1 1 100%">Notes<input type="text" name="notes" placeholder="optional"></label>
          <button type="submit">Add</button>
        </form>
        <a href="#component-{_e(component['id'])}" style="align-self:flex-end;font-size:12px;color:var(--color-neutral-500);text-decoration:none">Back</a>
      </div>
    </div>"""


def _gear_component_modal(c: dict, bike_name: str | None, history: list[dict], token: str | None) -> str:
    """The CSS-only (`:target`) modal a component row opens: status, wear
    bar, key dates, recent maintenance, a log-maintenance form, and an
    "Edit component" disclosure for renaming/adjusting the interval."""
    color = _status_color(c["status"])
    lifespan = c.get("lifespan_km")
    usage = c.get("component_usage_km") or c.get("distance_since_km") or 0
    pct = min((usage / lifespan) * 100, 100) if lifespan else 8
    status_label = "N/A" if c["status"] == "unknown" else c["status"].title()
    lifespan_value = "" if lifespan is None else f"{lifespan:g}"
    last_serviced = c.get("last_serviced") or "Never"

    linked_note = (
        '<div style="font-size:11px;color:var(--color-neutral-600)">Linked to its own '
        'Garmin-tracked gear item &mdash; distance comes from Garmin, not this bike.</div>'
        if c.get("linked_gear_uuid") else ""
    )

    services = c.get("services") or []
    service_rows = "".join(_gear_service_row(c, service) for service in services)
    services_empty = '<div class="muted" style="font-size:12px">No services yet.</div>' if not services else ""
    service_modals = "".join(
        _gear_service_log_modal(c, service, token) + _gear_service_edit_modal(c, service, token)
        for service in services
    ) + _gear_add_service_modal(c, token)

    component_edit = ""
    if not c.get("linked_gear_uuid"):
        component_edit = f"""
        <details class="gear-actions">
          <summary>Edit component</summary>
          <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/components', token))}">
            <input type="hidden" name="component_id" value="{_e(c['id'])}">
            <input type="hidden" name="bike_uuid" value="{_e(c['bike_uuid'])}">
            <label>Name<input type="text" name="name" value="{_e(c['name'])}" required></label>
            <label>Install date<input type="date" name="install_date" value="{_e(c['install_date'])}"></label>
            <label>Lifespan (km)<input type="number" step="1" min="0" name="maintenance_interval_km"
                value="{lifespan_value}" placeholder="optional"></label>
            <button type="submit">Save</button>
          </form>
        </details>"""

    unlink_form = f"""
      <form method="post" action="{_e(_gear_api_url('/api/gear/components', token))}" style="margin:0">
        <input type="hidden" name="component_id" value="{_e(c['id'])}">
        <input type="hidden" name="bike_uuid" value="{_e(c['bike_uuid'])}">
        <input type="hidden" name="name" value="{_e(c['name'])}">
        <input type="hidden" name="unlink" value="1">
        <button type="submit" style="border:0;background:transparent;padding:0;font:inherit;font-size:12px;color:var(--color-neutral-500);cursor:pointer">Unlink</button>
      </form>"""

    return f"""
    <div id="component-{_e(c['id'])}" class="gear-modal">
      <a href="#gm-modal-close" class="gear-modal-backdrop" aria-label="Close"></a>
      <div class="gear-modal-dialog">
        <div>
          <div style="font-size:16px;font-weight:600;font-family:var(--font-heading)">{_e(c["name"])}</div>
          <div style="font-size:12px;color:var(--color-neutral-500);margin-top:2px">{_e(bike_name)}</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span style="{_dot_style(color)}"></span><span style="font-size:12px;color:{color}">{_e(status_label)}</span>
        </div>
        <div style="height:6px;border-radius:99px;background:var(--color-neutral-800);overflow:hidden">
          <div style="height:100%;border-radius:99px;background:{color};width:{pct:.0f}%"></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px;color:var(--color-neutral-300)">
          <div><div style="color:var(--color-neutral-500);font-size:11px;margin-bottom:2px">Installed</div>{_e(c["install_date"])}</div>
          <div><div style="color:var(--color-neutral-500);font-size:11px;margin-bottom:2px">Last Serviced</div>{_e(last_serviced)}</div>
          <div><div style="color:var(--color-neutral-500);font-size:11px;margin-bottom:2px">KMs used</div>{_fmt_km(c.get("component_usage_km") or c.get("distance_since_km"))}</div>
          <div><div style="color:var(--color-neutral-500);font-size:11px;margin-bottom:2px">KMs left</div>{_fmt_km(c.get("km_until_replacement"))}</div>
        </div>
        {linked_note}
        <div style="border-top:1px solid var(--color-divider)"></div>
        <div>
          <div style="font-size:13px;font-weight:600;font-family:var(--font-heading);margin-bottom:8px">Services</div>
          <div class="gt-service-list">
            <div class="gt-service-head"><div>Service</div><div>Last</div><div>Next</div><div></div><div></div></div>
            {service_rows}
            {services_empty}
          </div>
          <a href="#service-add-{_e(c['id'])}" class="gt-service-log" title="Add service" aria-label="Add service" style="margin-top:7px">{_gear_icon("plus")}</a>
        </div>
        {component_edit}
        <div style="display:flex;align-items:center;justify-content:space-between;gap:12px">
          {unlink_form}
          <a href="#gm-modal-close" style="font-size:12px;color:var(--color-neutral-500);text-decoration:none">Close</a>
        </div>
      </div>
    </div>
    {service_modals}"""


def _gear_link_component_form(g: dict, linkable_gear: list[dict], token: str | None) -> str:
    """Replaces the old free-text "Add component" form (issue 63): pick one
    of the athlete's own Garmin-tracked gear items (a chain, cassette, tires,
    ... registered as its own gear — see the gear tool) to link, so status is
    judged against that item's real tracked distance instead of a
    hand-typed/guessed one. "Custom" still creates a plain, bike-distance-based
    component for parts Garmin doesn't track individually.

    A linked component is named after the Garmin gear item it is linked to.
    Custom components use the optional Name field, falling back to Type.

    There's no Install date field either, for the same reason: a linked
    component's install date comes from that Garmin gear item's own
    ``dateBegin`` — also carried in the option's value — falling back to
    today when Garmin doesn't have one (see post_component). A Custom
    component still just defaults to today, same as before.
    """
    from tools.gear_tracker import COMPONENT_TYPES

    opts = ['<option value="">Custom (not tracked in Garmin)</option>']
    for lg in linkable_gear:
        gear_name = lg.get("name") or "Unnamed gear"
        label = gear_name
        if lg.get("distance_km") is not None:
            label += f" — {_fmt_km(lg['distance_km'])}"
        value = f"{lg['uuid']}:{gear_name}:{lg.get('date_begin') or ''}"
        opts.append(f'<option value="{_e(value)}">{_e(label)}</option>')
    options_html = "".join(opts)
    type_options = "".join(f'<option value="{t}">{t}</option>' for t in COMPONENT_TYPES)
    return f"""
    <details class="gear-actions" style="margin-top:4px">
      <summary>+ Link component</summary>
      <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/components', token))}">
        <input type="hidden" name="bike_uuid" value="{_e(g.get('uuid') or '')}">
        <input type="hidden" name="bike_name" value="{_e(g.get('name') or '')}">
        <label>Garmin gear<select name="linked_gear_uuid">{options_html}</select></label>
        <label>Name<input type="text" name="name" placeholder="Custom component"></label>
        <label>Type<select name="component_type">{type_options}</select></label>
        <label>Lifespan (km)<input type="number" step="1" min="0" name="maintenance_interval_km" placeholder="optional"></label>
        <button type="submit">Link</button>
      </form>
    </details>"""


def _gear_bike_block(g: dict, linkable_gear: list[dict], token: str | None, is_first: bool) -> str:
    """One bike's entry in the Bike Component Tracker — an expandable
    `<details>` (open by default for the first bike, or any bike needing
    attention) holding its component rows and the Link-component form."""
    from tools.gear_tracker import list_maintenance_log

    components = g["components"]
    is_open = is_first or g["status_indicator"] == "red"

    if components:
        rows_html = "".join(_gear_component_row(c) for c in components)
        modals_html = "".join(
            _gear_component_modal(
                c, g.get("name"), list_maintenance_log(component_id=c["id"], limit=3), token,
            )
            for c in components
        )
        list_html = f"""
        <div style="overflow-x:auto">
          <div style="min-width:620px">
            <div class="gt-comp-grid" style="padding:4px 10px 8px;font-size:10px;text-transform:uppercase;
                letter-spacing:.05em;color:var(--color-neutral-500)">
              <div>Component</div><div>Last Serviced</div><div>KMs Used</div><div>Lifespan</div><div>Status</div>
            </div>
            <div style="display:flex;flex-direction:column;gap:4px">{rows_html}</div>
          </div>
        </div>
        {modals_html}"""
    else:
        list_html = '<div class="muted" style="font-size:12px">No components tracked yet — link one below.</div>'

    return f"""
    <details class="gt-bike" id="bike-{_e(g.get('uuid') or '')}"{" open" if is_open else ""}>
      <summary>
        <span style="display:flex;align-items:center;gap:10px">
          <span style="{_dot_style(_status_color(g["status_indicator"]))}"></span>
          <span style="font-size:15px;font-weight:600;font-family:var(--font-heading)">{_e(g.get("name"))}</span>
        </span>
      </summary>
      <div style="padding:0 14px 14px;display:flex;flex-direction:column;gap:6px">
        {list_html}
        {_gear_link_component_form(g, linkable_gear, token)}
      </div>
    </details>"""


def _gear_history_section(log_entries: list[dict], bike_names: dict) -> str:
    """Maintenance History — a table with a CSS-only (radio-driven, matching
    the tab/range-toggle pattern above) "Show all"/"Show less" beyond the
    first 5 rows, so long histories don't dominate the tab by default."""
    if not log_entries:
        return """
    <div>
      <div class="section-title">Maintenance History</div>
      <div class="muted" style="font-size:13px">No maintenance logged yet.</div>
    </div>"""

    visible_cap = 5
    rows = []
    for i, entry in enumerate(log_entries):
        bike_name = bike_names.get(entry.get("bike_uuid")) or entry.get("bike_name")
        cls = ' class="gt-hist-extra"' if i >= visible_cap else ""
        rows.append(f"""
        <tr{cls}>
          <td style="color:var(--color-neutral-400);white-space:nowrap">{_e(entry["date"])}</td>
          <td>{_e(bike_name)}</td>
          <td>{_e(entry.get("component_name"))}</td>
          <td>{_e((entry.get("action") or "").title())}</td>
          <td style="color:var(--color-neutral-500)">{_e(entry.get("notes") or "—")}</td>
        </tr>""")
    rows_html = "".join(rows)

    toggle_inputs, toggle_labels = "", ""
    if len(log_entries) > visible_cap:
        toggle_inputs = (
            '<input class="hide" type="radio" name="gt-hist" id="gt-hist-lim" checked>'
            '<input class="hide" type="radio" name="gt-hist" id="gt-hist-all">'
        )
        toggle_labels = (
            '<label class="gt-hist-toggle" for="gt-hist-all">Show all</label>'
            '<label class="gt-hist-toggle" for="gt-hist-lim">Show less</label>'
        )

    return f"""
    {toggle_inputs}
    <div class="gt-history">
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
        <div class="section-title" style="margin-bottom:0">Maintenance History</div>
        {toggle_labels}
      </div>
      <div style="overflow-x:auto">
        <table class="gear-table">
          <thead><tr><th>Date</th><th>Bike</th><th>Component</th><th>Action</th><th>Notes</th></tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>"""


def _panel_gear(data: dict, token: str | None = None, error: str | None = None) -> str:
    gear_status = data.get("gear_status")
    if not gear_status:
        err = data.get("gear_status_err") or "no data"
        return f'<section class="panel tabpanel tp-gear"><div class="err">Gear tracker unavailable — {_e(err)}</div></section>'

    gear = gear_status.get("gear") or []
    active_gear = [g for g in gear if (g.get("status") or "active").lower() == "active"]
    bikes = [g for g in active_gear if g["is_bike"]]
    shoes = [g for g in active_gear if g["is_shoe"]]
    linkable_gear = gear_status.get("linkable_gear") or []

    error_html = (
        f'<div style="padding:10px 12px;border-radius:var(--radius-md);font-size:13px;color:#f0b3ab;'
        f'background:color-mix(in srgb, #cf5a4e 16%, var(--color-surface))">{_e(error)}</div>'
        if error else ""
    )

    bike_cards_html = "".join(_gear_bike_overview_card(g) for g in bikes) or \
        '<div class="muted" style="font-size:13px">No bikes registered yet.</div>'
    shoe_cards_html = "".join(_gear_shoe_card(g) for g in shoes) or \
        '<div class="muted" style="font-size:13px">No shoes registered yet.</div>'

    bikes_html = "".join(
        _gear_bike_block(g, linkable_gear, token, is_first=(i == 0)) for i, g in enumerate(bikes)
    ) or '<div class="muted" style="font-size:13px">No bikes registered yet.</div>'

    # The maintenance log comes from gear_status itself (build_gear_status
    # already fetched it) rather than a second gear_tracker call/connection —
    # see issue #58.
    bike_names = {g["uuid"]: g.get("name") for g in gear if g.get("uuid")}
    log_entries = gear_status.get("maintenance_log") or []
    history_html = _gear_history_section(log_entries, bike_names)

    return f"""
    <section class="panel tabpanel tp-gear" style="flex-direction:column;gap:20px">
      <div style="font-family:var(--font-heading);font-size:20px">Gear</div>
      {error_html}
      <span id="gm-modal-close"></span>
      <div>
        <div class="section-title">Bikes</div>
        <div class="gt-grid">{bike_cards_html}</div>
      </div>
      <div>
        <div class="section-title">Shoes</div>
        <div class="gt-grid">{shoe_cards_html}</div>
      </div>
      <div>
        <div class="section-title">Bike Component Tracker</div>
        {bikes_html}
      </div>
      {history_html}
    </section>"""


# ── ASSEMBLY ─────────────────────────────────────────────────────────────────

_SVG_DEFS = """
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <linearGradient id="gArea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--color-accent)" stop-opacity="0.34"></stop><stop offset="1" stop-color="var(--color-accent)" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gRhr" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#cf5a4e" stop-opacity="0.34"></stop><stop offset="1" stop-color="#cf5a4e" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gHrv" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7fc9b0" stop-opacity="0.34"></stop><stop offset="1" stop-color="#7fc9b0" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gSleep" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6f9ce8" stop-opacity="0.34"></stop><stop offset="1" stop-color="#6f9ce8" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gStress" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d9a441" stop-opacity="0.34"></stop><stop offset="1" stop-color="#d9a441" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gSteps" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4fae72" stop-opacity="0.34"></stop><stop offset="1" stop-color="#4fae72" stop-opacity="0"></stop></linearGradient>
</defs></svg>"""


def _pwa_asset_url(path: str, token: str | None) -> str:
  return f"{path}?{urlencode({'token': token})}" if token else path


def _gear_api_url(path: str, token: str | None) -> str:
    """A /api/gear/* route URL, carrying the bearer token when one was supplied."""
    return f"{path}?{urlencode({'token': token})}" if token else path


# ── CHART INTERACTIVITY (vanilla JS, no external libraries) ────────────────
# Reads data off the markup emitted above: `data-points` (JSON [{x,y,d,v}, ...]
# in viewBox coordinates) on line/area charts, and `data-date`/`data-value`
# text attributes on tappable bar segments. Drives a single shared tooltip +
# per-chart crosshair overlay; never touches the underlying data.

_CHART_JS = """
(function () {
  var tip = document.getElementById('chart-tooltip');
  if (!tip) return;
  var tipDate = tip.querySelector('.tt-date');
  var tipVal = tip.querySelector('.tt-val');
  var activeBar = null;
  var activeLine = null;

  function pointFromEvent(e) {
    if (e.touches && e.touches.length) return e.touches[0];
    if (e.changedTouches && e.changedTouches.length) return e.changedTouches[0];
    return e;
  }

  function showTip(clientX, clientY, dateText, valueText, above) {
    tipDate.textContent = dateText;
    tipVal.textContent = valueText;
    tip.style.display = 'block';
    var pad = 14;
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = clientX - tw / 2;
    var y = above ? clientY - th - pad : clientY + pad;
    if (above && y < 4) y = clientY + pad;
    if (!above && y + th > window.innerHeight) y = clientY - th - pad;
    if (x + tw > window.innerWidth) x = window.innerWidth - tw - 4;
    if (x < 4) x = 4;
    tip.style.left = x + 'px';
    tip.style.top = Math.max(4, y) + 'px';
  }

  function hideAll() {
    tip.style.display = 'none';
    if (activeLine) {
      activeLine.crosshair.style.opacity = 0;
      activeLine.dot.style.opacity = 0;
      activeLine = null;
    }
    activeBar = null;
  }

  function wireCharts(root) {
    (root || document).querySelectorAll('svg.js-linechart').forEach(function (svg) {
      if (svg.__wired) return;
      svg.__wired = true;
      var pts;
      try { pts = JSON.parse(svg.getAttribute('data-points') || '[]'); } catch (err) { return; }
      if (!pts.length) return;
      var hit = svg.querySelector('.chart-hit');
      var crosshair = svg.querySelector('.chart-crosshair');
      var dot = svg.querySelector('.chart-dot');
      if (!hit || !crosshair || !dot) return;
      var vb = svg.viewBox.baseVal;

      function nearest(clientX) {
        var rect = svg.getBoundingClientRect();
        var vx = rect.width ? (clientX - rect.left) / rect.width * vb.width : pts[0].x;
        var best = pts[0], bestDist = Infinity;
        for (var i = 0; i < pts.length; i++) {
          var d = Math.abs(pts[i].x - vx);
          if (d < bestDist) { bestDist = d; best = pts[i]; }
        }
        return best;
      }

      function update(e) {
        var p = pointFromEvent(e);
        var pt = nearest(p.clientX);
        crosshair.setAttribute('x1', pt.x);
        crosshair.setAttribute('x2', pt.x);
        crosshair.style.opacity = 1;
        dot.setAttribute('cx', pt.x);
        dot.setAttribute('cy', pt.y);
        dot.style.opacity = 1;
        activeBar = null;
        activeLine = { crosshair: crosshair, dot: dot };
        showTip(p.clientX, p.clientY, pt.d, pt.v, true);
      }

      hit.addEventListener('mousemove', update);
      hit.addEventListener('mousedown', update);
      hit.addEventListener('mouseleave', hideAll);
      hit.addEventListener('touchstart', function (e) { e.preventDefault(); update(e); }, { passive: false });
      hit.addEventListener('touchmove', function (e) { e.preventDefault(); update(e); }, { passive: false });
    });

    (root || document).querySelectorAll('.js-bar').forEach(function (bar) {
      if (bar.__wired) return;
      bar.__wired = true;
      function activate(e) {
        if (activeBar === bar) { hideAll(); return; }
        hideAll();
        activeBar = bar;
        var p = pointFromEvent(e);
        showTip(p.clientX, p.clientY, bar.getAttribute('data-date') || '', bar.getAttribute('data-value') || '');
      }
      bar.addEventListener('click', activate);
      bar.addEventListener('touchstart', function (e) { e.preventDefault(); activate(e); }, { passive: false });
    });
  }

  wireCharts(document);
  window.__wireCharts = wireCharts;

  // The activity-detail HR/power charts (issue 74) read inline into the
  // card itself (a big current value, a zone badge, a marker sliding along
  // the zone-bounds bar) rather than the floating #chart-tooltip above —
  // wired separately since it targets a different set of sibling elements.
  function wireAdCharts(root) {
    (root || document).querySelectorAll('svg.js-adchart').forEach(function (svg) {
      if (svg.__wired) return;
      svg.__wired = true;
      var pts;
      try { pts = JSON.parse(svg.getAttribute('data-points') || '[]'); } catch (err) { return; }
      if (!pts.length) return;
      var card = svg.closest('.ad-chart-card');
      var hit = svg.querySelector('.chart-hit');
      var crosshair = svg.querySelector('.chart-crosshair');
      var dot = svg.querySelector('.chart-dot');
      if (!card || !hit || !crosshair || !dot) return;
      var vb = svg.viewBox.baseVal;
      var numEl = card.querySelector('.ad-chart-num');
      var capEl = card.querySelector('.ad-chart-caption');
      var zoneEl = card.querySelector('.ad-chart-zone');
      var markerEl = card.querySelector('.ad-chart-marker');
      var avg = {
        value: card.getAttribute('data-avg-value'),
        caption: card.getAttribute('data-avg-caption'),
        zoneLabel: card.getAttribute('data-avg-zone-label'),
        zoneColor: card.getAttribute('data-avg-zone-color'),
        marker: card.getAttribute('data-avg-marker'),
      };

      function nearest(clientX) {
        var rect = svg.getBoundingClientRect();
        var vx = rect.width ? (clientX - rect.left) / rect.width * vb.width : pts[0].x;
        var best = pts[0], bestDist = Infinity;
        for (var i = 0; i < pts.length; i++) {
          var d = Math.abs(pts[i].x - vx);
          if (d < bestDist) { bestDist = d; best = pts[i]; }
        }
        return best;
      }

      function apply(value, caption, zoneLabel, zoneColor, marker) {
        if (numEl) numEl.textContent = value;
        if (capEl) capEl.textContent = caption;
        if (zoneEl && zoneLabel) { zoneEl.textContent = zoneLabel; zoneEl.style.color = zoneColor; }
        if (markerEl && marker !== '' && marker != null) {
          markerEl.style.left = marker + '%';
          markerEl.style.background = zoneColor;
        }
      }

      function update(e) {
        var p = pointFromEvent(e);
        var pt = nearest(p.clientX);
        crosshair.setAttribute('x1', pt.x);
        crosshair.setAttribute('x2', pt.x);
        crosshair.style.opacity = 1;
        dot.setAttribute('cx', pt.x);
        dot.setAttribute('cy', pt.y);
        dot.style.opacity = 1;
        apply(pt.v, pt.t, pt.zl, pt.zc, pt.mp);
      }

      function reset() {
        crosshair.style.opacity = 0;
        dot.style.opacity = 0;
        apply(avg.value, avg.caption, avg.zoneLabel, avg.zoneColor, avg.marker);
      }

      hit.addEventListener('mousemove', update);
      hit.addEventListener('mousedown', update);
      hit.addEventListener('mouseleave', reset);
      hit.addEventListener('touchstart', function (e) { e.preventDefault(); update(e); }, { passive: false });
      hit.addEventListener('touchmove', function (e) { e.preventDefault(); update(e); }, { passive: false });
      hit.addEventListener('touchend', reset);
    });
  }

  wireAdCharts(document);
  window.__wireAdCharts = wireAdCharts;

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.js-linechart') && !e.target.closest('.js-bar')) hideAll();
  });

  window.addEventListener('scroll', hideAll, { passive: true, capture: true });
})();
"""

# Converts the server-rendered "Last sync" time (offset by the operator's
# configured DASHBOARD_TZ_OFFSET_HOURS) to the viewer's actual local
# timezone, using the UTC instant stashed in the element's data attribute.
_TZ_JS = """
document.querySelectorAll('[data-sync-utc]').forEach(function (el) {
  var d = new Date(el.getAttribute('data-sync-utc'));
  if (!isNaN(d.getTime())) {
    el.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
});
"""

# A component's modal (tools/dashboard.py's .gear-modal, shown via CSS
# :target — see _gear_component_modal) is a single, persistent DOM node
# toggled purely by visibility, not re-rendered each time it opens. Its
# "Edit component" <details> is native HTML state, so once expanded it stays
# expanded across later opens/closes within the same page load — this resets
# it back closed on every navigation between (or out of) a component modal,
# so "Edit component" is always collapsed on a fresh open.
_GEAR_MODAL_JS = """
window.addEventListener('hashchange', function () {
  document.querySelectorAll('.gear-modal .gear-actions[open]').forEach(function (d) { d.open = false; });
});
"""

# The activity-detail modal (issue 74) — a single persistent bottom-sheet node
# (#activity-modal), fetched-and-injected per open rather than pre-rendered
# per activity, so opening it never pulls chart/lap/gear data for activities
# the viewer hasn't clicked. Route map tiles need Leaflet, loaded lazily from
# a CDN on first use rather than bundled, since most opens never show a map
# (indoor/no-GPS activities hide that section entirely).
_ACTIVITY_MODAL_JS = """
(function () {
  var modal = document.getElementById('activity-modal');
  if (!modal) return;
  var backdrop = modal.querySelector('.activity-modal-backdrop');
  var body = document.getElementById('activity-modal-body');
  var leafletMap = null;
  var leafletLoading = null;

  function token() { return document.body.getAttribute('data-token') || ''; }

  function destroyMap() {
    if (leafletMap) { leafletMap.remove(); leafletMap = null; }
  }

  function loadLeaflet() {
    if (window.L) return Promise.resolve();
    if (leafletLoading) return leafletLoading;
    leafletLoading = new Promise(function (resolve) {
      var link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
      document.head.appendChild(link);
      var script = document.createElement('script');
      script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
      script.onload = function () { resolve(); };
      script.onerror = function () { resolve(); };
      document.head.appendChild(script);
    });
    return leafletLoading;
  }

  function initMap() {
    var el = document.getElementById('activity-map');
    if (!el || !window.L) return;
    var pts;
    try { pts = JSON.parse(el.getAttribute('data-route') || '[]'); } catch (err) { return; }
    if (!pts.length) return;
    destroyMap();
    var map = L.map(el, { zoomControl: true, attributionControl: true, scrollWheelZoom: false });
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; OpenStreetMap contributors &copy; CARTO', maxZoom: 19,
    }).addTo(map);
    var line = L.polyline(pts, { color: '#9184d9', weight: 3, opacity: 0.9 }).addTo(map);
    map.fitBounds(line.getBounds(), { padding: [16, 16] });
    L.circleMarker(pts[0], { radius: 6, color: '#4fae72', fillColor: '#4fae72', fillOpacity: 1 }).addTo(map);
    L.circleMarker(pts[pts.length - 1], { radius: 6, color: '#cf5a4e', fillColor: '#cf5a4e', fillOpacity: 1 }).addTo(map);
    leafletMap = map;
  }

  window.openActivityModal = function (id) {
    modal.classList.add('open');
    document.body.style.overflow = 'hidden';
    body.innerHTML = '<div class="muted" style="padding:32px 20px;text-align:center;font-size:13px">Loading…</div>';
    var url = '/api/activity/' + id + (token() ? '?token=' + encodeURIComponent(token()) : '');
    fetch(url).then(function (res) { return res.text(); }).then(function (responseHtml) {
      body.innerHTML = responseHtml;
      window.__wireCharts && window.__wireCharts(body);
      window.__wireAdCharts && window.__wireAdCharts(body);
      if (body.querySelector('#activity-map')) loadLeaflet().then(initMap);
    }).catch(function () {
      body.innerHTML = '<div class="muted" style="padding:32px 20px;text-align:center;font-size:13px">Couldn\\u2019t load this activity.</div>';
    });
  };

  window.closeActivityModal = function () {
    modal.classList.remove('open');
    document.body.style.overflow = '';
    destroyMap();
  };

  backdrop.addEventListener('click', window.closeActivityModal);
  var closeBtn = modal.querySelector('.activity-modal-close');
  if (closeBtn) closeBtn.addEventListener('click', window.closeActivityModal);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.classList.contains('open')) window.closeActivityModal();
  });

  var dragStartY = null;
  function dragStart(e) { dragStartY = (e.touches ? e.touches[0] : e).clientY; }
  function dragEnd(e) {
    if (dragStartY == null) return;
    var endY = (e.changedTouches ? e.changedTouches[0] : e).clientY;
    if (endY - dragStartY > 80) window.closeActivityModal();
    dragStartY = null;
  }
  var handle = modal.querySelector('.activity-modal-handle');
  if (handle) {
    handle.addEventListener('touchstart', dragStart, { passive: true });
    handle.addEventListener('touchend', dragEnd);
  }
})();
"""


def _fmt_sync_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            dt = (datetime.fromtimestamp(value / 1000, tz=timezone.utc) + timedelta(hours=_tz_offset_hours()))
            return dt.strftime("%H:%M")
        except (ValueError, OverflowError, OSError):
            return str(value)
    return str(value).replace("T", " ").split(".")[0][-8:-3] if "T" in str(value) else str(value)


def _sync_time_utc_iso(value):
    """The device's last-upload time as a UTC ISO-8601 string, for the
    browser to convert to the viewer's own local timezone client-side.

    Returns None if `value` can't be parsed — the caller then falls back to
    the server-rendered `_fmt_sync_time` text (offset by
    DASHBOARD_TZ_OFFSET_HOURS rather than the viewer's real timezone).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).split(".")[0])
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


_TAB_IDS = {
    "today": "tab-today", "trends": "tab-trends", "activity": "tab-activity",
    "fitness": "tab-you", "gear": "tab-gear",
}
_DEFAULT_TAB = "today"


def render_dashboard_html(data: dict, token: str | None = None,
                          initial_tab: str | None = None, error: str | None = None) -> str:
    """Render the dashboard data dict into a complete HTML document.

    ``token`` is threaded into the injected site navigation and every
    gear-tracker form action, so navigating or submitting never drops the
    ``?token=`` bearer auth.

    ``initial_tab`` (one of "today" (default), "trends", "activity",
    "fitness", "gear") selects which tab starts open — used so a gear-tracker
    form submission can redirect back to the Gear tab specifically rather
    than always landing on Today.

    ``error``, when given, is shown as a banner at the top of the Gear tab —
    a gear-tracker form error (see ``_error_redirect_url`` in
    tools/gear_tracker.py) redirects back here with ``?error=...``.
    """
    weekday_line = data.get("date") or ""
    try:
        weekday_line = date.fromisoformat(data["date"]).strftime("%A %-d %B")
    except (KeyError, ValueError, TypeError):
        pass

    sync = data.get("last_sync") or {}
    sync_time = _fmt_sync_time(sync.get("upload_time"))
    sync_utc_iso = _sync_time_utc_iso(sync.get("upload_time"))
    sync_attr = f' data-sync-utc="{html.escape(sync_utc_iso, quote=True)}"' if sync_utc_iso else ""
    sync_line = (
        f'Last sync <span id="sync-time"{sync_attr}>{_e(sync_time)}</span>'
        if sync_time else "Live from Garmin Connect"
    )

    # A plain <meta http-equiv="refresh"> reloads unconditionally on its
    # timer — including mid-look at the activity-detail modal (issue 74
    # feedback: the page would reload and silently close it out from under
    # whoever was reading it). This does the same periodic refresh from JS
    # instead, so it can check first and defer while that modal is open.
    refresh_script = (
        f"""<script>(function () {{
  var seconds = {REFRESH_SECONDS};
  function tick() {{
    var modal = document.getElementById('activity-modal');
    if (modal && modal.classList.contains('open')) {{ setTimeout(tick, 15000); return; }}
    location.reload();
  }}
  setTimeout(tick, seconds * 1000);
}})();</script>""" if REFRESH_SECONDS > 0 else ""
    )

    active_tab_id = _TAB_IDS.get(initial_tab, _TAB_IDS[_DEFAULT_TAB])
    activity_filter_inputs = "".join(
      f'<input class="hide" type="radio" name="activity-filter" id="activity-filter-{key}"{" checked" if key == "all" else ""}>'
      for key in _ACTIVITY_FILTERS
    )

    panels = (_panel_today(data) + _panel_trends(data) + _panel_activity(data, token)
             + _panel_fitness(data) + _panel_gear(data, token, error))

    body = f"""
<div style="min-height:100vh;background:
    radial-gradient(120% 60% at 12% -10%, color-mix(in srgb, var(--color-accent) 13%, transparent), transparent 60%),
    var(--color-bg);color:var(--color-text);font-family:var(--font-body);padding-bottom:104px">
  {_SVG_DEFS}
  <div style="position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);
      background:color-mix(in srgb, var(--color-bg) 78%, transparent);border-bottom:1px solid var(--color-divider)">
    <div style="max-width:1120px;margin:0 auto;padding:11px 16px;display:flex;align-items:center;gap:12px">
      <div style="width:26px;height:26px;border-radius:50%;border:1px solid var(--color-accent);
          display:grid;place-items:center;color:var(--color-accent);font-size:15px"><i class="ph">{_PULSE}</i></div>
      <div style="flex:1;min-width:0">
        <div style="font-family:var(--font-heading);font-size:15px;line-height:1.1">{_e(weekday_line)}</div>
        <div style="font-size:11px;color:var(--color-neutral-500)">{sync_line}</div>
      </div>
    </div>
  </div>

  <input class="hide" type="radio" name="tab" id="tab-today"{" checked" if active_tab_id == "tab-today" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-trends"{" checked" if active_tab_id == "tab-trends" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-activity"{" checked" if active_tab_id == "tab-activity" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-you"{" checked" if active_tab_id == "tab-you" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-gear"{" checked" if active_tab_id == "tab-gear" else ""}>
  <input class="hide" type="checkbox" id="more-menu">
  {activity_filter_inputs}

  <div class="tabpanels" style="max-width:1120px;margin:0 auto;padding:16px">{panels}</div>

  <div class="botnav" style="position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;justify-content:center;padding:0 16px 16px;pointer-events:none">
    <div style="pointer-events:auto;display:flex;gap:2px;padding:6px;border-radius:999px;width:min(420px,100%);
        background:color-mix(in srgb, var(--color-surface) 92%, transparent);backdrop-filter:blur(16px);box-shadow:var(--shadow-md)">
      <label for="tab-today"><i class="ph">&#xe2c2;</i><span>Today</span></label>
      <label for="tab-trends"><i class="ph">&#xe154;</i><span>Trends</span></label>
      <label for="tab-activity"><i class="ph">&#xed60;</i><span>Activity</span></label>
      <label for="more-menu">{_svg_icon(_MORE_ICON_PATH)}<span>More</span></label>
    </div>
  </div>

  <label for="more-menu" class="more-menu-backdrop" aria-hidden="true"></label>
  <div class="more-menu-sheet" role="dialog" aria-modal="true" aria-label="More">
    <label for="tab-gear" class="more-menu-item" onclick="document.getElementById('more-menu').checked=false">
      <i class="ph">{_WRENCH}</i>Gear</label>
    <label for="tab-you" class="more-menu-item" onclick="document.getElementById('more-menu').checked=false">
      <i class="ph">{_HEARTBEAT}</i>Fitness</label>
    <a class="more-menu-item more-menu-group-start" href="{_e(_pwa_asset_url('/weekly-summary', token))}">
      {_svg_icon(_CHART_ICON_PATH)}Weekly Summary</a>
    <a class="more-menu-item" href="{_e(_pwa_asset_url('/training-plan', token))}">
      {_svg_icon(_CALENDAR_ICON_PATH)}Training Plan</a>
  </div>

  <div id="chart-tooltip" class="chart-tooltip" role="status" aria-live="polite">
    <div class="tt-date"></div><div class="tt-val"></div>
  </div>

  <div id="activity-modal" class="activity-modal" role="dialog" aria-modal="true" aria-label="Activity detail">
    <div class="activity-modal-backdrop"></div>
    <div class="activity-modal-sheet">
      <button type="button" class="activity-modal-close" aria-label="Close">&times;</button>
      <div class="activity-modal-draghandle"><div class="activity-modal-handle"></div></div>
      <div id="activity-modal-body" class="activity-modal-body"></div>
    </div>
  </div>
</div>"""

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        f'<link rel="manifest" href="{_e(_pwa_asset_url("/manifest.webmanifest", token))}">'
        f"{refresh_script}"
        "<title>Garmin Health Dashboard</title>"
        f"<style>{_STYLE}</style>"
        f'</head><body data-token="{html.escape(token, quote=True) if token else ""}">'
        f"{body}"
        f"<script>{_CHART_JS}</script>"
        f"<script>{_TZ_JS}</script>"
        f"<script>{_GEAR_MODAL_JS}</script>"
        f"<script>{_ACTIVITY_MODAL_JS}</script>"
        f'<script>if ("serviceWorker" in navigator) navigator.serviceWorker.register("{_e(_pwa_asset_url("/sw.js", token))}");</script>'
        "</body></html>"
    )
