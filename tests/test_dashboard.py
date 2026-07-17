# tests/test_dashboard.py
"""Tests for the server-rendered health dashboard (tools/dashboard.py).

These are offline unit tests: `render_dashboard_html` is a pure function of a
data dict, and `build_dashboard_data` is exercised with the underlying tool
functions monkeypatched, so no live Garmin session is needed.
"""
from tools import dashboard


SAMPLE = {
    "date": "2026-07-17",
    "generated_at": "2026-07-17 08:30",
    "tz_offset_hours": -4.0,
    "readiness": {
        "body_battery": {"current_level": 62, "charged": 71, "drained": 44,
                         "highest": 88, "lowest": 18, "feedback": "Recharged well overnight"},
        "daily_stats": {"resting_hr": 48, "resting_hr_7day_avg": 50,
                        "avg_stress": 28, "max_stress": 92, "total_steps": 8123},
    },
    "readiness_err": None,
    "health": {
        "heart_rate": {"resting_hr": 48, "max_hr": 142, "min_hr": 44,
                       "seven_day_avg_resting_hr": 50},
        "stress": {"avg_stress": 28, "max_stress": 92, "rest_stress_mins": 410.0,
                   "low_stress_mins": 180.5, "medium_stress_mins": 60.0, "high_stress_mins": 12.0},
        "body_battery": {"charged": 71, "drained": 44},
    },
    "health_err": None,
    "sleep": {"sleep_score": 84, "sleep_score_label": "good", "total_sleep_hrs": 7.4,
              "deep_sleep_hrs": 1.2, "light_sleep_hrs": 4.1, "rem_sleep_hrs": 1.8, "awake_hrs": 0.3,
              "deep_pct": 16.2, "light_pct": 55.4, "rem_pct": 24.3, "awake_count": 3},
    "sleep_err": None,
    "training": {"readiness": {"score": 76, "level": "READY", "feedback_short": "Good to train"}},
    "training_err": None,
    "activities": [
        {"date": "2026-07-16T18:00:00", "name": "Evening Run", "type": "running",
         "distance_km": 10.2, "duration_min": 52.3, "training_load": 120.0},
    ],
    "activities_err": None,
    "week": {"week_start": "2026-07-13", "week_end": "2026-07-17", "total_activities": 5,
             "total_distance_km": 62.4, "total_duration_min": 330.0, "total_training_load": 410.0,
             "by_type": {"running": {"count": 3, "distance_km": 32.4, "duration_min": 170.0}}},
    "week_err": None,
}


def test_render_includes_all_sections():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert html.startswith("<!doctype html>")
    for title in ("Body Battery", "Sleep", "Heart Rate", "Stress",
                  "Training Readiness", "Recent Activities", "This Week's Training"):
        assert title in html


def test_render_shows_key_values():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "76" in html          # readiness score
    assert "84" in html          # sleep score
    assert "62" in html          # body battery current
    assert "Evening Run" in html
    assert "Good to train" in html


def test_render_degrades_when_sections_missing():
    data = {k: None for k in SAMPLE}
    data.update({
        "date": "2026-07-17", "generated_at": "x", "tz_offset_hours": 0,
        "readiness_err": "Boom", "health_err": "Boom", "sleep_err": "Boom",
        "training_err": "Boom", "activities_err": "Boom", "week_err": "Boom",
    })
    html = dashboard.render_dashboard_html(data)
    assert html.startswith("<!doctype html>")
    assert "Unavailable" in html


def test_render_escapes_untrusted_strings():
    data = dict(SAMPLE)
    data["activities"] = [{"date": "2026-07-16T18:00:00",
                           "name": "<script>alert(1)</script>", "type": "running",
                           "distance_km": 1.0, "duration_min": 5.0, "training_load": 1.0}]
    html = dashboard.render_dashboard_html(data)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_none_values_render_as_dash():
    data = dict(SAMPLE)
    data["sleep"] = {"sleep_score": None, "sleep_score_label": None, "total_sleep_hrs": None,
                     "deep_sleep_hrs": None, "light_sleep_hrs": None, "rem_sleep_hrs": None,
                     "awake_hrs": None, "deep_pct": None, "light_pct": None,
                     "rem_pct": None, "awake_count": None}
    html = dashboard.render_dashboard_html(data)
    assert "&mdash;" in html


def test_build_dashboard_data_aggregates(monkeypatch):
    from tools import health, activities

    monkeypatch.setattr(health, "get_daily_readiness", lambda d: {"body_battery": {"current_level": 60}})
    monkeypatch.setattr(health, "get_daily_health", lambda d: {"heart_rate": {"resting_hr": 47}})
    monkeypatch.setattr(health, "get_sleep", lambda d: {"sleep_score": 80})
    monkeypatch.setattr(health, "get_training_readiness", lambda d: {"readiness": {"score": 70}})
    monkeypatch.setattr(activities, "get_activities", lambda limit=5: [{"name": "Run"}])
    monkeypatch.setattr(activities, "get_weekly_summary", lambda: {"total_activities": 3})

    data = dashboard.build_dashboard_data()
    assert data["readiness"]["body_battery"]["current_level"] == 60
    assert data["sleep"]["sleep_score"] == 80
    assert data["activities"] == [{"name": "Run"}]
    assert all(data[k] is None for k in
               ("readiness_err", "health_err", "sleep_err", "training_err",
                "activities_err", "week_err"))


def test_build_dashboard_data_captures_section_errors(monkeypatch):
    from tools import health, activities

    def boom(*a, **k):
        raise RuntimeError("garmin down")

    monkeypatch.setattr(health, "get_daily_readiness", boom)
    monkeypatch.setattr(health, "get_daily_health", boom)
    monkeypatch.setattr(health, "get_sleep", lambda d: {"sleep_score": 80})
    monkeypatch.setattr(health, "get_training_readiness", boom)
    monkeypatch.setattr(activities, "get_activities", lambda limit=5: [])
    monkeypatch.setattr(activities, "get_weekly_summary", boom)

    data = dashboard.build_dashboard_data()
    assert data["readiness"] is None
    assert "garmin down" in data["readiness_err"]
    assert data["sleep"] == {"sleep_score": 80}   # unaffected section still populated
    # The whole thing still renders without raising.
    assert dashboard.render_dashboard_html(data).startswith("<!doctype html>")
