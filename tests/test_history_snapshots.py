# tests/test_history_snapshots.py
"""Threshold / race-prediction snapshots and the one-off history backfill
(tools/history_snapshots.py), and their sync_garmin.py wiring. Offline — db
writes are captured, Garmin is faked."""
from datetime import date, datetime, timezone

import pytest

import db
import sync_garmin
from tests.plan_fakes import sample_plan
from tools import history_snapshots as hs


@pytest.fixture
def writes(monkeypatch):
    out = {"thresholds": [], "predictions": []}
    monkeypatch.setattr(db, "upsert_threshold_snapshot",
                        lambda day, source, values: out["thresholds"].append((day, source, values)))
    monkeypatch.setattr(db, "upsert_race_predictions",
                        lambda day, values: out["predictions"].append((day, values)))
    return out


def test_garmin_threshold_values_units():
    values = hs.garmin_threshold_values({"ftp": 262, "lactate_threshold_hr": 170,
                                         "lactate_threshold_pace": 4.17, "vo2max_running": 55})
    assert values == {"ftp": 262, "run_lthr": 170, "threshold_pace_s": 250.2,
                      "vo2max_run": 55, "vo2max_bike": None}


def test_plan_threshold_values_use_overrides():
    plan = sample_plan()
    plan["overrides"] = {"ftp": 262, "cssSeconds": 121}
    assert hs.plan_threshold_values(plan) == {"ftp": 262, "bike_lthr": 160, "run_lthr": 170,
                                              "threshold_pace_s": 257, "css_s": 121}
    assert hs.plan_threshold_values(None) == {}


def test_prediction_values():
    raw = {"calendarDate": "2026-09-25", "time5K": 1205, "time10K": 2530, "timeHalfMarathon": 5620,
           "timeMarathon": None}
    assert hs.prediction_values(raw) == {"5K": 1205, "10K": 2530, "half_marathon": 5620}


@pytest.mark.parametrize("resp,expected", [
    ([{"from": "2026-01-02", "value": 170}, {"calendarDate": "2026-02-01", "value": 171}],
     {"2026-01-02": 170.0, "2026-02-01": 171.0}),
    ({"values": [{"date": "2026-03-01", "value": 4.0}]}, {"2026-03-01": 4.0}),
    (None, {}),
])
def test_stats_points_shapes(resp, expected):
    assert hs.stats_points(resp) == expected


def test_lt_speed_units():
    assert hs.lt_speed_to_pace(0.4) == 250.0     # profile units (tenths of m/s)
    assert hs.lt_speed_to_pace(4.0) == 250.0     # m/s
    assert hs.lt_speed_to_pace(0) is None


class FakeGarmin:
    def __init__(self, ftp_fails=False):
        self.ftp_fails = ftp_fails
        self.max_metric_days = []

    def get_race_predictions(self, *args):
        if not args:
            return {"calendarDate": "2026-09-25", "time5K": 1205}
        return [{"calendarDate": "2026-06-01", "time5K": 1240, "time10K": 2600},
                {"calendarDate": "2026-09-01", "time5K": 1210}]

    def get_lactate_threshold(self, **kwargs):
        assert kwargs["latest"] is False
        return {"heart_rate": [{"from": "2026-05-01", "value": 168}],
                "speed": [{"from": "2026-05-01", "value": 0.38}], "power": []}

    def connectapi(self, url):
        assert "functionalThresholdPower" in url and "CYCLING" in url
        if self.ftp_fails:
            raise RuntimeError("404")
        return [{"from": "2026-04-01", "value": 248.0}]

    def get_max_metrics(self, day):
        self.max_metric_days.append(day)
        return [{"generic": {"vo2MaxPreciseValue": 54.2}, "cycling": None}]


def test_daily_snapshots(writes, monkeypatch):
    monkeypatch.setattr(db, "get_training_plan", lambda plan_id: {"plan": sample_plan()})
    hs.snapshot_plan_thresholds(date(2026, 9, 25))
    assert writes["thresholds"][0][:2] == ("2026-09-25", "plan")
    assert hs.snapshot_race_predictions(date(2026, 9, 25), FakeGarmin()) == {"5K": 1205}
    assert writes["predictions"] == [("2026-09-25", {"5K": 1205})]


def test_backfill_history(writes, monkeypatch):
    monkeypatch.setattr(db, "get_vo2max_history_from_daily_metrics", lambda a, b: [
        {"metric_date": date(2026, 9, 24), "running": 55.0, "cycling": 50.0}])
    monkeypatch.setattr(db, "list_plan_revisions_for_thresholds", lambda: [
        {"plan": sample_plan(), "created_at": datetime(2026, 9, 1, 9, tzinfo=timezone.utc)},
        {"plan": {**sample_plan(), "overrides": {"ftp": 262}}, "created_at": datetime(2026, 9, 1, 18, tzinfo=timezone.utc)},
    ])
    garmin = FakeGarmin()
    results = hs.backfill_history(garmin, today=date(2026, 9, 25), days=3)

    assert results == {"race_predictions": 2, "run_threshold": 1, "ftp": 1, "vo2max": 4, "plan_thresholds": 1}
    garmin_rows = [(d, v) for d, s, v in writes["thresholds"] if s == "garmin"]
    assert ("2026-05-01", {"run_lthr": 168.0, "threshold_pace_s": 263.2}) in garmin_rows
    assert ("2026-04-01", {"ftp": 248}) in garmin_rows
    # VO2max: the stored day is reused, only the other three are fetched.
    assert sorted(garmin.max_metric_days) == ["2026-09-22", "2026-09-23", "2026-09-25"]
    plan_rows = [(d, v) for d, s, v in writes["thresholds"] if s == "plan"]
    assert plan_rows == [("2026-09-01", hs.plan_threshold_values({**sample_plan(), "overrides": {"ftp": 262}}))]


def test_backfill_ftp_falls_back_to_rides_and_errors_are_isolated(writes, monkeypatch):
    monkeypatch.setattr(db, "get_ride_ftp_history", lambda a, b: [{"day": "2026-09-20", "ftp": 250.0}])
    monkeypatch.setattr(db, "get_vo2max_history_from_daily_metrics", lambda a, b: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(db, "list_plan_revisions_for_thresholds", lambda: [])
    results = hs.backfill_history(FakeGarmin(ftp_fails=True), today=date(2026, 9, 25), days=3)
    assert results["ftp"] == 1
    assert results["vo2max"].startswith("error:")
    assert ("2026-09-20", "garmin", {"ftp": 250}) in writes["thresholds"]


# ── sync wiring ──────────────────────────────────────────────────────────────

def test_parse_backfill_flag():
    args = sync_garmin.parse_args(["--backfill-history", "--history-days", "200"])
    assert args.backfill_history is True and args.history_days == 200
    assert sync_garmin.parse_args([]).backfill_history is False


def test_athlete_profile_sync_snapshots_garmin_thresholds(writes, monkeypatch):
    monkeypatch.setattr("tools.profile.get_athlete_profile", lambda: {"ftp": 262, "lactate_threshold_hr": 170})
    monkeypatch.setattr(db, "upsert_athlete_profile", lambda p: None)
    monkeypatch.setattr(db, "update_sync_state", lambda *a, **k: None)
    sync_garmin.sync_athlete_profile()
    (day, source, values), = writes["thresholds"]
    assert source == "garmin" and values["ftp"] == 262 and values["run_lthr"] == 170


def test_backfill_raises_when_a_part_failed(monkeypatch):
    monkeypatch.setattr("garmin_client.get_client", lambda: object())
    monkeypatch.setattr(hs, "backfill_history", lambda client, days: {"ftp": 3, "vo2max": "error: x"})
    with pytest.raises(RuntimeError, match="vo2max"):
        sync_garmin.backfill_history(30)
