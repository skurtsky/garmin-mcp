"""The athlete's local clock.

The server (and its container) runs on UTC, so `date.today()` and a naive
`datetime.now()` roll over to tomorrow at UTC midnight — the evening before,
for anyone west of Greenwich. Everything that means "the athlete's today"
goes through here instead.

The zone, first match wins:
  1. the time zone picked in Settings (app_settings under ``timezone``, an
     IANA name such as `America/Toronto`; follows daylight saving);
  2. LOCAL_TIMEZONE, the same kind of name, from the environment;
  3. the older fixed DASHBOARD_TZ_OFFSET_HOURS (e.g. `-4`);
  4. UTC.
"""
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db

logger = logging.getLogger(__name__)

SETTING_KEY = "timezone"
# local_tz() runs often (per row, in places), so the stored zone is read from
# the database at most this often; saving it here takes effect at once.
_CACHE_SECONDS = 60
_cache_lock = threading.Lock()
_cache: dict = {}


class TimezoneError(ValueError):
    """An invalid time zone; the message is user-facing."""


def validate_zone(name) -> str:
    """A known IANA zone name, or TimezoneError."""
    name = str(name or "").strip()
    if not name:
        raise TimezoneError("Pick a time zone.")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise TimezoneError(f"Unknown time zone: {name}.") from None
    return name


def clear_cache():
    with _cache_lock:
        _cache.clear()


def stored_zone() -> str | None:
    """The zone picked in Settings, or None (unset, invalid, or no database)."""
    if not db.is_configured():
        return None
    now = time.monotonic()
    with _cache_lock:
        if "at" in _cache and now - _cache["at"] < _CACHE_SECONDS:
            return _cache["value"]
    try:
        value = db.get_setting(SETTING_KEY)
        value = validate_zone(value) if isinstance(value, str) else None
    except TimezoneError:
        value = None
    except Exception:  # noqa: BLE001 — an unreachable database mustn't stop the clock
        logger.warning("Couldn't read the stored time zone", exc_info=True)
        value = None
    with _cache_lock:
        _cache.update(at=now, value=value)
    return value


def save_zone(name: str | None) -> str | None:
    """Store the Settings time zone (validated), or clear it with None."""
    name = validate_zone(name) if name is not None else None
    db.set_setting(SETTING_KEY, name)
    clear_cache()
    return name


def _env_zone() -> tzinfo | None:
    name = (os.environ.get("LOCAL_TIMEZONE") or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("LOCAL_TIMEZONE=%r is not a known zone; ignoring it", name)
        return None


def _offset_zone() -> tzinfo | None:
    try:
        hours = float(os.environ.get("DASHBOARD_TZ_OFFSET_HOURS") or "0")
    except ValueError:
        return None
    return timezone(timedelta(hours=hours)) if hours else None


def zone_source() -> tuple[tzinfo, str]:
    """(the zone in effect, where it came from: settings / env / offset / default)."""
    stored = stored_zone()
    if stored:
        return ZoneInfo(stored), "settings"
    if (env := _env_zone()) is not None:
        return env, "env"
    if (offset := _offset_zone()) is not None:
        return offset, "offset"
    return timezone.utc, "default"


def local_tz() -> tzinfo:
    return zone_source()[0]


def zone_label(tz: tzinfo) -> str:
    """'America/Toronto', or 'UTC-04:00' for a fixed offset."""
    key = getattr(tz, "key", None)
    if key:
        return key
    minutes = int(datetime.now(tz).utcoffset().total_seconds() // 60)
    if not minutes:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


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


def settings_payload() -> dict:
    """What the Settings editor needs: the zone picked there (or None), the
    zone actually in effect and where it comes from, and the server's idea
    of now (the picker's list of zones comes from the browser)."""
    tz, source = zone_source()
    now = datetime.now(tz)
    return {
        "timezone": stored_zone(),
        "effective": zone_label(tz),
        "source": source,
        "now": now.isoformat(timespec="minutes"),
    }
