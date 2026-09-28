# tools/planned_sessions.py
"""The active training plan's sessions over a date range, as the Activity
and Trends tabs use them: each with its estimated load, its Garmin-bucket
training benefit and whether it's done, still to come, or missed.

The plan (Claude Coach, in PostgreSQL) is the source — not the Garmin
calendar. Which activity completed which workout is already recorded by the
sync job's plan matching (training_plan_workout_state.activity_id), so a
matched activity's "✓ workout name" tag comes straight from there.
"""
from datetime import date, timedelta

import db
from tools import plan_doc, training_load
from tools.plan_service import sport_for_activity_type

# How far back activities are read to learn load-per-minute rates.
RATE_LOOKBACK_DAYS = 120


def _load_of(activity: dict) -> float | None:
    summary = activity.get("summary") or {}
    load = activity.get("training_load") if activity.get("training_load") is not None else summary.get("training_load")
    return float(load) if load is not None else None


def learn_rates(plan: dict, states: dict, today: date) -> dict:
    """Load-per-minute tables (training_load.learn_load_rates) from this
    plan's completed workouts and the last RATE_LOOKBACK_DAYS of activities."""
    since = today - timedelta(days=RATE_LOOKBACK_DAYS)
    workouts = {w.get("id"): w for _, _, w in plan_doc.iter_workouts(plan)}
    linked = {wid: s["activity_id"] for wid, s in states.items()
              if s.get("completed") and s.get("activity_id") and wid in workouts}
    acts = db.get_activities_by_ids(list(linked.values()))
    matched = []
    for wid, aid in linked.items():
        act = acts.get(aid)
        if act:
            w = workouts[wid]
            matched.append((w.get("sport") or "other", w.get("primaryZone"), _load_of(act), act.get("duration_min")))
    recent = []
    for act in db.get_activities_in_range(since.isoformat(), (today + timedelta(days=1)).isoformat()):
        sport = sport_for_activity_type(act.get("activity_type")) or "other"
        recent.append((sport, _load_of(act), act.get("duration_min")))
    return training_load.learn_load_rates(matched, recent)


def sessions_from_plan(plan: dict, states: dict, start: date, end: date, today: date,
                       rates: dict | None = None) -> list[dict]:
    """Planned sessions from ``start`` through ``end`` (rest days left out),
    in date order. ``status``: "done" (ticked — ``activity_id`` is the
    activity that did it, when matched), "planned" (today or later, not
    done) or "missed" (an earlier day, not done)."""
    out = []
    for _, day, w in plan_doc.iter_workouts(plan):
        d = str(day.get("date"))[:10]
        if w.get("sport") == "rest" or not (start.isoformat() <= d <= end.isoformat()):
            continue
        state = states.get(w.get("id")) or {}
        if state.get("completed"):
            status = "done"
        else:
            status = "planned" if d >= today.isoformat() else "missed"
        out.append({
            "workout_id": w.get("id"),
            "date": d,
            "sport": w.get("sport") or "other",
            "name": w.get("name"),
            "type": w.get("type"),
            "duration_min": w.get("durationMinutes"),
            "distance_km": w.get("distanceKm") or (round(w["distanceMeters"] / 1000, 2) if w.get("distanceMeters") else None),
            "primary_zone": w.get("primaryZone"),
            "is_test": plan_doc.is_test_workout(w),
            "est_load": training_load.estimate_planned_load(w, rates),
            "benefit": training_load.planned_benefit(w),
            "status": status,
            "activity_id": state.get("activity_id") if status == "done" else None,
        })
    out.sort(key=lambda s: s["date"])
    return out


def planned_sessions(start: date, end: date, today: date) -> list[dict]:
    """The active plan's sessions in a range; [] without a database or plan."""
    if not db.is_configured():
        return []
    row = db.get_training_plan(None)
    if row is None:
        return []
    states = db.get_workout_states(row["id"])
    rates = learn_rates(row["plan"], states, today)
    return sessions_from_plan(row["plan"], states, start, end, today, rates)


def planned_load_by_day(sessions: list[dict], statuses=("done", "planned", "missed")) -> dict[str, int]:
    """{date: summed estimated load} over sessions with the given statuses."""
    out: dict[str, int] = {}
    for s in sessions:
        if s["status"] in statuses and s["est_load"]:
            out[s["date"]] = out.get(s["date"], 0) + s["est_load"]
    return out


def matched_workouts(sessions: list[dict]) -> dict[int, dict]:
    """{activity_id: session} for the done sessions linked to an activity —
    the Activity tab's "✓ workout name" tags."""
    return {s["activity_id"]: s for s in sessions if s["status"] == "done" and s["activity_id"]}


# Days of actual load read before the first day shown, so the 42-day average
# has settled by then (seeded from the first 42 of them).
FITNESS_WARMUP_DAYS = 90 + training_load.FITNESS_DAYS
# How far ahead the projection may run — it stops earlier at the last
# planned session.
PROJECTION_MAX_DAYS = 120


def fitness_with_projection(today: date, days_shown: int) -> list[dict]:
    """Fitness / fatigue / form for the last ``days_shown`` days up to today,
    then projected forward over the plan's upcoming sessions (their
    estimated load; missed and done sessions don't project)."""
    start = today - timedelta(days=days_shown - 1)
    loads = db.get_daily_activity_loads((start - timedelta(days=FITNESS_WARMUP_DAYS)).isoformat(), today.isoformat())
    upcoming = planned_sessions(today + timedelta(days=1), today + timedelta(days=PROJECTION_MAX_DAYS), today)
    planned = planned_load_by_day(upcoming, statuses=("planned",))
    return training_load.fitness_series(loads, start, today + timedelta(days=PROJECTION_MAX_DAYS), today, planned)
