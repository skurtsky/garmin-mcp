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
from tools import plan_service

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(db, "_pool", None)
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS training_plan_workout_state, training_plan_revisions, training_plans")
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
