# tests/test_db_first.py
"""Tests for the MCP tools' database-first read path (tools/db_first.py).

Offline: the db module's read functions and the live Garmin fetchers are
monkeypatched, so neither PostgreSQL nor a Garmin session is needed.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import db
from tools import db_first, trends


NOW = datetime.now(timezone.utc)
TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
LAST_WEEK = (date.today() - timedelta(days=7)).isoformat()


@pytest.fixture(autouse=True)
def db_on(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.delenv("MCP_DB_FIRST", raising=False)
    monkeypatch.delenv("MCP_DB_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("DASHBOARD_TZ_OFFSET_HOURS", raising=False)


def _rows_by_date(monkeypatch, rows: dict):
    monkeypatch.setattr(db, "get_today_metrics", lambda d: rows.get(d))


def _live(calls: list, result=None):
    def fetch(d):
        calls.append(d)
        return result if result is not None else {"date": d, "from": "live"}
    return fetch


def _final_synced_at(day: str) -> datetime:
    """A sync timestamp shortly after `day` ended (UTC midnight)."""
    return datetime.combine(date.fromisoformat(day) + timedelta(days=1),
                            datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=5)


# ── row_is_usable ────────────────────────────────────────────────────────────

def test_row_synced_after_day_end_is_final():
    assert db_first.row_is_usable(LAST_WEEK, _final_synced_at(LAST_WEEK), NOW)


def test_row_synced_during_day_is_stale_once_old():
    synced = datetime.combine(date.fromisoformat(LAST_WEEK), datetime.min.time(),
                              tzinfo=timezone.utc) + timedelta(hours=12)
    assert not db_first.row_is_usable(LAST_WEEK, synced, NOW)


def test_todays_row_is_usable_only_while_fresh():
    assert db_first.row_is_usable(TODAY, NOW - timedelta(minutes=5), NOW)
    assert not db_first.row_is_usable(TODAY, NOW - timedelta(hours=2), NOW)


def test_max_age_is_configurable(monkeypatch):
    monkeypatch.setenv("MCP_DB_MAX_AGE_SECONDS", "10800")
    assert db_first.row_is_usable(TODAY, NOW - timedelta(hours=2), NOW)


def test_day_end_follows_local_offset(monkeypatch):
    # 2026-07-01 ends at 04:00 UTC when local time is UTC-4, so a 02:00 UTC
    # sync is still inside that local day and not final.
    monkeypatch.setenv("DASHBOARD_TZ_OFFSET_HOURS", "-4")
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    assert not db_first.row_is_usable("2026-07-01", datetime(2026, 7, 2, 2, tzinfo=timezone.utc), now)
    assert db_first.row_is_usable("2026-07-01", datetime(2026, 7, 2, 5, tzinfo=timezone.utc), now)


def test_missing_synced_at_is_not_usable():
    assert not db_first.row_is_usable(TODAY, None, NOW)


# ── daily_payload ────────────────────────────────────────────────────────────

def test_final_past_day_is_served_from_db_without_live_call(monkeypatch):
    synced = _final_synced_at(LAST_WEEK)
    _rows_by_date(monkeypatch, {LAST_WEEK: {"sleep_data": {"sleep_score": 81}, "synced_at": synced}})
    calls = []
    result = db_first.daily_payload("sleep_data", LAST_WEEK, _live(calls))
    assert calls == []
    assert result["sleep_score"] == 81
    assert result["data_source"] == {"source": "db", "synced_at": synced.isoformat()}


def test_fresh_today_row_is_served_from_db(monkeypatch):
    _rows_by_date(monkeypatch, {TODAY: {"health_data": {"x": 1}, "synced_at": NOW - timedelta(minutes=3)}})
    calls = []
    result = db_first.daily_payload("health_data", "today", _live(calls))
    assert calls == []
    assert result["data_source"]["source"] == "db"


def test_stale_today_row_goes_live(monkeypatch):
    _rows_by_date(monkeypatch, {TODAY: {"health_data": {"x": 1}, "synced_at": NOW - timedelta(hours=3)}})
    calls = []
    result = db_first.daily_payload("health_data", "today", _live(calls))
    assert calls == [TODAY]
    assert result["from"] == "live"
    assert result["data_source"] == {"source": "live"}


def test_unsynced_day_goes_live(monkeypatch):
    _rows_by_date(monkeypatch, {})
    calls = []
    db_first.daily_payload("sleep_data", YESTERDAY, _live(calls))
    assert calls == [YESTERDAY]


def test_null_payload_goes_live(monkeypatch):
    # The sync job stores JSON null when that one fetch failed.
    _rows_by_date(monkeypatch, {LAST_WEEK: {"sleep_data": None, "synced_at": _final_synced_at(LAST_WEEK)}})
    calls = []
    db_first.daily_payload("sleep_data", LAST_WEEK, _live(calls))
    assert calls == [LAST_WEEK]


def test_live_failure_falls_back_to_stale_row(monkeypatch):
    synced = NOW - timedelta(hours=3)
    _rows_by_date(monkeypatch, {TODAY: {"training_data": {"score": 55}, "synced_at": synced}})

    def boom(d):
        raise RuntimeError("Garmin down")

    result = db_first.daily_payload("training_data", TODAY, boom)
    assert result["score"] == 55
    assert result["data_source"]["source"] == "db"
    assert result["data_source"]["stale"] is True
    assert result["data_source"]["synced_at"] == synced.isoformat()


def test_live_failure_without_row_raises(monkeypatch):
    _rows_by_date(monkeypatch, {})

    def boom(d):
        raise RuntimeError("Garmin down")

    with pytest.raises(RuntimeError):
        db_first.daily_payload("training_data", TODAY, boom)


def test_db_error_goes_live(monkeypatch):
    def broken(d):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(db, "get_today_metrics", broken)
    calls = []
    result = db_first.daily_payload("sleep_data", LAST_WEEK, _live(calls))
    assert calls == [LAST_WEEK]
    assert result["data_source"]["source"] == "live"


def test_no_database_goes_live_without_querying(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    monkeypatch.setattr(db, "get_today_metrics", lambda d: pytest.fail("DB queried"))
    calls = []
    db_first.daily_payload("sleep_data", LAST_WEEK, _live(calls))
    assert calls == [LAST_WEEK]


def test_kill_switch_disables_db_path(monkeypatch):
    monkeypatch.setenv("MCP_DB_FIRST", "0")
    monkeypatch.setattr(db, "get_today_metrics", lambda d: pytest.fail("DB queried"))
    calls = []
    db_first.daily_payload("sleep_data", LAST_WEEK, _live(calls))
    assert calls == [LAST_WEEK]


# ── get_trends ───────────────────────────────────────────────────────────────

def _trend_row(day: str, synced_at: datetime, **overrides) -> dict:
    row = {
        "metric_date": date.fromisoformat(day), "resting_hr": 48, "hrv": Decimal("62"),
        "sleep_score": 80, "stress": 25, "steps": 9000, "training_load": 410.0,
        "body_battery_wake": Decimal("90"), "body_battery_drain": Decimal("70"),
        "synced_at": synced_at,
    }
    row.update(overrides)
    return row


def _window_rows(period: str, today_synced_at: datetime | None) -> list:
    dates = trends.trend_window(period)
    rows = [_trend_row(d, _final_synced_at(d)) for d in dates[:-1]]
    if today_synced_at is not None:
        rows.append(_trend_row(dates[-1], today_synced_at))
    return rows


def _live_series(calls: list):
    def fetch(client, dates, requested):
        calls.append(list(dates))
        return {key: {d: 1 for d in dates} for key in trends.output_series_keys(requested)}
    return fetch


@pytest.fixture
def live_trends(monkeypatch):
    calls = []
    monkeypatch.setattr(db_first, "get_client", lambda: object())
    monkeypatch.setattr(trends, "fetch_live_series", _live_series(calls))
    return calls


def test_trends_fully_synced_window_needs_no_live_call(monkeypatch, live_trends):
    rows = _window_rows("7d", NOW - timedelta(minutes=2))
    monkeypatch.setattr(db, "get_trend_series_rows", lambda s, e: rows)

    result = db_first.get_trends("7d", ["rhr", "hrv", "body_battery"])

    assert live_trends == []
    assert list(result["metrics"]) == ["rhr", "hrv", "body_battery_wake", "body_battery_drain"]
    assert [p["value"] for p in result["metrics"]["rhr"]["daily"]] == [48] * 7
    hrv = result["metrics"]["hrv"]["daily"][0]["value"]
    assert hrv == 62 and isinstance(hrv, int)  # Decimal normalised
    assert result["data_source"]["source"] == "db"
    assert result["data_source"]["db_days"] == 7
    assert result["data_source"]["live_days"] == 0


def test_trends_fetches_only_unsynced_days_live(monkeypatch, live_trends):
    rows = _window_rows("7d", NOW - timedelta(hours=4))  # today's row is stale
    monkeypatch.setattr(db, "get_trend_series_rows", lambda s, e: rows)

    result = db_first.get_trends("7d", ["steps"])

    assert live_trends == [[TODAY]]
    steps = [p["value"] for p in result["metrics"]["steps"]["daily"]]
    assert steps == [9000] * 6 + [1]
    assert result["data_source"]["source"] == "db+live"
    assert result["data_source"]["db_days"] == 6
    assert result["data_source"]["live_days"] == 1


def test_trends_with_empty_db_is_all_live(monkeypatch, live_trends):
    monkeypatch.setattr(db, "get_trend_series_rows", lambda s, e: [])
    result = db_first.get_trends("7d", ["rhr"])
    assert live_trends == [trends.trend_window("7d")]
    assert result["data_source"]["source"] == "live"


def test_trends_live_failure_fills_from_stale_rows(monkeypatch):
    rows = _window_rows("7d", NOW - timedelta(hours=4))
    monkeypatch.setattr(db, "get_trend_series_rows", lambda s, e: rows)

    def boom():
        raise RuntimeError("login failed")
    monkeypatch.setattr(db_first, "get_client", boom)

    result = db_first.get_trends("7d", ["rhr"])
    assert [p["value"] for p in result["metrics"]["rhr"]["daily"]] == [48] * 7
    assert result["data_source"]["stale"] is True
    assert result["data_source"]["stale_days"] == 1
    assert result["data_source"]["live_days"] == 0


def test_trends_rejects_unknown_period_before_touching_db(monkeypatch):
    monkeypatch.setattr(db, "get_trend_series_rows", lambda s, e: pytest.fail("DB queried"))
    with pytest.raises(ValueError):
        db_first.get_trends("2w")


def test_trends_without_database_uses_live_get_trends(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    monkeypatch.setattr(trends, "get_trends",
                        lambda period, metrics: {"period": period, "metrics": {}})
    result = db_first.get_trends("7d")
    assert result["data_source"] == {"source": "live"}


# ── trends.fetch_live_series (gap filling) ───────────────────────────────────

class _FakeClient:
    def __init__(self):
        self.step_ranges = []

    def get_heart_rates(self, d):
        return {"restingHeartRate": 50}

    def get_daily_steps(self, start, end):
        self.step_ranges.append((start, end))
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        return [{"calendarDate": (s + timedelta(days=i)).isoformat(), "totalSteps": 100}
                for i in range((e - s).days + 1)]


def test_fetch_live_series_trims_ranged_metrics_to_requested_days():
    client = _FakeClient()
    values = trends.fetch_live_series(client, ["2026-07-01", "2026-07-04"], ["rhr", "steps"])
    assert values["rhr"] == {"2026-07-01": 50, "2026-07-04": 50}
    assert values["steps"] == {"2026-07-01": 100, "2026-07-04": 100}
    assert client.step_ranges == [("2026-07-01", "2026-07-04")]
