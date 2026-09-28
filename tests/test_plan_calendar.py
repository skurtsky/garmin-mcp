# tests/test_plan_calendar.py
"""Tests for the calendar feed (tools/plan_calendar.py) and its routes.

Offline: the db.py plan and calendar functions are the in-memory fake from
tests/plan_fakes.py.
"""
from datetime import date, datetime, timezone

import pytest
from starlette.testclient import TestClient

import db
import server
from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import plan_calendar, plan_doc, plan_service, training_plan
from tools.plan_doc import PlanError

TOKEN = {"token": "t0k"}
NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


@pytest.fixture
def client(fake_db):
    return TestClient(training_plan.create_app())


def _events(ics: str) -> list[dict]:
    """Each VEVENT as {property: value}, with folded lines rejoined."""
    unfolded = ics.replace("\r\n ", "")
    events, current = [], None
    for line in unfolded.split("\r\n"):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key] = value
    return events


# ── SCHEDULE VALIDATION ───────────────────────────────────────────────────────

def test_normalize_schedule_tidies_and_sorts():
    default, slots = plan_calendar.normalize_schedule("6:30", [
        {"day": "sunday", "sport": "Bike", "time": "7:00"},
        {"day": "Thursday", "sport": "strength", "time": "05:30"},
    ])
    assert default == "06:30"
    assert slots == [
        {"day": "Thursday", "sport": "strength", "time": "05:30"},
        {"day": "Sunday", "sport": "bike", "time": "07:00"},
    ]


@pytest.mark.parametrize("slots, message", [
    ([{"day": "Funday", "sport": "run", "time": "06:00"}], "Unknown day"),
    ([{"day": "Monday", "sport": "rest", "time": "06:00"}], "sport must be"),
    ([{"day": "Monday", "sport": "run", "time": "25:00"}], "HH:MM"),
    ([{"day": "Monday", "sport": "run", "time": "06:00"},
      {"day": "monday", "sport": "run", "time": "07:00"}], "listed twice"),
    ("nope", "must be a list"),
])
def test_normalize_schedule_rejects_bad_rows(slots, message):
    with pytest.raises(PlanError, match=message):
        plan_calendar.normalize_schedule("06:00", slots)


def test_parse_clock():
    assert plan_doc.parse_clock("7:05") == "07:05"
    for bad in ("7", "24:00", "07:60", "7pm", None, 700):
        with pytest.raises(PlanError):
            plan_doc.parse_clock(bad)


# ── START TIMES ───────────────────────────────────────────────────────────────

TUESDAY = date(2026, 9, 15)


def _clock(entries):
    return [(w["id"], s and s.strftime("%H:%M"), e and e.strftime("%H:%M")) for w, s, e in entries]


def test_schedule_uses_slot_then_default():
    workouts = [{"id": "run", "sport": "run", "durationMinutes": 50},
                {"id": "swim", "sport": "swim", "durationMinutes": 45}]
    slots = [{"day": "Tuesday", "sport": "swim", "time": "12:00"}]

    got = plan_calendar.schedule_day(TUESDAY, workouts, "06:00", slots)

    assert _clock(got) == [("run", "06:00", "06:50"), ("swim", "12:00", "12:45")]


def test_schedule_orders_by_time_not_plan_order():
    # Run listed first but its slot is in the evening; strength is in the morning.
    workouts = [{"id": "run", "sport": "run", "durationMinutes": 50},
                {"id": "gym", "sport": "strength", "durationMinutes": 45}]
    slots = [{"day": "Tuesday", "sport": "run", "time": "17:30"},
             {"day": "Tuesday", "sport": "strength", "time": "05:30"}]

    got = plan_calendar.schedule_day(TUESDAY, workouts, "06:00", slots)

    assert _clock(got) == [("gym", "05:30", "06:15"), ("run", "17:30", "18:20")]


def test_overlapping_workouts_stack():
    workouts = [{"id": "bike", "sport": "bike", "durationMinutes": 90},
                {"id": "run", "sport": "run", "durationMinutes": 20}]

    got = plan_calendar.schedule_day(TUESDAY, workouts, "07:00", [])

    assert _clock(got) == [("bike", "07:00", "08:30"), ("run", "08:30", "08:50")]


def test_workout_start_time_wins_and_is_never_moved():
    workouts = [{"id": "long", "sport": "bike", "durationMinutes": 180},
                {"id": "race", "sport": "race", "durationMinutes": 60, "startTime": "08:00"}]

    got = plan_calendar.schedule_day(TUESDAY, workouts, "07:00", [])

    assert _clock(got) == [("long", "07:00", "10:00"), ("race", "08:00", "09:00")]


def test_no_duration_is_all_day_and_rest_is_skipped():
    workouts = [{"id": "rest", "sport": "rest", "name": "Rest"},
                {"id": "mob", "sport": "other", "name": "Mobility"},
                {"id": "bad", "sport": "run", "durationMinutes": 30, "startTime": "nonsense"}]

    got = plan_calendar.schedule_day(TUESDAY, workouts, "06:00", [])

    assert _clock(got) == [("mob", None, None), ("bad", "06:00", "06:30")]


# ── ICS ───────────────────────────────────────────────────────────────────────

def _row(plan=None, **extra):
    return {"id": "test-block-2026", "version": 3, "plan": plan or sample_plan(),
            "updated_at": datetime(2026, 9, 20, 8, tzinfo=timezone.utc), **extra}


def test_build_ics_events():
    plan = sample_plan()
    plan["weeks"][0]["days"][1]["workouts"][0].update(
        description="Tempo, steady; controlled", humanReadable="Main: 3 x 10min @ {{run-pace:4}}")

    ics = plan_calendar.build_ics(_row(plan), {"w1-mon-swim"}, "06:00",
                                  [{"day": "Tuesday", "sport": "strength", "time": "05:30"}], NOW)

    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    events = {e["UID"]: e for e in _events(ics)}
    assert set(events) == {f"{wid}@test-block-2026.garmin-mcp" for wid in
                           ("w1-mon-swim", "w1-tue-run", "w1-tue-strength", "w1-thu-bike", "w2-tue-run")}

    swim = events["w1-mon-swim@test-block-2026.garmin-mcp"]
    assert swim["SUMMARY"] == "✓ Easy swim"                  # completed
    assert (swim["DTSTART"], swim["DTEND"]) == ("20260914T060000", "20260914T064500")  # floating
    assert swim["SEQUENCE"] == "3"
    assert swim["LAST-MODIFIED"] == "20260920T080000Z"

    gym = events["w1-tue-strength@test-block-2026.garmin-mcp"]
    run = events["w1-tue-run@test-block-2026.garmin-mcp"]
    assert gym["DTSTART"] == "20260915T053000"
    assert run["DTSTART"] == "20260915T061500"                # 06:00 overlaps the gym: stacks after it
    assert "Tempo\\, steady\\; controlled" in run["DESCRIPTION"]
    assert "Zone 4 pace" in run["DESCRIPTION"] and "{{" not in run["DESCRIPTION"]
    assert run["SUMMARY"] == "Tempo run"


def test_build_ics_without_a_plan_is_an_empty_calendar():
    ics = plan_calendar.build_ics(None, set(), "06:00", [], NOW)

    assert "BEGIN:VEVENT" not in ics
    assert "X-WR-CALNAME:Training plan" in ics


def test_fold_never_splits_a_character():
    line = "SUMMARY:" + "é" * 80
    folded = plan_calendar._fold(line)

    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == line


# ── ROUTES ────────────────────────────────────────────────────────────────────

def test_settings_default_before_anything_is_saved(client):
    r = client.get("/training-plan/api/calendar", params=TOKEN)

    assert r.status_code == 200
    assert r.json() == {"defaultTime": "06:00", "slots": [], "published": False,
                        "feedPath": None, "publishedAt": None}


def test_save_schedule(client, fake_db):
    r = client.post("/training-plan/api/calendar/schedule", params=TOKEN, json={
        "defaultTime": "05:45", "slots": [{"day": "Sunday", "sport": "bike", "time": "07:00"}]})

    assert r.status_code == 200
    assert r.json()["slots"] == [{"day": "Sunday", "sport": "bike", "time": "07:00"}]
    assert fake_db.calendar["default_time"] == "05:45"

    bad = client.post("/training-plan/api/calendar/schedule", params=TOKEN,
                      json={"slots": [{"day": "Sunday", "sport": "bike", "time": "7am"}]})
    assert bad.status_code == 400 and "HH:MM" in bad.json()["error"]
    assert fake_db.calendar["default_time"] == "05:45"          # unchanged


def test_publish_feed_and_revoke(client, fake_db):
    plan_service.save_upload(sample_plan())
    assert client.get(plan_calendar.FEED_PATH, params={"key": "anything"}).status_code == 401

    published = client.post("/training-plan/api/calendar/publish", params=TOKEN).json()
    assert published["published"] and published["publishedAt"]
    key = fake_db.calendar["feed_token"]
    assert published["feedPath"] == f"{plan_calendar.FEED_PATH}?key={key}"

    r = client.get(plan_calendar.FEED_PATH, params={"key": key})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert len(_events(r.text)) == 5
    assert client.get(plan_calendar.FEED_PATH, params={"key": key + "x"}).status_code == 401
    assert client.get(plan_calendar.FEED_PATH).status_code == 401
    assert client.get(plan_calendar.FEED_PATH, params={"key": "é"}).status_code == 401

    # A new link cuts off the old one.
    client.post("/training-plan/api/calendar/publish", params=TOKEN)
    assert client.get(plan_calendar.FEED_PATH, params={"key": key}).status_code == 401
    key = fake_db.calendar["feed_token"]

    revoked = client.post("/training-plan/api/calendar/revoke", params=TOKEN).json()
    assert revoked["published"] is False and revoked["feedPath"] is None
    assert client.get(plan_calendar.FEED_PATH, params={"key": key}).status_code == 401


def test_feed_follows_the_active_plan(client, fake_db):
    key = client.post("/training-plan/api/calendar/publish", params=TOKEN).json()["feedPath"].split("key=")[1]
    assert "BEGIN:VEVENT" not in client.get(plan_calendar.FEED_PATH, params={"key": key}).text

    plan_service.save_upload(sample_plan())
    other = sample_plan(id="next-block", event="Next Block")
    plan_service.save_upload(other)                            # becomes the active plan

    text = client.get(plan_calendar.FEED_PATH, params={"key": key}).text
    assert "@next-block.garmin-mcp" in text and "@test-block-2026" not in text
    assert "X-WR-CALNAME:Next Block — Alex" in text


def test_workout_start_time_edit(client, fake_db):
    plan_service.save_upload(sample_plan())
    ops = {"operations": [{"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"startTime": "7:15"}}]}

    r = client.post("/training-plan/api/operations", params=TOKEN, json=ops)
    assert r.status_code == 200
    bike = r.json()["plan"]["weeks"][0]["days"][2]["workouts"][0]
    assert bike["startTime"] == "07:15"

    bad = {"operations": [{"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"startTime": "soon"}}]}
    assert client.post("/training-plan/api/operations", params=TOKEN, json=bad).status_code == 400

    cleared = {"operations": [{"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"startTime": None}}]}
    r = client.post("/training-plan/api/operations", params=TOKEN, json=cleared)
    assert "startTime" not in r.json()["plan"]["weeks"][0]["days"][2]["workouts"][0]


def test_upload_warns_about_a_bad_start_time():
    plan = sample_plan()
    plan["weeks"][0]["days"][0]["workouts"][0]["startTime"] = "6am"

    errors, warnings = plan_doc.validate_plan(plan)

    assert not errors
    assert any("startTime" in w for w in warnings)


def test_settings_card_is_in_the_viewer(client):
    plan_service.save_upload(sample_plan())
    page = client.get("/training-plan", params=TOKEN).text

    assert "Publish calendar to URL" in page
    assert "data-cal-default" in page


# ── SERVER AUTH ───────────────────────────────────────────────────────────────

def test_feed_skips_the_bearer_token_but_nothing_else_does(fake_db, monkeypatch):
    monkeypatch.setattr(server, "BEARER_TOKEN", "bearer-secret")
    monkeypatch.setattr(db, "ensure_schema", lambda: None)
    app = TestClient(server.build_asgi_app())
    plan_service.save_upload(sample_plan())
    key = plan_calendar.publish()["feedPath"].split("key=")[1]

    assert app.get(plan_calendar.FEED_PATH, params={"key": key}).status_code == 200
    assert app.get(plan_calendar.FEED_PATH, params={"token": "bearer-secret"}).status_code == 401
    # The feed key opens nothing but the feed.
    assert app.get("/training-plan/api/calendar", params={"token": key}).status_code == 401
    assert app.get("/training-plan", params={"key": key}).status_code == 401
    assert app.get("/training-plan/api/calendar", params={"token": "bearer-secret"}).status_code == 200


# ── START TIMES IN THE VIEWER ─────────────────────────────────────────────────

def test_start_times_match_the_feed(fake_db):
    plan = sample_plan()
    plan["weeks"][0]["days"][2]["workouts"][0]["startTime"] = "08:15"          # Thu bike
    plan["weeks"][0]["days"][0]["workouts"].append({"id": "w1-mon-mob", "sport": "other", "name": "Mobility"})
    plan_calendar.save_schedule("06:00", [{"day": "Tuesday", "sport": "run", "time": "17:30"},
                                          {"day": "Tuesday", "sport": "strength", "time": "05:30"}])

    times = plan_calendar.start_times(plan)

    assert times == {"w1-mon-swim": "06:00", "w1-tue-run": "17:30", "w1-tue-strength": "05:30",
                     "w1-thu-bike": "08:15", "w2-tue-run": "17:30"}      # Mobility: no duration, no time


def test_viewer_payload_carries_start_times(client, fake_db):
    plan_service.save_upload(sample_plan())
    client.post("/training-plan/api/calendar/schedule", params=TOKEN, json={
        "defaultTime": "06:00", "slots": [{"day": "Tuesday", "sport": "run", "time": "18:00"}]})

    times = client.get("/training-plan/api/plan", params=TOKEN).json()["startTimes"]
    assert times["w1-tue-run"] == "18:00" and times["w1-tue-strength"] == "06:00"

    # A workout's own start time wins, and edits answer with the new times.
    ops = {"operations": [{"op": "update_workout", "workout_id": "w1-tue-strength", "fields": {"startTime": "19:00"}}]}
    times = client.post("/training-plan/api/operations", params=TOKEN, json=ops).json()["startTimes"]
    assert times["w1-tue-strength"] == "19:00"

    page = client.get("/training-plan", params=TOKEN).text
    assert '"startTimes"' in page and "byStartTime(" in page


def test_activity_week_sessions_carry_start_times(fake_db):
    from tools import dashboard_activity
    plan_service.save_upload(sample_plan())
    plan_calendar.save_schedule("06:00", [{"day": "Tuesday", "sport": "run", "time": "17:30"},
                                          {"day": "Tuesday", "sport": "strength", "time": "05:30"}])

    extras = dashboard_activity.week_extras("2026-09-14", "2026-09-20", [], date(2026, 9, 14))

    times = {s["workout_id"]: s["start_time"] for s in extras["planned"]}
    assert times["w1-tue-strength"] == "05:30" and times["w1-tue-run"] == "17:30"
