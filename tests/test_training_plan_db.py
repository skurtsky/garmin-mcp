# tests/test_training_plan_db.py
"""The training-plan SQL in db.py against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL points at a disposable database — every
test drops the training-plan tables first:

    TEST_DATABASE_URL=postgresql://postgres@localhost/garmin_test pytest tests/test_training_plan_db.py
"""
import os

import pytest

import db
from tests.plan_fakes import sample_plan
from tools import plan_calendar, plan_service

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(db, "_pool", None)
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS training_plan_workout_state, training_plan_revisions, training_plans, "
                    "calendar_settings")
    db.ensure_schema()
    yield
    db._pool.close()


def test_upload_replace_and_archive():
    row = plan_service.save_upload(sample_plan())
    assert (row["status"], row["version"]) == ("active", 1)

    again = plan_service.save_upload(sample_plan())
    assert again["version"] == 2

    plan_service.save_upload(sample_plan(id="next-block"))
    plans = {p["id"]: p for p in plan_service.list_plans()}
    assert plans["test-block-2026"]["status"] == "archived"
    assert plans["next-block"]["status"] == "active"
    assert plans["next-block"]["event"] == "Test Block"
    assert db.last_upload_version("test-block-2026") == 2


def test_only_one_active_plan_is_possible():
    plan_service.save_upload(sample_plan())
    plan_service.save_upload(sample_plan(id="b"))
    plan_service.set_status("test-block-2026", "active")

    active = [p["id"] for p in plan_service.list_plans() if p["status"] == "active"]
    assert active == ["test-block-2026"]
    assert db.set_training_plan_status("nope", "active") is False
    assert [p["id"] for p in plan_service.list_plans() if p["status"] == "active"] == ["test-block-2026"]


def test_update_writes_a_revision_and_rolls_back_on_error():
    plan_service.save_upload(sample_plan())
    result = plan_service.apply_operations(None, [{"op": "remove_workout", "workout_id": "w1-tue-run"}], "web")
    assert result["row"]["version"] == 2

    with pytest.raises(Exception):
        plan_service.apply_operations(None, [{"op": "remove_workout", "workout_id": "nope"}], "web")

    revisions = plan_service.revisions(None)
    assert [(r["version"], r["source"]) for r in revisions] == [(2, "web"), (1, "upload")]
    assert db.get_training_plan("test-block-2026")["version"] == 2

    restored = plan_service.restore_revision(None, 1, "web")
    assert restored["version"] == 3
    assert db.get_training_plan_revision("test-block-2026", 3)["summary"] == "Restored revision 1"


def test_workout_state_upserts_partially():
    plan_service.save_upload(sample_plan())
    plan_service.set_completion(None, "w1-tue-run", True, activity_id=123, notes="ok")
    plan_service.link_garmin_workout(None, "w1-tue-run", 987)

    state = db.get_workout_states("test-block-2026")["w1-tue-run"]
    assert state["completed"] is True and state["activity_id"] == 123 and state["notes"] == "ok"
    assert state["garmin_workout_id"] == 987 and str(state["garmin_scheduled_date"]) == "2026-09-15"

    plan_service.set_completion(None, "w1-tue-run", False)
    state = db.get_workout_states("test-block-2026")["w1-tue-run"]
    assert state["completed"] is False and state["activity_id"] is None
    assert state["garmin_workout_id"] == 987  # untouched


def test_deleting_a_plan_cascades():
    plan_service.save_upload(sample_plan())
    plan_service.set_completion(None, "w1-tue-run", True)
    plan_service.save_upload(sample_plan(id="b"))
    plan_service.delete_plan("test-block-2026")

    assert db.get_training_plan("test-block-2026") is None
    assert db.get_workout_states("test-block-2026") == {}
    assert db.list_training_plan_revisions("test-block-2026") == []


def test_activity_brief():
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO activities (garmin_id, activity_date, activity_type, name)
                       VALUES (5, '2026-09-15 07:00+00', 'running', 'Run') ON CONFLICT (garmin_id) DO NOTHING""")
    brief = db.get_activity_brief(5)
    assert brief["activity_type"] == "running"
    assert db.get_activity_brief(6) is None


def test_calendar_settings_round_trip():
    assert db.get_calendar_settings() is None
    assert plan_calendar.get_settings()["published"] is False

    plan_calendar.save_schedule("5:45", [{"day": "Sunday", "sport": "bike", "time": "07:00"}])
    published = plan_calendar.publish()
    key = published["feedPath"].split("key=")[1]
    assert plan_calendar.key_is_valid(key)

    # Saving the schedule keeps the token; publishing keeps the schedule.
    plan_calendar.save_schedule("06:00", [])
    assert plan_calendar.key_is_valid(key)
    again = plan_calendar.save_schedule("5:45", [{"day": "Sunday", "sport": "bike", "time": "07:00"}])
    assert again["slots"] == [{"day": "Sunday", "sport": "bike", "time": "07:00"}]

    revoked = plan_calendar.revoke()
    assert revoked["published"] is False and revoked["publishedAt"] is None
    assert revoked["defaultTime"] == "05:45"
    assert not plan_calendar.key_is_valid(key)


def test_future_plans():
    plan_service.save_upload(sample_plan())
    row = plan_service.save_upload(sample_plan(id="later", planStartDate="2027-03-01"), activate=False)
    assert row["status"] == "future"
    plan_service.save_upload(sample_plan(id="sooner", planStartDate="2026-11-01"), activate=False)

    # re-uploading without activating keeps each plan's status
    assert plan_service.save_upload(sample_plan(), activate=False)["status"] == "active"
    assert plan_service.save_upload(sample_plan(id="later", planStartDate="2027-03-01"), activate=False)["status"] == "future"
    assert [(p["id"], p["status"]) for p in plan_service.list_plans()] == [
        ("test-block-2026", "active"), ("sooner", "future"), ("later", "future")]

    # an archived plan comes back as a future one
    plan_service.set_status("sooner", "archived")
    assert plan_service.save_upload(sample_plan(id="sooner"), activate=False)["status"] == "future"

    plan_service.set_status("test-block-2026", "future")
    assert db.get_training_plan() is None
    plan_service.set_status("later", "active")
    assert plan_service.get_plan()["id"] == "later"
    assert {p["id"]: p["status"] for p in plan_service.list_plans()}["test-block-2026"] == "future"


def test_auto_switch_and_carry_over(monkeypatch):
    from datetime import date
    plan_service.save_upload(sample_plan())
    plan_service.apply_operations("test-block-2026", [{"op": "set_zones", "ftp": 270}], "web")
    plan_service.save_upload(sample_plan(id="next", planStartDate="2026-10-05"), activate=False)
    for key in (plan_service.AUTOSWITCH_KEY, plan_service.CARRYOVER_KEY):
        db.set_setting(key, None)
    monkeypatch.setattr(plan_service, "_checked_day", None)

    plan_service.advance_plans(date(2026, 10, 1))
    monkeypatch.setattr(plan_service, "_checked_day", None)
    assert plan_service.advance_plans(date(2026, 10, 5))["id"] == "next"

    row = plan_service.get_plan()
    assert row["id"] == "next"
    assert plan_service.pending_carryover(row)["items"][0]["from"] == 270
    assert plan_service.apply_carryover(["ftp"], "web")["plan"]["overrides"]["ftp"] == 270
    assert db.get_setting(plan_service.CARRYOVER_KEY) is None


def test_season_reads_plans_and_completions_together():
    plan_service.save_upload(sample_plan())
    plan_service.save_upload(sample_plan(id="old", planEndDate="2024-01-01"), activate=False)
    plan_service.set_status("old", "archived")
    plan_service.set_completion(None, "w1-tue-run", True)

    rows, done = db.get_season_plans("2025-01-01")

    assert [r["id"] for r in rows] == ["test-block-2026"]
    assert done == {"test-block-2026": {"w1-tue-run"}}


def test_auto_skip_leaves_decided_and_completed_workouts_alone():
    row = plan_service.save_upload(sample_plan())
    db.upsert_workout_state(row["id"], "w1-mon-swim", completed=True)
    db.upsert_workout_state(row["id"], "w1-tue-run", skipped=False)

    skipped = db.auto_skip_workouts(row["id"], ["w1-mon-swim", "w1-tue-run", "w1-tue-strength", "w1-thu-bike"])

    assert sorted(skipped) == ["w1-thu-bike", "w1-tue-strength"]
    states = db.get_workout_states(row["id"])
    assert states["w1-tue-strength"]["skipped"] is True and states["w1-tue-strength"]["skipped_auto"] is True
    assert states["w1-tue-strength"]["skipped_at"] is not None
    assert states["w1-mon-swim"]["skipped"] is None and states["w1-tue-run"]["skipped"] is False
    assert db.auto_skip_workouts(row["id"], ["w1-thu-bike"]) == []   # already decided


def test_skip_columns_are_added_to_an_existing_state_table():
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE training_plan_workout_state DROP COLUMN skipped, DROP COLUMN skipped_at, "
                    "DROP COLUMN skipped_auto, DROP COLUMN skip_reason")
    db.ensure_schema()
    row = plan_service.save_upload(sample_plan())
    assert db.upsert_workout_state(row["id"], "w1-mon-swim", skipped=True)["skipped"] is True


def test_skip_reason_round_trips():
    row = plan_service.save_upload(sample_plan())
    plan_service.set_skip(row["id"], "w1-tue-run", True, "Sick")
    assert db.get_workout_states(row["id"])["w1-tue-run"]["skip_reason"] == "Sick"
    plan_service.set_completion(row["id"], "w1-tue-run", True)
    assert db.get_workout_states(row["id"])["w1-tue-run"]["skip_reason"] is None
