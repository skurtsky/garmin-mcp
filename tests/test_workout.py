# tests/test_workout.py
from unittest.mock import MagicMock, patch

from tools.workout import (
    pace_to_mps,
    get_saved_workouts,
    get_scheduled_workouts,
    build_workout_payload,
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


def test_build_payload_strength_training():
    payload = build_workout_payload(
        name="TEST - Strength",
        sport_type="strength_training",
        steps=[
            {
                "type": "warmup",
                "description": "Warm-up sets",
                "category": "SQUAT",
                "exercise_name": "BARBELL_BACK_SQUAT",
                "weight_kg": -1.0,
            },
            {
                "type": "repeat",
                "sets": 3,
                "category": "SQUAT",
                "exercise_name": "BARBELL_BACK_SQUAT",
                "weight_kg": 100.0,
            },
        ],
    )

    assert payload["sportType"]["sportTypeKey"] == "strength_training"
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 2

    # warmup
    warmup = steps[0]
    assert warmup["type"] == "ExecutableStepDTO"
    assert warmup["stepType"]["stepTypeKey"] == "warmup"
    assert warmup["stepOrder"] == 1
    assert warmup["category"] == "SQUAT"
    assert warmup["exerciseName"] == "BARBELL_BACK_SQUAT"
    assert warmup["weightValue"] == -1.0

    # repeat group — flat stepOrders: group=2, interval=3, rest=4
    repeat = steps[1]
    assert repeat["type"] == "RepeatGroupDTO"
    assert repeat["stepOrder"] == 2
    assert repeat["numberOfIterations"] == 3
    assert repeat["endCondition"]["conditionTypeKey"] == "iterations"
    assert repeat["endConditionValue"] == 3

    inner = repeat["workoutSteps"]
    assert len(inner) == 2

    interval = inner[0]
    assert interval["type"] == "ExecutableStepDTO"
    assert interval["stepType"]["stepTypeKey"] == "interval"
    assert interval["stepOrder"] == 3
    assert interval["childStepId"] == repeat["childStepId"]
    assert interval["category"] == "SQUAT"
    assert interval["exerciseName"] == "BARBELL_BACK_SQUAT"
    assert interval["weightValue"] == 100.0

    rest = inner[1]
    assert rest["type"] == "ExecutableStepDTO"
    assert rest["stepType"]["stepTypeKey"] == "rest"
    assert rest["stepOrder"] == 4
    assert rest["childStepId"] == repeat["childStepId"]


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
