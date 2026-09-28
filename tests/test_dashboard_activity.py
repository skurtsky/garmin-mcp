# tests/test_dashboard_activity.py
"""The redesigned Activity tab (tools/dashboard_activity.py). Offline — the
week, its plan sessions and commute flags are built here."""
import re
from datetime import date

from tools import dashboard_activity as da

TODAY = date(2026, 9, 25)          # a Friday
WEEK_START = "2026-09-21"


def _a(i, day, typ, name="A", km=0.0, mins=60, load=50, **kw):
    return {"id": i, "date": f"{day} 08:{i % 60:02d}:00", "type": typ, "name": name, "distance_km": km,
            "duration_min": mins, "training_load": load, **kw}


WEEK = {"week_start": WEEK_START, "week_end": "2026-09-25", "activities": [
    _a(1, "2026-09-21", "road_biking", "Ottawa Road Cycling", 73.06, 176, 180, avg_power=171, avg_hr=138,
       training_effect_label="AEROBIC_BASE"),
    _a(2, "2026-09-22", "lap_swimming", "Lap Swimming", 2.0, 48, 45, training_effect_label="AEROBIC_BASE"),
    _a(3, "2026-09-22", "volleyball", "Volleyball", 0, 103, 70, avg_hr=131, training_effect_label="ANAEROBIC_CAPACITY"),
    _a(4, "2026-09-23", "road_biking", "Ottawa Cycling", 17.86, 63, 30, avg_power=148),
    _a(5, "2026-09-23", "road_biking", "Ottawa Cycling", 19.37, 59, 32, avg_power=152),
    _a(6, "2026-09-23", "bouldering", "Bouldering", 0, 55, 35, avg_hr=118),
    _a(7, "2026-09-25", "road_biking", "Ottawa - FTP Test", 26.16, 52, 110, avg_power=248,
       training_effect_label="LACTATE_THRESHOLD"),
]}
PREV = {"week_start": "2026-09-14", "activities": [
    _a(21, "2026-09-15", "lap_swimming", "Lap Swimming", 2.2, 50, 48),
    _a(22, "2026-09-19", "road_biking", "Ride", 62.4, 160, 165),
]}


def _session(wid, day, sport, name, status, mins=60, load=60, activity_id=None, zone="Zone 2"):
    return {"workout_id": wid, "date": day, "sport": sport, "name": name, "status": status,
            "duration_min": mins, "est_load": load, "activity_id": activity_id, "primary_zone": zone,
            "benefit": "AEROBIC_BASE", "is_test": False}


EXTRAS = {
    "planned": [
        _session("w1", "2026-09-21", "bike", "Endurance ride", "done", 180, 170, activity_id=1),
        _session("w2", "2026-09-24", "run", "Easy run", "missed", 30, 35),
        _session("w3", "2026-09-25", "bike", "FTP test", "done", 60, 110, activity_id=7),
        _session("w4", "2026-09-26", "bike", "Long endurance ride", "planned", 180, 190),
    ],
    "plan_total": 505,
    "commutes": {4: True, 5: True},
    "chips": {7: "FTP 262 W ↑8"},
}


def _panel(**over):
    kw = dict(week=WEEK, prev_week=PREV, extras=EXTRAS, offset=0, today=TODAY)
    kw.update(over)
    return da.render_panel(kw["week"], kw["prev_week"], kw["extras"], kw["offset"], kw["today"])


def _section(html, key):
    start = html.index(f'activity-filter-section activity-filter-{key}"')
    nxt = html.find("activity-filter-section activity-filter-", start + 10)
    return html[start: nxt if nxt > 0 else len(html)]


def test_key_metric_per_sport():
    assert da.key_metric({"avg_power": 171, "avg_hr": 138}, "bike") == "171 W avg · HR 138"
    assert da.key_metric({"distance_km": 20, "duration_min": 60}, "bike") == "20.0 km/h"
    assert da.key_metric({"distance_km": 2.0, "duration_min": 48}, "swim") == "2:24 /100m"
    assert da.key_metric({"distance_km": 8.2, "duration_min": 45, "avg_hr": 148}, "run") == "5:29 /km · HR 148"
    assert da.key_metric({"avg_hr": 131}, "other") == "HR 131 avg"


def test_change_lines():
    assert da._pct_change(110, 100) == "↑ 10%"
    assert da._pct_change(90, 100) == "↓ 10%"
    assert da._pct_change(5, 0) == "new"
    assert da._pct_change(0, 0) == "–"


def test_summary_vs_last_week_with_planned_load():
    all_ = _section(_panel(), "all")
    assert "vs last week" in all_
    # 7 sessions, 502 load vs 2 sessions, 213 load last week.
    assert '<div style="font-size:24px">502</div>' in all_ and "↑ 136%" in all_
    assert '<div style="font-size:24px">7</div>' in all_ and "↑ 5" in all_
    assert "502 / 505 planned" in all_
    assert "Bike " in all_ and "Climb 55 min" in all_          # the breakdown legend
    # Planned load only on the All filter.
    assert "planned</div>" not in _section(_panel(), "bike")


def test_no_previous_week_hides_the_changes():
    all_ = _section(_panel(prev_week=None), "all")
    assert "vs last week" not in all_ and "↑" not in all_.split("Daily load")[0]


def test_pills_count_done_sessions_and_hide_empty_sports():
    html = _panel()
    pills = html[html.index("act-pills"):html.index("act-pills") + 1200]
    assert re.search(r'for="activity-filter-all">All<span class="act-count">7<', pills)
    assert re.search(r'for="activity-filter-triathlon">Triathlon<span class="act-count">5<', pills)
    assert 'for="activity-filter-run"' not in pills          # no run done this week
    assert re.search(r'for="activity-filter-other">Other<span class="act-count">1<', pills)


def test_day_groups_commutes_missed_and_tags():
    all_ = _section(_panel(), "all")
    assert all_.index("Fri 25") < all_.index("Thu 24") < all_.index("Wed 23")   # newest first
    assert '<span class="act-today">Today</span>' in all_
    assert "Commute &times;2" in all_ and "Ottawa Cycling · auto-grouped" in all_
    assert "openActivityModal(4)" in all_ and "openActivityModal(5)" in all_    # children open the modal
    assert "Missed · Zone 2" in all_ and "Easy run" in all_
    assert "FTP 262 W ↑8" in all_ and "act-chip-plan" in all_ and "Endurance ride" in all_
    assert "2h56 · 180 load" in all_ and "73.06 km" in all_


def test_single_commute_is_a_normal_row():
    extras = {**EXTRAS, "commutes": {4: True}}
    all_ = _section(_panel(extras=extras), "all")
    assert "Commute &times;" not in all_


def test_coming_up_and_strip():
    all_ = _section(_panel(), "all")
    assert "Coming up" in all_ and "Long endurance ride" in all_ and "~190 load" in all_ and "Sat 26" in all_
    # Friday is today; the planned-only Saturday has a dashed bar; Monday a plan outline.
    assert 'class="act-day today" data-act-day="4"' in all_
    sat = all_[all_.index('data-act-day="5"'):all_.index('data-act-day="6"')]
    assert "dashed" in sat and ">190<" in sat
    mon = all_[all_.index('data-act-day="0"'):all_.index('data-act-day="1"')]
    assert "act-plan-outline" in mon
    assert "Base" in all_ and "Threshold" in all_          # benefit legend
    assert "act-plan-outline" not in _section(_panel(), "bike")   # plan outlines only under All


def test_coming_up_is_collapsed_and_opens_the_workout():
    all_ = _section(_panel(), "all")
    block = all_[all_.index('<details class="act-upcoming"'):all_.index("</details>")]
    assert "Coming up · 1" in block
    assert "<details class=\"act-upcoming\">" in block            # no open attribute: collapsed
    assert 'data-plan-workout="w4"' in block                     # tapping opens the plan workout
    # The missed session in the log isn't part of Coming up.
    assert 'data-plan-workout="w2"' not in all_


def test_coming_up_dialogs_travel_with_the_panel():
    from tools import dashboard
    extras = {**EXTRAS, "planned": EXTRAS["planned"] + [
        {**_session("w5", "2026-09-27", "swim", "Pool swim", "planned", 45, 40),
         "distance_km": 2.0, "distance_meters": 2000, "description": "Easy aerobic",
         "details": "WARM-UP: 400m easy"}]}
    html = dashboard._panel_activity({"activity_week": WEEK, "activity_prev_week": PREV,
                                      "activity_extras": extras, "date": TODAY.isoformat()}, token="t0k")

    templates = html[html.index('<template id="plan-workout-w4">'):html.rindex("</section>")]
    assert '<template id="plan-workout-w5">' in templates
    assert "Sun 27 Sep" in templates and "2,000 m" in templates and "WARM-UP: 400m easy" in templates
    assert "/training-plan?workout=w5&amp;token=t0k" in templates    # Open in plan
    assert 'id="plan-workout-w2"' not in html                          # missed: no dialog


def test_past_week_header():
    html = _panel(offset=2, extras={})
    assert "Sep 21–27, 2026 · 2 weeks ago" in html
    assert 'data-week="0"' in html and "This week</a>" in html
    assert "act-today" not in html


def test_filters_show_empty_state():
    html = _panel(week={"week_start": WEEK_START, "activities": []}, extras={})
    assert "No activities match this filter." in _section(html, "all")


def test_error_panel_keeps_its_week():
    html = da.render_panel(None, None, None, 3, TODAY, err="boom")
    assert 'data-week="3"' in html and "boom" in html


def test_result_chips_from_threshold_changes():
    snaps = [
        {"source": "plan", "metric": "ftp", "snapshot_date": "2026-09-24", "value": 254},
        {"source": "plan", "metric": "ftp", "snapshot_date": "2026-09-25", "value": 262},
        {"source": "garmin", "metric": "threshold_pace_s", "snapshot_date": "2026-09-22", "value": 255},
        {"source": "garmin", "metric": "threshold_pace_s", "snapshot_date": "2026-09-23", "value": 250},
    ]
    chips = da.result_chips(snaps, WEEK["activities"], date(2026, 9, 21), date(2026, 9, 27))
    assert chips == {7: "FTP 262 W ↑8"}      # no run on the pace-change day: no chip


def test_calendar_month_grid():
    cal = {"grid_start": date(2026, 8, 31), "grid_end": date(2026, 10, 4),
           "activities": WEEK["activities"], "planned": EXTRAS["planned"]}
    html = da.render_calendar(2026, 9, TODAY, cal, "all", viewed_offset=0)
    assert "September 2026" in html
    assert html.count('class="act-cal-row') == 6            # header + 5 weeks
    # Weeks back from this one; the week still to come (Sep 28) isn't a link.
    assert re.findall(r'data-cal-week="(-?\d+)"', html) == ["3", "2", "1", "0"]
    assert 'act-cal-row sel click" data-cal-week="0"' in html
    assert "act-cell today" in html
    assert "7 sessions" in html and "All sports" in html
    assert 'data-cal-month="2026-08"' in html and 'data-cal-month="2026-10"' not in html   # no future months
    bike = da.render_calendar(2026, 9, TODAY, cal, "bike")
    assert "4 sessions" in bike and ">Bike<" in bike


def test_calendar_route(monkeypatch):
    from tools import dashboard
    seen = {}

    def fake(year, month, today):
        seen["ym"] = (year, month)
        return {"grid_start": date(2026, 6, 29), "grid_end": date(2026, 8, 2), "activities": [], "planned": []}

    monkeypatch.setattr(da, "calendar_data", fake)
    html = dashboard.render_activity_calendar("2026-07", "run", 3)
    assert seen["ym"] == (2026, 7) and "July 2026" in html and "No activities" in html
    dashboard.render_activity_calendar("garbage", None)
    today = dashboard._local_now().date()
    assert seen["ym"] == (today.year, today.month)


def test_coming_up_lists_a_day_by_start_time():
    evening = {**_session("w6", "2026-09-26", "run", "Evening run", "planned", 40, 40), "start_time": "17:30"}
    morning = {**_session("w7", "2026-09-26", "strength", "Gym", "planned", 45, 30), "start_time": "05:30"}
    extras = {**EXTRAS, "planned": [s for s in EXTRAS["planned"] if s["workout_id"] != "w4"] + [evening, morning]}
    block = _section(_panel(extras=extras), "all")
    assert block.index("Gym") < block.index("Evening run")
    assert "Sat 26 · 05:30 · Zone 2" in block
