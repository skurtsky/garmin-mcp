# tests/test_training_plan.py
"""Tests for the hosted training-plan viewer (tools/training_plan.py).

Fully offline: storage is redirected to a tmp directory via TRAINING_PLAN_DIR
and the routes are exercised with Starlette's TestClient, so no Garmin session
and no Azure File Share are involved.
"""
import json

import pytest
from starlette.testclient import TestClient

from tools import training_plan


PLAN_HTML = (
    "<!doctype html><html><head><title>Plan</title></head><body>"
        '<script type="application/json" id="plan-data">{"meta":{"event":"Marathon build","athlete":"Alex"},"weeks":12}</script>'
    "<div id=app></div></body></html>"
)


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    """Redirect plan storage at the tmp dir for every test in this module."""
    target = tmp_path / "training-plan"
    monkeypatch.setenv("TRAINING_PLAN_DIR", str(target))
    return target


@pytest.fixture
def client():
    return TestClient(training_plan.create_app())


def _upload_files(html_text=PLAN_HTML):
    return {
        training_plan.HTML_FIELD: ("plan.html", html_text, "text/html"),
    }


# ── STORAGE ───────────────────────────────────────────────────────────────────

def test_no_plan_by_default():
    assert training_plan.plan_exists() is False
    assert training_plan.read_plan_html() is None
    assert training_plan.plan_info() is None


def test_save_plan_extracts_embedded_json(plan_dir):
    training_plan.save_plan(PLAN_HTML.encode())

    assert training_plan.plan_exists() is True
    assert (plan_dir / "plan.html").read_text() == PLAN_HTML
    assert json.loads((plan_dir / "plan.json").read_text()) == {
        "meta": {"event": "Marathon build", "athlete": "Alex"}, "weeks": 12
    }
    # The embedded plan-data script survives the round trip untouched.
    assert training_plan.read_plan_html() == PLAN_HTML


def test_save_plan_replaces_previous_plan():
    training_plan.save_plan(PLAN_HTML.encode())
    newer = PLAN_HTML.replace('"weeks":12', '"weeks":8')
    training_plan.save_plan(newer.encode())

    assert training_plan.read_plan_html() == newer
    with open(training_plan.plan_json_path()) as f:
        assert json.load(f)["weeks"] == 8


def test_save_plan_leaves_no_temp_files(plan_dir):
    training_plan.save_plan(PLAN_HTML.encode())
    assert sorted(p.name for p in plan_dir.iterdir()) == ["plan.html", "plan.json"]


@pytest.mark.parametrize("html_bytes", [
    b"   ",                                      # empty HTML
    b"not html at all",                          # no markup
    PLAN_HTML.replace('{"meta":', "{not json").encode(),  # invalid embedded JSON
])
def test_save_plan_rejects_bad_uploads(html_bytes):
    with pytest.raises(ValueError):
        training_plan.save_plan(html_bytes)
    assert training_plan.plan_exists() is False


def test_save_plan_rejects_oversized_html(monkeypatch):
    monkeypatch.setattr(training_plan, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ValueError):
        training_plan.save_plan(PLAN_HTML.encode())


def test_plan_info_reports_title_and_size():
    training_plan.save_plan(PLAN_HTML.encode())
    info = training_plan.plan_info()

    assert info["title"] == "Marathon build"
    assert info["html_bytes"] == len(PLAN_HTML.encode())
    assert info["updated_at"].endswith("UTC")


def test_plan_info_tolerates_unparseable_json(plan_dir):
    training_plan.save_plan(PLAN_HTML.encode())
    (plan_dir / "plan.json").write_text("{ broken")

    info = training_plan.plan_info()
    assert info is not None
    assert info["title"] is None


def test_reset_plan_removes_files_and_is_idempotent():
    training_plan.save_plan(PLAN_HTML.encode())

    assert sorted(training_plan.reset_plan()) == ["plan.html", "plan.json"]
    assert training_plan.plan_exists() is False
    assert training_plan.reset_plan() == []


# ── ROUTES ────────────────────────────────────────────────────────────────────

def test_get_plan_without_upload_returns_no_plan_page(client):
    r = client.get("/training-plan", params={"token": "t0k"})

    assert r.status_code == 200
    assert "No plan active" in r.text
    assert "/training-plan/upload?token=t0k" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_get_plan_serves_stored_html_without_the_nav_bar(client):
    training_plan.save_plan(PLAN_HTML.encode())

    r = client.get("/training-plan", params={"token": "t0k"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # The plan itself is untouched, and no longer gets the shared nav bar
    # injected (issue: /training-plan navigates via the dashboard's More menu now).
    assert '<script type="application/json" id="plan-data">{"meta":{"event":"Marathon build","athlete":"Alex"},"weeks":12}</script>' in r.text
    assert "<div id=app></div>" in r.text
    assert 'id="gm-nav"' not in r.text
    plan_with_meta = PLAN_HTML.replace(
        '<head>',
        '<head><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">',
    )
    assert r.text == plan_with_meta


def test_no_plan_page_has_no_nav_bar(client):
    r = client.get("/training-plan", params={"token": "t0k"})

    assert 'id="gm-nav"' not in r.text
    assert "/training-plan/upload?token=t0k" in r.text


def test_upload_form_has_one_file_input_and_carries_token(client):
    r = client.get("/training-plan/upload", params={"token": "t0k"})

    assert r.status_code == 200
    assert f'name="{training_plan.HTML_FIELD}" type="file"' in r.text
    assert "plan_json" not in r.text
    assert 'enctype="multipart/form-data"' in r.text
    assert 'action="/training-plan/upload?token=t0k"' in r.text
    assert "No plan is active" in r.text


def test_upload_form_shows_active_plan_and_reset_button(client):
    training_plan.save_plan(PLAN_HTML.encode())

    r = client.get("/training-plan/upload", params={"token": "t0k"})

    assert "Marathon build" in r.text
    assert 'action="/training-plan/reset?token=t0k"' in r.text


def test_post_upload_stores_plan_and_redirects(client):
    r = client.post("/training-plan/upload", params={"token": "t0k"},
                    files=_upload_files(), follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/training-plan?token=t0k"
    assert training_plan.read_plan_html() == PLAN_HTML


def test_post_upload_redirect_lands_on_the_plan(client):
    r = client.post("/training-plan/upload", params={"token": "t0k"},
                    files=_upload_files())

    assert r.status_code == 200
    plan_with_meta = PLAN_HTML.replace(
        '<head>',
        '<head><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">',
    )
    assert r.text == plan_with_meta


def test_post_upload_with_invalid_embedded_json_returns_form_with_error(client):
    r = client.post("/training-plan/upload", params={"token": "t0k"},
                    files=_upload_files(html_text=PLAN_HTML.replace('{"meta":', "{ nope")), follow_redirects=False)

    assert r.status_code == 400
    assert "embedded plan JSON" in r.text
    assert training_plan.plan_exists() is False


def test_post_upload_keeps_previous_plan_when_new_one_is_invalid(client):
    training_plan.save_plan(PLAN_HTML.encode())

    r = client.post("/training-plan/upload", params={"token": "t0k"},
                    files=_upload_files(html_text=""), follow_redirects=False)

    assert r.status_code == 400
    assert training_plan.read_plan_html() == PLAN_HTML


def test_post_upload_without_files_reports_html_required(client):
    r = client.post("/training-plan/upload", params={"token": "t0k"},
                    data={"nothing": "here"}, follow_redirects=False)

    assert r.status_code == 400
    assert "HTML file is required" in r.text


def test_post_reset_deletes_the_plan(client):
    training_plan.save_plan(PLAN_HTML.encode())

    r = client.post("/training-plan/reset", params={"token": "t0k"})

    assert r.status_code == 200
    assert "plan.html" in r.text
    assert training_plan.plan_exists() is False


def test_post_reset_with_no_plan_still_confirms(client):
    r = client.post("/training-plan/reset", params={"token": "t0k"})

    assert r.status_code == 200
    assert "no plan was active" in r.text.lower()


def test_plan_routes_reject_wrong_methods(client):
    assert client.post("/training-plan", params={"token": "t0k"}).status_code == 405
    assert client.get("/training-plan/reset", params={"token": "t0k"}).status_code == 405


def test_owns_path_matches_only_plan_routes():
    assert training_plan.owns_path("/training-plan") is True
    assert training_plan.owns_path("/training-plan/upload") is True
    assert training_plan.owns_path("/training-plan-other") is False
    assert training_plan.owns_path("/dashboard") is False


# ── AUTH WIRING ───────────────────────────────────────────────────────────────

def test_training_plan_routes_require_the_bearer_token(monkeypatch):
    """The routes sit behind the same ?token= auth as /mcp and /dashboard."""
    import server

    monkeypatch.setattr(server, "BEARER_TOKEN", "s3cret")
    app = TestClient(server.build_asgi_app())

    assert app.get("/training-plan").status_code == 401
    assert app.get("/training-plan", params={"token": "wrong"}).status_code == 401

    ok = app.get("/training-plan", params={"token": "s3cret"})
    assert ok.status_code == 200
    assert "No plan active" in ok.text
