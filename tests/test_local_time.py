from datetime import date, datetime, timezone

import pytest

from tools import local_time


class _FixedDatetime(datetime):
    """datetime whose now() is 2026-09-29 02:30 UTC — 22:30 the evening
    before in Toronto."""
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 9, 29, 2, 30, tzinfo=timezone.utc)
        return base.astimezone(tz) if tz else base.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LOCAL_TIMEZONE", raising=False)
    monkeypatch.delenv("DASHBOARD_TZ_OFFSET_HOURS", raising=False)
    monkeypatch.setattr(local_time, "datetime", _FixedDatetime)


def test_defaults_to_utc():
    assert local_time.local_today() == date(2026, 9, 29)
    assert local_time.utc_offset_hours() == 0


def test_named_zone_keeps_evening_on_the_same_day(monkeypatch):
    monkeypatch.setenv("LOCAL_TIMEZONE", "America/Toronto")
    assert local_time.local_today() == date(2026, 9, 28)
    assert local_time.local_now().strftime("%H:%M") == "22:30"


def test_named_zone_follows_daylight_saving(monkeypatch):
    monkeypatch.setenv("LOCAL_TIMEZONE", "America/Toronto")
    assert local_time.utc_offset_hours(datetime(2026, 7, 1, tzinfo=timezone.utc)) == -4
    assert local_time.utc_offset_hours(datetime(2026, 12, 1, tzinfo=timezone.utc)) == -5


def test_fixed_offset_fallback(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TZ_OFFSET_HOURS", "-4")
    assert local_time.local_today() == date(2026, 9, 28)


def test_named_zone_wins_over_offset(monkeypatch):
    monkeypatch.setenv("LOCAL_TIMEZONE", "Asia/Tokyo")
    monkeypatch.setenv("DASHBOARD_TZ_OFFSET_HOURS", "-4")
    assert local_time.utc_offset_hours() == 9


def test_unknown_zone_falls_back(monkeypatch):
    monkeypatch.setenv("LOCAL_TIMEZONE", "Mars/Olympus_Mons")
    monkeypatch.setenv("DASHBOARD_TZ_OFFSET_HOURS", "-4")
    assert local_time.utc_offset_hours() == -4


def test_to_local_treats_naive_as_utc(monkeypatch):
    monkeypatch.setenv("LOCAL_TIMEZONE", "America/Toronto")
    assert local_time.to_local(datetime(2026, 8, 21, 9, 46)).strftime("%H:%M") == "05:46"
