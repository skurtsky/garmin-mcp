# tests/test_season_doc.py
"""The season data model (tools/season_doc.py): what is stored, what is
rejected, and what is only a warning. No database."""
import pytest

from tools import season_doc
from tools.season_doc import SeasonError

SEASON = {"id": "s_1", "name": "Season 2026–27", "startDate": "2026-09-14", "endDate": "2027-09-19",
          "status": "active", "goals": [], "objectives": []}


def race(name="Tremblant", date="2027-06-20", priority="A", type="70.3", **extra):
    return {"id": f"r_{name}", "seasonId": "s_1", "name": name, "date": date, "priority": priority,
            "type": type, "goal": "", "location": "", "url": "", "notes": "", **extra}


# ── season ────────────────────────────────────────────────────────────────────

def test_season_fields_clean_input():
    out = season_doc.season_fields({"name": "  Season 2026–27 ", "startDate": "2026-09-14T00:00:00", "endDate": "2027-09-19"})

    assert out == {"name": "Season 2026–27", "startDate": "2026-09-14", "endDate": "2027-09-19", "status": "active"}


@pytest.mark.parametrize("data, message", [
    ({"name": "", "startDate": "2026-09-14", "endDate": "2027-09-19"}, "name is required"),
    ({"name": "X", "startDate": "soon", "endDate": "2027-09-19"}, "Start date must be a date"),
    ({"name": "X", "startDate": "2026-09-14", "endDate": ""}, "End date must be a date"),
    ({"name": "X", "startDate": "2027-09-20", "endDate": "2027-09-19"}, "season ends .2027-09-19. before it starts"),
    ({"name": "X", "startDate": "2026-09-14", "endDate": "2027-09-19", "status": "paused"}, "Status must be one of"),
    ({"name": "x" * 121, "startDate": "2026-09-14", "endDate": "2027-09-19"}, "at most 120"),
])
def test_season_fields_rejects_invalid(data, message):
    with pytest.raises(SeasonError, match=message):
        season_doc.season_fields(data)


def test_season_fields_update_keeps_what_is_not_sent():
    out = season_doc.season_fields({"status": "archived"}, SEASON)

    assert out["name"] == SEASON["name"] and out["status"] == "archived"
    assert out["startDate"] == "2026-09-14"


def test_an_update_cannot_move_the_start_past_the_stored_end():
    with pytest.raises(SeasonError, match="before it starts"):
        season_doc.season_fields({"startDate": "2027-12-01"}, SEASON)


# ── races ─────────────────────────────────────────────────────────────────────

def test_race_fields_clean_input():
    out = season_doc.race_fields({"name": "Tremblant 70.3", "date": "2027-06-20", "priority": "a", "type": "70.3",
                                  "goal": "Sub 5:30", "url": "https://example.com/race"})

    assert out["priority"] == "A" and out["type"] == "70.3" and out["goal"] == "Sub 5:30"
    assert out["location"] == "" and out["notes"] == ""


@pytest.mark.parametrize("data, message", [
    ({"date": "2027-06-20", "priority": "A"}, "Race name is required"),
    ({"name": "X", "date": "June", "priority": "A"}, "Race date must be a date"),
    ({"name": "X", "date": "2027-06-20"}, "Priority is required"),
    ({"name": "X", "date": "2027-06-20", "priority": "D"}, "Priority must be one of"),
    ({"name": "X", "date": "2027-06-20", "priority": "A", "type": "swim"}, "Race type must be one of"),
    ({"name": "X", "date": "2027-06-20", "priority": "A", "url": "ftp://x"}, "URL must start with"),
])
def test_race_fields_rejects_invalid(data, message):
    with pytest.raises(SeasonError, match=message):
        season_doc.race_fields(data)


def test_race_type_defaults_to_other_and_update_keeps_the_rest():
    current = race(priority="B", type="half", goal="1:35")
    out = season_doc.race_fields({"priority": "A"}, {**current, "date": "2027-04-18"})

    assert out["priority"] == "A" and out["type"] == "half" and out["goal"] == "1:35" and out["date"] == "2027-04-18"
    assert season_doc.race_fields({"name": "X", "date": "2027-06-20", "priority": "C"})["type"] == "other"


def test_every_goal_race_distance_is_a_race_type():
    from tools import goal_race
    assert set(goal_race.DISTANCES) <= set(season_doc.RACE_TYPES)
    assert {"ride", "other"} <= set(season_doc.RACE_TYPES)


# ── goals and objectives ──────────────────────────────────────────────────────

def test_goals_accept_text_or_objects_and_drop_blank_rows():
    out = season_doc.goals_list(["Sub 5:30 at Tremblant", {"text": "Sub 3:30 marathon", "targetDate": "2027-09-19"},
                                 {"text": "  "}, ""])

    assert [g["text"] for g in out] == ["Sub 5:30 at Tremblant", "Sub 3:30 marathon"]
    assert out[1]["targetDate"] == "2027-09-19" and "raceId" not in out[0]
    assert all(g["id"].startswith("g_") for g in out)


def test_goals_keep_their_ids_when_resaved():
    out = season_doc.goals_list([{"id": "g_keep", "text": "Stay healthy"}])

    assert out[0]["id"] == "g_keep"


def test_goal_race_link_must_be_a_race_of_the_season():
    assert season_doc.goals_list([{"text": "Win", "raceId": "r_1"}], {"r_1"})[0]["raceId"] == "r_1"
    with pytest.raises(SeasonError, match="isn't in this season"):
        season_doc.goals_list([{"text": "Win", "raceId": "r_9"}], {"r_1"})


def test_objectives_have_a_hand_set_status_that_defaults_to_not_started():
    out = season_doc.objectives_list([{"text": "FTP 260 W"}, {"text": "20 km Z2", "status": "on_track"},
                                      {"text": "Swim 1900 m", "status": "met"}])

    assert [o["status"] for o in out] == ["not_started", "on_track", "met"]
    with pytest.raises(SeasonError, match="Objective status must be one of"):
        season_doc.objectives_list([{"text": "x", "status": "done"}])


def test_goal_and_objective_dates_are_validated():
    with pytest.raises(SeasonError, match="Target date must be a date"):
        season_doc.goals_list([{"text": "x", "targetDate": "later"}])
    with pytest.raises(SeasonError, match="must be a list"):
        season_doc.objectives_list("FTP")


# ── soft warnings ─────────────────────────────────────────────────────────────

def test_friel_limits_warn_but_never_block():
    races = [race(f"A{i}", priority="A") for i in range(4)] + [race(f"B{i}", priority="B") for i in range(6)]
    season = {**SEASON, "goals": [{"text": str(i)} for i in range(4)], "objectives": [{"text": str(i)} for i in range(5)]}

    warnings = season_doc.season_warnings(season, races, [])

    assert any("4 A races" in w for w in warnings)
    assert any("6 B races" in w for w in warnings)
    assert any("4 goals" in w for w in warnings)
    assert any("5 objectives" in w for w in warnings)


def test_at_the_limits_there_are_no_warnings():
    races = [race(f"A{i}", priority="A") for i in range(3)] + [race(f"B{i}", priority="B") for i in range(5)]
    season = {**SEASON, "goals": [{"text": "a"}] * 3, "objectives": [{"text": "a"}] * 4}

    assert season_doc.season_warnings(season, races, []) == []


def test_c_races_are_unlimited():
    assert season_doc.season_warnings(SEASON, [race(f"C{i}", priority="C") for i in range(20)], []) == []


def test_races_and_plans_outside_the_season_dates_are_warned_about():
    inside = {"id": "p1", "event": "Winter base", "start": "2026-09-14", "end": "2027-02-28"}
    straddles = {"id": "p2", "event": "Marathon", "start": "2027-06-28", "end": "2027-09-26"}
    before = {"id": "p3", "event": "Last year", "start": "2026-01-05", "end": "2026-03-01"}

    warnings = season_doc.season_warnings(SEASON, [race("Late", date="2027-12-01")], [inside, straddles, before])

    assert len(warnings) == 3
    assert any("Late" in w and "outside the season" in w for w in warnings)
    assert any("Marathon" in w for w in warnings) and any("Last year" in w for w in warnings)
    assert not any("Winter base" in w for w in warnings)


def test_a_plan_without_dates_is_not_warned_about():
    assert season_doc.plan_warning(SEASON, {"id": "p", "event": "E", "start": None, "end": None}) is None


# ── derived goal race ─────────────────────────────────────────────────────────

def test_a_triathlon_race_becomes_the_goal_race_with_a_parsed_target():
    goal = season_doc.derived_goal_race(race(type="70.3", goal="5:30:00"))

    assert goal == {"name": "Tremblant", "date": "2027-06-20", "distance": "70.3", "target_sec": 19800,
                    "show_on_today": True}


def test_a_free_text_goal_gives_no_target_time():
    assert season_doc.derived_goal_race(race(type="marathon", goal="enjoy it"))["target_sec"] is None


def test_ride_and_other_races_have_no_goal_race():
    assert season_doc.derived_goal_race(race(type="ride")) is None
    assert season_doc.derived_goal_race(race(type="other")) is None
    assert season_doc.derived_goal_race(None) is None
