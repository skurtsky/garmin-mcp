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
                         "highest": 88, "lowest": 18, "feedback": "GOOD_SLEEP_LAST_NIGHT"},
        "hrv": {"last_night_avg": 42, "weekly_avg": 45, "status": "BALANCED"},
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
    "training": {"readiness": {"score": 76, "level": "READY", "feedback_short": "Good to train",
                               "acute_load": 573.6}},
    "training_err": None,
    "training_status": {"vo2max": {"running": 52, "cycling": 48}, "acwr": 1.3,
                        "acwr_status": "OPTIMAL"},
    "training_status_err": None,
    "activities": [
        {"date": "2026-07-16T18:00:00", "name": "Evening Run", "type": "running",
         "distance_km": 10.2, "duration_min": 52.3, "training_load": 120.0},
    ],
    "activities_err": None,
    "week": {"week_start": "2026-07-13", "week_end": "2026-07-17", "total_activities": 5,
             "total_distance_km": 62.4, "total_duration_min": 330.0, "total_training_load": 410.0,
             "by_type": {"running": {"count": 3, "distance_km": 32.4, "duration_min": 170.0}}},
    "week_err": None,
    "trends": {"period": "7d", "metrics": {
        "rhr": {"unit": "bpm", "daily": [{"date": "2026-07-11", "value": 49},
                                          {"date": "2026-07-12", "value": 48},
                                          {"date": "2026-07-13", "value": 50},
                                          {"date": "2026-07-14", "value": 47},
                                          {"date": "2026-07-15", "value": 48},
                                          {"date": "2026-07-16", "value": 49},
                                          {"date": "2026-07-17", "value": 48}],
                "start": 49, "end": 48, "delta": -1.0},
        "hrv": {"unit": "ms", "daily": [{"date": "2026-07-11", "value": 40},
                                         {"date": "2026-07-17", "value": 42}],
                "start": 40, "end": 42, "delta": 2.0},
        "sleep_score": {"unit": "score", "daily": [{"date": "2026-07-11", "value": 80},
                                                    {"date": "2026-07-17", "value": 84}],
                        "start": 80, "end": 84, "delta": 4.0},
    }},
    "trends_err": None,
    "last_sync": {"device_name": "Forerunner 965", "upload_time": "2026-07-17T08:05:00.0"},
    "last_sync_err": None,
}


def test_render_includes_all_sections():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert html.startswith("<!doctype html>")
    # "This Week's Training" renders with its apostrophe HTML-escaped, so match
    # the apostrophe-free prefix here.
    for title in ("Body Battery", "Sleep", "Heart Rate", "Stress",
                  "Training Readiness", "Recent Activities", "This Week"):
        assert title in html


def test_render_shows_key_values():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "76" in html          # readiness score
    assert "84" in html          # sleep score
    assert "62" in html          # body battery current
    assert "Evening Run" in html
    assert "Good to train" in html


def test_render_humanizes_enum_strings():
    html = dashboard.render_dashboard_html(SAMPLE)
    # Raw Garmin enum from body-battery feedback is mapped to readable text.
    assert "Good sleep last night" in html
    assert "GOOD_SLEEP_LAST_NIGHT" not in html


def test_render_shows_hrv_and_vo2max():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "HRV" in html
    assert "42" in html and "45" in html      # current + baseline HRV
    assert "VO" in html                        # VO2max label
    assert "52" in html and "48" in html       # run / bike VO2max


def test_render_shows_load_context_and_sparklines():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "573.6" in html                     # acute load
    assert "ACWR" in html
    assert "7-Day Trends" in html
    assert "<svg" in html                       # sparkline present


def test_render_shows_last_sync_and_theme_toggle():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Last Garmin sync" in html
    assert "Forerunner 965" in html
    assert 'id="theme-toggle"' in html


def test_sparklines_omitted_when_absent():
    data = dict(SAMPLE)
    data["trends"] = None
    data["trends_err"] = None
    html = dashboard.render_dashboard_html(data)
    assert "7-Day Trends" not in html


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
    from tools import health, activities, trends

    monkeypatch.setattr(health, "get_daily_readiness", lambda d: {"body_battery": {"current_level": 60}})
    monkeypatch.setattr(health, "get_daily_health", lambda d: {"heart_rate": {"resting_hr": 47}})
    monkeypatch.setattr(health, "get_sleep", lambda d: {"sleep_score": 80})
    monkeypatch.setattr(health, "get_training_readiness", lambda d: {"readiness": {"score": 70}})
    monkeypatch.setattr(health, "get_training_status", lambda d: {"vo2max": {"running": 51}})
    monkeypatch.setattr(activities, "get_activities", lambda limit=5: [{"name": "Run"}])
    monkeypatch.setattr(activities, "get_weekly_summary", lambda: {"total_activities": 3})
    monkeypatch.setattr(trends, "get_trends", lambda period, metrics: {"period": period, "metrics": {}})
    monkeypatch.setattr(dashboard, "_fetch_last_sync", lambda: {"device_name": "Watch", "upload_time": "x"})

    data = dashboard.build_dashboard_data()
    assert data["readiness"]["body_battery"]["current_level"] == 60
    assert data["sleep"]["sleep_score"] == 80
    assert data["activities"] == [{"name": "Run"}]
    assert data["training_status"]["vo2max"]["running"] == 51
    assert data["trends"]["period"] == "7d"
    assert data["last_sync"]["device_name"] == "Watch"
    assert all(data[k] is None for k in
               ("readiness_err", "health_err", "sleep_err", "training_err",
                "training_status_err", "activities_err", "week_err",
                "trends_err", "last_sync_err"))


def test_build_dashboard_data_captures_section_errors(monkeypatch):
    from tools import health, activities, trends

    def boom(*a, **k):
        raise RuntimeError("garmin down")

    monkeypatch.setattr(health, "get_daily_readiness", boom)
    monkeypatch.setattr(health, "get_daily_health", boom)
    monkeypatch.setattr(health, "get_sleep", lambda d: {"sleep_score": 80})
    monkeypatch.setattr(health, "get_training_readiness", boom)
    monkeypatch.setattr(health, "get_training_status", boom)
    monkeypatch.setattr(activities, "get_activities", lambda limit=5: [])
    monkeypatch.setattr(activities, "get_weekly_summary", boom)
    monkeypatch.setattr(trends, "get_trends", boom)
    monkeypatch.setattr(dashboard, "_fetch_last_sync", boom)

    data = dashboard.build_dashboard_data()
    assert data["readiness"] is None
    assert "garmin down" in data["readiness_err"]
    assert "garmin down" in data["training_status_err"]
    assert "garmin down" in data["trends_err"]
    assert data["sleep"] == {"sleep_score": 80}   # unaffected section still populated
    # The whole thing still renders without raising.
    assert dashboard.render_dashboard_html(data).startswith("<!doctype html>")
