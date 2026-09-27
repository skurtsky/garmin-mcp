# tools/history_snapshots.py
"""Daily history for values Garmin only reports as "current": thresholds
(FTP, LTHR, threshold pace, CSS, VO2max) and race predictions.

The regular sync stores today's values each run (``snapshot_*``); the
one-off ``backfill_history`` (``sync_garmin.py --backfill-history``) fills
the past year from the history endpoints behind Garmin Connect's own charts,
plus what the database already holds (VO2max from daily metrics, FTP from
each ride, plan thresholds from every saved plan revision).

Threshold metrics, one unit each:
    ftp (W), bike_lthr / run_lthr (bpm), threshold_pace_s (s/km),
    css_s (s/100 m), vo2max_run / vo2max_bike (ml/kg/min)
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import db
from tools import plan_doc

logger = logging.getLogger(__name__)

PREDICTION_KEYS = {"time5K": "5K", "time10K": "10K", "timeHalfMarathon": "half_marathon",
                   "timeMarathon": "marathon"}
BACKFILL_DAYS = 365
_MAX_WORKERS = 8


# ── VALUES FROM ONE SOURCE ───────────────────────────────────────────────────

def garmin_threshold_values(profile: dict | None) -> dict:
    """Thresholds from the synced athlete profile (tools/profile.py)."""
    p = profile or {}
    pace_min = p.get("lactate_threshold_pace")  # decimal minutes per km
    return {
        "ftp": p.get("ftp"),
        "run_lthr": p.get("lactate_threshold_hr"),
        "threshold_pace_s": round(float(pace_min) * 60, 1) if pace_min else None,
        "vo2max_run": p.get("vo2max_running"),
        "vo2max_bike": p.get("vo2max_cycling"),
    }


def plan_threshold_values(plan: dict | None) -> dict:
    """Thresholds a plan currently uses (overrides over its own zones)."""
    if not plan:
        return {}
    t = plan_doc.effective_thresholds(plan)
    css = (plan.get("overrides") or {}).get("cssSeconds") or plan_doc.single_pace_seconds(t.get("css"))
    return {
        "ftp": t.get("ftp"),
        "bike_lthr": t.get("bikeLthr"),
        "run_lthr": t.get("runLthr"),
        "threshold_pace_s": plan_doc.single_pace_seconds(t.get("thresholdPace")),
        "css_s": css,
    }


def prediction_values(raw: dict | None) -> dict:
    """{distance: seconds} from one Garmin race-predictor entry."""
    return {label: int(raw[key]) for key, label in PREDICTION_KEYS.items()
            if (raw or {}).get(key)}


# ── DAILY SNAPSHOTS (regular sync) ───────────────────────────────────────────

def snapshot_garmin_thresholds(today: date, profile: dict | None):
    db.upsert_threshold_snapshot(today.isoformat(), "garmin", garmin_threshold_values(profile))


def snapshot_plan_thresholds(today: date):
    row = db.get_training_plan(None)
    if row is not None:
        db.upsert_threshold_snapshot(today.isoformat(), "plan", plan_threshold_values(row["plan"]))


def snapshot_race_predictions(today: date, client) -> dict:
    raw = client.get_race_predictions() or {}
    values = prediction_values(raw)
    day = str(raw.get("calendarDate") or today.isoformat())[:10]
    db.upsert_race_predictions(day, values)
    return values


# ── BACKFILL (one-off) ───────────────────────────────────────────────────────

def _entries(resp) -> list[dict]:
    """Garmin's stats/range responses as a flat list of dicts."""
    if isinstance(resp, dict):
        for key in ("values", "data", "entries", "metrics"):
            if isinstance(resp.get(key), list):
                return resp[key]
        return [resp]
    return [e for e in resp or [] if isinstance(e, dict)]


def _entry_day(entry: dict) -> str | None:
    for key in ("calendarDate", "from", "date", "startDate"):
        if entry.get(key):
            return str(entry[key])[:10]
    return None


def stats_points(resp) -> dict[str, float]:
    """{date: value} from a biometric-service stats/range response."""
    out = {}
    for e in _entries(resp):
        day, value = _entry_day(e), e.get("value")
        if day and value is not None:
            out[day] = float(value)
    return out


def lt_speed_to_pace(value: float) -> float | None:
    """Garmin's lactate-threshold speed → seconds per km. The profile reports
    it in tenths of m/s (0.39 = 3.9 m/s); plain m/s values pass through."""
    if not value or value <= 0:
        return None
    ms = value * 10 if value < 1.5 else value
    return round(1000 / ms, 1)


def _backfill_predictions(client, start: date, end: date) -> int:
    start = max(start, end - timedelta(days=365))  # Garmin's limit per request
    resp = client.get_race_predictions(start.isoformat(), end.isoformat(), "daily")
    n = 0
    for entry in _entries(resp):
        day = _entry_day(entry)
        values = prediction_values(entry)
        if day and values:
            db.upsert_race_predictions(day, values)
            n += 1
    return n


def _backfill_run_threshold(client, start: date, end: date) -> int:
    resp = client.get_lactate_threshold(latest=False, start_date=start.isoformat(),
                                        end_date=end.isoformat(), aggregation="daily") or {}
    hr = stats_points(resp.get("heart_rate"))
    pace = {d: lt_speed_to_pace(v) for d, v in stats_points(resp.get("speed")).items()}
    for day in sorted(set(hr) | set(pace)):
        db.upsert_threshold_snapshot(day, "garmin", {"run_lthr": hr.get(day), "threshold_pace_s": pace.get(day)})
    return len(set(hr) | set(pace))


def _backfill_ftp(client, start: date, end: date) -> int:
    """FTP history from the same endpoint Garmin Connect's chart reads; when
    that isn't available, the FTP recorded on each synced ride."""
    points = {}
    try:
        resp = client.connectapi(
            f"/biometric-service/stats/functionalThresholdPower/range/{start.isoformat()}/{end.isoformat()}"
            "?sport=CYCLING&aggregation=daily&aggregationStrategy=LATEST")
        points = stats_points(resp)
    except Exception:
        logger.warning("Garmin FTP history unavailable — using the FTP stored on each ride", exc_info=True)
    if not points:
        points = {row["day"]: row["ftp"] for row in db.get_ride_ftp_history(start.isoformat(), end.isoformat())}
    for day, ftp in sorted(points.items()):
        db.upsert_threshold_snapshot(day, "garmin", {"ftp": round(ftp)})
    return len(points)


def _vo2_from_max_metrics(resp) -> tuple[float | None, float | None]:
    entry = (resp[0] if isinstance(resp, list) and resp else resp) or {}
    run = (entry.get("generic") or {}).get("vo2MaxPreciseValue")
    bike = (entry.get("cycling") or {}).get("vo2MaxPreciseValue")
    return run, bike


def _backfill_vo2max(client, start: date, end: date) -> int:
    """VO2max from what the daily-metrics sync already stored; only the days
    missing from it are fetched from Garmin (one call per day)."""
    known = {}
    for row in db.get_vo2max_history_from_daily_metrics(start.isoformat(), end.isoformat()):
        if row["running"] is not None or row["cycling"] is not None:
            known[str(row["metric_date"])] = (row["running"], row["cycling"])
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    missing = [d for d in days if d not in known]

    def fetch(d):
        try:
            return d, _vo2_from_max_metrics(client.get_max_metrics(d))
        except Exception:
            return d, (None, None)

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        known.update(dict(pool.map(fetch, missing)))
    n = 0
    for day, (run, bike) in sorted(known.items()):
        if run is not None or bike is not None:
            db.upsert_threshold_snapshot(day, "garmin", {"vo2max_run": run, "vo2max_bike": bike})
            n += 1
    return n


def _backfill_plan_thresholds() -> int:
    """Replay every saved plan revision: the thresholds in force at the end
    of each day a plan changed."""
    by_day = {}
    for rev in db.list_plan_revisions_for_thresholds():
        created = rev["created_at"]
        day = (created.date() if isinstance(created, datetime) else date.fromisoformat(str(created)[:10])).isoformat()
        by_day[day] = plan_threshold_values(rev["plan"])
    for day, values in sorted(by_day.items()):
        db.upsert_threshold_snapshot(day, "plan", values)
    return len(by_day)


def backfill_history(client, today: date | None = None, days: int = BACKFILL_DAYS) -> dict:
    """Fill the past ``days`` of threshold and prediction history. Each part
    runs on its own, so one unavailable endpoint doesn't stop the rest.
    Returns {part: rows written, or the error}."""
    end = today or date.today()
    start = end - timedelta(days=days)
    parts = {
        "race_predictions": lambda: _backfill_predictions(client, start, end),
        "run_threshold": lambda: _backfill_run_threshold(client, start, end),
        "ftp": lambda: _backfill_ftp(client, start, end),
        "vo2max": lambda: _backfill_vo2max(client, start, end),
        "plan_thresholds": _backfill_plan_thresholds,
    }
    results = {}
    for name, fn in parts.items():
        try:
            results[name] = fn()
        except Exception as e:  # noqa: BLE001 — report and carry on
            logger.exception("Backfill of %s failed", name)
            results[name] = f"error: {e}"
    return results
