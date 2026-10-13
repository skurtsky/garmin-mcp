# tools/strength_sets.py
"""Actual per-set data for strength activities, and planned-vs-actual comparison.

Garmin's ``/activity-service/activity/{id}/exerciseSets`` returns one entry per
set (``setType`` ACTIVE or REST) with the recognised exercise, reps, weight in
grams and duration in seconds. ``wktStepIndex`` is the index of the planned
workout step the set was recorded against, counted the way the FIT workout
counts them: every executable step takes one index, and a repeat group takes one
more index *after* its children (so Strength A's first warm-up squat is step 1,
its rest 2, the repeat marker 3, and the working squat is step 4).
"""
from garmin_client import get_client
from tools.weight_units import grams_to_kg, kg_to_lb

_WEIGHT_TOL_KG = 0.5   # watch records the nearest plate-friendly load


def _planned_step_index(workout: dict) -> dict[int, dict]:
    """Map FIT step index -> {"step": raw step, "iterations": n, "in_group": bool}."""
    segments = workout.get("workoutSegments") or []
    steps = segments[0].get("workoutSteps") if segments else []
    out: dict[int, dict] = {}
    counter = [0]

    def walk(items, iterations=1, in_group=False):
        for st in items or []:
            if st.get("type") == "RepeatGroupDTO":
                n = st.get("numberOfIterations") or 1
                walk(st.get("workoutSteps"), n, True)
                counter[0] += 1  # the repeat marker itself
            else:
                out[counter[0]] = {"step": st, "iterations": iterations, "in_group": in_group}
                counter[0] += 1

    walk(steps)
    return out


def _step_type(step: dict) -> str | None:
    return (step.get("stepType") or {}).get("stepTypeKey")


def parse_exercise_sets(raw: dict, planned: dict[int, dict] | None = None) -> list[dict]:
    """Normalise a raw exerciseSets response into an ordered list of sets."""
    sets: list[dict] = []
    for i, s in enumerate((raw or {}).get("exerciseSets") or [], start=1):
        ex = (s.get("exercises") or [None])[0] or {}
        raw_type = (s.get("setType") or "").upper()
        set_type = raw_type.lower() or None
        step_idx = s.get("wktStepIndex")
        if planned and step_idx in planned and _step_type(planned[step_idx]["step"]) == "warmup" \
                and raw_type == "ACTIVE":
            set_type = "warm-up"
        weight = s.get("weight")
        sets.append({
            "order": i,
            "set_type": set_type,
            "exercise_name": ex.get("name"),
            "category": ex.get("category"),
            "reps": s.get("repetitionCount"),
            "weight_kg": grams_to_kg(weight) if weight is not None else None,
            "weight_lb": kg_to_lb(weight / 1000.0) if weight is not None else None,
            "duration_s": round(s["duration"], 1) if s.get("duration") is not None else None,
            "start_time": s.get("startTime"),
            "workout_step_index": step_idx,
        })
    return sets


def summarize_sets(sets: list[dict]) -> list[dict]:
    """Compact per-exercise summary of the working (non-rest) sets."""
    out: dict[tuple, dict] = {}
    for s in sets:
        if s["set_type"] == "rest" or not s["exercise_name"]:
            continue
        e = out.setdefault((s["exercise_name"], s["category"]), {
            "exercise_name": s["exercise_name"], "category": s["category"],
            "sets": 0, "warmup_sets": 0, "total_reps": 0, "total_time_s": 0.0,
            "top_weight_kg": None, "top_weight_lb": None, "volume_kg": 0.0,
        })
        if s["set_type"] == "warm-up":
            e["warmup_sets"] += 1
        else:
            e["sets"] += 1
        e["total_reps"] += s["reps"] or 0
        e["total_time_s"] = round(e["total_time_s"] + (s["duration_s"] or 0), 1)
        if s["weight_kg"] is not None:
            if e["top_weight_kg"] is None or s["weight_kg"] > e["top_weight_kg"]:
                e["top_weight_kg"], e["top_weight_lb"] = s["weight_kg"], s["weight_lb"]
            e["volume_kg"] = round(e["volume_kg"] + s["weight_kg"] * (s["reps"] or 0), 1)
    return list(out.values())


def _fetch(client, activity_id: int, workout_id: int | None):
    raw = client.get_activity_exercise_sets(activity_id)
    planned = _planned_step_index(client.get_workout_by_id(workout_id)) if workout_id else None
    return raw, planned


def strength_sets(activity_id: int, workout_id: int | None = None) -> dict:
    """
    Per-set data for a strength activity: exercise (category + name), reps,
    weight (kg and lb), duration, set type and order.

    Args:
        activity_id: Garmin activity ID of a strength session.
        workout_id: Optional saved workout the session followed. When given,
            warm-up sets (recorded by the watch as ACTIVE) are labelled
            "warm-up" using the workout's step types.

    Returns:
        {"activity_id", "sets": [{"order", "set_type" ("warm-up"|"active"|"rest"),
         "exercise_name", "category", "reps", "weight_kg", "weight_lb",
         "duration_s", "start_time", "workout_step_index"}], "exercises": [...]}
    """
    client = get_client()
    raw, planned = _fetch(client, activity_id, workout_id)
    sets = parse_exercise_sets(raw, planned)
    return {"activity_id": activity_id, "sets": sets, "exercises": summarize_sets(sets)}


def activity_exercises_summary(activity_id: int, client=None) -> list[dict]:
    """Compact exercises summary for activity_detail (empty on any failure)."""
    try:
        client = client or get_client()
        return summarize_sets(parse_exercise_sets(client.get_activity_exercise_sets(activity_id)))
    except Exception:  # noqa: BLE001 — a missing summary must not break activity_detail
        return []


def compare_strength_session(activity_id: int, workout_id: int) -> dict:
    """
    Planned vs actual sets for a strength activity against a saved workout.

    Sets are matched through the watch's step index, so the same exercise
    appearing as a warm-up and a working block is compared block by block.
    Planned load per set comes from the step's wave (set_weights in the
    description) when present, otherwise the step weight.
    """
    from tools.workout import parse_wave  # local import: workout imports weight_units only

    client = get_client()
    raw, planned = _fetch(client, activity_id, workout_id)
    actual = [s for s in parse_exercise_sets(raw, planned) if s["set_type"] != "rest"]

    seen: dict[int, int] = {}
    rows: list[dict] = []
    for s in actual:
        idx = s["workout_step_index"]
        n = seen[idx] = seen.get(idx, 0) + 1
        plan = planned.get(idx)
        row = {"order": s["order"], "exercise_name": s["exercise_name"], "set_type": s["set_type"],
               "actual": {"reps": s["reps"], "weight_kg": s["weight_kg"], "weight_lb": s["weight_lb"],
                          "duration_s": s["duration_s"]}}
        if plan is None:
            row["planned"], row["flags"] = None, ["not in planned workout"]
        else:
            st = plan["step"]
            wave = parse_wave(st.get("description"))
            p_kg = st.get("weightValue")
            if wave and len(wave) >= n:
                p_kg_lb = wave[n - 1]
                p_kg = round(p_kg_lb * 0.45359237, 2)
            if p_kg is not None and p_kg < 0:
                p_kg = None
            cond = (st.get("endCondition") or {}).get("conditionTypeKey")
            p_reps = st.get("endConditionValue") if cond == "reps" else None
            p_dur = st.get("endConditionValue") if cond == "time" else None
            row["planned"] = {
                "exercise_name": st.get("exerciseName"), "set_number": n, "of": plan["iterations"],
                "reps": int(p_reps) if p_reps else None, "duration_s": p_dur,
                "weight_kg": p_kg, "weight_lb": kg_to_lb(p_kg) if p_kg is not None else None,
            }
            flags = []
            if st.get("exerciseName") != s["exercise_name"]:
                flags.append(f"exercise differs (planned {st.get('exerciseName')})")
            if p_kg is not None and s["weight_kg"] is not None and abs(p_kg - s["weight_kg"]) > _WEIGHT_TOL_KG:
                flags.append(f"load {s['weight_lb']} lb vs planned {kg_to_lb(p_kg)} lb")
            if p_reps and s["reps"] is not None and s["reps"] != int(p_reps):
                flags.append(f"reps {s['reps']} vs planned {int(p_reps)}")
            if p_dur and s["duration_s"] is not None and abs(s["duration_s"] - p_dur) > 3:
                flags.append(f"time {s['duration_s']}s vs planned {p_dur:g}s")
            row["flags"] = flags
        rows.append(row)

    # planned work steps with fewer actual sets than iterations
    missed = []
    for idx, plan in planned.items():
        st = plan["step"]
        if not plan["in_group"] or not st.get("exerciseName"):
            continue
        done = seen.get(idx, 0)
        if done < plan["iterations"]:
            missed.append({"exercise_name": st["exerciseName"], "step_type": _step_type(st),
                           "planned_sets": plan["iterations"], "completed_sets": done})
    return {"activity_id": activity_id, "workout_id": workout_id, "sets": rows,
            "missed": missed, "sets_with_flags": sum(1 for r in rows if r["flags"])}
