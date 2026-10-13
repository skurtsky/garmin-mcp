# tools/workout.py
import copy
import re
from tools.local_time import local_today
from typing import Literal

from garmin_client import get_client
from tools.exercise_db import exercise_notes, validate_exercise
from tools.weight_units import KG_PER_LB, kg_to_lb, lb_to_kg


_STEP_TYPE_MAP = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
}

_END_CONDITION_MAP = {
    "distance": {
        "conditionTypeId": 3,
        "conditionTypeKey": "distance",
        "displayOrder": 3,
        "displayable": True,
    },
    "time": {
        "conditionTypeId": 2,
        "conditionTypeKey": "time",
        "displayOrder": 2,
        "displayable": True,
    },
    "lap_button": {
        "conditionTypeId": 1,
        "conditionTypeKey": "lap.button",
        "displayOrder": 1,
        "displayable": True,
    },
    # Strength exercise steps: a rep target. Confirmed against the live API
    # (stored back as conditionTypeKey "reps"); accepted for every exercise.
    "reps": {
        "conditionTypeId": 10,
        "conditionTypeKey": "reps",
        "displayOrder": 10,
        "displayable": True,
    },
    "iterations": {
        "conditionTypeId": 7,
        "conditionTypeKey": "iterations",
        "displayOrder": 7,
        "displayable": False,
    },
}


_TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_TARGET_HEART_RATE = {
    "workoutTargetTypeId": 4,
    "workoutTargetTypeKey": "heart.rate.zone",
    "displayOrder": 4,
}
_TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}
_TARGET_POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2}

# create_workout supports these sport types. Other sport types (cardio,
# swimming, ...) can still be viewed via get_workout_detail and
# get_saved_workouts, but not created — see build_workout_payload.
_SPORT_TYPE_MAP = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "strength_training": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
}

# Step input fields create_workout accepts. Anything else is rejected rather
# than silently dropped (that is how a strength duration_s once vanished).
_ENDURANCE_STEP_FIELDS = frozenset({
    "type", "description", "distance_m", "duration_s", "sets", "rest_duration_s",
    "pace_min_per_km", "pace_max_per_km", "hr_min", "hr_max",
    "power_watts_min", "power_watts_max",
})
_STRENGTH_STEP_FIELDS = frozenset({
    "type", "description", "sets", "exercise_name", "category",
    "duration_s", "reps", "rest_duration_s",
    "weight_kg", "weight_lb", "set_weights", "set_weights_lb",
})
_STRENGTH_REST_FIELDS = frozenset({"type", "description", "duration_s"})


def _check_fields(step: dict, allowed: frozenset, where: str) -> None:
    """Raise a clear ValueError for step keys the builder would otherwise ignore."""
    unknown = sorted(set(step) - allowed)
    if unknown:
        raise ValueError(
            f"{where}: unknown step field(s) {unknown}. Allowed fields: {sorted(allowed)}"
        )


def _positive_number(step: dict, key: str, where: str, *, integer: bool = False):
    value = step.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{where}: {key} must be a positive number, got {value!r}")
    if integer and float(value) != int(value):
        raise ValueError(f"{where}: {key} must be a whole number, got {value!r}")
    return int(value) if integer else float(value)


_STROKE_TYPE = {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0}
_EQUIPMENT_TYPE = {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0}
_WEIGHT_UNIT_KG = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
# Garmin's sentinel weightValue meaning "bodyweight / no external load".
_BODYWEIGHT_SENTINEL = -1.0


def mps_to_pace(mps: float | None) -> str | None:
    """Convert speed in m/s to a 'M:SS' min/km pace string. Inverse of pace_to_mps."""
    if not mps or mps <= 0:
        return None
    secs_per_km = 1000.0 / mps
    m = int(secs_per_km // 60)
    s = int(round(secs_per_km % 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def pace_to_mps(pace: float | str) -> float:
    """
    Convert pace in min/km to m/s.

    Accepted formats:
    - float: 6.0 means 6:00/km
    - str: "4:10", "4:10/km", "6:00"
    """
    total_seconds_per_km: float

    if isinstance(pace, (int, float)):
        if pace <= 0:
            raise ValueError("pace must be > 0")
        total_seconds_per_km = float(pace) * 60.0
    else:
        text = str(pace).strip().lower().replace("/km", "")
        if ":" in text:
            mm, ss = text.split(":", 1)
            minutes = int(mm)
            seconds = int(ss)
            if minutes < 0 or seconds < 0 or seconds >= 60:
                raise ValueError("pace string must be in M:SS format")
            total_seconds_per_km = float(minutes * 60 + seconds)
        else:
            numeric = float(text)
            if numeric <= 0:
                raise ValueError("pace must be > 0")
            total_seconds_per_km = numeric * 60.0

    if total_seconds_per_km <= 0:
        raise ValueError("pace must be > 0")

    return round(1000.0 / total_seconds_per_km, 3)


def parse_wave(description: str | None) -> list[float] | None:
    """
    Parse strength "wave" notation into per-set weights.

    A wave description encodes an ascending/varying load across the sets of a
    single exercise, e.g. "130 - 150 - 160" (three sets) or a single "60".
    Returns a list of floats, or None if `description` is empty or contains any
    non-numeric token (i.e. it's free-text, not a wave — e.g. "400m rep").
    """
    if not description:
        return None
    tokens = [t.strip() for t in re.split(r"[-–—,/]+", description) if t.strip()]
    if not tokens:
        return None
    weights: list[float] = []
    for tok in tokens:
        try:
            weights.append(float(tok))
        except ValueError:
            return None
    return weights


def format_wave(weights: list[float]) -> str:
    """Inverse of parse_wave: render per-set weights as 'a - b - c' notation."""
    def fmt(w: float) -> str:
        return str(int(w)) if float(w).is_integer() else str(w)

    return " - ".join(fmt(w) for w in weights)


def _iter_months(months_ahead: int) -> list[tuple[int, int]]:
    """Return [(year, month)] from current month through months_ahead inclusive."""
    if months_ahead < 0:
        raise ValueError("months_ahead must be >= 0")

    start = local_today()
    months: list[tuple[int, int]] = []

    for offset in range(months_ahead + 1):
        zero_based_month = (start.month - 1) + offset
        year = start.year + (zero_based_month // 12)
        month = (zero_based_month % 12) + 1
        months.append((year, month))

    return months


def _extract_sport_type(item: dict) -> str | None:
    return (
        item.get("sportTypeKey")
        or (item.get("sportType") or {}).get("sportTypeKey")
        or (item.get("workout") or {}).get("sportType", {}).get("sportTypeKey")
    )


def _extract_workout_name(item: dict) -> str | None:
    return (
        item.get("workoutName")
        or (item.get("workout") or {}).get("workoutName")
        or (item.get("metadata") or {}).get("workoutName")
    )


def get_scheduled_workouts(months_ahead: int = 3) -> list[dict]:
    """
    Get upcoming scheduled running workouts from Garmin calendar items.

    Args:
        months_ahead: Number of months ahead to scan, inclusive of current month.
    """
    client = get_client()
    today_iso = local_today().isoformat()

    out: list[dict] = []
    for year, month in _iter_months(months_ahead):
        month_data = client.get_scheduled_workouts(year, month) or {}
        items = month_data.get("calendarItems") or []

        for item in items:
            item_type = item.get("itemType")
            item_date = item.get("date")
            sport_type = _extract_sport_type(item)

            if item_type != "workout":
                continue
            if sport_type != "running":
                continue
            if not item_date or item_date < today_iso:
                continue

            out.append(
                {
                    "schedule_id": item.get("workoutScheduleId") or item.get("calendarItemId") or item.get("id"),
                    "workout_id": item.get("workoutId") or (item.get("workout") or {}).get("workoutId"),
                    "workout_name": _extract_workout_name(item),
                    "sport_type": sport_type,
                    "date": item_date,
                    "item_type": item_type,
                    "raw": item,
                }
            )

    out.sort(key=lambda x: x.get("date") or "")
    return out


def get_saved_workouts(sport_type: str | None = None) -> list[dict]:
    """
    Get saved workouts from Garmin workout library (summary metadata only —
    no step detail; use get_workout_detail(workout_id) for the full steps).

    Args:
        sport_type: Optional sport type filter (e.g. running, cycling).
    """
    client = get_client()
    workouts = client.get_workouts() or []

    normalized: list[dict] = []
    for w in workouts:
        workout_sport = (w.get("sportType") or {}).get("sportTypeKey")
        if sport_type and workout_sport != sport_type:
            continue

        normalized.append(
            {
                "workout_id": w.get("workoutId"),
                "workout_name": w.get("workoutName"),
                "sport_type": workout_sport,
                "description": w.get("description"),
                "estimated_duration_secs": w.get("estimatedDurationInSecs"),
                "estimated_distance_m": w.get("estimatedDistanceInMeters"),
                "updated_date": w.get("updatedDate"),
                "raw": w,
            }
        )

    return normalized


def schedule_workout(workout_id: int, date: str) -> dict:
    """Schedule an existing workout on a specific date (YYYY-MM-DD)."""
    client = get_client()
    result = client.schedule_workout(workout_id, date)
    return {
        "workout_id": workout_id,
        "date": date,
        "result": result,
    }


def unschedule_workout(schedule_id: int) -> dict:
    """Remove a scheduled workout from the calendar."""
    client = get_client()
    result = client.unschedule_workout(schedule_id)
    return {
        "status": "ok",
        "schedule_id": schedule_id,
        "result": result,
    }


def _make_executable_step(
    step_order: int,
    step_type_key: str,
    end_condition: dict,
    end_condition_value,
    target_type: dict,
    target_value_one,
    target_value_two,
    description: str | None = None,
    child_step_id: int | None = None,
    exercise_name: str | None = None,
    category: str | None = None,
    weight_value: float | None = None,
    strength: bool = False,
) -> dict:
    """
    Build an ExecutableStepDTO dict.

    When `strength` is True, the step also carries Garmin's strength fields
    (exerciseName/category/weightValue/weightUnit). A weight_value of None on a
    strength step becomes the bodyweight sentinel (-1.0), matching Garmin.
    """
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": _STEP_TYPE_MAP[step_type_key],
        "childStepId": child_step_id,
        "description": description,
        "endCondition": end_condition,
        "endConditionValue": end_condition_value,
        "targetType": target_type,
        "targetValueOne": target_value_one,
        "targetValueTwo": target_value_two,
        "targetValueUnit": None,
        "zoneNumber": None,
        "secondaryTargetType": None,
        "secondaryTargetValueOne": None,
        "secondaryTargetValueTwo": None,
        "secondaryTargetValueUnit": None,
        "secondaryZoneNumber": None,
        "strokeType": _STROKE_TYPE,
        "equipmentType": _EQUIPMENT_TYPE,
    }
    if strength:
        step["category"] = category
        step["exerciseName"] = exercise_name
        step["weightValue"] = weight_value if weight_value is not None else _BODYWEIGHT_SENTINEL
        step["weightUnit"] = _WEIGHT_UNIT_KG
    return step


def _infer_end_condition(step: dict) -> tuple[dict, object]:
    """Return (endCondition dict, endConditionValue) from distance_m or duration_s."""
    if "distance_m" in step and "duration_s" in step:
        raise ValueError("step cannot specify both distance_m and duration_s; give exactly one")
    if "distance_m" in step:
        return _END_CONDITION_MAP["distance"], step["distance_m"]
    if "duration_s" in step:
        return _END_CONDITION_MAP["time"], step["duration_s"]
    return _END_CONDITION_MAP["lap_button"], 0.0


def _resolve_target(sport_type: str, step: dict, *, required: bool) -> tuple[dict, float | None, float | None]:
    """
    Resolve a step's target DTO from its input fields.

    running: pace_min_per_km/pace_max_per_km (converted to m/s) or hr_min/hr_max.
    cycling: power_watts_min/power_watts_max.

    Raises ValueError if `required` and no matching target fields are present,
    or if a running step specifies both pace and HR fields at once.
    """
    # A half-specified range used to be skipped, silently producing a step with
    # no target. Require both ends.
    for lo, hi in (("pace_min_per_km", "pace_max_per_km"), ("hr_min", "hr_max"),
                   ("power_watts_min", "power_watts_max")):
        if (lo in step) != (hi in step):
            raise ValueError(f"target range needs both {lo} and {hi}; only one was given")
    if sport_type == "running":
        if "power_watts_min" in step:
            raise ValueError("power targets are for cycling workouts, not running")
        has_pace = "pace_min_per_km" in step and "pace_max_per_km" in step
        has_hr = "hr_min" in step and "hr_max" in step
        if has_pace and has_hr:
            raise ValueError("step cannot specify both pace and HR targets")
        if has_pace:
            return _TARGET_PACE, pace_to_mps(step["pace_min_per_km"]), pace_to_mps(step["pace_max_per_km"])
        if has_hr:
            return _TARGET_HEART_RATE, step["hr_min"], step["hr_max"]
        if required:
            raise ValueError(
                "running interval/repeat step requires a target: "
                "pace_min_per_km + pace_max_per_km, or hr_min + hr_max"
            )
    else:  # cycling
        if any(k in step for k in ("pace_min_per_km", "hr_min")):
            raise ValueError("cycling steps take a power target (power_watts_min/max), not pace or HR")
        has_power = "power_watts_min" in step and "power_watts_max" in step
        if has_power:
            # Garmin convention: targetValueOne = max watts, targetValueTwo = min watts
            return _TARGET_POWER, step["power_watts_max"], step["power_watts_min"]
        if required:
            raise ValueError(
                "cycling interval/repeat step requires a target: power_watts_min + power_watts_max"
            )

    return _TARGET_NONE, None, None


def _build_plain_step(step: dict, sport_type: str, step_order: int) -> dict:
    """Build a warmup/cooldown/recovery/rest ExecutableStepDTO. Target is optional."""
    _check_fields(step, _ENDURANCE_STEP_FIELDS - {"sets", "rest_duration_s"}, f"{step['type']} step")
    end_cond, end_val = _infer_end_condition(step)
    target_type, t_one, t_two = _resolve_target(sport_type, step, required=False)
    return _make_executable_step(
        step_order=step_order,
        step_type_key=step["type"],
        end_condition=end_cond,
        end_condition_value=end_val,
        target_type=target_type,
        target_value_one=t_one,
        target_value_two=t_two,
        description=step.get("description"),
        child_step_id=None,
    )


def _build_single_interval_step(step: dict, sport_type: str, step_order: int) -> dict:
    """Build a standalone (non-repeated) interval ExecutableStepDTO. Target is required."""
    _check_fields(step, _ENDURANCE_STEP_FIELDS - {"sets", "rest_duration_s"}, "interval step")
    end_cond, end_val = _infer_end_condition(step)
    if end_cond == _END_CONDITION_MAP["lap_button"]:
        raise ValueError(f"interval step requires distance_m or duration_s for {sport_type}")
    target_type, t_one, t_two = _resolve_target(sport_type, step, required=True)
    return _make_executable_step(
        step_order=step_order,
        step_type_key="interval",
        end_condition=end_cond,
        end_condition_value=end_val,
        target_type=target_type,
        target_value_one=t_one,
        target_value_two=t_two,
        description=step.get("description"),
        child_step_id=None,
    )


def _build_repeat_group(step: dict, sport_type: str, step_order: int, child_step_id: int) -> tuple[dict, int]:
    """
    Build a RepeatGroupDTO wrapping one targeted interval step and one rest step.
    Returns (repeat_group_dto, new_step_order).
    """
    _check_fields(step, _ENDURANCE_STEP_FIELDS, "repeat step")
    sets = step.get("sets")
    if not isinstance(sets, int) or sets < 1:
        raise ValueError("repeat step requires sets as a positive integer")

    interval_end_cond, interval_end_val = _infer_end_condition(step)
    if interval_end_cond == _END_CONDITION_MAP["lap_button"]:
        raise ValueError(f"{sport_type} repeat step requires distance_m or duration_s")
    interval_target, t_one, t_two = _resolve_target(sport_type, step, required=True)

    rest_duration_s = step.get("rest_duration_s")
    if rest_duration_s and rest_duration_s > 0:
        rest_end_cond, rest_end_val = _END_CONDITION_MAP["time"], rest_duration_s
    else:
        rest_end_cond, rest_end_val = _END_CONDITION_MAP["lap_button"], 0.0

    # stepOrders are part of the flat global counter shared with sibling steps
    repeat_step_order = step_order + 1
    interval_step_order = step_order + 2
    rest_step_order = step_order + 3

    interval_inner = _make_executable_step(
        step_order=interval_step_order,
        step_type_key="interval",
        end_condition=interval_end_cond,
        end_condition_value=interval_end_val,
        target_type=interval_target,
        target_value_one=t_one,
        target_value_two=t_two,
        description=step.get("description"),
        child_step_id=child_step_id,
    )
    rest_inner = _make_executable_step(
        step_order=rest_step_order,
        step_type_key="rest",
        end_condition=rest_end_cond,
        end_condition_value=rest_end_val,
        target_type=_TARGET_NONE,
        target_value_one=None,
        target_value_two=None,
        description=None,
        child_step_id=child_step_id,
    )

    repeat_dto = {
        "type": "RepeatGroupDTO",
        "stepOrder": repeat_step_order,
        "childStepId": child_step_id,
        "stepType": _STEP_TYPE_MAP["repeat"],
        "numberOfIterations": sets,
        "endCondition": _END_CONDITION_MAP["iterations"],
        "endConditionValue": sets,
        "skipLastRestStep": False,
        "smartRepeat": False,
        "workoutSteps": [interval_inner, rest_inner],
    }
    return repeat_dto, rest_step_order


# ── Strength workout building ──────────────────────────────────────────────────
# Strength steps don't use pace/power/HR targets or distance/time end conditions.
# Each working/warm-up set is a RepeatGroupDTO (numberOfIterations = sets) wrapping
# one exercise step (interval- or warmup-typed) plus a rest step, mirroring the
# running/cycling repeat structure so the read-side decoder can be reused.

# Strength step types map to a Garmin inner step-type for the "work" step.
_STRENGTH_WORK_STEP_TYPE = {
    "warmup": "warmup",
    "cooldown": "cooldown",
    "repeat": "interval",
    "interval": "interval",
}


def _resolve_strength_load(step: dict) -> tuple[float | None, str | None]:
    """
    Resolve (weight_kg, description) for a strength set from its input fields.

    Load can be given as `weight_kg` or `weight_lb` (converted with a single
    rounding rule, see tools/weight_units.py); giving both is only accepted when
    they agree, so a get_workout_detail result (which carries both) round-trips.
    A wave is given as `set_weights` (legacy: base load defaults to the first
    entry taken as kg) or `set_weights_lb` (entries in lb; base load defaults to
    the first entry converted to kg). The wave `description` is synthesized from
    it when not given. Returns (None, description) for bodyweight exercises.
    """
    weight = step.get("weight_kg")
    description = step.get("description")
    set_weights = step.get("set_weights")
    set_weights_lb = step.get("set_weights_lb")

    if step.get("weight_lb") is not None:
        lb_kg = lb_to_kg(step["weight_lb"])
        if weight is not None and weight >= 0 and abs(weight - lb_kg) > 0.06:
            raise ValueError(
                f"weight_kg={weight} and weight_lb={step['weight_lb']} disagree "
                f"({step['weight_lb']} lb = {lb_kg} kg); give only one"
            )
        if weight is None or weight < 0:
            weight = lb_kg

    if set_weights is not None and set_weights_lb is not None:
        raise ValueError("give set_weights or set_weights_lb, not both")
    wave_lb = set_weights_lb is not None
    raw_wave = set_weights_lb if wave_lb else set_weights

    if raw_wave is not None:
        if not isinstance(raw_wave, (list, tuple)) or not raw_wave:
            raise ValueError("set_weights must be a non-empty list of numbers")
        try:
            wave = [float(w) for w in raw_wave]
        except (TypeError, ValueError):
            raise ValueError("set_weights must contain only numbers")
        if description is None:
            description = format_wave(wave)
        if weight is None:
            weight = lb_to_kg(wave[0]) if wave_lb else wave[0]

    return weight, description


def _resolve_strength_end(step: dict, where: str) -> tuple[dict, float]:
    """(endCondition, value) for a strength exercise step: reps, a timed hold, or lap button."""
    has_reps, has_time = step.get("reps") is not None, step.get("duration_s") is not None
    if has_reps and has_time:
        raise ValueError(f"{where}: give reps or duration_s, not both")
    if has_reps:
        return _END_CONDITION_MAP["reps"], float(_positive_number(step, "reps", where, integer=True))
    if has_time:
        return _END_CONDITION_MAP["time"], _positive_number(step, "duration_s", where)
    return _END_CONDITION_MAP["lap_button"], 0.0


def _build_strength_exercise_step(step: dict, step_type_key: str, step_order: int, child_step_id: int) -> dict:
    """Build the exercise ('work') ExecutableStepDTO for a strength set."""
    where = f"{step.get('type')} step {step.get('exercise_name')!r}"
    _check_fields(step, _STRENGTH_STEP_FIELDS, where)
    canonical_name, default_category = validate_exercise(step.get("exercise_name"))
    # An explicit category (e.g. from a get_workout_detail round-trip) wins over
    # the reference default, which matters for exercises listed under more than
    # one Garmin category.
    category = step.get("category") or default_category
    weight, description = _resolve_strength_load(step)
    end_cond, end_val = _resolve_strength_end(step, where)
    return _make_executable_step(
        step_order=step_order,
        step_type_key=step_type_key,
        end_condition=end_cond,
        end_condition_value=end_val,
        target_type=_TARGET_NONE,
        target_value_one=None,
        target_value_two=None,
        description=description,
        child_step_id=child_step_id,
        exercise_name=canonical_name,
        category=category,
        weight_value=weight,
        strength=True,
    )


def _build_strength_rest_step(
    step_order: int, child_step_id: int | None, duration_s: float | None = None, description: str | None = None
) -> dict:
    """Build a bodyweight rest ExecutableStepDTO (timed when duration_s is given, else lap button)."""
    if duration_s:
        end_cond, end_val = _END_CONDITION_MAP["time"], float(duration_s)
    else:
        end_cond, end_val = _END_CONDITION_MAP["lap_button"], 0.0
    return _make_executable_step(
        step_order=step_order,
        step_type_key="rest",
        end_condition=end_cond,
        end_condition_value=end_val,
        target_type=_TARGET_NONE,
        target_value_one=None,
        target_value_two=None,
        description=description,
        child_step_id=child_step_id,
        exercise_name=None,
        category=None,
        weight_value=None,
        strength=True,
    )


def _build_strength_repeat_group(step: dict, step_order: int, child_step_id: int) -> tuple[dict, int]:
    """
    Build a RepeatGroupDTO for a strength set (warmup or working set).
    Returns (repeat_group_dto, new_step_order).
    """
    sets = step.get("sets")
    if not isinstance(sets, int) or isinstance(sets, bool) or sets < 1:
        raise ValueError("strength repeat/warmup step requires sets as a positive integer")

    work_step_type = _STRENGTH_WORK_STEP_TYPE[step["type"]]

    repeat_step_order = step_order + 1
    exercise_step_order = step_order + 2
    rest_step_order = step_order + 3

    exercise_inner = _build_strength_exercise_step(step, work_step_type, exercise_step_order, child_step_id)
    rest_s = None
    if step.get("rest_duration_s") is not None:
        rest_s = _positive_number(step, "rest_duration_s", f"{step['type']} step rest")
    rest_inner = _build_strength_rest_step(rest_step_order, child_step_id, rest_s)

    repeat_dto = {
        "type": "RepeatGroupDTO",
        "stepOrder": repeat_step_order,
        "childStepId": child_step_id,
        "stepType": _STEP_TYPE_MAP["repeat"],
        "numberOfIterations": sets,
        "endCondition": _END_CONDITION_MAP["iterations"],
        "endConditionValue": sets,
        "skipLastRestStep": False,
        "smartRepeat": False,
        "workoutSteps": [exercise_inner, rest_inner],
    }
    return repeat_dto, rest_step_order


def _build_strength_steps(steps: list[dict]) -> list[dict]:
    """Build the workoutSteps list for a strength_training workout."""
    workout_steps: list[dict] = []
    step_order = 0
    child_step_id = 0

    for step in steps:
        step_type = step.get("type")

        if step_type == "rest":
            _check_fields(step, _STRENGTH_REST_FIELDS, "rest step")
            rest_s = _positive_number(step, "duration_s", "rest step") if step.get("duration_s") is not None else None
            step_order += 1
            workout_steps.append(_build_strength_rest_step(step_order, None, rest_s, step.get("description")))

        elif step_type == "interval":
            # A single set with no repeat wrapper.
            if step.get("rest_duration_s") is not None or step.get("sets") is not None:
                raise ValueError(
                    "interval step is a single set: sets/rest_duration_s apply to 'repeat' or 'warmup' steps"
                )
            step_order += 1
            workout_steps.append(_build_strength_exercise_step(step, "interval", step_order, None))

        elif step_type in ("warmup", "cooldown", "repeat"):
            child_step_id += 1
            repeat_dto, step_order = _build_strength_repeat_group(step, step_order, child_step_id)
            workout_steps.append(repeat_dto)

        else:
            raise ValueError(
                f"Unknown strength step type: {step_type!r}. "
                "Use 'warmup', 'repeat', 'interval', 'cooldown', or 'rest'."
            )

    return workout_steps


def build_workout_payload(name: str, sport_type: str, steps: list[dict]) -> dict:
    """
    Build a Garmin workout JSON payload ready for upload.

    Supports "running" (pace or heart-rate target), "cycling" (power target),
    and "strength_training" (exercise + per-set weights) — other sport types
    raise ValueError.

    Strength steps (sport_type="strength_training"):
        Each set carries a Garmin exerciseName (validated against the exercise
        reference — a typo fails fast), a per-set weight, and optional wave
        notation for ascending loads:

            Working set (one RepeatGroupDTO of `sets` reps):
                {"type": "repeat", "sets": 3,
                 "exercise_name": "BARBELL_BACK_SQUAT",  # required, validated
                 "weight_kg": 58.97,                     # base/first-set load
                 "description": "130 - 150 - 160",       # optional wave notation
                 "set_weights": [130, 150, 160]}         # optional structured wave
            Warm-up set (same shape, distinct step type):
                {"type": "warmup", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT",
                 "weight_kg": 22.7, "description": "50 - 50 - 90"}
            Standalone rest between exercises:
                {"type": "rest"}

        `description` and `set_weights` are two views of the same wave — give
        either. If only `set_weights` is given, `description` is synthesized from
        it and `weight_kg` defaults to the first set. Omit weight for bodyweight
        exercises. `category` is looked up automatically from `exercise_name`.

    Args:
        name: Workout name.
        sport_type: "running", "cycling", or "strength_training".
        steps: List of step dicts. Each step must have a "type" field.
            Running/cycling step shapes:

            Warmup/cooldown/recovery/rest step (target optional):
                {"type": "warmup"|"cooldown"|"recovery"|"rest",
                 "description": str | None,
                 "distance_m": float,           # optional — omit for lap-button end
                 "duration_s": int,             # optional — omit for lap-button end
                 "pace_min_per_km": float, "pace_max_per_km": float,  # optional, running
                 "hr_min": int, "hr_max": int,                       # optional, running
                 "power_watts_min": int, "power_watts_max": int}     # optional, cycling

            Standalone interval step — a single effort, target required:
                {"type": "interval",
                 "distance_m": float, "duration_s": int,  # exactly one required
                 "pace_min_per_km": float, "pace_max_per_km": float,  # running: pace ...
                 "hr_min": int, "hr_max": int,                        # ... or HR
                 "power_watts_min": int, "power_watts_max": int,      # cycling: power
                 "description": str | None}

            Repeat group — use this for ANY repeated effort (e.g. "3 x 400m" or
            "5 x 3min") instead of writing out multiple identical interval steps:
                {"type": "repeat", "sets": int,
                 "distance_m": float, "duration_s": int,  # exactly one required
                 "rest_duration_s": int,  # optional — omit for lap-button rest
                 "pace_min_per_km": float, "pace_max_per_km": float,  # running: pace ...
                 "hr_min": int, "hr_max": int,                        # ... or HR
                 "power_watts_min": int, "power_watts_max": int,      # cycling: power
                 "description": str | None}
                # Expands to one RepeatGroupDTO wrapping one interval + one rest step.

        Examples:
            Running, pace repeat:  {"type": "repeat", "sets": 6, "distance_m": 400,
                "rest_duration_s": 90, "pace_min_per_km": 4.5, "pace_max_per_km": 4.0}
            Running, HR repeat:    {"type": "repeat", "sets": 5, "duration_s": 180,
                "rest_duration_s": 120, "hr_min": 155, "hr_max": 170}
            Cycling, power repeat: {"type": "repeat", "sets": 4, "duration_s": 240,
                "rest_duration_s": 180, "power_watts_min": 264, "power_watts_max": 288}
    """
    if not name:
        raise ValueError("name is required")
    if not steps:
        raise ValueError("steps must not be empty")
    if sport_type not in _SPORT_TYPE_MAP:
        raise ValueError(f"Invalid sport_type: {sport_type!r}. Choose from {list(_SPORT_TYPE_MAP)}")

    sport_dto = _SPORT_TYPE_MAP[sport_type]

    if sport_type == "strength_training":
        workout_steps = _build_strength_steps(steps)
    else:
        workout_steps = []
        step_order = 0   # flat global counter
        child_step_id = 0  # increments per RepeatGroupDTO

        for step in steps:
            step_type = step.get("type")

            if step_type in ("warmup", "cooldown", "recovery", "rest"):
                step_order += 1
                workout_steps.append(_build_plain_step(step, sport_type, step_order))

            elif step_type == "interval":
                step_order += 1
                workout_steps.append(_build_single_interval_step(step, sport_type, step_order))

            elif step_type == "repeat":
                child_step_id += 1
                repeat_dto, step_order = _build_repeat_group(step, sport_type, step_order, child_step_id)
                workout_steps.append(repeat_dto)

            else:
                raise ValueError(f"Unknown step type: {step_type!r}")

    return {
        "workoutName": name,
        "description": None,
        "sportType": sport_dto,
        "subSportType": None,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport_dto,
                "workoutSteps": workout_steps,
            }
        ],
    }


def _extract_uploaded_workout_id(upload_result: dict) -> int | None:
    return (
        upload_result.get("workoutId")
        or (upload_result.get("workout") or {}).get("workoutId")
        or (upload_result.get("metadata") or {}).get("workoutId")
    )


def _norm_text(value) -> str:
    return (value or "").strip()


def _close(a, b, rel: float = 0.005, abs_tol: float = 0.011) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= max(abs_tol, rel * abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def _flatten_for_diff(steps: list[dict], out: list[dict] | None = None) -> list[dict]:
    """Depth-first list of repeat/step dicts in workout order (stepIds ignored)."""
    out = [] if out is None else out
    for st in steps or []:
        if st.get("type") == "RepeatGroupDTO":
            out.append(st)
            _flatten_for_diff(st.get("workoutSteps"), out)
        else:
            out.append(st)
    return out


def diff_workouts(requested: dict, stored: dict) -> list[str]:
    """
    Compare the workout we sent with what Garmin stored and describe every
    difference that matters (timers, reps, weights, targets, descriptions,
    set counts, exercises). Returns [] when everything survived.
    """
    warnings: list[str] = []
    if _norm_text(requested.get("workoutName")) != _norm_text(stored.get("workoutName")):
        warnings.append(f"workout name: requested {requested.get('workoutName')!r}, stored {stored.get('workoutName')!r}")
    if _norm_text(requested.get("description")) != _norm_text(stored.get("description")):
        warnings.append(
            f"workout description: requested {requested.get('description')!r}, stored {stored.get('description')!r}"
        )

    def steps_of(w):
        segs = w.get("workoutSegments") or []
        return _flatten_for_diff(segs[0].get("workoutSteps") if segs else [])

    req, got = steps_of(requested), steps_of(stored)
    if len(req) != len(got):
        warnings.append(f"step count: requested {len(req)} (incl. repeat groups), stored {len(got)}")
    for i, (r, g) in enumerate(zip(req, got), start=1):
        label = f"step {i} ({r.get('exerciseName') or _step_type_key(r)})"
        if r.get("type") != g.get("type"):
            warnings.append(f"{label}: requested {r.get('type')}, stored {g.get('type')}")
            continue
        if _step_type_key(r) != _step_type_key(g):
            warnings.append(f"{label}: step type requested {_step_type_key(r)}, stored {_step_type_key(g)}")
        if r.get("type") == "RepeatGroupDTO":
            if r.get("numberOfIterations") != g.get("numberOfIterations"):
                warnings.append(
                    f"{label}: sets requested {r.get('numberOfIterations')}, stored {g.get('numberOfIterations')}"
                )
            continue
        rk = (r.get("endCondition") or {}).get("conditionTypeKey")
        gk = (g.get("endCondition") or {}).get("conditionTypeKey")
        if rk != gk:
            warnings.append(
                f"{label}: end condition requested {rk}={r.get('endConditionValue')}, "
                f"stored {gk}={g.get('endConditionValue')}"
            )
        elif rk != "lap.button" and not _close(r.get("endConditionValue"), g.get("endConditionValue")):
            warnings.append(
                f"{label}: {rk} requested {r.get('endConditionValue')}, stored {g.get('endConditionValue')}"
            )
        rt = (r.get("targetType") or {}).get("workoutTargetTypeKey")
        gt = (g.get("targetType") or {}).get("workoutTargetTypeKey")
        if rt != gt:
            warnings.append(f"{label}: target type requested {rt}, stored {gt}")
        elif rt not in (None, "no.target"):
            for key in ("targetValueOne", "targetValueTwo"):
                if not _close(r.get(key), g.get(key)):
                    warnings.append(f"{label}: {key} ({rt}) requested {r.get(key)}, stored {g.get(key)}")
        if _norm_text(r.get("description")) != _norm_text(g.get("description")):
            warnings.append(f"{label}: description requested {r.get('description')!r}, stored {g.get('description')!r}")
        if r.get("exerciseName") or g.get("exerciseName"):
            for key in ("exerciseName", "category"):
                if r.get(key) != g.get(key):
                    warnings.append(f"{label}: {key} requested {r.get(key)!r}, stored {g.get(key)!r}")
            if not _close(r.get("weightValue"), g.get("weightValue")):
                warnings.append(f"{label}: weight requested {r.get('weightValue')} kg, stored {g.get('weightValue')} kg")
    return warnings


def verify_stored(client, workout_id, requested: dict) -> tuple[bool, list[str]]:
    """Re-fetch a workout and diff it against `requested`. Returns (verified, warnings)."""
    try:
        stored = client.get_workout_by_id(workout_id)
    except Exception as exc:  # noqa: BLE001
        return False, [f"could not re-fetch workout {workout_id} to verify it was stored as requested: {exc}"]
    return True, diff_workouts(requested, stored)


def create_workout(
    name: str,
    sport_type: Literal["running", "cycling", "strength_training"],
    steps: list[dict],
    schedule_date: str | None = None,
) -> dict:
    """
    Create a new workout and optionally schedule it.

    After upload the workout is re-fetched and compared with what was sent;
    anything Garmin did not store (timers, reps, weights, targets, descriptions)
    is reported in `warnings`. Unknown step fields are rejected up front.

    Args:
        name: Workout name.
        sport_type: "running" (pace or HR target), "cycling" (power target),
            or "strength_training" (exercise + per-set weights).
        steps: Workout steps (see build_workout_payload for schema).
        schedule_date: Optional date YYYY-MM-DD to schedule after upload.

    Returns:
        {"workout_id", "name", "sport_type", "scheduled_date",
         "warnings": [...], "verified": bool, "notes": [...]}
    """
    payload = build_workout_payload(name=name, sport_type=sport_type, steps=steps)
    client = get_client()
    upload_result = client.upload_workout(payload) or {}

    workout_id = _extract_uploaded_workout_id(upload_result)

    scheduled_date = None
    if schedule_date and workout_id is not None:
        client.schedule_workout(workout_id, schedule_date)
        scheduled_date = schedule_date

    if workout_id is not None:
        verified, warnings = verify_stored(client, workout_id, payload)
    else:
        verified, warnings = False, ["upload response carried no workout id; could not verify the stored workout"]

    notes: list[str] = []
    if sport_type == "strength_training":
        for exercise in dict.fromkeys(
            st["exerciseName"] for st in _walk_steps(payload["workoutSegments"][0]["workoutSteps"])
            if st.get("exerciseName")
        ):
            notes.extend(exercise_notes(exercise))

    return {
        "workout_id": workout_id,
        "name": name,
        "sport_type": sport_type,
        "scheduled_date": scheduled_date,
        "warnings": warnings,
        "verified": verified,
        "notes": notes,
    }


def delete_workout(workout_id: int) -> dict:
    """Delete a saved workout by ID."""
    client = get_client()
    client.delete_workout(workout_id)
    return {"status": "ok", "workout_id": workout_id}


# ── Workout detail decoding ────────────────────────────────────────────────────
# Inverse of build_workout_payload/_make_executable_step: turns Garmin's raw
# step DTOs back into the same schema build_workout_payload's `steps` accepts.

def _decode_end_condition(step: dict) -> dict:
    """Return {'distance_m': ...} or {'duration_s': ...}, or {} for lap-button/none."""
    cond_key = (step.get("endCondition") or {}).get("conditionTypeKey")
    value = step.get("endConditionValue")
    if cond_key == "distance":
        return {"distance_m": value}
    if cond_key == "time":
        return {"duration_s": value}
    if cond_key == "reps":
        return {"reps": int(value)}
    return {}


def _decode_target(step: dict) -> dict:
    """Return pace/power/hr target fields based on targetType, or {} for no target."""
    target_key = (step.get("targetType") or {}).get("workoutTargetTypeKey")
    t_one = step.get("targetValueOne")
    t_two = step.get("targetValueTwo")
    if target_key == "pace.zone":
        return {"pace_min_per_km": mps_to_pace(t_one), "pace_max_per_km": mps_to_pace(t_two)}
    if target_key == "power.zone":
        # Garmin convention: targetValueOne = max watts, targetValueTwo = min watts
        return {"power_watts_max": t_one, "power_watts_min": t_two}
    if target_key == "heart.rate.zone":
        # Garmin convention: targetValueOne = min bpm, targetValueTwo = max bpm
        return {"hr_min": t_one, "hr_max": t_two}
    return {}


def _step_type_key(step: dict) -> str | None:
    return (step.get("stepType") or {}).get("stepTypeKey")


def _decode_executable_step(step: dict) -> dict:
    """Decode a single ExecutableStepDTO into the create_workout step-input schema."""
    out: dict = {"type": _step_type_key(step)}
    if step.get("description"):
        out["description"] = step["description"]
    out.update(_decode_end_condition(step))
    if step.get("exerciseName"):
        out["exercise_name"] = step["exerciseName"]
        if step.get("category"):
            out["category"] = step["category"]
        if "weightValue" in step:
            out["weight_kg"] = step["weightValue"]
            if step["weightValue"] is not None and step["weightValue"] >= 0:
                out["weight_lb"] = kg_to_lb(step["weightValue"])
        # Bug 1: expose the wave notation carried in `description` as structured
        # per-set weights, instead of leaving it as a string to re-parse.
        set_weights = parse_wave(step.get("description"))
        if set_weights is not None:
            out["set_weights"] = set_weights
    out.update(_decode_target(step))
    return out


def _decode_repeat_group(step: dict) -> dict:
    """
    Decode a RepeatGroupDTO into the create_workout step-input schema.

    The inner "work" step may be interval-typed (running/cycling reps, or a
    strength working set) or warmup/cooldown-typed (a strength warm-up wave).
    Bug 2: a warm-up wave is a repeat group whose work step is warmup-typed —
    keying only on "interval" left it an empty shell with no exercise/weight/
    wave data, so match the first non-rest step and carry its type through.
    """
    inner = step.get("workoutSteps") or []
    rest = next((s for s in inner if _step_type_key(s) == "rest"), None)
    work = next((s for s in inner if _step_type_key(s) not in (None, "rest")), None)

    work_key = _step_type_key(work) if work is not None else "interval"
    # interval reps read back as "repeat"; warmup/cooldown keep their own type.
    out_type = "repeat" if work_key == "interval" else work_key
    out: dict = {"type": out_type, "sets": step.get("numberOfIterations")}
    if work is not None:
        decoded = _decode_executable_step(work)
        decoded.pop("type", None)
        out.update(decoded)
    if rest is not None:
        rest_decoded = _decode_end_condition(rest)
        if "duration_s" in rest_decoded:
            out["rest_duration_s"] = rest_decoded["duration_s"]
    return out


def _decode_steps(steps: list[dict]) -> list[dict]:
    """Decode a top-level workout steps list (ExecutableStepDTO/RepeatGroupDTO)."""
    decoded = []
    for step in steps:
        if step.get("type") == "RepeatGroupDTO":
            decoded.append(_decode_repeat_group(step))
        elif step.get("type") == "ExecutableStepDTO":
            decoded.append(_decode_executable_step(step))
    return decoded


def get_workout_detail(workout_id: int) -> dict:
    """
    Get full step-by-step detail for a saved workout by ID.

    Returns the workout name/sport/description plus a decoded steps list in
    the same shape build_workout_payload's `steps` argument accepts (warmup/
    interval/cooldown/recovery/rest and repeat groups, with distance/duration,
    pace/power/HR targets, and strength exercise/weight fields decoded back to
    readable values) — so the result can be reused directly as input to
    create_workout to clone or modify a workout.

    Args:
        workout_id: Garmin workout ID (from get_saved_workouts).
    """
    client = get_client()
    workout = client.get_workout_by_id(workout_id)

    segments = workout.get("workoutSegments") or []
    steps = segments[0].get("workoutSteps") if segments else []

    return {
        "workout_id": workout.get("workoutId"),
        "workout_name": workout.get("workoutName"),
        "sport_type": (workout.get("sportType") or {}).get("sportTypeKey"),
        "description": workout.get("description"),
        "steps": _decode_steps(steps or []),
    }


# ── Weight update helpers ─────────────────────────────────────────────────────

def _walk_steps(steps: list[dict]):
    """Recursively yield all ExecutableStepDTO dicts, descending into RepeatGroupDTOs."""
    for step in steps:
        if step.get("type") == "RepeatGroupDTO":
            yield from _walk_steps(step.get("workoutSteps") or [])
        elif step.get("type") == "ExecutableStepDTO":
            yield step


def _put_workout(client, workout_id: int, workout: dict) -> None:
    """
    Update a saved workout in place (PUT /workout-service/workout/{id}).

    Verified against the live API: the workoutId is preserved and calendar
    schedule entries keep pointing at it, so plan links never break. The
    garminconnect library has no wrapper for this call, so go through its
    underlying session. Step ids are left in place (Garmin accepts them).
    """
    client.client.put("connectapi", f"/workout-service/workout/{int(workout_id)}", json=workout, api=True)


def _work_steps(workout: dict):
    """
    Yield (container, work_step, rest_step) for every strength exercise step in
    workout order. `container` is the RepeatGroupDTO (or the step itself when it
    stands alone); `rest_step` is the group's rest child, or None.
    """
    segments = workout.get("workoutSegments") or []
    for top in (segments[0].get("workoutSteps") if segments else None) or []:
        if top.get("type") == "RepeatGroupDTO":
            inner = top.get("workoutSteps") or []
            work = next((s for s in inner if s.get("exerciseName")), None)
            rest = next((s for s in inner if _step_type_key(s) == "rest"), None)
            if work is not None:
                yield top, work, rest
        elif top.get("exerciseName"):
            yield top, top, None


def _apply_load_update(step: dict, update) -> dict:
    """Apply a weight/description update to a raw step in place; return {} or what changed."""
    if not isinstance(update, dict):
        update = {"weight_kg": update}
    unknown = sorted(set(update) - {"weight_kg", "weight_lb", "description", "set_weights", "set_weights_lb"})
    if unknown:
        raise ValueError(f"unknown weight update field(s) {unknown}")
    weight, description = _resolve_strength_load(update)
    applied: dict = {}
    if weight is not None:
        applied["old_kg"] = step.get("weightValue")
        applied["new_kg"] = weight
        applied["old_lb"] = kg_to_lb(step["weightValue"]) if (step.get("weightValue") or -1) >= 0 else None
        applied["new_lb"] = kg_to_lb(weight)
        step["weightValue"] = weight
    if description is not None:
        applied["old_description"] = step.get("description")
        applied["new_description"] = description
        step["description"] = description
    return applied


def update_workout_weights(
    workout_name: str,
    weight_updates: dict[str, float | dict],
    workout_description: str | None = None,
    include_warmup: bool = False,
    warmup_updates: dict[str, float | dict] | None = None,
) -> dict:
    """
    Update exercise weights (and optionally per-set notes) in a strength
    workout by name, IN PLACE: the workout keeps its Garmin ID, so calendar
    schedule entries and plan links stay valid.

    Finds the workout in the saved library, updates weightValue and/or the
    per-exercise description text for matching exercises, optionally replaces
    the workout-level description, saves it with an in-place update, then
    re-fetches it and reports anything that was not stored.

    By default only working ("interval"/repeat) steps change. Set
    include_warmup=True to apply `weight_updates` to warm-up steps as well, or
    give `warmup_updates` (same shape) to change warm-ups independently of
    working sets.

    Args:
        workout_name: Exact workout name as it appears in Garmin Connect.
        weight_updates: Mapping of exerciseName to either:
            - a plain number (kg), for a weight-only update (backward compatible)
              e.g. {"OVERHEAD_BARBELL_PRESS": 32.5}
            - a dict with optional "weight_kg" or "weight_lb" and/or
              "description" (or "set_weights"/"set_weights_lb" for a wave)
              e.g. {"BARBELL_BACK_SQUAT": {"weight_lb": 125,
                                            "description": "115 - 125 - 135"}}
            Omitting a key leaves that field unchanged (no accidental blanking).
        workout_description: Optional new workout-level description (e.g.
            "Next weight progression day: July 30"). Omit to leave unchanged.
        include_warmup: Also apply weight_updates to warm-up steps.
        warmup_updates: Updates applied only to warm-up steps.

    Returns:
        {"workout_name", "workout_id", "old_id", "new_id" (== old_id; kept for
         compatibility), "id_changed": False,
         "updates_applied": {exerciseName | "EXERCISE (warmup)": {"old_kg", "new_kg",
             "old_lb", "new_lb", "old_description", "new_description"}},
         "workout_description": {"old", "new"} | None,
         "warnings": [...], "verified": bool}
    """
    client = get_client()
    workouts = client.get_workouts(start=0, limit=999) or []
    match = next((w for w in workouts if w.get("workoutName") == workout_name), None)
    if match is None:
        raise ValueError(f"Workout not found: {workout_name!r}")

    workout_id: int = match["workoutId"]
    updated = copy.deepcopy(client.get_workout_by_id(workout_id))
    warmup_updates = warmup_updates or {}

    updates_applied: dict[str, dict] = {}
    matched: set[tuple[str, str]] = set()
    for _container, work, _rest in _work_steps(updated):
        kind = _step_type_key(work)
        exercise = work.get("exerciseName")
        if kind == "interval":
            update, label, bucket = weight_updates.get(exercise), exercise, "work"
        elif kind == "warmup":
            update = warmup_updates.get(exercise)
            if update is None and include_warmup:
                update = weight_updates.get(exercise)
            label, bucket = f"{exercise} (warmup)", "warmup"
        else:
            continue
        if update is None:
            continue
        matched.add((bucket, exercise))
        applied = _apply_load_update(work, update)
        if applied:
            updates_applied[label] = applied

    warnings: list[str] = []
    for ex in weight_updates:
        if ("work", ex) not in matched and not (include_warmup and ("warmup", ex) in matched):
            warnings.append(f"{ex}: no matching step in {workout_name!r} was updated")
    for ex in warmup_updates:
        if ("warmup", ex) not in matched:
            warnings.append(f"{ex}: no matching warm-up step in {workout_name!r} was updated")

    workout_description_applied = None
    if workout_description is not None:
        workout_description_applied = {"old": updated.get("description"), "new": workout_description}
        updated["description"] = workout_description

    _put_workout(client, workout_id, updated)
    verified, diff = verify_stored(client, workout_id, updated)

    return {
        "workout_name": workout_name,
        "workout_id": workout_id,
        "old_id": workout_id,
        "new_id": workout_id,
        "id_changed": False,
        "updates_applied": updates_applied,
        "workout_description": workout_description_applied,
        "warnings": warnings + diff,
        "verified": verified,
    }


_PATCH_FIELDS = frozenset({
    "sets", "reps", "duration_s", "rest_duration_s",
    "weight_kg", "weight_lb", "set_weights", "set_weights_lb", "description",
})


def _set_end_condition(step: dict, key: str, value) -> None:
    """Point a raw step at reps/time (value given) or the lap button (value None)."""
    if value is None:
        step["endCondition"], step["endConditionValue"] = _END_CONDITION_MAP["lap_button"], 0.0
    else:
        step["endCondition"], step["endConditionValue"] = _END_CONDITION_MAP[key], float(value)


def patch_workout_step(
    workout_id: int,
    exercise_name: str,
    occurrence: int = 1,
    fields: dict | None = None,
    step_type: str | None = None,
) -> dict:
    """
    Change one step of a saved strength workout in place (same workout ID), without
    rebuilding the rest of the workout.

    `exercise_name` + `occurrence` pick the step: occurrence=2 is the second step
    for that exercise in workout order (a warm-up block counts, so with a warm-up
    first, the working block is occurrence=2 — or pass step_type="repeat" /
    "warmup" to count only that kind).

    Args:
        workout_id: Garmin workout ID.
        exercise_name: Garmin exercise name (see search_exercises).
        occurrence: 1-based occurrence among matching steps.
        fields: What to change; any of
            sets (int), reps (int | None), duration_s (seconds for a hold | None),
            rest_duration_s (timed rest | None for lap-button rest),
            weight_kg | weight_lb (number | None for bodyweight),
            set_weights | set_weights_lb (per-set wave; rewrites description),
            description (str | None).
            None on reps/duration_s reverts that step to an open (lap button) step.
            Setting reps replaces a timer and vice versa; do not pass both.
        step_type: Optional filter: "warmup", "repeat" (working sets) or "cooldown".

    Examples:
        {"duration_s": 40}                       # hold side plank for 40 s
        {"reps": 8, "weight_lb": 135}            # 8 reps at 135 lb
        {"sets": 4, "rest_duration_s": 90}

    Returns:
        {"workout_id", "workout_name", "matched": {...}, "changes": {field: {"old", "new"}},
         "step": <decoded step>, "warnings": [...], "verified": bool, "notes": [...]}
    """
    fields = dict(fields or {})
    if not fields:
        raise ValueError("fields must name at least one thing to change")
    unknown = sorted(set(fields) - _PATCH_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s) {unknown}. Allowed: {sorted(_PATCH_FIELDS)}")
    if fields.get("reps") is not None and fields.get("duration_s") is not None:
        raise ValueError("give reps or duration_s, not both")
    if not isinstance(occurrence, int) or occurrence < 1:
        raise ValueError("occurrence must be a positive integer")

    client = get_client()
    original = client.get_workout_by_id(workout_id)
    updated = copy.deepcopy(original)
    wanted = validate_exercise_name_for_patch(exercise_name)

    candidates = [
        (c, w, r) for c, w, r in _work_steps(updated)
        if w.get("exerciseName") == wanted
        and (step_type is None or (_step_type_key(w) == ("interval" if step_type == "repeat" else step_type)))
    ]
    if len(candidates) < occurrence:
        present = [
            f"{w.get('exerciseName')} ({'repeat' if _step_type_key(w) == 'interval' else _step_type_key(w)})"
            for _c, w, _r in _work_steps(updated)
        ]
        raise ValueError(
            f"No occurrence #{occurrence} of {wanted} in workout {workout_id}"
            f"{f' with step_type={step_type!r}' if step_type else ''}. "
            f"Exercises in this workout, in order: {present}"
        )
    container, work, rest = candidates[occurrence - 1]
    in_group = container is not work

    changes: dict[str, dict] = {}

    def record(name, old, new):
        changes[name] = {"old": old, "new": new}

    if "sets" in fields:
        if not in_group:
            raise ValueError("this step is a single set (not a repeat group); sets cannot be changed")
        sets = fields["sets"]
        if not isinstance(sets, int) or isinstance(sets, bool) or sets < 1:
            raise ValueError("sets must be a positive integer")
        record("sets", container.get("numberOfIterations"), sets)
        container["numberOfIterations"] = sets
        container["endConditionValue"] = sets

    old_end = _decode_end_condition(work)
    for key in ("reps", "duration_s"):
        if key in fields:
            value = fields[key]
            if value is not None:
                value = _positive_number(fields, key, "patch", integer=(key == "reps"))
            _set_end_condition(work, "reps" if key == "reps" else "time", value)
            record(key, old_end.get(key), value)
            other = "duration_s" if key == "reps" else "reps"
            if value is not None and other not in fields and old_end.get(other) is not None:
                record(other, old_end[other], None)  # a rep target replaces a timer and vice versa

    if "rest_duration_s" in fields:
        if rest is None:
            raise ValueError("this step has no rest step to change (it is not in a repeat group)")
        value = fields["rest_duration_s"]
        if value:
            value = _positive_number(fields, "rest_duration_s", "patch")
        old = _decode_end_condition(rest).get("duration_s")
        _set_end_condition(rest, "time", value or None)
        record("rest_duration_s", old, value or None)

    load_keys = {k: fields[k] for k in ("weight_kg", "weight_lb", "set_weights", "set_weights_lb") if k in fields}
    if load_keys or "description" in fields:
        old_w, old_d = work.get("weightValue"), work.get("description")
        weight, description = _resolve_strength_load({**load_keys, **({"description": fields["description"]}
                                                                      if fields.get("description") is not None else {})})
        if "weight_kg" in load_keys and fields["weight_kg"] is None and "weight_lb" not in load_keys:
            weight = _BODYWEIGHT_SENTINEL
        if weight is not None:
            work["weightValue"] = weight
            record("weight_kg", old_w, weight)
            record("weight_lb", kg_to_lb(old_w) if old_w is not None and old_w >= 0 else None, kg_to_lb(weight) if weight >= 0 else None)
        if "description" in fields and fields["description"] is None:
            description = None
            work["description"] = None
            record("description", old_d, None)
        elif description is not None:
            work["description"] = description
            record("description", old_d, description)

    _put_workout(client, workout_id, updated)
    verified, warnings = verify_stored(client, workout_id, updated)

    return {
        "workout_id": workout_id,
        "workout_name": updated.get("workoutName"),
        "matched": {"exercise_name": wanted, "occurrence": occurrence,
                    "step_type": "repeat" if _step_type_key(work) == "interval" else _step_type_key(work)},
        "changes": changes,
        "step": (_decode_steps([container]) or [None])[0],
        "warnings": warnings,
        "verified": verified,
        "notes": exercise_notes(wanted),
    }


def validate_exercise_name_for_patch(exercise_name: str) -> str:
    """Normalise an exercise name for matching; unknown names raise with 'did you mean' suggestions."""
    canonical, _category = validate_exercise(exercise_name)
    return canonical


