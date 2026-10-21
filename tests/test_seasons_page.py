# tests/test_seasons_page.py
"""The Seasons settings pages (tools/seasons_page.py) and the viewer's
goal-race dropdown API (tools/training_plan.py), through Starlette's
TestClient on the in-memory fake of db.py."""
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

import db
from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import goal_race, plan_service, season_service, training_plan

TOKEN = {"token": "t0k"}
NEW = {"name": "Season 2026–27", "startDate": "2026-09-01", "endDate": "2027-09-30"}
RACE = {"name": "Tremblant 70.3", "date": "2027-06-20", "priority": "A", "type": "70.3", "goal": "5:30:00",
        "location": "", "url": "", "notes": ""}


@pytest.fixture
def client(fake_db):
    return TestClient(training_plan.create_app(), follow_redirects=False)


def _query(response):
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


def _path(response):
    return urlsplit(response.headers["location"]).path


@pytest.fixture
def season(client):
    r = client.post("/training-plan/seasons", params=TOKEN, data=NEW)
    return _path(r).split("/")[3]


def _plan(plan_id="block-1", activate=False):
    plan_service.create_plan(sample_plan(id=plan_id))
    if activate:
        plan_service.activate(plan_id)
    return plan_id


# ── pages ─────────────────────────────────────────────────────────────────────

def test_the_list_starts_empty_with_a_create_form(client):
    r = client.get("/training-plan/seasons", params=TOKEN)

    assert r.status_code == 200 and "No seasons yet" in r.text
    assert 'name="startDate"' in r.text and 'action="/training-plan/seasons?token=t0k"' in r.text
    assert r.headers["cache-control"] == "no-store" and r.text.count('id="gm-nav"') == 1


def test_create_a_season_goes_to_its_page_and_keeps_the_token(client):
    r = client.post("/training-plan/seasons", params=TOKEN, data=NEW)

    assert r.status_code == 303 and _path(r).endswith("/edit") and _query(r)["token"] == "t0k"
    page = client.get(_path(r), params=TOKEN)
    assert "Season 2026–27" in page.text and "2026-09-01 – 2027-09-30" in page.text
    assert "Season created." in client.get(r.headers["location"]).text


def test_create_with_bad_dates_shows_the_error_and_stores_nothing(client, fake_db):
    r = client.post("/training-plan/seasons", params=TOKEN, data={**NEW, "endDate": "2026-01-01"})

    assert r.status_code == 303 and _path(r) == "/training-plan/seasons"
    assert "before it starts" in _query(r)["error"] and fake_db.seasons == {}
    assert "before it starts" in client.get(r.headers["location"]).text


def test_the_list_shows_counts_and_warnings_and_archived_seasons_apart(client, season):
    client.post(f"/training-plan/seasons/{season}/races", params=TOKEN, data={**RACE, "date": "2028-01-01"})
    client.post("/training-plan/seasons", params=TOKEN, data={**NEW, "name": "Old"})
    old = next(s["id"] for s in season_service.list_seasons() if s["name"] == "Old")
    client.post(f"/training-plan/seasons/{old}/archive", params=TOKEN)

    text = client.get("/training-plan/seasons", params=TOKEN).text

    assert "1 race(s)" in text and "1 warning(s)" in text
    assert text.index("Season 2026–27") < text.index("Archived") < text.index(">Old<")
    assert "Restore" in text


def test_edit_save_details_archive_and_restore(client, season):
    r = client.post(f"/training-plan/seasons/{season}/details", params=TOKEN,
                    data={"name": "Renamed", "startDate": "2026-08-01", "endDate": "2027-10-31"})
    assert r.status_code == 303 and _query(r)["notice"] == "Details saved."
    s = season_service.get_season(season)
    assert (s["name"], s["startDate"], s["endDate"]) == ("Renamed", "2026-08-01", "2027-10-31")

    client.post(f"/training-plan/seasons/{season}/archive", params=TOKEN)
    assert season_service.get_season(season)["status"] == "archived"
    assert "Restore season" in client.get(f"/training-plan/seasons/{season}/edit", params=TOKEN).text
    client.post(f"/training-plan/seasons/{season}/restore", params=TOKEN)
    assert season_service.get_season(season)["status"] == "active"


def test_link_and_unlink_plans(client, season):
    plan_id = _plan()

    r = client.post(f"/training-plan/seasons/{season}/link", params=TOKEN, data={"plan_id": plan_id})
    assert _query(r)["notice"] == "Plan linked."
    page = client.get(f"/training-plan/seasons/{season}/edit", params=TOKEN).text
    assert "Test Block" in page and "Unlink" in page
    client.post(f"/training-plan/seasons/{season}/unlink", params=TOKEN, data={"plan_id": plan_id})
    assert season_service.get_season(season)["planIds"] == []


def test_a_plan_inside_the_dates_shows_no_warning(client, season):
    plan_id = _plan()
    client.post(f"/training-plan/seasons/{season}/link", params=TOKEN, data={"plan_id": plan_id})

    assert 'class="warnings"' not in client.get(f"/training-plan/seasons/{season}/edit", params=TOKEN).text


def test_a_plan_outside_the_dates_is_linked_with_a_visible_warning(client):
    late = client.post("/training-plan/seasons", params=TOKEN, data={**NEW, "startDate": "2026-10-01"})
    sid = _path(late).split("/")[3]
    plan_id = _plan()

    client.post(f"/training-plan/seasons/{sid}/link", params=TOKEN, data={"plan_id": plan_id})
    page = client.get(f"/training-plan/seasons/{sid}/edit", params=TOKEN).text

    assert 'class="warnings"' in page and "runs 2026-09-14 – 2026-09-27, outside the season" in page


def test_the_link_picker_names_the_season_a_plan_would_leave(client, season):
    plan_id = _plan()
    client.post(f"/training-plan/seasons/{season}/link", params=TOKEN, data={"plan_id": plan_id})
    other = _path(client.post("/training-plan/seasons", params=TOKEN, data={**NEW, "name": "Other"})).split("/")[3]

    page = client.get(f"/training-plan/seasons/{other}/edit", params=TOKEN).text

    assert "Test Block — in Season 2026–27" in page


def test_add_edit_and_delete_a_race(client, season):
    r = client.post(f"/training-plan/seasons/{season}/races", params=TOKEN, data=RACE)
    assert _query(r)["notice"] == "Race added."
    race = season_service.get_season(season)["races"][0]
    assert race["name"] == "Tremblant 70.3" and race["priority"] == "A"
    page = client.get(f"/training-plan/seasons/{season}/edit", params=TOKEN).text
    assert "Tremblant 70.3" in page and "Run half marathon" in page and 'value="5:30:00"' in page

    client.post(f"/training-plan/seasons/{season}/races/{race['id']}/save", params=TOKEN,
                data={**RACE, "priority": "B", "location": "Mont-Tremblant"})
    saved = season_service.get_season(season)["races"][0]
    assert saved["priority"] == "B" and saved["location"] == "Mont-Tremblant"

    r = client.post(f"/training-plan/seasons/{season}/races/{race['id']}/delete", params=TOKEN)
    assert _query(r)["notice"] == "Race deleted." and season_service.get_season(season)["races"] == []


def test_a_bad_race_is_reported_not_stored(client, season):
    r = client.post(f"/training-plan/seasons/{season}/races", params=TOKEN, data={**RACE, "url": "ftp://x"})

    assert "URL must start with" in _query(r)["error"] and season_service.get_season(season)["races"] == []


def test_friel_warnings_show_on_the_page_but_do_not_block(client, season):
    for i in range(4):
        client.post(f"/training-plan/seasons/{season}/races", params=TOKEN, data={**RACE, "name": f"A{i}"})

    page = client.get(f"/training-plan/seasons/{season}/edit", params=TOKEN).text

    assert len(season_service.get_season(season)["races"]) == 4 and "4 A races; Friel suggests at most 3" in page


def test_goals_and_objectives_forms(client, season):
    client.post(f"/training-plan/seasons/{season}/races", params=TOKEN, data=RACE)
    race_id = season_service.get_season(season)["races"][0]["id"]

    client.post(f"/training-plan/seasons/{season}/goals", params=TOKEN, data={
        "id": ["", ""], "text": ["Sub 5:30", ""], "targetDate": ["2027-06-20", ""], "raceId": [race_id, ""]})
    client.post(f"/training-plan/seasons/{season}/objectives", params=TOKEN, data={
        "id": ["", ""], "text": ["FTP 260 W", ""], "targetDate": ["", ""], "raceId": ["", ""],
        "status": ["on_track", "not_started"]})
    s = season_service.get_season(season)

    assert [(g["text"], g["raceId"], g["targetDate"]) for g in s["goals"]] == [("Sub 5:30", race_id, "2027-06-20")]
    assert [(o["text"], o["status"]) for o in s["objectives"]] == [("FTP 260 W", "on_track")]
    page = client.get(f"/training-plan/seasons/{season}/edit", params=TOKEN).text
    assert 'value="Sub 5:30"' in page and '<option value="on_track" selected>On track</option>' in page


def test_unknown_season_and_action(client, season):
    r = client.get("/training-plan/seasons/s_nope/edit", params=TOKEN)
    assert r.status_code == 303 and _path(r) == "/training-plan/seasons" and "No season" in _query(r)["error"]
    assert client.post(f"/training-plan/seasons/{season}/explode", params=TOKEN).status_code == 404
    r = client.post("/training-plan/seasons/s_nope/archive", params=TOKEN)
    assert "No season" in _query(r)["error"]


def test_without_a_database_the_pages_explain_why(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    c = TestClient(training_plan.create_app())

    r = c.get("/training-plan/seasons", params=TOKEN)
    assert r.status_code == 503 and "DATABASE_URL" in r.text
    assert c.post("/training-plan/seasons", params=TOKEN, data=NEW).status_code == 503


def test_the_plans_page_links_to_seasons(client):
    assert "/training-plan/seasons?token=t0k" in client.get("/training-plan/plans", params=TOKEN).text


# ── viewer: the goal-race dropdown ────────────────────────────────────────────

def _server(html):
    return json.loads(re.search(r'<script id="plan-server" type="application/json">(.*?)</script>', html, re.S).group(1))


def test_a_plan_without_a_season_is_offered_the_active_seasons(client, season):
    _plan(activate=True)

    server = _server(client.get("/training-plan", params=TOKEN).text)

    assert server["seasonChoice"] == {"season": None, "seasons": [{"id": season, "name": "Season 2026–27"}]}


def test_link_a_plan_then_pick_its_goal_race_from_the_viewer(client, season):
    _plan(activate=True)
    client.post(f"/training-plan/seasons/{season}/races", params=TOKEN, data=RACE)
    race_id = season_service.get_season(season)["races"][0]["id"]

    r = client.post("/training-plan/api/plan-season", params=TOKEN, json={"season_id": season})
    assert r.status_code == 200 and r.json()["seasonChoice"]["season"]["id"] == season
    assert [x["name"] for x in r.json()["seasonChoice"]["races"]] == ["Tremblant 70.3"]
    assert r.json()["goalRace"]["goal"] is None

    r = client.post("/training-plan/api/season-goal-race", params=TOKEN, json={"race_id": race_id})
    body = r.json()
    assert body["seasonChoice"]["selectedRaceId"] == race_id
    assert body["goalRace"]["from_season"] is True
    assert body["goalRace"]["goal"]["name"] == "Tremblant 70.3" and body["goalRace"]["goal"]["target"] == "5:30:00"
    assert goal_race.get_goal_race()["distance"] == "70.3"
    assert _server(client.get("/training-plan", params=TOKEN).text)["seasonChoice"]["selectedRaceId"] == race_id

    r = client.post("/training-plan/api/season-goal-race", params=TOKEN, json={"race_id": None})
    assert r.json()["seasonChoice"]["selectedRaceId"] is None and r.json()["goalRace"]["goal"] is None


def test_unlinking_from_the_viewer(client, season):
    plan_id = _plan(activate=True)
    client.post("/training-plan/api/plan-season", params=TOKEN, json={"season_id": season})

    r = client.post("/training-plan/api/plan-season", params=TOKEN, json={"season_id": None})

    assert r.json()["seasonChoice"]["season"] is None and season_service.season_for_plan(plan_id) is None


def test_viewer_api_errors(client, season):
    assert client.post("/training-plan/api/plan-season", params=TOKEN, json={"season_id": season}).status_code == 404  # no plan
    _plan(activate=True)
    r = client.post("/training-plan/api/season-goal-race", params=TOKEN, json={"race_id": "r_1"})
    assert r.status_code == 400 and "Link this plan" in r.json()["error"]
    assert client.post("/training-plan/api/plan-season", params=TOKEN, json={"season_id": "s_nope"}).status_code == 404
    assert client.post("/training-plan/api/plan-season", params=TOKEN, content=b"[1]").status_code == 400


def test_the_dropdown_works_on_an_other_plan_selected_by_id(client, season):
    _plan("active-1", activate=True)
    _plan("future-1")

    r = client.post("/training-plan/api/plan-season", params={**TOKEN, "plan": "future-1"}, json={"season_id": season})

    assert r.status_code == 200
    assert season_service.season_for_plan("future-1")["id"] == season and season_service.season_for_plan("active-1") is None


def test_the_viewer_template_has_the_dropdown_and_the_seasons_links(client):
    text = open(training_plan.TEMPLATE_PATH, encoding="utf-8").read()

    for needle in ("data-season-goal", "data-season-link", "season-goal-race", "plan-season", "/training-plan/seasons"):
        assert needle in text
