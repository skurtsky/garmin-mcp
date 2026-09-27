# tests/test_dashboard_trends.py
"""The Trends tab's fitness & fatigue card and metric cards
(tools/dashboard_trends.py). Offline — the series is built here."""
import json
import re
from datetime import date, timedelta

from tools import dashboard, dashboard_trends as dt, training_load

TODAY = date(2026, 9, 25)


def _fit(actual_days=120, projected_days=16, load=80, planned=40):
    loads = {(TODAY - timedelta(days=i)).isoformat(): load for i in range(actual_days)}
    plan = {(TODAY + timedelta(days=i)).isoformat(): planned for i in range(1, projected_days + 1)}
    series = training_load.fitness_series(loads, TODAY - timedelta(days=89), TODAY + timedelta(days=120), TODAY, plan)
    return {"today": TODAY.isoformat(), "series": series}


def _trends():
    daily = lambda base: [{"date": (TODAY - timedelta(days=i)).isoformat(), "value": base + i % 5} for i in range(100)]
    return {"metrics": {"hrv": {"daily": daily(60)}, "rhr": {"daily": daily(50)},
                        "sleep_score": {"daily": daily(75)}, "stress": {"daily": daily(28)}}}


def _goal(days_left):
    d = TODAY + timedelta(days=days_left)
    return {"name": "Fall Classic", "date": d.isoformat(), "days_left": days_left}


def test_window_is_the_range_plus_at_most_as_much_projection():
    win, ti = dt.window(_fit()["series"], TODAY.isoformat(), 14)
    assert win[ti]["date"] == TODAY.isoformat() and ti == 13
    assert len(win) == 14 + 14 and all(p["projected"] for p in win[ti + 1:])
    win, ti = dt.window(_fit()["series"], TODAY.isoformat(), 90)
    assert len(win) == 90 + 16          # the projection stops at the last planned session


def test_card_stats_and_data_island():
    html = dt.render(_fit(), None, _trends(), 30, "r30")
    assert "Fitness &amp; fatigue" in html and "Today · load 80" in html
    days = json.loads(re.search(r'class="ff-data">(.*?)</script>', html).group(1))
    assert len(days) == 30 + 16 and days[29]["d"] == TODAY.isoformat() and days[-1]["p"] is True
    assert days[29]["hrv"] == 60 and days[-1]["hrv"] is None
    assert 'data-ti="29"' in html
    assert "since Aug 27" in html
    assert html.count('class="ff-overlay"') == 4 and html.count('class="ff-metric"') == 4
    assert "Planned</span>" in html


def test_race_shown_only_inside_the_projected_window():
    inside = dt.render(_fit(), _goal(10), _trends(), 30, "a")
    assert "ff-race" in inside and 'class="ff-flag"' in inside and "Race-day target" in inside
    assert "10 days · projected from plan" in inside
    beyond = dt.render(_fit(), _goal(20), _trends(), 30, "b")        # plan projects 16 days
    assert "ff-race" not in beyond and "ff-flag" not in beyond and "Race-day target" not in beyond
    short = dt.render(_fit(), _goal(10), _trends(), 7, "c")         # the 7d range projects 7 days
    assert "ff-race" not in short
    past = dt.render(_fit(), _goal(-3), _trends(), 30, "d")
    assert "ff-race" not in past


def test_race_strip_target_wording():
    # Rest before the race: form climbs well above +15.
    fresh = _fit(projected_days=16, planned=0)
    html = dt.render(fresh, _goal(14), _trends(), 30, "a")
    assert "Too fresh · target +5 to +15" in html


def test_without_fitness_the_metric_cards_still_show():
    html = dt.render(None, None, _trends(), 30, "x")
    assert "ff-card" not in html and html.count('class="ff-metric"') == 4
    assert "disabled" in html and "Overlay on chart" not in html


def test_metric_delta_vs_previous_range():
    daily = {(TODAY - timedelta(days=i)).isoformat(): (60 if i < 7 else 55) for i in range(14)}
    dates = [(TODAY - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    assert dt._delta(daily, dates, lower_better=False) == ("+5", "#7fc9b0")
    assert dt._delta(daily, dates, lower_better=True) == ("+5", "#cf8a80")
    assert dt._delta({}, dates, False) == ("", "")


def test_trends_panel_uses_overlay_cards_and_keeps_load_and_steps():
    data = {"trends": {**_trends(), "days": 90}, "redesign": {"fitness": _fit(), "goal": None},
            "training_status": {}, "training_status_daily_history": []}
    data["trends"]["metrics"]["training_load"] = {"unit": "", "daily": _trends()["metrics"]["hrv"]["daily"]}
    html = dashboard._panel_trends(data)
    assert html.count("ff-card") >= 5            # one per range
    assert 'id="lc-hrv-7"' not in html and 'id="lc-training_load-7"' in html
