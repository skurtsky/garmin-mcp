# tests/test_workout_detail.py
"""The shared workout detail view (tools/workout_detail.py): what it renders
for a planned, a completed, a skipped and a strength workout, its endpoint,
and that both the plan viewer and the dashboard load it. FakePlanDB stands in
for PostgreSQL."""
import pytest
from starlette.testclient import TestClient

from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import dashboard, plan_doc, plan_service, training_plan, workout_detail

PLAN_ID = "test-block-2026"
# sample_plan: FTP 250, run threshold pace 4:17/km, run LTHR 170, CSS 2:05.
BIKE = [{"kind": "warmup", "durationS": 900, "target": {"zone": "1-2"}},
        {"kind": "repeat", "count": 3, "steps": [
            {"kind": "work", "durationS": 480, "target": {"pctFtp": [88, 92]}, "note": "Seated"},
            {"kind": "recovery", "durationS": 240, "target": {"zone": "1"}}]},
        {"kind": "cooldown", "durationS": 600}]
STRENGTH = [{"kind": "exercise", "exercise": "GOBLET_SQUAT", "sets": 2, "reps": 10, "weightKg": 16, "warmup": True},
            {"kind": "exercise", "exercise": "BARBELL_BACK_SQUAT", "sets": 3, "reps": 8,
             "setWeightsKg": [60, 65, 70], "restS": 90, "note": "Brace"},
            {"kind": "exercise", "exercise": "PLANK", "sets": 2, "durationS": 45}]


@pytest.fixture
def plan(fake_db):
    fake_db.save_uploaded_training_plan(PLAN_ID, plan_doc.normalize_plan(sample_plan()), "Uploaded")
    plan_service.apply_operations(PLAN_ID, [
        {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {
            "steps": BIKE, "description": "Sweet spot blocks.", "keyTargets": "3 x 8 min; 85-95 rpm",
            "humanReadable": "3 x 8 min {{bike-ftp:88-92}}", "difficulty": "moderate"}},
        {"op": "update_workout", "workout_id": "w1-tue-strength", "fields": {"steps": STRENGTH}},
    ], "test", "steps")
    return fake_db


def _view(workout_id, token=None):
    return workout_detail.render(plan_service.require_plan(PLAN_ID), workout_id, token)


def test_a_planned_workout_with_steps(plan):
    v = _view("w1-thu-bike")
    html = v["html"]
    assert 'data-id="w1-thu-bike"' in html and "Thu 17 Sep · Week 1 · Base" in html
    # The graph starts on the first work step; every rep is a bar.
    assert html.count('<button type="button" class="wd-bar') == 8 and 'class="wd-bar on" data-wg-i="1"' in html
    assert "Step 2 of 8 · Work · Rep 1 of 3" in html and "8:00 · 220–230 W · 88–92% FTP" in html
    assert "FTP 250 W" in html and "0:15" in html
    # The step table keeps the repeat together.
    assert "3 &times; repeat" in html and html.count('class="wd-row') == 4
    # Coach notes split key targets; the original text is kept, resolved.
    assert "<li>3 x 8 min</li><li>85-95 rpm</li>" in html
    assert "Coach&rsquo;s original text" in html and "220–230 W" in html
    # Zones: the bike scale, with the zones this workout uses lit.
    assert "Your bike zones" in html and "this workout uses Z1, Z2, Z3, Z4" in html
    # Actions: send, skip, complete, the menu, prev/next.
    assert "data-wd-send" in html and 'data-wd-skip="on"' in html and 'data-wd-complete="on"' in html
    assert 'data-wd-act="edit"' in html and 'data-wd-act="delete"' in html
    assert v["prev"] == "w1-tue-strength" and v["next"] == "w2-tue-run"
    assert "Open in plan" in html and "/training-plan?workout=w1-thu-bike" in html


def test_a_completed_workout_compares_plan_and_activity(plan):
    plan.add_activity(77, "2026-09-15", "running", "Orléans Running", 50.2, 9.1, avg_hr=148)
    plan_service.complete_from_activity(PLAN_ID, 77)
    html = _view("w1-tue-run", token="t0k")["html"]
    assert ">Done</span>" in html and 'data-wd-complete="off"' in html and "Mark incomplete" in html
    assert "Done with" in html and "Orléans Running" in html
    assert "50:12" in html and "9.1 km" in html and "5:31 /km" in html and "148 bpm" in html
    assert "tab=activity&amp;activity=77&amp;from=plan&amp;token=t0k" in html
    assert "data-wd-send " not in html and "data-wd-skip" not in html     # done: neither applies


def test_a_skipped_workout_shows_why_and_offers_unskip(plan):
    plan_service.set_skip(PLAN_ID, "w1-mon-swim", True, "Pool closed")
    html = _view("w1-mon-swim")["html"]
    assert "Skipped" in html and "“Pool closed”" in html and "Edit reason" in html
    assert 'data-wd-skip="off"' in html and ">Unskip<" in html
    assert "data-wd-skipform" in html and 'value="Pool closed"' in html
    # Swim can't go to Garmin yet; without steps it says so via the greyed icon.
    assert "data-wd-send " not in html and "Send to Garmin — Swim workouts" in html


def test_strength_is_exercise_cards_not_a_graph(plan):
    html = _view("w1-tue-strength")["html"]
    assert 'wd-bars' not in html and ">Exercises<" in html and "3 exercises · 7 sets" in html
    assert "Barbell Back Squat" in html and "60 – 65 – 70 kg" in html and "65 kg</small>" in html
    assert ">Warm-up</div>" in html and "Bodyweight" in html and "0:45<small>hold</small>" in html


def test_without_steps_the_text_is_the_workout(plan):
    plan_service.apply_operations(PLAN_ID, [{"op": "update_workout", "workout_id": "w2-tue-run",
                                             "fields": {"humanReadable": "6 x 3 min {{run-pace:5a}}"}}], "test", "text")
    html = _view("w2-tue-run")["html"]
    assert "6 x 3 min 4:15-4:19/km" in html
    assert 'wd-bars' not in html and ">Workout</h3>" in html
    assert "Add steps to this workout" in html      # the send icon says why it's greyed


def test_an_archived_plan_is_read_only(plan):
    plan.plans[PLAN_ID]["status"] = "archived"
    v = _view("w1-thu-bike")
    assert v["readOnly"] and "data-wd-complete" not in v["html"] and "data-wd-skip" not in v["html"]
    assert 'data-wd-act="edit"' not in v["html"] and "Open in plan" in v["html"]


def test_unknown_workout(plan):
    with pytest.raises(LookupError):
        _view("nope")


def test_the_endpoint(plan):
    client = TestClient(training_plan.create_app())
    r = client.get("/training-plan/api/workout", params={"workout_id": "w1-thu-bike", "token": "t0k"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["id"] == "w1-thu-bike" and 'data-wd' in r.json()["html"]
    assert client.get("/training-plan/api/workout", params={"workout_id": "nope"}).status_code == 404
    assert client.get("/training-plan/api/workout").status_code == 400


def test_both_pages_load_the_shared_view(plan):
    viewer = TestClient(training_plan.create_app()).get("/training-plan").text
    assert "window.WD = {" in viewer and ".wd-overlay {" in viewer and "openWorkout(open.getAttribute('data-open'))" in viewer
    page = dashboard.render_dashboard_html({"date": "2026-09-17", "plan": None})
    assert "window.WD = {" in page and ".wd-overlay {" in page
    # Today's plan cards open it, the dialog in the page standing in offline.
    assert "return window.WD.open(id, {" in dashboard._ACTIVITY_MODAL_JS
    assert "fallback: function (wid) { return openPlanTemplate(wid); }" in dashboard._ACTIVITY_MODAL_JS
