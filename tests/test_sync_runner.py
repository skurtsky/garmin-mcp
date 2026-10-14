# tests/test_sync_runner.py
"""Settings → Garmin Sync (tools/sync_runner.py): turning the form into
sync_garmin.py flags, the JSON routes, and — against a real PostgreSQL when
TEST_DATABASE_URL is set — background runs end to end with a stand-in
script, so no Garmin session is involved:

    TEST_DATABASE_URL=postgresql://postgres@localhost/garmin_test pytest tests/test_sync_runner.py
"""
import os
import time

import pytest
from starlette.testclient import TestClient

import db
import sync_garmin
from tools import sync_runner, training_plan
from tools.sync_runner import SyncRunError, build_argv

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


# ── form → flags ──────────────────────────────────────────────────────────────

def test_full_sync_with_no_options_is_the_scheduled_command():
    assert build_argv({}) == ({"mode": "full"}, [])
    assert sync_runner.command_line([]) == "python sync_garmin.py"


def test_activity_backfill_flags():
    clean, argv = build_argv({"mode": "activities", "activities_since": "2025-01-01",
                              "activities_end_date": "2025-06-30", "days": 9})   # days isn't for this mode
    assert argv == ["--activities-only", "--activities-since", "2025-01-01", "--activities-end-date", "2025-06-30"]
    assert "days" not in clean


def test_details_backfill_flags_parse_like_the_cli():
    _, argv = build_argv({"mode": "details", "detail_limit": "999", "overwrite": True})
    assert argv == ["--details-only", "--detail-limit", "999", "--overwrite"]
    args = sync_garmin.parse_args(argv)
    assert args.details_only and args.detail_limit == 999 and args.overwrite


def test_every_mode_parses_with_the_real_cli():
    for options in ({"mode": "full", "days": 5, "since": "2026-01-01", "end_date": "2026-01-31",
                     "activities_since": "2026-01-01", "detail_limit": 20, "overwrite": True},
                    {"mode": "daily", "days": 30},
                    {"mode": "history", "history_days": 180}):
        sync_garmin.parse_args(build_argv(options)[1])


@pytest.mark.parametrize("options, message", [
    ({"mode": "nope"}, "Unknown sync mode"),
    ({"mode": "daily", "days": "abc"}, "whole number"),
    ({"mode": "daily", "days": 0}, "between"),
    ({"mode": "history", "history_days": 400}, "between"),
    ({"mode": "daily", "since": "01/02/2026"}, "date"),
    ({"mode": "daily", "since": "2026-02-01", "end_date": "2026-01-01"}, "on or before"),
    ({"mode": "activities", "activities_end_date": "2026-01-01"}, "needs --activities-since"),
    ({"mode": "details", "overwrite": "yes"}, "on or off"),
])
def test_bad_options_are_refused(options, message):
    with pytest.raises(SyncRunError, match=message):
        build_argv(options)


# ── routes ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: True)
    return TestClient(training_plan.create_app())


def _run_row(**over):
    from datetime import datetime, timezone
    row = {"id": 3, "options": {"mode": "daily"}, "argv": ["--daily-only"], "status": "running",
           "exit_code": None, "cancel_requested": False,
           "started_at": datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc), "finished_at": None}
    return {**row, **over}


def test_start_route_returns_the_new_run(client, monkeypatch):
    monkeypatch.setattr(sync_runner, "start", lambda body: _run_row(options=body))
    r = client.post("/training-plan/api/sync", json={"mode": "daily"})
    assert r.status_code == 202
    assert r.json()["command"] == "python sync_garmin.py --daily-only"
    assert r.json()["status"] == "running"


def test_start_route_refuses_a_second_run(client, monkeypatch):
    monkeypatch.setattr(db, "create_sync_run", lambda *a: None)
    r = client.post("/training-plan/api/sync", json={"mode": "daily"})
    assert r.status_code == 409 and "already running" in r.json()["error"]


def test_start_route_refuses_bad_options(client):
    r = client.post("/training-plan/api/sync", json={"mode": "daily", "days": -1})
    assert r.status_code == 400


def test_overview_route(client, monkeypatch):
    from datetime import datetime, timezone
    t = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(db, "get_sync_state", lambda: {
        "gear": {"last_sync_time": None, "last_synced_date": None, "status": "ok", "error_message": None},
        "activities": {"last_sync_time": t, "last_synced_date": t.date(), "status": "ok", "error_message": None},
    })
    monkeypatch.setattr(db, "list_sync_runs", lambda limit, stale: [_run_row()])
    monkeypatch.setattr(db, "get_sync_run", lambda run_id: {**_run_row(), "log": "hello\n"})
    data = client.get("/training-plan/api/sync").json()
    assert data["lastSync"] == t.isoformat()
    assert [d["key"] for d in data["dataTypes"]] == ["activities", "gear"]
    assert data["latest"]["log"] == "hello\n"
    assert data["runs"][0]["id"] == 3


def test_run_and_cancel_routes(client, monkeypatch):
    monkeypatch.setattr(db, "get_sync_run", lambda run_id: None)
    assert client.get("/training-plan/api/sync/runs/9").status_code == 404
    monkeypatch.setattr(db, "request_sync_run_cancel", lambda run_id: False)
    assert client.post("/training-plan/api/sync/runs/9/cancel").status_code == 409


def test_routes_need_the_database(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    c = TestClient(training_plan.create_app())
    assert c.get("/training-plan/api/sync").status_code == 503
    assert c.post("/training-plan/api/sync", json={}).status_code == 503


# ── background runs against PostgreSQL ────────────────────────────────────────

@pytest.fixture
def database(monkeypatch, tmp_path):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(db, "_pool", None)
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS sync_runs")
    db.ensure_schema()
    monkeypatch.setattr(sync_runner, "TICK_S", 0.1)
    yield tmp_path
    db._pool.close()


def _script(tmp_path, monkeypatch, body):
    path = tmp_path / "sync_garmin.py"
    path.write_text("import sys, time\n" + body)
    monkeypatch.setattr(sync_runner, "SCRIPT", str(path))


def _wait(run_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = db.get_sync_run(run_id)
        if run["status"] != "running":
            return run
        time.sleep(0.1)
    raise AssertionError("sync run never finished")


def test_a_run_streams_its_output_and_records_success(database, monkeypatch):
    _script(database, monkeypatch, "print('args', sys.argv[1:])\nprint('done')\n")
    run = sync_runner.start({"mode": "daily", "days": 2})
    done = _wait(run["id"])
    assert done["status"] == "succeeded" and done["exit_code"] == 0
    assert "args ['--daily-only', '--days', '2']" in done["log"] and "done" in done["log"]
    assert sync_runner.overview()["latest"]["id"] == run["id"]


def test_a_failing_run_is_failed(database, monkeypatch):
    _script(database, monkeypatch, "print('boom')\nsys.exit(1)\n")
    done = _wait(sync_runner.start({})["id"])
    assert done["status"] == "failed" and done["exit_code"] == 1


def test_only_one_run_at_a_time_and_cancel_stops_it(database, monkeypatch):
    _script(database, monkeypatch, "print('working', flush=True)\ntime.sleep(60)\n")
    run = sync_runner.start({})
    with pytest.raises(SyncRunError, match="already running"):
        sync_runner.start({})
    sync_runner.cancel(run["id"])
    done = _wait(run["id"])
    assert done["status"] == "cancelled"
    assert "working" in done["log"] and "Cancel requested" in done["log"]
    with pytest.raises(SyncRunError, match="isn't running"):
        sync_runner.cancel(run["id"])


def test_a_run_whose_server_went_away_reads_as_interrupted(database):
    run = db.create_sync_run({"mode": "full"}, [], 120)
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE sync_runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (run["id"],))
    assert db.list_sync_runs(5, 120)[0]["status"] == "interrupted"
    assert db.create_sync_run({"mode": "full"}, [], 120) is not None   # no longer blocks a new run
    assert "stopped before it finished" in db.get_sync_run(run["id"])["log"]
