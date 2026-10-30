# tests/test_recovery.py
"""Offline tests for the Recovery page (tools/recovery.py): the Garmin
intraday parsing, the rendered page, the day picker's clamping, the data
fallbacks and the /recovery route."""
from datetime import date, datetime, timedelta, timezone

import pytest

from tools import local_time, recovery


@pytest.fixture(autouse=True)
def utc_zone(monkeypatch):
    monkeypatch.setattr(local_time, "local_tz", lambda: timezone.utc)


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _day(hrv=56, score=74, level="MODERATE"):
    return {
        "sleep": {"sleep_score": 84, "sleep_score_label": "GOOD", "total_sleep_hrs": 7.5, "deep_sleep_hrs": 1.5,
                  "light_sleep_hrs": 4.2, "rem_sleep_hrs": 1.8, "awake_hrs": 0.4, "awake_count": 3,
                  "sleep_need_hrs": 8.0, "avg_respiration": 14.0, "resting_hr": 49},
        "readiness": {"hrv": {"last_night_avg": hrv, "weekly_avg": 54, "status": "BALANCED",
                              "baseline_low": 48, "baseline_high": 62},
                      "body_battery": {"charged": 61, "drained": 22, "highest": 87, "lowest": 24,
                                       "current_level": 63, "start_level": 85},
                      "daily_stats": {"resting_hr": 49, "resting_hr_7day_avg": 50}},
        "health": {"stress": {"avg_stress": 22, "rest_stress_mins": 310, "low_stress_mins": 150,
                              "medium_stress_mins": 40, "high_stress_mins": 8}},
        "training": {"readiness": {"score": score, "level": level, "feedback_short": "RECOVERED_AND_READY",
                                   "sleep_score": 84, "sleep_score_factor_percent": 84,
                                   "sleep_score_factor_feedback": "GOOD", "acute_load": 664,
                                   "acwr_factor_percent": 62, "acwr_factor_feedback": "MODERATE",
                                   "recovery_time": 240, "recovery_time_factor_percent": 88,
                                   "recovery_time_factor_feedback": "GOOD"}},
    }


def _intraday():
    bb = [None] * 24
    for h in range(15):
        bb[h] = 24 + h * 4 if h <= 6 else 85 - (h - 6) * 2
    stress = [12 if h < 7 else 30 for h in range(15)] + [None] * 9
    return {"bed": "22:44", "wake": "06:42", "wake_hour": 6.7,
            "stages": [{"stage": "light", "minutes": 20}, {"stage": "deep", "minutes": 40},
                       {"stage": "rem", "minutes": 25}, {"stage": "awake", "minutes": 5}],
            "body_battery": bb, "stress": stress}


def _data(day="2026-10-15", intraday=True):
    end = date(2026, 10, 15)
    days = {(end - timedelta(days=i)).isoformat(): _day(hrv=50 + i) for i in range(10)}
    return {"date": day, "today": "2026-10-15", "now_hour": 14, "days": days,
            "scores": {"2026-10-14": 81, "2026-10-12": 41}, "workout_hours": [11],
            "intraday": _intraday() if intraday else None}


# ── intraday parsing ──

def test_parse_intraday_maps_stages_and_reduces_to_hours():
    start = datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc)

    def lvl(offset, minutes, level):
        s = start + timedelta(minutes=offset)
        e = s + timedelta(minutes=minutes)
        return {"startGMT": s.strftime("%Y-%m-%dT%H:%M:%S.0"), "endGMT": e.strftime("%Y-%m-%dT%H:%M:%S.0"),
                "activityLevel": float(level)}

    sleep_raw = {
        "dailySleepDTO": {"sleepStartTimestampLocal": _ms(start),
                          "sleepEndTimestampLocal": _ms(datetime(2026, 10, 15, 6, 30, tzinfo=timezone.utc))},
        "sleepLevels": [lvl(0, 30, 1), lvl(30, 15, 1), lvl(45, 60, 0), lvl(105, 20, 2), lvl(125, 5, 3)],
    }
    t = lambda h, m=0: _ms(datetime(2026, 10, 15, h, m, tzinfo=timezone.utc))
    stress_raw = {
        "bodyBatteryValuesArray": [[t(7, 0), "MEASURED", 80, 3], [t(7, 45), "MEASURED", 78, 3],
                                   [_ms(start), "MEASURED", 20, 3]],          # yesterday: ignored
        "stressValuesArray": [[t(9, 0), 20], [t(9, 30), 40], [t(9, 45), -1], [t(10, 0), -2]],
    }
    out = recovery.parse_intraday("2026-10-15", sleep_raw, stress_raw)
    assert out["bed"] == "22:00" and out["wake"] == "06:30" and out["wake_hour"] == 6.5
    # Garmin levels 0/1/2/3 = deep/light/REM/awake; neighbouring light runs merge.
    assert out["stages"] == [{"stage": "light", "minutes": 45.0}, {"stage": "deep", "minutes": 60.0},
                             {"stage": "rem", "minutes": 20.0}, {"stage": "awake", "minutes": 5.0}]
    assert out["body_battery"][7] == 78 and out["body_battery"][22] is None
    assert out["stress"][9] == 30 and out["stress"][10] is None


def test_parse_intraday_handles_empty_payloads():
    out = recovery.parse_intraday("2026-10-15", None, {})
    assert out["stages"] == [] and out["body_battery"] == [None] * 24 and out["bed"] is None


# ── the page ──

def test_page_has_every_section_filter_pills_and_a_back_link():
    page = recovery.render_recovery_html(_data(), token="abc")
    for sec in ("readiness", "sleep", "hrv", "battery", "stress", "vitals"):
        assert f'data-sec="{sec}"' in page and f'data-filter="{sec}"' in page
    assert 'data-filter="all" aria-pressed="true"' in page
    assert 'href="/dashboard?token=abc"' in page
    assert "<title>Recovery · Ridgeline</title>" in page
    assert '"2026-10-12": 41' in page                     # calendar readiness dots
    assert "Recovered and ready" in page and "74" in page


def _hrv_card(page):
    return page.split('data-sec="hrv"', 1)[1].split("</section>", 1)[0]


def test_the_week_runs_monday_to_sunday_and_future_days_are_empty():
    page = recovery.render_recovery_html(_data())          # Thu 15 Oct 2026
    card = _hrv_card(page)
    days = [m.split('"')[0] for m in card.split('data-go="')[1:]]
    assert days == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]    # Mon..Thu, Fri-Sun not yet
    assert card.count("rc-dot-none") == 3
    assert card.index("<span>M 12</span>") < card.index("<span>Today</span>")
    assert '<div class="rc-stat-l">Last night</div>' in card
    assert 'class="rc-dot on" data-go="2026-10-15"' in card


def test_a_past_day_shows_its_date_and_its_own_week():
    page = recovery.render_recovery_html(_data(day="2026-10-12"))
    card = _hrv_card(page)
    assert 'aria-label="Choose day, showing Mon 12 Oct"' in page
    assert '<div class="rc-stat-l">Mon 12 Oct</div>' in card
    assert 'class="rc-dot on" data-go="2026-10-12"' in card
    assert "Mon 12 Oct, morning" in page


def test_vitals_bars_are_the_same_week_and_pick_the_day():
    page = recovery.render_recovery_html(_data(day="2026-10-13"))
    vitals = page.split('data-sec="vitals"', 1)[1].split("</section>", 1)[0]
    assert vitals.count('class="rc-mbar on" data-go="2026-10-13"') == 2     # resting HR and respiration
    assert vitals.count('data-go="2026-10-12"') == 2
    assert vitals.count("<button") == 14 and vitals.count(" disabled") == 6    # Fri-Sun can't be picked
    assert "data-hrv" not in page


def test_a_week_in_the_past_loads_all_seven_days(monkeypatch):
    seen = []
    import db
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_recovery_days", lambda a, b: seen.append((a, b)) or [])
    monkeypatch.setattr(db, "get_readiness_scores", lambda a, b: {})
    monkeypatch.setattr(recovery, "_workout_hours", lambda d: [])
    monkeypatch.setattr(recovery, "_live_day", lambda iso: {})
    monkeypatch.setattr(recovery, "_intraday", lambda iso, t: None)
    monkeypatch.setattr(recovery, "_local_now", lambda: datetime(2026, 10, 15, 9, tzinfo=timezone.utc))
    recovery.build_recovery_data("2026-10-07")                # a Wednesday
    assert seen == [("2026-10-05", "2026-10-11")]
    seen.clear()
    recovery.build_recovery_data(None)                        # this week stops at today
    assert seen == [("2026-10-12", "2026-10-15")]


def test_hourly_charts_and_sleep_timeline_render_and_today_stops_at_now():
    page = recovery.render_recovery_html(_data())
    assert page.count('data-hour="') == 48
    assert "14:00 (now)" in page
    assert 'data-seg="deep"' in page and 'data-stage="rem"' in page
    assert 'data-hour="20"' in page and "20:00, body battery no reading" in page


def test_missing_hourly_data_says_so():
    page = recovery.render_recovery_html(_data(intraday=False))
    assert "Hour-by-hour readings aren't available" in page
    assert "timeline isn't available" in page


def test_a_day_without_data_renders_empty_sections():
    data = _data()
    data["days"] = {}
    page = recovery.render_recovery_html(data)
    assert "No readiness score" in page and "No sleep was recorded" in page


# ── data ──

def test_day_param_is_clamped_to_the_last_year():
    today = date(2026, 10, 15)
    assert recovery._parse_day(None, today) == today
    assert recovery._parse_day("nonsense", today) == today
    assert recovery._parse_day("2026-12-01", today) == today
    assert recovery._parse_day("2020-01-01", today) == today - timedelta(days=recovery.HISTORY_DAYS)
    assert recovery._parse_day("2026-10-12", today) == date(2026, 10, 12)


def test_without_a_database_the_day_comes_live(monkeypatch):
    import db
    monkeypatch.setattr(db, "is_configured", lambda: False)
    monkeypatch.setattr(recovery, "_local_now", lambda: datetime(2026, 10, 15, 14, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(recovery, "_live_day", lambda iso: _day())
    monkeypatch.setattr(recovery, "_intraday", lambda iso, is_today: None)
    data = recovery.build_recovery_data("2026-10-13")
    assert data["date"] == "2026-10-13" and list(data["days"]) == ["2026-10-13"]
    assert data["scores"] == {"2026-10-13": 74} and data["now_hour"] == 14


def test_stored_days_come_from_the_database(monkeypatch):
    import db
    rows = [{"metric_date": date(2026, 10, 15), "sleep_data": {"sleep_score": 80}, "readiness_data": None,
             "health_data": {}, "training_data": {"readiness": {"score": 70}}}]
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_recovery_days", lambda start, end: rows)
    monkeypatch.setattr(db, "get_readiness_scores", lambda start, end: {"2026-10-15": 70})
    monkeypatch.setattr(db, "get_activities_in_range", lambda start, end: [])
    monkeypatch.setattr(recovery, "_local_now", lambda: datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(recovery, "_live_day", lambda iso: pytest.fail("stored day fetched live"))
    monkeypatch.setattr(recovery, "_intraday", lambda iso, is_today: None)
    data = recovery.build_recovery_data(None)
    assert data["days"]["2026-10-15"]["sleep"] == {"sleep_score": 80}
    assert data["days"]["2026-10-15"]["readiness"] == {}


def test_workout_hours_cover_the_activity(monkeypatch):
    import db
    rows = [{"activity_date": datetime(2026, 10, 15, 6, 40, tzinfo=timezone.utc), "duration_min": 80},
            {"activity_date": datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), "duration_min": 60}]
    monkeypatch.setattr(db, "get_activities_in_range", lambda start, end: rows)
    assert recovery._workout_hours(date(2026, 10, 15)) == [6, 7, 18]


def test_hourly_data_is_cached_and_a_stale_copy_beats_none(monkeypatch):
    from tools import dashboard
    dashboard._clear_section_cache()
    calls = []
    monkeypatch.setattr(recovery, "_fetch_intraday", lambda iso: calls.append(iso) or {"stages": []})
    assert recovery._intraday("2026-10-01", False) == {"stages": []}
    assert recovery._intraday("2026-10-01", False) == {"stages": []}
    assert calls == ["2026-10-01"]
    monkeypatch.setattr(recovery, "_get_cached", lambda key, ttl: ({"stages": ["old"]}, None, False))
    monkeypatch.setattr(recovery, "_fetch_intraday", lambda iso: (_ for _ in ()).throw(RuntimeError("down")))
    assert recovery._intraday("2026-10-01", False) == {"stages": ["old"]}


# ── wiring ──

def test_today_recovery_card_opens_the_recovery_page():
    from tools import dashboard
    from tests.test_dashboard import SAMPLE
    page = dashboard.render_dashboard_html(SAMPLE, token="abc")
    assert '<a class="focus-card t-link" href="/recovery?token=abc" data-title="Recovery"' in page


def test_recovery_route_serves_the_page(monkeypatch):
    import server
    from starlette.testclient import TestClient
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    monkeypatch.setattr(server, "BEARER_TOKEN", None, raising=False)
    seen = {}
    monkeypatch.setattr(recovery, "render_recovery_page",
                        lambda day, token: seen.update(day=day, token=token) or "<html>recovery</html>")
    response = TestClient(server.build_asgi_app()).get("/recovery?date=2026-10-12")
    assert response.status_code == 200 and response.text == "<html>recovery</html>"
    assert seen == {"day": "2026-10-12", "token": None}


def test_picking_a_day_swaps_the_page_in_place():
    js = recovery._RECOVERY_JS
    assert "function swapDay" in js and "swapDay(u.toString(), true)" in js
    assert "window.location.href = u.toString()" not in js          # no full navigation / scroll reset
    assert "window.scrollTo(0, y)" in js and "popstate" in js       # scroll kept, Back still works
    assert "window.location.href = url" in js                       # fallback if the fetch fails
