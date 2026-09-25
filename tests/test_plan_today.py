# tests/test_plan_today.py
"""Linking planned workouts to the Garmin activities that did them, and the
plan read model behind the dashboard's Today and Fitness screens
(tools/plan_service.py matching, tools/plan_today.py, tools/plan_doc.py
zones and threshold status). The in-memory FakePlanDB stands in for
PostgreSQL.
"""
from datetime import date

import pytest
from starlette.testclient import TestClient

import db
import sync_garmin
from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import plan_doc, plan_service, plan_today, training_plan

PLAN_ID = "test-block-2026"


@pytest.fixture
def plan(fake_db):
    fake_db.save_uploaded_training_plan(PLAN_ID, plan_doc.normalize_plan(sample_plan()), "Uploaded")
    return fake_db


def _state(fake, workout_id):
    return fake.states.get((PLAN_ID, workout_id)) or {}


# ── matching ────────────────────────────────────────────────────────────────

def test_a_newly_synced_activity_ticks_off_its_planned_workout(plan):
    plan.add_activity(1, "2026-09-17", "road_biking", "Morning ride", 88)

    assert plan_service.match_new_activities([1]) == [{"workout_id": "w1-thu-bike", "activity_id": 1}]
    state = _state(plan, "w1-thu-bike")
    assert state["completed"] is True and state["activity_id"] == 1


def test_matching_needs_the_same_sport_and_date(plan):
    plan.add_activity(1, "2026-09-17", "running", "Run")        # Thursday is a ride
    plan.add_activity(2, "2026-09-16", "road_biking", "Ride")   # nothing planned Wednesday
    plan.add_activity(3, "2026-09-17", "yoga", "Yoga")          # no plan sport at all

    assert plan_service.match_new_activities([1, 2, 3]) == []


def test_several_candidates_pick_the_closest_duration(plan):
    plan.add_activity(1, "2026-09-15", "strength_training", "Gym", 44)
    assert plan_service.match_new_activities([1])[0]["workout_id"] == "w1-tue-strength"


def test_matching_skips_ticked_workouts_and_linked_activities(plan):
    plan.upsert_workout_state(PLAN_ID, "w1-thu-bike", completed=True)
    plan.add_activity(1, "2026-09-17", "road_biking", "Ride")
    assert plan_service.match_new_activities([1]) == []

    # An activity already linked to one workout never completes a second.
    plan.add_activity(2, "2026-09-14", "lap_swimming", "Swim")
    plan_service.match_new_activities([2])
    plan.upsert_workout_state(PLAN_ID, "w1-mon-swim", completed=False)
    assert plan_service.match_new_activities([2]) == []


def test_an_archived_plan_is_never_matched(plan):
    plan.set_training_plan_status(PLAN_ID, "archived")
    plan.add_activity(1, "2026-09-17", "road_biking", "Ride")
    assert plan_service.match_new_activities([1]) == []


def test_hand_ticked_workouts_get_their_activity_attached(plan):
    plan.upsert_workout_state(PLAN_ID, "w1-tue-run", completed=True)
    plan.add_activity(7, "2026-09-15", "running", "Tempo", 51)

    assert plan_service.link_completed_workouts(today=date(2026, 9, 25)) == [
        {"workout_id": "w1-tue-run", "activity_id": 7}]
    assert _state(plan, "w1-tue-run")["activity_id"] == 7
    # Only links — an unticked workout stays unticked.
    assert not _state(plan, "w1-tue-strength").get("completed")


def test_ticking_in_the_viewer_attaches_the_activity(plan):
    plan.add_activity(7, "2026-09-15", "running", "Tempo", 51, 10.2)
    client = TestClient(training_plan.create_app())

    r = client.post("/training-plan/api/completion", json={"workout_id": "w1-tue-run", "completed": True})

    assert r.status_code == 200
    assert r.json()["activities"]["w1-tue-run"] == {"id": 7, "name": "Tempo", "durationMin": 51, "distanceKm": 10.2}


def test_sync_matches_only_newly_inserted_activities(monkeypatch, plan):
    rows = [{"id": 1, "date": "2026-09-17T07:00:00", "type": "road_biking", "name": "Ride"},
            {"id": 2, "date": "2026-09-14T07:00:00", "type": "lap_swimming", "name": "Swim"}]
    monkeypatch.setattr("tools.activities.get_activities", lambda **kw: rows)
    monkeypatch.setattr(db, "update_sync_state", lambda *a, **k: None)
    plan.add_activity(1, "2026-09-17", "road_biking", "Ride")
    plan.add_activity(2, "2026-09-14", "lap_swimming", "Swim")
    monkeypatch.setattr(db, "upsert_activity", lambda garmin_id, **kw: garmin_id == 1)   # 2 was already synced

    sync_garmin.sync_activities()

    assert _state(plan, "w1-thu-bike").get("activity_id") == 1
    assert not _state(plan, "w1-mon-swim").get("completed")


def test_sync_survives_plan_matching_errors(monkeypatch):
    monkeypatch.setattr(plan_service, "match_new_activities", lambda ids: 1 / 0)
    sync_garmin.sync_plan_matches([1])   # logged, not raised


# ── the Today / Fitness read model ─────────────────────────────────────────

def test_context_for_a_session_day(plan):
    plan.add_activity(9, "2026-09-17", "road_biking", "Ride", 92, 46.5)
    plan_service.match_new_activities([9])

    ctx = plan_today.build_plan_context(date(2026, 9, 17))

    assert ctx["week_number"] == 1 and ctx["phase"] == "Base" and ctx["in_plan"]
    [today] = ctx["today"]
    assert today["name"] == "Endurance ride" and today["completed"]
    assert today["activity"]["name"] == "Ride" and today["activity"]["distance_km"] == 46.5
    assert ctx["tomorrow"] == []
    week = ctx["week"]
    assert week["done_hours"] == 1.5 and week["plan_hours"] == round((45 + 50 + 45 + 90) / 60, 1)
    assert [d["state"] for d in week["days"]] == ["planned", "planned", "rest", "done", "rest", "rest", "rest"]
    assert week["days"][3]["is_today"]


def test_context_is_none_without_an_active_plan(fake_db):
    assert plan_today.build_plan_context(date(2026, 9, 17)) is None


def test_ftp_prompt_from_a_matched_test_ride(plan):
    plan.update_training_plan(PLAN_ID, lambda p: (_with_test(p), "test"), "web")
    plan.add_activity(5, "2026-09-22", "road_biking", "FTP test", 55, max_20min_power=261)
    plan_service.match_new_activities([5])

    ftp = plan_today.build_plan_context(date(2026, 9, 22))["ftp_test"]

    assert ftp["best_20min"] == 261 and ftp["estimate"] == 248 and ftp["current"] == 250
    assert ftp["is_today"] and not ftp["applied"]


def test_ftp_prompt_falls_back_to_the_power_stream(plan, monkeypatch):
    plan.update_training_plan(PLAN_ID, lambda p: (_with_test(p), "test"), "web")
    plan.add_activity(5, "2026-09-22", "road_biking", "FTP test", 55)
    plan_service.match_new_activities([5])
    series = [{"t_offset_sec": t, "value": 300 if 600 <= t < 1800 else 150} for t in range(0, 3000)]
    monkeypatch.setattr(db, "get_activity_detail_from_db", lambda i: {"detail": {"power_series": series}})

    assert plan_today.build_plan_context(date(2026, 9, 22))["ftp_test"]["best_20min"] == 300


def _with_test(p):
    p["weeks"][1]["days"][0]["workouts"][0].update(id="w2-tue-bike", sport="bike", type="test", name="FTP Field Test")
    return p


def test_best_rolling_power():
    flat = [{"t_offset_sec": t, "value": 200} for t in range(0, 1500)]
    assert plan_today.best_rolling_power(flat) == 200
    assert plan_today.best_rolling_power(flat[:600]) is None


# ── plan_doc: zones and threshold status ───────────────────────────────────

def test_bike_zones_follow_ftp_and_lthr_overrides():
    p = sample_plan()
    p["overrides"] = {"ftp": 248}
    rows = plan_doc.zone_rows(p, "bike")
    assert rows[0] == {"zone": "1", "name": "Recovery", "watts": "0–136", "hr": "0–130"}
    assert rows[4]["watts"] == "248–260" and rows[-1]["watts"] == "298+"


def test_run_and_swim_zones_use_paces():
    p = sample_plan()
    assert plan_doc.zone_rows(p, "run")[4]["pace"] == "4:15–4:19"
    assert plan_doc.zone_rows(p, "swim")[3] == {"zone": "4", "name": "Threshold", "pace": "2:05"}


@pytest.mark.parametrize("source, validated, expected", [
    ("TESTED — athlete-configured on Edge 1040", False, ("Tested", "athlete-configured on Edge 1040")),
    ("PROVISIONAL bridge value. Garmin over-reads", False, ("Provisional", "bridge value. Garmin over-reads")),
    ("", False, ("Unvalidated", "")),
    ("PROVISIONAL — until tested", True, ("Tested", "until tested")),
])
def test_threshold_status(source, validated, expected):
    p = sample_plan()
    p["zones"]["bike"]["power"]["ftpSource"] = source
    if validated:
        p["zonesValidated"] = {"bike": {"validated": True}}
    assert plan_doc.threshold_status(p, "ftp") == expected


def test_the_ftp_dialogs_write_back_updates_the_plan(plan):
    """The operations the dashboard's FTP dialog sends, through the real route."""
    plan.update_training_plan(PLAN_ID, lambda p: (_with_test(p), "test"), "web")
    plan.add_activity(5, "2026-09-22", "road_biking", "FTP test", 55, max_20min_power=261)
    plan_service.match_new_activities([5])
    client = TestClient(training_plan.create_app())

    r = client.post("/training-plan/api/operations", json={
        "operations": [{"op": "set_zones", "ftp": 248},
                       {"op": "set_zone_validation", "sport": "bike", "validated": True, "at": "2026-09-22"}],
        "reason": "FTP test 2026-09-22"})

    assert r.status_code == 200
    ctx = plan_today.build_plan_context(date(2026, 9, 22))
    assert ctx["ftp_test"]["applied"] is True
    ftp_row = next(t for t in ctx["thresholds"] if t["key"] == "ftp")
    assert ftp_row["plan"] == "248 W" and ftp_row["status"] == "Tested"
    assert ctx["zones"]["bike"][4]["watts"] == "248–260"
