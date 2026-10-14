# tests/test_plan_tools.py
"""Tests for the training-plan MCP tools (tools/plan_tools.py, via
tools/plan_service.py) against the in-memory db fake."""
from datetime import date, datetime, timezone

import pytest
from fastmcp.exceptions import ToolError

from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import plan_service, plan_tools
from tools.plan_doc import PlanError


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


def test_sections_return_parts_of_the_document_for_editing(plan):
    plan_tools.amend_training_plan([
        {"op": "set_field", "path": "raceStrategy.keyRisks", "value": ["Heat"]},
        {"op": "update_week", "week_number": 1, "fields": {"notes": "Away Sat"}},
    ], "Race notes")

    out = plan_tools.get_training_plan(week_number=1, sections=["raceStrategy", "preferences"])
    assert out["sections"] == {"raceStrategy": {"keyRisks": ["Heat"]},
                               "preferences": {"swim": "meters", "bike": "kilometers", "run": "kilometers"}}
    assert out["weeks"][0]["notes"] == "Away Sat"
    assert "sections" not in plan_tools.get_training_plan()
    with pytest.raises(ToolError, match="No section 'assessment'"):
        plan_tools.get_training_plan(sections=["assessment"])


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


def test_amend_works_on_future_plans_but_not_archived_ones(plan):
    plan_service.save_upload(sample_plan(id="next-block"), activate=False)

    result = plan_tools.amend_training_plan(
        [{"op": "remove_workout", "workout_id": "w2-tue-run"}], "Planning ahead", plan_id="next-block")
    assert result["plan_id"] == "next-block" and result["version"] == 2
    assert plan_tools.get_training_plan("next-block")["status"] == "future"
    assert plan_tools.get_training_plan()["id"] == "test-block-2026"   # the active plan is untouched

    plan_service.set_status("next-block", "archived")
    with pytest.raises(ToolError, match="read-only"):
        plan_tools.amend_training_plan([{"op": "set_unit", "unit": "imperial"}], "x", plan_id="next-block")


def test_list_orders_active_then_future_by_start_then_archived(plan):
    plan_service.save_upload(sample_plan(id="later", planStartDate="2027-03-01"), activate=False)
    plan_service.save_upload(sample_plan(id="sooner", planStartDate="2026-11-01"), activate=False)
    plan_service.save_upload(sample_plan(id="old"), activate=False)
    plan_service.set_status("old", "archived")

    assert [(p["id"], p["status"]) for p in plan_tools.list_training_plans()] == [
        ("test-block-2026", "active"), ("sooner", "future"), ("later", "future"), ("old", "archived")]
    with pytest.raises(PlanError, match="Unknown plan status"):
        plan_service.set_status("old", "paused")


def _shifted(plan_id, start):
    """The sample plan moved to start on ``start`` (a Monday)."""
    from datetime import timedelta
    plan = sample_plan(id=plan_id, event=plan_id.title())
    delta = date.fromisoformat(start) - date(2026, 9, 14)
    plan["meta"]["planStartDate"] = start
    plan["meta"]["planEndDate"] = (date.fromisoformat(start) + timedelta(days=13)).isoformat()
    for week in plan["weeks"]:
        for key in ("startDate", "endDate"):
            week[key] = (date.fromisoformat(week[key]) + delta).isoformat()
        for day in week["days"]:
            day["date"] = (date.fromisoformat(day["date"]) + delta).isoformat()
            for w in day["workouts"]:
                w["id"] = plan_id + "-" + w["id"]
    return plan


def test_a_future_plan_takes_over_on_its_start_date(plan, fake_db):
    plan_service.save_upload(_shifted("winter", "2026-10-05"), activate=False)

    assert plan_service.advance_plans(date(2026, 10, 1)) is None     # the first check only notes the day
    assert fake_db.settings[plan_service.AUTOSWITCH_KEY] == "2026-10-01"
    plan_service._checked_day = None
    assert plan_service.advance_plans(date(2026, 10, 4)) is None
    plan_service._checked_day = None
    assert plan_service.advance_plans(date(2026, 10, 5))["id"] == "winter"

    assert fake_db.plans["winter"]["status"] == "active"
    assert fake_db.plans["test-block-2026"]["status"] == "archived"
    offer = plan_service.pending_carryover(fake_db.get_training_plan())
    assert offer["auto"] is True and offer["on"] == "2026-10-05" and offer["items"] == []


def test_auto_switch_skips_start_dates_already_past(plan, fake_db):
    plan_service.advance_plans(date(2026, 10, 10))
    plan_service.save_upload(_shifted("late", "2026-10-05"), activate=False)   # its start date has passed
    plan_service.save_upload(_shifted("today", "2026-10-10"), activate=False)  # starts today, after today's check

    plan_service._checked_day = None
    assert plan_service.advance_plans(date(2026, 10, 11)) is None
    assert fake_db.plans["late"]["status"] == fake_db.plans["today"]["status"] == "future"


def test_auto_switch_takes_the_latest_due_plan(plan, fake_db, monkeypatch):
    plan_service.advance_plans(date(2026, 10, 1))
    plan_service.save_upload(_shifted("first", "2026-10-05"), activate=False)
    plan_service.save_upload(_shifted("second", "2026-10-12"), activate=False)

    plan_service._checked_day = None
    plan_service.advance_plans(date(2026, 10, 20))   # the server was off for a while

    assert fake_db.plans["second"]["status"] == "active"
    assert fake_db.plans["first"]["status"] == "archived"
    assert plan_service.advance_plans(date(2026, 10, 20)) is None   # once per day per process


def test_auto_switch_never_breaks_the_read(plan, fake_db, monkeypatch):
    def boom(*_):
        raise RuntimeError("db down")
    import db
    monkeypatch.setattr(db, "set_setting", boom)

    assert plan_service.advance_plans(date(2026, 10, 1)) is None
    assert plan_tools.get_training_plan()["id"] == "test-block-2026"


def test_overview_mentions_a_pending_carry_over(plan):
    plan_service.apply_operations(None, [{"op": "set_zones", "ftp": 270}], "mcp", "test")
    plan_service.save_upload(sample_plan(id="next-block"), activate=True)

    carry = plan_tools.get_training_plan()["thresholdCarryover"]
    assert carry["items"][0]["key"] == "ftp" and carry["items"][0]["from"] == 270
    plan_service.dismiss_carryover()
    assert "thresholdCarryover" not in plan_tools.get_training_plan()


def test_season_data(plan, fake_db):
    plan_service.set_completion(None, "w1-tue-run", True)
    plan_service.save_upload(_shifted("winter", "2026-10-05"), activate=False)
    plan_service.save_upload(_shifted("ancient", "2024-01-01"), activate=False)
    plan_service.set_status("ancient", "archived")

    data = plan_service.season(date(2026, 9, 20))

    assert [p["id"] for p in data["plans"]] == ["test-block-2026", "winter"]   # old archived plans left out
    first = data["plans"][0]
    assert first["weeks"][0] == {"weekNumber": 1, "start": "2026-09-14", "end": "2026-09-20", "phase": "Base",
                                 "recovery": False, "plannedMin": 230, "doneMin": 50}
    assert first["phases"] == [{"name": "Base", "start": "2026-09-14", "end": "2026-09-27"}]


def test_coach_can_queue_and_archive_plans_but_not_change_the_active_one(plan, fake_db, monkeypatch):
    monkeypatch.setattr(plan_tools, "local_today", lambda: date(2026, 9, 20))
    plan_service.save_upload(_shifted("winter", "2026-09-28"), activate=False)

    out = plan_tools.set_training_plan_status("winter", "archived")
    assert (out["status"], out["previous_status"]) == ("archived", "future")
    assert fake_db.plans["winter"]["status"] == "archived"

    out = plan_tools.set_training_plan_status("winter", "future")
    assert out["status"] == "future" and out["becomes_active_on"] == "2026-09-28"
    assert out["timeline"] == []        # starts the day after the active plan ends

    with pytest.raises(ToolError, match="Only the athlete"):
        plan_tools.set_training_plan_status("winter", "active")
    with pytest.raises(ToolError, match="is the active plan"):
        plan_tools.set_training_plan_status("test-block-2026", "archived")
    with pytest.raises(ToolError, match="'future' or 'archived'"):
        plan_tools.set_training_plan_status("winter", "paused")
    with pytest.raises(ToolError, match="No plan with id"):
        plan_tools.set_training_plan_status("nope", "future")
    assert fake_db.plans["test-block-2026"]["status"] == "active"


def test_queueing_a_plan_reports_overlaps_and_a_past_start(plan, fake_db, monkeypatch):
    monkeypatch.setattr(plan_tools, "local_today", lambda: date(2026, 9, 20))
    plan_service.save_upload(_shifted("clash", "2026-09-21"), activate=False)
    plan_service.set_status("clash", "archived")

    out = plan_tools.set_training_plan_status("clash", "future")

    assert "overlaps" in out["timeline"][0]
    assert "becomes_active_on" in out
    plan_service.set_status("clash", "archived")
    plan_service.save_upload(_shifted("old", "2026-08-03"), activate=False)
    assert "won't take over by itself" in plan_tools.set_training_plan_status("old", "future")["note"]
