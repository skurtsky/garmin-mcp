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


def test_render_is_a_complete_document():
    html = dashboard.render_dashboard_html(SAMPLE)
    assert html.startswith("<!doctype html>")
    assert html.endswith("</html>")


def test_render_includes_mobile_app_metadata():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert 'maximum-scale=1' in html
    assert 'apple-mobile-web-app-capable' in html
    assert 'rel="manifest"' in html


def test_render_omits_the_shared_navbar_now_that_more_menu_replaces_it():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert 'id="gm-nav"' not in html
    assert 'href="/training-plan?token=t0k"' in html  # still reachable, via the More menu


def test_botnav_shows_today_trends_activity_and_more_only():
    html = dashboard.render_dashboard_html(SAMPLE)
    botnav = html[html.index('<div class="botnav"'):html.index('class="more-menu-backdrop"')]

    assert 'for="tab-today"' in botnav
    assert 'for="tab-trends"' in botnav
    assert 'for="tab-activity"' in botnav
    assert 'for="more-menu"' in botnav
    assert 'for="tab-you"' not in botnav
    assert 'for="tab-gear"' not in botnav


def test_more_menu_lists_gear_fitness_weekly_summary_and_training_plan():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    sheet = html[html.index('more-menu-sheet'):html.index('id="chart-tooltip"')]

    assert 'for="tab-gear"' in sheet and "Gear" in sheet
    assert 'for="tab-you"' in sheet and "Fitness" in sheet
    assert 'href="/weekly-summary?token=t0k"' in sheet
    assert 'href="/training-plan?token=t0k"' in sheet


def test_more_menu_has_a_gap_between_the_tab_group_and_the_page_links():
    html = dashboard.render_dashboard_html(SAMPLE, token="t0k")
    assert 'class="more-menu-item more-menu-group-start"' in html
    assert 'href="/weekly-summary?token=t0k"' in html.split('class="more-menu-item more-menu-group-start"')[1]


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


def test_render_hrv_status_uses_weekly_average():
    html = dashboard.render_dashboard_html(SAMPLE)
    hrv_card = html.split('<div class="kicker">HRV status</div>', 1)[1].split(
        '<div class="card"', 1
    )[0]
    assert '>45<' in hrv_card
    assert '>42<' not in hrv_card


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


def test_load_ratio_card_lives_on_today_panel_not_trends():
    html = dashboard.render_dashboard_html(SAMPLE)
    today_section = re.search(r'<section class="panel tabpanel tp-today".*?</section>', html, re.S).group(0)
    trends_section = re.search(r'<section class="panel tabpanel tp-trends".*?</section>', html, re.S).group(0)

    assert "Load ratio" in today_section
    assert "1.30" in today_section
    assert "Load ratio" not in trends_section
    assert "Acute : chronic load" not in html


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


def test_render_compact_mobile_metric_layouts():
    html = dashboard.render_dashboard_html(SAMPLE)

    assert 'grid-template-columns:repeat(4,minmax(0,1fr))' in html
    assert 'grid-template-columns:repeat(2,minmax(0,1fr))' in html
    assert 'grid-template-columns:repeat(3,minmax(0,1fr))' in html
    assert "Avg load/session" in html


def test_render_includes_longer_trend_ranges_when_data_is_available():
    data = {**SAMPLE, "trends": {**SAMPLE["trends"], "days": 90}}
    html = dashboard.render_dashboard_html(data)

    assert 'id="range-42"' in html
    assert 'id="range-90"' in html
    assert ">3 months<" in html


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
