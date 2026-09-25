# tools/db_first.py
"""Database-first reads for the MCP tools.

sync_garmin.py stores the per-day health tools' output (get_sleep,
get_daily_health, get_daily_readiness, get_training_readiness,
get_training_status) in daily_metrics exactly as those functions return it.
When DATABASE_URL is set, the MCP tools serve a synced row instead of calling
Garmin Connect, and go live only for data the sync job hasn't captured yet.

A synced row is used when it is either:
  - final:  synced after the day ended (local midnight, per
            DASHBOARD_TZ_OFFSET_HOURS), so the day can no longer change; or
  - fresh:  synced within MCP_DB_MAX_AGE_SECONDS (default 900s) — covers
            today, which is still accumulating.

Anything else falls through to the live Garmin call. If that call fails and a
(stale) synced row exists, the stale row is returned rather than an error.

Every result carries a 'data_source' block saying where it came from.
Set MCP_DB_FIRST=0 to disable the database path entirely.
"""
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from garmin_client import get_client
from tools import trends
from tools.health import resolve_date

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AGE_SECONDS = 900

# get_trends series key → get_trend_series_rows column, where they differ.
_TREND_COLUMNS = {'rhr': 'resting_hr'}

_STALE_NOTE = "Garmin Connect was unreachable; returning the last synced copy."


def enabled() -> bool:
    """True when DATABASE_URL is set and MCP_DB_FIRST isn't switched off."""
    if os.environ.get("MCP_DB_FIRST", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    import db
    return db.is_configured()


def _max_age() -> timedelta:
    try:
        seconds = float(os.environ.get("MCP_DB_MAX_AGE_SECONDS", _DEFAULT_MAX_AGE_SECONDS))
    except ValueError:
        seconds = _DEFAULT_MAX_AGE_SECONDS
    return timedelta(seconds=seconds)


def _local_tz() -> timezone:
    try:
        hours = float(os.environ.get("DASHBOARD_TZ_OFFSET_HOURS", "0"))
    except ValueError:
        hours = 0.0
    return timezone(timedelta(hours=hours))


def row_is_usable(metric_date: str, synced_at: Optional[datetime],
                  now: Optional[datetime] = None) -> bool:
    """Whether a daily_metrics row synced at `synced_at` can stand in for a
    live fetch of `metric_date` — final (synced after that day ended) or
    fresh (synced within the max age)."""
    if synced_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    day_end = datetime.combine(date.fromisoformat(metric_date) + timedelta(days=1),
                               time.min, tzinfo=_local_tz())
    return synced_at >= day_end or now - synced_at <= _max_age()


def _source(source: str, synced_at: Optional[datetime] = None, **extra) -> dict:
    info = {"source": source}
    if synced_at is not None:
        info["synced_at"] = synced_at.isoformat()
    info.update(extra)
    return info


def _with_source(payload, info: dict):
    return {**payload, "data_source": info} if isinstance(payload, dict) else payload


def daily_payload(column: str, date_str: str, live_fn: Callable[[str], dict]) -> dict:
    """Serve one per-day tool from daily_metrics.<column>, else `live_fn`.

    `column` is the JSONB column sync_garmin.py fills from `live_fn` itself
    (e.g. 'sleep_data' ← get_sleep), so both paths return the same shape.
    """
    d = resolve_date(date_str)

    row = None
    if enabled():
        try:
            import db
            row = db.get_today_metrics(d)
        except Exception:
            logger.warning("DB read of daily_metrics for %s failed; going live", d, exc_info=True)
    payload = (row or {}).get(column)

    if payload and row_is_usable(d, row.get("synced_at")):
        return _with_source(payload, _source("db", row["synced_at"]))

    try:
        result = live_fn(d)
    except Exception:
        if payload:
            logger.warning("Live %s fetch for %s failed; serving stale DB row",
                           getattr(live_fn, "__name__", "tool"), d, exc_info=True)
            return _with_source(payload, _source("db", row.get("synced_at"),
                                                 stale=True, note=_STALE_NOTE))
        raise
    return _with_source(result, _source("live"))


def _num(value):
    """Normalise DB numerics (Decimal / whole floats) to what the live path
    returns — ints for whole values, floats otherwise."""
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def get_trends(period: str = '1m', metrics: Optional[list] = None) -> dict:
    """tools.trends.get_trends, reading synced days from daily_metrics and
    fetching live only the days the DB doesn't cover (or covers stalely).

    After a history backfill (sync_garmin.py --since …) a 1y trend is one
    query plus a live fetch of just today, instead of hundreds of per-day
    Garmin calls.
    """
    requested = trends.validate_trends_request(period, metrics)
    if not enabled():
        return _with_source(trends.get_trends(period, requested), _source("live"))

    dates = trends.trend_window(period)
    keys = trends.output_series_keys(requested)

    try:
        import db
        rows = db.get_trend_series_rows(dates[0], dates[-1])
    except Exception:
        logger.warning("DB read of trend rows failed; going live", exc_info=True)
        rows = []

    now = datetime.now(timezone.utc)
    by_date = {str(r["metric_date"]): r for r in rows}
    usable = {d: r for d, r in by_date.items() if row_is_usable(d, r.get("synced_at"), now)}
    missing = [d for d in dates if d not in usable]

    def fill(target: dict, day_rows: dict):
        for d, r in day_rows.items():
            for key in keys:
                target[key][d] = _num(r.get(_TREND_COLUMNS.get(key, key)))

    values = {key: {} for key in keys}
    fill(values, usable)

    extra = {}
    live_days = len(missing)
    if missing:
        try:
            live = trends.fetch_live_series(get_client(), missing, requested)
        except Exception:
            if not by_date:
                raise
            logger.warning("Live trend fetch failed; filling gaps from stale DB rows", exc_info=True)
            stale_rows = {d: by_date[d] for d in missing if d in by_date}
            fill(values, stale_rows)
            live_days = 0
            extra = {"stale": True, "stale_days": len(stale_rows), "note": _STALE_NOTE}
        else:
            for key in keys:
                values[key].update(live.get(key, {}))

    result = trends.assemble_trends(period, dates, requested, values)
    if not usable and live_days:
        source = "live"
    elif live_days:
        source = "db+live"
    else:
        source = "db"
    synced = [r["synced_at"] for r in usable.values() if r.get("synced_at")]
    return _with_source(result, _source(
        source, max(synced) if synced else None,
        db_days=len(usable), live_days=live_days, **extra,
    ))
