# tests/test_mcp_auth.py
"""Auth on the /mcp Streamable HTTP endpoint: `Authorization: Bearer` first,
the legacy ?token= as a fallback, 401 otherwise — and the dashboard still
takes ?token= only."""
import json

import pytest
from starlette.testclient import TestClient

import server

SECRET = "s3cret-for-tests"
PROTOCOL = "2025-06-18"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if id_ is not None:
        msg["id"] = id_
    return msg


def _initialize():
    return _rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                               "clientInfo": {"name": "test", "version": "0"}})


def _body(response):
    """The JSON-RPC message from a JSON or SSE response."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        raise AssertionError("no data line in SSE response")
    return response.json()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "BEARER_TOKEN", SECRET)
    with TestClient(server.build_asgi_app()) as c:
        yield c


# ── validate_token ────────────────────────────────────────────────────────────

def test_validate_token():
    assert server.validate_token(SECRET, SECRET) is True
    assert server.validate_token("nope", SECRET) is False
    assert server.validate_token("", SECRET) is False
    assert server.validate_token(None, SECRET) is False
    assert server.validate_token("é", SECRET) is False          # non-ASCII is a mismatch, not a crash
    assert server.validate_token("anything", "") is False       # no secret never validates


# ── gate ──────────────────────────────────────────────────────────────────────

def test_bearer_header_with_valid_token_succeeds(client):
    r = client.post("/mcp", json=_initialize(), headers={**MCP_HEADERS, "Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200
    assert _body(r)["result"]["serverInfo"]["name"] == "garmin"


def test_bearer_scheme_is_case_insensitive(client):
    r = client.post("/mcp", json=_initialize(), headers={**MCP_HEADERS, "Authorization": f"bearer {SECRET}"})
    assert r.status_code == 200


def test_query_token_still_works(client):
    r = client.post("/mcp", params={"token": SECRET}, json=_initialize(), headers=MCP_HEADERS)
    assert r.status_code == 200


def test_no_token_is_401(client):
    r = client.post("/mcp", json=_initialize(), headers=MCP_HEADERS)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_401(client):
    assert client.post("/mcp", params={"token": "wrong"}, json=_initialize(), headers=MCP_HEADERS).status_code == 401
    r = client.post("/mcp", json=_initialize(), headers={**MCP_HEADERS, "Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_wrong_bearer_is_not_rescued_by_a_valid_query_token(client):
    r = client.post("/mcp", params={"token": SECRET}, json=_initialize(),
                    headers={**MCP_HEADERS, "Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_non_bearer_scheme_falls_back_to_query_token(client):
    r = client.post("/mcp", params={"token": SECRET}, json=_initialize(),
                    headers={**MCP_HEADERS, "Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 200


def test_dashboard_ignores_the_bearer_header(client):
    """Only /mcp gained header auth; other routes are ?token= only."""
    assert client.get("/weekly-summary", headers={"Authorization": f"Bearer {SECRET}"}).status_code == 401
    assert client.get("/weekly-summary", params={"token": SECRET}).status_code == 200


def test_dashboard_401_does_not_gain_a_challenge_header(client):
    r = client.get("/dashboard")
    assert r.status_code == 401
    assert "www-authenticate" not in r.headers


# ── full MCP session over bearer ──────────────────────────────────────────────

def test_bearer_session_initialize_list_and_call_athlete_profile(client, monkeypatch):
    monkeypatch.setattr(server, "get_athlete_profile", lambda: {"weight_kg": 70})
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {SECRET}"}

    init = client.post("/mcp", json=_initialize(), headers=auth)
    assert init.status_code == 200
    session = init.headers["mcp-session-id"]          # not stripped by the auth layer

    session_headers = {**auth, "Mcp-Session-Id": session, "MCP-Protocol-Version": PROTOCOL}
    ack = client.post("/mcp", json=_rpc("notifications/initialized", id_=None), headers=session_headers)
    assert ack.status_code == 202

    listed = client.post("/mcp", json=_rpc("tools/list", id_=2), headers=session_headers)
    assert listed.status_code == 200
    assert "athlete_profile" in {t["name"] for t in _body(listed)["result"]["tools"]}

    called = client.post("/mcp", json=_rpc("tools/call", {"name": "athlete_profile", "arguments": {}}, id_=3),
                         headers=session_headers)
    assert called.status_code == 200
    result = _body(called)["result"]
    assert not result.get("isError")
    assert result["structuredContent"] == {"weight_kg": 70}


def test_every_post_in_a_session_needs_the_token(client):
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {SECRET}"}
    session = client.post("/mcp", json=_initialize(), headers=auth).headers["mcp-session-id"]
    r = client.post("/mcp", json=_rpc("tools/list", id_=2),
                    headers={**MCP_HEADERS, "Mcp-Session-Id": session, "MCP-Protocol-Version": PROTOCOL})
    assert r.status_code == 401
