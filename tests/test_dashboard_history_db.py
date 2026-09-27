# tests/test_dashboard_history_db.py
"""The dashboard-history SQL in db.py (snapshots, record history, best
efforts, daily loads, overrides, settings) against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL points at a disposable database — every
test drops the tables it uses first:

    TEST_DATABASE_URL=postgresql://postgres@localhost/garmin_test pytest tests/test_dashboard_history_db.py
"""
import os

import pytest

import db
from tools import goal_race

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

_TABLES = ("threshold_snapshots, race_prediction_snapshots, personal_record_history, "
           "activity_best_efforts, activity_overrides, app_settings, personal_records, "
           "activity_details, activities, daily_metrics")


@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(db, "_pool", None)
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {_TABLES}")
    db.ensure_schema()
    yield
    db._pool.close()


def _activity(gid, local, kind="running", load=50.0, km=10.0, **summary):
    db.upsert_activity(gid, activity_date=local.replace(" ", "T") + "+00:00", activity_type=kind,
                       name=f"A{gid}", distance_km=km, duration_min=60, avg_hr=140, training_load=load,
                       summary={"id": gid, "date": local, "training_load": load, **summary})


def test_threshold_and_prediction_snapshots_upsert():
    db.upsert_threshold_snapshot("2026-09-24", "garmin", {"ftp": 250, "run_lthr": None})
    db.upsert_threshold_snapshot("2026-09-24", "garmin", {"ftp": 262})
    db.upsert_threshold_snapshot("2026-09-24", "plan", {"ftp": 248, "css_s": 121})
    rows = db.get_threshold_snapshots("2026-09-01", "2026-09-30")
    assert [(r["source"], r["metric"], r["value"]) for r in rows] == [
        ("garmin", "ftp", 262.0), ("plan", "css_s", 121.0), ("plan", "ftp", 248.0)]

    db.upsert_race_predictions("2026-09-24", {"5K": 1205, "marathon": None})
    db.upsert_race_predictions("2026-09-24", {"5K": 1200})
    assert [(r["distance"], r["seconds"]) for r in db.get_race_prediction_snapshots("2026-09-24", "2026-09-24")] == [("5K", 1200)]


def test_record_history_logs_only_changes():
    rec = {"label": "Fastest 5K", "value_raw": 1235.0, "value_formatted": "20:35",
           "date": "2026-05-01 09:00:00", "activity_id": 1}
    db.upsert_personal_records({"running": [rec]})          # first sight: no history
    db.upsert_personal_records({"running": [rec]})          # unchanged: no history
    assert db.get_personal_record_history("2000-01-01") == []

    better = {**rec, "value_raw": 1205.0, "value_formatted": "20:05", "date": "2026-09-20 08:00:00", "activity_id": 2}
    db.upsert_personal_records({"running": [better]})
    (row,) = db.get_personal_record_history("2026-09-01")
    assert (row["value_raw"], row["previous_value_raw"], row["previous_formatted"]) == (1205.0, 1235.0, "20:35")
    assert str(row["previous_record_date"]) == "2026-05-01"
    assert str(row["record_date"]) == "2026-09-20"


def test_best_efforts_replaced_with_detail_and_dated_locally():
    _activity(10, "2026-09-20 23:30:00")
    db.upsert_activity_detail(10, {"best_efforts": [
        {"sport": "running", "record_type": "Fastest 5K", "value": 1210.0},
        {"sport": "running", "record_type": "Fastest 1K", "value": 230.0}]}, None)
    db.upsert_activity_detail(10, {"best_efforts": [
        {"sport": "running", "record_type": "Fastest 5K", "value": 1208.0}]}, None)
    (effort,) = db.get_best_efforts("2026-09-01")
    assert (effort["record_type"], effort["value"], str(effort["activity_date"]), effort["name"]) == \
        ("Fastest 5K", 1208.0, "2026-09-20", "A10")
    db.upsert_activity_detail(10, {"hr_zones": []}, None)   # older payload shape: clears efforts
    assert db.get_best_efforts("2026-09-01") == []


def test_daily_loads_by_local_date():
    _activity(1, "2026-09-20 07:00:00", load=40)
    _activity(2, "2026-09-20 18:00:00", load=60)
    _activity(3, "2026-09-21 06:00:00", load=None)
    _activity(4, "2026-09-25 06:00:00", load=99)
    assert db.get_daily_activity_loads("2026-09-20", "2026-09-21") == {"2026-09-20": 100.0, "2026-09-21": 0.0}


def test_rides_and_ride_ftp_history():
    _activity(1, "2026-09-20 07:00:00", kind="road_biking", start_lat=45.4)
    _activity(2, "2026-09-20 18:00:00", kind="running")
    _activity(3, "2026-09-21 06:00:00", kind="indoor_cycling")
    assert [r["garmin_id"] for r in db.get_rides_in_range("2026-09-01", "2026-10-01")] == [1, 3]
    db.upsert_activity_detail(1, {"ftp": 255}, None)
    db.upsert_activity_detail(3, {"ftp": 260}, None)
    assert [(r["day"], r["ftp"]) for r in db.get_ride_ftp_history("2026-09-01", "2026-09-30")] == [
        ("2026-09-20", 255.0), ("2026-09-21", 260.0)]


def test_commute_overrides():
    db.set_commute_override(5, True)
    db.set_commute_override(6, False)
    db.set_commute_override(6, True)
    assert {k: v["is_commute"] for k, v in db.get_activity_overrides([5, 6, 7]).items()} == {5: True, 6: True}
    db.set_commute_override(5, None)
    assert list(db.get_activity_overrides([5, 6])) == [6]


def test_goal_race_setting_round_trip():
    assert goal_race.get_goal_race() is None
    saved = goal_race.save_goal_race({"name": "Fall Classic", "date": "2026-10-11", "distance": "half",
                                      "target": "1:32:00"})
    assert goal_race.get_goal_race() == saved
    goal_race.clear_goal_race()
    assert goal_race.get_goal_race() is None


def test_vo2max_history_from_daily_metrics():
    db.upsert_daily_metric("2026-09-20", training_status_data={"vo2max": {"running": 54.6, "cycling": None}})
    (row,) = db.get_vo2max_history_from_daily_metrics("2026-09-01", "2026-09-30")
    assert (row["running"], row["cycling"]) == (pytest.approx(54.6), None)


def test_close_pool_is_idempotent_and_reopens():
    db.get_setting("anything")
    pool = db._pool
    db.close_pool()
    assert db._pool is None and pool.closed
    db.close_pool()
    assert db.get_setting("anything") is None   # a fresh pool opens on demand
