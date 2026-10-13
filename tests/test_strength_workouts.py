import pytest
from unittest.mock import patch

from tests.workout_fakes import FakeGarmin, drop_end_conditions, drop_targets
from tools.workout import (
    build_workout_payload,
    create_workout,
    diff_workouts,
    get_workout_detail,
    patch_workout_step,
    update_workout_weights,
)


def _strength(steps):
    return build_workout_payload("TEST strength", "strength_training", steps)


def _work(payload, i=0):
    """The exercise step inside the i-th top-level repeat group."""
    return payload["workoutSegments"][0]["workoutSteps"][i]["workoutSteps"][0]


def _rest(payload, i=0):
    return payload["workoutSegments"][0]["workoutSteps"][i]["workoutSteps"][1]


# ── building: timers, reps, rest ─────────────────────────────────────────────

def test_timed_hold_sets_time_end_condition_and_timed_rest():
    p = _strength([{"type": "repeat", "sets": 6, "exercise_name": "SIDE_PLANK",
                    "duration_s": 30, "rest_duration_s": 15}])
    assert _work(p)["endCondition"]["conditionTypeKey"] == "time"
    assert _work(p)["endConditionValue"] == 30.0
    assert _rest(p)["endCondition"]["conditionTypeKey"] == "time"
    assert _rest(p)["endConditionValue"] == 15.0


def test_reps_sets_rep_end_condition():
    p = _strength([{"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP", "reps": 10}])
    assert _work(p)["endCondition"]["conditionTypeKey"] == "reps"
    assert _work(p)["endConditionValue"] == 10.0
    assert _rest(p)["endCondition"]["conditionTypeKey"] == "lap.button"


def test_no_end_condition_stays_open_step():
    p = _strength([{"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP"}])
    assert _work(p)["endCondition"]["conditionTypeKey"] == "lap.button"


def test_reps_and_duration_together_rejected():
    with pytest.raises(ValueError, match="reps or duration_s"):
        _strength([{"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP", "reps": 10, "duration_s": 30}])


@pytest.mark.parametrize("bad", [0, -5, 2.5, "10", True])
def test_bad_reps_rejected(bad):
    with pytest.raises(ValueError, match="reps"):
        _strength([{"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP", "reps": bad}])


def test_standalone_timed_rest():
    p = _strength([{"type": "rest", "duration_s": 90}])
    step = p["workoutSegments"][0]["workoutSteps"][0]
    assert step["endCondition"]["conditionTypeKey"] == "time" and step["endConditionValue"] == 90.0


def test_interval_with_sets_rejected():
    with pytest.raises(ValueError, match="single set"):
        _strength([{"type": "interval", "exercise_name": "PUSH_UP", "sets": 3}])


# ── fail loudly ──────────────────────────────────────────────────────────────

def test_unknown_strength_field_rejected_with_name():
    with pytest.raises(ValueError, match=r"unknown step field.*durationn_s"):
        _strength([{"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP", "durationn_s": 30}])


def test_unknown_field_on_strength_rest_rejected():
    with pytest.raises(ValueError, match="unknown step field"):
        _strength([{"type": "rest", "exercise_name": "PUSH_UP"}])


def test_unknown_field_on_running_step_rejected():
    with pytest.raises(ValueError, match="unknown step field"):
        build_workout_payload("t", "running", [{"type": "warmup", "duration_s": 60, "cadence": 180}])


def test_half_specified_pace_range_rejected_instead_of_dropped():
    with pytest.raises(ValueError, match="pace_min_per_km and pace_max_per_km"):
        build_workout_payload("t", "running", [
            {"type": "repeat", "sets": 3, "distance_m": 400, "pace_min_per_km": 4.5}])


def test_half_specified_power_range_rejected():
    with pytest.raises(ValueError, match="power_watts_min and power_watts_max"):
        build_workout_payload("t", "cycling", [
            {"type": "interval", "duration_s": 300, "power_watts_min": 250}])


def test_distance_and_duration_together_rejected():
    with pytest.raises(ValueError, match="both distance_m and duration_s"):
        build_workout_payload("t", "running", [
            {"type": "warmup", "distance_m": 1000, "duration_s": 300}])


def test_unknown_exercise_error_has_suggestions():
    with pytest.raises(ValueError, match="Did you mean.*SIDE_PLANK"):
        _strength([{"type": "repeat", "sets": 2, "exercise_name": "SIDE_PLANKK"}])


# ── pounds ───────────────────────────────────────────────────────────────────

def test_weight_lb_converted_with_two_decimal_rounding():
    p = _strength([{"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT", "weight_lb": 115}])
    assert _work(p)["weightValue"] == 52.16


def test_weight_kg_and_lb_must_agree():
    with pytest.raises(ValueError, match="disagree"):
        _strength([{"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT",
                    "weight_kg": 60, "weight_lb": 115}])
    p = _strength([{"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT",
                    "weight_kg": 52.16, "weight_lb": 115}])
    assert _work(p)["weightValue"] == 52.16


def test_set_weights_lb_sets_description_and_base_load():
    p = _strength([{"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT",
                    "set_weights_lb": [115, 125, 135]}])
    assert _work(p)["description"] == "115 - 125 - 135"
    assert _work(p)["weightValue"] == 52.16


def test_set_weights_and_set_weights_lb_conflict():
    with pytest.raises(ValueError, match="not both"):
        _strength([{"type": "repeat", "sets": 2, "exercise_name": "BARBELL_BACK_SQUAT",
                    "set_weights": [1, 2], "set_weights_lb": [1, 2]}])


# ── create: verification ─────────────────────────────────────────────────────

STEPS = [
    {"type": "repeat", "sets": 6, "exercise_name": "SIDE_PLANK", "duration_s": 30, "rest_duration_s": 15},
    {"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP", "reps": 10},
]


def test_create_workout_clean_round_trip_has_no_warnings():
    fake = FakeGarmin()
    with patch("tools.workout.get_client", return_value=fake):
        res = create_workout("TEST hold", "strength_training", STEPS)
    assert res["verified"] is True and res["warnings"] == []


def test_create_workout_reports_silently_dropped_timer_and_reps():
    fake = FakeGarmin(transform=drop_end_conditions)
    with patch("tools.workout.get_client", return_value=fake):
        res = create_workout("TEST hold", "strength_training", STEPS)
    assert res["verified"] is True
    text = "\n".join(res["warnings"])
    assert "SIDE_PLANK" in text and "time=30.0" in text and "lap.button" in text
    assert "PUSH_UP" in text and "reps=10.0" in text


def test_create_workout_reports_dropped_pace_target():
    fake = FakeGarmin(transform=drop_targets)
    with patch("tools.workout.get_client", return_value=fake):
        res = create_workout("TEST run", "running", [
            {"type": "repeat", "sets": 3, "distance_m": 400, "pace_min_per_km": 5.0, "pace_max_per_km": 4.5}])
    assert any("target type requested pace.zone, stored no.target" in w for w in res["warnings"])


def test_create_workout_reports_dropped_power_target():
    fake = FakeGarmin(transform=drop_targets)
    with patch("tools.workout.get_client", return_value=fake):
        res = create_workout("TEST ride", "cycling", [
            {"type": "interval", "duration_s": 300, "power_watts_min": 250, "power_watts_max": 280}])
    assert any("power.zone" in w for w in res["warnings"])


def test_create_workout_reports_dropped_hr_target_values():
    requested = build_workout_payload("t", "running", [
        {"type": "interval", "duration_s": 300, "hr_min": 150, "hr_max": 165}])
    stored = build_workout_payload("t", "running", [
        {"type": "interval", "duration_s": 300, "hr_min": 150, "hr_max": 160}])
    assert any("targetValueTwo" in w for w in diff_workouts(requested, stored))


def test_create_workout_notes_flag_workouts_with_no_garmin_page():
    fake = FakeGarmin()
    with patch("tools.workout.get_client", return_value=fake):
        res = create_workout("TEST", "strength_training",
                             [{"type": "repeat", "sets": 2, "exercise_name": "WEIGHTED_SIDE_PLANK", "duration_s": 30}])
    assert res["warnings"] == []
    assert any("WEIGHTED_SIDE_PLANK" in n and "SIDE_PLANK" in n for n in res["notes"])


def test_create_workout_refetch_failure_is_a_warning_not_a_crash():
    fake = FakeGarmin()
    fake.get_workout_by_id = lambda _id: (_ for _ in ()).throw(RuntimeError("boom"))
    with patch("tools.workout.get_client", return_value=fake):
        res = create_workout("TEST", "strength_training", STEPS)
    assert res["verified"] is False and "boom" in res["warnings"][0]


# ── get_workout_detail round trip ────────────────────────────────────────────

def test_detail_returns_reps_time_rest_and_both_units_and_round_trips():
    fake = FakeGarmin()
    steps = [
        {"type": "warmup", "sets": 2, "exercise_name": "BARBELL_BACK_SQUAT", "weight_lb": 50},
        {"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT", "weight_lb": 115,
         "reps": 8, "rest_duration_s": 120},
        {"type": "repeat", "sets": 6, "exercise_name": "SIDE_PLANK", "duration_s": 30},
        {"type": "rest", "duration_s": 60},
    ]
    with patch("tools.workout.get_client", return_value=fake):
        wid = create_workout("TEST rt", "strength_training", steps)["workout_id"]
        detail = get_workout_detail(wid)
        squat = detail["steps"][1]
        assert squat["reps"] == 8 and squat["rest_duration_s"] == 120
        assert squat["weight_kg"] == 52.16 and squat["weight_lb"] == 115.0
        assert detail["steps"][2]["duration_s"] == 30.0
        assert detail["steps"][3]["duration_s"] == 60.0
        # decoded detail can be fed straight back into create_workout
        again = create_workout("TEST rt2", "strength_training", detail["steps"])
    assert again["warnings"] == []
    assert fake.workouts[again["workout_id"]]["workoutSegments"] == \
        [{**fake.workouts[wid]["workoutSegments"][0]}]


# ── in-place update ──────────────────────────────────────────────────────────

SQUAT_DAY = [
    {"type": "warmup", "sets": 2, "exercise_name": "BARBELL_BACK_SQUAT", "weight_lb": 50, "description": "50 - 90"},
    {"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT", "weight_lb": 115,
     "description": "115 - 125 - 135"},
]


def _fake_with(steps, name="TEST squat day"):
    fake = FakeGarmin()
    with patch("tools.workout.get_client", return_value=fake):
        wid = create_workout(name, "strength_training", steps)["workout_id"]
    fake.schedule_workout(wid, "2027-01-05")
    return fake, wid


def test_update_workout_weights_keeps_id_and_schedule():
    fake, wid = _fake_with(SQUAT_DAY)
    with patch("tools.workout.get_client", return_value=fake):
        res = update_workout_weights("TEST squat day", {"BARBELL_BACK_SQUAT": {"weight_lb": 125}})
    assert res["old_id"] == res["new_id"] == res["workout_id"] == wid and res["id_changed"] is False
    assert fake.uploads == 1 and fake.deletes == []           # only the original create
    assert list(fake.workouts) == [wid]
    assert fake.schedule == [{"workoutId": wid, "date": "2027-01-05"}]   # calendar entry still points at wid
    assert res["warnings"] == [] and res["verified"] is True
    assert res["updates_applied"]["BARBELL_BACK_SQUAT"]["new_lb"] == 125.0
    top = fake.workouts[wid]["workoutSegments"][0]["workoutSteps"]
    assert top[1]["workoutSteps"][0]["weightValue"] == 56.7
    assert top[0]["workoutSteps"][0]["weightValue"] == 22.68   # warm-up untouched


def test_update_workout_weights_include_warmup():
    fake, wid = _fake_with(SQUAT_DAY)
    with patch("tools.workout.get_client", return_value=fake):
        res = update_workout_weights("TEST squat day", {"BARBELL_BACK_SQUAT": {"weight_lb": 60}},
                                     include_warmup=True)
    assert set(res["updates_applied"]) == {"BARBELL_BACK_SQUAT", "BARBELL_BACK_SQUAT (warmup)"}
    top = fake.workouts[wid]["workoutSegments"][0]["workoutSteps"]
    assert top[0]["workoutSteps"][0]["weightValue"] == 27.22
    assert top[1]["workoutSteps"][0]["weightValue"] == 27.22


def test_update_workout_weights_separate_warmup_updates():
    fake, wid = _fake_with(SQUAT_DAY)
    with patch("tools.workout.get_client", return_value=fake):
        update_workout_weights("TEST squat day", {"BARBELL_BACK_SQUAT": {"weight_lb": 125}},
                               warmup_updates={"BARBELL_BACK_SQUAT": {"weight_lb": 55, "description": "55 - 95"}})
    top = fake.workouts[wid]["workoutSegments"][0]["workoutSteps"]
    assert top[0]["workoutSteps"][0]["weightValue"] == 24.95
    assert top[0]["workoutSteps"][0]["description"] == "55 - 95"
    assert top[1]["workoutSteps"][0]["weightValue"] == 56.7


def test_update_workout_weights_warns_about_unmatched_exercise():
    fake, _ = _fake_with(SQUAT_DAY)
    with patch("tools.workout.get_client", return_value=fake):
        res = update_workout_weights("TEST squat day", {"BENCH_PRESS": 50})
    assert any("BENCH_PRESS" in w for w in res["warnings"])


def test_update_workout_weights_surfaces_silent_drop_after_put():
    fake, wid = _fake_with(SQUAT_DAY)
    fake.transform = lambda w: _revert_weights(w)
    with patch("tools.workout.get_client", return_value=fake):
        res = update_workout_weights("TEST squat day", {"BARBELL_BACK_SQUAT": {"weight_lb": 125}})
    assert any("weight requested 56.7 kg, stored 52.16 kg" in w for w in res["warnings"])


def _revert_weights(w):
    for st in w["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"]:
        if st.get("exerciseName"):
            st["weightValue"] = 52.16
    return w


# ── patch_workout_step ───────────────────────────────────────────────────────

def _patch(fake, wid, *a, **kw):
    with patch("tools.workout.get_client", return_value=fake):
        return patch_workout_step(wid, *a, **kw)


def _steps(fake, wid):
    return fake.workouts[wid]["workoutSegments"][0]["workoutSteps"]


def test_patch_adds_timer_to_open_step():
    fake, wid = _fake_with([{"type": "repeat", "sets": 6, "exercise_name": "SIDE_PLANK"}])
    res = _patch(fake, wid, "SIDE_PLANK", fields={"duration_s": 45})
    work = _steps(fake, wid)[0]["workoutSteps"][0]
    assert work["endCondition"]["conditionTypeKey"] == "time" and work["endConditionValue"] == 45.0
    assert res["changes"]["duration_s"] == {"old": None, "new": 45.0}
    assert res["step"]["duration_s"] == 45.0 and res["warnings"] == [] and res["verified"] is True
    assert fake.client.put_calls[0][0] == wid          # in-place PUT, no new workout
    assert fake.uploads == 1


def test_patch_reps_replaces_timer():
    fake, wid = _fake_with([{"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP", "duration_s": 30}])
    res = _patch(fake, wid, "PUSH_UP", fields={"reps": 12})
    work = _steps(fake, wid)[0]["workoutSteps"][0]
    assert work["endCondition"]["conditionTypeKey"] == "reps" and work["endConditionValue"] == 12.0
    assert res["changes"]["duration_s"] == {"old": 30.0, "new": None}


def test_patch_none_reverts_to_open_step():
    fake, wid = _fake_with([{"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP", "reps": 10}])
    _patch(fake, wid, "PUSH_UP", fields={"reps": None})
    assert _steps(fake, wid)[0]["workoutSteps"][0]["endCondition"]["conditionTypeKey"] == "lap.button"


def test_patch_occurrence_and_step_type_pick_warmup_or_working_block():
    fake, wid = _fake_with(SQUAT_DAY)
    _patch(fake, wid, "BARBELL_BACK_SQUAT", occurrence=2, fields={"reps": 8, "weight_lb": 135})
    top = _steps(fake, wid)
    assert top[1]["workoutSteps"][0]["weightValue"] == 61.23
    assert top[0]["workoutSteps"][0]["weightValue"] == 22.68
    _patch(fake, wid, "BARBELL_BACK_SQUAT", step_type="warmup", fields={"sets": 3, "weight_lb": 55})
    top = _steps(fake, wid)
    assert top[0]["numberOfIterations"] == 3 and top[0]["endConditionValue"] == 3
    assert top[0]["workoutSteps"][0]["weightValue"] == 24.95
    _patch(fake, wid, "BARBELL_BACK_SQUAT", step_type="repeat", fields={"rest_duration_s": 90})
    rest = _steps(fake, wid)[1]["workoutSteps"][1]
    assert rest["endCondition"]["conditionTypeKey"] == "time" and rest["endConditionValue"] == 90.0


def test_patch_set_weights_lb_rewrites_description_and_base_weight():
    fake, wid = _fake_with(SQUAT_DAY)
    _patch(fake, wid, "BARBELL_BACK_SQUAT", step_type="repeat", fields={"set_weights_lb": [120, 130, 140]})
    work = _steps(fake, wid)[1]["workoutSteps"][0]
    assert work["description"] == "120 - 130 - 140" and work["weightValue"] == 54.43


def test_patch_clear_weight_means_bodyweight():
    fake, wid = _fake_with(SQUAT_DAY)
    _patch(fake, wid, "BARBELL_BACK_SQUAT", step_type="repeat", fields={"weight_kg": None})
    assert _steps(fake, wid)[1]["workoutSteps"][0]["weightValue"] == -1.0


def test_patch_missing_occurrence_lists_what_is_there():
    fake, wid = _fake_with(SQUAT_DAY)
    with pytest.raises(ValueError, match=r"occurrence #3.*BARBELL_BACK_SQUAT \(warmup\)"):
        _patch(fake, wid, "BARBELL_BACK_SQUAT", occurrence=3, fields={"reps": 5})


def test_patch_unknown_exercise_suggests():
    fake, wid = _fake_with(SQUAT_DAY)
    with pytest.raises(ValueError, match="Did you mean"):
        _patch(fake, wid, "BARBELL_BACK_SQUATT", fields={"reps": 5})


def test_patch_rejects_unknown_fields_and_both_end_conditions():
    fake, wid = _fake_with(SQUAT_DAY)
    with pytest.raises(ValueError, match="unknown field"):
        _patch(fake, wid, "BARBELL_BACK_SQUAT", fields={"tempo": 3})
    with pytest.raises(ValueError, match="not both"):
        _patch(fake, wid, "BARBELL_BACK_SQUAT", fields={"reps": 3, "duration_s": 30})
    with pytest.raises(ValueError, match="at least one"):
        _patch(fake, wid, "BARBELL_BACK_SQUAT", fields={})
    assert fake.client.put_calls == []                 # nothing was written


def test_patch_reports_silently_dropped_timer():
    fake, wid = _fake_with([{"type": "repeat", "sets": 6, "exercise_name": "SIDE_PLANK"}])
    fake.transform = drop_end_conditions
    res = _patch(fake, wid, "SIDE_PLANK", fields={"duration_s": 30})
    assert any("time=30.0" in w for w in res["warnings"])
