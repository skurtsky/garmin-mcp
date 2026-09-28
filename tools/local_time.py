"""The athlete's local clock.

The server (and its container) runs on UTC, so `date.today()` and a naive
`datetime.now()` roll over to tomorrow at UTC midnight — the evening before,
for anyone west of Greenwich. Everything that means "the athlete's today"
goes through here instead.

The zone comes from LOCAL_TIMEZONE, an IANA name such as
`America/Toronto` (follows daylight saving). Failing that, the older fixed
DASHBOARD_TZ_OFFSET_HOURS (e.g. `-4`); failing both, UTC.
"""
import logging
import os
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def local_tz() -> tzinfo:
    name = (os.environ.get("LOCAL_TIMEZONE") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("LOCAL_TIMEZONE=%r is not a known zone; ignoring it", name)
    try:
        hours = float(os.environ.get("DASHBOARD_TZ_OFFSET_HOURS") or "0")
    except ValueError:
        hours = 0.0
    return timezone(timedelta(hours=hours))


def local_now() -> datetime:
    """Current time in the athlete's zone (timezone-aware)."""
    return datetime.now(local_tz())


def local_today() -> date:
    """Today's date in the athlete's zone."""
    return local_now().date()


def utc_offset_hours(at: datetime | None = None) -> float:
    """The zone's offset from UTC in hours at `at` (default now)."""
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(local_tz()).utcoffset().total_seconds() / 3600


def to_local(dt: datetime) -> datetime:
    """A UTC instant (naive values are taken as UTC) in the athlete's zone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz())
