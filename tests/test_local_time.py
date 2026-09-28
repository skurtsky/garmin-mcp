from datetime import date, datetime, timezone

import pytest

import db
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
    monkeypatch.setattr(db, "is_configured", lambda: False)
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


@pytest.fixture
def settings_db(monkeypatch):
    store = {}
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_setting", lambda key: store.get(key))
    monkeypatch.setattr(db, "set_setting",
                        lambda key, value: store.pop(key, None) if value is None else store.__setitem__(key, value))
    return store


def test_settings_zone_wins_over_environment(monkeypatch, settings_db):
    monkeypatch.setenv("LOCAL_TIMEZONE", "Asia/Tokyo")
    local_time.save_zone("America/Toronto")
    assert settings_db["timezone"] == "America/Toronto"
    assert local_time.local_today() == date(2026, 9, 28)
    assert local_time.zone_source()[1] == "settings"
    local_time.save_zone(None)
    assert "timezone" not in settings_db
    assert local_time.zone_source()[1] == "env" and local_time.utc_offset_hours() == 9


def test_save_zone_rejects_unknown_names(settings_db):
    for bad in ("", "Mars/Olympus_Mons", "../etc/passwd"):
        with pytest.raises(local_time.TimezoneError):
            local_time.save_zone(bad)
    assert settings_db == {}


def test_invalid_stored_zone_is_ignored(settings_db):
    settings_db["timezone"] = "Not/AZone"
    assert local_time.zone_source()[1] == "default"


def test_stored_zone_is_cached_until_saved(monkeypatch, settings_db):
    calls = []
    real_get = db.get_setting
    monkeypatch.setattr(db, "get_setting", lambda key: calls.append(key) or real_get(key))
    local_time.local_today()
    local_time.local_today()
    assert len(calls) == 1
    local_time.save_zone("Europe/Paris")
    assert local_time.utc_offset_hours() == 2 and len(calls) == 2


def test_unreachable_database_falls_back(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: True)
    def boom(key):
        raise OSError("down")
    monkeypatch.setattr(db, "get_setting", boom)
    monkeypatch.setenv("DASHBOARD_TZ_OFFSET_HOURS", "-4")
    assert local_time.zone_source()[1] == "offset"
    assert local_time.zone_label(local_time.local_tz()) == "UTC-04:00"


def test_settings_payload(settings_db):
    local_time.save_zone("America/Toronto")
    p = local_time.settings_payload()
    assert (p["timezone"], p["effective"], p["source"]) == ("America/Toronto", "America/Toronto", "settings")
    assert p["now"].startswith("2026-09-28T22:30")
