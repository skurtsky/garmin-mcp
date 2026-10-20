# tests/test_workout_steps.py
"""Structured workout steps (tools/workout_steps.py): validation, what Today
draws from them, the conversion to Garmin's workout builder, and the plan /
API / dashboard plumbing around them (amend ops, Send to Garmin, the graph).
The in-memory FakePlanDB stands in for PostgreSQL; Garmin is monkeypatched.
"""
import re
from datetime import date

import pytest
from starlette.testclient import TestClient

from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import dashboard, plan_doc, plan_service, plan_today, training_plan, workout_steps
from tools.plan_doc import PlanError
from tools.workout import build_workout_payload

PLAN_ID = "test-block-2026"
# sample_plan: FTP 250, run threshold pace 4:17/km, run LTHR 170, CSS 2:05.
PLAN = sample_plan()

BIKE = [{"kind": "warmup", "durationS": 900, "target": {"zone": "Z1-2"}},
        {"kind": "repeat", "count": 3, "steps": [
            {"kind": "work", "durationS": 480, "target": {"pctFtp": [88, 92]}, "note": "Seated"},
            {"kind": "recovery", "durationS": 240}]},
        {"kind": "work", "durationS": 840, "target": {"zone": "2"}},
        {"kind": "cooldown", "durationS": 600}]
SWIM = [{"kind": "warmup", "distanceM": 300, "stroke": "choice"},
        {"kind": "repeat", "count": 8, "steps": [{"kind": "work", "distanceM": 100, "target": {"zone": "4"}},
                                                  {"kind": "rest", "durationS": 20}]},
        {"kind": "work", "distanceM": 200, "stroke": "pull", "equipment": ["pullbuoy"], "target": {"zone": "2"}}]
STRENGTH = [{"kind": "exercise", "exercise": "BARBELL_BACK_SQUAT", "sets": 3, "reps": 8, "weightKg": 60,
             "restS": 90},
            {"kind": "rest", "durationS": 120}]


# ── validation ────────────────────────────────────────────────────────────────

def test_clean_steps_normalises_targets():
    steps = workout_steps.clean_steps("bike", BIKE)
    assert steps[0]["target"] == {"zone": "1-2"}
    assert steps[1]["steps"][0] == {"kind": "work", "durationS": 480, "target": {"pctFtp": [88, 92]}, "note": "Seated"}
    assert workout_steps.clean_steps("bike", [{"kind": "work", "target": {"pctFtp": [90, 90]}}])[0]["target"] == {"pctFtp": 90}
    assert workout_steps.clean_steps("run", []) is None and workout_steps.clean_steps("run", None) is None
    assert workout_steps.clean_steps("swim", SWIM)[2]["equipment"] == ["pullbuoy"]
    assert workout_steps.clean_steps("strength", STRENGTH)[0]["exercise"] == "BARBELL_BACK_SQUAT"


@pytest.mark.parametrize("sport, steps, message", [
    ("bike", [{"kind": "repeat", "count": 2, "steps": [{"kind": "repeat", "count": 2, "steps": []}]}], "another repeat"),
    ("run", [{"kind": "work", "durationS": 60, "distanceM": 400}], "not both"),
    ("run", [{"kind": "work", "target": {"pctFtp": 90}}], "pctFtp is for bike"),
    ("swim", [{"kind": "work", "target": {"hrZone": "2"}}], "not hrZone"),
    ("bike", [{"kind": "work", "watts": 200}], "unknown field"),
    ("bike", [{"kind": "work", "target": {"zone": "3-1"}}], "backwards"),
    ("bike", [{"kind": "work", "target": {"zone": "strides"}}], "isn't a bike zone"),
    ("bike", [{"kind": "work", "target": {"zone": "2", "rpe": 5}}], "exactly one"),
    ("run", [{"kind": "repeat", "count": 50, "steps": [{"kind": "work"}, {"kind": "rest"}]}], "keep it to"),
    ("strength", [{"kind": "exercise", "exercise": "BARBEL_SQAT"}], "Unknown exercise"),
    ("rest", [{"kind": "work"}], "not rest"),
    ("bike", [{"kind": "sprint"}], "kind must be"),
])
def test_clean_steps_rejects_with_the_step_named(sport, steps, message):
    with pytest.raises(PlanError, match=message):
        workout_steps.clean_steps(sport, steps)


# ── what Today shows ──────────────────────────────────────────────────────────

def test_resolve_bike_expands_repeats_into_bars_from_the_plan_ftp():
    r = workout_steps.resolve(PLAN, "bike", workout_steps.clean_steps("bike", BIKE))
    assert r["graph"] and len(r["bars"]) == 9 and r["total_s"] == 900 + 3 * 720 + 840 + 600
    work = r["bars"][1]
    assert work["target"] == "220–230 W · 88–92% FTP" and work["rep"] == "1 of 3" and work["note"] == "Seated"
    assert r["bars"][7]["target"] == "140–188 W · Z2"
    # The step list keeps the repeat together, pointing at its first bar.
    rep = r["groups"][1]
    assert rep["repeat"] == 3 and rep["index"] == 1 and [c["index"] for c in rep["children"]] == [1, 2]
    assert r["groups"][2]["index"] == 7
    assert r["bars"][1]["intensity"] > r["bars"][7]["intensity"] > r["bars"][2]["intensity"]


def test_resolve_swim_estimates_time_from_css():
    r = workout_steps.resolve(PLAN, "swim", workout_steps.clean_steps("swim", SWIM))
    rep = r["bars"][1]
    assert rep["target"] == "2:05/100m · Z4" and rep["amount"] == "100 m" and rep["approx"] == "~2:05"
    assert r["estimated"] and r["bars"][-1]["extras"] == "Pull · pull buoy"


def test_resolve_strength_is_a_list_not_a_graph():
    r = workout_steps.resolve(PLAN, "strength", workout_steps.clean_steps("strength", STRENGTH))
    assert r["graph"] is False
    assert r["groups"][0]["label"] == "Barbell Back Squat"
    assert r["groups"][0]["detail"] == "3 × 8 reps · 60 kg · rest 1:30"
    assert r["groups"][1]["amount"] == "2:00"


def test_resolve_without_thresholds_still_draws():
    bare = {"meta": {}, "weeks": []}
    r = workout_steps.resolve(bare, "run", [{"kind": "work", "durationS": 300, "target": {"zone": "5a"}}])
    assert r["bars"][0]["target"] == "Z5a" and r["bars"][0]["intensity"] == 1.0


# ── Garmin ────────────────────────────────────────────────────────────────────

def test_to_garmin_bike_collapses_a_simple_repeat_and_builds():
    sport, steps = workout_steps.to_garmin(PLAN, "bike", workout_steps.clean_steps("bike", BIKE))
    assert sport == "cycling"
    assert steps[1] == {"type": "repeat", "duration_s": 480, "power_watts_min": 220, "power_watts_max": 230,
                        "description": "Seated", "sets": 3, "rest_duration_s": 240}
    assert steps[0]["type"] == "warmup" and steps[0]["power_watts_min"] == 113   # zone 1 has no floor: 45%
    payload = build_workout_payload("Sweet spot", sport, steps)
    assert len(payload["workoutSegments"][0]["workoutSteps"]) == 4


def test_to_garmin_run_pace_is_slow_then_fast_and_unrolls_other_repeats():
    steps = workout_steps.clean_steps("run", [
        {"kind": "repeat", "count": 2, "steps": [
            {"kind": "work", "distanceM": 400, "target": {"zone": "3"}},
            {"kind": "recovery", "durationS": 60, "target": {"hrZone": "1"}},
            {"kind": "work", "durationS": 30, "target": {"hrZone": "4"}}]}])
    sport, out = workout_steps.to_garmin(PLAN, "run", steps)
    assert sport == "running" and len(out) == 6
    assert out[0] == {"type": "interval", "distance_m": 400, "pace_min_per_km": "4:42", "pace_max_per_km": "4:32"}
    assert out[2]["hr_min"] == 160 and out[2]["hr_max"] == 168
    build_workout_payload("Hills", sport, out)


@pytest.mark.parametrize("sport, steps, message", [
    ("swim", SWIM, "Swim workouts can't be sent"),
    ("brick", [{"kind": "work"}], "can't be sent"),
    ("run", None, "Add steps"),
    ("run", [{"kind": "work", "durationS": 60, "target": {"rpe": 7}}], "RPE target can't be sent"),
    ("bike", [{"kind": "work", "durationS": 60, "target": {"hrZone": "2"}}], "power target"),
    ("bike", [{"kind": "work", "target": {"zone": "2"}}], "duration or distance"),
])
def test_to_garmin_says_why_not(sport, steps, message):
    with pytest.raises(PlanError, match=message):
        workout_steps.to_garmin(PLAN, sport, steps)


def test_to_garmin_needs_the_threshold_it_converts_from():
    bare = {"meta": {}, "weeks": []}
    with pytest.raises(PlanError, match="FTP"):
        workout_steps.to_garmin(bare, "bike", [{"kind": "work", "durationS": 60, "target": {"zone": "3"}}])


def test_to_garmin_strength_feeds_the_strength_builder():
    sport, out = workout_steps.to_garmin(PLAN, "strength", workout_steps.clean_steps("strength", STRENGTH))
    assert sport == "strength_training"
    assert out[0] == {"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT", "reps": 8,
                      "weight_kg": 60, "rest_duration_s": 90}
    build_workout_payload("Strength A", sport, out)


# ── the plan document and its edits ───────────────────────────────────────────

def _amend(fake, *ops):
    return plan_service.apply_operations(PLAN_ID, list(ops), "test", "steps")


@pytest.fixture
def plan(fake_db):
    fake_db.save_uploaded_training_plan(PLAN_ID, plan_doc.normalize_plan(sample_plan()), "Uploaded")
    return fake_db


def _workout(fake, workout_id):
    return plan_doc.find_workout(fake.get_training_plan(PLAN_ID)["plan"], workout_id)[2]


def test_update_workout_stores_cleaned_steps_and_null_removes_them(plan):
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"steps": BIKE}})
    assert _workout(plan, "w1-thu-bike")["steps"][0]["target"] == {"zone": "1-2"}
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"steps": None}})
    assert "steps" not in _workout(plan, "w1-thu-bike")


def test_bad_steps_reject_the_whole_amend(plan):
    with pytest.raises(PlanError, match=r"steps\[1\]\.steps\[0\]\.target"):
        _amend(plan, {"op": "update_workout", "workout_id": "w1-tue-run",
                      "fields": {"steps": [{"kind": "warmup"},
                                           {"kind": "repeat", "count": 2, "steps": [
                                               {"kind": "work", "target": {"pctFtp": 90}}]}]}})


def test_a_sport_change_must_bring_fitting_steps(plan):
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"steps": BIKE}})
    with pytest.raises(PlanError, match="don't fit a run workout"):
        _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"sport": "run"}})
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"sport": "run", "steps": None}})


def test_add_workout_with_steps(plan):
    _amend(plan, {"op": "add_workout", "date": "2026-09-18",
                  "workout": {"sport": "swim", "name": "CSS", "steps": SWIM}})
    added = next(w for _, d, w in plan_doc.iter_workouts(plan.get_training_plan(PLAN_ID)["plan"]) if w["name"] == "CSS")
    assert added["steps"][1]["count"] == 8


def test_an_upload_with_bad_steps_only_warns():
    doc = sample_plan()
    doc["weeks"][0]["days"][0]["workouts"][0]["steps"] = [{"kind": "nap"}]
    errors, warnings = plan_doc.validate_plan(doc)
    assert not errors and any("w1-mon-swim: steps ignored" in w for w in warnings)


def test_get_training_plan_returns_steps(plan):
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"steps": BIKE}})
    overview = plan_service.plan_overview(PLAN_ID, week_number=1)
    bike = next(w for wk in overview["weeks"] for d in wk["days"] for w in d["workouts"] if w["id"] == "w1-thu-bike")
    assert bike["steps"][1]["kind"] == "repeat"


def test_todays_context_carries_resolved_steps_and_the_garmin_state(plan):
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"steps": BIKE}})
    ctx = plan_today.build_plan_context(date(2026, 9, 17))
    [ride] = ctx["today"]
    assert ride["steps"]["graph"] and len(ride["steps"]["bars"]) == 9
    assert ride["garmin_blocker"] is None and ride["garmin_workout_id"] is None
    swim = plan_today.build_plan_context(date(2026, 9, 14))["today"][0]
    assert swim["steps"] is None and swim["garmin_blocker"] == "Swim workouts can't be sent to Garmin yet."


# ── Send to Garmin ────────────────────────────────────────────────────────────

@pytest.fixture
def garmin(monkeypatch):
    calls = []

    def create_workout(name, sport_type, steps, schedule_date=None):
        build_workout_payload(name, sport_type, steps)     # what Garmin would be sent is valid
        calls.append({"name": name, "sport_type": sport_type, "steps": steps, "date": schedule_date})
        return {"workout_id": 9001, "scheduled_date": schedule_date, "warnings": []}

    monkeypatch.setattr("tools.workout.create_workout", create_workout)
    return calls


def test_send_to_garmin_builds_schedules_and_links(plan, garmin):
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"steps": BIKE}})
    result = plan_service.send_to_garmin(PLAN_ID, "w1-thu-bike")
    assert result == {"workout_id": "w1-thu-bike", "garmin_workout_id": 9001, "scheduled_date": "2026-09-17",
                      "warnings": []}
    assert garmin[0]["sport_type"] == "cycling" and garmin[0]["name"] == "Endurance ride"
    state = plan.states[(PLAN_ID, "w1-thu-bike")]
    assert state["garmin_workout_id"] == 9001 and str(state["garmin_scheduled_date"]) == "2026-09-17"
    with pytest.raises(PlanError, match="Already on Garmin"):
        plan_service.send_to_garmin(PLAN_ID, "w1-thu-bike")
    assert len(garmin) == 1


def test_send_to_garmin_api(plan, garmin):
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"steps": BIKE}})
    client = TestClient(training_plan.create_app())
    ok = client.post("/training-plan/api/garmin/send", json={"workout_id": "w1-thu-bike"})
    assert ok.status_code == 200 and ok.json()["garmin_workout_id"] == 9001
    swim = client.post("/training-plan/api/garmin/send", json={"workout_id": "w1-mon-swim"})
    assert swim.status_code == 400 and "Swim" in swim.json()["error"]
    assert client.post("/training-plan/api/garmin/send", json={}).status_code == 400


def test_send_to_garmin_api_reports_a_garmin_failure(plan, monkeypatch):
    _amend(plan, {"op": "update_workout", "workout_id": "w1-thu-bike", "fields": {"steps": BIKE}})

    def down(**kwargs):
        raise ConnectionError("Garmin is down")

    monkeypatch.setattr("tools.workout.create_workout", down)
    r = TestClient(training_plan.create_app()).post("/training-plan/api/garmin/send", json={"workout_id": "w1-thu-bike"})
    assert r.status_code == 502 and "Garmin is down" in r.json()["error"]
    assert not (plan.states.get((PLAN_ID, "w1-thu-bike")) or {}).get("garmin_workout_id")


# ── the desktop Today hero ────────────────────────────────────────────────────

def _hero(**w):
    base = {"id": "w1", "sport": "bike", "name": "Ride", "durationMinutes": 75, "completed": False,
            "steps": workout_steps.resolve(PLAN, "bike", workout_steps.clean_steps("bike", BIKE)),
            "garmin_blocker": None}
    return dashboard._desk_session_hero({**base, **w}, None, None, "Today")


def test_hero_draws_the_graph_with_a_detail_strip_on_the_first_work_step():
    html = _hero()
    assert html.count('<button type="button" class="wg-bar') == 9
    assert 'class="wg-bar on" data-wg-i="1"' in html
    assert "Step 2 of 9 · Work · Rep 1 of 3" in html and "8:00 · 220–230 W · 88–92% FTP" in html
    assert html.count('<button type="button" class="wg-row') == 5 and "data-wg" in html
    assert 'data-garmin-send="w1"' in html


def test_hero_send_button_states():
    swim = workout_steps.resolve(PLAN, "swim", workout_steps.clean_steps("swim", SWIM))
    greyed = _hero(sport="swim", steps=swim, garmin_blocker="Swim workouts can't be sent to Garmin yet.")
    assert 'class="td-gsend" disabled aria-label="Send to Garmin — Swim workouts' in greyed
    assert "data-garmin-send" not in greyed
    assert 'class="td-gsend sent" disabled aria-label="On Garmin' in _hero(garmin_workout_id=42)
    assert "td-gsend" not in _hero(completed=True)
    # The live button says it schedules on the workout's own day.
    assert 'aria-label="Send to Garmin — create and schedule it for Thu 17 Sep"' in _hero(date="2026-09-17")


def test_hero_lists_strength_without_a_graph_and_falls_back_to_text():
    strength = workout_steps.resolve(PLAN, "strength", workout_steps.clean_steps("strength", STRENGTH))
    html = _hero(sport="strength", steps=strength)
    assert "wg-bar" not in html and "Barbell Back Squat" in html and ">Exercises<" in html
    text = _hero(steps=None, details="WU 15 min")
    assert "wg-bar" not in text and 'class="td-steps">WU 15 min' in text


def test_workout_details_button_opens_its_dialog():
    # The dialog opener used to ignore every click inside a <button>, the
    # Workout details button included.
    assert "return inner && inner !== card ? null : card;" in dashboard._ACTIVITY_MODAL_JS
    assert "fetch('/training-plan/api/garmin/send'" in dashboard._ACTIVITY_MODAL_JS


# ── ramps (build / recover) ───────────────────────────────────────────────────

RAMPS = [{"kind": "warmup", "durationS": 120, "target": {"pctFtp": [66, 75]}, "ramp": "build"},
         {"kind": "work", "durationS": 600, "target": {"pctFtp": [88, 92]}, "ramp": "sustain"},
         {"kind": "cooldown", "durationS": 300, "target": {"pctFtp": [45, 55]}, "ramp": "recover"}]


def test_ramps_are_kept_and_sustain_is_the_default():
    steps = workout_steps.clean_steps("bike", RAMPS)
    assert steps[0]["ramp"] == "build" and steps[2]["ramp"] == "recover"
    assert "ramp" not in steps[1]          # sustain is what a range does anyway


@pytest.mark.parametrize("sport, step, message", [
    ("bike", {"kind": "work", "durationS": 60, "target": {"pctFtp": 80}, "ramp": "build"}, "steps[0].ramp needs a % FTP range"),
    ("bike", {"kind": "work", "durationS": 60, "target": {"pctFtp": [80, 80]}, "ramp": "build"}, "steps[0].ramp needs a % FTP range"),
    ("bike", {"kind": "work", "durationS": 60, "target": {"zone": "2-3"}, "ramp": "build"}, "steps[0].ramp needs a % FTP range"),
    ("bike", {"kind": "work", "durationS": 60, "ramp": "recover"}, "steps[0].ramp needs a % FTP range"),
    ("run", {"kind": "work", "durationS": 60, "target": {"zone": "2"}, "ramp": "build"}, "steps[0].ramp needs a % FTP range"),
    ("bike", {"kind": "work", "durationS": 60, "target": {"pctFtp": [60, 70]}, "ramp": "up"}, "steps[0].ramp must be build, sustain or recover"),
])
def test_a_ramp_needs_a_pct_ftp_range(sport, step, message):
    with pytest.raises(PlanError, match=re.escape(message)):
        workout_steps.clean_steps(sport, [step])


def test_resolve_draws_a_ramp_from_one_end_of_the_range_to_the_other():
    bars = workout_steps.resolve(PLAN, "bike", workout_steps.clean_steps("bike", RAMPS))["bars"]
    up, hold, down = bars
    assert up["target"] == "165 → 188 W · 66 → 75% FTP"
    assert up["ramp"] == {"shape": "build", "from": .66, "to": .75, "c1": "#5eb8ad", "c2": "#5eb8ad"}
    assert hold["ramp"] is None and hold["target"] == "220–230 W · 88–92% FTP" and hold["fill"] == hold["color"]
    assert down["target"] == "138 → 113 W · 55 → 45% FTP"
    assert down["ramp"]["from"] == .55 and down["ramp"]["to"] == .45
    # Crossing a zone fades from one zone's colour to the next.
    cross = workout_steps.resolve(PLAN, "bike", [{"kind": "warmup", "durationS": 60, "target": {"pctFtp": [50, 70]}, "ramp": "build"}])["bars"][0]
    assert (cross["ramp"]["c1"], cross["ramp"]["c2"]) == ("#6c93b0", "#5eb8ad")
    assert cross["fill"] == "linear-gradient(180deg, #5eb8ad, #6c93b0)"
    # Its height is the range's high end.
    assert cross["intensity"] == .7


def test_ramp_svg_is_one_sloped_shape():
    bars = workout_steps.resolve(PLAN, "bike", workout_steps.clean_steps("bike", RAMPS))["bars"]
    up = workout_steps.ramp_svg(bars[0], "g0")
    top, low = workout_steps.graph_height(.75), workout_steps.graph_height(.66)
    cut = round((top - low) / top * 100, 1)
    assert f'points="0,{cut} 100,0 100,100 0,100"' in up and 'fill="url(#g0)"' in up
    assert f'points="0,0 100,{round((workout_steps.graph_height(.55) - workout_steps.graph_height(.45)) / workout_steps.graph_height(.55) * 100, 1)} 100,100 0,100"' in workout_steps.ramp_svg(bars[2], "g2")
    assert workout_steps.ramp_svg(bars[1], "g1") == ""


def test_the_graph_starts_above_zero():
    assert workout_steps.graph_height(workout_steps.GRAPH_FLOOR) == 6.0           # the floor bar is a sliver
    assert workout_steps.graph_height(1.25) == 100.0
    assert workout_steps.THRESHOLD_AT == round(.7 / .95 * 100, 1)


def test_garmin_gets_the_range_whatever_the_ramp():
    shaped = workout_steps.to_garmin(PLAN, "bike", workout_steps.clean_steps("bike", RAMPS))
    flat = workout_steps.to_garmin(PLAN, "bike", workout_steps.clean_steps("bike", [{k: v for k, v in s.items() if k != "ramp"} for s in RAMPS]))
    assert shaped == flat
    assert shaped[1][0]["power_watts_min"] == 165 and shaped[1][0]["power_watts_max"] == 188
    assert "ramp" not in str(shaped)


def test_hero_draws_ramps_as_sloped_shapes():
    steps = workout_steps.resolve(PLAN, "bike", workout_steps.clean_steps("bike", RAMPS))
    html = _hero(steps=steps)
    assert html.count('class="wg-bar ramp') == 2 and '<polygon points="0,' in html
    assert "165 → 188 W · 66 → 75% FTP" in html
    assert f'class="wg-line" style="bottom:{workout_steps.THRESHOLD_AT}%"' in html
