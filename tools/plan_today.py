# tools/plan_today.py
"""The training plan as the dashboard shows it: the Today screen's session,
tomorrow and this-week cards, the plan header's week/phase pill, the FTP
test prompt, and the Fitness page's thresholds and plan zones.

A read model only — plans are stored and edited by tools/plan_service.py.
Everything comes from PostgreSQL (the active plan, its workout state and
the synced activities that completed its workouts); ``build_plan_context``
returns None when there's no database or no active plan, and the dashboard
then simply leaves the plan cards out.
"""
import logging
from datetime import date, timedelta

import db
from tools import plan_doc
from tools.plan_service import activity_day

logger = logging.getLogger(__name__)

# The viewer's phase colours (tools/assets/plan-viewer.html); anything else
# (e.g. "Field Testing") gets the neutral dot.
PHASE_COLORS = {
    "Prep": "#6c93b0", "Base 1": "#5ea0b8", "Base 2": "#5eb0a3", "Base 3": "#7fb87a", "Base": "#5eb0a3",
    "Build 1": "#b8b05e", "Build 2": "#d9a05a", "Build": "#b8b05e", "Peak": "#d97a5e",
    "Taper": "#9184d9", "Race": "#d9506a",
}
DEFAULT_PHASE_COLOR = "#7c8194"

SPORT_COLORS = {
    "swim": "#5eb8c9", "bike": "#7fb87a", "run": "#d99a5e", "strength": "#9184d9",
    "brick": "#c97fa0", "race": "#d9b35a", "rest": "#7c8194", "other": "#7c8194",
}

# An FTP test is scored as 95% of the best 20-minute power.
FTP_FROM_20MIN = 0.95
# How long after a test the Fitness page keeps offering to use it.
TEST_PROMPT_DAYS = 7

_WEEKDAY_LETTERS = "MTWTFSS"


def _day(d) -> str:
    return str(d.get("date"))[:10]


def _phase_for_week(plan: dict, week: dict | None) -> str | None:
    if not week:
        return None
    n = week.get("weekNumber")
    for p in plan.get("phases") or []:
        if isinstance(n, int) and p.get("startWeek", 0) <= n <= p.get("endWeek", 0):
            return p.get("name")
    return week.get("phase")


def best_rolling_power(series: list[dict], window_sec: int = 1200) -> int | None:
    """Best average power over any ``window_sec`` stretch of an activity's
    power samples (``{t_offset_sec, value}``, as stored in activity_details).
    Each sample holds until the next one, with gaps over 5s (pauses) not
    counted. None when the ride is shorter than the window."""
    pts = [(p["t_offset_sec"], p["value"] or 0) for p in series or [] if p.get("t_offset_sec") is not None]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < window_sec:
        return None
    # (duration, energy) per sample, then a sliding window over time.
    spans = [(min(pts[i + 1][0] - pts[i][0], 5.0), pts[i][1]) for i in range(len(pts) - 1)]
    best, lo, dur, energy = None, 0, 0.0, 0.0
    for dt, watts in spans:
        dur += dt
        energy += dt * watts
        while dur - spans[lo][0] >= window_sec:
            dur -= spans[lo][0]
            energy -= spans[lo][0] * spans[lo][1]
            lo += 1
        if dur >= window_sec:
            best = max(best or 0, energy / dur)
    return round(best) if best is not None else None


def _best_20min(activity: dict) -> int | None:
    summary = activity.get("summary") or {}
    if summary.get("max_20min_power"):
        return int(summary["max_20min_power"])
    try:
        row = db.get_activity_detail_from_db(activity["garmin_id"])
    except Exception:  # noqa: BLE001 — detail is optional
        logger.warning("Activity detail unavailable for %s", activity.get("garmin_id"), exc_info=True)
        return None
    return best_rolling_power(((row or {}).get("detail") or {}).get("power_series") or [])


def _activity_view(act: dict | None) -> dict | None:
    if not act:
        return None
    return {
        "id": act["garmin_id"], "name": act.get("name"), "type": act.get("activity_type"),
        "distance_km": act.get("distance_km"), "duration_min": act.get("duration_min"),
        "date": activity_day(act),
    }


def _workout_view(w: dict, d: dict, state: dict | None, activities: dict) -> dict:
    state = state or {}
    return {
        "id": w.get("id"), "date": _day(d), "name": w.get("name"), "sport": w.get("sport") or "other",
        "type": w.get("type"), "durationMinutes": w.get("durationMinutes"),
        "distanceKm": w.get("distanceKm"), "distanceMeters": w.get("distanceMeters"),
        "primaryZone": w.get("primaryZone"), "completed": bool(state.get("completed")),
        "is_test": plan_doc.is_test_workout(w),
        "activity": _activity_view(activities.get(state.get("activity_id"))) if state.get("completed") else None,
    }


def _week_strip(plan_week: dict | None, monday: date, today: date, views_by_day: dict) -> list[dict]:
    """M–S: done (filled tick), planned (open circle) or rest (moon), in the
    first session's sport colour."""
    strip = []
    for i in range(7):
        day = (monday + timedelta(days=i)).isoformat()
        views = [v for v in views_by_day.get(day, []) if v["sport"] != "rest"]
        if not views:
            state, color = "rest", None
        else:
            state = "done" if all(v["completed"] for v in views) else "planned"
            color = SPORT_COLORS.get(views[0]["sport"], SPORT_COLORS["other"])
        strip.append({"date": day, "letter": _WEEKDAY_LETTERS[i], "is_today": day == today.isoformat(),
                      "state": state, "color": color})
    return strip


def _next_test(views: list[dict], sport: str, today: date) -> dict | None:
    """Today's test of this sport (done or not), else the next one to come."""
    upcoming = [v for v in views if v["is_test"] and v["sport"] == sport
                and (v["date"] == today.isoformat() or (not v["completed"] and v["date"] > today.isoformat()))]
    return min(upcoming, key=lambda v: v["date"]) if upcoming else None


def _garmin_pace(decimal_minutes) -> str | None:
    """Garmin's LT pace (decimal minutes per km) as m:ss."""
    if not decimal_minutes:
        return None
    return plan_doc.fmt_mmss(float(decimal_minutes) * 60)


def _thresholds(plan: dict, views: list[dict], athlete: dict, today: date) -> list[dict]:
    """The Fitness page's Garmin-vs-plan rows."""
    t = plan_doc.effective_thresholds(plan)
    athlete = athlete or {}
    pace = plan_doc.single_pace_seconds(t.get("thresholdPace"))
    css = plan_doc.single_pace_seconds(t.get("css"))
    rows = [
        ("ftp", "FTP", "bike", "", f"{athlete['ftp']} W" if athlete.get("ftp") else None,
         f"{t['ftp']} W" if t.get("ftp") else None),
        ("bikeLthr", "Bike LTHR", None, "", None, str(t["bikeLthr"]) if t.get("bikeLthr") else None),
        ("runLthr", "Run LTHR", "run", "",
         str(athlete["lactate_threshold_hr"]) if athlete.get("lactate_threshold_hr") else None,
         str(t["runLthr"]) if t.get("runLthr") else None),
        ("thresholdPace", "Threshold pace", "run", "/km", _garmin_pace(athlete.get("lactate_threshold_pace")),
         plan_doc.fmt_mmss(pace) if pace is not None else None),
        ("css", "Swim CSS", "swim", "/100m", None, plan_doc.fmt_mmss(css) if css is not None else None),
    ]
    out = []
    for key, label, test_sport, unit, garmin, planned in rows:
        status, note = plan_doc.threshold_status(plan, key)
        test = _next_test(views, test_sport, today) if test_sport else None
        if test and test["date"] == today.isoformat():
            when = "test today"
        elif test:
            when = "test " + date.fromisoformat(test["date"]).strftime("%a %b %-d")
        else:
            when = None
        sub = " · ".join(p for p in (unit, when or note) if p)
        out.append({"key": key, "label": label, "garmin": garmin, "plan": planned,
                    "status": status, "sub": sub})
    return out


def _ftp_prompt(plan: dict, views: list[dict], activities: dict, today: date) -> dict | None:
    """The most recent completed bike test in the last week that has power:
    its best 20 minutes, the FTP it implies and the plan's current FTP."""
    since = (today - timedelta(days=TEST_PROMPT_DAYS)).isoformat()
    tests = [v for v in views if v["is_test"] and v["sport"] == "bike" and v["completed"] and v["activity"]
             and since <= v["date"] <= today.isoformat()]
    for v in sorted(tests, key=lambda v: v["date"], reverse=True):
        best = _best_20min(activities[v["activity"]["id"]])
        if not best:
            continue
        current = plan_doc.effective_thresholds(plan).get("ftp")
        estimate = round(best * FTP_FROM_20MIN)
        return {"workout_id": v["id"], "date": v["date"], "is_today": v["date"] == today.isoformat(),
                "activity": v["activity"], "best_20min": best, "estimate": estimate,
                "current": current, "applied": current == estimate}
    return None


def build_plan_context(today: date, athlete: dict | None = None) -> dict | None:
    """Everything the dashboard shows from the active plan on ``today``, or
    None without a database or an active plan."""
    if not db.is_configured():
        return None
    row = db.get_training_plan(None)
    if row is None:
        return None
    plan = row["plan"]
    states = db.get_workout_states(row["id"])
    linked_ids = [s["activity_id"] for s in states.values() if s.get("completed") and s.get("activity_id")]
    activities = db.get_activities_by_ids(linked_ids)

    views, views_by_day = [], {}
    for _, d, w in plan_doc.iter_workouts(plan):
        v = _workout_view(w, d, states.get(w.get("id")), activities)
        views.append(v)
        views_by_day.setdefault(v["date"], []).append(v)

    week = plan_doc.week_for_date(plan, today)
    phase = _phase_for_week(plan, week)
    monday = today - timedelta(days=today.weekday())
    week_views = [v for v in views if monday.isoformat() <= v["date"] <= (monday + timedelta(days=6)).isoformat()
                  and v["sport"] != "rest"]
    done_min = sum(v["durationMinutes"] or 0 for v in week_views if v["completed"])
    plan_hours = ((week or {}).get("summary") or {}).get("totalHours")
    if plan_hours is None:
        plan_hours = sum(v["durationMinutes"] or 0 for v in week_views) / 60

    non_rest = [v for v in views if v["sport"] != "rest"]
    tomorrow = (today + timedelta(days=1)).isoformat()
    meta = plan.get("meta") or {}
    return {
        "id": row["id"],
        "title": meta.get("event") or "Training plan",
        "athlete": meta.get("athlete"),
        "total_weeks": meta.get("totalWeeks"),
        "week_number": (week or {}).get("weekNumber"),
        "phase": phase,
        "phase_color": PHASE_COLORS.get(phase or "", DEFAULT_PHASE_COLOR),
        "today": [v for v in views_by_day.get(today.isoformat(), []) if v["sport"] != "rest"],
        "tomorrow": [v for v in views_by_day.get(tomorrow, []) if v["sport"] != "rest"],
        "in_plan": week is not None,
        "week": {
            "done_hours": round(done_min / 60, 1),
            "plan_hours": round(plan_hours, 1),
            "days": _week_strip(week, monday, today, views_by_day),
        },
        "progress": {"done": sum(1 for v in non_rest if v["completed"]), "total": len(non_rest)},
        "ftp_test": _ftp_prompt(plan, views, activities, today),
        "thresholds": _thresholds(plan, views, athlete, today),
        "zones": {s: plan_doc.zone_rows(plan, s) for s in plan_doc.ZONE_SPORTS if (plan.get("zones") or {}).get(s)},
        "thresholds_now": plan_doc.effective_thresholds(plan),
    }
