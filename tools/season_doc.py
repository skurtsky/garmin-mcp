# tools/season_doc.py
"""The season data model: a season owns plans, races, goals and objectives.

Pure validation — no database. tools/season_service.py stores what this
module accepts. Everything here raises ``SeasonError`` (its message is safe to
show to the user) for input that can't be stored, and returns *warnings* for
what is merely unwise: Friel's limits (3 A races, 5 B races, 3 goals,
4 objectives) and dates that fall outside the season are soft — never blocks.

Shapes (camelCase, as the API, the MCP tools and the pages speak them):

    season     {id, name, startDate, endDate, status, goals, objectives, raceIds, planIds}
    race       {id, seasonId, name, date, priority A|B|C, type, goal, location, url, notes}
    goal       {id, text, targetDate?, raceId?}
    objective  {id, text, targetDate?, raceId?, status not_started|on_track|met}

A plan belongs to at most one season; that link lives beside the plan
(``seasonId``), not in the plan JSON.
"""
import uuid
from datetime import date

from tools import goal_race

STATUSES = ("active", "archived")
PRIORITIES = ("A", "B", "C")
OBJECTIVE_STATUSES = ("not_started", "on_track", "met")
OBJECTIVE_STATUS_LABELS = {"not_started": "Not started", "on_track": "On track", "met": "Met"}

# Friel's limits. Soft: they produce warnings, never errors.
MAX_A_RACES, MAX_B_RACES, MAX_GOALS, MAX_OBJECTIVES = 3, 5, 3, 4

# Race types share their keys with the goal race's distances, so a season race
# can become the goal race as it is; "ride" and "other" have no distance.
RACE_TYPES = {
    "sprint": "Sprint triathlon", "olympic": "Olympic triathlon", "70.3": "Triathlon 70.3",
    "140.6": "Triathlon 140.6", "5k": "Run 5K", "10k": "Run 10K", "half": "Run half marathon",
    "marathon": "Run marathon", "ride": "Ride", "other": "Other",
}
assert all(k in RACE_TYPES for k in goal_race.DISTANCES)

MAX_NAME, MAX_TEXT, MAX_NOTES = 120, 300, 2000


class SeasonError(ValueError):
    """An invalid season, race, goal or objective; the message is user-facing."""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def parse_date(value, label: str) -> date:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        raise SeasonError(f"{label} must be a date like 2026-09-14.") from None


def _text(value, label: str, limit: int, required: bool = False) -> str:
    text = str(value if value is not None else "").strip()
    if required and not text:
        raise SeasonError(f"{label} is required.")
    if len(text) > limit:
        raise SeasonError(f"{label} must be at most {limit} characters.")
    return text


def _choice(value, label: str, allowed, default=None) -> str:
    if value in (None, ""):
        if default is None:
            raise SeasonError(f"{label} is required.")
        return default
    if str(value) not in allowed:
        raise SeasonError(f"{label} must be one of: {', '.join(allowed)}.")
    return str(value)


# ── SEASON ────────────────────────────────────────────────────────────────────

def season_fields(data: dict, current: dict | None = None) -> dict:
    """The name, dates and status of a season: ``data`` laid over ``current``
    (an update) or on its own (a create). Start must not come after end."""
    if not isinstance(data, dict):
        raise SeasonError("A season must be an object.")
    base = current or {}
    name = _text(data.get("name", base.get("name")), "Season name", MAX_NAME, required=True)
    start = parse_date(data.get("startDate", base.get("startDate")), "Start date")
    end = parse_date(data.get("endDate", base.get("endDate")), "End date")
    if end < start:
        raise SeasonError(f"The season ends ({end}) before it starts ({start}).")
    status = _choice(data.get("status", base.get("status")), "Status", STATUSES, default="active")
    return {"name": name, "startDate": start.isoformat(), "endDate": end.isoformat(), "status": status}


# ── RACES ─────────────────────────────────────────────────────────────────────

def race_fields(data: dict, current: dict | None = None) -> dict:
    """A race's own fields (no id, no season): ``data`` laid over ``current``."""
    if not isinstance(data, dict):
        raise SeasonError("A race must be an object.")
    base = current or {}

    def pick(key):
        return data[key] if key in data else base.get(key)

    url = _text(pick("url"), "URL", 500)
    if url and not url.lower().startswith(("http://", "https://")):
        raise SeasonError("URL must start with http:// or https://.")
    return {
        "name": _text(pick("name"), "Race name", MAX_NAME, required=True),
        "date": parse_date(pick("date"), "Race date").isoformat(),
        "priority": _choice(str(pick("priority") or "").upper() or None, "Priority", PRIORITIES),
        "type": _choice(pick("type"), "Race type", tuple(RACE_TYPES), default="other"),
        "goal": _text(pick("goal"), "Race goal", MAX_TEXT),
        "location": _text(pick("location"), "Location", MAX_NAME),
        "url": url,
        "notes": _text(pick("notes"), "Notes", MAX_NOTES),
    }


# ── GOALS AND OBJECTIVES ──────────────────────────────────────────────────────

def _link_fields(item: dict, race_ids) -> dict:
    out = {}
    if item.get("targetDate") not in (None, ""):
        out["targetDate"] = parse_date(item["targetDate"], "Target date").isoformat()
    if item.get("raceId") not in (None, ""):
        race_id = str(item["raceId"])
        if race_ids is not None and race_id not in race_ids:
            raise SeasonError(f"Race {race_id!r} isn't in this season.")
        out["raceId"] = race_id
    return out


def _entries(items, label: str, race_ids, extra) -> list[dict]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise SeasonError(f"{label} must be a list.")
    out = []
    for item in items:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise SeasonError(f"Each {label[:-1]} must be text or an object.")
        text = _text(item.get("text"), f"{label[:-1].capitalize()} text", MAX_TEXT)
        if not text:
            continue   # a blank row in a form is "nothing here", not an error
        entry = {"id": str(item.get("id") or new_id(label[0])), "text": text,
                 **_link_fields(item, race_ids), **extra(item)}
        out.append(entry)
    return out


def goals_list(items, race_ids=None) -> list[dict]:
    return _entries(items, "goals", race_ids, lambda item: {})


def objectives_list(items, race_ids=None) -> list[dict]:
    return _entries(items, "objectives", race_ids, lambda item: {
        "status": _choice(item.get("status"), "Objective status", OBJECTIVE_STATUSES, default="not_started")})


# ── WARNINGS ──────────────────────────────────────────────────────────────────

def _in_season(day: str | None, season: dict) -> bool:
    return bool(day) and season["startDate"] <= str(day)[:10] <= season["endDate"]


def plan_warning(season: dict, plan: dict) -> str | None:
    """Why a plan doesn't sit inside the season's dates, or None when it does.
    ``plan`` needs ``event``, ``start`` and ``end`` (ISO dates, possibly empty)."""
    start, end = plan.get("start"), plan.get("end")
    if not start or not end:
        return None
    if _in_season(start, season) and _in_season(end, season):
        return None
    return (f"Plan “{plan.get('event') or plan.get('id')}” runs {start} – {end}, outside the season "
            f"({season['startDate']} – {season['endDate']}).")


def season_warnings(season: dict, races: list[dict], plans: list[dict]) -> list[str]:
    """Every soft problem with a season: Friel's limits and dates outside the
    season. ``season`` carries goals/objectives; ``races`` and ``plans`` are the
    ones it owns. Warnings only — nothing here blocks a save."""
    out = []
    counts = {p: sum(1 for r in races if r["priority"] == p) for p in PRIORITIES}
    if counts["A"] > MAX_A_RACES:
        out.append(f"{counts['A']} A races; Friel suggests at most {MAX_A_RACES}.")
    if counts["B"] > MAX_B_RACES:
        out.append(f"{counts['B']} B races; Friel suggests at most {MAX_B_RACES}.")
    if len(season.get("goals") or []) > MAX_GOALS:
        out.append(f"{len(season['goals'])} goals; Friel suggests at most {MAX_GOALS}.")
    if len(season.get("objectives") or []) > MAX_OBJECTIVES:
        out.append(f"{len(season['objectives'])} objectives; Friel suggests at most {MAX_OBJECTIVES}.")
    for race in sorted(races, key=lambda r: r["date"]):
        if not _in_season(race["date"], season):
            out.append(f"Race “{race['name']}” on {race['date']} is outside the season "
                       f"({season['startDate']} – {season['endDate']}).")
    for plan in plans:
        if (text := plan_warning(season, plan)):
            out.append(text)
    return out


def derived_goal_race(race: dict | None) -> dict | None:
    """A season race as the app-wide goal race (tools/goal_race.py), or None
    when its type has no goal-race distance (ride, other)."""
    if not race or race["type"] not in goal_race.DISTANCES:
        return None
    try:
        target = goal_race.parse_hms(race.get("goal"))
    except goal_race.GoalRaceError:
        target = None   # the race goal is free text ("sub 5:30 if the swim is wetsuit-legal")
    return {"name": race["name"], "date": race["date"], "distance": race["type"],
            "target_sec": target, "show_on_today": True}
