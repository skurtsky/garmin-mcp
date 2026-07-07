# tests/test_workout.py
from unittest.mock import MagicMock, patch

from tools.workout import (
    pace_to_mps,
    get_saved_workouts,
    get_scheduled_workouts,
    get_workout_detail,
    build_workout_payload,
    create_workout,
    update_workout_weights,
)


def test_pace_to_mps_mmss():
    assert pace_to_mps("4:10") == 4.0
    assert pace_to_mps("6:00/km") == 2.778


def test_pace_to_mps_decimal():
    assert pace_to_mps(6.0) == 2.778
    assert pace_to_mps(4.1666667) == 4.0


def test_get_saved_workouts_returns_list(client):
    workouts = get_saved_workouts(sport_type="running")
    assert isinstance(workouts, list)
    if workouts:
        item = workouts[0]
        assert "workout_id" in item
        assert "workout_name" in item
        assert "sport_type" in item


def test_get_scheduled_workouts_returns_list(client):
    items = get_scheduled_workouts(months_ahead=1)
    assert isinstance(items, list)
    if items:
        item = items[0]
        assert "schedule_id" in item
        assert "workout_id" in item
        assert "date" in item


def test_build_payload_contains_steps():
    payload = build_workout_payload(
        name="TEST - Workout Payload",
        sport_type="running",
        steps=[
            {
                "type": "warmup",
                "description": "Easy start",
                "distance_m": 1000,
                "pace_min_per_km": 6.0,
                "pace_max_per_km": 5.5,
            },
            {
                "type": "interval",
                "description": "On",
                "duration_s": 180,
                "pace_min_per_km": 4.5,
                "pace_max_per_km": 4.1,
            },
        ],
    )

    assert payload["workoutName"] == "TEST - Workout Payload"
    assert payload["sportType"]["sportTypeKey"] == "running"
    assert len(payload["workoutSegments"]) == 1
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 2
    assert steps[0]["type"] == "ExecutableStepDTO"
    assert steps[0]["stepOrder"] == 1
    assert steps[1]["stepOrder"] == 2
    assert steps[1]["targetType"]["workoutTargetTypeKey"] == "pace.zone"


def test_build_payload_warmup_pace_target_is_applied():
    """A warmup step with pace fields must get a real pace target, not be silently dropped."""
    payload = build_workout_payload(
        name="TEST - Warmup Target",
        sport_type="running",
        steps=[
            {
                "type": "warmup",
                "distance_m": 1000,
                "pace_min_per_km": 6.0,
                "pace_max_per_km": 5.5,
            },
        ],
    )
    warmup = payload["workoutSegments"][0]["workoutSteps"][0]
    assert warmup["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert warmup["targetValueOne"] == pace_to_mps(6.0)
    assert warmup["targetValueTwo"] == pace_to_mps(5.5)


def test_build_payload_warmup_without_target_is_no_target():
    payload = build_workout_payload(
        name="TEST - Warmup No Target",
        sport_type="running",
        steps=[{"type": "warmup", "distance_m": 1000}],
    )
    warmup = payload["workoutSegments"][0]["workoutSteps"][0]
    assert warmup["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_build_payload_rejects_unsupported_sport_type():
    for sport_type in ("strength_training", "cardio", "swimming"):
        try:
            build_workout_payload(
                name="TEST", sport_type=sport_type,
                steps=[{"type": "interval", "duration_s": 60}],
            )
            assert False, f"expected ValueError for sport_type={sport_type!r}"
        except ValueError:
            pass


def test_build_payload_interval_requires_target():
    try:
        build_workout_payload(
            name="TEST - No Target",
            sport_type="running",
            steps=[{"type": "interval", "duration_s": 180}],
        )
        assert False, "expected ValueError for interval without a target"
    except ValueError as e:
        assert "target" in str(e)


def test_build_payload_cycling_interval_requires_target():
    try:
        build_workout_payload(
            name="TEST - No Target",
            sport_type="cycling",
            steps=[{"type": "interval", "duration_s": 180}],
        )
        assert False, "expected ValueError for cycling interval without a power target"
    except ValueError as e:
        assert "power" in str(e)


def test_build_payload_running_step_rejects_both_pace_and_hr():
    try:
        build_workout_payload(
            name="TEST - Conflicting Targets",
            sport_type="running",
            steps=[{
                "type": "interval", "duration_s": 180,
                "pace_min_per_km": 4.5, "pace_max_per_km": 4.1,
                "hr_min": 150, "hr_max": 165,
            }],
        )
        assert False, "expected ValueError for both pace and HR targets"
    except ValueError as e:
        assert "both" in str(e)


def test_build_payload_interval_requires_end_condition():
    try:
        build_workout_payload(
            name="TEST - No End Condition",
            sport_type="running",
            steps=[{"type": "interval", "pace_min_per_km": 4.5, "pace_max_per_km": 4.1}],
        )
        assert False, "expected ValueError for interval without distance_m/duration_s"
    except ValueError as e:
        assert "distance_m or duration_s" in str(e)


def test_build_payload_repeat_requires_target():
    try:
        build_workout_payload(
            name="TEST - Repeat No Target",
            sport_type="cycling",
            steps=[{"type": "repeat", "sets": 4, "duration_s": 240}],
        )
        assert False, "expected ValueError for repeat step without a target"
    except ValueError as e:
        assert "power" in str(e)


def test_build_payload_repeat_requires_positive_sets():
    try:
        build_workout_payload(
            name="TEST - Bad Sets",
            sport_type="running",
            steps=[{
                "type": "repeat", "sets": 0, "duration_s": 180,
                "hr_min": 150, "hr_max": 165,
            }],
        )
        assert False, "expected ValueError for sets < 1"
    except ValueError as e:
        assert "sets" in str(e)


def test_create_workout_rejects_unsupported_sport_type_without_network_call():
    """Validation must happen before touching the network — get_client should never be called."""
    with patch("tools.workout.get_client") as mock_get_client:
        try:
            create_workout(
                name="TEST", sport_type="strength_training",
                steps=[{"type": "interval", "duration_s": 60}],
            )
            assert False, "expected ValueError for unsupported sport_type"
        except ValueError:
            pass
        mock_get_client.assert_not_called()


def test_create_workout_uploads_and_schedules():
    mock_client = MagicMock()
    mock_client.upload_workout.return_value = {"workoutId": 4242}

    with patch("tools.workout.get_client", return_value=mock_client):
        result = create_workout(
            name="TEST - 6x400",
            sport_type="running",
            steps=[{
                "type": "repeat", "sets": 6, "distance_m": 400, "rest_duration_s": 90,
                "pace_min_per_km": 5.0, "pace_max_per_km": 4.5,
            }],
            schedule_date="2026-08-01",
        )

    uploaded_payload = mock_client.upload_workout.call_args[0][0]
    assert uploaded_payload["sportType"]["sportTypeKey"] == "running"
    assert len(uploaded_payload["workoutSegments"][0]["workoutSteps"]) == 1

    mock_client.schedule_workout.assert_called_once_with(4242, "2026-08-01")
    assert result == {
        "workout_id": 4242,
        "name": "TEST - 6x400",
        "sport_type": "running",
        "scheduled_date": "2026-08-01",
    }


def test_create_workout_without_schedule_date_does_not_schedule():
    mock_client = MagicMock()
    mock_client.upload_workout.return_value = {"workoutId": 7}

    with patch("tools.workout.get_client", return_value=mock_client):
        result = create_workout(
            name="TEST - Easy Run",
            sport_type="running",
            steps=[{"type": "interval", "duration_s": 1800, "hr_min": 130, "hr_max": 150}],
        )

    mock_client.schedule_workout.assert_not_called()
    assert result["scheduled_date"] is None


def test_build_payload_cycling_power():
    payload = build_workout_payload(
        name="TEST - Cycling Power",
        sport_type="cycling",
        steps=[
            {
                "type": "interval",
                "description": "Threshold effort",
                "duration_s": 480,
                "power_watts_min": 250,
                "power_watts_max": 280,
            },
        ],
    )

    assert payload["sportType"]["sportTypeKey"] == "cycling"
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 1

    step = steps[0]
    assert step["targetType"]["workoutTargetTypeKey"] == "power.zone"
    assert step["targetValueOne"] == 280   # max watts
    assert step["targetValueTwo"] == 250   # min watts
    assert step["endCondition"]["conditionTypeKey"] == "time"
    assert step["endConditionValue"] == 480


def test_update_workout_weights():
    old_workout = {
        "workoutId": 123,
        "workoutName": "Strength - A",
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "workoutSteps": [
                    # warmup — should NOT be updated
                    {
                        "type": "ExecutableStepDTO",
                        "stepId": 10,
                        "stepOrder": 1,
                        "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
                        "exerciseName": "BARBELL_BACK_SQUAT",
                        "weightValue": 40.0,
                    },
                    # repeat group containing an interval step
                    {
                        "type": "RepeatGroupDTO",
                        "stepId": 20,
                        "stepOrder": 2,
                        "workoutSteps": [
                            {
                                "type": "ExecutableStepDTO",
                                "stepId": 30,
                                "stepOrder": 3,
                                "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
                                "exerciseName": "BARBELL_BACK_SQUAT",
                                "weightValue": 100.0,
                            },
                            {
                                "type": "ExecutableStepDTO",
                                "stepId": 40,
                                "stepOrder": 4,
                                "stepType": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
                                "exerciseName": None,
                                "weightValue": -1.0,
                            },
                        ],
                    },
                ],
            }
        ],
    }

    mock_client = MagicMock()
    mock_client.get_workouts.return_value = [
        {"workoutId": 123, "workoutName": "Strength - A"}
    ]
    mock_client.get_workout_by_id.return_value = old_workout
    mock_client.upload_workout.return_value = {"workoutId": 456}

    with patch("tools.workout.get_client", return_value=mock_client):
        result = update_workout_weights(
            workout_name="Strength - A",
            weight_updates={"BARBELL_BACK_SQUAT": 105.0},
        )

    assert result["workout_name"] == "Strength - A"
    assert result["old_id"] == 123
    assert result["new_id"] == 456
    assert result["updates_applied"] == {
        "BARBELL_BACK_SQUAT": {"old_kg": 100.0, "new_kg": 105.0}
    }

    # delete called on old ID
    mock_client.delete_workout.assert_called_once_with(123)

    # verify warmup weight was NOT modified in the uploaded payload
    uploaded_payload = mock_client.upload_workout.call_args[0][0]
    top_steps = uploaded_payload["workoutSegments"][0]["workoutSteps"]
    warmup_step = top_steps[0]
    assert warmup_step["weightValue"] == 40.0   # unchanged
    # stepIds stripped
    assert "stepId" not in warmup_step


def test_build_payload_cycling_repeat_power():
    payload = build_workout_payload(
        name="TEST - Cycling Repeat Power",
        sport_type="cycling",
        steps=[
            {
                "type": "repeat",
                "sets": 4,
                "duration_s": 240,
                "rest_duration_s": 180,
                "power_watts_min": 264,
                "power_watts_max": 288,
                "description": "VO2max interval",
            },
        ],
    )

    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 1

    repeat = steps[0]
    assert repeat["type"] == "RepeatGroupDTO"
    assert repeat["numberOfIterations"] == 4
    assert repeat["stepOrder"] == 1

    interval = repeat["workoutSteps"][0]
    assert interval["targetType"]["workoutTargetTypeId"] == 2  # power
    assert interval["targetValueOne"] == 288
    assert interval["targetValueTwo"] == 264
    assert interval["endCondition"]["conditionTypeId"] == 2  # time
    assert interval["endConditionValue"] == 240
    assert interval["stepOrder"] == 2

    rest = repeat["workoutSteps"][1]
    assert rest["endCondition"]["conditionTypeId"] == 2  # time
    assert rest["endConditionValue"] == 180
    assert rest["stepOrder"] == 3


def test_build_payload_running_repeat_pace():
    payload = build_workout_payload(
        name="TEST - Running Repeat Pace",
        sport_type="running",
        steps=[
            {
                "type": "repeat",
                "sets": 6,
                "distance_m": 400,
                "rest_duration_s": 90,
                "pace_min_per_km": 5.0,
                "pace_max_per_km": 4.5,
                "description": "400m rep",
            },
        ],
    )

    steps = payload["workoutSegments"][0]["workoutSteps"]
    repeat = steps[0]
    assert repeat["type"] == "RepeatGroupDTO"
    assert repeat["numberOfIterations"] == 6

    interval = repeat["workoutSteps"][0]
    assert interval["targetType"]["workoutTargetTypeId"] == 6  # pace
    assert interval["endCondition"]["conditionTypeId"] == 3  # distance
    assert interval["endConditionValue"] == 400

    rest = repeat["workoutSteps"][1]
    assert rest["endCondition"]["conditionTypeId"] == 2  # time
    assert rest["endConditionValue"] == 90


def test_build_payload_running_repeat_hr():
    payload = build_workout_payload(
        name="TEST - Running Repeat HR",
        sport_type="running",
        steps=[
            {
                "type": "repeat",
                "sets": 5,
                "duration_s": 180,
                "rest_duration_s": 120,
                "hr_min": 155,
                "hr_max": 170,
                "description": "Tempo interval",
            },
        ],
    )

    steps = payload["workoutSegments"][0]["workoutSteps"]
    repeat = steps[0]
    assert repeat["type"] == "RepeatGroupDTO"
    assert repeat["numberOfIterations"] == 5

    interval = repeat["workoutSteps"][0]
    assert interval["targetType"]["workoutTargetTypeId"] == 4  # HR
    assert interval["targetValueOne"] == 155
    assert interval["targetValueTwo"] == 170
    assert interval["endCondition"]["conditionTypeId"] == 2  # time
    assert interval["endConditionValue"] == 180


def _no_target():
    return {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}


def _end_condition(kind: str, value):
    key_by_kind = {
        "distance": (3, "distance", True),
        "time": (2, "time", True),
        "lap_button": (1, "lap.button", True),
        "iterations": (7, "iterations", False),
    }
    cid, key, displayable = key_by_kind[kind]
    return {
        "conditionTypeId": cid,
        "conditionTypeKey": key,
        "displayOrder": cid,
        "displayable": displayable,
    }, value


def _step(step_type_key: str, step_type_id: int, end_cond, target_type=None,
          t_one=None, t_two=None, description=None, exercise_name=None,
          category=None, weight_value=None):
    end_key, end_val = end_cond
    step = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": step_type_id, "stepTypeKey": step_type_key, "displayOrder": step_type_id},
        "endCondition": end_key,
        "endConditionValue": end_val,
        "targetType": target_type or _no_target(),
        "targetValueOne": t_one,
        "targetValueTwo": t_two,
    }
    if description is not None:
        step["description"] = description
    if exercise_name is not None:
        step["exerciseName"] = exercise_name
    if category is not None:
        step["category"] = category
    if weight_value is not None:
        step["weightValue"] = weight_value
    return step


def _repeat_group(sets: int, interval: dict, rest: dict) -> dict:
    return {
        "type": "RepeatGroupDTO",
        "numberOfIterations": sets,
        "endCondition": _end_condition("iterations", sets)[0],
        "endConditionValue": sets,
        "workoutSteps": [interval, rest],
    }


def test_get_workout_detail_running_pace_and_repeat():
    warmup = _step(
        "warmup", 1, _end_condition("distance", 1000.0),
        description="Easy warmup",
    )
    interval = _step(
        "interval", 3, _end_condition("distance", 400.0),
        target_type={"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
        t_one=pace_to_mps("5:00"), t_two=pace_to_mps("4:30"),
        description="400m rep",
    )
    rest = _step("rest", 5, _end_condition("time", 90.0))

    workout = {
        "workoutId": 555,
        "workoutName": "Track Session",
        "description": "5x400 @ 5K pace",
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
        "workoutSegments": [
            {"segmentOrder": 1, "workoutSteps": [warmup, _repeat_group(5, interval, rest)]}
        ],
    }

    mock_client = MagicMock()
    mock_client.get_workout_by_id.return_value = workout

    with patch("tools.workout.get_client", return_value=mock_client):
        result = get_workout_detail(555)

    mock_client.get_workout_by_id.assert_called_once_with(555)

    assert result["workout_id"] == 555
    assert result["workout_name"] == "Track Session"
    assert result["sport_type"] == "running"
    assert result["description"] == "5x400 @ 5K pace"

    steps = result["steps"]
    assert len(steps) == 2

    decoded_warmup = steps[0]
    assert decoded_warmup["type"] == "warmup"
    assert decoded_warmup["distance_m"] == 1000.0
    assert decoded_warmup["description"] == "Easy warmup"
    assert "pace_min_per_km" not in decoded_warmup

    repeat = steps[1]
    assert repeat["type"] == "repeat"
    assert repeat["sets"] == 5
    assert repeat["distance_m"] == 400.0
    assert repeat["description"] == "400m rep"
    assert repeat["pace_min_per_km"] == "5:00"
    assert repeat["pace_max_per_km"] == "4:30"
    assert repeat["rest_duration_s"] == 90.0


def test_get_workout_detail_cycling_power_repeat():
    interval = _step(
        "interval", 3, _end_condition("time", 240.0),
        target_type={"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2},
        t_one=280, t_two=250,  # Garmin convention: targetValueOne = max, targetValueTwo = min
        description="VO2max interval",
    )
    rest = _step("rest", 5, _end_condition("time", 180.0))

    workout = {
        "workoutId": 777,
        "workoutName": "VO2max Bike Set",
        "description": None,
        "sportType": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
        "workoutSegments": [
            {"segmentOrder": 1, "workoutSteps": [_repeat_group(4, interval, rest)]}
        ],
    }

    mock_client = MagicMock()
    mock_client.get_workout_by_id.return_value = workout

    with patch("tools.workout.get_client", return_value=mock_client):
        result = get_workout_detail(777)

    repeat = result["steps"][0]
    assert repeat["sets"] == 4
    assert repeat["duration_s"] == 240.0
    assert repeat["power_watts_max"] == 280
    assert repeat["power_watts_min"] == 250
    assert repeat["rest_duration_s"] == 180.0


def test_get_workout_detail_running_hr_repeat():
    interval = _step(
        "interval", 3, _end_condition("time", 180.0),
        target_type={"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
        t_one=155, t_two=170,  # Garmin convention: targetValueOne = min bpm, targetValueTwo = max bpm
        description="Tempo interval",
    )
    rest = _step("rest", 5, _end_condition("time", 120.0))

    workout = {
        "workoutId": 888,
        "workoutName": "Tempo Set",
        "description": None,
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
        "workoutSegments": [
            {"segmentOrder": 1, "workoutSteps": [_repeat_group(5, interval, rest)]}
        ],
    }

    mock_client = MagicMock()
    mock_client.get_workout_by_id.return_value = workout

    with patch("tools.workout.get_client", return_value=mock_client):
        result = get_workout_detail(888)

    repeat = result["steps"][0]
    assert repeat["sets"] == 5
    assert repeat["duration_s"] == 180.0
    assert repeat["hr_min"] == 155
    assert repeat["hr_max"] == 170
    assert repeat["rest_duration_s"] == 120.0


def test_get_workout_detail_strength_weights():
    warmup = _step(
        "warmup", 1, _end_condition("lap_button", 0.0),
        description="Warm-up sets",
        exercise_name="BARBELL_BACK_SQUAT", category="SQUAT", weight_value=-1.0,
    )
    interval = _step(
        "interval", 3, _end_condition("lap_button", 0.0),
        exercise_name="BARBELL_BACK_SQUAT", category="SQUAT", weight_value=100.0,
    )
    rest = _step("rest", 5, _end_condition("lap_button", 0.0))

    workout = {
        "workoutId": 999,
        "workoutName": "Strength - A",
        "description": None,
        "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
        "workoutSegments": [
            {"segmentOrder": 1, "workoutSteps": [warmup, _repeat_group(3, interval, rest)]}
        ],
    }

    mock_client = MagicMock()
    mock_client.get_workout_by_id.return_value = workout

    with patch("tools.workout.get_client", return_value=mock_client):
        result = get_workout_detail(999)

    decoded_warmup = result["steps"][0]
    assert decoded_warmup["exercise_name"] == "BARBELL_BACK_SQUAT"
    assert decoded_warmup["category"] == "SQUAT"
    assert decoded_warmup["weight_kg"] == -1.0  # bodyweight sentinel preserved as-is
    assert "distance_m" not in decoded_warmup
    assert "duration_s" not in decoded_warmup

    repeat = result["steps"][1]
    assert repeat["sets"] == 3
    assert repeat["exercise_name"] == "BARBELL_BACK_SQUAT"
    assert repeat["weight_kg"] == 100.0
    assert "rest_duration_s" not in repeat
