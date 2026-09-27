# tools/goal_race.py
"""The single goal race: set in Settings, read by Today (countdown card),
Trends (race marker on the fitness chart) and Fitness (highlighted
prediction row). Stored as one JSON value in app_settings under
``goal_race``:

    {"name": str, "date": "YYYY-MM-DD", "distance": one of DISTANCES,
     "target_sec": int | null, "show_on_today": bool}
"""
from datetime import date, timedelta

import db

SETTING_KEY = "goal_race"

# key → (short label, long label, taper days, run-predictor distance or None)
DISTANCES = {
    "5k":       ("5K", "5K", 7, "5K"),
    "10k":      ("10K", "10K", 7, "10K"),
    "half":     ("Half", "Half marathon", 10, "half_marathon"),
    "marathon": ("Marathon", "Marathon", 21, "marathon"),
    "sprint":   ("Sprint", "Sprint triathlon", 7, None),
    "olympic":  ("Olympic", "Olympic triathlon", 10, None),
    "70.3":     ("70.3", "Middle distance (70.3)", 14, None),
    "140.6":    ("140.6", "Full distance (140.6)", 21, None),
}
MAX_NAME = 120
# Race-day form is only worth showing on Today within this many days.
FORM_WINDOW_DAYS = 42


class GoalRaceError(ValueError):
    """An invalid goal race; the message is user-facing."""


def parse_hms(value) -> int | None:
    """"1:32:00" / "45:10" → seconds; blank → None."""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise GoalRaceError("Target time must be h:mm:ss (or mm:ss).")
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    if secs <= 0:
        raise GoalRaceError("Target time must be more than zero.")
    return secs


def fmt_hms(secs) -> str | None:
    if secs is None:
        return None
    secs = int(round(secs))
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def validate(data: dict) -> dict:
    """A clean goal race from form/JSON input, or GoalRaceError."""
    if not isinstance(data, dict):
        raise GoalRaceError("Goal race must be an object.")
    name = str(data.get("name") or "").strip()
    if not name:
        raise GoalRaceError("Race name is required.")
    if len(name) > MAX_NAME:
        raise GoalRaceError(f"Race name must be at most {MAX_NAME} characters.")
    try:
        race_date = date.fromisoformat(str(data.get("date") or "")[:10])
    except ValueError:
        raise GoalRaceError("Date must be YYYY-MM-DD.") from None
    distance = str(data.get("distance") or "")
    if distance not in DISTANCES:
        raise GoalRaceError("Distance must be one of: " + ", ".join(v[0] for v in DISTANCES.values()) + ".")
    target = data.get("target_sec")
    if target in (None, ""):
        target = parse_hms(data.get("target"))
    else:
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise GoalRaceError("Target time must be a number of seconds.") from None
        if target <= 0:
            raise GoalRaceError("Target time must be more than zero.")
    show = data.get("show_on_today", True)
    if isinstance(show, str):
        show = show.lower() in ("1", "true", "on", "yes")
    return {"name": name, "date": race_date.isoformat(), "distance": distance,
            "target_sec": target, "show_on_today": bool(show)}


def get_goal_race() -> dict | None:
    """The stored goal race, or None when unset (or without a database)."""
    if not db.is_configured():
        return None
    value = db.get_setting(SETTING_KEY)
    try:
        return validate(value) if value else None
    except GoalRaceError:
        return None


def save_goal_race(data: dict) -> dict:
    goal = validate(data)
    db.set_setting(SETTING_KEY, goal)
    return goal


def clear_goal_race() -> None:
    db.set_setting(SETTING_KEY, None)


def describe(goal: dict, today: date) -> dict:
    """What every screen shows about the race on ``today``: days left, taper
    window, phase (build / taper / race_day / done) and whether race-day
    form is worth showing yet."""
    race = date.fromisoformat(goal["date"])
    short, long_label, taper_days, predictor = DISTANCES[goal["distance"]]
    days = (race - today).days
    taper_start = race - timedelta(days=taper_days)
    if days < 0:
        phase = "done"
    elif days == 0:
        phase = "race_day"
    elif today >= taper_start:
        phase = "taper"
    else:
        phase = "build"
    return {
        **goal,
        "distance_label": short,
        "distance_long": long_label,
        "is_triathlon": predictor is None,
        "predictor_distance": predictor,
        "target": fmt_hms(goal.get("target_sec")),
        "days_left": days,
        "taper_days": taper_days,
        "taper_start": taper_start.isoformat(),
        "days_to_taper": (taper_start - today).days,
        "phase": phase,
        "show_form": 0 <= days <= FORM_WINDOW_DAYS,
    }
