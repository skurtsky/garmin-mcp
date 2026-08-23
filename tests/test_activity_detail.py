# tests/test_activity_detail.py
"""Tests for the activity-detail modal (tools/activity_detail.py, issue 74).

Fully offline: db.get_activity_with_detail is monkeypatched so no live
Postgres or Garmin session is needed. Routes are exercised with Starlette's
TestClient, same as tests/test_gear_tracker.py.
"""
import db
from starlette.testclient import TestClient

from tools import activity_detail as ad


# ── PURE HELPERS ─────────────────────────────────────────────────────────────

def test_zone_index_for_picks_the_highest_matching_bound():
    bounds = [0, 92, 130, 134, 139, 149]
    assert ad._zone_index_for(50, bounds) == 0
    assert ad._zone_index_for(92, bounds) == 1
    assert ad._zone_index_for(133, bounds) == 2
    assert ad._zone_index_for(200, bounds) == 5


def test_power_bounds_from_ftp_matches_coggan_zones():
    # Classic 7-zone Coggan power zones scaled to a 265W FTP.
    assert ad._power_bounds_from_ftp(265) == [0, 146, 199, 238, 278, 318, 398]


def test_power_bounds_from_ftp_none_when_missing():
    assert ad._power_bounds_from_ftp(None) is None
    assert ad._power_bounds_from_ftp(0) is None


def test_is_gap_true_only_when_pause_overlaps_the_sample_pair():
    pauses = [{"start_sec": 100, "end_sec": 200}]
    assert ad._is_gap(90, 210, pauses) is True   # straddles the pause
    assert ad._is_gap(50, 90, pauses) is False   # entirely before
    assert ad._is_gap(210, 250, pauses) is False  # entirely after


def test_split_runs_breaks_on_a_pause():
    series = [{"t_offset_sec": t, "value": 1} for t in (0, 10, 20, 500, 510)]
    pauses = [{"start_sec": 20, "end_sec": 490}]
    runs = ad._split_runs(series, pauses)
    assert [len(r) for r in runs] == [3, 2]


def test_speed_or_pace_matches_sport():
    assert ad._speed_or_pace("road_biking", 30.0) == ("Speed", "30.0 km/h")
    assert ad._speed_or_pace("running", 12.0)[0] == "Pace"
    assert ad._speed_or_pace("lap_swimming", 3.6)[0] == "Pace"


def test_fmt_hms_hides_hours_under_an_hour():
    assert ad._fmt_hms(125) == "2:05"
    assert ad._fmt_hms(3725) == "1:02:05"
    assert ad._fmt_hms(None) == "&mdash;"


def test_parse_start_seconds_reads_time_of_day():
    assert ad._parse_start_seconds("2026-08-21T05:46:07") == 5 * 3600 + 46 * 60 + 7
    assert ad._parse_start_seconds(None) == 0


def test_fmt_activity_datetime_matches_mockup_style():
    assert ad._fmt_activity_datetime("2026-08-21T05:46:07") == "August 21, 2026 at 05:46"


# ── FRAGMENT RENDERING ───────────────────────────────────────────────────────

_BASE_DETAIL = {
    "duration_active_sec": 2700, "elevation_gain_m": None, "calories": 320,
    "avg_speed_kph": 0, "avg_power": None, "ftp": None,
    "training_effect": None, "anaerobic_te": None, "training_effect_label": None,
    "weather": None, "hr_zones": [], "hr_series": [], "power_series": [],
    "pauses": [], "laps": [], "gear": [],
}


def _row(**overrides):
    row = {
        "garmin_id": 5, "activity_type": "strength_training", "name": "Gym Session",
        "distance_km": None, "duration_min": 45.0, "avg_hr": 110, "training_load": 30.0,
        "local_start_iso": "2026-08-20T18:00:00",
        "detail": dict(_BASE_DETAIL),
        "route": None,
    }
    row.update(overrides)
    return row


def test_render_minimal_activity_has_no_map_or_charts():
    html = ad.render_activity_detail_fragment(_row())
    assert "Gym Session" in html
    assert "activity-map" not in html
    assert "js-linechart" not in html


def test_render_includes_route_map_when_route_present():
    route = [{"lat": 45.39 + i * 0.001, "lon": -75.74 + i * 0.001,
              "elevation_m": 70 + i, "distance_km": i * 0.1} for i in range(20)]
    html = ad.render_activity_detail_fragment(_row(route=route))
    assert 'id="activity-map"' in html
    assert "data-route=" in html


def test_render_includes_hr_chart_when_hr_series_present():
    detail = dict(_BASE_DETAIL)
    detail["hr_series"] = [{"t_offset_sec": t, "value": 120 + (t % 10)} for t in range(0, 300, 5)]
    detail["hr_zones"] = [
        {"zone": 1, "min_hr": 90, "time_min": 4.0, "pct_time": 80.0},
        {"zone": 2, "min_hr": 130, "time_min": 1.0, "pct_time": 20.0},
    ]
    html = ad.render_activity_detail_fragment(_row(detail=detail))
    assert 'id="activity-hr-chart"' in html
    assert "Zone 1" in html


def test_render_skips_power_section_without_avg_power():
    detail = dict(_BASE_DETAIL)
    detail["power_series"] = [{"t_offset_sec": t, "value": 150} for t in range(0, 100, 5)]
    # avg_power missing/None -> no power section even though a series exists
    html = ad.render_activity_detail_fragment(_row(detail=detail))
    assert "activity-power-chart" not in html


def test_render_includes_multisport_legs():
    detail = dict(_BASE_DETAIL)
    detail["sub_activities"] = [
        {"type": "open_water_swimming", "name": "Swim", "duration_s": 900,
         "distance_m": 750, "pace_per_100m": "2:00"},
        {"type": "transition_v2", "name": "T1", "duration_s": 120},
        {"type": "road_biking", "name": "Bike", "duration_s": 2700,
         "distance_m": 20000, "avg_speed_kmh": 26.7},
    ]
    html = ad.render_activity_detail_fragment(_row(
        activity_type="multi_sport", name="Sprint Triathlon", detail=detail))
    assert "Legs" in html
    assert "Swim" in html and "T1" in html and "Bike" in html


def test_render_gear_shows_wear_bar_only_with_a_lifespan():
    detail = dict(_BASE_DETAIL)
    detail["gear"] = [
        {"name": "Chain", "uuid": "c1", "type": "Component", "distance_km": 1500, "lifespan_km": 3000},
        {"name": "Frame", "uuid": "f1", "type": "Bike", "distance_km": 5000, "lifespan_km": None},
    ]
    html = ad.render_activity_detail_fragment(_row(detail=detail))
    assert "Chain" in html and "Frame" in html
    assert "gt-wearbar" in html


# ── ROUTE ────────────────────────────────────────────────────────────────────

def _client():
    return TestClient(ad.create_app())


def test_get_activity_fragment_without_db_shows_empty_state(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    resp = _client().get("/api/activity/1")
    assert resp.status_code == 200
    assert "no database configured" in resp.text


def test_get_activity_fragment_not_yet_synced_shows_empty_state(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_activity_with_detail", lambda garmin_id: None)
    resp = _client().get("/api/activity/999")
    assert resp.status_code == 200
    assert "synced yet" in resp.text


def test_get_activity_fragment_renders_row(monkeypatch):
    row = _row()
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_activity_with_detail", lambda garmin_id: row)
    resp = _client().get("/api/activity/5")
    assert resp.status_code == 200
    assert "Gym Session" in resp.text


def test_owns_path():
    assert ad.owns_path("/api/activity/123")
    assert not ad.owns_path("/dashboard")
