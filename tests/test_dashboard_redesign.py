# tests/test_dashboard_redesign.py
"""The redesigned dashboard sections: race predictions, the goal race and
its Today countdown card (tools/race_predictor.py, tools/dashboard_data.py,
tools/dashboard.py). Offline — db reads are monkeypatched."""
from datetime import date

import pytest

import db
from tools import dashboard, dashboard_data, goal_race, race_predictor

TODAY = date(2026, 9, 25)


# ── race predictor ───────────────────────────────────────────────────────────

def test_tri_estimate_matches_the_design_formulas():
    # The prototype's inputs: CSS 2:01, FTP 248 W, threshold pace 4:15/km.
    tri = race_predictor.tri_predictions(121, 248, 255)
    sprint = next(r for r in tri["rows"] if r["key"] == "sprint")
    swim, bike, run, tt = (l["seconds"] for l in sprint["legs"])
    assert swim == pytest.approx(7.5 * 124)
    assert bike == pytest.approx(20 / (33 * (248 * 0.92 / 200) ** (1 / 3)) * 3600)
    assert run == pytest.approx(5 * 255 * 1.03)
    assert tt == 150
    assert sprint["seconds"] == pytest.approx(swim + bike + run + tt)
    assert [l["target"] for l in sprint["legs"]] == ["2:04/100m", "228 W", "4:23/km", None]
    assert tri["swim_source"] == "css" and tri["missing"] == []


def test_tri_swim_from_records_when_no_css():
    records = {"swimming": [{"label": "Fastest 400m Swim", "value_raw": 451}]}
    rec = race_predictor.swim_from_records(records)
    tri = race_predictor.tri_predictions(None, 248, 255, rec)
    oly = next(r for r in tri["rows"] if r["key"] == "olympic")
    assert oly["legs"][0]["seconds"] == pytest.approx(451 * (1500 / 400) ** 1.06)
    assert tri["swim_source"] == "record"


def test_tri_missing_inputs_leave_the_total_out():
    tri = race_predictor.tri_predictions(None, None, 255)
    assert all(r["seconds"] is None for r in tri["rows"])
    assert tri["missing"] == ["CSS or a swim record", "FTP"]


def test_tri_config_override(monkeypatch):
    monkeypatch.setenv("TRI_PREDICTOR_CONFIG", '{"sprint": {"bike_if": 0.8, "bogus": 1}, "nope": {}}')
    cfg = race_predictor.tri_config()
    assert cfg["sprint"]["bike_if"] == 0.8 and "bogus" not in cfg["sprint"] and "nope" not in cfg
    monkeypatch.setenv("TRI_PREDICTOR_CONFIG", "not json")
    assert race_predictor.tri_config()["sprint"]["bike_if"] == 0.92


def test_run_predictions_records_and_trend():
    records = {"running": [{"label": "Fastest 10K", "value_raw": 2686}]}
    rows = race_predictor.run_predictions({"10K": 2530, "5K": 1205}, {"10K": 2595}, records)
    ten = next(r for r in rows if r["key"] == "10K")
    assert ten["faster_than_record_by"] == 156 and ten["change_seconds"] == -65
    five = next(r for r in rows if r["key"] == "5K")
    assert five["record_seconds"] is None and five["change_seconds"] is None


def test_latest_by_distance():
    rows = [{"snapshot_date": "2026-06-01", "distance": "5K", "seconds": 1250},
            {"snapshot_date": "2026-09-01", "distance": "5K", "seconds": 1210}]
    assert race_predictor.latest_by_distance(rows) == {"5K": 1210}
    assert race_predictor.latest_by_distance(rows, "2026-07-01") == {"5K": 1250}


# ── goal race view ───────────────────────────────────────────────────────────

GOAL = {"name": "Fall Classic", "date": "2026-10-11", "distance": "half", "target_sec": 5520, "show_on_today": True}


def _preds():
    return {"run": [{"key": "half_marathon", "seconds": 5620}], "tri": {"rows": [{"key": "olympic", "seconds": 9000}]}}


def test_goal_view_joins_prediction_and_race_day_form(monkeypatch):
    monkeypatch.setattr(goal_race, "get_goal_race", lambda: dict(GOAL))
    fit = {"series": [{"date": "2026-10-11", "form": 9}]}
    g = dashboard_data.goal_view(TODAY, fit, _preds())
    assert (g["predicted_sec"], g["gap_sec"], g["days_left"]) == (5620, 100, 16)
    assert g["race_form"]["on_target"] is True

    monkeypatch.setattr(goal_race, "get_goal_race", lambda: {**GOAL, "distance": "olympic", "target_sec": None})
    g = dashboard_data.goal_view(TODAY, {"series": []}, _preds())
    assert (g["predicted_sec"], g["gap_sec"], g["race_form"]) == (9000, None, None)

    monkeypatch.setattr(goal_race, "get_goal_race", lambda: None)
    assert dashboard_data.goal_view(TODAY, None, None) is None


def _card(**over):
    g = goal_race.describe(GOAL, TODAY)
    g.update(predicted_sec=5620, gap_sec=100, race_form={"form": 9, "on_target": True, "too_fresh": False,
                                                          "zone": {"label": "Fresh"}})
    g.update(over)
    return dashboard._goal_card(g)


def test_goal_card_build_phase():
    html = _card()
    assert "Fall Classic" in html and "Sun Oct 11 · Half marathon" in html
    assert ">16<" in html and ">Build<" in html
    assert "1:33:40" in html and "1:40 to find" in html
    assert "+9" in html and "Fresh · on target" in html
    assert "Taper starts in 6 days (Thu Oct 1)." in html


def test_goal_card_ahead_too_fresh_and_outside_the_window():
    html = _card(gap_sec=-45, race_form={"form": 18, "on_target": False, "too_fresh": True, "zone": {"label": "Fresh"}})
    assert "0:45 ahead" in html and "Too fresh · target +5 to +15" in html
    assert "Plan ends before race" in _card(race_form=None)
    assert "Shown 6 weeks out" in _card(show_form=False)


def test_goal_card_taper_race_day_and_done():
    assert "In taper: keep intensity, cut volume." in dashboard._goal_card(
        {**goal_race.describe(GOAL, date(2026, 10, 3)), "race_form": None})
    assert ">Race day<" in dashboard._goal_card({**goal_race.describe(GOAL, date(2026, 10, 11)), "race_form": None})
    done = dashboard._goal_card({**goal_race.describe(GOAL, date(2026, 10, 12)), "race_form": None})
    assert ">Done<" in done and "Race complete." in done and "&#10003;" in done


def test_today_shows_the_card_only_when_switched_on():
    goal = {**goal_race.describe(GOAL, TODAY), "race_form": None}
    base = {"date": "2026-09-25", "plan": None}
    assert "Goal race" in dashboard._panel_today({**base, "redesign": {"goal": goal}})
    assert "Goal race" not in dashboard._panel_today({**base, "redesign": {"goal": {**goal, "show_on_today": False}}})
    assert "Goal race" not in dashboard._panel_today(base)


def test_build_without_a_database(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    assert dashboard_data.build(TODAY, None, None, None) == {
        "fitness": None, "predictions": None, "goal": None, "thresholds": None, "records": None}


def test_build_isolates_a_failing_section(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(dashboard_data, "fitness", lambda today: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(dashboard_data, "predictions", lambda *a: _preds())
    monkeypatch.setattr(dashboard_data, "threshold_history", lambda *a: {})
    monkeypatch.setattr(dashboard_data, "records_progress", lambda *a: {})
    monkeypatch.setattr(goal_race, "get_goal_race", lambda: dict(GOAL))
    out = dashboard_data.build(TODAY, None, None, None)
    assert out["fitness"] is None and out["goal"]["predicted_sec"] == 5620
