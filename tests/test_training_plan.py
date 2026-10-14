# tests/test_training_plan.py
"""Tests for the /training-plan routes (tools/training_plan.py).

Fully offline: the plan functions in db.py are replaced by an in-memory fake
(tests/plan_fakes.py) and the routes are exercised with Starlette's
TestClient, so no PostgreSQL and no Garmin session are involved.
"""
import json
import re
from datetime import date, timedelta

import pytest
from starlette.responses import Response
from starlette.testclient import TestClient

import db
from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import training_plan

TOKEN = {"token": "t0k"}


@pytest.fixture
def client(fake_db):
    return TestClient(training_plan.create_app())


def _upload(client, plan, activate=True, **kwargs):
    """Upload a plan file, ticking "Make this the active plan" unless told not to."""
    body = plan if isinstance(plan, (bytes, str)) else json.dumps(plan)
    data = {training_plan.ACTIVATE_FIELD: "1"} if activate else None
    return client.post("/training-plan/upload", params=TOKEN, data=data,
                       files={training_plan.FILE_FIELD: ("plan.json", body, "application/json")}, **kwargs)


def _embedded(html_text, element_id):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', html_text, re.S)
    return json.loads(match.group(1))


# ── VIEWER ────────────────────────────────────────────────────────────────────

def test_no_plan_page(client):
    r = client.get("/training-plan", params=TOKEN)

    assert r.status_code == 200
    assert "No plan active" in r.text
    assert "/training-plan/upload?token=t0k" in r.text
    assert "/training-plan/plans?token=t0k" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.text.count('id="gm-nav"') == 1   # the site nav, like every page


def test_without_a_database_the_pages_explain_why(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    client = TestClient(training_plan.create_app())

    r = client.get("/training-plan", params=TOKEN)
    assert r.status_code == 503 and "DATABASE_URL" in r.text
    assert client.get("/training-plan/api/plan", params=TOKEN).status_code == 503


def test_viewer_embeds_plan_and_server_state(client, fake_db):
    _upload(client, sample_plan())
    fake_db.upsert_workout_state("test-block-2026", "w1-tue-run", completed=True)

    r = client.get("/training-plan", params=TOKEN)

    assert r.status_code == 200
    assert "<title>Test Block — Alex</title>" in r.text
    assert "__PLAN" not in r.text
    assert 'maximum-scale=1, user-scalable=no' in r.text
    plan = _embedded(r.text, "plan-data")
    server = _embedded(r.text, "plan-server")
    assert plan["meta"]["id"] == "test-block-2026"
    # weekly totals are recomputed on upload, not trusted from the file
    assert plan["weeks"][0]["summary"]["totalHours"] == 3.83
    assert server.pop("goalRace")["goal"] is None   # Settings' goal race (none set yet)
    assert server.pop("timezone")["source"] in ("default", "env", "offset")   # Settings' time zone (none picked yet)
    assert server.pop("startTimes")["w1-thu-bike"] == "06:00"   # the default start time
    assert server == {"id": "test-block-2026", "status": "active", "version": 1,
                      "readOnly": False, "completed": {"w1-tue-run": True}, "activities": {},
                      "neighbors": {}, "carryover": None}
    assert 'data-nav="plan" aria-current="page"' in r.text   # the site nav, on Plan


def test_viewer_escapes_script_closers_in_plan_text(client):
    plan = sample_plan()
    plan["weeks"][0]["days"][0]["workouts"][0]["humanReadable"] = "</script><b>x</b>"
    _upload(client, plan)

    r = client.get("/training-plan", params=TOKEN)

    assert "</script><b>x" not in r.text
    assert _embedded(r.text, "plan-data")["weeks"][0]["days"][0]["workouts"][0]["humanReadable"] == "</script><b>x</b>"


def test_archived_plan_is_served_read_only(client):
    _upload(client, sample_plan())
    _upload(client, sample_plan(id="next-block"))

    r = client.get("/training-plan", params={**TOKEN, "plan": "test-block-2026"})

    assert _embedded(r.text, "plan-server")["readOnly"] is True
    assert _embedded(r.text, "plan-data")["meta"]["id"] == "test-block-2026"
    assert _embedded(client.get("/training-plan", params=TOKEN).text, "plan-server")["id"] == "next-block"


def test_unknown_plan_id_is_404(client):
    assert client.get("/training-plan", params={**TOKEN, "plan": "nope"}).status_code == 404


# ── UPLOAD ────────────────────────────────────────────────────────────────────

def test_upload_form(client):
    r = client.get("/training-plan/upload", params=TOKEN)

    assert r.status_code == 200
    assert f'name="{training_plan.FILE_FIELD}" type="file"' in r.text
    assert 'accept=".json,application/json"' in r.text
    assert 'action="/training-plan/upload?token=t0k"' in r.text
    assert "No plan is active" in r.text


def test_upload_stores_plan_and_redirects(client, fake_db):
    r = _upload(client, sample_plan(), follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/training-plan?token=t0k"
    assert fake_db.plans["test-block-2026"]["status"] == "active"
    assert fake_db.revisions["test-block-2026"][0]["source"] == "upload"


def test_upload_with_warnings_shows_them(client):
    plan = sample_plan()
    plan["weeks"][0]["days"][0]["workouts"][0]["description"] = "d" * 130

    r = _upload(client, plan, follow_redirects=False)

    assert r.status_code == 200
    assert "Plan uploaded" in r.text and "description is 130 characters" in r.text


@pytest.mark.parametrize("body, message", [
    (b"", "empty"),
    (b"{ nope", "not valid JSON"),
    (b"\xff\xfe", "UTF-8"),
    (json.dumps({"meta": {}, "weeks": []}), "meta.id"),
])
def test_bad_uploads_return_the_form_with_an_error(client, fake_db, body, message):
    r = _upload(client, body, follow_redirects=False)

    assert r.status_code == 400
    assert message in r.text
    assert fake_db.plans == {}


def test_upload_without_a_file(client):
    r = client.post("/training-plan/upload", params=TOKEN, data={"x": "y"}, follow_redirects=False)
    assert r.status_code == 400 and "file is required" in r.text


def test_new_id_archives_the_active_plan(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, sample_plan(id="next-block"))

    assert fake_db.plans["test-block-2026"]["status"] == "archived"
    assert fake_db.plans["next-block"]["status"] == "active"


def test_same_id_asks_for_confirmation_then_replaces(client, fake_db):
    _upload(client, sample_plan())
    client.post("/training-plan/api/operations", params=TOKEN,
                json={"operations": [{"op": "set_zones", "ftp": 262}]})
    client.post("/training-plan/api/completion", params=TOKEN, json={"workout_id": "w1-tue-run"})
    client.post("/training-plan/api/completion", params=TOKEN, json={"workout_id": "w1-mon-swim"})
    replacement = sample_plan()
    replacement["weeks"][0]["days"][0]["workouts"] = []  # drops the completed swim

    r = _upload(client, replacement, follow_redirects=False)

    assert r.status_code == 200
    assert "Replace existing plan?" in r.text
    assert "<b>1</b> completed workout(s) keep their tick" in r.text
    assert "1 web/MCP edit(s) since the last upload" in r.text
    assert "Easy swim" in r.text  # the orphaned completed workout is named
    assert "Zone overrides and validation flags reset" in r.text
    assert fake_db.plans["test-block-2026"]["version"] == 2  # nothing replaced yet

    carried = re.search(r'<textarea name="plan_text" hidden>(.*?)</textarea>', r.text, re.S).group(1)
    import html as html_mod
    r = client.post("/training-plan/upload", params=TOKEN,
                    data={training_plan.TEXT_FIELD: html_mod.unescape(carried), training_plan.CONFIRM_FIELD: "1"},
                    follow_redirects=False)

    assert r.status_code == 303
    row = fake_db.plans["test-block-2026"]
    assert row["version"] == 3
    assert "overrides" not in row["plan"]
    assert fake_db.revisions["test-block-2026"][-1]["summary"] == "Replaced plan content (upload)"


def test_replace_keeps_the_unit_choice(client, fake_db):
    _upload(client, sample_plan())
    client.post("/training-plan/api/operations", params=TOKEN,
                json={"operations": [{"op": "set_unit", "unit": "imperial"}]})

    client.post("/training-plan/upload", params=TOKEN,
                data={training_plan.TEXT_FIELD: json.dumps(sample_plan()), training_plan.CONFIRM_FIELD: "1"})

    assert fake_db.plans["test-block-2026"]["plan"]["unit"] == "imperial"



# ── FUTURE PLANS ──────────────────────────────────────────────────────────────

def _next_block():
    """A second block that starts the week after the sample plan ends."""
    plan = sample_plan(id="next-block", event="Next Block",
                       planStartDate="2026-09-28", planEndDate="2026-10-11")
    for week in plan["weeks"]:
        week["startDate"] = str(date.fromisoformat(week["startDate"]) + timedelta(days=14))
        week["endDate"] = str(date.fromisoformat(week["endDate"]) + timedelta(days=14))
        for day in week["days"]:
            day["date"] = str(date.fromisoformat(day["date"]) + timedelta(days=14))
            for w in day["workouts"]:
                w["id"] = "next-" + w["id"]
    return plan


def test_upload_form_only_pre_ticks_make_active_when_no_plan_is(client):
    box = f'name="{training_plan.ACTIVATE_FIELD}" value="1"'
    assert f"{box} checked" in client.get("/training-plan/upload", params=TOKEN).text

    _upload(client, sample_plan())
    r = client.get("/training-plan/upload", params=TOKEN)

    assert box in r.text and f"{box} checked" not in r.text
    assert "Archives Test Block" in r.text


def test_upload_without_make_active_stores_a_future_plan(client, fake_db):
    _upload(client, sample_plan())

    r = _upload(client, _next_block(), activate=False, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/training-plan?plan=next-block&token=t0k"
    assert fake_db.plans["next-block"]["status"] == "future"
    assert fake_db.plans["test-block-2026"]["status"] == "active"   # untouched


def test_replacing_keeps_the_status_unless_made_active(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, _next_block(), activate=False)
    form = {training_plan.TEXT_FIELD: json.dumps(_next_block()), training_plan.CONFIRM_FIELD: "1"}

    client.post("/training-plan/upload", params=TOKEN, data=form)
    assert fake_db.plans["next-block"]["status"] == "future"
    assert fake_db.plans["test-block-2026"]["status"] == "active"

    client.post("/training-plan/upload", params=TOKEN, data={**form, training_plan.ACTIVATE_FIELD: "1"})
    assert fake_db.plans["next-block"]["status"] == "active"
    assert fake_db.plans["test-block-2026"]["status"] == "archived"


def test_confirm_replace_carries_the_make_active_choice(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, _next_block())          # archives the sample plan

    r = _upload(client, sample_plan())      # same id as the archived plan, ticked
    assert "This archived plan becomes the active plan" in r.text
    assert f'name="{training_plan.ACTIVATE_FIELD}" value="1"' in r.text

    r = _upload(client, sample_plan(), activate=False)
    assert "comes back as a future plan" in r.text
    assert f'name="{training_plan.ACTIVATE_FIELD}"' not in r.text

    client.post("/training-plan/upload", params=TOKEN,
                data={training_plan.TEXT_FIELD: json.dumps(sample_plan()), training_plan.CONFIRM_FIELD: "1"})
    assert fake_db.plans["test-block-2026"]["status"] == "future"
    assert fake_db.plans["next-block"]["status"] == "active"


def test_future_plans_are_editable_in_the_viewer(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, _next_block(), activate=False)
    params = {**TOKEN, "plan": "next-block"}

    server = _embedded(client.get("/training-plan", params=params).text, "plan-server")
    assert (server["status"], server["readOnly"]) == ("future", False)

    r = client.post("/training-plan/api/operations", params=params,
                    json={"operations": [{"op": "remove_workout", "workout_id": "next-w2-tue-run"}]})
    assert r.status_code == 200 and r.json()["status"] == "future"
    r = client.post("/training-plan/api/completion", params=params, json={"workout_id": "next-w1-mon-swim"})
    assert r.status_code == 200


def test_viewer_knows_the_plans_before_and_after(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, _next_block(), activate=False)

    active = _embedded(client.get("/training-plan", params=TOKEN).text, "plan-server")["neighbors"]
    future = _embedded(client.get("/training-plan", params={**TOKEN, "plan": "next-block"}).text,
                       "plan-server")["neighbors"]

    assert set(active) == {"next"}
    nxt = active["next"]
    assert (nxt["id"], nxt["status"], nxt["event"]) == ("next-block", "future", "Next Block")
    assert (nxt["startDate"], nxt["endDate"], nxt["firstWeek"], nxt["lastWeek"]) == ("2026-09-28", "2026-10-11", 1, 2)
    assert nxt["days"]["2026-10-06"] == [{"sport": "run", "name": "Intervals", "durationMinutes": 55}]
    assert set(future) == {"prev"} and future["prev"]["id"] == "test-block-2026"

    # archived plans stand alone
    client.post("/training-plan/plans/next-block/archive", params=TOKEN)
    assert _embedded(client.get("/training-plan", params=TOKEN).text, "plan-server")["neighbors"] == {}
    assert _embedded(client.get("/training-plan", params={**TOKEN, "plan": "next-block"}).text,
                     "plan-server")["neighbors"] == {}


def test_no_plan_page_offers_the_future_plans(client, fake_db):
    _upload(client, _next_block(), activate=False)

    r = client.get("/training-plan", params=TOKEN)

    assert "No plan active" in r.text and "Next Block" in r.text and "Starts 2026-09-28" in r.text
    assert "/training-plan/plans/next-block/activate?token=t0k" in r.text

# ── PLANS LIST ────────────────────────────────────────────────────────────────

def test_plans_list_and_actions(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, sample_plan(id="next-block", event="Next Block"))

    r = client.get("/training-plan/plans", params=TOKEN)
    assert r.status_code == 200
    assert "Next Block" in r.text and "Test Block" in r.text
    assert "/training-plan/plans/test-block-2026/activate?token=t0k" in r.text
    assert "/training-plan?plan=test-block-2026&amp;token=t0k" in r.text

    r = client.post("/training-plan/plans/test-block-2026/activate", params=TOKEN, follow_redirects=False)
    assert r.status_code == 303
    assert fake_db.plans["test-block-2026"]["status"] == "active"
    assert fake_db.plans["next-block"]["status"] == "archived"

    client.post("/training-plan/plans/next-block/delete", params=TOKEN)
    assert "next-block" not in fake_db.plans


def test_plans_list_future_actions(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, _next_block(), activate=False)

    r = client.get("/training-plan/plans", params=TOKEN)
    assert '<span class="tag future">future</span>' in r.text
    assert r.text.index("Test Block") < r.text.index("Next Block")   # active first
    assert "/training-plan/plans/next-block/archive?token=t0k" in r.text
    assert "/training-plan/plans/next-block/delete?token=t0k" in r.text
    assert "/training-plan/plans/test-block-2026/future?token=t0k" in r.text

    client.post("/training-plan/plans/next-block/archive", params=TOKEN)
    assert fake_db.plans["next-block"]["status"] == "archived"
    assert "/training-plan/plans/next-block/future?token=t0k" in client.get("/training-plan/plans", params=TOKEN).text
    client.post("/training-plan/plans/next-block/future", params=TOKEN)
    assert fake_db.plans["next-block"]["status"] == "future"
    client.post("/training-plan/plans/test-block-2026/future", params=TOKEN)
    assert fake_db.plans["test-block-2026"]["status"] == "future"
    assert fake_db.get_training_plan() is None


def test_active_plan_cannot_be_deleted(client, fake_db):
    _upload(client, sample_plan())

    r = client.post("/training-plan/plans/test-block-2026/delete", params=TOKEN, follow_redirects=False)

    assert "error=" in r.headers["location"]
    assert "test-block-2026" in fake_db.plans


def test_archive_leaves_no_active_plan(client, fake_db):
    _upload(client, sample_plan())
    client.post("/training-plan/plans/test-block-2026/archive", params=TOKEN)

    assert "No plan active" in client.get("/training-plan", params=TOKEN).text


def test_unknown_plan_action(client):
    r = client.post("/training-plan/plans/nope/activate", params=TOKEN, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert client.post("/training-plan/plans/nope/explode", params=TOKEN).status_code == 404


# ── JSON API ──────────────────────────────────────────────────────────────────

def test_api_operations_return_the_updated_plan(client, fake_db):
    _upload(client, sample_plan())

    r = client.post("/training-plan/api/operations", params=TOKEN, json={"operations": [
        {"op": "move_workout", "workout_id": "w1-tue-run", "date": "2026-09-16"},
        {"op": "add_workout", "date": "2026-09-18", "workout": {"sport": "swim", "name": "Drills",
                                                                  "durationMinutes": 30, "distanceMeters": 1500}},
    ]})

    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 2
    assert data["touched"] == ["w1-tue-run", "w1-fri-swim"]
    assert data["plan"]["weeks"][0]["summary"]["bySport"]["swim"]["sessions"] == 2
    assert fake_db.revisions["test-block-2026"][-1]["source"] == "web"


def test_api_operation_errors_are_400_and_change_nothing(client, fake_db):
    _upload(client, sample_plan())

    r = client.post("/training-plan/api/operations", params=TOKEN,
                    json={"operations": [{"op": "remove_workout", "workout_id": "nope"}]})

    assert r.status_code == 400 and "nope" in r.json()["error"]
    assert fake_db.plans["test-block-2026"]["version"] == 1
    assert client.post("/training-plan/api/operations", params=TOKEN, content=b"x").status_code == 400


def test_api_archived_plans_are_read_only(client):
    _upload(client, sample_plan())
    _upload(client, sample_plan(id="next-block"))

    r = client.post("/training-plan/api/operations", params={**TOKEN, "plan": "test-block-2026"},
                    json={"operations": [{"op": "set_unit", "unit": "imperial"}]})

    assert r.status_code == 400 and "read-only" in r.json()["error"]


def test_api_completion_round_trip(client, fake_db):
    _upload(client, sample_plan())

    r = client.post("/training-plan/api/completion", params=TOKEN, json={"workout_id": "w1-tue-run"})
    assert r.json()["completed"] == {"w1-tue-run": True}
    r = client.post("/training-plan/api/completion", params=TOKEN, json={"workout_id": "w1-tue-run", "completed": False})
    assert r.json()["completed"] == {}
    assert client.post("/training-plan/api/completion", params=TOKEN, json={"workout_id": "nope"}).status_code == 400
    assert client.post("/training-plan/api/completion", params=TOKEN, json={}).status_code == 400


def test_api_revisions_and_restore(client, fake_db):
    _upload(client, sample_plan())
    client.post("/training-plan/api/operations", params=TOKEN,
                json={"operations": [{"op": "remove_workout", "workout_id": "w1-thu-bike"}]})
    client.post("/training-plan/api/completion", params=TOKEN, json={"workout_id": "w1-tue-run"})

    revisions = client.get("/training-plan/api/revisions", params=TOKEN).json()
    assert [r["version"] for r in revisions] == [2, 1]
    assert revisions[0]["summary"] == "Removed 'Endurance ride' from Thu Sep 17"

    r = client.post("/training-plan/api/revisions/1/restore", params=TOKEN)
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 3
    assert any(w["id"] == "w1-thu-bike" for d in data["plan"]["weeks"][0]["days"] for w in d["workouts"])
    assert data["completed"] == {"w1-tue-run": True}  # ticks never roll back
    assert client.post("/training-plan/api/revisions/99/restore", params=TOKEN).status_code == 404


def test_api_get_plan(client):
    assert client.get("/training-plan/api/plan", params=TOKEN).status_code == 404
    _upload(client, sample_plan())
    assert client.get("/training-plan/api/plan", params=TOKEN).json()["id"] == "test-block-2026"


# ── EXPORT / PDF ──────────────────────────────────────────────────────────────

def test_export_downloads_the_current_plan(client):
    _upload(client, sample_plan())

    r = client.get("/training-plan/export.json", params=TOKEN)

    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="test-block-2026.json"'
    assert r.json()["meta"]["id"] == "test-block-2026"
    assert client.get("/training-plan/export.json", params={**TOKEN, "plan": "nope"}).status_code == 404


@pytest.fixture
def captured_pdf(monkeypatch):
    """Stand in for the real renderer — building a PDF needs a print engine."""
    seen = {}

    async def fake_render(plan):
        seen["plan"] = plan
        return Response(b"%PDF-fake", media_type="application/pdf")

    monkeypatch.setattr(training_plan, "render_plan_pdf", fake_render)
    return seen


def test_pdf_renders_the_stored_plan_with_zone_overrides(client, captured_pdf):
    _upload(client, sample_plan())
    client.post("/training-plan/api/operations", params=TOKEN,
                json={"operations": [{"op": "set_zones", "ftp": 262}]})

    r = client.get("/training-plan/pdf", params=TOKEN)

    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert captured_pdf["plan"]["overrides"] == {"ftp": 262}


def test_pdf_without_a_plan_is_404(client, captured_pdf):
    assert client.get("/training-plan/pdf", params=TOKEN).status_code == 404
    assert "plan" not in captured_pdf


# ── ROUTING / AUTH ────────────────────────────────────────────────────────────

def test_plan_routes_reject_wrong_methods(client):
    assert client.post("/training-plan", params=TOKEN).status_code == 405
    assert client.get("/training-plan/api/operations", params=TOKEN).status_code == 405


def test_owns_path_matches_only_plan_routes():
    assert training_plan.owns_path("/training-plan") is True
    assert training_plan.owns_path("/training-plan/api/plan") is True
    assert training_plan.owns_path("/training-plan-other") is False
    assert training_plan.owns_path("/dashboard") is False


def test_training_plan_routes_require_the_bearer_token(monkeypatch, fake_db):
    """The routes sit behind the same ?token= auth as /mcp and /dashboard."""
    import server

    monkeypatch.setattr(server, "BEARER_TOKEN", "s3cret")
    monkeypatch.setattr(db, "ensure_schema", lambda: None)
    app = TestClient(server.build_asgi_app())

    assert app.get("/training-plan").status_code == 401
    assert app.get("/training-plan/api/plan", params={"token": "wrong"}).status_code == 401

    ok = app.get("/training-plan", params={"token": "s3cret"})
    assert ok.status_code == 200
    assert "No plan active" in ok.text


def test_api_completion_on_an_archived_plan_is_rejected(client):
    _upload(client, sample_plan())
    _upload(client, sample_plan(id="next-block"))

    r = client.post("/training-plan/api/completion", params={**TOKEN, "plan": "test-block-2026"},
                    json={"workout_id": "w1-tue-run"})

    assert r.status_code == 400 and "read-only" in r.json()["error"]


def test_viewer_links_workouts_and_their_garmin_activities(client):
    _upload(client, sample_plan())
    r = client.get("/training-plan", params=TOKEN)
    # ?workout=<id> opens a workout (Today's Tomorrow card), and a done
    # workout's Garmin activity links to its detail on the dashboard.
    assert "new URLSearchParams(APP_QUERY).get('workout')" in r.text
    assert "tab: 'activity', activity: String(id), from: 'plan'" in r.text


def test_plan_list_highlights_settings_in_the_nav(client):
    r = client.get("/training-plan/plans", params=TOKEN)
    assert 'data-nav="settings" aria-current="page"' in r.text


# ── GOAL RACE (Settings) ──────────────────────────────────────────────────────

GOAL = {"name": "Fall Classic Half Marathon", "date": "2026-10-11", "distance": "half",
        "target": "1:32:00", "show_on_today": True}


def test_goal_race_starts_empty_and_lists_distances(client):
    r = client.get("/training-plan/api/goal-race", params=TOKEN)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["goal"] is None
    assert [d["key"] for d in body["distances"]] == ["5k", "10k", "half", "marathon", "sprint", "olympic", "70.3", "140.6"]
    assert {d["key"]: d["taper_days"] for d in body["distances"]}["70.3"] == 14


def test_goal_race_save_edit_and_clear(client, fake_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TZ_OFFSET_HOURS", "0")
    r = client.post("/training-plan/api/goal-race", params=TOKEN, json=GOAL)
    assert r.status_code == 200
    goal = r.json()["goal"]
    assert (goal["name"], goal["target"], goal["target_sec"], goal["distance_long"]) == \
        ("Fall Classic Half Marathon", "1:32:00", 5520, "Half marathon")
    assert goal["taper_start"] == "2026-10-01"
    assert fake_db.settings["goal_race"]["target_sec"] == 5520

    r = client.post("/training-plan/api/goal-race", params=TOKEN, json={**GOAL, "target": "", "show_on_today": False})
    assert r.json()["goal"]["target"] is None and r.json()["goal"]["show_on_today"] is False

    r = client.post("/training-plan/api/goal-race/clear", params=TOKEN, json={})
    assert r.json()["goal"] is None and "goal_race" not in fake_db.settings


def test_goal_race_invalid_input_is_400_and_keeps_the_stored_race(client, fake_db):
    client.post("/training-plan/api/goal-race", params=TOKEN, json=GOAL)
    r = client.post("/training-plan/api/goal-race", params=TOKEN, json={**GOAL, "target": "soon"})
    assert r.status_code == 400 and "h:mm:ss" in r.json()["error"]
    assert fake_db.settings["goal_race"]["name"] == GOAL["name"]
    assert client.post("/training-plan/api/goal-race", params=TOKEN, content=b"[1]").status_code == 400


def test_goal_race_without_a_database(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    c = TestClient(training_plan.create_app())
    assert c.get("/training-plan/api/goal-race", params=TOKEN).json()["goal"] is None
    r = c.post("/training-plan/api/goal-race", params=TOKEN, json=GOAL)
    assert r.status_code == 503 and "DATABASE_URL" in r.json()["error"]


def test_viewer_embeds_the_goal_race(client, fake_db):
    _upload(client, sample_plan())
    client.post("/training-plan/api/goal-race", params=TOKEN, json=GOAL)
    server = _embedded(client.get("/training-plan", params=TOKEN).text, "plan-server")
    assert server["goalRace"]["goal"]["name"] == GOAL["name"]
    assert len(server["goalRace"]["distances"]) == 8


def test_timezone_setting_round_trip(client, fake_db, monkeypatch):
    monkeypatch.delenv("LOCAL_TIMEZONE", raising=False)
    monkeypatch.delenv("DASHBOARD_TZ_OFFSET_HOURS", raising=False)
    r = client.get("/training-plan/api/timezone", params=TOKEN)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert (r.json()["timezone"], r.json()["source"], r.json()["effective"]) == (None, "default", "UTC")

    r = client.post("/training-plan/api/timezone", params=TOKEN, json={"timezone": "America/Toronto"})
    assert r.status_code == 200 and r.json()["effective"] == "America/Toronto"
    assert fake_db.settings["timezone"] == "America/Toronto"

    r = client.post("/training-plan/api/timezone", params=TOKEN, json={"timezone": "Nowhere/Land"})
    assert r.status_code == 400 and "Unknown time zone" in r.json()["error"]
    assert fake_db.settings["timezone"] == "America/Toronto"

    r = client.post("/training-plan/api/timezone", params=TOKEN, json={"timezone": None})
    assert r.json()["timezone"] is None and "timezone" not in fake_db.settings
    assert client.post("/training-plan/api/timezone", params=TOKEN, json={}).status_code == 400


def test_timezone_setting_without_a_database(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    c = TestClient(training_plan.create_app())
    assert c.get("/training-plan/api/timezone", params=TOKEN).json()["timezone"] is None
    r = c.post("/training-plan/api/timezone", params=TOKEN, json={"timezone": "America/Toronto"})
    assert r.status_code == 503 and "LOCAL_TIMEZONE" in r.json()["error"]


# ── CARRY-OVER, TIMELINE, SEASON ──────────────────────────────────────────────

def test_activating_offers_the_outgoing_plans_thresholds(client, fake_db):
    _upload(client, sample_plan())
    client.post("/training-plan/api/operations", params=TOKEN,
                json={"operations": [{"op": "set_zones", "ftp": 262},
                                     {"op": "set_zone_validation", "sport": "bike", "at": "2026-10-01"}]})
    _upload(client, _next_block(), activate=False)

    client.post("/training-plan/plans/next-block/activate", params=TOKEN)
    carry = _embedded(client.get("/training-plan", params=TOKEN).text, "plan-server")["carryover"]

    assert carry["from"] == {"id": "test-block-2026", "event": "Test Block"} and carry["auto"] is False
    assert carry["items"] == [{"key": "ftp", "label": "FTP", "sport": "bike", "from": 262, "to": 250, "suggested": True}]

    r = client.post("/training-plan/api/carryover", params=TOKEN, json={"keys": ["ftp"]})
    assert r.status_code == 200 and r.json()["carryover"] is None
    stored = fake_db.plans["next-block"]["plan"]
    assert stored["overrides"]["ftp"] == 262
    assert stored["zonesValidated"]["bike"] == {"validated": True, "at": "2026-10-01"}
    assert fake_db.revisions["next-block"][-1]["summary"].startswith("Carried thresholds from Test Block")
    assert client.post("/training-plan/api/carryover", params=TOKEN, json={"keys": ["ftp"]}).status_code == 400


def test_carry_over_can_be_dismissed_and_needs_a_difference(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, _next_block())          # same thresholds: nothing to offer
    assert _embedded(client.get("/training-plan", params=TOKEN).text, "plan-server")["carryover"] is None

    client.post("/training-plan/api/operations", params=TOKEN, json={"operations": [{"op": "set_zones", "runLthr": 175}]})
    _upload(client, sample_plan())          # re-activate the first plan (confirm page) ...
    client.post("/training-plan/upload", params=TOKEN, data={
        training_plan.TEXT_FIELD: json.dumps(sample_plan()), training_plan.CONFIRM_FIELD: "1",
        training_plan.ACTIVATE_FIELD: "1"})
    assert [i["key"] for i in _embedded(client.get("/training-plan", params=TOKEN).text, "plan-server")["carryover"]["items"]] == ["runLthr"]

    r = client.post("/training-plan/api/carryover/dismiss", params=TOKEN)
    assert r.status_code == 200 and r.json()["carryover"] is None
    assert "overrides" not in fake_db.plans["test-block-2026"]["plan"]


def test_plans_list_and_upload_flag_overlaps_and_gaps(client, fake_db):
    _upload(client, sample_plan())                                   # Sep 14 – 27
    overlapping = _next_block()
    overlapping["meta"].update(id="overlap", event="Overlap", planStartDate="2026-09-24")
    for week in overlapping["weeks"]:
        week["startDate"] = "2026-09-24" if week["weekNumber"] == 1 else week["startDate"]
        for day in week["days"]:
            for w in day["workouts"]:
                w["id"] = "o-" + w["id"]

    r = _upload(client, overlapping, activate=False, follow_redirects=False)
    assert r.status_code == 200      # the done page, to show the warning
    assert "Overlap overlaps Test Block by 4 days (2026-09-24 – 2026-09-27)" in r.text
    assert "becomes the active plan on 2026-09-24" in r.text

    client.post("/training-plan/plans/overlap/delete", params=TOKEN)
    later = _next_block()
    later["meta"]["planStartDate"] = "2026-10-05"
    later["weeks"][0]["startDate"] = "2026-10-05"
    _upload(client, later, activate=False)
    page = client.get("/training-plan/plans", params=TOKEN).text
    assert "7 days with no plan between Test Block and Next Block (2026-09-28 – 2026-10-04)" in page


def test_season_page(client, fake_db):
    assert "No plans yet" in client.get("/training-plan/season", params=TOKEN).text
    race = sample_plan()
    race["weeks"][1]["days"][0]["workouts"].append({"id": "w2-race", "sport": "race", "name": "Fall 10K", "durationMinutes": 45})
    _upload(client, race)
    client.post("/training-plan/api/completion", params=TOKEN, json={"workout_id": "w1-tue-run"})
    _upload(client, _next_block(), activate=False)

    r = client.get("/training-plan/season", params=TOKEN)

    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "<svg" in r.text and "Fall 10K" in r.text
    assert "Next Block" in r.text and "Test Block" in r.text
    assert "/training-plan?plan=next-block&amp;week=2&amp;token=t0k" in r.text   # a column opens its week
    assert "/training-plan?week=1&amp;token=t0k" in r.text                        # the active plan's
    assert "50m done" in r.text   # the ticked tempo run, in its week's tooltip


def test_season_page_switches_back_to_week_and_month_and_keeps_race_names_in_view(client):
    plan = sample_plan()
    plan["weeks"][1]["days"][0]["workouts"].append(
        {"id": "w2-race", "sport": "race", "name": "A Very Long Race Name Championship", "durationMinutes": 45})
    _upload(client, plan)

    r = client.get("/training-plan/season", params=TOKEN)

    assert '<a href="/training-plan?mode=week&amp;token=t0k">Week</a>' in r.text
    assert 'href="/training-plan?mode=month&amp;token=t0k">Month</a>' in r.text
    assert '<span aria-current="page">Season</span>' in r.text
    # the race is in the last week: its label sits to the marker's left, inside the chart
    assert re.search(r'<text class="s-race-l" x="[\d.]+" y="[\d.]+" text-anchor="end">A Very Long Race', r.text)


def test_plans_list_and_viewer_offer_a_pdf_download_with_progress(client, fake_db):
    _upload(client, sample_plan())
    _upload(client, _next_block(), activate=False)

    page = client.get("/training-plan/plans", params=TOKEN).text
    assert '<a class="btn" data-pdf href="/training-plan/pdf?plan=next-block&amp;token=t0k">Download PDF</a>' in page
    assert '<a class="btn" data-pdf href="/training-plan/pdf?plan=test-block-2026&amp;token=t0k">Download PDF</a>' in page
    assert "window.__pdfLinks" in page                     # the "Generating PDF…" behaviour
    assert "window.__pdfLinks" in client.get("/training-plan", params=TOKEN).text


def test_season_page_reads_everything_in_one_call(client, fake_db, monkeypatch):
    _upload(client, sample_plan())
    _upload(client, _next_block(), activate=False)
    calls = []
    for name in ("get_training_plan", "get_workout_states", "list_training_plans"):
        original = getattr(db, name)
        monkeypatch.setattr(db, name, lambda *a, _n=name, _o=original: calls.append(_n) or _o(*a))

    r = client.get("/training-plan/season", params=TOKEN)

    assert r.status_code == 200 and "Next Block" in r.text
    assert calls == []                                   # just db.get_season_plans
    assert '<script type="speculationrules">' in r.text  # Week / Month load on hover
