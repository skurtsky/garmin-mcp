# tests/test_season_service.py
"""Season storage and rules (tools/season_service.py) against the in-memory
fake of db.py: create/edit/archive, races, goals and objectives, plan links,
the goal race, and that plans themselves are left alone."""
import pytest

from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import goal_race, plan_service, season_service
from tools.plan_service import PlanStorageUnavailable
from tools.season_doc import SeasonError

SEASON = {"name": "Season 2026–27", "startDate": "2026-09-01", "endDate": "2027-09-30"}
TREMBLANT = {"name": "Tremblant 70.3", "date": "2027-06-20", "priority": "A", "type": "70.3", "goal": "5:30:00"}


@pytest.fixture
def season(fake_db):
    return season_service.create_season(SEASON)


def _plan(fake_db, plan_id="block-1", status="future", start="2026-09-14", end="2026-09-27"):
    """A two-week plan (the sample's weeks start 2026-09-14); `status="active"` activates it after storing."""
    plan_service.create_plan(sample_plan(id=plan_id, planStartDate=start, planEndDate=end))
    if status == "active":
        plan_service.activate(plan_id)
    return plan_id


# ── seasons ───────────────────────────────────────────────────────────────────

def test_create_season(fake_db):
    s = season_service.create_season({**SEASON, "goals": ["Sub 5:30 at Tremblant"],
                                      "objectives": [{"text": "FTP 260 W", "status": "on_track"}]})

    assert s["id"].startswith("s_") and s["status"] == "active"
    assert (s["name"], s["startDate"], s["endDate"]) == ("Season 2026–27", "2026-09-01", "2027-09-30")
    assert [g["text"] for g in s["goals"]] == ["Sub 5:30 at Tremblant"] and s["objectives"][0]["status"] == "on_track"
    assert s["raceIds"] == [] and s["planIds"] == [] and s["warnings"] == []
    assert season_service.get_season(s["id"]) == s


def test_a_new_season_cannot_link_goals_to_races_that_do_not_exist_yet(fake_db):
    with pytest.raises(SeasonError, match="isn't in this season"):
        season_service.create_season({**SEASON, "goals": [{"text": "x", "raceId": "r_1"}]})


def test_invalid_dates_store_nothing(fake_db):
    with pytest.raises(SeasonError):
        season_service.create_season({**SEASON, "endDate": "2026-08-01"})

    assert fake_db.seasons == {}


def test_update_and_archive_a_season(season):
    renamed = season_service.update_season(season["id"], {"name": "Renamed"})
    archived = season_service.update_season(season["id"], {"status": "archived"})
    restored = season_service.update_season(season["id"], {"status": "active"})

    assert renamed["name"] == "Renamed" and renamed["startDate"] == SEASON["startDate"]
    assert archived["status"] == "archived" and restored["status"] == "active"


def test_seasons_list_active_first_then_newest_start(fake_db):
    old = season_service.create_season({**SEASON, "name": "Old", "startDate": "2025-09-01", "endDate": "2026-08-31"})
    new = season_service.create_season({**SEASON, "name": "New"})
    season_service.update_season(old["id"], {"status": "archived"})
    mid = season_service.create_season({**SEASON, "name": "Mid", "startDate": "2026-01-01", "endDate": "2026-12-31"})

    assert [s["name"] for s in season_service.list_seasons()] == ["New", "Mid", "Old"]
    assert new["id"] != mid["id"]


def test_unknown_season_is_a_lookup_error(fake_db):
    with pytest.raises(LookupError):
        season_service.get_season("s_nope")


def test_without_a_database_seasons_say_so(monkeypatch):
    import db
    monkeypatch.setattr(db, "is_configured", lambda: False)

    with pytest.raises(PlanStorageUnavailable, match="DATABASE_URL"):
        season_service.list_seasons()
    assert season_service.active_goal_race() is None


# ── races ─────────────────────────────────────────────────────────────────────

def test_add_update_and_delete_a_race(season):
    added = season_service.add_race(season["id"], TREMBLANT)
    race = added["races"][0]
    assert race["id"].startswith("r_") and race["seasonId"] == season["id"]
    assert added["raceIds"] == [race["id"]] and race["priority"] == "A" and race["type"] == "70.3"

    updated = season_service.update_race(season["id"], race["id"], {"goal": "Sub 5:20", "location": "Mont-Tremblant, QC"})
    assert updated["races"][0]["goal"] == "Sub 5:20" and updated["races"][0]["name"] == "Tremblant 70.3"

    assert season_service.delete_race(season["id"], race["id"])["races"] == []


def test_races_are_listed_by_date(season):
    season_service.add_race(season["id"], {**TREMBLANT, "name": "Late", "date": "2027-09-19"})
    season_service.add_race(season["id"], {**TREMBLANT, "name": "Early", "date": "2027-04-18"})

    assert [r["name"] for r in season_service.get_season(season["id"])["races"]] == ["Early", "Late"]


def test_an_invalid_race_is_rejected_and_not_stored(season):
    with pytest.raises(SeasonError, match="Priority"):
        season_service.add_race(season["id"], {**TREMBLANT, "priority": "Z"})

    assert season_service.get_season(season["id"])["races"] == []


def test_race_of_another_season_is_not_found(season):
    other = season_service.create_season({**SEASON, "name": "Other"})
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]

    with pytest.raises(LookupError):
        season_service.update_race(other["id"], race["id"], {"name": "X"})
    with pytest.raises(LookupError):
        season_service.delete_race(other["id"], race["id"])


def test_too_many_a_races_is_a_warning_not_an_error(season):
    for i in range(4):
        s = season_service.add_race(season["id"], {**TREMBLANT, "name": f"A{i}"})

    assert len(s["races"]) == 4
    assert any("4 A races" in w for w in s["warnings"])


def test_a_race_outside_the_season_is_stored_with_a_warning(season):
    s = season_service.add_race(season["id"], {**TREMBLANT, "date": "2028-03-01"})

    assert len(s["races"]) == 1 and any("outside the season" in w for w in s["warnings"])


# ── goals and objectives ──────────────────────────────────────────────────────

def test_set_goals_and_objectives_link_to_races(season):
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]

    s = season_service.set_goals(season["id"], [{"text": "Sub 5:30", "raceId": race["id"], "targetDate": "2027-06-20"}, "Stay healthy"])
    s = season_service.set_objectives(season["id"], [{"text": "FTP 260 W", "targetDate": "2027-04-29"}])

    assert s["goals"][0]["raceId"] == race["id"] and s["goals"][0]["targetDate"] == "2027-06-20"
    assert s["objectives"][0]["status"] == "not_started"
    with pytest.raises(SeasonError, match="isn't in this season"):
        season_service.set_goals(season["id"], [{"text": "x", "raceId": "r_gone"}])


def test_objective_status_is_set_by_hand(season):
    s = season_service.set_objectives(season["id"], [{"text": "FTP 260 W"}])
    oid = s["objectives"][0]["id"]

    s = season_service.set_objectives(season["id"], [{"id": oid, "text": "FTP 260 W", "status": "met"}])

    assert s["objectives"][0]["id"] == oid and s["objectives"][0]["status"] == "met"


def test_five_goals_are_kept_with_a_warning(season):
    s = season_service.set_goals(season["id"], [f"goal {i}" for i in range(5)])

    assert len(s["goals"]) == 5 and any("5 goals" in w for w in s["warnings"])


def test_deleting_a_race_unlinks_goals_objectives_and_goal_race_choices(season, fake_db):
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]
    plan_id = _plan(fake_db)
    season_service.link_plan(season["id"], plan_id)
    season_service.set_plan_goal_race(plan_id, race["id"])
    season_service.set_goals(season["id"], [{"text": "Sub 5:30", "raceId": race["id"]}])
    season_service.set_objectives(season["id"], [{"text": "FTP", "raceId": race["id"]}])

    s = season_service.delete_race(season["id"], race["id"])

    assert s["goals"][0]["text"] == "Sub 5:30" and "raceId" not in s["goals"][0]
    assert "raceId" not in s["objectives"][0] and s["planGoalRaces"] == {}


# ── plans ─────────────────────────────────────────────────────────────────────

def test_link_and_unlink_a_plan(season, fake_db):
    plan_id = _plan(fake_db)

    linked = season_service.link_plan(season["id"], plan_id)
    assert linked["planIds"] == [plan_id] and linked["plans"][0]["event"] == "Test Block" and linked["warnings"] == []
    assert season_service.season_for_plan(plan_id)["id"] == season["id"]

    assert season_service.unlink_plan(plan_id)["planIds"] == []
    assert season_service.season_for_plan(plan_id) is None
    assert season_service.unlink_plan(plan_id) is None   # already free


def test_a_plan_belongs_to_at_most_one_season(season, fake_db):
    other = season_service.create_season({**SEASON, "name": "Other"})
    plan_id = _plan(fake_db)
    season_service.link_plan(season["id"], plan_id)

    season_service.link_plan(other["id"], plan_id)

    assert season_service.get_season(season["id"])["planIds"] == []
    assert season_service.get_season(other["id"])["planIds"] == [plan_id]


def test_moving_a_plan_forgets_its_goal_race_in_the_old_season(season, fake_db):
    other = season_service.create_season({**SEASON, "name": "Other"})
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]
    plan_id = _plan(fake_db)
    season_service.link_plan(season["id"], plan_id)
    season_service.set_plan_goal_race(plan_id, race["id"])

    season_service.link_plan(other["id"], plan_id)

    assert season_service.get_season(season["id"])["planGoalRaces"] == {}


def test_a_plan_outside_the_season_links_with_a_warning(fake_db):
    late_season = season_service.create_season({**SEASON, "startDate": "2026-10-01"})
    plan_id = _plan(fake_db)   # runs 2026-09-14 – 2026-09-27, before the season starts

    s = season_service.link_plan(late_season["id"], plan_id)

    assert s["planIds"] == [plan_id]
    assert any("Test Block" in w and "outside the season" in w for w in s["warnings"])


def test_linking_an_unknown_plan_or_to_an_archived_season_fails(season, fake_db):
    with pytest.raises(LookupError, match="No plan"):
        season_service.link_plan(season["id"], "nope")
    plan_id = _plan(fake_db)
    season_service.update_season(season["id"], {"status": "archived"})
    with pytest.raises(SeasonError, match="archived"):
        season_service.link_plan(season["id"], plan_id)


def test_plan_json_is_untouched_by_linking(season, fake_db):
    plan_id = _plan(fake_db)
    before = plan_service.get_plan(plan_id)

    season_service.link_plan(season["id"], plan_id)
    after = plan_service.get_plan(plan_id)

    assert after["plan"] == before["plan"] and after["version"] == before["version"]
    assert "seasonId" not in after["plan"]["meta"]


def test_deleting_a_plan_drops_it_from_its_season(season, fake_db):
    plan_id = _plan(fake_db)
    season_service.link_plan(season["id"], plan_id)

    plan_service.delete_plan(plan_id)

    assert season_service.get_season(season["id"])["planIds"] == []


# ── goal race ─────────────────────────────────────────────────────────────────

def test_goal_race_choice_without_a_season_offers_the_active_seasons(fake_db):
    a = season_service.create_season(SEASON)
    b = season_service.create_season({**SEASON, "name": "Old"})
    season_service.update_season(b["id"], {"status": "archived"})
    plan_id = _plan(fake_db)

    choice = season_service.goal_race_choice(plan_id)

    assert choice == {"season": None, "seasons": [{"id": a["id"], "name": SEASON["name"]}]}


def test_goal_race_choice_lists_the_season_races_and_the_pick(season, fake_db):
    plan_id = _plan(fake_db)
    season_service.link_plan(season["id"], plan_id)
    season_service.add_race(season["id"], {**TREMBLANT, "name": "Late", "date": "2027-09-19"})
    early = season_service.add_race(season["id"], {**TREMBLANT, "name": "Early", "date": "2027-04-18"})
    early_id = next(r["id"] for r in early["races"] if r["name"] == "Early")

    season_service.set_plan_goal_race(plan_id, early_id)
    choice = season_service.goal_race_choice(plan_id)

    assert [r["name"] for r in choice["races"]] == ["Early", "Late"]
    assert choice["selectedRaceId"] == early_id and choice["season"]["name"] == SEASON["name"]


def test_goal_race_needs_a_linked_plan_and_a_race_of_its_season(season, fake_db):
    plan_id = _plan(fake_db)
    with pytest.raises(SeasonError, match="Link this plan"):
        season_service.set_plan_goal_race(plan_id, "r_1")
    season_service.link_plan(season["id"], plan_id)
    with pytest.raises(SeasonError, match="isn't in the season"):
        season_service.set_plan_goal_race(plan_id, "r_other")
    assert season_service.set_plan_goal_race(plan_id, None)["planGoalRaces"] == {}


def test_the_active_plans_season_choice_becomes_the_app_goal_race(season, fake_db):
    plan_id = _plan(fake_db, status="active")
    season_service.link_plan(season["id"], plan_id)
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]
    fake_db.settings["goal_race"] = {"name": "Old goal", "date": "2027-01-01", "distance": "10k",
                                     "target_sec": None, "show_on_today": True}
    assert goal_race.get_goal_race()["name"] == "Old goal"          # nothing chosen yet

    season_service.set_plan_goal_race(plan_id, race["id"])
    goal = goal_race.get_goal_race()

    assert (goal["name"], goal["date"], goal["distance"], goal["target_sec"]) == ("Tremblant 70.3", "2027-06-20", "70.3", 19800)
    payload = goal_race.settings_payload()
    assert payload["from_season"] is True and payload["goal"]["distance_long"] == "Middle distance (70.3)"

    season_service.set_plan_goal_race(plan_id, None)
    assert goal_race.get_goal_race()["name"] == "Old goal" and goal_race.settings_payload()["from_season"] is False


def test_only_the_active_plans_choice_drives_the_goal_race(season, fake_db):
    active = _plan(fake_db, "active-1", status="active")
    future = _plan(fake_db, "future-1", status="future")
    for pid in (active, future):
        season_service.link_plan(season["id"], pid)
    race = season_service.add_race(season["id"], TREMBLANT)["races"][0]

    season_service.set_plan_goal_race(future, race["id"])
    assert goal_race.get_goal_race() is None

    season_service.set_plan_goal_race(active, race["id"])
    assert goal_race.get_goal_race()["name"] == "Tremblant 70.3"


def test_a_ride_race_cannot_be_the_app_goal_race_so_the_saved_one_stays(season, fake_db):
    plan_id = _plan(fake_db, status="active")
    season_service.link_plan(season["id"], plan_id)
    race = season_service.add_race(season["id"], {**TREMBLANT, "name": "Gran Fondo", "type": "ride"})["races"][0]
    fake_db.settings["goal_race"] = {"name": "Saved", "date": "2027-01-01", "distance": "5k",
                                     "target_sec": None, "show_on_today": True}

    season_service.set_plan_goal_race(plan_id, race["id"])

    assert goal_race.get_goal_race()["name"] == "Saved"


def test_a_broken_season_lookup_falls_back_to_the_saved_goal_race(fake_db, monkeypatch):
    import db
    fake_db.settings["goal_race"] = {"name": "Saved", "date": "2027-01-01", "distance": "5k",
                                     "target_sec": None, "show_on_today": True}

    def boom():
        raise RuntimeError("seasons table missing")
    monkeypatch.setattr(db, "get_active_plan_goal_race", boom)

    assert goal_race.get_goal_race()["name"] == "Saved"
