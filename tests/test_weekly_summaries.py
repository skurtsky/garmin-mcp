# tests/test_weekly_summaries.py
"""Tests for the weekly training reports (tools/weekly_summaries.py).

Fully offline: storage is redirected to a tmp directory via WEEKLY_SUMMARY_DIR
and the routes are exercised with Starlette's TestClient, so no Garmin session
and no Azure File Share are involved.
"""
import json

import pytest
from starlette.testclient import TestClient

from tools import weekly_summaries as ws


# Three consecutive Mondays.
WEEK_A = "2026-08-03"
WEEK_B = "2026-08-10"
WEEK_C = "2026-08-17"

REPORT = (
    "<!doctype html><html><head><title>Week</title></head>"
    '<body class="report"><h1>Weekly summary</h1></body></html>'
)


def _report(marker: str) -> str:
    return REPORT.replace("Weekly summary", f"Weekly summary {marker}")


@pytest.fixture(autouse=True)
def summary_dir(tmp_path, monkeypatch):
    """Redirect report storage at the tmp dir for every test in this module."""
    target = tmp_path / "weekly-summaries"
    monkeypatch.setenv("WEEKLY_SUMMARY_DIR", str(target))
    return target


@pytest.fixture
def client():
    return TestClient(ws.create_app())


# ── WEEK IDENTIFIERS ──────────────────────────────────────────────────────────

def test_parse_week_start_accepts_a_monday():
    assert ws.parse_week_start(WEEK_A).isoformat() == WEEK_A


@pytest.mark.parametrize("value", [
    "2026-08-04",   # Tuesday
    "2026-08-09",   # Sunday
])
def test_parse_week_start_rejects_other_weekdays(value):
    with pytest.raises(ValueError) as excinfo:
        ws.parse_week_start(value)
    # The error names the Monday of that week so the caller can retry.
    assert "2026-08-03" in str(excinfo.value)


@pytest.mark.parametrize("value", ["03/08/2026", "2026-8-3x", "not a date", "", 20260803])
def test_parse_week_start_rejects_bad_formats(value):
    with pytest.raises(ValueError):
        ws.parse_week_start(value)


@pytest.mark.parametrize("value", [
    "2026-08-04",      # a real date, but a Tuesday
    "20260899",        # not a real date
    "202608",          # too short
    "../../etc/passwd",
    "20260803/../x",
])
def test_parse_week_id_rejects_non_monday_ids(value):
    assert ws.parse_week_id(value) is None


def test_week_label_is_human_readable():
    assert ws.week_label(ws.parse_week_start(WEEK_A)) == "Week of Aug 3, 2026"


# ── STORAGE ───────────────────────────────────────────────────────────────────

def test_no_reports_by_default():
    assert ws.list_weeks() == []
    assert ws.latest_week() is None
    assert ws.read_summary("20260803") is None


def test_save_summary_writes_the_file_verbatim(summary_dir):
    result = ws.save_summary(WEEK_A, REPORT)

    assert result == {"url": "/weekly-summary/20260803", "week": WEEK_A}
    assert (summary_dir / "20260803.html").read_text() == REPORT
    assert ws.read_summary("20260803") == REPORT


def test_save_summary_overwrites_the_same_week(summary_dir):
    ws.save_summary(WEEK_A, REPORT)
    ws.save_summary(WEEK_A, _report("v2"))

    assert ws.read_summary("20260803") == _report("v2")
    assert len(ws.list_weeks()) == 1


def test_save_summary_leaves_no_temp_files(summary_dir):
    ws.save_summary(WEEK_A, REPORT)
    assert [p.name for p in summary_dir.iterdir()] == ["20260803.html"]


def test_save_summary_rejects_a_non_monday():
    with pytest.raises(ValueError):
        ws.save_summary("2026-08-04", REPORT)
    assert ws.list_weeks() == []


@pytest.mark.parametrize("content", ["", "   ", "no markup here", None])
def test_save_summary_rejects_bad_html(content):
    with pytest.raises(ValueError):
        ws.save_summary(WEEK_A, content)
    assert ws.list_weeks() == []


def test_save_summary_rejects_oversized_html(monkeypatch):
    monkeypatch.setattr(ws, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ValueError):
        ws.save_summary(WEEK_A, REPORT)
    assert ws.list_weeks() == []


def test_list_weeks_is_newest_first_with_metadata():
    for week in (WEEK_A, WEEK_C, WEEK_B):
        ws.save_summary(week, REPORT)

    weeks = ws.list_weeks()

    assert [w["week"] for w in weeks] == [WEEK_C, WEEK_B, WEEK_A]
    assert weeks[0]["id"] == "20260817"
    assert weeks[0]["url"] == "/weekly-summary/20260817"
    assert weeks[0]["label"] == "Week of Aug 17, 2026"
    assert weeks[0]["bytes"] == len(REPORT.encode())
    assert weeks[0]["updated_at"].endswith("UTC")


def test_list_weeks_ignores_unrelated_files(summary_dir):
    ws.save_summary(WEEK_A, REPORT)
    (summary_dir / "notes.txt").write_text("scratch")
    (summary_dir / "20260804.html").write_text(REPORT)  # a Tuesday
    (summary_dir / "latest.html").write_text(REPORT)

    assert [w["id"] for w in ws.list_weeks()] == ["20260803"]


def test_latest_week_is_the_most_recent_monday():
    ws.save_summary(WEEK_A, REPORT)
    ws.save_summary(WEEK_C, REPORT)
    ws.save_summary(WEEK_B, REPORT)

    assert ws.latest_week()["week"] == WEEK_C


# ── NAVIGATION ────────────────────────────────────────────────────────────────

def test_nav_links_previous_and_next_weeks():
    for week in (WEEK_A, WEEK_B, WEEK_C):
        ws.save_summary(week, REPORT)

    nav = ws.render_nav_html("20260810", ws.list_weeks(), "t0k")

    assert 'href="/weekly-summary/20260803?token=t0k"' in nav  # previous
    assert 'href="/weekly-summary/20260817?token=t0k"' in nav  # next
    assert 'href="/dashboard?token=t0k"' in nav
    # Every week is offered in the dropdown, with the current one selected.
    assert nav.count("<option") == 3
    assert '<option value="/weekly-summary/20260810?token=t0k" selected>' in nav


def test_nav_disables_arrows_at_the_ends_of_the_range():
    ws.save_summary(WEEK_A, REPORT)
    ws.save_summary(WEEK_B, REPORT)
    weeks = ws.list_weeks()

    newest = ws.render_nav_html("20260810", weeks, "t0k")
    assert 'class="gm-week-nav__off gm-week-nav__next"' in newest
    assert 'href="/weekly-summary/20260803?token=t0k"' in newest

    oldest = ws.render_nav_html("20260803", weeks, "t0k")
    assert 'class="gm-week-nav__off gm-week-nav__prev"' in oldest
    assert 'href="/weekly-summary/20260810?token=t0k"' in oldest


def test_nav_omits_the_token_when_there_is_none():
    ws.save_summary(WEEK_A, REPORT)
    nav = ws.render_nav_html("20260803", ws.list_weeks())

    assert "token=" not in nav
    assert 'href="/dashboard"' in nav


def test_inject_nav_puts_the_header_inside_body_without_touching_the_report():
    ws.save_summary(WEEK_A, REPORT)
    page = ws.inject_nav(REPORT, "20260803", ws.list_weeks(), "t0k")

    assert page.startswith("<!doctype html><html><head>")
    assert '<body class="report">' in page          # the report's own body tag survives
    assert page.index("gm-week-nav") > page.index('<body class="report">')
    assert page.index("gm-week-nav") < page.index("<h1>Weekly summary</h1>")
    assert page.endswith("</body></html>")


def test_inject_nav_prepends_when_the_report_has_no_body_tag():
    fragment = "<h1>Fragment</h1>"
    page = ws.inject_nav(fragment, "20260803", [], "t0k")

    assert page.index("gm-week-nav") < page.index("<h1>Fragment</h1>")


# ── ROUTES ────────────────────────────────────────────────────────────────────

def test_get_latest_without_reports_returns_placeholder(client):
    r = client.get("/weekly-summary", params={"token": "t0k"})

    assert r.status_code == 200
    assert "No weekly reports yet" in r.text
    assert "upload_weekly_summary" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_get_latest_serves_the_newest_week_with_nav(client):
    ws.save_summary(WEEK_A, _report("A"))
    ws.save_summary(WEEK_C, _report("C"))

    r = client.get("/weekly-summary", params={"token": "t0k"})

    assert r.status_code == 200
    assert "Weekly summary C" in r.text
    assert 'href="/weekly-summary/20260803?token=t0k"' in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_get_specific_week_serves_that_week(client):
    ws.save_summary(WEEK_A, _report("A"))
    ws.save_summary(WEEK_C, _report("C"))

    r = client.get("/weekly-summary/20260803", params={"token": "t0k"})

    assert r.status_code == 200
    assert "Weekly summary A" in r.text
    assert "Weekly summary C" not in r.text


def test_get_missing_week_returns_404_page_listing_what_exists(client):
    ws.save_summary(WEEK_A, REPORT)

    r = client.get("/weekly-summary/20260817", params={"token": "t0k"})

    assert r.status_code == 404
    assert "Week of Aug 17, 2026" in r.text
    assert 'href="/weekly-summary/20260803?token=t0k"' in r.text


@pytest.mark.parametrize("path", [
    "/weekly-summary/20260804",     # a Tuesday
    "/weekly-summary/nonsense",
    "/weekly-summary/20260803.html",
])
def test_get_invalid_week_id_is_a_404_not_a_file_read(client, path):
    ws.save_summary(WEEK_A, REPORT)

    r = client.get(path, params={"token": "t0k"})

    assert r.status_code == 404
    assert "Weekly summary" not in r.text


def test_list_route_returns_the_weeks_as_json(client):
    ws.save_summary(WEEK_A, REPORT)
    ws.save_summary(WEEK_C, REPORT)

    r = client.get("/weekly-summary/list", params={"token": "t0k"})

    assert r.status_code == 200
    payload = r.json()
    assert [w["week"] for w in payload] == [WEEK_C, WEEK_A]
    assert payload[0]["url"] == "/weekly-summary/20260817"
    assert payload[0]["label"] == "Week of Aug 17, 2026"


def test_list_route_is_empty_json_when_nothing_is_stored(client):
    r = client.get("/weekly-summary/list", params={"token": "t0k"})

    assert r.status_code == 200
    assert r.json() == []


def test_list_route_wins_over_the_week_route(client):
    """A week can never be named "list", so the JSON route must take priority."""
    ws.save_summary(WEEK_A, REPORT)

    r = client.get("/weekly-summary/list", params={"token": "t0k"})

    assert r.headers["content-type"].startswith("application/json")
    assert json.loads(r.text)[0]["id"] == "20260803"


def test_weekly_summary_routes_reject_wrong_methods(client):
    assert client.post("/weekly-summary", params={"token": "t0k"}).status_code == 405
    assert client.post("/weekly-summary/20260803", params={"token": "t0k"}).status_code == 405


def test_owns_path_matches_only_weekly_summary_routes():
    assert ws.owns_path("/weekly-summary") is True
    assert ws.owns_path("/weekly-summary/20260803") is True
    assert ws.owns_path("/weekly-summary/list") is True
    assert ws.owns_path("/weekly-summaries") is False
    assert ws.owns_path("/dashboard") is False


# ── MCP TOOL + AUTH WIRING ────────────────────────────────────────────────────

def test_upload_weekly_summary_tool_stores_and_returns_the_url():
    import server

    result = server.upload_weekly_summary(week_start_date=WEEK_A, html_content=REPORT)

    assert result == {"url": "/weekly-summary/20260803", "week": WEEK_A}
    assert ws.read_summary("20260803") == REPORT


def test_upload_weekly_summary_tool_rejects_a_non_monday():
    import server

    with pytest.raises(ValueError):
        server.upload_weekly_summary(week_start_date="2026-08-04", html_content=REPORT)


def test_upload_weekly_summary_tool_documents_the_monday_rule():
    """The docstring is the only schema the calling LLM sees."""
    import server

    doc = server.upload_weekly_summary.__doc__
    assert "Monday" in doc
    assert "/weekly-summary" in doc


def test_weekly_summary_routes_require_the_bearer_token(monkeypatch):
    """The routes sit behind the same ?token= auth as /mcp and /dashboard."""
    import server

    monkeypatch.setattr(server, "BEARER_TOKEN", "s3cret")
    app = TestClient(server.build_asgi_app())

    assert app.get("/weekly-summary").status_code == 401
    assert app.get("/weekly-summary", params={"token": "wrong"}).status_code == 401
    assert app.get("/weekly-summary/list", params={"token": "wrong"}).status_code == 401

    ok = app.get("/weekly-summary", params={"token": "s3cret"})
    assert ok.status_code == 200
    assert "No weekly reports yet" in ok.text
