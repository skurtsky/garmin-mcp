# tests/test_plan_doc.py
"""Tests for the pure training-plan document operations (tools/plan_doc.py)."""
import copy
from datetime import date

import pytest

from tests.plan_fakes import sample_plan
from tools import plan_doc
from tools.plan_doc import PlanError


def _ids(plan, day):
    return [w["id"] for _, d, w in plan_doc.iter_workouts(plan) if d["date"] == day]


# ── VALIDATION ────────────────────────────────────────────────────────────────

def test_valid_plan_has_no_errors():
    errors, warnings = plan_doc.validate_plan(sample_plan())
    assert errors == []
    assert warnings == []


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p.pop("meta"), "meta"),
    (lambda p: p["meta"].pop("id"), "meta.id"),
    (lambda p: p["meta"].update(id="has spaces/slash"), "meta.id"),
    (lambda p: p.pop("weeks"), "weeks"),
    (lambda p: p["weeks"][1]["days"][0]["workouts"][0].update(id="w1-mon-swim"), "Duplicate"),
    (lambda p: p["weeks"][0]["days"][0]["workouts"][0].pop("id"), "no id"),
    (lambda p: p["weeks"][0].update(startDate="soon"), "startDate"),
])
def test_invalid_plans_are_rejected(mutate, message):
    plan = sample_plan()
    mutate(plan)
    errors, _ = plan_doc.validate_plan(plan)
    assert any(message in e for e in errors), errors


def test_non_object_is_rejected():
    assert plan_doc.validate_plan([1, 2])[0]


def test_warnings_for_budgets_phases_and_dates():
    plan = sample_plan()
    plan["phases"] = []
    plan["meta"].pop("totalWeeks")
    plan["weeks"][0]["days"][0]["workouts"][0]["keyTargets"] = "x" * 121
    plan["weeks"][0]["days"][0]["date"] = "2026-09-30"
    errors, warnings = plan_doc.validate_plan(plan)
    assert errors == []
    text = " ".join(warnings)
    assert "phases" in text and "meta.totalWeeks" in text
    assert "keyTargets is 121 characters" in text
    assert "outside the week" in text


# ── WEEKLY TOTALS ─────────────────────────────────────────────────────────────

def test_week_summary_is_computed_from_workouts():
    plan = plan_doc.normalize_plan(sample_plan())
    summary = plan["weeks"][0]["summary"]
    assert summary["totalHours"] == 3.83
    assert summary["bySport"] == {
        "swim": {"sessions": 1, "hours": 0.75, "km": 2},
        "run": {"sessions": 1, "hours": 0.83, "km": 10},
        "strength": {"sessions": 1, "hours": 0.75},
        "bike": {"sessions": 1, "hours": 1.5, "km": 45},
    }


def test_week_summary_ignores_rest_and_uses_range_midpoint():
    week = {"days": [{"date": "2026-09-14", "workouts": [
        {"id": "a", "sport": "rest", "name": "Rest", "durationMinutes": 0},
        {"id": "b", "sport": "bike", "name": "Rouvy", "durationMinutes": 60,
         "distanceKmRange": {"low": 40, "high": 50}},
    ]}]}
    assert plan_doc.week_summary(week) == {"totalHours": 1, "bySport": {"bike": {"sessions": 1, "hours": 1, "km": 45}}}


# ── OPERATIONS ────────────────────────────────────────────────────────────────

def test_apply_operations_never_mutates_the_input():
    plan = sample_plan()
    before = copy.deepcopy(plan)
    plan_doc.apply_operations(plan, [{"op": "remove_workout", "workout_id": "w1-tue-run"}])
    assert plan == before


def test_add_workout_generates_an_id_and_updates_the_summary():
    new, messages, touched = plan_doc.apply_operations(sample_plan(), [{
        "op": "add_workout", "date": "2026-09-15",
        "workout": {"sport": "run", "name": "Strides", "durationMinutes": 20, "distanceKm": "4"},
    }])
    assert touched == ["w1-tue-run-2"]  # w1-tue-run is taken
    assert _ids(new, "2026-09-15") == ["w1-tue-run", "w1-tue-strength", "w1-tue-run-2"]
    added = plan_doc.find_workout(new, "w1-tue-run-2")[2]
    assert added["distanceKm"] == 4 and added["completed"] is False
    assert new["weeks"][0]["summary"]["bySport"]["run"] == {"sessions": 2, "hours": 1.17, "km": 14}
    assert messages == ["Added 'Strides' on Tue Sep 15"]


def test_add_workout_creates_a_missing_day_entry_in_order():
    new, _, touched = plan_doc.apply_operations(sample_plan(), [{
        "op": "add_workout", "date": "2026-09-16", "workout": {"sport": "swim", "name": "Drills"},
    }])
    days = [d["date"] for d in new["weeks"][0]["days"]]
    assert days == sorted(days) and "2026-09-16" in days
    assert touched == ["w1-wed-swim"]
    assert next(d for d in new["weeks"][0]["days"] if d["date"] == "2026-09-16")["dayOfWeek"] == "Wednesday"


@pytest.mark.parametrize("op, message", [
    ({"op": "add_workout", "date": "2027-01-01", "workout": {"sport": "run", "name": "x"}}, "outside every week"),
    ({"op": "add_workout", "date": "2026-09-15", "workout": {"sport": "yoga", "name": "x"}}, "sport must be"),
    ({"op": "add_workout", "date": "2026-09-15", "workout": {"sport": "run"}}, "needs a name"),
    ({"op": "add_workout", "date": "15/09", "workout": {"sport": "run", "name": "x"}}, "YYYY-MM-DD"),
    ({"op": "update_workout", "workout_id": "w1-tue-run", "fields": {"durationMinutes": -5}}, "non-negative"),
    ({"op": "update_workout", "workout_id": "w1-tue-run", "fields": {"name": ""}}, "needs a name"),
    ({"op": "move_workout", "workout_id": "nope", "date": "2026-09-16"}, "No workout"),
    ({"op": "update_week", "week_number": 9, "fields": {"focus": "x"}}, "No week 9"),
    ({"op": "update_week", "week_number": 1, "fields": {"days": []}}, "can't be edited"),
    ({"op": "update_week", "week_number": 1, "fields": {"targetHours": True}}, "wrong type"),
    ({"op": "set_zones", "ftp": "fast"}, "whole number"),
    ({"op": "set_zones", "thresholdPace": "quick"}, "m:ss"),
    ({"op": "set_zones"}, "at least one"),
    ({"op": "set_unit", "unit": "furlongs"}, "unit must be"),
    ({"op": "explode"}, "unknown op"),
])
def test_bad_operations_raise_with_the_operation_named(op, message):
    with pytest.raises(PlanError) as exc:
        plan_doc.apply_operations(sample_plan(), [{"op": "set_unit", "unit": "metric"}, op])
    assert "Operation 2" in str(exc.value) and message in str(exc.value)


def test_operations_are_all_or_nothing():
    plan = sample_plan()
    with pytest.raises(PlanError):
        plan_doc.apply_operations(plan, [
            {"op": "remove_workout", "workout_id": "w1-tue-run"},
            {"op": "remove_workout", "workout_id": "missing"},
        ])
    assert plan_doc.find_workout(plan, "w1-tue-run")


def test_move_workout_across_weeks():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [
        {"op": "move_workout", "workout_id": "w1-thu-bike", "date": "2026-09-24"},
    ])
    week, day, _ = plan_doc.find_workout(new, "w1-thu-bike")
    assert week["weekNumber"] == 2 and day["date"] == "2026-09-24"
    assert "bike" not in new["weeks"][0]["summary"]["bySport"]
    assert new["weeks"][1]["summary"]["bySport"]["bike"]["sessions"] == 1
    assert messages == ["Moved 'Endurance ride' Thu Sep 17 → Thu Sep 24"]


def test_move_to_the_same_day_is_a_no_op():
    _, messages, touched = plan_doc.apply_operations(sample_plan(), [
        {"op": "move_workout", "workout_id": "w1-thu-bike", "date": "2026-09-17"},
    ])
    assert messages == [] and touched == []


def test_update_workout_edits_removes_and_moves():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [{
        "op": "update_workout", "workout_id": "w1-tue-run",
        "fields": {"name": "Hill reps", "distanceKm": None, "keyTargets": "6x90s", "date": "2026-09-16",
                   "id": "hijack", "completed": True},
    }])
    _, day, w = plan_doc.find_workout(new, "w1-tue-run")
    assert w["name"] == "Hill reps" and "distanceKm" not in w and w["keyTargets"] == "6x90s"
    assert day["date"] == "2026-09-16"
    assert "completed" not in w  # completion is tracked outside the document
    assert messages == ["Edited 'Hill reps' and moved 'Hill reps' Tue Sep 15 → Wed Sep 16"]


def test_remove_and_update_week():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [
        {"op": "remove_workout", "workout_id": "w1-mon-swim"},
        {"op": "update_week", "week_number": 1, "fields": {"focus": "Travel", "isRecoveryWeek": True}},
    ])
    assert plan_doc.find_workout(new, "w1-mon-swim") is None
    assert new["weeks"][0]["focus"] == "Travel" and new["weeks"][0]["isRecoveryWeek"] is True
    assert messages[1] == "Updated week 1 (focus, isRecoveryWeek)"


def test_zone_overrides_validation_and_units():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [
        {"op": "set_zones", "ftp": "262", "thresholdPace": "4:10", "cssLabel": "2:00"},
        {"op": "set_zone_validation", "sport": "bike", "validated": True, "at": "2026-09-24"},
        {"op": "set_unit", "unit": "imperial"},
    ])
    assert new["overrides"] == {"ftp": 262, "thresholdPace": "4:10", "cssLabel": "2:00/100m", "cssSeconds": 120}
    assert new["zonesValidated"] == {"bike": {"validated": True, "at": "2026-09-24"}}
    assert new["unit"] == "imperial"
    thresholds = plan_doc.effective_thresholds(new)
    assert thresholds["ftp"] == 262 and thresholds["bikeLthr"] == 160 and thresholds["css"] == "2:00/100m"

    reset, _, _ = plan_doc.apply_operations(new, [{"op": "set_zones", "ftp": None, "cssLabel": ""}])
    assert reset["overrides"] == {"thresholdPace": "4:10"}
    assert plan_doc.effective_thresholds(reset)["ftp"] == 250


def test_current_week_number():
    plan = sample_plan()
    assert plan_doc.current_week_number(plan, date(2026, 9, 23)) == 2
    assert plan_doc.current_week_number(plan, date(2026, 9, 1)) == 1
    assert plan_doc.current_week_number(plan, date(2027, 1, 1)) is None
