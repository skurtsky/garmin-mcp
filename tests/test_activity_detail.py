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


def test_parse_start_seconds_accepts_space_separator():
    """Garmin's startTimeLocal isn't consistently 'T'-separated — a
    space-separated value used to silently parse as midnight."""
    assert ad._parse_start_seconds("2026-08-21 05:46:07") == 5 * 3600 + 46 * 60 + 7


def test_fmt_activity_datetime_matches_mockup_style():
    assert ad._fmt_activity_datetime("2026-08-21T05:46:07") == "August 21, 2026 at 05:46"


def test_fmt_activity_datetime_accepts_space_separator():
    assert ad._fmt_activity_datetime("2026-08-21 05:46:07") == "August 21, 2026 at 05:46"


def test_resolve_start_iso_prefers_summary_date():
    row = {"local_start_iso": "2026-08-21T05:46:07", "activity_date": None}
    assert ad._resolve_start_iso(row) == "2026-08-21T05:46:07"


def test_resolve_start_iso_falls_back_to_activity_date(monkeypatch):
    """A row synced before activities.summary carried a 'date' key (or any
    other reason it comes back empty) still gets a timestamp, derived from
    the always-present activity_date column instead of going blank."""
    from datetime import datetime, timezone
    monkeypatch.setattr(ad, "_tz_offset_hours", lambda: -4)
    row = {"local_start_iso": None, "activity_date": datetime(2026, 8, 21, 9, 46, 7, tzinfo=timezone.utc)}
    assert ad._resolve_start_iso(row) == "2026-08-21T05:46:07"


def test_resolve_start_iso_none_when_nothing_available():
    assert ad._resolve_start_iso({"local_start_iso": None, "activity_date": None}) is None


def test_swim_duration_sec_excludes_rest_laps():
    laps = [
        {"distance_m": 50, "duration_sec": 40},
        {"distance_m": 0, "duration_sec": 15},   # rest at the wall
        {"distance_m": 25, "duration_sec": 22},  # one length still counts
        {"distance_m": 10, "duration_sec": 8},   # under 20m -> not a real length
    ]
    assert ad._swim_duration_sec(laps) == 40 + 22


def test_swim_duration_sec_none_when_nothing_qualifies():
    assert ad._swim_duration_sec([{"distance_m": 0, "duration_sec": 30}]) is None


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


def test_render_header_shows_timestamp_for_space_separated_iso():
    html = ad.render_activity_detail_fragment(_row(local_start_iso="2026-08-20 18:00:00"))
    assert "August 20, 2026 at 18:00" in html


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
    # No standalone "Route" header — the map is self-explanatory (issue 74 feedback)
    assert 'section-title" style="margin-bottom:0">Route<' not in html
    # The map sits right after the name/date header, before quick stats
    assert html.index('id="activity-map"') < html.index('ad-stat-grid')


def test_render_map_shows_weather_overlay_instead_of_a_weather_card():
    route = [{"lat": 45.39 + i * 0.001, "lon": -75.74 + i * 0.001,
              "elevation_m": 70 + i, "distance_km": i * 0.1} for i in range(20)]
    detail = dict(_BASE_DETAIL)
    detail["weather"] = {"temp_c": 11.1, "humidity_pct": 80, "wind_kph": 12, "wind_dir": "N", "conditions": "Clear"}
    html = ad.render_activity_detail_fragment(_row(detail=detail, route=route))
    assert "ad-map-weather" in html
    assert "11&deg;C" in html
    # No separate weather card when the map (and its overlay) is present
    assert html.count('class="card"') == 0 or "Conditions" not in html


def test_render_drops_weather_entirely_without_a_route():
    """An indoor/no-GPS activity (strength, pool swim, yoga, ...) gets no
    weather at all — not the map overlay (no map to overlay it on) and not
    a fallback card either."""
    detail = dict(_BASE_DETAIL)
    detail["weather"] = {"temp_c": 11.1, "humidity_pct": 80, "wind_kph": 12, "wind_dir": "N", "conditions": "Clear"}
    html = ad.render_activity_detail_fragment(_row(detail=detail, route=None))
    assert "ad-map-weather" not in html
    assert "Conditions" not in html
    assert "11" not in html.split("Avg HR")[0]  # no stray temp reading pre-stats either


def test_render_drops_elevation_without_a_route():
    detail = dict(_BASE_DETAIL)
    detail["elevation_gain_m"] = 229
    html = ad.render_activity_detail_fragment(_row(detail=detail, route=None))
    assert "Elevation" not in html

    route = [{"lat": 45.39, "lon": -75.74, "elevation_m": 70, "distance_km": 0}]
    html_with_route = ad.render_activity_detail_fragment(_row(detail=detail, route=route))
    assert "Elevation" in html_with_route


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
    # Big current-value readout + zone-breakdown table, both present
    assert "ad-chart-num" in html
    assert "ad-zone-row-head" in html
    assert "DURATION" in html


def test_render_skips_power_section_without_avg_power():
    detail = dict(_BASE_DETAIL)
    detail["power_series"] = [{"t_offset_sec": t, "value": 150} for t in range(0, 100, 5)]
    # avg_power missing/None -> no power section even though a series exists
    html = ad.render_activity_detail_fragment(_row(detail=detail, activity_type="road_biking"))
    assert "activity-power-chart" not in html


def test_render_skips_power_section_for_non_cycling_sports():
    """A runner's footpod/watch can report power too, but the power chart
    is cycling-only (issue 74 feedback)."""
    detail = dict(_BASE_DETAIL)
    detail["power_series"] = [{"t_offset_sec": t, "value": 150 + (t % 20)} for t in range(0, 300, 5)]
    detail["avg_power"] = 250
    html = ad.render_activity_detail_fragment(_row(detail=detail, activity_type="running"))
    assert "activity-power-chart" not in html


def test_render_hr_and_power_sections_share_structure():
    detail = dict(_BASE_DETAIL)
    detail["hr_series"] = [{"t_offset_sec": t, "value": 120 + (t % 10)} for t in range(0, 300, 5)]
    detail["hr_zones"] = [{"zone": 1, "min_hr": 90, "time_min": 4.0, "pct_time": 80.0}]
    detail["power_series"] = [{"t_offset_sec": t, "value": 150 + (t % 20)} for t in range(0, 300, 5)]
    detail["avg_power"] = 160
    detail["ftp"] = 250
    html = ad.render_activity_detail_fragment(_row(detail=detail, activity_type="road_biking"))
    assert html.count("ad-chart-num") == 2
    assert html.count("ad-bounds-bar") == 2
    assert html.count("ad-zone-row-head") == 2


def test_zone_table_column_order_is_zone_duration_bar_pct():
    rows_html = ad._zone_table_html([("Zone 1", 10.0, 50.0, ad._HR_ZONE_COLORS[1])])
    zone_idx = rows_html.index("Zone 1")
    duration_idx = rows_html.index("10:00")
    bar_idx = rows_html.index("ad-zone-bar")
    pct_idx = rows_html.index("50.0%")
    assert zone_idx < duration_idx < bar_idx < pct_idx


def test_splits_table_drops_lap_number_for_non_swim():
    laps = [{"lap_num": 1, "cum_km": 10, "duration_sec": 1200, "speed_kph": 30, "avg_hr": 140}]
    html = ad._splits_table_html(laps, "road_biking")
    assert "<th>Lap</th>" not in html
    assert "10.00 km" in html


def test_splits_table_keeps_lap_number_for_swim():
    laps = [{"lap_num": 1, "cum_km": 0.05, "distance_m": 50, "duration_sec": 40,
              "speed_kph": 4.5, "avg_hr": 130}]
    html = ad._splits_table_html(laps, "lap_swimming")
    assert "<th>Lap</th>" in html
    assert "50 m" in html
    assert "<th>Distance</th>" in html and "<th>Pace</th>" in html
    assert "km/h" not in html  # swim splits show pace, never a raw speed


def test_render_swim_quick_stats_show_swim_duration_strokes_and_swolf():
    detail = dict(_BASE_DETAIL)
    detail["duration_active_sec"] = 3600
    detail["avg_strokes_per_length"] = 14.5
    detail["avg_swolf"] = 38
    detail["laps"] = [
        {"lap_num": 1, "distance_m": 50, "duration_sec": 45, "cum_km": 0.05},
        {"lap_num": 2, "distance_m": 0, "duration_sec": 20, "cum_km": 0.05},
    ]
    html = ad.render_activity_detail_fragment(
        _row(activity_type="lap_swimming", detail=detail, route=None))
    assert "Swim Duration" in html
    assert "Active Duration" not in html
    assert "0:45" in html  # the 45s swum lap, not the 65s total incl. rest
    # Strokes/length, not the swim cadence *rate* (a different metric —
    # see the review comment this fixed)
    assert "Avg Strokes/Length" in html and "14.5 spl" in html
    assert "SWOLF" in html and "38" in html


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


def test_render_gear_shows_wear_bar_pct_and_header(monkeypatch):
    detail = dict(_BASE_DETAIL)
    detail["gear"] = [
        {"name": "Chain", "uuid": "c1", "type": "Component", "distance_km": 1500, "lifespan_km": 3000},
        {"name": "Frame", "uuid": "f1", "type": "Bike", "distance_km": 5000, "lifespan_km": None},
    ]
    html = ad.render_activity_detail_fragment(_row(detail=detail), token="tok123")
    assert "Chain" in html and "Frame" in html
    assert "gt-wearbar" in html
    assert "50%" in html  # 1500/3000
    assert "COMPONENT" in html and "DISTANCE" in html and "LIFE USED" in html
    assert "Open in Gear Tracker" in html
    assert "token=tok123" in html


def test_render_training_effect_shows_load_and_primary_benefit():
    detail = dict(_BASE_DETAIL)
    detail["training_effect"] = 3.1
    detail["anaerobic_te"] = 1.3
    detail["training_effect_label"] = "AEROBIC_BASE"
    html = ad.render_activity_detail_fragment(_row(detail=detail, training_load=107.4))
    assert "Primary Benefit" in html
    assert "Training Load" in html
    assert "107" in html
    assert "Aerobic base" in html


def test_smooth_run_reduces_variance_of_a_noisy_1hz_series():
    import random
    random.seed(0)
    run = [{"t_offset_sec": t, "value": 140 + random.randint(-20, 20)} for t in range(0, 300)]
    smoothed = ad._smooth_run(run)
    raw_values = [p["value"] for p in run]
    smoothed_values = [p["value"] for p in smoothed]

    def variance(vals):
        mean = sum(vals) / len(vals)
        return sum((v - mean) ** 2 for v in vals) / len(vals)

    assert variance(smoothed_values) < variance(raw_values) * 0.5


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
