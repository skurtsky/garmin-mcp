# tests/test_plan_tools.py
"""Tests for the training-plan MCP tools (tools/plan_tools.py, via
tools/plan_service.py) against the in-memory db fake."""
from datetime import date, datetime, timezone

import pytest
from fastmcp.exceptions import ToolError

from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import plan_service, plan_tools


@pytest.fixture
def plan(fake_db):
    plan_service.save_upload(sample_plan())
    return fake_db


def test_tools_report_missing_plan_and_storage(fake_db, monkeypatch):
    with pytest.raises(ToolError, match="No active training plan"):
        plan_tools.get_training_plan()
    import db
    monkeypatch.setattr(db, "is_configured", lambda: False)
    with pytest.raises(ToolError, match="DATABASE_URL"):
        plan_tools.list_training_plans()


def test_overview_defaults_to_current_and_next_week(plan, monkeypatch):
    plan_service.set_completion(None, "w1-tue-run", True)

    overview = plan_service.plan_overview(today=date(2026, 9, 16), include_details=False)

    assert overview["currentWeek"] == 1
    assert [w["weekNumber"] for w in overview["weeks"]] == [1, 2]
    assert overview["thresholds"]["ftp"] == 250
    assert overview["weekSummaries"][0] == {
        "weekNumber": 1, "startDate": "2026-09-14", "endDate": "2026-09-20", "phase": "Base",
        "isRecoveryWeek": False, "targetHours": 3, "plannedHours": 3.83, "workouts": 4, "completed": 1,
    }
    run = next(x for d in overview["weeks"][0]["days"] for x in d["workouts"] if x["id"] == "w1-tue-run")
    assert run["completed"] is True and run["date"] == "2026-09-15"
    assert "humanReadable" not in run and "description" not in run


def test_overview_by_week_and_range(plan):
    assert [w["weekNumber"] for w in plan_tools.get_training_plan(week_number=2)["weeks"]] == [2]
    ranged = plan_tools.get_training_plan(start_date="2026-09-19", end_date="2026-09-22")
    assert [w["weekNumber"] for w in ranged["weeks"]] == [1, 2]
    with pytest.raises(ToolError, match="No week 7"):
        plan_tools.get_training_plan(week_number=7)


def test_amend_needs_a_reason_and_reports_changes(plan):
    with pytest.raises(ToolError, match="reason"):
        plan_tools.amend_training_plan([{"op": "set_unit", "unit": "metric"}], " ")

    result = plan_tools.amend_training_plan([
        {"op": "move_workout", "workout_id": "w1-thu-bike", "date": "2026-09-19"},
        {"op": "remove_workout", "workout_id": "w2-tue-run"},
    ], "Travel Thursday")

    assert result["version"] == 2
    assert result["changes"] == ["Moved 'Endurance ride' Thu Sep 17 → Sat Sep 19",
                                 "Removed 'Intervals' from Tue Sep 22"]
    assert [w["weekNumber"] for w in result["weeks"]] == [1]  # removed workout's week no longer holds it
    revision = plan.revisions["test-block-2026"][-1]
    assert revision["source"] == "mcp"
    assert revision["summary"].startswith("Travel Thursday — Moved")


def test_amend_errors_surface_as_tool_errors(plan):
    with pytest.raises(ToolError, match="Operation 1"):
        plan_tools.amend_training_plan([{"op": "remove_workout", "workout_id": "nope"}], "x")


def test_complete_by_synced_activity(plan):
    plan.activities[42] = {"garmin_id": 42, "activity_type": "road_biking", "name": "Ride",
                           "activity_date": datetime(2026, 9, 17, 7, tzinfo=timezone.utc)}

    result = plan_tools.complete_plan_workout(activity_id=42, notes="Solid")

    assert result["workout_id"] == "w1-thu-bike"
    assert result["completed"] is True and result["activityId"] == 42 and result["notes"] == "Solid"
    assert plan_service.completed_map("test-block-2026") == {"w1-thu-bike": True}


def test_complete_by_date_and_sport_picks_the_matching_sport(plan):
    result = plan_tools.complete_plan_workout(activity_date="2026-09-15", sport="strength")
    assert result["workout_id"] == "w1-tue-strength"


def test_complete_ambiguous_or_missing_lists_candidates(plan):
    with pytest.raises(ToolError, match="Several planned workouts") as exc:
        plan_tools.complete_plan_workout(activity_date="2026-09-15")
    assert "w1-tue-run" in str(exc.value) and "w1-tue-strength" in str(exc.value)

    with pytest.raises(ToolError, match="No planned swim workout on 2026-09-16") as exc:
        plan_tools.complete_plan_workout(activity_date="2026-09-16", sport="swim")
    assert "w1-mon-swim" in str(exc.value)

    with pytest.raises(ToolError, match="isn't synced"):
        plan_tools.complete_plan_workout(activity_id=999)


def test_uncomplete_by_workout_id(plan):
    plan_tools.complete_plan_workout(workout_id="w1-mon-swim", activity_id=7)
    result = plan_tools.complete_plan_workout(workout_id="w1-mon-swim", completed=False)
    assert result["completed"] is False and "activityId" not in result
    assert plan_service.completed_map("test-block-2026") == {}


def test_link_garmin_workout_shows_in_the_overview(plan):
    linked = plan_tools.link_plan_workout_to_garmin("w2-tue-run", 555)
    assert linked["garminWorkoutId"] == 555 and linked["garminScheduledDate"] == "2026-09-22"

    week = plan_tools.get_training_plan(week_number=2)["weeks"][0]
    run = week["days"][0]["workouts"][0]
    assert run["garminWorkoutId"] == 555

    cleared = plan_tools.link_plan_workout_to_garmin("w2-tue-run", None)
    assert "garminWorkoutId" not in cleared
    with pytest.raises(ToolError, match="No workout"):
        plan_tools.link_plan_workout_to_garmin("nope", 1)


def test_revisions_and_restore(plan):
    plan_tools.amend_training_plan([{"op": "set_zones", "ftp": 270}], "FTP test")
    assert [r["version"] for r in plan_tools.get_training_plan_revisions()] == [2, 1]

    restored = plan_tools.restore_training_plan_revision(1)

    assert restored == {"plan_id": "test-block-2026", "version": 3, "restored_from": 1}
    assert plan_tools.get_training_plan()["thresholds"]["ftp"] == 250
    with pytest.raises(ToolError, match="no revision 9"):
        plan_tools.restore_training_plan_revision(9)


def test_sport_for_activity_type():
    assert plan_service.sport_for_activity_type("lap_swimming") == "swim"
    assert plan_service.sport_for_activity_type("virtual_ride") == "bike"
    assert plan_service.sport_for_activity_type("indoor_cycling") == "bike"
    assert plan_service.sport_for_activity_type("treadmill_running") == "run"
    assert plan_service.sport_for_activity_type("strength_training") == "strength"
    assert plan_service.sport_for_activity_type("multi_sport") == "brick"
    assert plan_service.sport_for_activity_type("yoga") is None
