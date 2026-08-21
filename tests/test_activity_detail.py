# tests/test_activity_detail.py
"""Tests for the Activity Detail page (tools/dashboard.py, issue 74).

Offline unit tests: render_activity_detail_html is a pure function of a data
dict (mirroring render_dashboard_html — see tests/test_dashboard.py), and
get_activity_detail_data is exercised with the underlying tool functions
monkeypatched, so no live Garmin session is needed.
"""
import pytest

from tools import dashboard


SAMPLE_DETAIL = {
    "summary": {
        "id": 123, "name": "Ottawa Road Cycling", "type": "road_biking",
        "date": "2026-08-21 05:46:07", "distance_km": 63.24, "duration_min": 132.6,
        "elevation_gain_m": 229, "avg_hr": 125, "max_hr": 147, "calories": 1644,
        "training_effect": 3.1, "anaerobic_training_effect": 1.3,
        "training_load": 107.4, "training_effect_label": "AEROBIC_BASE",
        "avg_power": 181, "normalized_power": 190, "ftp": 265,
    },
    "laps": [
        {"lap": 1, "distance_km": 10.0, "avg_pace": "2:05 /km", "avg_hr": 124,
         "avg_power_w": 187},
        {"lap": 2, "distance_km": 10.0, "avg_pace": "2:00 /km", "avg_hr": 128,
         "avg_power_w": 192},
    ],
    "hr_zones": [
        {"zone": 1, "min_hr": 90, "time_min": 87.7, "pct_time": 66.1},
        {"zone": 2, "min_hr": 130, "time_min": 29.2, "pct_time": 22.0},
    ],
    "weather": {"temp_c": 11.1, "humidity_pct": 100, "wind_speed": 5,
                "wind_direction_compass": "N", "conditions": "Mostly Clear"},
    "gear": [{"name": "Canyon Endurace 7", "uuid": "bike-1", "type": "Bike",
              "distance_km": 5742.07}],
    "sub_activities": [],
}

SAMPLE_ROUTE = {
    "points": [{"lat": 45.40, "lon": -75.70}, {"lat": 45.41, "lon": -75.71},
               {"lat": 45.42, "lon": -75.69}],
    "elevation_profile": [
        {"distance_km": 0.0, "elevation_m": 60.0},
        {"distance_km": 1.2, "elevation_m": 75.0},
        {"distance_km": 2.5, "elevation_m": 55.0},
    ],
    "elevation_gain_m": 25.0,
    "elevation_loss_m": 20.0,
}


def _data(**overrides):
    base = {
        "activity_id": 123,
        "detail": SAMPLE_DETAIL, "detail_err": None,
        "route": SAMPLE_ROUTE, "route_err": None,
    }
    base.update(overrides)
    return base


def test_render_is_a_complete_document():
    html = dashboard.render_activity_detail_html(_data())
    assert html.startswith("<!doctype html>")
    assert html.endswith("</html>")


def test_render_shows_name_and_date():
    html = dashboard.render_activity_detail_html(_data())
    assert "Ottawa Road Cycling" in html
    assert "Aug 21, 2026" in html


def test_render_back_link_carries_token_and_goes_to_activity_tab():
    html = dashboard.render_activity_detail_html(_data(), token="t0k")
    assert 'href="/dashboard?tab=activity&amp;token=t0k"' in html
    assert "Back to Activity" in html


def test_render_shows_quick_stats():
    html = dashboard.render_activity_detail_html(_data())
    assert "Duration" in html
    assert "Distance" in html
    assert "Avg Speed" in html
    assert "Elevation Gain" in html
    assert "Calories" in html
    assert "Avg HR" in html
    assert "Avg Power" in html


def test_render_shows_training_effect_card():
    html = dashboard.render_activity_detail_html(_data())
    assert "Training Effect" in html
    assert "Base" in html          # humanized AEROBIC_BASE
    assert "107.4" in html         # training load


def test_render_shows_hr_zone_breakdown():
    html = dashboard.render_activity_detail_html(_data())
    assert "Heart Rate" in html
    assert "Zone 1" in html
    assert "Zone 2" in html
    assert "66.1%" in html


def test_render_shows_splits_table_with_power_column():
    html = dashboard.render_activity_detail_html(_data())
    assert "Splits" in html
    assert "187 W" in html
    assert "2:05 /km" in html


def test_render_splits_table_omits_power_column_when_no_lap_has_power():
    data = _data()
    data["detail"] = {**SAMPLE_DETAIL, "laps": [
        {"lap": 1, "distance_km": 5.0, "avg_pace": "5:00 /km", "avg_hr": 140, "avg_power_w": None},
    ]}
    html = dashboard.render_activity_detail_html(data)
    splits_start = html.index("Splits")
    splits_end = html.index("Gear", splits_start)
    assert "Power" not in html[splits_start:splits_end]


def test_render_shows_gear_table():
    html = dashboard.render_activity_detail_html(_data())
    assert "Gear" in html
    assert "Canyon Endurace 7" in html
    assert "5742.07 km" in html


def test_render_omits_gear_section_when_no_gear():
    data = _data()
    data["detail"] = {**SAMPLE_DETAIL, "gear": []}
    html = dashboard.render_activity_detail_html(data)
    assert "Component</span>" not in html


def test_render_shows_route_map_and_elevation_when_route_present():
    html = dashboard.render_activity_detail_html(_data())
    assert "<svg viewBox=\"0 0 600 220\"" in html
    assert "&uarr; 25 m" in html
    assert "&darr; 20 m" in html


def test_render_hides_map_section_when_no_route():
    """Activities with no GPS track (pool swim, strength, indoor trainer)
    hide the map/elevation section rather than showing it empty or erroring
    (issue 74)."""
    data = _data(route=None)
    html = dashboard.render_activity_detail_html(data)
    assert "<svg viewBox=\"0 0 600 220\"" not in html
    assert "&uarr;" not in html


def test_render_shows_standalone_weather_when_no_route():
    """Even with no map to overlay it on, real weather data is still shown."""
    data = _data(route=None)
    html = dashboard.render_activity_detail_html(data)
    assert "11&deg;C" in html
    assert "100% humidity" in html


def test_render_shows_error_when_detail_fetch_failed():
    data = _data(detail=None, detail_err="AuthError: expired")
    html = dashboard.render_activity_detail_html(data)
    assert "Activity unavailable" in html
    assert "AuthError" in html
    assert html.startswith("<!doctype html>")


def test_render_shows_multisport_sub_activities():
    data = _data()
    data["detail"] = {**SAMPLE_DETAIL, "sub_activities": [
        {"activity_id": 201, "type": "lap_swimming", "name": "Swim",
         "distance_km": 0.75, "duration_s": 900, "pace_per_100m": "2:00", "avg_hr": 130},
        {"activity_id": 202, "type": "transition", "name": "T1", "duration_s": 60},
        {"activity_id": 203, "type": "road_biking", "name": "Bike",
         "distance_km": 40.0, "duration_s": 4200, "avg_speed_kmh": 34.3,
         "avg_power": 210, "avg_hr": 140},
    ]}
    html = dashboard.render_activity_detail_html(data, token="t0k")
    assert "Legs" in html
    assert "Swim" in html and "Bike" in html and "T1" in html
    assert 'href="/dashboard/activity/201?token=t0k"' in html
    assert 'href="/dashboard/activity/203?token=t0k"' in html


def test_render_escapes_untrusted_activity_name():
    data = _data()
    data["detail"] = {**SAMPLE_DETAIL, "name": None}
    data["detail"]["summary"] = {**SAMPLE_DETAIL["summary"], "name": "<script>alert(1)</script>"}
    html = dashboard.render_activity_detail_html(data)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_no_external_requests():
    html = dashboard.render_activity_detail_html(_data())
    assert "<script src=" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_get_activity_detail_data_aggregates(monkeypatch):
    from tools import activities, activity_route

    monkeypatch.setattr(activities, "get_activity", lambda activity_id: {"summary": {"name": "Run"}})
    monkeypatch.setattr(activity_route, "get_activity_route", lambda activity_id: {"points": []})

    data = dashboard.get_activity_detail_data(123)
    assert data["activity_id"] == 123
    assert data["detail"]["summary"]["name"] == "Run"
    assert data["detail_err"] is None
    assert data["route"] == {"points": []}
    assert data["route_err"] is None


def test_get_activity_detail_data_captures_detail_error_independently_of_route(monkeypatch):
    from tools import activities, activity_route

    def boom(activity_id):
        raise RuntimeError("garmin down")

    monkeypatch.setattr(activities, "get_activity", boom)
    monkeypatch.setattr(activity_route, "get_activity_route", lambda activity_id: None)

    data = dashboard.get_activity_detail_data(123)
    assert data["detail"] is None
    assert "garmin down" in data["detail_err"]
    assert data["route"] is None
    assert data["route_err"] is None  # no GPS track is not an error

    assert dashboard.render_activity_detail_html(data).startswith("<!doctype html>")
