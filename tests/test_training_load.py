# tests/test_training_load.py
"""Training benefit buckets, planned-load estimates and fitness / fatigue /
form (tools/training_load.py), plus the plan read model on top of them
(tools/planned_sessions.py)."""
from datetime import date, timedelta

import pytest

from tests.plan_fakes import sample_plan
from tools import planned_sessions, training_load as tl


# ── benefits ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,key,label", [
    ("AEROBIC_BASE", "AEROBIC_BASE", "Base"),
    ("lactate_threshold", "LACTATE_THRESHOLD", "Threshold"),
    ("SPEED", "SPEED", "Sprint"),
    ("SPRINT", "SPEED", "Sprint"),
    ("ANAEROBIC_CAPACITY", "ANAEROBIC_CAPACITY", "Anaerobic"),
])
def test_benefit_buckets_follow_garmin(raw, key, label):
    assert tl.benefit_key(raw) == key
    assert tl.benefit_info(key)["label"] == label


def test_no_and_unknown_benefit():
    assert tl.benefit_key(None) is None
    assert tl.benefit_key("NO_BENEFIT") is None
    info = tl.benefit_info(tl.benefit_key("NEW_GARMIN_THING"))
    assert info == {"key": "NEW_GARMIN_THING", "label": "New Garmin Thing", "color": tl.NEUTRAL_BENEFIT_COLOR}


@pytest.mark.parametrize("zone,parsed", [
    ("Zone 2", "2"), ("Zone 1-2", "2"), ("Z3–4", "4"), ("Z5b", "5b"), ("zone 5a-5c", "5c"),
    ("Test", None), (None, None),
])
def test_parse_zone(zone, parsed):
    assert tl.parse_zone(zone) == parsed


def test_planned_benefit_prefers_coach_training_effect():
    assert tl.planned_benefit({"trainingEffect": "Threshold", "primaryZone": "Zone 2"}) == "LACTATE_THRESHOLD"
    assert tl.planned_benefit({"primaryZone": "Zone 2"}) == "AEROBIC_BASE"
    assert tl.planned_benefit({"primaryZone": "Z5c"}) == "ANAEROBIC_CAPACITY"
    assert tl.planned_benefit({"name": "Open"}) is None


# ── planned load ─────────────────────────────────────────────────────────────

def test_learned_rates_need_three_samples():
    rates = tl.learn_load_rates(
        matched=[("bike", "Zone 2", 60, 60), ("bike", "Zone 2", 90, 60), ("bike", "Zone 2", 120, 60),
                 ("run", "Zone 3", 100, 50)],
        recent=[("run", 80, 40), ("run", 60, 40), ("run", 100, 40)],
    )
    assert rates["zone"] == {("bike", "2"): 1.5}
    assert rates["sport"] == {"run": 2.0}


def test_estimate_falls_back_zone_then_sport_then_default():
    rates = {"zone": {("bike", "2"): 1.5}, "sport": {"run": 2.0}}
    assert tl.estimate_planned_load({"sport": "bike", "primaryZone": "Zone 2", "durationMinutes": 60}, rates) == 90
    assert tl.estimate_planned_load({"sport": "run", "primaryZone": "Zone 4", "durationMinutes": 30}, rates) == 60
    assert tl.estimate_planned_load({"sport": "swim", "primaryZone": "Zone 3", "durationMinutes": 40}, rates) == 60
    assert tl.estimate_planned_load({"sport": "strength", "durationMinutes": 50}, {}) == 30
    assert tl.estimate_planned_load({"sport": "bike"}, rates) is None


# ── fitness / fatigue / form ─────────────────────────────────────────────────

TODAY = date(2026, 9, 25)


def _loads(days, value):
    return {(TODAY - timedelta(days=i)).isoformat(): value for i in range(days)}


def test_constant_load_is_steady_state():
    series = tl.fitness_series(_loads(200, 100), TODAY - timedelta(days=6), TODAY, TODAY)
    assert len(series) == 7
    assert series[-1]["fitness"] == pytest.approx(100, abs=0.1)
    assert series[-1]["fatigue"] == pytest.approx(100, abs=0.1)
    assert series[-1]["form"] == 0
    assert not any(p["projected"] for p in series)


def test_ewma_step_matches_formula():
    loads = _loads(60, 100)
    loads[TODAY.isoformat()] = 200
    series = tl.fitness_series(loads, TODAY, TODAY, TODAY)
    # One day of 200 after a steady 100: fitness +100/42, fatigue +100/7.
    assert series[0]["fitness"] == pytest.approx(100 + 100 / 42, abs=0.1)
    assert series[0]["fatigue"] == pytest.approx(100 + 100 / 7, abs=0.1)
    assert series[0]["form"] == round(-100 / 7 + 100 / 42)


def test_rest_days_count_as_zero_and_projection_stops_at_last_planned_day():
    planned = {(TODAY + timedelta(days=i)).isoformat(): 0 for i in range(1, 11)}
    series = tl.fitness_series(_loads(120, 100), TODAY - timedelta(days=2), TODAY + timedelta(days=60), TODAY, planned)
    assert series[-1]["date"] == (TODAY + timedelta(days=10)).isoformat()
    assert [p["projected"] for p in series[:3]] == [False, False, False]
    assert all(p["projected"] for p in series[3:])
    # Ten rest days: fatigue drops faster than fitness, so form climbs.
    assert series[-1]["form"] > 20
    assert series[-1]["zone"] == "fresh"


def test_form_zones():
    assert tl.form_zone(6)["key"] == "fresh"
    assert tl.form_zone(5)["key"] == "neutral"
    assert tl.form_zone(-10)["key"] == "productive"
    assert tl.form_zone(-30)["key"] == "high_risk"


def test_race_day_form():
    series = [{"date": "2026-10-11", "form": 9}, {"date": "2026-10-12", "form": 20}]
    assert tl.race_day_form(series, "2026-10-11")["on_target"] is True
    assert tl.race_day_form(series, "2026-10-12")["too_fresh"] is True
    assert tl.race_day_form(series, "2026-11-01") is None


# ── planned sessions (plan read model) ───────────────────────────────────────

def test_sessions_from_plan_statuses_loads_and_matches():
    plan = sample_plan()
    states = {"w1-mon-swim": {"completed": True, "activity_id": 99}}
    sessions = planned_sessions.sessions_from_plan(
        plan, states, date(2026, 9, 14), date(2026, 9, 27), today=date(2026, 9, 17), rates={})
    by_id = {s["workout_id"]: s for s in sessions}
    assert by_id["w1-mon-swim"]["status"] == "done"
    assert by_id["w1-mon-swim"]["activity_id"] == 99
    assert by_id["w1-tue-run"]["status"] == "missed"
    assert by_id["w1-thu-bike"]["status"] == "planned"   # today
    assert by_id["w2-tue-run"]["status"] == "planned"
    assert by_id["w1-mon-swim"]["distance_km"] == 2.0
    assert by_id["w1-thu-bike"]["est_load"] == 90        # 90 min × default 1.0/min (no zone)
    assert planned_sessions.matched_workouts(sessions) == {99: by_id["w1-mon-swim"]}

    by_day = planned_sessions.planned_load_by_day(sessions, statuses=("planned",))
    assert by_day == {"2026-09-17": 90, "2026-09-22": 55}


def test_fitness_with_projection_reads_loads_and_upcoming_plan(monkeypatch):
    import db
    plan = sample_plan()
    today = date(2026, 9, 16)
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_training_plan", lambda plan_id: {"id": "p", "plan": plan})
    monkeypatch.setattr(db, "get_workout_states", lambda plan_id: {})
    monkeypatch.setattr(db, "get_activities_by_ids", lambda ids: {})
    monkeypatch.setattr(db, "get_activities_in_range", lambda a, b: [
        {"activity_type": "running", "duration_min": 50, "summary": {"training_load": 100}}] * 3)
    asked = {}

    def loads(start, end):
        asked["range"] = (start, end)
        return {(today - timedelta(days=i)).isoformat(): 80 for i in range(150)}

    monkeypatch.setattr(db, "get_daily_activity_loads", loads)
    series = planned_sessions.fitness_with_projection(today, 7)
    assert asked["range"] == ((today - timedelta(days=6 + 132)).isoformat(), today.isoformat())
    assert series[0]["date"] == "2026-09-10"
    # Projection runs to the plan's last session (Tue Sep 22), using its
    # learned-rate estimate: run 55 min × 2.0/min.
    assert series[-1]["date"] == "2026-09-22" and series[-1]["projected"]
    assert series[-1]["load"] == 110
    assert next(p for p in series if p["date"] == "2026-09-17")["load"] == 90   # bike, default rate
