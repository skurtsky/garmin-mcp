# tests/test_training_plan.py
"""Tests for the /training-plan routes (tools/training_plan.py).

Fully offline: the plan functions in db.py are replaced by an in-memory fake
(tests/plan_fakes.py) and the routes are exercised with Starlette's
TestClient, so no PostgreSQL and no Garmin session are involved.
"""
import json
import re

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


def _upload(client, plan, **kwargs):
    body = plan if isinstance(plan, (bytes, str)) else json.dumps(plan)
    return client.post("/training-plan/upload", params=TOKEN,
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
    assert 'id="gm-nav"' not in r.text


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
    assert server == {"id": "test-block-2026", "status": "active", "version": 1,
                      "readOnly": False, "completed": {"w1-tue-run": True}}


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
