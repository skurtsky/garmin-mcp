# tests/test_season_tools.py
"""The season MCP tools (tools/season_tools.py, registered in server.py) and
the season block in get_training_plan, against the in-memory db fake."""
import asyncio
from datetime import date

import pytest
from fastmcp.exceptions import ToolError

import server
from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import plan_service, plan_tools, season_service, season_tools

SEASON = dict(name="Season 2026–27", start_date="2026-09-14", end_date="2026-10-18")
TREMBLANT = dict(name="Tremblant 70.3", date="2026-10-10", priority="A", type="70.3", goal="5:30:00")
TOOLS = ["list_seasons", "get_season", "create_season", "update_season", "link_plan_to_season",
         "add_race", "update_race", "delete_race", "set_season_goals", "set_season_objectives"]


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(season_service, "local_today", lambda: date(2026, 9, 16))


@pytest.fixture
def season(fake_db):
    return season_tools.create_season(**SEASON)


def _plan(plan_id="block-1", skeleton=False, activate=False):
    plan = sample_plan(id=plan_id)
    if skeleton:
        plan["weeks"][1]["days"] = []
        plan["weeks"][1]["status"] = "skeleton"
    plan_service.create_plan(plan)
    if activate:
        plan_service.activate(plan_id)
    return plan_id


# ── registration ──────────────────────────────────────────────────────────────

def test_every_season_tool_is_registered_with_its_arguments():
    async def fetch():
        return {n: (await server.mcp.get_tool(n)) for n in TOOLS}

    tools = asyncio.run(fetch())

    assert set(tools) == set(TOOLS)
    assert tools["create_season"].parameters["required"] == ["name", "start_date", "end_date"]
    assert tools["add_race"].parameters["required"] == ["season_id", "name", "date", "priority"]
    assert tools["link_plan_to_season"].parameters["required"] == ["plan_id"]
    assert tools["set_season_goals"].parameters["required"] == ["season_id", "goals"]
    for name in TOOLS:
        assert tools[name].description and len(tools[name].description) > 40


def test_the_docstrings_spell_out_the_rules_the_coach_needs():
    assert "targetHours" in server.get_season.__doc__ and "includesTargets" in server.get_season.__doc__
    assert "never mark one \"met\"" in server.set_season_objectives.__doc__
    assert "A\" (peak" in server.add_race.__doc__ and "70.3" in server.add_race.__doc__
    assert "season" in server.get_training_plan.__doc__


# ── create / read / update ────────────────────────────────────────────────────

def test_create_and_list_seasons(fake_db):
    s = season_tools.create_season(**SEASON, goals=["Sub 5:30", {"text": "Sub 3:30", "target_date": "2027-09-19"}],
                                   objectives=[{"text": "FTP 260 W", "status": "on_track"}])
    old = season_tools.create_season("Old", "2025-09-01", "2026-08-31")
    season_tools.update_season(old["id"], status="archived")

    assert s["goals"][1]["targetDate"] == "2027-09-19" and s["objectives"][0]["status"] == "on_track"
    everything = season_tools.list_seasons()
    assert [x["name"] for x in everything] == ["Season 2026–27", "Old"]
    assert everything[0] == {"id": s["id"], "name": "Season 2026–27", "startDate": "2026-09-14", "endDate": "2026-10-18",
                             "status": "active", "planIds": [], "raceCount": 0, "goalCount": 2, "objectiveCount": 1,
                             "warnings": []}
    assert [x["name"] for x in season_tools.list_seasons(include_archived=False)] == ["Season 2026–27"]


def test_update_season_changes_only_what_is_passed(season):
    out = season_tools.update_season(season["id"], name="Renamed")
    assert out["name"] == "Renamed" and out["startDate"] == "2026-09-14" and out["status"] == "active"

    out = season_tools.update_season(season["id"], end_date="2026-11-01", status="archived")
    assert (out["endDate"], out["status"], out["name"]) == ("2026-11-01", "archived", "Renamed")


def test_get_season_defaults_to_the_active_plans_season_and_reports_totals(season):
    _plan(activate=True)
    season_tools.link_plan_to_season("block-1", season["id"])
    season_tools.add_race(season["id"], **TREMBLANT)

    out = season_tools.get_season()

    assert out["id"] == season["id"] and out["planIds"] == ["block-1"] and out["races"][0]["name"] == "Tremblant 70.3"
    assert out["totals"]["hours"] == pytest.approx(4.75, abs=0.01) and out["totals"]["includesTargets"] is False
    assert out["nextARace"]["name"] == "Tremblant 70.3" and out["nextARace"]["days"] == 24 and out["nextARace"]["weeks"] == 3
    assert "weeks" not in out["timeline"] and out["timeline"]["weekCount"] == 5


def test_get_season_weeks_show_the_target_fallback_and_gaps(season):
    _plan(skeleton=True)
    season_tools.link_plan_to_season("block-1", season["id"])

    weeks = season_tools.get_season(season["id"], include_weeks=True)["timeline"]["weeks"]

    assert [(w["week"], w["kind"], w["hours"]) for w in weeks] == [
        (1, "planned", 3.83), (2, "target", 4.0), (3, "unplanned", None), (4, "unplanned", None), (5, "unplanned", None)]
    assert weeks[0]["plan"] == "Test Block" and weeks[1]["planWeek"] == 2 and weeks[0]["phase"] == "Base"
    assert season_tools.get_season(season["id"])["totals"]["includesTargets"] is True


def test_errors_are_tool_errors_the_assistant_can_act_on(season, fake_db):
    fake_db.seasons.clear()
    with pytest.raises(ToolError, match="No season yet"):
        season_tools.get_season()
    with pytest.raises(ToolError, match="No season with id"):
        season_tools.get_season("s_nope")
    with pytest.raises(ToolError, match="before it starts"):
        season_tools.create_season("X", "2026-10-01", "2026-09-01")
    with pytest.raises(ToolError, match="Start date must be a date"):
        season_tools.create_season("X", "soon", "2026-09-01")


def test_without_a_database_the_tools_say_so(monkeypatch):
    import db
    monkeypatch.setattr(db, "is_configured", lambda: False)

    with pytest.raises(ToolError, match="DATABASE_URL"):
        season_tools.list_seasons()


# ── plans ─────────────────────────────────────────────────────────────────────

def test_link_a_plan_choose_its_goal_race_and_unlink(season):
    _plan()
    race = season_tools.add_race(season["id"], **TREMBLANT)["races"][0]

    out = season_tools.link_plan_to_season("block-1", season["id"], goal_race_id=race["id"])

    assert out["linked"] is True and out["goalRaceId"] == race["id"] and out["season"]["planIds"] == ["block-1"]
    assert season_service.season_for_plan("block-1")["planGoalRaces"] == {"block-1": race["id"]}
    assert season_tools.link_plan_to_season("block-1", season["id"], goal_race_id="")["goalRaceId"] is None

    gone = season_tools.link_plan_to_season("block-1")
    assert gone["linked"] is False and "Unlinked from “Season 2026–27”" in gone["message"]
    assert season_tools.link_plan_to_season("block-1")["message"] == "The plan was in no season."


def test_link_errors(season):
    _plan()
    with pytest.raises(ToolError, match="No plan with id"):
        season_tools.link_plan_to_season("nope", season["id"])
    with pytest.raises(ToolError, match="No season with id"):
        season_tools.link_plan_to_season("block-1", "s_nope")
    with pytest.raises(ToolError, match="isn't in the season"):
        season_tools.link_plan_to_season("block-1", season["id"], goal_race_id="r_other")
    with pytest.raises(ToolError, match="needs a season_id"):
        season_tools.link_plan_to_season("block-1", goal_race_id="r_1")
    season_tools.update_season(season["id"], status="archived")
    with pytest.raises(ToolError, match="archived"):
        season_tools.link_plan_to_season("block-1", season["id"])


def test_a_plan_outside_the_dates_links_with_a_warning(fake_db):
    late = season_tools.create_season("Late", "2026-10-01", "2027-03-01")
    _plan()

    out = season_tools.link_plan_to_season("block-1", late["id"])

    assert out["linked"] is True and any("outside the season" in w for w in out["season"]["warnings"])


# ── races ─────────────────────────────────────────────────────────────────────

def test_add_update_and_delete_a_race(season):
    added = season_tools.add_race(season["id"], **TREMBLANT, location="Mont-Tremblant", url="https://example.com/r")
    race = added["races"][0]
    assert (race["priority"], race["type"], race["location"]) == ("A", "70.3", "Mont-Tremblant")

    out = season_tools.update_race(season["id"], race["id"], priority="B", goal="")
    saved = out["races"][0]
    assert (saved["priority"], saved["goal"], saved["name"], saved["location"]) == ("B", "", "Tremblant 70.3", "Mont-Tremblant")

    gone = season_tools.delete_race(season["id"], race["id"])
    assert gone["deleted"]["name"] == "Tremblant 70.3" and gone["season"]["races"] == []


def test_race_defaults_and_validation(season):
    out = season_tools.add_race(season["id"], "Fun run", "2026-09-27", "C")
    assert out["races"][0]["type"] == "other" and out["races"][0]["goal"] == ""
    with pytest.raises(ToolError, match="Priority must be one of"):
        season_tools.add_race(season["id"], "X", "2026-09-27", "Z")
    with pytest.raises(ToolError, match="Race type must be one of"):
        season_tools.add_race(season["id"], "X", "2026-09-27", "A", type="swim")
    with pytest.raises(ToolError, match="Race date must be a date"):
        season_tools.add_race(season["id"], "X", "next week", "A")
    with pytest.raises(ToolError, match="has no race"):
        season_tools.update_race(season["id"], "r_nope", name="X")
    with pytest.raises(ToolError, match="has no race"):
        season_tools.delete_race(season["id"], "r_nope")


def test_friel_limits_are_warnings_through_the_tools_too(season):
    for i in range(4):
        out = season_tools.add_race(season["id"], f"A{i}", "2026-09-27", "A")

    assert len(out["races"]) == 4 and any("4 A races" in w for w in out["warnings"])
    assert season_tools.list_seasons()[0]["warnings"] == out["warnings"]


def test_deleting_a_race_keeps_goal_text_and_drops_only_the_link(season):
    race = season_tools.add_race(season["id"], **TREMBLANT)["races"][0]
    season_tools.set_season_goals(season["id"], [{"text": "Sub 5:30", "race_id": race["id"]}])

    out = season_tools.delete_race(season["id"], race["id"])["season"]

    assert out["goals"][0]["text"] == "Sub 5:30" and "raceId" not in out["goals"][0]


# ── goals and objectives ──────────────────────────────────────────────────────

def test_set_goals_accepts_snake_case_and_replaces_the_list(season):
    race = season_tools.add_race(season["id"], **TREMBLANT)["races"][0]

    out = season_tools.set_season_goals(season["id"], [{"text": "Sub 5:30", "race_id": race["id"], "target_date": "2026-10-10"}, "Stay healthy"])
    assert out["goals"][0]["raceId"] == race["id"] and out["goals"][0]["targetDate"] == "2026-10-10"

    assert season_tools.set_season_goals(season["id"], ["Only one"])["goals"][0]["text"] == "Only one"
    assert season_tools.set_season_goals(season["id"], [])["goals"] == []
    with pytest.raises(ToolError, match="isn't in this season"):
        season_tools.set_season_goals(season["id"], [{"text": "x", "race_id": "r_gone"}])
    with pytest.raises(ToolError, match="must be a list"):
        season_tools.set_season_goals(season["id"], "Sub 5:30")


def test_set_objectives_keeps_ids_and_statuses_the_athlete_set(season):
    first = season_tools.set_season_objectives(season["id"], [{"text": "FTP 260 W"}, {"text": "Swim 1900 m"}])["objectives"]
    oid = first[0]["id"]

    out = season_tools.set_season_objectives(season["id"], [{"id": oid, "text": "FTP 260 W", "status": "met"},
                                                            {"text": "Swim 1900 m", "status": "on_track"}])

    assert [(o["text"], o["status"]) for o in out["objectives"]] == [("FTP 260 W", "met"), ("Swim 1900 m", "on_track")]
    assert out["objectives"][0]["id"] == oid
    with pytest.raises(ToolError, match="Objective status must be one of"):
        season_tools.set_season_objectives(season["id"], [{"text": "x", "status": "done"}])


def test_five_objectives_are_kept_with_a_warning(season):
    out = season_tools.set_season_objectives(season["id"], [f"o{i}" for i in range(5)])

    assert len(out["objectives"]) == 5 and any("5 objectives" in w for w in out["warnings"])


# ── get_training_plan ─────────────────────────────────────────────────────────

def test_get_training_plan_carries_a_short_season_summary_when_linked(season):
    _plan(activate=True)
    race = season_tools.add_race(season["id"], **TREMBLANT)["races"][0]
    season_tools.set_season_goals(season["id"], [{"text": "Sub 5:30", "race_id": race["id"]}])
    season_tools.link_plan_to_season("block-1", season["id"], goal_race_id=race["id"])

    overview = plan_tools.get_training_plan(include_weeks=False)

    assert overview["season"] == {
        "id": season["id"], "name": "Season 2026–27", "startDate": "2026-09-14", "endDate": "2026-10-18", "status": "active",
        "goals": [{"text": "Sub 5:30", "raceId": race["id"]}],
        "goalRace": {"id": race["id"], "name": "Tremblant 70.3", "date": "2026-10-10", "priority": "A", "type": "70.3",
                     "goal": "5:30:00"}}


def test_get_training_plan_without_a_season_is_unchanged(fake_db):
    _plan(activate=True)

    assert "season" not in plan_tools.get_training_plan(include_weeks=False)


def test_a_linked_plan_with_no_goal_race_has_a_null_goal_race(season):
    _plan(activate=True)
    season_tools.link_plan_to_season("block-1", season["id"])

    assert plan_tools.get_training_plan(include_weeks=False)["season"]["goalRace"] is None


def test_a_broken_season_lookup_does_not_break_get_training_plan(season, monkeypatch):
    _plan(activate=True)
    season_tools.link_plan_to_season("block-1", season["id"])
    monkeypatch.setattr(season_service, "plan_summary", lambda plan_id: 1 / 0)

    overview = plan_tools.get_training_plan(include_weeks=False)

    assert overview["id"] == "block-1" and "season" not in overview


def test_a_future_plan_read_by_id_gets_its_own_seasons_summary(season):
    _plan("active-1", activate=True)
    _plan("future-1")
    season_tools.link_plan_to_season("future-1", season["id"])

    assert plan_tools.get_training_plan("future-1", include_weeks=False)["season"]["name"] == "Season 2026–27"
    assert "season" not in plan_tools.get_training_plan(include_weeks=False)


# ── through the MCP server itself ─────────────────────────────────────────────

def test_the_tools_work_end_to_end_through_the_mcp_server(fake_db):
    from fastmcp import Client

    async def run():
        async with Client(server.mcp) as c:
            made = (await c.call_tool("create_season", SEASON)).data
            sid = made["id"]
            await c.call_tool("add_race", {"season_id": sid, "name": "Tremblant", "date": "2026-10-10", "priority": "A", "type": "70.3"})
            await c.call_tool("set_season_objectives", {"season_id": sid, "objectives": [{"text": "FTP 260 W", "status": "on_track"}]})
            got = (await c.call_tool("get_season", {"season_id": sid})).data
            error = await c.call_tool("add_race", {"season_id": sid, "name": "X", "date": "2026-10-10", "priority": "Z"},
                                      raise_on_error=False)
            return got, error

    got, error = asyncio.run(run())

    assert got["races"][0]["name"] == "Tremblant" and got["objectives"][0]["status"] == "on_track"
    assert got["nextARace"]["name"] == "Tremblant"
    assert error.is_error and "Priority must be one of" in error.content[0].text
