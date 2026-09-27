# tools/race_predictor.py
"""Race predictions for the Fitness tab and the Today countdown.

Run: Garmin's own race predictor (5K / 10K / half / marathon), from the
daily snapshots, with how it moved over the last three months.

Triathlon: estimated from threshold values, leg by leg:
* swim — CSS plus a few seconds per 100 m (more for longer races); without a
  CSS, the swim is extrapolated from the swim personal records with
  Riegel's formula (T2 = T1 × (D2 / D1)^1.06);
* bike — a fraction of FTP (the intensity factor) turned into speed with
  33 km/h × ∛(P / 200): a flat course and an aero position;
* run — threshold pace slowed by a factor (running off the bike);
* transitions — a fixed allowance.

The multipliers are defaults; set TRI_PREDICTOR_CONFIG to a JSON object
({"70.3": {"bike_if": 0.8}, ...}) to change any of them.
"""
import copy
import json
import logging
import os

logger = logging.getLogger(__name__)

RUN_DISTANCES = (
    # (predictor key, label, record label, metres)
    ("5K", "5K", "Fastest 5K", 5000),
    ("10K", "10K", "Fastest 10K", 10000),
    ("half_marathon", "Half marathon", "Fastest Half Marathon", 21097.5),
    ("marathon", "Marathon", "Fastest Marathon", 42195),
)

TRI_DEFAULTS = {
    "sprint":  {"name": "Sprint", "swim_m": 750, "bike_km": 20, "run_km": 5,
                "swim_offset_s": 3, "bike_if": 0.92, "run_factor": 1.03, "transitions_s": 150},
    "olympic": {"name": "Olympic", "swim_m": 1500, "bike_km": 40, "run_km": 10,
                "swim_offset_s": 6, "bike_if": 0.88, "run_factor": 1.06, "transitions_s": 180},
    "70.3":    {"name": "Middle distance (70.3)", "swim_m": 1900, "bike_km": 90, "run_km": 21.1,
                "swim_offset_s": 10, "bike_if": 0.78, "run_factor": 1.15, "transitions_s": 300},
    "140.6":   {"name": "Full distance (140.6)", "swim_m": 3800, "bike_km": 180, "run_km": 42.2,
                "swim_offset_s": 15, "bike_if": 0.70, "run_factor": 1.30, "transitions_s": 480},
}
RIEGEL_EXPONENT = 1.06
# Speed at 200 W on a flat course in an aero position; speed ∝ ∛power.
BIKE_KMH_AT_200W = 33.0
SWIM_RECORDS = (("Fastest 400m Swim", 400.0), ("Fastest 100m Swim", 100.0))


def tri_config() -> dict:
    """TRI_DEFAULTS with any TRI_PREDICTOR_CONFIG overrides applied."""
    cfg = copy.deepcopy(TRI_DEFAULTS)
    raw = os.environ.get("TRI_PREDICTOR_CONFIG")
    if raw:
        try:
            for key, values in json.loads(raw).items():
                if key in cfg and isinstance(values, dict):
                    cfg[key].update({k: v for k, v in values.items() if k in cfg[key]})
        except (ValueError, AttributeError):
            logger.warning("Ignoring invalid TRI_PREDICTOR_CONFIG")
    return cfg


def fmt_time(seconds) -> str | None:
    if seconds is None:
        return None
    s = int(round(seconds))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


def fmt_pace(seconds) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def swim_from_records(records: dict | None) -> dict | None:
    """The swim record to extrapolate from: the 400 m, else the 100 m.
    {"metres", "seconds", "label"} or None."""
    by_label = {r.get("label"): r for r in (records or {}).get("swimming") or []}
    for label, metres in SWIM_RECORDS:
        value = (by_label.get(label) or {}).get("value_raw")
        if value:
            return {"metres": metres, "seconds": float(value), "label": label}
    return None


def tri_predictions(css_s: float | None, ftp: float | None, threshold_pace_s: float | None,
                    swim_record: dict | None = None) -> dict:
    """Every triathlon distance's estimate.

    Returns {"rows": [...], "missing": [...], "swim_source": "css" | "record" | None}.
    A row's total is None when a leg can't be estimated; its legs still
    show what can be."""
    missing = [name for name, v in (("FTP", ftp), ("threshold pace", threshold_pace_s)) if not v]
    swim_source = "css" if css_s else ("record" if swim_record else None)
    if swim_source is None:
        missing.insert(0, "CSS or a swim record")
    rows = []
    for key, c in tri_config().items():
        legs, total = [], 0.0
        # Swim
        if css_s:
            pace = css_s + c["swim_offset_s"]
            swim = c["swim_m"] / 100 * pace
        elif swim_record:
            swim = swim_record["seconds"] * (c["swim_m"] / swim_record["metres"]) ** RIEGEL_EXPONENT
            pace = swim / (c["swim_m"] / 100)
        else:
            swim = pace = None
        legs.append({"leg": "swim", "label": "Swim", "seconds": swim,
                     "target": f"{fmt_pace(pace)}/100m" if pace else None})
        # Bike
        if ftp:
            power = ftp * c["bike_if"]
            kmh = BIKE_KMH_AT_200W * (power / 200) ** (1 / 3)
            bike = c["bike_km"] / kmh * 3600
        else:
            power = bike = None
        legs.append({"leg": "bike", "label": "Bike", "seconds": bike,
                     "target": f"{round(power)} W" if power else None})
        # Run
        if threshold_pace_s:
            run_pace = threshold_pace_s * c["run_factor"]
            run = c["run_km"] * run_pace
        else:
            run_pace = run = None
        legs.append({"leg": "run", "label": "Run", "seconds": run,
                     "target": f"{fmt_pace(run_pace)}/km" if run_pace else None})
        legs.append({"leg": "transitions", "label": "T1+T2", "seconds": c["transitions_s"], "target": None})
        known = [l["seconds"] for l in legs]
        total = sum(known) if all(v is not None for v in known) else None
        rows.append({
            "key": key, "name": c["name"],
            "distances": f"{_fmt_m(c['swim_m'])} · {_fmt_km(c['bike_km'])} · {_fmt_km(c['run_km'])}",
            "seconds": total, "legs": legs,
        })
    return {"rows": rows, "missing": missing, "swim_source": swim_source}


def _fmt_m(m) -> str:
    return f"{m:g} m" if m < 1000 else f"{m / 1000:g} km"


def _fmt_km(km) -> str:
    return f"{km:g} km"


def run_predictions(latest: dict[str, int], earlier: dict[str, int], records: dict | None) -> list[dict]:
    """Garmin's run predictions with the matching record and the change
    since ``earlier`` (a snapshot about three months back). Seconds
    throughout; a negative change means the prediction got faster."""
    by_label = {r.get("label"): r for r in (records or {}).get("running") or []}
    out = []
    for key, label, record_label, _ in RUN_DISTANCES:
        pred = latest.get(key)
        record = (by_label.get(record_label) or {}).get("value_raw")
        before = earlier.get(key)
        out.append({
            "key": key, "name": label, "seconds": pred,
            "record_seconds": float(record) if record else None,
            "faster_than_record_by": (float(record) - pred) if record and pred and pred < float(record) else None,
            "change_seconds": (pred - before) if pred and before else None,
        })
    return out


def latest_by_distance(rows: list[dict], on_or_before: str | None = None) -> dict[str, int]:
    """{distance: seconds} from the latest snapshot per distance (rows are
    race_prediction_snapshots rows, oldest first), optionally no later than
    a date."""
    out = {}
    for r in rows:
        day = str(r["snapshot_date"])[:10]
        if on_or_before and day > on_or_before:
            continue
        out[r["distance"]] = int(r["seconds"])
    return out
