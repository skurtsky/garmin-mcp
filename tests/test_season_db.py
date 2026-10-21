# tests/test_season_db.py
"""The season SQL in db.py against a real PostgreSQL, through tools/season_service.py.

Skipped unless TEST_DATABASE_URL points at a disposable database — every
test drops the season and training-plan tables first:

    TEST_DATABASE_URL=postgresql://postgres@localhost/garmin_test pytest tests/test_season_db.py
"""
import os

import pytest

import db
from tests.plan_fakes import sample_plan
from tools import goal_race, plan_service, season_service

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

SEASON = {"name": "Season 2026–27", "startDate": "2026-09-01", "endDate": "2027-09-30"}
TREMBLANT = {"name": "Tremblant 70.3", "date": "2027-06-20", "priority": "A", "type": "70.3", "goal": "5:30:00"}


@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(db, "_pool", None)
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS season_races, seasons, training_plan_idempotency, "
                    "training_plan_workout_state, training_plan_revisions, training_plans, app_settings")
    db.ensure_schema()
    yield
    db._pool.close()


def test_schema_is_idempotent_and_adds_the_plan_column():
    db.ensure_schema()
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'training_plans' AND column_name = 'season_id'")
        assert cur.fetchone() is not None


def test_a_season_round_trips_with_its_races_goals_and_objectives():
    season = season_service.create_season({**SEASON, "goals": ["Sub 5:30"], "objectives": [{"text": "FTP 260 W", "status": "met"}]})
    after = season_service.add_race(season["id"], TREMBLANT)
    race = after["races"][0]

    got = season_service.get_season(season["id"])

    assert got == after
    assert (got["startDate"], got["endDate"]) == ("2026-09-01", "2027-09-30")
    assert got["goals"][0]["text"] == "Sub 5:30" and got["objectives"][0]["status"] == "met"
    assert (race["date"], race["priority"], race["type"], race["goal"]) == ("2027-06-20", "A", "70.3", "5:30:00")


def test_listing_orders_active_first_and_newest_start_first():
    old = season_service.create_season({**SEASON, "name": "Old", "startDate": "2025-09-01", "endDate": "2026-08-31"})
    season_service.create_season({**SEASON, "name": "New"})
    season_service.update_season(old["id"], {"status": "archived"})

    assert [s["name"] for s in season_service.list_seasons()] == ["New", "Old"]
    assert season_service.list_seasons()[1]["status"] == "archived"


def test_races_update_delete_and_cascade_with_the_season():
    season = season_service.create_season(SEASON)
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]
    season_service.update_race(season["id"], race["id"], {"priority": "B"})
    assert season_service.get_season(season["id"])["races"][0]["priority"] == "B"
    with pytest.raises(LookupError):
        season_service.delete_race(season["id"], "r_nope")

    season_service.delete_race(season["id"], race["id"])
    assert season_service.get_season(season["id"])["races"] == []
    with pytest.raises(Exception):
        db.save_season_race({"id": "r_x", "season_id": "s_missing", "name": "x", "race_date": "2027-01-01",
                             "priority": "A", "race_type": "other", "goal": "", "location": "", "url": "", "notes": ""})


def test_a_priority_outside_abc_is_refused_by_the_database():
    season = season_service.create_season(SEASON)

    with pytest.raises(Exception):
        db.save_season_race({"id": "r_x", "season_id": season["id"], "name": "x", "race_date": "2027-01-01",
                             "priority": "D", "race_type": "other", "goal": "", "location": "", "url": "", "notes": ""})


def test_plan_links_survive_replacing_the_plan_and_move_between_seasons():
    one = season_service.create_season(SEASON)
    two = season_service.create_season({**SEASON, "name": "Other"})
    plan_service.save_upload(sample_plan(), activate=False)
    plan_id = "test-block-2026"

    season_service.link_plan(one["id"], plan_id)
    plan_service.save_upload(sample_plan(), activate=False)   # the same id again: a new version of the plan
    assert season_service.get_season(one["id"])["planIds"] == [plan_id]

    season_service.link_plan(two["id"], plan_id)
    assert season_service.get_season(one["id"])["planIds"] == []
    assert db.get_plan_seasons() == {plan_id: two["id"]}
    with pytest.raises(LookupError):
        season_service.link_plan(one["id"], "nope")


def test_deleting_a_plan_drops_it_from_the_season():
    season = season_service.create_season(SEASON)
    plan_service.save_upload(sample_plan(), activate=False)
    season_service.link_plan(season["id"], "test-block-2026")

    plan_service.delete_plan("test-block-2026")

    assert season_service.get_season(season["id"])["planIds"] == []


def test_the_active_plans_goal_race_is_one_query_and_follows_the_choice():
    season = season_service.create_season(SEASON)
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]
    plan_service.save_upload(sample_plan())          # active
    plan_service.save_upload(sample_plan(id="later"), activate=False)
    season_service.link_plan(season["id"], "test-block-2026")
    season_service.link_plan(season["id"], "later")
    assert db.get_active_plan_goal_race() is None

    season_service.set_plan_goal_race("later", race["id"])
    assert db.get_active_plan_goal_race() is None     # only the active plan's choice counts

    season_service.set_plan_goal_race("test-block-2026", race["id"])
    got = db.get_active_plan_goal_race()
    assert got["id"] == race["id"] and got["race_date"] == "2027-06-20"
    goal = goal_race.get_goal_race()
    assert (goal["name"], goal["distance"], goal["target_sec"]) == ("Tremblant 70.3", "70.3", 19800)

    season_service.delete_race(season["id"], race["id"])
    assert db.get_active_plan_goal_race() is None and goal_race.get_goal_race() is None


def test_delete_race_removes_references_in_the_season_json():
    season = season_service.create_season(SEASON)
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]
    season_service.set_goals(season["id"], [{"text": "Sub 5:30", "raceId": race["id"]}])

    after = season_service.delete_race(season["id"], race["id"])

    assert after["goals"] == [{"id": after["goals"][0]["id"], "text": "Sub 5:30"}]
