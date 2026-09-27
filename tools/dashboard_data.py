# tools/dashboard_data.py
"""Data for the redesigned Today / Trends / Fitness screens, from PostgreSQL:
the goal race with its prediction and race-day form, fitness / fatigue /
form with the plan projection, race predictions, threshold history and
records progress.

Each part is built independently — a failure is logged and that part is
left out, so one missing table never blanks the page. Without a database
everything is None and the screens simply don't show these sections.
"""
import logging
from datetime import date, timedelta

import db
from tools import goal_race, plan_doc, race_predictor, training_load

logger = logging.getLogger(__name__)

# Days of fitness / fatigue history the Trends card can show (its longest
# range), before the projection.
FITNESS_DAYS = 90
PREDICTION_TREND_DAYS = 90
RECORD_WINDOW_DAYS = 30
CLOSE_CALL_PCT = 0.02

# The Fitness page's threshold rows: (plan key, label, snapshot metric,
# unit, lower is better).
THRESHOLDS = (
    ("ftp", "FTP", "ftp", "W", False),
    ("bikeLthr", "Bike LTHR", "bike_lthr", "bpm", False),
    ("runLthr", "Run LTHR", "run_lthr", "bpm", False),
    ("thresholdPace", "Threshold pace", "threshold_pace_s", "/km", True),
    ("css", "Swim CSS", "css_s", "/100m", True),
)


def build(today: date, plan: dict | None, athlete: dict | None, records: dict | None) -> dict:
    """Every redesign section's data for ``today``; a part that fails is None."""
    out = {"fitness": None, "predictions": None, "goal": None, "thresholds": None, "records": None}
    if not db.is_configured():
        return out
    for key, fn in (
        ("fitness", lambda: fitness(today)),
        ("predictions", lambda: predictions(today, plan, athlete, records)),
        ("thresholds", lambda: threshold_history(today, plan, athlete)),
        ("records", lambda: records_progress(today, records)),
    ):
        try:
            out[key] = fn()
        except Exception:  # noqa: BLE001 — one section never takes down the page
            logger.exception("Dashboard section %s failed", key)
    try:
        out["goal"] = goal_view(today, out["fitness"], out["predictions"])
    except Exception:  # noqa: BLE001
        logger.exception("Dashboard section goal failed")
    return out


# ── FITNESS / FATIGUE ────────────────────────────────────────────────────────

def fitness(today: date) -> dict:
    from tools import planned_sessions
    series = planned_sessions.fitness_with_projection(today, FITNESS_DAYS)
    return {"today": today.isoformat(), "series": series}


# ── PREDICTIONS ──────────────────────────────────────────────────────────────

def _thresholds_now(plan: dict | None, athlete: dict | None) -> dict:
    """FTP (W), threshold pace (s/km) and CSS (s/100 m): the plan's values,
    else Garmin's."""
    t = (plan or {}).get("thresholds_now") or {}
    a = athlete or {}
    pace = plan_doc.single_pace_seconds(t.get("thresholdPace"))
    if pace is None and a.get("lactate_threshold_pace"):
        pace = round(float(a["lactate_threshold_pace"]) * 60)
    return {"ftp": t.get("ftp") or a.get("ftp"), "threshold_pace_s": pace,
            "css_s": plan_doc.single_pace_seconds(t.get("css"))}


def predictions(today: date, plan: dict | None, athlete: dict | None, records: dict | None) -> dict:
    rows = db.get_race_prediction_snapshots((today - timedelta(days=PREDICTION_TREND_DAYS + 30)).isoformat(),
                                            today.isoformat())
    latest = race_predictor.latest_by_distance(rows)
    earlier = race_predictor.latest_by_distance(rows, (today - timedelta(days=PREDICTION_TREND_DAYS)).isoformat())
    t = _thresholds_now(plan, athlete)
    tri = race_predictor.tri_predictions(t["css_s"], t["ftp"], t["threshold_pace_s"],
                                         race_predictor.swim_from_records(records))
    return {"run": race_predictor.run_predictions(latest, earlier, records), "tri": tri,
            "trend_days": PREDICTION_TREND_DAYS}


# ── GOAL RACE ────────────────────────────────────────────────────────────────

def goal_view(today: date, fit: dict | None, preds: dict | None) -> dict | None:
    goal = goal_race.get_goal_race()
    if not goal:
        return None
    g = goal_race.describe(goal, today)
    predicted = None
    if preds:
        if g["predictor_distance"]:
            predicted = next((r["seconds"] for r in preds["run"] if r["key"] == g["predictor_distance"]), None)
        else:
            predicted = next((r["seconds"] for r in preds["tri"]["rows"] if r["key"] == g["distance"]), None)
    g["predicted_sec"] = predicted
    g["gap_sec"] = (predicted - g["target_sec"]) if predicted and g.get("target_sec") else None
    g["race_form"] = training_load.race_day_form(fit["series"], g["date"]) if fit and g["days_left"] >= 0 else None
    return g


# ── THRESHOLD HISTORY ────────────────────────────────────────────────────────

def _month_ends(today: date, months: int = 12) -> list[date]:
    """The last day of each of the past ``months`` months, the current month
    ending today."""
    ends, d = [], today
    for _ in range(months):
        ends.append(d)
        d = d.replace(day=1) - timedelta(days=1)
    return list(reversed(ends))


def _value_on(points: list[tuple[str, float]], day: str) -> float | None:
    value = None
    for d, v in points:
        if d > day:
            break
        value = v
    return value


def _changes(points: list[tuple[str, float]]) -> list[dict]:
    """Each day the value changed (the first value counts as a change too)."""
    out, prev = [], None
    for d, v in points:
        if prev is None or abs(v - prev) > 1e-6:
            out.append({"date": d, "value": v, "delta": None if prev is None else v - prev})
        prev = v
    return out


def _z2_note(key: str, old: float | None, new: float | None) -> str | None:
    """How a threshold change moved the plan's Z2 (Aerobic) zone."""
    if old is None or new is None or abs(old - new) < 1e-6:
        return None

    def z2(v):
        if key == "ftp":
            _, _, lo, hi = plan_doc.BIKE_POWER_PCT_TABLE[1]
            return f"{plan_doc._pct(int(v), lo)}–{plan_doc._pct(int(v), hi)} W"
        if key in ("bikeLthr", "runLthr"):
            _, _, lo, hi = plan_doc.HR_PCT_TABLE[1]
            return f"{plan_doc._pct(int(v), lo)}–{plan_doc._pct(int(v), hi)} bpm"
        table = plan_doc.RUN_PACE_OFFSET_TABLE if key == "thresholdPace" else plan_doc.SWIM_PACE_OFFSET_TABLE
        _, _, lo, hi = table[1]
        unit = "/km" if key == "thresholdPace" else "/100m"
        return f"{plan_doc.fmt_mmss(v + lo)}–{plan_doc.fmt_mmss(v + hi)}{unit}"

    return f"Plan Z2 Aerobic is now {z2(new)} (was {z2(old)})."


def threshold_history(today: date, plan: dict | None, athlete: dict | None) -> dict:
    """Per threshold: 12 month-end values (plan history, else Garmin's),
    every change as a test, the 12-month change and a zone note."""
    rows = db.get_threshold_snapshots((today - timedelta(days=366)).isoformat(), today.isoformat())
    by = {}
    for r in rows:
        by.setdefault((r["source"], r["metric"]), []).append((str(r["snapshot_date"])[:10], float(r["value"])))
    months = _month_ends(today)
    out = {}
    for key, label, metric, unit, lower_better in THRESHOLDS:
        plan_pts, garmin_pts = by.get(("plan", metric), []), by.get(("garmin", metric), [])
        source = "plan" if plan_pts else ("garmin" if garmin_pts else None)
        pts = plan_pts or garmin_pts
        monthly = [_value_on(pts, m.isoformat()) for m in months]
        changes = _changes(pts)
        # A test (a change after the first value) marks the month it fell in.
        test_months = set()
        for c in changes[1:]:
            i = next((i for i, m in enumerate(months) if c["date"] <= m.isoformat()), None)
            if i is not None:
                test_months.add(i)
        known = [v for v in monthly if v is not None]
        change = (known[-1] - known[0]) if len(known) >= 2 else None
        improving = None if not change else (change < 0) == lower_better
        last_two = [c for c in changes if c["delta"] is not None][-1:] if changes else []
        note = _z2_note(key, last_two[0]["value"] - last_two[0]["delta"], last_two[0]["value"]) if last_two else None
        out[key] = {"label": label, "unit": unit, "lower_better": lower_better, "source": source,
                    "months": [m.isoformat() for m in months], "monthly": monthly,
                    "test_months": sorted(test_months), "changes": changes,
                    "change": change, "improving": improving, "zone_note": note}
    return out


# ── RECORDS PROGRESS ─────────────────────────────────────────────────────────

_SPORT_OF = {"running": "run", "cycling": "bike", "swimming": "swim"}


def _record_kind(label: str) -> str:
    if label.startswith("Fastest"):
        return "time"
    if "Power" in label:
        return "power"
    return "distance"


def _improvement(kind: str, new: float, old: float) -> str:
    if kind == "time":
        return f"{race_predictor.fmt_time(old - new) if old - new >= 60 else f'{round(old - new)} s'} faster"
    if kind == "power":
        return f"↑ {round(new - old)} W"
    return f"↑ {round((new - old) / 1000, 2)} km"


def records_progress(today: date, records: dict | None) -> dict:
    """New records in the last 30 days (with what they beat, when the
    change was logged) and close calls: recent best efforts within 2% of a
    record."""
    since = (today - timedelta(days=RECORD_WINDOW_DAYS)).isoformat()
    history = {(h["sport"], h["record_type"], str(h["record_date"])[:10]): h
               for h in db.get_personal_record_history(since)}
    new, current = [], {}
    for sport, recs in (records or {}).items():
        for r in recs:
            current[(sport, r.get("label"))] = r
            day = str(r.get("date") or "")[:10]
            if not day or day < since:
                continue
            kind = _record_kind(r.get("label") or "")
            prev = history.get((sport, r.get("label"), day))
            new.append({
                "sport": _SPORT_OF.get(sport, "other"), "name": r.get("label"), "value": r.get("value_formatted"),
                "date": day, "activity_id": r.get("activity_id"),
                "previous": prev["previous_formatted"] if prev else None,
                "improvement": _improvement(kind, float(r["value_raw"]), float(prev["previous_value_raw"]))
                if prev and prev.get("previous_value_raw") is not None and r.get("value_raw") is not None else None,
            })
    new.sort(key=lambda x: x["date"], reverse=True)

    close = {}
    for e in db.get_best_efforts(since):
        rec = current.get((e["sport"], e["record_type"]))
        if not rec or rec.get("value_raw") is None or rec.get("activity_id") == e["garmin_id"]:
            continue
        best, value = float(rec["value_raw"]), float(e["value"])
        kind = _record_kind(e["record_type"])
        gap = (value - best) if kind == "time" else (best - value)
        if gap <= 0 or gap / best > CLOSE_CALL_PCT:
            continue
        key = (e["sport"], e["record_type"])
        if key in close and close[key]["gap"] <= gap:
            continue
        close[key] = {
            "sport": _SPORT_OF.get(e["sport"], "other"), "name": e["record_type"],
            "value": race_predictor.fmt_time(value) if kind == "time" else f"{round(value)} W",
            "record": rec.get("value_formatted"), "date": str(e["activity_date"])[:10],
            "activity_id": e["garmin_id"], "gap": gap,
            "off": (f"{round(gap)} s off" if gap < 60 else f"{race_predictor.fmt_time(gap)} off")
            if kind == "time" else f"{round(gap)} W off",
        }
    count = sum(len(v or []) for v in (records or {}).values())
    return {"new": new, "close": sorted(close.values(), key=lambda c: c["date"], reverse=True), "count": count}
