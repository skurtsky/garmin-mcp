# tests/test_season_view.py
"""The Season view (tools/season_view.py): the page and its routes, through
Starlette's TestClient on the in-memory fake of db.py. The week-by-week rules
themselves are in tests/test_season_timeline.py."""
import re
from datetime import date
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

import db
from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import plan_service, season_service, training_plan

TOKEN = {"token": "t0k"}
NEW = {"name": "Season 2026–27", "startDate": "2026-09-14", "endDate": "2026-10-18"}
TODAY = date(2026, 9, 16)   # a Wednesday, in week 1 of the sample plan


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(season_service, "local_today", lambda: TODAY)


@pytest.fixture
def client(fake_db):
    return TestClient(training_plan.create_app(), follow_redirects=False)


def _plan(plan_id="block-1", status="future", skeleton=False, **meta):
    """The sample plan: week 1 has workouts (3.83 h planned, target 3), week 2 one run (target 4)."""
    plan = sample_plan(id=plan_id, **meta)
    if skeleton:
        plan["weeks"][1]["days"] = []
        plan["weeks"][1]["status"] = "skeleton"
    plan_service.create_plan(plan)
    if status == "active":
        plan_service.activate(plan_id)
    return plan_id


@pytest.fixture
def season(fake_db):
    return season_service.create_season({**NEW, "goals": ["Sub 5:30 at Tremblant"],
                                         "objectives": [{"text": "FTP 260 W", "status": "on_track", "targetDate": "2026-10-10"}]})["id"]


def _view(client, season_id, **params):
    return client.get(f"/training-plan/seasons/{season_id}", params={**TOKEN, **params})


# ── routes ────────────────────────────────────────────────────────────────────

def test_the_season_route_opens_the_active_plans_season_and_keeps_the_token(client, season):
    _plan(status="active")
    season_service.link_plan(season, "block-1")
    other = season_service.create_season({**NEW, "name": "Other", "startDate": "2027-01-01", "endDate": "2027-12-31"})

    r = client.get("/training-plan/season", params=TOKEN)

    assert r.status_code == 303 and r.headers["location"] == f"/training-plan/seasons/{season}?token=t0k"
    assert other["id"] != season


def test_without_a_linked_active_plan_the_newest_active_season_opens(client, season):
    newer = season_service.create_season({**NEW, "name": "Newer", "startDate": "2027-01-01", "endDate": "2027-12-31"})["id"]
    season_service.update_season(season, {"status": "archived"})

    r = client.get("/training-plan/season", params=TOKEN)

    assert r.headers["location"] == f"/training-plan/seasons/{newer}?token=t0k"


def test_with_no_season_the_page_says_so_and_lists_the_unassigned_plans(client):
    _plan("alpha", event="Alpha Block")

    r = client.get("/training-plan/season", params=TOKEN)

    assert r.status_code == 200 and "No season yet" in r.text and "Not in a season" in r.text
    assert "Alpha Block" in r.text and "/training-plan/seasons?token=t0k" in r.text


def test_an_unknown_season_goes_back_to_the_list_with_an_error(client):
    r = client.get("/training-plan/seasons/s_nope", params=TOKEN)

    assert r.status_code == 303 and urlsplit(r.headers["location"]).path == "/training-plan/seasons"
    assert "No+season" in r.headers["location"]


def test_without_a_database_the_view_explains_why(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    c = TestClient(training_plan.create_app())

    assert c.get("/training-plan/seasons/s_1", params=TOKEN).status_code == 503
    assert "DATABASE_URL" in c.get("/training-plan/season", params=TOKEN).text


def test_the_season_name_links_to_the_view_from_the_list_and_the_edit_page(client, season):
    assert f'href="/training-plan/seasons/{season}?token=t0k"' in client.get("/training-plan/seasons", params=TOKEN).text
    assert f'href="/training-plan/seasons/{season}?token=t0k"' in client.get(f"/training-plan/seasons/{season}/edit", params=TOKEN).text
    assert f'/training-plan/seasons/{season}/edit?token=t0k' in _view(client, season).text


# ── the page ──────────────────────────────────────────────────────────────────

def test_the_page_shows_goals_objectives_and_the_week_view_switch(client, season):
    r = _view(client, season)

    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and r.text.count('id="gm-nav"') == 1
    assert "Season 2026–27" in r.text and "Sep 14 – Oct 18, 2026" in r.text
    assert "Sub 5:30 at Tremblant" in r.text and "FTP 260 W" in r.text
    assert '<span class="pill on_track">On track</span>' in r.text and "by Oct 10, 2026" in r.text
    assert '<a href="/training-plan?mode=week&amp;token=t0k">Week</a>' in r.text
    assert 'href="/training-plan?mode=month&amp;token=t0k">Month</a>' in r.text
    assert '<span aria-current="page">Season</span>' in r.text


def test_planned_weeks_show_hours_and_workouts_done_in_their_tooltip(client, season):
    _plan(status="active")
    season_service.link_plan(season, "block-1")
    client.post("/training-plan/api/completion", params=TOKEN, json={"workout_id": "w1-tue-run"})

    text = _view(client, season).text

    assert "Planned 3.8 h, 1 of 4 workouts done" in text
    assert "Planned 0.9 h, 0 of 1 workouts done" in text
    assert "Test Block · week 1" in text and "Sep 14 – Sep 20, 2026" in text and "Base" in text


def test_a_skeleton_week_shows_its_target_hatched_and_the_totals_say_they_include_targets(client, season):
    _plan(skeleton=True)
    season_service.link_plan(season, "block-1")

    text = _view(client, season).text

    assert "Target 4.0 h (no workouts yet)" in text and "Planned 3.8 h, 0 of 4 workouts done" in text
    assert '<pattern id="hatch0"' in text and 'fill="url(#hatch0)"' in text
    assert "planned total · includes targets" in text and "average per week, over 2 week(s) with hours · includes targets" in text
    assert "* includes weeks with target hours only" in text and "8 h" in text   # 3.83 + 4.0


def test_with_every_week_planned_there_is_no_hatching_and_no_includes_targets(client, season):
    _plan()
    season_service.link_plan(season, "block-1")

    text = _view(client, season).text

    assert "hatch0" not in text and "includes targets" not in text
    assert "Target " not in text.split('<div class="season">')[1].split("</svg>")[0]


def test_a_week_with_neither_workouts_nor_a_target_is_an_empty_slot(client, season):
    plan = sample_plan()
    plan["weeks"][1]["days"] = []
    plan["weeks"][1]["status"] = "skeleton"
    del plan["weeks"][1]["targetHours"]
    plan_service.create_plan(plan)
    season_service.link_plan(season, "test-block-2026")

    text = _view(client, season).text

    assert "No workouts or target hours yet" in text and 'class="s-empty"' in text


def test_gaps_between_plans_are_explicit_unplanned_weeks(client, season):
    _plan()
    season_service.link_plan(season, "block-1")
    season_service.add_race(season, {"name": "Tremblant", "date": "2026-09-27", "priority": "A", "type": "70.3"})

    text = _view(client, season).text

    assert "Unplanned week (after Tremblant)" in text and 'class="s-gap"' in text
    assert "Season week 3" in text and "3 unplanned" in text
    assert ">Unplanned<" in text   # the dashed segment in the phase band


def test_a_week_opens_in_its_plan_the_active_plan_by_bare_url_and_others_by_id(client, season):
    _plan("active-1", status="active")
    _plan("future-1", event="Next Block")
    season_service.link_plan(season, "active-1")

    text = _view(client, season).text

    assert 'href="/training-plan?week=1&amp;token=t0k"' in text
    other = season_service.create_season({**NEW, "name": "Later"})["id"]
    season_service.link_plan(other, "future-1")
    assert 'href="/training-plan?plan=future-1&amp;week=2&amp;token=t0k"' in _view(client, other).text


def test_race_markers_by_priority_with_the_week_they_fall_in(client, season):
    _plan(status="active")
    season_service.link_plan(season, "block-1")
    for name, d, p, t in [("Big one", "2026-10-10", "A", "70.3"), ("Mid", "2026-09-27", "B", "10k"),
                          ("Fun run", "2026-09-19", "C", "5k")]:
        season_service.add_race(season, {"name": name, "date": d, "priority": p, "type": t, "goal": "sub 5:30" if p == "A" else ""})

    text = _view(client, season).text

    assert text.count('class="s-rA"') == 1 and text.count('class="s-rB"') == 1 and text.count('class="s-rC"') == 1
    assert "Big one · A" in text and "Goal: sub 5:30" in text
    assert "Test Block · week 1" in text and "Test Block · week 2" in text   # the races table: Fun run, Mid
    assert "Unplanned week" in text                                          # Big one: the week no plan covers


def test_races_in_plans_show_as_small_c_markers_unless_a_season_race_covers_them(client, season):
    plan = sample_plan()
    plan["weeks"][1]["days"][0]["workouts"].append({"id": "w2-race", "sport": "race", "name": "Fall 10K", "durationMinutes": 45})
    plan_service.create_plan(plan)
    season_service.link_plan(season, "test-block-2026")

    text = _view(client, season).text
    assert text.count('class="s-rC"') == 1 and "Fall 10K" in text and "(from the plan)" in text

    season_service.add_race(season, {"name": "Fall 10K", "date": "2026-09-22", "priority": "B", "type": "10k"})
    again = _view(client, season).text
    assert again.count('class="s-rC"') == 0 and again.count('class="s-rB"') == 1 and "(from the plan)" not in again


def test_weeks_to_the_next_a_race(client, season):
    season_service.update_season(season, {"endDate": "2027-09-30"})
    season_service.add_race(season, {"name": "Tremblant", "date": "2027-06-20", "priority": "A", "type": "70.3"})
    season_service.add_race(season, {"name": "Earlier B", "date": "2026-10-01", "priority": "B", "type": "10k"})

    text = _view(client, season).text

    assert "<b>39 wk</b>" in text and "Tremblant · Jun 20, 2027 (277 days)" in text


def test_with_no_a_race_ahead_the_stat_says_so(client, season):
    assert "No A race ahead" in _view(client, season).text


def test_the_today_line_is_drawn_when_today_is_in_the_timeline(client, season, monkeypatch):
    assert 'class="s-today"' in _view(client, season).text
    monkeypatch.setattr(season_service, "local_today", lambda: date(2030, 1, 1))
    assert 'class="s-today"' not in _view(client, season).text


def test_plan_totals_table_and_the_unassigned_plans(client, season):
    _plan("in-season", event="In Season")
    season_service.link_plan(season, "in-season")
    _plan("loose", event="Loose Plan")

    text = _view(client, season).text

    assert "In Season" in text and "4.8 h" in text.split("<h2>Plans</h2>")[1]   # 3.83 + 0.92
    assert "Not in a season" in text and "Loose Plan" in text.split("Not in a season")[1]
    assert "In Season" not in text.split("Not in a season")[1]


def test_soft_warnings_are_shown_on_the_page(client, season):
    late = season_service.create_season({**NEW, "name": "Late", "startDate": "2026-10-01"})["id"]
    _plan()
    season_service.link_plan(late, "block-1")

    text = _view(client, late).text

    assert 'class="warnings"' in text and "outside the season" in text
    assert "Test Block · week 1" in text   # the plan is still drawn: the timeline widens to include it


def test_the_page_is_safe_against_markup_in_names(client, season):
    season_service.update_season(season, {"name": "<script>alert(1)</script>"})
    season_service.set_goals(season, ["<img src=x onerror=1>"])

    text = _view(client, season).text

    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<img src=x" not in text


def test_the_page_reads_plans_in_one_call(client, season, monkeypatch):
    _plan("a")
    _plan("b")
    season_service.link_plan(season, "a")
    season_service.link_plan(season, "b")
    calls = []
    for name in ("get_training_plan", "get_workout_states"):
        original = getattr(db, name)
        monkeypatch.setattr(db, name, lambda *a, _n=name, _o=original: calls.append(_n) or _o(*a))

    r = _view(client, season)

    assert r.status_code == 200 and calls == []   # just db.get_plans_with_completion (b overlaps a, so a keeps the weeks)
    assert re.search(r'role="group" aria-label="Season timeline of 5 weeks across 1 plan', r.text)
