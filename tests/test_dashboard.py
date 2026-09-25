# tests/test_dashboard.py
"""Tests for the server-rendered health dashboard (tools/dashboard.py).

These are offline unit tests: `render_dashboard_html` is a pure function of a
data dict, and `build_dashboard_data` is exercised with the underlying tool
functions monkeypatched, so no live Garmin session is needed.
"""
import re

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
    dashboard._clear_section_cache()


def _trend_series(values, unit="bpm"):
    return {"unit": unit, "daily": [{"date": f"2026-07-{i+1:02d}", "value": v}
                                     for i, v in enumerate(values)]}


def _status_history(statuses, end="2026-07-17"):
    """Build a daily training-status history (oldest first) of len(statuses)
    days ending on `end`, matching dashboard._daily_training_status_history's
    shape."""
    from datetime import date, timedelta
    end_d = date.fromisoformat(end)
    n = len(statuses)
    return [
        {"date": (end_d - timedelta(days=n - 1 - i)).isoformat(), "status": s}
        for i, s in enumerate(statuses)
    ]


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
                        "acwr_status": "OPTIMAL", "status": "PRODUCTIVE_2", "sport": "RUNNING",
                        "load_balance": "BALANCED"},
    "training_status_err": None,
    "training_status_daily_history": _status_history(
        ["MAINTAINING_1"] * 10 + ["STRAINED_0"] * 4 + ["PRODUCTIVE_1"] * 7 + [None] * 3 + ["PRODUCTIVE_2"] * 4
    ),
    "training_status_daily_history_err": None,
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
                 {"id": 1, "date": "2026-07-17T06:00:00", "name": "Evening Run", "type": "running",
                  "distance_km": 10.2, "duration_min": 52.3, "avg_hr": 141, "training_load": 120.0},
                 {"id": 2, "date": "2026-07-16T18:00:00", "name": "Pool Swim", "type": "lap_swimming",
                  "distance_km": 1.5, "duration_min": 45.0, "avg_hr": 130, "training_load": 60.0},
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
              "maintenance_interval_km": 400.0,
              "linked_gear_uuid": None, "last_serviced": "2026-01-01",
              "ever_serviced": False, "distance_since_km": 380.0,
              "component_usage_km": 380.0, "component_duration_min": 3000.0,
              "lifespan_km": 400.0, "status": "yellow", "status_emoji": "\U0001F7E1",
              "services": [{"id": "svc-1", "component_id": 1, "service_type": "Lube",
                            "service_interval_km": 400.0, "last_serviced": "2026-01-01",
                            "km_until_next_service": 20.0, "status": "yellow"}]},
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


# The active training plan as tools/plan_today.py shapes it — a session day
# whose FTP test was matched to a Garmin ride.
def _day(date_str, letter, state, color="#7fb87a", is_today=False):
    return {"date": date_str, "letter": letter, "is_today": is_today, "state": state, "color": color}


PLAN_CONTEXT = {
    "id": "kurt-winter", "title": "Winter Base Block — FTP Focus", "athlete": "Kurt", "total_weeks": 24,
    "week_number": 2, "phase": "Field Testing", "phase_color": "#7c8194", "in_plan": True,
    "today": [{
        "id": "w2-fri-bike", "date": "2026-07-17", "name": "FTP Field Test", "sport": "bike", "type": "test",
        "durationMinutes": 40, "distanceKm": 20, "distanceMeters": None, "primaryZone": "Test",
        "completed": True, "is_test": True,
        "activity": {"id": 900, "name": "Ottawa - FTP Test (20 min warmup)", "type": "road_biking",
                     "distance_km": 26.16, "duration_min": 52, "date": "2026-07-17"},
    }],
    "tomorrow": [{
        "id": "w2-sat-bike", "date": "2026-07-18", "name": "Plantagenet Ride w/ Cam", "sport": "bike",
        "type": "endurance", "durationMinutes": 285, "distanceKm": 115, "distanceMeters": None,
        "primaryZone": "Zone 1-2", "completed": False, "is_test": False, "activity": None,
    }],
    "week": {"done_hours": 7.8, "plan_hours": 13.3, "days": [
        _day("2026-07-13", "M", "done", "#5eb8c9"), _day("2026-07-14", "T", "done"),
        _day("2026-07-15", "W", "done"), _day("2026-07-16", "T", "done"),
        _day("2026-07-17", "F", "done", is_today=True), _day("2026-07-18", "S", "planned"),
        _day("2026-07-19", "S", "planned"),
    ]},
    "progress": {"done": 9, "total": 184},
    "ftp_test": {"workout_id": "w2-fri-bike", "date": "2026-07-17", "is_today": True, "activity": None,
                 "best_20min": 261, "estimate": 248, "current": 250, "applied": False},
    "thresholds": [
        {"key": "ftp", "label": "FTP", "garmin": "275 W", "plan": "250 W", "status": "Provisional", "sub": "test today"},
        {"key": "bikeLthr", "label": "Bike LTHR", "garmin": None, "plan": "164", "status": "Tested", "sub": "Set on Edge 1040"},
        {"key": "css", "label": "Swim CSS", "garmin": None, "plan": "2:20", "status": "Unvalidated", "sub": "/100m"},
    ],
    "zones": {
        "bike": [{"zone": "1", "name": "Recovery", "watts": "0–138", "hr": "0–133"},
                 {"zone": "5c", "name": "Anaerobic", "watts": "300+", "hr": "174+"}],
        "run": [{"zone": "1", "name": "Recovery", "hr": "0–138", "pace": "5:25–5:45"}],
    },
    "thresholds_now": {"ftp": 250, "bikeLthr": 164, "runLthr": 170, "thresholdPace": "4:15"},
}


def test_render_is_a_complete_document():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert html.startswith("<!doctype html>")
    assert html.endswith("</html>")


def test_render_includes_mobile_app_metadata():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert 'maximum-scale=1' in html
    assert 'apple-mobile-web-app-capable' in html
    assert 'rel="manifest"' in html


def test_render_includes_favicon_and_touch_icon():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert 'rel="icon"' in html
    assert 'rel="apple-touch-icon" href="/icons/apple-touch-icon.png"' in html


def test_render_uses_the_shared_site_nav():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert html.count('id="gm-nav"') == 1
    assert 'href="/training-plan?token=t0k" data-nav="plan"' in html


def test_nav_pill_is_today_plan_trends_activity_and_more():
    html = dashboard.render_dashboard_html(SAMPLE)
    nav = html.index('<nav id="gm-nav"')
    pill = html[nav:html.index('gm-nav__rail', nav)]

    assert 'for="tab-today"' in pill
    assert 'data-nav="plan"' in pill
    assert 'for="tab-trends"' in pill
    assert 'for="tab-activity"' in pill
    assert 'for="tab-you"' not in pill and 'for="tab-gear"' not in pill
    assert 'data-nav="more"' in html
    # The tab radios come before the nav, so its highlighting can follow them.
    assert html.index('id="tab-today"') < html.index('<nav id="gm-nav"')


def test_more_menu_lists_fitness_gear_reports_pdf_and_settings():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    sheet = html[html.index('class="gm-nav-more-sheet"'):html.index('id="chart-tooltip"')]

    assert 'for="tab-you"' in sheet and "Fitness" in sheet
    assert 'for="tab-gear"' in sheet and "Gear" in sheet
    assert 'href="/weekly-summary?token=t0k"' in sheet
    assert 'href="/training-plan/pdf?token=t0k"' in sheet
    assert 'href="/training-plan?view=settings&amp;token=t0k"' in sheet
    assert "/training-plan/plans" not in sheet   # the plan list lives under Settings


def test_desktop_rail_is_titled_with_the_athlete_from_the_plan():
    data = {**SAMPLE, "plan": {**PLAN_CONTEXT}}
    html = dashboard.render_dashboard_html(data)
    assert '<div class="gm-nav__title">Kurt</div>' in html


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


def test_render_component_modal_shows_services_log_flow():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert "Services" in html
    assert "Date/time" in html
    assert 'name="service_datetime"' in html
    assert 'type="hidden" name="action" value="Lube"' in html
    assert "Service<select" not in html
    assert 'href="#service-1-svc-1"' in html
    assert 'aria-label="Log service"' in html
    assert 'aria-label="Edit service"' in html
    assert 'aria-label="Add service"' in html
    assert '<svg viewBox="0 0 24 24" aria-hidden="true">' in html


def test_render_component_modal_shows_lifespan_remaining():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "KMs used" in html
    assert "KMs left" in html
    assert "Time used" not in html


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
                           "distance_km": 1969.68, "max_distance_km": 3000.0,
                           "date_begin": "2026-02-22"}],
    }}
    html = dashboard.render_dashboard_html(data)
    # The option's value carries "<uuid>:<name>:<date_begin>" so the POST
    # doesn't need a live Garmin lookup to resolve them (issue 63 follow-up).
    assert '<option value="chain-1:Shimano 12s Chain:2026-02-22">' in html
    assert "Shimano 12s Chain" in html


def test_render_gear_panel_link_form_option_tolerates_missing_date_begin():
    data = {**SAMPLE, "gear_status": {
        **SAMPLE["gear_status"],
        "linkable_gear": [{"uuid": "chain-1", "name": "Shimano 12s Chain", "model": None,
                           "distance_km": 1969.68, "max_distance_km": 3000.0,
                           "date_begin": None}],
    }}
    html = dashboard.render_dashboard_html(data)
    assert '<option value="chain-1:Shimano 12s Chain:">' in html


def test_render_gear_panel_link_form_has_no_install_date_field():
    """Install date is no longer a form field on the Link form (issue 63
    follow-up) — it's sourced from the linked Garmin gear's own date, or
    defaults to today for a Custom component, either way without asking."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "+ Link component" in html
    # The Edit-component form (inside each component's modal) still has one;
    # this only checks the Link form doesn't gain a second, redundant one.
    link_form_start = html.index("+ Link component")
    link_form_end = html.index("</details>", link_form_start)
    assert "Install date" not in html[link_form_start:link_form_end]


def test_render_gear_panel_link_form_offers_custom_name_and_type():
    """The Link form has a Name input for Custom components plus a Type
    dropdown for classification/defaults."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'name="name" placeholder="Custom component"' in html
    assert '<select name="component_type">' in html
    for option in ("Chain", "Cassette", "Tire", "Brakes"):
        assert f'<option value="{option}">{option}</option>' in html
    assert 'placeholder="e.g. Chain"' not in html


def test_render_gear_panel_uses_lifespan_and_service_interval_labels():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Lifespan (km)" in html
    assert "Interval (km)" in html
    assert "Interval override" not in html


def test_render_gear_panel_unlink_button_present_for_unlinked_component():
    """Unlink is available for any component and rendered as a minimal footer action."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Unlink" in html
    assert "background:transparent" in html


def test_render_gear_panel_unlink_button_present_for_linked_component():
    data = {**SAMPLE, "gear_status": {
        **SAMPLE["gear_status"],
        "gear": [
            {**SAMPLE["gear_status"]["gear"][0], "components": [
                {**SAMPLE["gear_status"]["gear"][0]["components"][0],
                 "linked_gear_uuid": "chain-1"},
            ]},
            SAMPLE["gear_status"]["gear"][1],
        ],
    }}
    html = dashboard.render_dashboard_html(data)
    assert "Unlink" in html
    assert '<input type="hidden" name="unlink" value="1">' in html


def test_render_gear_panel_hides_component_edit_for_linked_component():
    data = {**SAMPLE, "gear_status": {
        **SAMPLE["gear_status"],
        "gear": [
            {**SAMPLE["gear_status"]["gear"][0], "components": [
                {**SAMPLE["gear_status"]["gear"][0]["components"][0],
                 "linked_gear_uuid": "chain-1"},
            ]},
            SAMPLE["gear_status"]["gear"][1],
        ],
    }}
    html = dashboard.render_dashboard_html(data)
    assert "Edit component" not in html


def test_render_gear_panel_history_bike_column_not_gear():
    """The Maintenance History table's second column reads 'Bike', not the
    more ambiguous 'Gear' (issue 63 follow-up)."""
    data = {**SAMPLE, "gear_status": {
        **SAMPLE["gear_status"],
        "maintenance_log": [
            {"id": 1, "component_id": 1, "date": "2026-07-10", "action": "lubed",
             "distance_at_service_km": 5400.0, "notes": None,
             "component_name": "Chain", "bike_name": "Canyon Ultimate", "bike_uuid": "bike-1"},
        ],
    }}
    html = dashboard.render_dashboard_html(data)
    assert "<th>Bike</th>" in html
    assert "<th>Gear</th>" not in html


def test_render_includes_gear_modal_reset_script():
    """A component modal's 'Edit component' <details> is native, persistent
    DOM state (the modal itself is CSS :target-toggled, not re-rendered per
    open) — this script resets it closed on every modal navigation so it's
    always collapsed on a fresh open (issue 63 follow-up)."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "gear-modal .gear-actions[open]" in html


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


def test_readiness_card_mentions_hrv_status():
    html = dashboard.render_dashboard_html(SAMPLE)
    today = re.search(r'<section class="panel tabpanel tp-today".*?</section>', html, re.S).group(0)
    card = today.split(">Readiness<", 1)[1].split('class="t-card', 1)[0]
    assert "HRV balanced" in card


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


def test_training_status_is_the_first_in_focus_card_with_load_ratio():
    html = dashboard.render_dashboard_html(SAMPLE)
    today_section = re.search(r'<section class="panel tabpanel tp-today".*?</section>', html, re.S).group(0)
    focus = today_section[today_section.index('class="focus"'):]

    assert "Productive" in focus
    # readiness first, then In Focus: Training status (with Load ratio), Recovery
    assert today_section.index(">Readiness<") < today_section.index('class="focus"')
    assert focus.index('data-title="Training status"') < focus.index("Load ratio") < focus.index('data-title="Recovery"')


def test_training_status_widget_has_no_blurb_or_sport():
    html = dashboard.render_dashboard_html(SAMPLE)
    card = html.split("Training status</div>", 1)[1].split("Load ratio", 1)[0]

    assert "RUNNING" not in card
    assert "Running" not in card  # sport label removed — status covers all activities
    # no per-status one-line explanation text
    assert "recovery likely isn't keeping up" not in card.lower()
    assert "ideal competitive form" not in card.lower()


def test_trends_training_status_follows_the_range_picker():
    html = dashboard.render_dashboard_html(SAMPLE)
    trends = _section(html, "tp-trends")
    # No toggle of its own: one card per range set, each with that range's strip.
    assert 'name="ts-range"' not in html
    for r in (7, 14, 30):
        rs = trends.split(f'class="range-set rs-{r}"', 1)[1].split('class="range-set', 1)[0]
        assert rs.count('class="card ts-card"') == 1


def test_training_status_kicker_lives_inside_the_card_not_outside():
    html = dashboard.render_dashboard_html(SAMPLE)
    # the "Training status" label is the kicker inside .card.ts-card, not a
    # sibling .section-title sitting above/outside the card.
    assert '<div class="card ts-card"' in html
    before, after = html.split('<div class="card ts-card"', 1)
    card_and_after = '<div class="card ts-card"' + after
    assert 'Training status</div>' in card_and_after.split("Load ratio", 1)[0]
    # and it is *not* rendered as a section-title before the card opens
    assert 'section-title">Training status' not in html


def test_training_status_widget_shows_icon_and_load_focus():
    html = dashboard.render_dashboard_html(SAMPLE)
    card = html.split('<div class="card ts-card"', 1)[1].split("Load ratio", 1)[0]

    assert card.count("<svg") >= 2  # header kicker icon + coloured badge icon
    assert "Load Focus" in card
    assert "Balanced" in card


def test_training_status_widget_shows_range_captions():
    trends = _section(dashboard.render_dashboard_html(SAMPLE), "tp-trends")
    card_7 = trends.split('class="range-set rs-7"', 1)[1].split('class="card ts-card"', 1)[1]
    card_30 = trends.split('class="range-set rs-30"', 1)[1].split('class="card ts-card"', 1)[1]

    assert "Last 7d" in card_7 and "Since Jul 11" in card_7      # 7 days back from 2026-07-17
    assert "Last 1 month" in card_30 and "Since Jun 20" in card_30  # all 28 days on hand


def test_training_status_widget_renders_one_segment_per_day_of_the_range():
    trends = _section(dashboard.render_dashboard_html(SAMPLE), "tp-trends")

    def strip(r):
        rs = trends.split(f'class="range-set rs-{r}"', 1)[1]
        return rs.split('class="card ts-card"', 1)[1].split("Last ", 1)[0]

    assert strip(7).count('class="js-bar"') == 7
    assert strip(14).count('class="js-bar"') == 14
    assert strip(30).count('class="js-bar"') == 28   # only 28 days of history exist
    assert "Maintaining" in strip(30) and "Strained" in strip(30)


def test_trends_defaults_to_7d_and_remembers_the_last_range():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'id="range-7" checked' in html
    assert "localStorage.setItem(RANGE_KEY" in html


def test_trends_has_no_daily_steps_card():
    trends = _section(dashboard.render_dashboard_html(SAMPLE), "tp-trends")
    assert "Daily steps" not in trends


def test_in_focus_training_status_shows_the_last_seven_days():
    html = dashboard.render_dashboard_html(SAMPLE)
    focus = html.split('data-title="Training status"', 1)[1].split('data-title="Recovery"', 1)[0]
    strip = focus.split("Load ratio", 1)[0]
    assert strip.count('class="js-bar"') == 7
    assert ">Today<" in strip


@pytest.mark.parametrize("raw, expected_label", [
    ("PRODUCTIVE_2", "Productive"),
    ("productive", "Productive"),
    ("PEAKING_1", "Peaking"),
    ("MAINTAINING_0", "Maintaining"),
    ("STRAINED_1", "Strained"),
    ("UNPRODUCTIVE_2", "Unproductive"),
    ("OVERREACHING_1", "Overreaching"),
    ("RECOVERY_0", "Recovery"),
    ("DETRAINING_2", "Detraining"),
    ("NO_STATUS_0", "No Status"),
    ("PAUSED_0", "Paused"),
    (None, "No Status"),
    ("", "No Status"),
    ("something_unrecognized_9", "No Status"),
])
def test_training_status_info_maps_known_phrases(raw, expected_label):
    label, color = dashboard._training_status_info(raw)
    assert label == expected_label
    assert color


def test_training_status_colors_match_garmin_app_palette():
    colors = {label: color for label, color in dashboard._TRAINING_STATUS_INFO.values()}
    assert colors["Peaking"] == "#9184d9"       # purple
    assert colors["Productive"] == "#4fae72"    # green
    assert colors["Maintaining"] == "#d9c23e"   # yellow
    assert colors["Strained"] == "#d9689a"      # pink
    assert colors["Unproductive"] == "#e2734a"  # orange
    assert colors["Overreaching"] == "#cf5a4e"  # red
    assert colors["Recovery"] == "#4aa7d8"      # blue
    assert colors["Detraining"] == "#9397ab"    # gray
    # every status maps to a distinct colour
    assert len(set(colors.values())) == len(colors)


@pytest.mark.parametrize("raw, expected_icon", [
    ("PEAKING_1", dashboard._TREND_UP_ICON),
    ("PRODUCTIVE_2", dashboard._TREND_UP_ICON),
    ("MAINTAINING_0", dashboard._TREND_FLAT_ICON),
    ("RECOVERY_0", dashboard._TREND_FLAT_ICON),
    ("STRAINED_1", dashboard._TREND_DOWN_ICON),
    ("UNPRODUCTIVE_2", dashboard._TREND_DOWN_ICON),
    ("OVERREACHING_1", dashboard._TREND_DOWN_ICON),
    ("DETRAINING_2", dashboard._TREND_DOWN_ICON),
    ("PAUSED_0", dashboard._PAUSE_ICON),
    ("NO_STATUS_0", dashboard._HELP_ICON),
    (None, dashboard._HELP_ICON),
])
def test_training_status_icon_matches_category(raw, expected_icon):
    assert dashboard._training_status_icon(raw) == expected_icon


def test_daily_training_status_history_covers_28_days_oldest_first():
    from datetime import date
    rows = [
        {"metric_date": date(2026, 6, 25), "training_status_data": {"status": "MAINTAINING_1"}},
        {"metric_date": date(2026, 7, 2), "training_status_data": {"status": "PRODUCTIVE_1"}},
        {"metric_date": date(2026, 7, 9), "training_status_data": {"status": "STRAINED_0"}},
        {"metric_date": date(2026, 7, 17), "training_status_data": {"status": "PRODUCTIVE_2"}},
    ]
    history = dashboard._daily_training_status_history(rows, date(2026, 7, 17))
    assert len(history) == 28
    assert history[0]["date"] == "2026-06-20"
    assert history[-1]["date"] == "2026-07-17"
    assert history[-1]["status"] == "PRODUCTIVE_2"  # today
    assert history[0]["status"] is None             # no row that far back
    by_date = {h["date"]: h["status"] for h in history}
    assert by_date["2026-06-25"] == "MAINTAINING_1"
    assert by_date["2026-07-09"] == "STRAINED_0"


def test_daily_training_status_history_handles_missing_data():
    from datetime import date
    history = dashboard._daily_training_status_history([], date(2026, 7, 17))
    assert len(history) == 28
    assert all(h["status"] is None for h in history)


def test_load_ratio_card_lives_on_today_panel_not_trends():
    html = dashboard.render_dashboard_html(SAMPLE)
    today_section = re.search(r'<section class="panel tabpanel tp-today".*?</section>', html, re.S).group(0)
    trends_section = re.search(r'<section class="panel tabpanel tp-trends".*?</section>', html, re.S).group(0)

    assert "Load ratio" in today_section
    assert "1.30" in today_section
    assert "Load ratio" not in trends_section
    assert "Acute : chronic load" not in html


def test_render_shows_readiness_card():
    html = dashboard.render_dashboard_html(SAMPLE)
    card = html.split(">Readiness<", 1)[1][:400]
    assert "t-ring" in html and "t-sub" in card


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
    fitness = re.search(r'<section class="panel tabpanel tp-you".*?</section>', html, re.S).group(0)
    assert ">170<" in fitness   # LTHR
    assert ">265 W<" in fitness   # FTP


def test_render_vo2max_gauges_show_rating_and_sport_labels():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Running VO" in html
    assert "Cycling VO" in html
    assert "Excellent" in html  # 52 ml/kg/min running -> Excellent band
    assert "Good" in html       # 48 ml/kg/min cycling -> Good band


def test_fitness_without_a_plan_falls_back_to_garmin_hr_zones():
    html = dashboard.render_dashboard_html(SAMPLE)
    fitness = re.search(r'<section class="panel tabpanel tp-you".*?</section>', html, re.S).group(0)
    assert "Heart-rate zones" in fitness
    assert "Plan zones" not in fitness


def test_render_omits_ftp_toggle_without_weight():
    data = dict(SAMPLE)
    data["athlete"] = dict(SAMPLE["athlete"])
    del data["athlete"]["weight_kg"]
    html = dashboard.render_dashboard_html(data)
    assert 'id="ftp-w"' not in html
    assert "265" in html  # FTP still shown in watts


def test_render_shows_activity_list_opening_the_detail_modal():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert 'class="actcard actcard-click"' in html
    assert "Pool Swim" in html
    assert "openActivityModal(2)" in html
    assert 'id="activity-modal"' in html
    assert 'id="activity-modal-body"' in html


def test_render_activity_filters_include_all_requested_options():
    html = dashboard.render_dashboard_html(SAMPLE)

    for key in ("all", "triathlon", "bike", "run", "strength", "other"):
        assert f'id="activity-filter-{key}"' in html
        assert f'for="activity-filter-{key}"' in html
    assert html.index('for="activity-filter-strength"') < html.index('for="activity-filter-other"')
    assert "410" in html


def test_strength_filter_section_is_rendered_and_selectable():
    html = dashboard.render_dashboard_html(SAMPLE)

    assert "activity-filter-strength" in html
    assert "#activity-filter-strength:checked" in html


def test_activity_filter_classifies_sports():
    assert dashboard._activity_filter_key({"type": "multi_sport"}) == "triathlon"
    assert dashboard._activity_filter_key({"type": "road_biking"}) == "bike"
    assert dashboard._activity_filter_key({"type": "running"}) == "run"
    assert dashboard._activity_filter_key({"type": "lap_swimming"}) == "other"
    assert dashboard._activity_filter_key({"type": "strength_training"}) == "strength"


def test_activity_filter_classifies_all_run_types_as_run():
    for activity_type in ("running", "trail_running", "treadmill_running",
                          "track_running", "indoor_running"):
        assert dashboard._activity_filter_key({"type": activity_type}) == "run"
        assert dashboard._activity_filter_matches({"type": activity_type}, "run")
        assert dashboard._activity_filter_matches({"type": activity_type}, "triathlon")


def test_triathlon_filter_includes_all_endurance_sports_but_not_strength():
    for activity_type in ("multi_sport", "triathlon", "duathlon", "cycling",
                          "road_biking", "running", "lap_swimming"):
        assert dashboard._activity_filter_matches({"type": activity_type}, "triathlon")
    assert not dashboard._activity_filter_matches({"type": "strength_training"}, "triathlon")


def test_render_activity_list_is_limited_to_current_week():
    data = {**SAMPLE, "activities": SAMPLE["activities"] + [
        {"id": 3, "date": "2026-07-10T06:00:00", "name": "Older Run",
         "type": "running", "distance_km": 5, "duration_min": 30,
         "training_load": 50},
    ]}
    html = dashboard.render_dashboard_html(data)

    assert "Pool Swim" in html
    assert "Older Run" not in html
    assert "View More" in html


def test_format_week_range_formats_same_and_cross_month():
    assert dashboard._format_week_range("2026-07-13", "2026-07-19") == "Jul 13–19, 2026"
    assert dashboard._format_week_range("2026-07-27", "2026-08-02") == "Jul 27 – Aug 2, 2026"
    assert dashboard._format_week_range("2025-12-29", "2026-01-04") == "Dec 29, 2025 – Jan 4, 2026"
    assert dashboard._format_week_range("", "") == ""


def test_render_shows_activity_week_date_range_and_nav_arrows():
    html = dashboard.render_dashboard_html(SAMPLE)

    assert "Jul 13–17, 2026" in html
    # Current week (no activity_week_offset set -> defaults to 0): the next
    # (right) arrow is disabled — no navigating into a future, dataless week.
    assert 'href="/dashboard?tab=activity&amp;week=1"' in html
    assert '<span aria-hidden="true"' in html
    assert "week=-1" not in html


def test_render_activity_week_nav_enables_next_arrow_when_browsing_a_past_week():
    data = {**SAMPLE, "activity_week": SAMPLE["week"], "activity_week_offset": 2}
    html = dashboard.render_dashboard_html(data, token="secret")

    assert 'href="/dashboard?tab=activity&amp;week=3&amp;token=secret"' in html
    assert 'href="/dashboard?tab=activity&amp;week=1&amp;token=secret"' in html
    assert "week=-1" not in html


def test_render_shows_this_week_quick_jump_only_when_browsing_a_past_week():
    on_current_week = dashboard.render_dashboard_html(SAMPLE)
    assert ">This week<" not in on_current_week

    past_week_data = {**SAMPLE, "activity_week": SAMPLE["week"], "activity_week_offset": 5}
    on_past_week = dashboard.render_dashboard_html(past_week_data, token="secret")
    assert ">This week<" in on_past_week
    assert 'href="/dashboard?tab=activity&amp;week=0&amp;token=secret"' in on_past_week


def test_build_dashboard_data_fetches_the_requested_week_for_the_activity_tab(monkeypatch):
    from tools import health, activities, trends, performance, profile, challenges

    monkeypatch.setattr(health, "get_daily_readiness", lambda d: {})
    monkeypatch.setattr(health, "get_daily_health", lambda d: {})
    monkeypatch.setattr(health, "get_sleep", lambda d: {})
    monkeypatch.setattr(health, "get_training_readiness", lambda d: {})
    monkeypatch.setattr(health, "get_training_status", lambda d: {})
    monkeypatch.setattr(activities, "get_activities", lambda limit=20: [])

    def fake_weekly_summary(week_offset=0):
        return {"week_offset": week_offset, "total_activities": 0}

    monkeypatch.setattr(activities, "get_weekly_summary", fake_weekly_summary)
    monkeypatch.setattr(trends, "get_trends", lambda period, metrics: {"period": period, "days": 0, "metrics": {}})
    monkeypatch.setattr(performance, "get_personal_records", lambda: {})
    monkeypatch.setattr(profile, "get_athlete_profile", lambda: {})
    monkeypatch.setattr(challenges, "get_active_goals", lambda: [])
    monkeypatch.setattr(dashboard, "_fetch_last_sync", lambda: {})

    data = dashboard.build_dashboard_data(week_offset=3)
    # The Today tab's own widget always reflects the current week...
    assert data["week"]["week_offset"] == 0
    # ...while the Activity tab reflects whichever week was requested.
    assert data["activity_week"]["week_offset"] == 3
    assert data["activity_week_offset"] == 3
    assert data["activity_week_err"] is None


def _patch_dashboard_sections_except_week(monkeypatch, weekly_summary_fn):
    """Stub out every build_dashboard_data() section except get_weekly_summary
    with something instant, so tests can isolate the weekly-summary fetch's
    timing/call-count without every other section's own (mocked) latency."""
    from tools import health, activities, trends, performance, profile, challenges

    monkeypatch.setattr(health, "get_daily_readiness", lambda d: {})
    monkeypatch.setattr(health, "get_daily_health", lambda d: {})
    monkeypatch.setattr(health, "get_sleep", lambda d: {})
    monkeypatch.setattr(health, "get_training_readiness", lambda d: {})
    monkeypatch.setattr(health, "get_training_status", lambda d: {})
    monkeypatch.setattr(activities, "get_activities", lambda limit=20: [])
    monkeypatch.setattr(activities, "get_weekly_summary", weekly_summary_fn)
    monkeypatch.setattr(trends, "get_trends", lambda period, metrics: {"period": period, "days": 0, "metrics": {}})
    monkeypatch.setattr(performance, "get_personal_records", lambda: {})
    monkeypatch.setattr(profile, "get_athlete_profile", lambda: {})
    monkeypatch.setattr(challenges, "get_active_goals", lambda: [])
    monkeypatch.setattr(dashboard, "_fetch_last_sync", lambda: {})


def test_build_dashboard_data_fetches_current_and_requested_week_concurrently(monkeypatch):
    """Issue 85 follow-up: navigating to a past week used to bolt an extra
    get_weekly_summary() call on *after* the whole page's usual parallel
    fetch, doubling the wait. It must now run alongside the current week's
    fetch instead of stacked after it.

    Asserted structurally (both calls were in flight at the same time via a
    shared threading.Event) rather than by a wall-clock threshold — a timing
    assertion here was flaky under CI load: a busy/throttled runner can push
    even genuinely-concurrent threads' wall time well past a tight budget,
    which isn't the thing this test is meant to catch.
    """
    import threading

    started = set()
    both_started = threading.Event()
    lock = threading.Lock()

    def slow_weekly_summary(week_offset=0):
        with lock:
            started.add(week_offset)
            if len(started) == 2:
                both_started.set()
        # Only returns quickly if the *other* call has also started — a
        # serial implementation would have this call return (and the whole
        # fetch batch move on) before the second call is even dispatched,
        # so `both_started` would still be unset when the timeout hits.
        both_started.wait(timeout=2)
        return {"week_offset": week_offset, "both_started": both_started.is_set()}

    _patch_dashboard_sections_except_week(monkeypatch, slow_weekly_summary)

    data = dashboard.build_dashboard_data(week_offset=7)

    assert data["week"]["both_started"] is True
    assert data["activity_week"]["both_started"] is True


def test_build_dashboard_data_caches_a_past_week_across_requests(monkeypatch):
    """A past week's activities don't change, so browsing back to a week
    already viewed this session should hit no live Garmin call at all."""
    calls = []

    def counting_weekly_summary(week_offset=0):
        calls.append(week_offset)
        return {"week_offset": week_offset}

    _patch_dashboard_sections_except_week(monkeypatch, counting_weekly_summary)

    dashboard.build_dashboard_data(week_offset=4)
    calls_after_first = list(calls)
    dashboard.build_dashboard_data(week_offset=4)

    # First request: one call for the current week (offset 0) and one for
    # the requested week (offset 4). Second request for the same offset 4
    # should be served entirely from cache — no new calls at all.
    assert sorted(calls_after_first) == [0, 4]
    assert calls == calls_after_first


def test_build_dashboard_data_from_db_never_calls_garmin_live_for_weeks_or_sync(monkeypatch):
    """When Postgres is configured, the whole page — including the Activity
    tab's current and navigated weeks, and the "last sync" line — must come
    from the DB. Only the gear list has no DB-backed source yet and stays
    live (see the comment in _build_dashboard_data_from_db)."""
    import db
    from datetime import datetime, timezone
    from tools import activities as activities_mod
    from tools import gear_tracker

    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_today_metrics", lambda d: {
        "readiness_data": {}, "health_data": {}, "sleep_data": {},
        "training_data": {}, "training_status_data": {},
    })
    monkeypatch.setattr(db, "get_trend_metrics", lambda s, e: [])
    monkeypatch.setattr(db, "get_recent_activities", lambda limit=20: [])

    week_calls = []

    def fake_get_weekly_activities(week_start, week_end):
        week_calls.append((week_start, week_end))
        return [{"summary": {"id": 1, "date": f"{week_start}T06:00:00", "name": "Run",
                             "type": "running", "distance_km": 5.0, "duration_min": 30.0,
                             "avg_hr": 140, "training_load": 40.0}}]

    monkeypatch.setattr(db, "get_weekly_activities", fake_get_weekly_activities)
    monkeypatch.setattr(db, "get_personal_records_from_db", lambda: {})
    monkeypatch.setattr(db, "get_athlete_profile_from_db", lambda: {})
    monkeypatch.setattr(db, "get_active_goals_from_db", lambda: [])

    synced_at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(db, "get_sync_state", lambda: {
        "activities": {"data_type": "activities", "last_sync_time": synced_at},
    })

    def boom(*a, **k):
        raise AssertionError("DB path must never call Garmin live")

    monkeypatch.setattr(activities_mod, "get_weekly_summary", boom)
    monkeypatch.setattr(dashboard, "_fetch_last_sync", boom)
    monkeypatch.setattr(gear_tracker, "build_gear_status", lambda: {"gear": []})

    data = dashboard.build_dashboard_data(week_offset=3)

    # One DB query for the current week, one for the requested week — never
    # a live Garmin call for either.
    assert len(week_calls) == 2
    assert data["week"]["total_activities"] == 1
    assert data["activity_week"]["total_activities"] == 1
    assert data["activity_week_offset"] == 3
    assert data["activity_week_err"] is None
    assert data["last_sync"]["upload_time"] == int(synced_at.timestamp() * 1000)
    assert data["gear_status"] == {"gear": []}

    # Navigating back to the same past week should hit no DB query at all
    # for it (the current-week query still runs — it's never cached, since
    # it's still accumulating today's activities).
    dashboard.build_dashboard_data(week_offset=3)
    assert len(week_calls) == 3  # +1 for "week" only, none for "activity_week"


def test_build_dashboard_data_from_db_derives_training_status_history_from_trend_rows(monkeypatch):
    """The DB path's training-status history bar reuses the trend_rows window
    already fetched for the Trends tab rather than issuing a second query."""
    import db
    from datetime import date, datetime, timezone
    from tools import gear_tracker

    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_today_metrics", lambda d: {
        "readiness_data": {}, "health_data": {}, "sleep_data": {},
        "training_data": {}, "training_status_data": {"status": "PRODUCTIVE_2"},
    })
    monkeypatch.setattr(dashboard, "_local_now", lambda: datetime(2026, 7, 17, tzinfo=timezone.utc))
    monkeypatch.setattr(db, "get_trend_metrics", lambda s, e: [
        {"metric_date": date(2026, 7, 10), "training_status_data": {"status": "STRAINED_0"}},
        {"metric_date": date(2026, 7, 17), "training_status_data": {"status": "PRODUCTIVE_2"}},
    ])
    monkeypatch.setattr(db, "get_recent_activities", lambda limit=20: [])
    monkeypatch.setattr(db, "get_weekly_activities", lambda ws, we: [])
    monkeypatch.setattr(db, "get_personal_records_from_db", lambda: {})
    monkeypatch.setattr(db, "get_athlete_profile_from_db", lambda: {})
    monkeypatch.setattr(db, "get_active_goals_from_db", lambda: [])
    monkeypatch.setattr(db, "get_sync_state", lambda: {})
    monkeypatch.setattr(gear_tracker, "build_gear_status", lambda: {"gear": []})

    data = dashboard.build_dashboard_data()

    assert data["training_status_daily_history_err"] is None
    history = data["training_status_daily_history"]
    assert len(history) == 90          # the longest Trends range
    assert history[-1]["date"] == "2026-07-17"
    assert history[-1]["status"] == "PRODUCTIVE_2"  # today
    assert history[-8]["status"] == "STRAINED_0"     # 2026-07-10, 7 days back
    assert history[0]["status"] is None              # nothing that far back


def test_render_compact_mobile_metric_layouts():
    html = dashboard.render_dashboard_html(SAMPLE)

    assert 'grid-template-columns:repeat(2,minmax(0,1fr))' in html
    assert 'grid-template-columns:repeat(3,minmax(0,1fr))' in html
    assert "Avg load/session" in html


def test_render_includes_longer_trend_ranges_when_data_is_available():
    data = {**SAMPLE, "trends": {**SAMPLE["trends"], "days": 90}}
    html = dashboard.render_dashboard_html(data)

    assert 'id="range-42"' in html
    assert 'id="range-90"' in html
    assert ">3 months<" in html


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
    malicious_activity = {"id": 1, "date": "2026-07-16T18:00:00",
                          "name": "<script>alert(1)</script>", "type": "running",
                          "distance_km": 1.0, "duration_min": 5.0, "avg_hr": 100,
                          "training_load": 1.0}
    data["activities"] = [malicious_activity]
    data["week"] = {**SAMPLE["week"], "activities": [malicious_activity]}
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


def test_footer_links_are_removed_from_dashboard():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "Latest weekly report" not in html
    assert "View training plan" not in html


def test_periodic_refresh_defers_while_activity_modal_is_open():
    """A plain <meta http-equiv="refresh"> would reload unconditionally,
    closing the activity-detail modal out from under whoever's reading it
    (issue 74 feedback) — the refresh is JS-driven instead so it can check
    first."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert '<meta http-equiv="refresh"' not in html
    assert "classList.contains('open')" in html
    assert "location.reload()" in html


def test_no_external_requests_on_load():
    """Tabs/range/PR-filter switching stays pure CSS (radio-driven visibility)
    and the page itself never eagerly fetches anything external on load
    (fonts/icons are embedded, and every <script> is inline) — Leaflet
    (issue 74's route map) is the one exception, and even that is only
    fetched lazily from JS when an activity with a GPS route is opened,
    never as a static tag the page loads up front."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "<script src=" not in html
    assert "http://" not in html
    external_urls = re.findall(r"https://\S+", html)
    assert external_urls and all(
        "unpkg.com/leaflet" in u or "cartocdn.com" in u for u in external_urls
    )


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
    recovery = html.split('data-title="Recovery"', 1)[1]
    assert "Stress today" in recovery
    assert ">Rest<" in recovery and ">Med<" in recovery and ">High<" in recovery


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
    history = data["training_status_daily_history"]
    assert len(history) == 28
    assert all(h["status"] is None for h in history)  # fixture has no 'status' key
    assert len({h["date"] for h in history}) == 28     # 28 distinct daily snapshots
    assert data["trends"]["period"] == dashboard.TREND_PERIOD
    assert data["personal_records"] == {"running": []}
    assert data["athlete"]["weight_kg"] == 70
    assert data["last_sync"]["device_name"] == "Watch"
    assert all(data[k] is None for k in
               ("readiness_err", "health_err", "sleep_err", "training_err",
                "training_status_err", "training_status_daily_history_err", "activities_err", "week_err",
                "trends_err", "personal_records_err", "active_goals_err", "athlete_err", "last_sync_err"))


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
    # per-day fetches inside get_training_status_daily_history() swallow their own
    # errors (one bad day shouldn't blank the whole 28-day bar), so this section
    # comes back as an all-None history rather than a section-level error.
    assert data["training_status_daily_history_err"] is None
    assert all(h["status"] is None for h in data["training_status_daily_history"])
    assert "garmin down" in data["trends_err"]
    assert data["sleep"] == {"sleep_score": 80}   # unaffected section still populated
    # The whole thing still renders without raising.
    assert dashboard.render_dashboard_html(data).startswith("<!doctype html>")


# ── mobile: streamed first paint, in-place week navigation, kept state ──────

def test_full_render_has_no_loading_skeleton_but_ends_with_the_complete_marker():
    """The skeleton is only for the streamed response; the service worker
    only caches a page that ends with the marker (sw.js)."""
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert 'id="dash-boot"' not in html
    assert html.endswith(dashboard.DASHBOARD_COMPLETE_MARKER + "</body></html>")


def test_render_no_longer_reloads_the_page_on_a_timer():
    """Refreshing happens on coming back to a stale page (_APP_JS), not on a
    blind timer that could kick you back to the Today tab."""
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "setTimeout(tick" not in html
    assert f'"staleAfterMs": {dashboard.REFRESH_SECONDS * 1000}' in html


def test_render_opens_on_the_requested_activity_filter():
    html = dashboard.render_dashboard_html(SAMPLE, initial_tab="activity", activity_filter="bike")
    assert 'id="activity-filter-bike" checked' in html
    assert 'id="activity-filter-all" checked' not in html


def test_render_ignores_an_unknown_activity_filter():
    html = dashboard.render_dashboard_html(SAMPLE, activity_filter="<script>")
    assert 'id="activity-filter-all" checked' in html
    assert "<script>\"" not in html


def test_activity_panel_marks_its_week_and_nav_links_for_in_place_navigation():
    data = {**SAMPLE, "activity_week": SAMPLE["week"], "activity_week_offset": 2}
    panel = dashboard.render_activity_panel(data, token="t0k")
    assert panel.lstrip().startswith('<section class="panel tabpanel tp-activity" data-week="2"')
    assert 'data-week="3" aria-label="Previous week"' in panel
    assert 'data-week="1" aria-label="Next week"' in panel
    assert 'data-week="0"' in panel          # the "This week" jump


def test_activity_panel_error_still_carries_its_week():
    panel = dashboard.render_activity_panel({"activity_week": None, "activity_week_err": "boom",
                                             "activity_week_offset": 4})
    assert 'data-week="4"' in panel and "boom" in panel


def _collect(agen):
    import asyncio

    async def run():
        return [chunk async for chunk in agen]
    return asyncio.run(run())


def test_stream_dashboard_sends_the_skeleton_before_fetching_data(monkeypatch):
    fetched = []

    def fake_data(week_offset):
        fetched.append(week_offset)
        return SAMPLE

    monkeypatch.setattr(dashboard, "get_dashboard_data", fake_data)
    chunks = _collect(dashboard.stream_dashboard(2, "t0k", "activity", None, "run"))
    assert len(chunks) == 2
    assert chunks[0].startswith("<!doctype html>") and 'id="dash-boot"' in chunks[0]
    assert "<section class=\"panel tabpanel" not in chunks[0]
    assert chunks[1].startswith("<style>#dash-boot{display:none}</style>")
    assert 'id="activity-filter-run" checked' in chunks[1]
    assert chunks[1].endswith(dashboard.DASHBOARD_COMPLETE_MARKER + "</body></html>")
    assert fetched == [2]


def test_stream_dashboard_shows_a_failure_in_the_page_and_is_never_cacheable(monkeypatch):
    def boom(week_offset):
        raise RuntimeError("db down")

    monkeypatch.setattr(dashboard, "get_dashboard_data", boom)
    chunks = _collect(dashboard.stream_dashboard())
    assert "Dashboard error: db down" in chunks[-1]
    assert dashboard.DASHBOARD_COMPLETE_MARKER not in "".join(chunks)


def test_get_activity_week_data_reads_the_db_and_caches_past_weeks(monkeypatch):
    import db
    from tools import activities as activities_mod

    calls = []
    monkeypatch.setattr(db, "is_configured", lambda: True)

    def from_db(week_offset=0):
        calls.append(week_offset)
        return {"week_start": "2026-07-06", "total_activities": 2, "activities": []}

    def boom(*a, **k):
        raise AssertionError("DB path must never call Garmin live")

    monkeypatch.setattr(activities_mod, "get_weekly_summary_from_db", from_db)
    monkeypatch.setattr(activities_mod, "get_weekly_summary", boom)

    data = dashboard.get_activity_week_data(3)
    assert data == {"activity_week": from_db(), "activity_week_err": None, "activity_week_offset": 3}
    calls.clear()
    dashboard.get_activity_week_data(3)      # a closed week: served from cache
    assert calls == []
    dashboard.get_activity_week_data(0)      # the current week: always fresh
    dashboard.get_activity_week_data(0)
    assert calls == [0, 0]


def test_get_activity_week_data_falls_back_to_garmin_without_a_db(monkeypatch):
    import db
    from tools import activities as activities_mod

    monkeypatch.setattr(db, "is_configured", lambda: False)
    monkeypatch.setattr(activities_mod, "get_weekly_summary",
                        lambda week_offset=0: {"week_start": "2026-06-29", "offset": week_offset})
    data = dashboard.get_activity_week_data(-5)   # clamped to the current week
    assert data["activity_week"] == {"week_start": "2026-06-29", "offset": 0}
    assert data["activity_week_offset"] == 0


# ── unified Today / Fitness (the training plan joined with Garmin) ──────────

def _with_plan(**changes):
    import copy
    plan = copy.deepcopy(PLAN_CONTEXT)
    plan.update(changes)
    return {**SAMPLE, "plan": plan}


def _section(html, cls):
    return re.search(rf'<section class="panel tabpanel {cls}".*?</section>', html, re.S).group(0)


def test_today_leads_with_readiness_session_tomorrow_and_this_week():
    today = _section(dashboard.render_dashboard_html(_with_plan()), "tp-today")

    order = [today.index(">Readiness<"), today.index("Today&rsquo;s session"),
             today.index(">Tomorrow<"), today.index(">This week<"), today.index('class="focus"')]
    assert order == sorted(order)
    assert "Plantagenet Ride w/ Cam" in today and "4h 45m · 115 km · Zone 1-2" in today
    assert "7.8 of 13.3 h" in today and "width:59%" in today


def test_session_card_joins_the_planned_workout_with_its_garmin_activity():
    today = _section(dashboard.render_dashboard_html(_with_plan()), "tp-today")
    card = today.split("Today&rsquo;s session", 1)[1].split(">Tomorrow<", 1)[0]

    assert "FTP Field Test" in card and ">Done" in card and "Planned 40 min · Test · 20 km" in card
    assert "Ottawa - FTP Test (20 min warmup)" in card
    assert "26.16 km · 52 min · from Garmin" in card
    assert "openActivityModal(900)" in card
    assert "data-ftp-open" in card and "Update FTP from test" in card


def test_session_card_says_so_once_the_test_ftp_is_applied():
    plan = _with_plan()
    plan["plan"]["ftp_test"]["applied"] = True
    plan["plan"]["ftp_test"]["current"] = 248
    html = dashboard.render_dashboard_html(plan)
    assert "Plan FTP set to 248 W from this test" in html
    assert 'id="ftp-dialog"' not in html and "data-ftp-open>" not in html


def test_rest_day_shows_rest_card_and_opens_in_focus_on_recovery():
    today = _section(dashboard.render_dashboard_html(_with_plan(today=[])), "tp-today")
    assert "Rest day" in today and "Nothing planned." in today
    assert 'data-focus-start="1"' in today
    assert 'class="focus-dot on" data-focus-dot="1"' in today


def test_session_day_opens_in_focus_on_training_status():
    today = _section(dashboard.render_dashboard_html(_with_plan()), "tp-today")
    assert 'data-focus-start="0"' in today


def test_today_without_a_plan_offers_an_upload():
    today = _section(dashboard.render_dashboard_html({**SAMPLE, "plan": None}, token="t0k"), "tp-today")
    assert "No active plan" in today and "/training-plan/upload?token=t0k" in today
    assert "Today&rsquo;s session" not in today


def test_topbar_carries_the_plan_week_and_phase():
    html = dashboard.render_dashboard_html(_with_plan(), token="t0k")
    pill = html.split('class="week-pill"', 1)[1].split("</a>", 1)[0]
    assert "Wk 2 · Field Testing" in pill and 'href="/training-plan?token=t0k"' in pill


def test_fitness_compares_garmin_and_plan_thresholds_with_tags():
    fitness = _section(dashboard.render_dashboard_html(_with_plan()), "tp-you")
    ftp_row = fitness.split(">FTP<", 1)[1].split('class="f-row"', 1)[0]

    assert ">275 W<" in ftp_row and ">250 W<" in ftp_row and ">Provisional<" in ftp_row
    assert "test today" in ftp_row
    assert ">Tested<" in fitness and ">Unvalidated<" in fitness


def test_fitness_shows_plan_zones_with_bike_first():
    fitness = _section(dashboard.render_dashboard_html(_with_plan()), "tp-you")
    assert "Plan zones" in fitness and "Heart-rate zones" not in fitness
    assert 'id="fz-bike" checked' in fitness
    assert "FTP 250 W · LTHR 164 bpm" in fitness
    assert ">300+<" in fitness and ">174+<" in fitness
    assert 'label for="fz-swim"' not in fitness   # no swim zones in this plan


def test_fitness_offers_to_review_a_recent_ftp_test():
    fitness = _section(dashboard.render_dashboard_html(_with_plan()), "tp-you")
    assert "FTP test done today" in fitness
    assert "Best 20-min 261 W &rarr; FTP &asymp; 248 W" in fitness


def test_ftp_dialog_posts_to_the_plan_operations_api():
    html = dashboard.render_dashboard_html(_with_plan(), token="t0k")
    dialog = html.split('id="ftp-dialog"', 1)[1].split("</div>\n  </div>", 1)[0]
    assert 'data-endpoint="/training-plan/api/operations?token=t0k"' in dialog
    assert 'value="248"' in dialog and "261 W" in dialog and "250 W now" in dialog


def test_personal_records_fold_into_one_row():
    fitness = _section(dashboard.render_dashboard_html(SAMPLE), "tp-you")
    assert '<details class="f-prs">' in fitness and "Personal records" in fitness


def test_fitness_topbar_swaps_in_with_its_tab():
    html = dashboard.render_dashboard_html(_with_plan())
    assert 'class="topbar-fitness topbar-inner"' in html
    assert "Garmin + Winter Base Block — FTP Focus" in html
    assert "#tab-you:checked ~ .topbar .topbar-fitness { display:flex; }" in html


def test_tomorrow_card_opens_that_workout_in_the_plan():
    today = _section(dashboard.render_dashboard_html(_with_plan(), token="t0k"), "tp-today")
    card = today.split(">Tomorrow<", 1)[0].rsplit("<a ", 1)[1]
    assert 'href="/training-plan?workout=w2-sat-bike&amp;token=t0k"' in card


def test_rest_tomorrow_is_not_a_link():
    today = _section(dashboard.render_dashboard_html(_with_plan(tomorrow=[])), "tp-today")
    card = today.split(">Tomorrow<", 1)[0].rsplit('class="t-card', 1)[1]
    assert "t-link" not in card


def test_activity_parameter_opens_the_activity_detail():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert "q.get('activity')" in html and "returnOnClose = q.get('from') === 'plan'" in html
