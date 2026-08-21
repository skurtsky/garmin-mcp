# tests/test_server.py
"""
Tests that the MCP tool surface itself (not just the underlying tools/workout.py
helpers) restricts workout creation to running and cycling — this is the schema
the calling LLM actually sees, so it must be correct and self-contained.
"""
import asyncio

import pytest
from starlette.testclient import TestClient

import server
from tools import dashboard


def _get_tool_schema(tool_name: str) -> dict:
    async def _fetch():
        tool = await server.mcp.get_tool(tool_name)
        return tool.parameters

    return asyncio.run(_fetch())


def test_create_workout_sport_type_supports_running_cycling_and_strength():
    schema = _get_tool_schema("create_workout")
    sport_type_schema = schema["properties"]["sport_type"]
    assert sport_type_schema["enum"] == ["running", "cycling", "strength_training"]


def test_create_workout_docstring_documents_repeat_and_targets():
    """
    The MCP-facing docstring is the only schema the calling LLM sees — it must
    spell out the repeat-step and required-target rules inline, not just point
    at tools/workout.py (which the LLM cannot read).
    """
    doc = server.create_workout.__doc__
    assert "repeat" in doc
    assert "target" in doc.lower()
    assert "pace" in doc.lower()
    assert "power" in doc.lower()


# ── ROUTE: /dashboard/activity/<id> (issue 74) ──────────────────────────────

def _fake_detail_data(activity_id: int) -> dict:
    return {
        "activity_id": activity_id,
        "detail": {"summary": {"id": activity_id, "name": "Evening Run", "type": "running",
                               "date": "2026-08-21 06:00:00", "distance_km": 10.0,
                               "duration_min": 50.0}},
        "detail_err": None, "route": None, "route_err": None,
    }


def test_activity_detail_route_renders_the_page(monkeypatch):
    monkeypatch.setattr(dashboard, "get_activity_detail_data", _fake_detail_data)
    app = TestClient(server.build_asgi_app())

    resp = app.get("/dashboard/activity/123")

    assert resp.status_code == 200
    assert "Evening Run" in resp.text


def test_activity_detail_route_requires_the_bearer_token(monkeypatch):
    monkeypatch.setattr(dashboard, "get_activity_detail_data", _fake_detail_data)
    monkeypatch.setattr(server, "BEARER_TOKEN", "s3cret")
    app = TestClient(server.build_asgi_app())

    assert app.get("/dashboard/activity/123").status_code == 401
    assert app.get("/dashboard/activity/123", params={"token": "wrong"}).status_code == 401

    ok = app.get("/dashboard/activity/123", params={"token": "s3cret"})
    assert ok.status_code == 200
    assert "Evening Run" in ok.text


def test_activity_detail_route_rejects_a_non_numeric_id(monkeypatch):
    monkeypatch.setattr(dashboard, "get_activity_detail_data", _fake_detail_data)
    app = TestClient(server.build_asgi_app())

    resp = app.get("/dashboard/activity/not-a-number")
    assert resp.status_code == 400


def test_activity_detail_route_surfaces_render_errors_as_500(monkeypatch):
    def boom(activity_id):
        raise RuntimeError("garmin down")

    monkeypatch.setattr(dashboard, "get_activity_detail_data", boom)
    app = TestClient(server.build_asgi_app())

    resp = app.get("/dashboard/activity/123")
    assert resp.status_code == 500
    assert "garmin down" in resp.text
