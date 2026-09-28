# tools/training_load.py
"""Training load for the Activity and Trends tabs: Garmin's training-benefit
buckets, the planned-load estimate for sessions not done yet, and fitness /
fatigue / form.

Pure functions (no I/O) — tools/planned_sessions.py and the dashboard feed
them from PostgreSQL.

Fitness and fatigue are exponentially weighted averages of daily load — the
classic impulse-response model: fitness has a 42-day time constant, fatigue a
7-day one, and form is fitness minus fatigue. Each day moves the average 1/42
(or 1/7) of the way toward that day's load. Loads are Garmin's own
per-activity training load.
"""
import re
import statistics
from datetime import date, timedelta

FITNESS_DAYS = 42
FATIGUE_DAYS = 7

# Form zones, highest first: (lower bound — form above it, key, label, colour).
FORM_ZONES = (
    (5, "fresh", "Fresh", "#4aa7d8"),
    (-10, "neutral", "Neutral", "#9397ab"),
    (-30, "productive", "Productive", "#4fae72"),
    (float("-inf"), "high_risk", "High risk", "#cf8a80"),
)
RACE_FORM_TARGET = (5, 15)

# Garmin's primary training benefit (trainingEffectLabel) → (label, colour).
BENEFITS = {
    "RECOVERY":           ("Recovery", "#7f9bb3"),
    "AEROBIC_BASE":       ("Base", "#4aa7d8"),
    "TEMPO":              ("Tempo", "#4fae72"),
    "LACTATE_THRESHOLD":  ("Threshold", "#d9a441"),
    "VO2MAX":             ("VO2 Max", "#e2734a"),
    "ANAEROBIC_CAPACITY": ("Anaerobic", "#a07fe0"),
    "SPEED":              ("Sprint", "#d9689a"),
}
_BENEFIT_ALIASES = {"SPRINT": "SPEED", "BASE": "AEROBIC_BASE", "THRESHOLD": "LACTATE_THRESHOLD",
                    "ANAEROBIC": "ANAEROBIC_CAPACITY", "VO2_MAX": "VO2MAX"}
_NO_BENEFIT = {"", "NONE", "NO_BENEFIT", "UNKNOWN"}
NEUTRAL_BENEFIT_COLOR = "#75798c"

# A planned session's benefit, from the coach's free-text trainingEffect
# ("Aerobic base", "Threshold") — keyword → Garmin bucket, first match wins.
_BENEFIT_KEYWORDS = (
    ("recover", "RECOVERY"), ("sprint", "SPEED"), ("speed", "SPEED"),
    ("anaerobic", "ANAEROBIC_CAPACITY"), ("vo2", "VO2MAX"),
    ("threshold", "LACTATE_THRESHOLD"), ("tempo", "TEMPO"),
    ("base", "AEROBIC_BASE"), ("aerobic", "AEROBIC_BASE"), ("endurance", "AEROBIC_BASE"),
)
# … or from its primary zone (the plan's Friel zones).
_ZONE_BENEFIT = {
    "1": "RECOVERY", "2": "AEROBIC_BASE", "3": "TEMPO", "4": "LACTATE_THRESHOLD",
    "5": "VO2MAX", "5a": "LACTATE_THRESHOLD", "5b": "VO2MAX", "5c": "ANAEROBIC_CAPACITY",
}
_ZONE_RE = re.compile(r"(?:zone|z)\s*(\d)([abc])?(?:\s*[-–/]\s*(?:z(?:one)?\s*)?(\d)([abc])?)?", re.IGNORECASE)


def benefit_key(label) -> str | None:
    """Garmin's trainingEffectLabel normalised to a BENEFITS key; unknown
    labels pass through upper-cased, "no benefit" is None."""
    key = str(label or "").strip().upper().replace(" ", "_")
    if key in _NO_BENEFIT:
        return None
    return _BENEFIT_ALIASES.get(key, key)


def benefit_info(key: str | None) -> dict | None:
    """{key, label, color} for a benefit; an unrecognised Garmin label keeps
    its own (humanised) name in the neutral colour."""
    if not key:
        return None
    label, color = BENEFITS.get(key, (key.replace("_", " ").title(), NEUTRAL_BENEFIT_COLOR))
    return {"key": key, "label": label, "color": color}


def parse_zone(primary_zone) -> str | None:
    """The top zone a planned workout's ``primaryZone`` names ("Zone 2" → "2",
    "Zone 1-2" → "2", "Z5b" → "5b"), or None."""
    m = _ZONE_RE.search(str(primary_zone or ""))
    if not m:
        return None
    lo = m.group(1) + (m.group(2) or "").lower()
    hi = (m.group(3) + (m.group(4) or "").lower()) if m.group(3) else None
    return max(lo, hi) if hi else lo


def planned_benefit(workout: dict) -> str | None:
    """A planned workout's benefit bucket: the coach's trainingEffect when it
    names one, else its primary zone."""
    text = str(workout.get("trainingEffect") or "").lower()
    for keyword, key in _BENEFIT_KEYWORDS:
        if keyword in text:
            return key
    return _ZONE_BENEFIT.get(parse_zone(workout.get("primaryZone")) or "")


# ── PLANNED LOAD ─────────────────────────────────────────────────────────────
# Planned workouts carry no load, so it's estimated as duration × load per
# minute, learned from the athlete's own history, most specific first:
#   1. completed plan workouts of the same sport and zone (their matched
#      activity's load / duration);
#   2. any recent activity of the same sport;
#   3. a default by zone.
MIN_SAMPLES = 3
# Load per minute by zone when there's no history to learn from — roughly
# Garmin's EPOC load for an hour in that zone, divided by 60.
DEFAULT_LOAD_PER_MIN = {"1": 0.5, "2": 0.9, "3": 1.5, "4": 2.0, "5": 2.6}
DEFAULT_LOAD_PER_MIN_SPORT = {"strength": 0.6, "other": 0.8}
DEFAULT_LOAD_PER_MIN_ANY = 1.0


def _zone_digit(zone: str | None) -> str | None:
    return zone[0] if zone else None


def learn_load_rates(matched: list[tuple[str, str | None, float, float]],
                     recent: list[tuple[str, float, float]]) -> dict:
    """Median load-per-minute tables.

    ``matched``: (sport, primaryZone, activity load, activity minutes) for
    completed plan workouts; ``recent``: (sport, load, minutes) for recent
    activities. Returns {"zone": {(sport, zone digit): rate}, "sport": {sport: rate}}."""
    by_zone: dict[tuple, list[float]] = {}
    for sport, zone_text, load, minutes in matched:
        if load and minutes and minutes > 0:
            by_zone.setdefault((sport, _zone_digit(parse_zone(zone_text))), []).append(load / minutes)
    by_sport: dict[str, list[float]] = {}
    for sport, load, minutes in recent:
        if load and minutes and minutes > 0:
            by_sport.setdefault(sport, []).append(load / minutes)
    return {
        "zone": {k: statistics.median(v) for k, v in by_zone.items() if len(v) >= MIN_SAMPLES},
        "sport": {k: statistics.median(v) for k, v in by_sport.items() if len(v) >= MIN_SAMPLES},
    }


def estimate_planned_load(workout: dict, rates: dict | None = None) -> int | None:
    """Estimated Garmin load for a planned workout, or None when it has no
    duration (rest days, open sessions)."""
    minutes = workout.get("durationMinutes")
    if not minutes:
        return None
    rates = rates or {}
    sport = workout.get("sport") or "other"
    zone = _zone_digit(parse_zone(workout.get("primaryZone")))
    rate = ((rates.get("zone") or {}).get((sport, zone))
            or (rates.get("sport") or {}).get(sport)
            or DEFAULT_LOAD_PER_MIN.get(zone or "")
            or DEFAULT_LOAD_PER_MIN_SPORT.get(sport)
            or DEFAULT_LOAD_PER_MIN_ANY)
    return round(float(minutes) * rate)


# ── FITNESS / FATIGUE / FORM ─────────────────────────────────────────────────

def form_zone(form: float) -> dict:
    for lower, key, label, color in FORM_ZONES:
        if form > lower:
            return {"key": key, "label": label, "color": color}
    lower, key, label, color = FORM_ZONES[-1]
    return {"key": key, "label": label, "color": color}


def fitness_series(daily_loads: dict[str, float], start: date, end: date, today: date,
                   planned_loads: dict[str, float] | None = None,
                   seed_days: int = FITNESS_DAYS) -> list[dict]:
    """Fitness, fatigue and form per day from ``start`` through ``end``.

    ``daily_loads`` holds actual load per local date (missing days are rest,
    load 0) and must reach back far enough to warm the averages up — at
    least 90 days before the first day shown. Days after ``today`` use
    ``planned_loads`` and are marked ``projected``; the projection stops at
    the last planned day, so ``end`` beyond it is trimmed.

    Both averages start at the mean load of the first ``seed_days`` days,
    rather than zero, so a short history doesn't read as "no fitness".
    """
    planned_loads = planned_loads or {}
    last_planned = max((date.fromisoformat(d) for d, v in planned_loads.items() if v is not None), default=None)
    if last_planned and last_planned > today:
        end = min(end, last_planned)
    else:
        end = min(end, today)
    first = min((date.fromisoformat(d) for d in daily_loads), default=start)
    first = min(first, start)

    days = (end - first).days + 1
    loads = []
    for i in range(days):
        d = (first + timedelta(days=i)).isoformat()
        projected = d > today.isoformat()
        load = (planned_loads.get(d) or 0) if projected else daily_loads.get(d, 0)
        loads.append((d, float(load or 0), projected))

    seed = [l for _, l, p in loads[:seed_days] if not p]
    ctl = atl = (sum(seed) / len(seed)) if seed else 0.0
    out = []
    for d, load, projected in loads:
        ctl += (load - ctl) / FITNESS_DAYS
        atl += (load - atl) / FATIGUE_DAYS
        if d < start.isoformat():
            continue
        form = round(ctl - atl)
        out.append({"date": d, "load": round(load), "fitness": round(ctl, 1), "fatigue": round(atl, 1),
                    "form": form, "zone": form_zone(form)["key"], "projected": projected})
    return out


def race_day_form(series: list[dict], race_date: str) -> dict | None:
    """Projected form on race day and whether it's inside the target, or
    None when the series (actual + projection) doesn't reach the race."""
    day = next((p for p in series if p["date"] == race_date), None)
    if day is None:
        return None
    lo, hi = RACE_FORM_TARGET
    return {"form": day["form"], "on_target": lo <= day["form"] <= hi,
            "too_fresh": day["form"] > hi, "zone": form_zone(day["form"])}
