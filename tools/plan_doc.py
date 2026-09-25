# tools/plan_doc.py
"""Pure operations on a Claude Coach training-plan document (no I/O).

The plan JSON produced by the coach skill is stored verbatim as one document
(see ``reference/plan-schema.md`` in the skill) — ``meta``, ``zones``,
``phases`` and ``weeks[].days[].workouts[]``. Everything that changes it goes
through this module so the web viewer and the MCP tools share one set of
rules:

* ``validate_plan`` — what an upload must satisfy (errors) and should satisfy
  (warnings).
* ``normalize_plan`` — recomputes every week's ``summary`` from the workouts it
  actually contains, so the totals can't drift from the sessions after an edit.
* ``apply_operations`` — the edit vocabulary shared by the viewer and the
  ``amend_training_plan`` MCP tool (add/update/move/remove a workout, edit a
  week, zones, units).

Viewer settings live in the document as top-level keys the renderers already
understand: ``unit``, ``overrides`` (live FTP/LTHR/threshold-pace/CSS values)
and ``zonesValidated`` — the same keys the viewer's export and the PDF builder
use.
"""
import copy
import re
from datetime import date

# Plan ids end up in URLs and as a primary key — keep them to safe characters.
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")

REQUIRED_META = ("id", "event", "planStartDate", "planEndDate", "totalWeeks")

SPORTS = ("swim", "bike", "run", "brick", "strength", "race", "rest", "other")

# plan-schema.md's length budget: these print in a 1.4in PDF column.
FIELD_BUDGETS = {"description": 120, "keyTargets": 120}

# Override keys the viewer and build_pdf.py read (plan["overrides"]).
THRESHOLD_KEYS = ("ftp", "bikeLthr", "runLthr", "thresholdPace", "cssLabel", "cssSeconds")
ZONE_SPORTS = ("swim", "bike", "run")
UNITS = ("metric", "imperial")

WEEK_FIELDS = {"focus": str, "targetHours": (int, float), "isRecoveryWeek": bool, "phase": str}

_DOW = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MMSS_RE = re.compile(r"(\d+):(\d{1,2})")


class PlanError(ValueError):
    """A plan or an edit that can't be applied. The message is user-facing."""


# ── HELPERS ───────────────────────────────────────────────────────────────────

def parse_date(value, what="date") -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise PlanError(f"Invalid {what} {value!r} — use YYYY-MM-DD.") from None


def iter_workouts(plan: dict):
    """Yield (week, day, workout) for every workout in the plan."""
    for week in plan.get("weeks") or []:
        for day in week.get("days") or []:
            for workout in day.get("workouts") or []:
                yield week, day, workout


def find_workout(plan: dict, workout_id: str):
    """(week, day, workout) for an id, or None."""
    for week, day, workout in iter_workouts(plan):
        if workout.get("id") == workout_id:
            return week, day, workout
    return None


def _require_workout(plan: dict, workout_id) -> tuple:
    hit = find_workout(plan, workout_id) if workout_id else None
    if not hit:
        raise PlanError(f"No workout with id {workout_id!r} in this plan.")
    return hit


def week_for_date(plan: dict, day: date) -> dict | None:
    for week in plan.get("weeks") or []:
        try:
            start = parse_date(week.get("startDate"))
            end = parse_date(week.get("endDate"))
        except PlanError:
            continue
        if start <= day <= end:
            return week
    return None


def _day_entry(plan: dict, day: date) -> dict:
    """The ``days[]`` entry for a date, created inside its week if missing."""
    week = week_for_date(plan, day)
    if week is None:
        raise PlanError(f"{day.isoformat()} is outside every week of this plan.")
    days = week.setdefault("days", [])
    for entry in days:
        if str(entry.get("date"))[:10] == day.isoformat():
            entry.setdefault("workouts", [])
            return entry
    entry = {"date": day.isoformat(), "dayOfWeek": _DOW[day.weekday()], "workouts": []}
    days.append(entry)
    days.sort(key=lambda d: str(d.get("date")))
    return entry


def _describe(workout: dict) -> str:
    return f"'{workout.get('name') or workout.get('id')}'"


def _short(day) -> str:
    d = parse_date(day) if not isinstance(day, date) else day
    return d.strftime("%a %b ") + str(d.day)


def new_workout_id(plan: dict, day: date, sport: str) -> str:
    """``w{week}-{dow}-{sport}`` like the coach skill's ids, suffixed until unique."""
    week = week_for_date(plan, day) or {}
    base = f"w{week.get('weekNumber', 0)}-{day.strftime('%a').lower()}-{sport or 'workout'}"
    taken = {w.get("id") for _, _, w in iter_workouts(plan)}
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _parse_mmss(value) -> int | None:
    m = _MMSS_RE.search(str(value or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


# ── VALIDATION ────────────────────────────────────────────────────────────────

def validate_plan(plan) -> tuple[list[str], list[str]]:
    """(errors, warnings) for an uploaded plan document.

    Errors make the upload unusable (no id, duplicate workout ids — completion
    ticks would collide); warnings are the same niceties the skill's
    render_plan.py warns about, plus the plan-schema.md field budgets.
    """
    errors, warnings = [], []
    if not isinstance(plan, dict):
        return ["The file must contain a JSON object (the plan)."], warnings

    meta = plan.get("meta")
    if not isinstance(meta, dict):
        errors.append("Missing 'meta' object.")
        meta = {}
    plan_id = meta.get("id")
    if not plan_id:
        errors.append("Missing meta.id — every plan needs an id.")
    elif not isinstance(plan_id, str) or not PLAN_ID_RE.match(plan_id):
        errors.append(f"meta.id {plan_id!r} must be 1–120 letters, digits, '.', '_' or '-'.")
    for key in REQUIRED_META:
        if key != "id" and key not in meta:
            warnings.append(f"Missing meta.{key}.")

    weeks = plan.get("weeks")
    if not isinstance(weeks, list):
        errors.append("Missing 'weeks' list.")
        return errors, warnings
    if not plan.get("phases"):
        warnings.append("No phases — the phase selector and badges will be empty.")

    seen = set()
    for week in weeks:
        if not isinstance(week, dict):
            errors.append("Every entry in 'weeks' must be an object.")
            continue
        label = f"Week {week.get('weekNumber', '?')}"
        if not isinstance(week.get("weekNumber"), int):
            errors.append(f"{label} has no integer weekNumber.")
        try:
            start = parse_date(week.get("startDate"), "startDate")
            end = parse_date(week.get("endDate"), "endDate")
        except PlanError as e:
            errors.append(f"{label}: {e}")
            start = end = None
        for day in week.get("days") or []:
            try:
                day_date = parse_date(day.get("date"))
            except PlanError as e:
                errors.append(f"{label}: {e}")
                continue
            if start and end and not start <= day_date <= end:
                warnings.append(f"{label}: {day_date} is outside the week's dates.")
            for w in day.get("workouts") or []:
                wid = w.get("id")
                if not wid:
                    errors.append(f"{label} has a workout with no id.")
                    continue
                if wid in seen:
                    errors.append(f"Duplicate workout id '{wid}' — completion ticks would collide.")
                seen.add(wid)
                for field, budget in FIELD_BUDGETS.items():
                    text = w.get(field)
                    if isinstance(text, str) and len(text) > budget:
                        warnings.append(f"{wid}: {field} is {len(text)} characters (budget {budget}).")
    return errors, warnings


# ── WEEKLY TOTALS ─────────────────────────────────────────────────────────────

def _workout_km(w: dict) -> float:
    if isinstance(w.get("distanceKm"), (int, float)):
        return float(w["distanceKm"])
    rng = w.get("distanceKmRange")
    if isinstance(rng, dict) and isinstance(rng.get("low"), (int, float)) and isinstance(rng.get("high"), (int, float)):
        return (rng["low"] + rng["high"]) / 2
    if isinstance(w.get("distanceMeters"), (int, float)):
        return w["distanceMeters"] / 1000
    return 0.0


def _tidy(value: float, places: int):
    value = round(value, places)
    return int(value) if value == int(value) else value


def week_summary(week: dict) -> dict:
    """``{"totalHours", "bySport": {sport: {sessions, hours[, km]}}}`` from the
    week's workouts — the shape the viewer and the PDF read."""
    by_sport, total_min = {}, 0.0
    for day in week.get("days") or []:
        for w in day.get("workouts") or []:
            sport = w.get("sport") or "other"
            if sport == "rest":
                continue
            minutes = w.get("durationMinutes") if isinstance(w.get("durationMinutes"), (int, float)) else 0
            row = by_sport.setdefault(sport, {"sessions": 0, "minutes": 0.0, "km": 0.0})
            row["sessions"] += 1
            row["minutes"] += minutes
            row["km"] += _workout_km(w)
            total_min += minutes
    out = {}
    for sport, row in by_sport.items():
        entry = {"sessions": row["sessions"], "hours": _tidy(row["minutes"] / 60, 2)}
        if row["km"]:
            entry["km"] = _tidy(row["km"], 1)
        out[sport] = entry
    return {"totalHours": _tidy(total_min / 60, 2), "bySport": out}


def normalize_plan(plan: dict) -> dict:
    """Recompute every week's summary in place; returns the plan."""
    for week in plan.get("weeks") or []:
        week["summary"] = week_summary(week)
    return plan


# ── EDIT OPERATIONS ───────────────────────────────────────────────────────────

def _clean_workout_fields(fields: dict, *, adding: bool) -> dict:
    if not isinstance(fields, dict):
        raise PlanError("Workout fields must be an object.")
    fields = dict(fields)
    fields.pop("id", None)
    fields.pop("completed", None)  # completion is tracked separately
    if "sport" in fields or adding:
        sport = fields.get("sport")
        if sport not in SPORTS:
            raise PlanError(f"sport must be one of {', '.join(SPORTS)}.")
    if adding and not str(fields.get("name") or "").strip():
        raise PlanError("A new workout needs a name.")
    for key in ("durationMinutes", "distanceMeters", "distanceKm"):
        if fields.get(key) is not None:
            value = fields[key]
            if isinstance(value, str) and value.strip():
                try:
                    value = float(value)
                except ValueError:
                    raise PlanError(f"{key} must be a number.") from None
            if value == "":
                value = None
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
                raise PlanError(f"{key} must be a non-negative number.")
            fields[key] = _tidy(value, 2) if value is not None else None
    for key in ("name", "type", "description", "keyTargets", "humanReadable", "primaryZone"):
        if key in fields and fields[key] is not None and not isinstance(fields[key], str):
            raise PlanError(f"{key} must be text.")
    return fields


def _apply_fields(workout: dict, fields: dict) -> None:
    for key, value in fields.items():
        if value is None or value == "":
            workout.pop(key, None)
        else:
            workout[key] = value


def _op_add_workout(plan, op):
    day = parse_date(op.get("date"))
    fields = _clean_workout_fields(op.get("workout") or {}, adding=True)
    entry = _day_entry(plan, day)
    workout = {"id": new_workout_id(plan, day, fields["sport"])}
    _apply_fields(workout, fields)
    workout["completed"] = False
    entry["workouts"].append(workout)
    return f"Added {_describe(workout)} on {_short(day)}", workout["id"]


def _op_update_workout(plan, op):
    _, _, workout = _require_workout(plan, op.get("workout_id"))
    fields = _clean_workout_fields(op.get("fields") or {}, adding=False)
    new_date = fields.pop("date", None)
    _apply_fields(workout, fields)
    if not str(workout.get("name") or "").strip():
        raise PlanError("A workout needs a name.")
    message = f"Edited {_describe(workout)}"
    if new_date:
        moved = _op_move_workout(plan, {"workout_id": workout["id"], "date": new_date})
        if moved:
            message += " and " + moved[0][0].lower() + moved[0][1:]
    return message, workout["id"]


def _op_move_workout(plan, op):
    week, day, workout = _require_workout(plan, op.get("workout_id"))
    target = parse_date(op.get("date"))
    if str(day.get("date"))[:10] == target.isoformat():
        return None
    entry = _day_entry(plan, target)
    day["workouts"].remove(workout)
    entry["workouts"].append(workout)
    return f"Moved {_describe(workout)} {_short(day.get('date'))} → {_short(target)}", workout["id"]


def _op_remove_workout(plan, op):
    _, day, workout = _require_workout(plan, op.get("workout_id"))
    day["workouts"].remove(workout)
    return f"Removed {_describe(workout)} from {_short(day.get('date'))}", workout["id"]


def _op_update_week(plan, op):
    number = op.get("week_number")
    week = next((w for w in plan.get("weeks") or [] if w.get("weekNumber") == number), None)
    if week is None:
        raise PlanError(f"No week {number!r} in this plan.")
    fields = op.get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        raise PlanError("update_week needs fields to change.")
    for key, value in fields.items():
        kind = WEEK_FIELDS.get(key)
        if kind is None:
            raise PlanError(f"Week field {key!r} can't be edited (allowed: {', '.join(WEEK_FIELDS)}).")
        if not isinstance(value, kind) or (kind != bool and isinstance(value, bool)):
            raise PlanError(f"Week field {key!r} has the wrong type.")
        week[key] = value
    return f"Updated week {number} ({', '.join(fields)})", None


def _op_set_zones(plan, op):
    overrides = dict(plan.get("overrides") or {})
    changed = []
    for key in THRESHOLD_KEYS:
        if key not in op:
            continue
        value = op[key]
        if key in ("thresholdPace", "cssLabel"):
            if value in (None, ""):
                overrides.pop(key, None)
                if key == "cssLabel":
                    overrides.pop("cssSeconds", None)
            else:
                if _parse_mmss(value) is None:
                    raise PlanError(f"{key} must look like m:ss (e.g. 4:17).")
                value = str(value).strip()
                if key == "cssLabel":
                    if "/" not in value:
                        value += "/100m"
                    overrides["cssSeconds"] = _parse_mmss(value)
                overrides[key] = value
        else:
            if value in (None, ""):
                overrides.pop(key, None)
            else:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise PlanError(f"{key} must be a whole number.") from None
                if value <= 0:
                    raise PlanError(f"{key} must be positive.")
                overrides[key] = value
        changed.append(key)
    if not changed:
        raise PlanError(f"set_zones needs at least one of {', '.join(THRESHOLD_KEYS)}.")
    plan["overrides"] = overrides
    return "Updated zones (" + ", ".join(f"{k} {overrides.get(k, 'reset')}" for k in changed) + ")", None


def _op_set_zone_validation(plan, op):
    sport = op.get("sport")
    if sport not in ZONE_SPORTS:
        raise PlanError(f"sport must be one of {', '.join(ZONE_SPORTS)}.")
    validated = bool(op.get("validated", True))
    zv = dict(plan.get("zonesValidated") or {})
    zv[sport] = {"validated": validated, "at": op.get("at") or date.today().isoformat()}
    plan["zonesValidated"] = zv
    return f"Marked {sport} zones {'validated' if validated else 'for retest'}", None


def _op_set_unit(plan, op):
    unit = op.get("unit")
    if unit not in UNITS:
        raise PlanError(f"unit must be one of {', '.join(UNITS)}.")
    plan["unit"] = unit
    return f"Units set to {unit}", None


OPERATIONS = {
    "add_workout": _op_add_workout,
    "update_workout": _op_update_workout,
    "move_workout": _op_move_workout,
    "remove_workout": _op_remove_workout,
    "update_week": _op_update_week,
    "set_zones": _op_set_zones,
    "set_zone_validation": _op_set_zone_validation,
    "set_unit": _op_set_unit,
}


def apply_operations(plan: dict, operations: list) -> tuple[dict, list[str], list[str]]:
    """Apply edits to a copy of ``plan``, all-or-nothing.

    Returns (new_plan, change_messages, touched_workout_ids). Raises PlanError
    naming the failing operation; the input plan is never modified.
    """
    if not isinstance(operations, list) or not operations:
        raise PlanError("operations must be a non-empty list.")
    plan = copy.deepcopy(plan)
    messages, touched = [], []
    for i, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            raise PlanError(f"Operation {i} must be an object.")
        handler = OPERATIONS.get(op.get("op"))
        if handler is None:
            raise PlanError(f"Operation {i}: unknown op {op.get('op')!r} (allowed: {', '.join(OPERATIONS)}).")
        try:
            result = handler(plan, op)
        except PlanError as e:
            raise PlanError(f"Operation {i} ({op.get('op')}): {e}") from None
        if result:
            messages.append(result[0])
            if result[1]:
                touched.append(result[1])
    normalize_plan(plan)
    meta = plan.setdefault("meta", {})
    meta["updatedAt"] = date.today().isoformat() + "T00:00:00Z"
    return plan, messages, touched


# ── READ HELPERS ──────────────────────────────────────────────────────────────

def effective_thresholds(plan: dict) -> dict:
    """The thresholds the viewer and PDF currently use: overrides over plan zones."""
    z = plan.get("zones") or {}
    ov = plan.get("overrides") or {}
    bike, run, swim = z.get("bike") or {}, z.get("run") or {}, z.get("swim") or {}
    out = {
        "ftp": ov.get("ftp", (bike.get("power") or {}).get("ftp")),
        "bikeLthr": ov.get("bikeLthr", (bike.get("hr") or {}).get("lthr")),
        "runLthr": ov.get("runLthr", (run.get("hr") or {}).get("lthr")),
        "thresholdPace": ov.get("thresholdPace") or (run.get("pace") or {}).get("thresholdPace"),
        "css": ov.get("cssLabel") or swim.get("css"),
        "zonesValidated": plan.get("zonesValidated") or {},
    }
    return {k: v for k, v in out.items() if v not in (None, "", {})}


def current_week_number(plan: dict, today: date | None = None) -> int | None:
    today = today or date.today()
    week = week_for_date(plan, today)
    if week:
        return week.get("weekNumber")
    weeks = plan.get("weeks") or []
    if weeks and today < parse_date(weeks[0].get("startDate")):
        return weeks[0].get("weekNumber")
    return None


def plan_title(plan: dict) -> str:
    meta = plan.get("meta") or {}
    return " — ".join(p for p in (meta.get("event") or "Training plan", meta.get("athlete") or "") if p)
