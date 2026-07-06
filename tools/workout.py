# tools/workout.py
import copy
from datetime import date

from garmin_client import get_client


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

_SPORT_TYPE_MAP = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "road_biking": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "cardio": {"sportTypeId": 3, "sportTypeKey": "cardio", "displayOrder": 3},
    "strength_training": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
}

_WEIGHT_UNIT = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
_STROKE_TYPE = {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0}
_EQUIPMENT_TYPE = {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0}


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


def _iter_months(months_ahead: int) -> list[tuple[int, int]]:
    """Return [(year, month)] from current month through months_ahead inclusive."""
    if months_ahead < 0:
        raise ValueError("months_ahead must be >= 0")

    start = date.today()
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
    today_iso = date.today().isoformat()

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
    category: str | None = None,
    exercise_name: str | None = None,
    weight_value: float | None = None,
) -> dict:
    """Build an ExecutableStepDTO dict."""
    step: dict = {
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
    if category is not None:
        step["category"] = category
    if exercise_name is not None:
        step["exerciseName"] = exercise_name
    if weight_value is not None:
        step["weightValue"] = weight_value
        step["weightUnit"] = _WEIGHT_UNIT
    return step


def _infer_cardio_end_condition(step: dict) -> tuple[dict, object]:
    """Return (endCondition dict, endConditionValue) from distance_m or duration_s."""
    if "distance_m" in step:
        return _END_CONDITION_MAP["distance"], step["distance_m"]
    if "duration_s" in step:
        return _END_CONDITION_MAP["time"], step["duration_s"]
    return _END_CONDITION_MAP["lap_button"], 0.0


def build_workout_payload(name: str, sport_type: str, steps: list[dict]) -> dict:
    """
    Build a Garmin workout JSON payload ready for upload.

    Args:
        name: Workout name.
        sport_type: Sport type key — "running", "cycling", "strength_training", "cardio".
        steps: List of step dicts. Each step must have a "type" field:

            Warmup/cooldown/recovery/rest step:
                {"type": "warmup"|"cooldown"|"recovery"|"rest",
                 "description": str | None,
                 "distance_m": float,   # optional, cardio only
                 "duration_s": int,     # optional, cardio only
                 "category": str,       # optional, strength
                 "exercise_name": str,  # optional, strength
                 "weight_kg": float}    # optional, strength

            Interval step (strength):
                {"type": "interval",
                 "category": str, "exercise_name": str, "weight_kg": float,
                 "description": str | None}

            Interval step (running — pace target):
                {"type": "interval",
                 "distance_m": float | None, "duration_s": int | None,
                 "pace_min_per_km": float, "pace_max_per_km": float,
                 "description": str | None}

            Interval step (cycling — power target):
                {"type": "interval",
                 "duration_s": int | None, "distance_m": float | None,
                 "power_watts_min": int, "power_watts_max": int,
                 "description": str | None}

            Repeat group (strength sets):
                {"type": "repeat", "sets": int,
                 "category": str, "exercise_name": str, "weight_kg": float,
                 "description": str | None}

            Repeat group (cycling — power target):
                {"type": "repeat", "sets": 4,
                 "duration_s": 240, "rest_duration_s": 180,
                 "power_watts_min": 264, "power_watts_max": 288,
                 "description": "VO2max interval"}

            Repeat group (running — pace target):
                {"type": "repeat", "sets": 6,
                 "distance_m": 400, "rest_duration_s": 90,
                 "pace_min_per_km": 4.5, "pace_max_per_km": 4.0,
                 "description": "400m rep"}

            Repeat group (running — HR target):
                {"type": "repeat", "sets": 5,
                 "duration_s": 180, "rest_duration_s": 120,
                 "hr_min": 155, "hr_max": 170,
                 "description": "Tempo interval"}
                # All repeat variants expand to RepeatGroupDTO wrapping interval + rest steps.
    """
    if not name:
        raise ValueError("name is required")
    if not steps:
        raise ValueError("steps must not be empty")
    if sport_type not in _SPORT_TYPE_MAP:
        raise ValueError(f"Invalid sport_type: {sport_type!r}. Choose from {list(_SPORT_TYPE_MAP)}")

    sport_dto = _SPORT_TYPE_MAP[sport_type]
    is_strength = sport_type == "strength_training"
    workout_steps: list[dict] = []
    step_order = 0   # flat global counter
    child_step_id = 0  # increments per RepeatGroupDTO

    for step in steps:
        step_type = step.get("type")
        description = step.get("description")

        if step_type in ("warmup", "cooldown", "recovery", "rest"):
            step_order += 1
            if is_strength:
                end_cond = _END_CONDITION_MAP["lap_button"]
                end_val = 0.0
            else:
                end_cond, end_val = _infer_cardio_end_condition(step)

            workout_steps.append(_make_executable_step(
                step_order=step_order,
                step_type_key=step_type,
                end_condition=end_cond,
                end_condition_value=end_val,
                target_type=_TARGET_NONE,
                target_value_one=None,
                target_value_two=None,
                description=description,
                child_step_id=None,
                category=step.get("category"),
                exercise_name=step.get("exercise_name"),
                weight_value=step.get("weight_kg"),
            ))

        elif step_type == "interval":
            step_order += 1

            if is_strength:
                end_cond = _END_CONDITION_MAP["lap_button"]
                end_val = 0.0
                target_type = _TARGET_NONE
                t_one = None
                t_two = None
            elif sport_type == "running":
                end_cond, end_val = _infer_cardio_end_condition(step)
                if end_cond == _END_CONDITION_MAP["lap_button"]:
                    raise ValueError("interval step requires distance_m or duration_s for running")
                if "pace_min_per_km" in step and "pace_max_per_km" in step:
                    target_type = _TARGET_PACE
                    # targetValueOne = slow end (lower m/s), targetValueTwo = fast end (higher m/s)
                    t_one = pace_to_mps(step["pace_min_per_km"])
                    t_two = pace_to_mps(step["pace_max_per_km"])
                else:
                    target_type = _TARGET_NONE
                    t_one = None
                    t_two = None
            else:  # cycling / cardio
                end_cond, end_val = _infer_cardio_end_condition(step)
                if end_cond == _END_CONDITION_MAP["lap_button"]:
                    raise ValueError("interval step requires distance_m or duration_s for cycling/cardio")
                if "power_watts_min" in step and "power_watts_max" in step:
                    target_type = _TARGET_POWER
                    t_one = step["power_watts_max"]
                    t_two = step["power_watts_min"]
                else:
                    target_type = _TARGET_NONE
                    t_one = None
                    t_two = None

            workout_steps.append(_make_executable_step(
                step_order=step_order,
                step_type_key="interval",
                end_condition=end_cond,
                end_condition_value=end_val,
                target_type=target_type,
                target_value_one=t_one,
                target_value_two=t_two,
                description=description,
                child_step_id=None,
                category=step.get("category"),
                exercise_name=step.get("exercise_name"),
                weight_value=step.get("weight_kg"),
            ))

        elif step_type == "repeat":
            child_step_id += 1
            sets = step.get("sets")
            if not isinstance(sets, int) or sets < 1:
                raise ValueError("repeat step requires sets as a positive integer")

            # Determine interval end condition and target based on sport
            if is_strength:
                interval_end_cond = _END_CONDITION_MAP["lap_button"]
                interval_end_val = 0.0
                interval_target = _TARGET_NONE
                t_one = None
                t_two = None
                interval_category = step.get("category")
                interval_exercise = step.get("exercise_name")
                interval_weight = step.get("weight_kg")
            elif sport_type == "cycling" and "power_watts_min" in step:
                interval_end_cond, interval_end_val = _infer_cardio_end_condition(step)
                if interval_end_cond == _END_CONDITION_MAP["lap_button"]:
                    raise ValueError("cycling repeat step requires distance_m or duration_s")
                interval_target = _TARGET_POWER
                t_one = step["power_watts_max"]
                t_two = step["power_watts_min"]
                interval_category = None
                interval_exercise = None
                interval_weight = None
            elif sport_type == "running" and "pace_min_per_km" in step:
                interval_end_cond, interval_end_val = _infer_cardio_end_condition(step)
                if interval_end_cond == _END_CONDITION_MAP["lap_button"]:
                    raise ValueError("running repeat step requires distance_m or duration_s")
                interval_target = _TARGET_PACE
                t_one = pace_to_mps(step["pace_min_per_km"])
                t_two = pace_to_mps(step["pace_max_per_km"])
                interval_category = None
                interval_exercise = None
                interval_weight = None
            elif sport_type == "running" and "hr_min" in step:
                interval_end_cond, interval_end_val = _infer_cardio_end_condition(step)
                if interval_end_cond == _END_CONDITION_MAP["lap_button"]:
                    raise ValueError("running repeat step requires distance_m or duration_s")
                interval_target = _TARGET_HEART_RATE
                t_one = step["hr_min"]
                t_two = step["hr_max"]
                interval_category = None
                interval_exercise = None
                interval_weight = None
            else:
                raise ValueError(
                    f"repeat step missing required target fields for sport_type {sport_type!r}"
                )

            # Determine rest end condition
            rest_duration_s = step.get("rest_duration_s")
            if rest_duration_s and rest_duration_s > 0:
                rest_end_cond = _END_CONDITION_MAP["time"]
                rest_end_val = rest_duration_s
            else:
                rest_end_cond = _END_CONDITION_MAP["lap_button"]
                rest_end_val = 0.0

            # Build inner steps — stepOrders are part of the flat global counter
            repeat_step_order = step_order + 1
            interval_step_order = step_order + 2
            rest_step_order = step_order + 3
            step_order = rest_step_order  # advance global counter past all three

            interval_inner = _make_executable_step(
                step_order=interval_step_order,
                step_type_key="interval",
                end_condition=interval_end_cond,
                end_condition_value=interval_end_val,
                target_type=interval_target,
                target_value_one=t_one,
                target_value_two=t_two,
                description=description,
                child_step_id=child_step_id,
                category=interval_category,
                exercise_name=interval_exercise,
                weight_value=interval_weight,
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

            workout_steps.append({
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
            })

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


def create_workout(
    name: str,
    sport_type: str,
    steps: list[dict],
    schedule_date: str | None = None,
) -> dict:
    """
    Create a new workout and optionally schedule it.

    Args:
        name: Workout name.
        sport_type: Sport type key — "running", "cycling", "strength_training", "cardio".
        steps: Workout steps (see build_workout_payload for schema).
        schedule_date: Optional date YYYY-MM-DD to schedule after upload.
    """
    client = get_client()
    payload = build_workout_payload(name=name, sport_type=sport_type, steps=steps)
    upload_result = client.upload_workout(payload) or {}

    workout_id = _extract_uploaded_workout_id(upload_result)

    scheduled_date = None
    if schedule_date and workout_id is not None:
        client.schedule_workout(workout_id, schedule_date)
        scheduled_date = schedule_date

    return {
        "workout_id": workout_id,
        "name": name,
        "sport_type": sport_type,
        "scheduled_date": scheduled_date,
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


def _decode_executable_step(step: dict) -> dict:
    """Decode a single ExecutableStepDTO into the create_workout step-input schema."""
    out: dict = {"type": (step.get("stepType") or {}).get("stepTypeKey")}
    if step.get("description"):
        out["description"] = step["description"]
    out.update(_decode_end_condition(step))
    if step.get("exerciseName"):
        out["exercise_name"] = step["exerciseName"]
        if step.get("category"):
            out["category"] = step["category"]
        if "weightValue" in step:
            out["weight_kg"] = step["weightValue"]
    out.update(_decode_target(step))
    return out


def _decode_repeat_group(step: dict) -> dict:
    """Decode a RepeatGroupDTO into the create_workout 'repeat' step-input schema."""
    inner = step.get("workoutSteps") or []
    interval = next((s for s in inner if (s.get("stepType") or {}).get("stepTypeKey") == "interval"), None)
    rest = next((s for s in inner if (s.get("stepType") or {}).get("stepTypeKey") == "rest"), None)

    out: dict = {"type": "repeat", "sets": step.get("numberOfIterations")}
    if interval is not None:
        decoded = _decode_executable_step(interval)
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


def _strip_server_fields(workout: dict) -> None:
    """Remove server-assigned fields from a workout dict in place."""
    for field in (
        "workoutId", "ownerId", "author", "createdDate", "updatedDate",
        "uploadTimestamp", "workoutProvider", "workoutSourceId",
    ):
        workout.pop(field, None)


def _strip_step_ids(steps: list[dict]) -> None:
    """Recursively remove stepId from all steps in place."""
    for step in steps:
        step.pop("stepId", None)
        if step.get("type") == "RepeatGroupDTO":
            _strip_step_ids(step.get("workoutSteps") or [])


def update_workout_weights(workout_name: str, weight_updates: dict[str, float]) -> dict:
    """
    Update exercise weights in a strength workout by name.

    Finds the workout in the saved library, updates weightValue for matching
    exercises (interval steps only — warmup steps are left unchanged), uploads
    as a new workout, and deletes the old one.

    Args:
        workout_name: Exact workout name as it appears in Garmin Connect.
        weight_updates: Mapping of exerciseName → new weight in kg.
            e.g. {"BARBELL_BACK_SQUAT": 105.0, "OVERHEAD_BARBELL_PRESS": 32.5}

    Returns:
        {"workout_name", "old_id", "new_id",
         "updates_applied": {exerciseName: {"old_kg": float, "new_kg": float}}}
    """
    client = get_client()
    workouts = client.get_workouts(start=0, limit=999) or []
    match = next((w for w in workouts if w.get("workoutName") == workout_name), None)
    if match is None:
        raise ValueError(f"Workout not found: {workout_name!r}")

    old_id: int = match["workoutId"]
    full_workout = client.get_workout_by_id(old_id)

    updated = copy.deepcopy(full_workout)
    _strip_server_fields(updated)
    _strip_step_ids(updated["workoutSegments"][0]["workoutSteps"])

    updates_applied: dict[str, dict] = {}
    for step in _walk_steps(updated["workoutSegments"][0]["workoutSteps"]):
        if (step.get("stepType") or {}).get("stepTypeKey") != "interval":
            continue
        exercise = step.get("exerciseName")
        if exercise in weight_updates:
            old_kg = step["weightValue"]
            step["weightValue"] = weight_updates[exercise]
            updates_applied[exercise] = {"old_kg": old_kg, "new_kg": weight_updates[exercise]}

    upload_result = client.upload_workout(updated) or {}
    new_id = _extract_uploaded_workout_id(upload_result)
    client.delete_workout(old_id)

    return {
        "workout_name": workout_name,
        "old_id": old_id,
        "new_id": new_id,
        "updates_applied": updates_applied,
    }

