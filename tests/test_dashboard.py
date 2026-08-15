# tests/test_dashboard.py
"""Tests for the server-rendered health dashboard (tools/dashboard.py).

These are offline unit tests: `render_dashboard_html` is a pure function of a
data dict, and `build_dashboard_data` is exercised with the underlying tool
functions monkeypatched, so no live Garmin session is needed.
"""
import pytest

from tools import dashboard


@pytest.fixture(autouse=True)
def gear_db(tmp_path, monkeypatch):
    """build_dashboard_data() calls tools.gear_tracker.build_gear_status,
    which hits the gear-tracker JSON store — redirect it at a tmp file so
    these tests stay fully offline, matching every other test here.
    (render_dashboard_html itself is a pure function of its data dict and
    doesn't touch gear_tracker — see issue #58.)"""
    monkeypatch.setenv("GEAR_TRACKER_DATA_PATH", str(tmp_path / "gear-tracker" / "gear_data.json"))


def _trend_series(values, unit="bpm"):
    return {"unit": unit, "daily": [{"date": f"2026-07-{i+1:02d}", "value": v}
                                     for i, v in enumerate(values)]}


SAMPLE = {
    "date": "2026-07-17",
    "generated_at": "2026-07-17 08:30",
    "tz_offset_hours": -4.0,
    "readiness": {
        "body_battery": {"current_level": 62, "charged": 71, "drained": 44,
                         "highest": 88, "lowest": 18, "feedback": "GOOD_SLEEP_LAST_NIGHT"},
        "hrv": {"last_night_avg": 42, "weekly_avg": 45, "status": "BALANCED",
                "baseline_low": 35, "baseline_high": 55},
        "daily_stats": {"resting_hr": 48, "resting_hr_7day_avg": 50,
                        "avg_stress": 28, "max_stress": 92, "total_steps": 8123,
                        "active_seconds": 2520},
    },
    "readiness_err": None,
    "health": {
        "heart_rate": {"resting_hr": 48, "max_hr": 142, "min_hr": 44,
                       "seven_day_avg_resting_hr": 50},
        "stress": {"avg_stress": 28, "max_stress": 92, "rest_stress_mins": 410.0,
                   "low_stress_mins": 180.5, "medium_stress_mins": 60.0, "high_stress_mins": 12.0},
        "body_battery": {"charged": 71, "drained": 44},
        "respiration": {"avg_waking": 14, "avg_sleep": 12, "highest": 18, "lowest": 10},
    },
    "health_err": None,
    "sleep": {"sleep_score": 84, "sleep_score_label": "good", "total_sleep_hrs": 7.4,
              "deep_sleep_hrs": 1.2, "light_sleep_hrs": 4.1, "rem_sleep_hrs": 1.8, "awake_hrs": 0.3,
              "deep_pct": 16.2, "light_pct": 55.4, "rem_pct": 24.3, "awake_count": 3,
              "avg_hr": 57, "avg_hrv": 57, "avg_respiration": 13, "sleep_need_hrs": 9.0},
    "sleep_err": None,
    "training": {"readiness": {
        "score": 76, "level": "READY", "feedback_short": "Good to train",
        "acute_load": 573.6, "sleep_score_factor_percent": 62,
        "recovery_time_factor_percent": 70, "acwr_factor_percent": 92,
        "hrv_factor_percent": 99, "stress_history_factor_percent": 88,
    }},
    "training_err": None,
    "training_status": {"vo2max": {"running": 52, "cycling": 48}, "acwr": 1.3,
                        "acwr_status": "OPTIMAL"},
    "training_status_err": None,
    "activities": [
        {"id": 1, "date": "2026-07-17T06:00:00", "name": "Evening Run", "type": "running",
         "distance_km": 10.2, "duration_min": 52.3, "avg_hr": 141, "training_load": 120.0},
        {"id": 2, "date": "2026-07-16T18:00:00", "name": "Pool Swim", "type": "lap_swimming",
         "distance_km": 1.5, "duration_min": 45.0, "avg_hr": 130, "training_load": 60.0},
    ],
    "activities_err": None,
    "week": {"week_start": "2026-07-13", "week_end": "2026-07-17", "total_activities": 5,
             "total_distance_km": 62.4, "total_duration_min": 330.0, "total_training_load": 410.0,
             "by_type": {"running": {"count": 3, "distance_km": 32.4, "duration_min": 170.0},
                        "lap_swimming": {"count": 2, "distance_km": 3.0, "duration_min": 90.0}},
             "activities": [
                 {"date": "2026-07-17T06:00:00", "training_load": 120.0},
                 {"date": "2026-07-16T18:00:00", "training_load": 60.0},
             ]},
    "week_err": None,
    "trends": {"period": "1m", "days": 30, "metrics": {
        "rhr":           _trend_series([49, 48, 50, 47, 48, 49, 48], "bpm"),
        "hrv":           _trend_series([40, 41, 42, 41, 43, 44, 42], "ms"),
        "sleep_score":   _trend_series([80, 81, 79, 82, 83, 84, 84], "score"),
        "stress":        _trend_series([30, 28, 32, 27, 25, 26, 28], "level"),
        "steps":         _trend_series([8000, 9000, 7500, 8123, 9500, 11000, 8123], "steps"),
        "training_load": _trend_series([600, 610, 590, 605, 615, 620, 573.6], "load"),
    }},
    "trends_err": None,
    "personal_records": {
        "running": [{"label": "Fastest 5K", "value_formatted": "20:35",
                    "value_raw": 1235, "date": "2025-10-12", "activity_id": 99}],
        "cycling": [{"label": "Longest Ride", "value_formatted": "165 km",
                    "value_raw": 165000, "date": "2026-07-01", "activity_id": 98}],
        "swimming": [],
    },
    "personal_records_err": None,
    "active_goals": [{"goal_category": "STEPS", "goal_type_name": "Daily Steps",
                      "target_value": 12000, "current_value": 8123}],
    "active_goals_err": None,
    "athlete": {"weight_kg": 72.9, "lactate_threshold_hr": 170,
               "lactate_threshold_pace": 4.28, "ftp": 265},
    "athlete_err": None,
    "last_sync": {"device_name": "Forerunner 965", "upload_time": "2026-07-17T08:05:00.0"},
    "last_sync_err": None,
    "gear_status": {"gear": [
        {"name": "Canyon Ultimate", "model": None, "uuid": "bike-1", "activity_type": "Bike",
         "status": "active", "distance_km": 5800.0, "duration_min": 3000.0,
         "total_activities": 40, "max_distance_km": None,
         "date_begin": "2025-01-01", "date_end": None,
         "status_indicator": "yellow", "status_emoji": "\U0001F7E1", "status_color": "#d9a441",
         "is_bike": True, "is_shoe": False,
         "components": [
             {"id": 1, "bike_uuid": "bike-1", "bike_name": "Canyon Ultimate", "name": "Chain",
              "install_date": "2026-01-01", "install_distance_km": 5420.0,
              "maintenance_interval_km": 400.0, "effective_interval_km": 400.0,
              "linked_gear_uuid": None, "last_serviced": "2026-01-01",
              "ever_serviced": False, "distance_since_km": 380.0,
              "status": "yellow", "status_emoji": "\U0001F7E1"},
         ]},
        {"name": "Nike Vaporfly", "model": None, "uuid": "shoe-1", "activity_type": "Shoes",
         "status": "active", "distance_km": 320.0, "duration_min": None,
         "total_activities": 20, "max_distance_km": 800.0,
         "date_begin": "2025-01-01", "date_end": None,
         "status_indicator": "green", "status_emoji": "\U0001F7E2", "status_color": "#4fae72",
         "is_bike": False, "is_shoe": True, "components": []},
    ], "linkable_gear": []},
    "gear_status_err": None,
}


def test_render_is_a_complete_document():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert html.startswith("<!doctype html>")
    assert html.endswith("</html>")


def test_render_includes_all_five_tabs():
    html = dashboard.render_dashboard_html(SAMPLE)
    for marker in ("tp-today", "tp-trends", "tp-activity", "tp-you", "tp-gear"):
        assert marker in html
    for label in ("Trends", "Activity", "Fitness", "Gear"):
        assert label in html


def test_render_today_tab_checked_by_default():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'id="tab-today" checked' in html
    assert 'id="tab-gear" checked' not in html


def test_render_gear_tab_checked_when_requested():
    html = dashboard.render_dashboard_html(SAMPLE, initial_tab="gear")
    assert 'id="tab-gear" checked' in html
    assert 'id="tab-today" checked' not in html


def test_render_unknown_initial_tab_falls_back_to_today():
    html = dashboard.render_dashboard_html(SAMPLE, initial_tab="not-a-real-tab")
    assert 'id="tab-today" checked' in html


def test_render_gear_panel_shows_overview_and_components():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Canyon Ultimate" in html
    assert "Nike Vaporfly" in html
    assert "Chain" in html
    assert "380" in html  # distance since service
    assert "400" in html  # maintenance interval


def test_render_gear_panel_shows_maintenance_log_from_data():
    """The log comes from gear_status["maintenance_log"] (populated by
    build_gear_status), not a live gear_tracker call — the gear_db fixture
    points at an empty database, so this would show nothing if the panel
    still queried it directly (issue #58)."""
    data = {**SAMPLE, "gear_status": {
        **SAMPLE["gear_status"],
        "maintenance_log": [
            {"id": 1, "component_id": 1, "date": "2026-07-10", "action": "lubed",
             "distance_at_service_km": 5400.0, "notes": "squeaky",
             "component_name": "Chain", "bike_name": "Canyon Ultimate", "bike_uuid": "bike-1"},
        ],
    }}
    html = dashboard.render_dashboard_html(data)
    assert "Lubed" in html
    assert "squeaky" in html


def test_render_gear_panel_forms_carry_token():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert 'action="/api/gear/maintenance?token=t0k"' in html
    assert 'action="/api/gear/components?token=t0k"' in html


def test_render_gear_panel_offers_link_component_not_add(monkeypatch):
    """'Add component' (free-text) was replaced with 'Link component' (pick
    from the athlete's own Garmin-tracked gear) — issue 63."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Link component" in html
    assert "Add component" not in html


def test_render_gear_panel_link_form_lists_linkable_gear():
    data = {**SAMPLE, "gear_status": {
        **SAMPLE["gear_status"],
        "linkable_gear": [{"uuid": "chain-1", "name": "Shimano 12s Chain", "model": None,
                           "distance_km": 1969.68, "max_distance_km": 3000.0}],
    }}
    html = dashboard.render_dashboard_html(data)
    assert '<option value="chain-1">' in html
    assert "Shimano 12s Chain" in html


def test_render_gear_panel_component_row_opens_target_modal():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'href="#component-1"' in html
    assert 'id="component-1"' in html


def test_render_gear_panel_shows_error_banner():
    html = dashboard.render_dashboard_html(SAMPLE, error="name is required.")
    assert "name is required." in html


def test_render_gear_panel_no_error_banner_by_default():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "name is required." not in html


def test_render_gear_panel_bike_card_links_to_component_tracker():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'href="#bike-bike-1"' in html
    assert 'id="bike-bike-1"' in html


def test_render_gear_panel_handles_missing_data():
    data = {**SAMPLE, "gear_status": None, "gear_status_err": "AuthError: expired"}
    html = dashboard.render_dashboard_html(data)
    assert "Gear tracker unavailable" in html
    assert "AuthError" in html


def test_render_no_longer_links_out_to_a_separate_gear_page():
    """Gear moved from a footer link into the tab bar (follow-up to #53)."""
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert "/dashboard/gear" not in html


def test_render_shows_key_values():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "76" in html          # readiness score
    assert "84" in html          # sleep score
    assert "62" in html          # body battery current
    assert "Evening Run" in html
    assert "Good to train" in html


def test_render_humanizes_enum_strings():
    html = dashboard.render_dashboard_html(SAMPLE)
    # HRV status enum ("BALANCED") is humanized for the "Today" HRV card.
    assert "Balanced" in html
    assert "BALANCED" not in html


def test_render_last_sync_carries_utc_instant_for_client_side_conversion():
    """The "Last sync" time is server-rendered (offset by the operator's
    DASHBOARD_TZ_OFFSET_HOURS) as a no-JS fallback, but also carries the raw
    UTC instant in a data attribute so the inline script (issue #52) can
    rewrite it to the viewer's actual local timezone."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'id="sync-time"' in html
    assert 'data-sync-utc="2026-07-17T08:05:00+00:00"' in html
    assert "Last sync" in html
    assert "document.querySelectorAll('[data-sync-utc]')" in html
    assert "toLocaleTimeString" in html


def test_render_last_sync_falls_back_when_no_sync_data():
    data = {**SAMPLE, "last_sync": {}, "last_sync_err": "boom"}
    html = dashboard.render_dashboard_html(data)
    assert "Live from Garmin Connect" in html
    assert "data-sync-utc=" not in html


@pytest.mark.parametrize("value,expected", [
    (1784275500000, "2026-07-17T08:05:00+00:00"),
    ("2026-07-17T08:05:00.0", "2026-07-17T08:05:00+00:00"),
    ("2026-07-17T08:05:00", "2026-07-17T08:05:00+00:00"),
    (None, None),
    ("not-a-date", None),
])
def test_sync_time_utc_iso(value, expected):
    assert dashboard._sync_time_utc_iso(value) == expected


def test_render_shows_vo2max_and_acwr():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "52" in html and "48" in html   # run / bike VO2max
    assert "1.30" in html                  # ACWR value
    assert "Optimal" in html


def test_render_shows_readiness_factors():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Sleep" in html
    assert "Recovery" in html
    assert "Load balance" in html
    assert "Stress history" in html


def test_render_shows_trend_charts():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "<svg" in html
    assert "Resting HR" in html
    assert "HRV" in html


def test_range_toggle_offers_7_14_30_when_30_days_fetched():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'id="range-7"' in html
    assert 'id="range-14"' in html
    assert 'id="range-30"' in html
    assert 'checked' in html.split('id="range-30"')[1][:20]


def test_range_toggle_shrinks_to_available_days():
    data = dict(SAMPLE)
    trends = dict(SAMPLE["trends"])
    trends["days"] = 10
    data["trends"] = trends
    html = dashboard.render_dashboard_html(data)
    assert 'id="range-7"' in html
    assert 'id="range-14"' not in html
    assert 'id="range-30"' not in html


def test_render_shows_personal_records_grouped_by_sport():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Personal records" in html
    assert "Fastest 5K" in html
    assert "20:35" in html
    assert "Longest Ride" in html
    assert 'pr-running' in html
    assert 'pr-cycling' in html


def test_render_shows_thresholds_from_athlete_profile():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "170" in html   # LTHR
    assert "265" in html   # FTP
    assert "72.9" in html  # weight


def test_render_shows_activity_list_with_expandable_detail():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert '<details class="actcard">' in html
    assert "Pool Swim" in html
    assert "Avg HR" in html


def test_render_uses_step_goal_from_active_goals():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "12,000" in html or "12000" in html


def test_render_degrades_when_sections_missing():
    data = {k: None for k in SAMPLE}
    data.update({
        "date": "2026-07-17", "generated_at": "x", "tz_offset_hours": 0,
        "readiness_err": "Boom", "health_err": "Boom", "sleep_err": "Boom",
        "training_err": "Boom", "training_status_err": "Boom",
        "activities_err": "Boom", "week_err": "Boom", "trends_err": "Boom",
        "personal_records_err": "Boom", "active_goals_err": "Boom", "athlete_err": "Boom",
    })
    html = dashboard.render_dashboard_html(data)
    assert html.startswith("<!doctype html>")
    assert "unavailable" in html.lower()


def test_render_escapes_untrusted_strings():
    data = dict(SAMPLE)
    data["activities"] = [{"id": 1, "date": "2026-07-16T18:00:00",
                           "name": "<script>alert(1)</script>", "type": "running",
                           "distance_km": 1.0, "duration_min": 5.0, "avg_hr": 100,
                           "training_load": 1.0}]
    html = dashboard.render_dashboard_html(data)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_none_values_render_as_dash():
    data = dict(SAMPLE)
    data["sleep"] = {"sleep_score": None, "sleep_score_label": None, "total_sleep_hrs": None,
                     "deep_sleep_hrs": None, "light_sleep_hrs": None, "rem_sleep_hrs": None,
                     "awake_hrs": None, "deep_pct": None, "light_pct": None,
                     "rem_pct": None, "awake_count": None, "avg_hr": None, "avg_hrv": None,
                     "avg_respiration": None, "sleep_need_hrs": None}
    html = dashboard.render_dashboard_html(data)
    assert "&mdash;" in html


def test_footer_links_to_latest_weekly_report():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert 'class="footer-link"' in html
    assert 'href="/weekly-summary?token=t0k"' in html


def test_footer_link_omits_token_param_when_none():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'href="/weekly-summary"' in html
    assert "token=" not in html.split('class="footer-link"')[1].split("</a>")[0]


def test_no_external_requests():
    """Tabs/range/PR-filter switching stays pure CSS (radio-driven visibility);
    the only JS is the inline chart-interactivity module — no network requests
    (fonts/icons are embedded, and the script has no external src)."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "<script src=" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_render_includes_inline_chart_interactivity_script():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "<script>" in html
    assert "chart-tooltip" in html
    assert "addEventListener" in html
    assert "touchstart" in html and "touchmove" in html


def test_line_charts_expose_point_data_for_tooltips():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'class="js-linechart"' in html
    assert "data-points=" in html
    assert "chart-crosshair" in html
    assert "chart-dot" in html
    assert "chart-hit" in html


def test_bar_charts_are_tappable_with_date_and_value():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'class="js-bar"' in html
    assert "data-date=" in html
    assert "data-value=" in html


def test_render_shows_stress_breakdown_bar_chart():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "stress breakdown" in html.lower()
    assert "Rest" in html and "Medium" in html and "High" in html


def test_build_dashboard_data_aggregates(monkeypatch):
    from tools import health, activities, trends, performance, profile, challenges

    monkeypatch.setattr(health, "get_daily_readiness", lambda d: {"body_battery": {"current_level": 60}})
    monkeypatch.setattr(health, "get_daily_health", lambda d: {"heart_rate": {"resting_hr": 47}})
    monkeypatch.setattr(health, "get_sleep", lambda d: {"sleep_score": 80})
    monkeypatch.setattr(health, "get_training_readiness", lambda d: {"readiness": {"score": 70}})
    monkeypatch.setattr(health, "get_training_status", lambda d: {"vo2max": {"running": 51}})
    monkeypatch.setattr(activities, "get_activities", lambda limit=20: [{"name": "Run"}])
    monkeypatch.setattr(activities, "get_weekly_summary", lambda: {"total_activities": 3})
    monkeypatch.setattr(trends, "get_trends", lambda period, metrics: {"period": period, "days": 30, "metrics": {}})
    monkeypatch.setattr(performance, "get_personal_records", lambda: {"running": []})
    monkeypatch.setattr(profile, "get_athlete_profile", lambda: {"weight_kg": 70})
    monkeypatch.setattr(challenges, "get_active_goals", lambda: [])
    monkeypatch.setattr(dashboard, "_fetch_last_sync", lambda: {"device_name": "Watch", "upload_time": "x"})

    data = dashboard.build_dashboard_data()
    assert data["readiness"]["body_battery"]["current_level"] == 60
    assert data["sleep"]["sleep_score"] == 80
    assert data["activities"] == [{"name": "Run"}]
    assert data["training_status"]["vo2max"]["running"] == 51
    assert data["trends"]["period"] == dashboard.TREND_PERIOD
    assert data["personal_records"] == {"running": []}
    assert data["athlete"]["weight_kg"] == 70
    assert data["last_sync"]["device_name"] == "Watch"
    assert all(data[k] is None for k in
               ("readiness_err", "health_err", "sleep_err", "training_err",
                "training_status_err", "activities_err", "week_err", "trends_err",
                "personal_records_err", "active_goals_err", "athlete_err", "last_sync_err"))


def test_build_dashboard_data_captures_section_errors(monkeypatch):
    from tools import health, activities, trends, performance, profile, challenges

    def boom(*a, **k):
        raise RuntimeError("garmin down")

    monkeypatch.setattr(health, "get_daily_readiness", boom)
    monkeypatch.setattr(health, "get_daily_health", boom)
    monkeypatch.setattr(health, "get_sleep", lambda d: {"sleep_score": 80})
    monkeypatch.setattr(health, "get_training_readiness", boom)
    monkeypatch.setattr(health, "get_training_status", boom)
    monkeypatch.setattr(activities, "get_activities", lambda limit=20: [])
    monkeypatch.setattr(activities, "get_weekly_summary", boom)
    monkeypatch.setattr(trends, "get_trends", boom)
    monkeypatch.setattr(performance, "get_personal_records", boom)
    monkeypatch.setattr(profile, "get_athlete_profile", boom)
    monkeypatch.setattr(challenges, "get_active_goals", boom)
    monkeypatch.setattr(dashboard, "_fetch_last_sync", boom)

    data = dashboard.build_dashboard_data()
    assert data["readiness"] is None
    assert "garmin down" in data["readiness_err"]
    assert "garmin down" in data["training_status_err"]
    assert "garmin down" in data["trends_err"]
    assert data["sleep"] == {"sleep_score": 80}   # unaffected section still populated
    # The whole thing still renders without raising.
    assert dashboard.render_dashboard_html(data).startswith("<!doctype html>")
