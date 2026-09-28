# tools/plan_calendar.py
"""The active training plan as a subscribable calendar feed (iCalendar).

A calendar app subscribes to ``/training-plan/calendar.ics?key=<token>`` and
re-downloads it on its own schedule, so plan edits show up without anything
being pushed. The key is a separate, read-only token generated (and revoked)
from the plan viewer's Settings; it opens this one route and nothing else.

Plans only carry a date per workout, so each event's start time comes from,
in order:

1. the workout's own ``startTime`` ("HH:MM"), when the plan sets one;
2. the weekly schedule in Settings — one row per weekday and sport, e.g.
   Sunday · bike · 07:00;
3. the schedule's default time.

The end is the start plus ``durationMinutes``; a workout without a duration
becomes an all-day event. When two workouts on a day would overlap (a brick,
or two sports on the same slot), the later one starts when the earlier ends.
Times are "floating" (no time zone): 06:00 means 06:00 wherever the athlete
is, which is what a training routine means when travelling.

Every function raises ``PlanStorageUnavailable`` without DATABASE_URL and
``PlanError`` for an invalid schedule (its message is user-facing).
"""
import re
import secrets
from datetime import date, datetime, timedelta, timezone

import db
from tools import plan_doc, plan_service
from tools.plan_doc import PlanError

FEED_PATH = "/training-plan/calendar.ics"
DEFAULT_TIME = "06:00"
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SCHEDULE_SPORTS = tuple(s for s in plan_doc.SPORTS if s != "rest")

_TOKEN_RE = re.compile(r"\{\{\s*(run-pace|run-hr|bike-watts|bike-hr|swim-pace)\s*:\s*([1-5][abc]?)\s*\}\}", re.I)
_TOKEN_LABELS = {"run-pace": "Zone {} pace", "run-hr": "Zone {} HR", "bike-watts": "Zone {}",
                 "bike-hr": "Zone {} HR", "swim-pace": "Zone {} pace"}


# ── SETTINGS ──────────────────────────────────────────────────────────────────

def normalize_schedule(default_time, slots) -> tuple[str, list[dict]]:
    """Validate the weekly schedule; returns (default_time, sorted slots)."""
    default_time = plan_doc.parse_clock(default_time or DEFAULT_TIME, "default time")
    if slots is None:
        slots = []
    if not isinstance(slots, list):
        raise PlanError("slots must be a list.")
    days = {d.lower(): d for d in DAYS}
    out, seen = [], set()
    for slot in slots:
        if not isinstance(slot, dict):
            raise PlanError("Every schedule row must be an object.")
        day = days.get(str(slot.get("day") or "").strip().lower())
        if day is None:
            raise PlanError(f"Unknown day {slot.get('day')!r}.")
        sport = str(slot.get("sport") or "").strip().lower()
        if sport not in SCHEDULE_SPORTS:
            raise PlanError(f"sport must be one of {', '.join(SCHEDULE_SPORTS)}.")
        if (day, sport) in seen:
            raise PlanError(f"{day} · {sport} is listed twice — keep one row per day and sport.")
        seen.add((day, sport))
        out.append({"day": day, "sport": sport,
                    "time": plan_doc.parse_clock(slot.get("time"), f"time for {day} · {sport}")})
    out.sort(key=lambda s: (DAYS.index(s["day"]), s["time"], s["sport"]))
    return default_time, out


def _view(row: dict | None) -> dict:
    """What Settings shows. The feed path carries the token so Settings can
    offer the link again later (it only ever reads the plan)."""
    row = row or {}
    token = row.get("feed_token")
    created = row.get("token_created_at")
    return {
        "defaultTime": row.get("default_time") or DEFAULT_TIME,
        "slots": list(row.get("slots") or []),
        "published": bool(token),
        "feedPath": f"{FEED_PATH}?key={token}" if token else None,
        "publishedAt": created.isoformat() if isinstance(created, datetime) else created,
    }


def get_settings() -> dict:
    plan_service._require_db()
    return _view(db.get_calendar_settings())


def save_schedule(default_time, slots) -> dict:
    plan_service._require_db()
    default_time, slots = normalize_schedule(default_time, slots)
    return _view(db.save_calendar_schedule(default_time, slots))


def publish() -> dict:
    """Create the feed token — or replace it, which cuts off the old link."""
    plan_service._require_db()
    return _view(db.set_calendar_token(secrets.token_urlsafe(24)))


def revoke() -> dict:
    plan_service._require_db()
    return _view(db.set_calendar_token(None))


def key_is_valid(key: str | None) -> bool:
    """Whether ``key`` is the current feed token (false when unpublished)."""
    plan_service._require_db()
    token = (db.get_calendar_settings() or {}).get("feed_token")
    # Bytes: compare_digest refuses non-ASCII str, and ?key= is user input.
    return bool(token and key) and secrets.compare_digest(str(key).encode(), token.encode())


# ── START TIMES ───────────────────────────────────────────────────────────────

def _minutes(workout: dict) -> float | None:
    value = workout.get("durationMinutes")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _own_start(workout: dict) -> str | None:
    try:
        return plan_doc.parse_clock(workout["startTime"]) if workout.get("startTime") else None
    except PlanError:
        return None  # validate_plan warns about it; the schedule applies instead


def schedule_day(day: date, workouts: list[dict], default_time: str, slots: list[dict]) -> list[tuple]:
    """``[(workout, start, end)]`` for one day, in start order. ``start`` and
    ``end`` are datetimes, or None for an all-day event (no duration). Rest
    days are left out."""
    by_slot = {(s["day"], s["sport"]): s["time"] for s in slots}
    weekday = DAYS[day.weekday()]
    timed, all_day = [], []
    for index, w in enumerate(workouts):
        if w.get("sport") == "rest":
            continue
        minutes = _minutes(w)
        if minutes is None:
            all_day.append((w, None, None))
            continue
        own = _own_start(w)
        clock = own or by_slot.get((weekday, w.get("sport"))) or default_time
        hour, minute = map(int, clock.split(":"))
        timed.append((datetime(day.year, day.month, day.day, hour, minute), index, bool(own), w, minutes))

    out, last_end = [], None
    for start, _, fixed, w, minutes in sorted(timed, key=lambda t: (t[0], t[1])):
        if not fixed and last_end and start < last_end:
            start = last_end            # stack after the earlier session
        end = start + timedelta(minutes=minutes)
        last_end = max(last_end, end) if last_end else end
        out.append((w, start, end))
    return all_day + out


def _schedule() -> tuple[str, list[dict]]:
    settings = db.get_calendar_settings() or {}
    return settings.get("default_time") or DEFAULT_TIME, list(settings.get("slots") or [])


def start_times(plan: dict) -> dict[str, str]:
    """``{workout id: "HH:MM"}`` — each timed workout's start as the feed
    places it, so the viewer can order a day's workouts the same way.
    Workouts without a duration (all-day in the feed) are left out."""
    plan_service._require_db()
    default_time, slots = _schedule()
    out = {}
    for week in plan.get("weeks") or []:
        for day in week.get("days") or []:
            try:
                day_date = plan_doc.parse_date(day.get("date"))
            except PlanError:
                continue
            for w, start, _ in schedule_day(day_date, day.get("workouts") or [], default_time, slots):
                if start is not None and w.get("id"):
                    out[w["id"]] = f"{start:%H:%M}"
    return out


# ── ICS ───────────────────────────────────────────────────────────────────────

def _escape(text) -> str:
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545 line folding: at most 75 octets per line, never splitting a
    UTF-8 character."""
    out, current, size = [], "", 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > 75:
            out.append(current)
            current, size = " ", 1
        current += ch
        size += width
    out.append(current)
    return "\r\n".join(out)


def _fmt_duration(minutes: float) -> str:
    minutes = round(minutes)
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours}h{rest:02d}" if rest else f"{hours}h"


def _fmt_distance(w: dict, imperial: bool) -> str:
    def km(v):
        return f"{v * 0.621371:.1f} mi" if imperial else f"{v:g} km"
    rng = w.get("distanceKmRange")
    if isinstance(rng, dict) and rng.get("low") is not None and rng.get("high") is not None:
        lo, hi = km(rng["low"]).split()[0], km(rng["high"])
        return f"{lo}–{hi}"
    if isinstance(w.get("distanceKm"), (int, float)):
        return km(w["distanceKm"])
    if isinstance(w.get("distanceMeters"), (int, float)):
        return f"{round(w['distanceMeters'] * 1.09361)} yd" if imperial else f"{w['distanceMeters']:g} m"
    return ""


def _details(w: dict, done: bool, imperial: bool) -> str:
    minutes = _minutes(w)
    facts = [str(w.get("sport") or "").capitalize(), w.get("type"),
             _fmt_duration(minutes) if minutes else "", _fmt_distance(w, imperial), w.get("primaryZone")]
    parts = [" · ".join(str(f) for f in facts if f)]
    if w.get("description"):
        parts.append(w["description"])
    if w.get("keyTargets"):
        parts.append(f"Key targets: {w['keyTargets']}")
    if w.get("humanReadable"):
        # Zone tokens become their zone name; the viewer shows the live paces.
        parts.append(_TOKEN_RE.sub(lambda m: _TOKEN_LABELS[m.group(1).lower()].format(m.group(2).lower()),
                                   w["humanReadable"]).strip())
    if done:
        parts.append("✓ Completed")
    return "\n\n".join(p for p in parts if p)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(row: dict | None, completed: set[str], default_time: str, slots: list[dict],
              now: datetime | None = None) -> str:
    """The iCalendar text for a plan row (None → an empty calendar)."""
    now = now or datetime.now(timezone.utc)
    plan = (row or {}).get("plan") or {}
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//garmin-mcp//Training plan//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(plan_doc.plan_title(plan) if row else 'Training plan')}",
        "X-WR-CALDESC:Workouts from the active Claude Coach training plan",
        # Hints only: Apple and Outlook may honour them, Google refreshes on
        # its own schedule regardless.
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    if row:
        imperial = plan.get("unit") == "imperial"
        modified = row.get("updated_at")
        for week in plan.get("weeks") or []:
            for day in week.get("days") or []:
                try:
                    day_date = plan_doc.parse_date(day.get("date"))
                except PlanError:
                    continue
                for w, start, end in schedule_day(day_date, day.get("workouts") or [], default_time, slots):
                    if not w.get("id"):
                        continue
                    done = w["id"] in completed
                    # Plan-scoped: workout ids like w1-mon-swim repeat across plans.
                    event = [
                        "BEGIN:VEVENT",
                        f"UID:{_escape(w['id'])}@{_escape(row['id'])}.garmin-mcp",
                        f"DTSTAMP:{_stamp(now)}",
                        f"SEQUENCE:{int(row.get('version') or 0)}",
                    ]
                    if isinstance(modified, datetime):
                        event.append(f"LAST-MODIFIED:{_stamp(modified)}")
                    if start is None:
                        event += [f"DTSTART;VALUE=DATE:{day_date:%Y%m%d}",
                                  f"DTEND;VALUE=DATE:{day_date + timedelta(days=1):%Y%m%d}",
                                  "TRANSP:TRANSPARENT"]
                    else:
                        event += [f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}"]
                    event += [
                        f"SUMMARY:{_escape(('✓ ' if done else '') + str(w.get('name') or w.get('sport') or 'Workout'))}",
                        f"DESCRIPTION:{_escape(_details(w, done, imperial))}",
                        f"CATEGORIES:{_escape(w.get('sport') or 'other')}",
                        "END:VEVENT",
                    ]
                    lines += event
    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)


def feed() -> str:
    """The feed for whichever plan is active right now."""
    plan_service._require_db()
    default_time, slots = _schedule()
    row = db.get_training_plan(None)
    completed = set(plan_service.completed_map(row["id"])) if row else set()
    return build_ics(row, completed, default_time, slots)
