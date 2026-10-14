# tests/test_sync_garmin.py
"""Tests for sync_garmin.py's --overwrite flag (issue 74 feedback): a
one-time backfill for activity_details rows synced before a schema change
(e.g. hr_zones, sub_activities) so old rows can pick up the new fields
without waiting for get_activity_ids_needing_detail's normal staleness
check, which only revisits a *missing* or *stale* detail row.
"""
import db
import sync_garmin


def test_parse_args_overwrite_defaults_false():
    args = sync_garmin.parse_args([])
    assert args.overwrite is False


def test_parse_args_overwrite_flag():
    args = sync_garmin.parse_args(["--details-only", "--detail-limit", "999", "--overwrite"])
    assert args.overwrite is True
    assert args.detail_limit == 999


def test_sync_activity_details_uses_needing_detail_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "get_activity_ids_needing_detail", lambda limit: calls.append(("needing", limit)) or [])
    monkeypatch.setattr(db, "get_recent_activity_ids", lambda limit: calls.append(("recent", limit)) or [])
    monkeypatch.setattr(db, "update_sync_state", lambda *a, **k: None)

    sync_garmin.sync_activity_details(limit=5)

    assert calls == [("needing", 5)]


def test_sync_activity_details_overwrite_uses_recent_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "get_activity_ids_needing_detail", lambda limit: calls.append(("needing", limit)) or [])
    monkeypatch.setattr(db, "get_recent_activity_ids", lambda limit: calls.append(("recent", limit)) or [])
    monkeypatch.setattr(db, "update_sync_state", lambda *a, **k: None)

    sync_garmin.sync_activity_details(limit=999, overwrite=True)

    assert calls == [("recent", 999)]


def test_sync_activity_details_overwrite_refetches_every_returned_id(monkeypatch):
    fetched = []
    monkeypatch.setattr(db, "get_recent_activity_ids", lambda limit: [1, 2, 3])
    monkeypatch.setattr(db, "upsert_activity_detail", lambda garmin_id, detail, route: fetched.append(garmin_id))
    monkeypatch.setattr(db, "update_sync_state", lambda *a, **k: None)
    monkeypatch.setattr(
        "tools.activities.get_activity_detail_row",
        lambda activity_id: ({"hr_zones": []}, None),
    )

    sync_garmin.sync_activity_details(limit=10, overwrite=True)

    assert fetched == [1, 2, 3]


def test_prune_recent_sync_only_reaches_back_to_the_oldest_activity_returned(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "delete_activities_not_in", lambda keep, **kw: calls.append((keep, kw)) or [])
    acts = [{"id": 5, "date": "2026-10-13 07:00:00"}, {"id": 4, "date": "2026-10-10 18:30:00"}]

    sync_garmin.prune_deleted_activities(acts)

    assert calls == [([5, 4], {"after": "2026-10-10 18:30:00"})]


def test_prune_range_sync_covers_the_whole_range(monkeypatch):
    from datetime import date
    calls = []
    monkeypatch.setattr(db, "delete_activities_not_in", lambda keep, **kw: calls.append((keep, kw)) or [])

    sync_garmin.prune_deleted_activities([{"id": 7, "date": "2026-09-02 07:00:00"}],
                                         date(2026, 9, 1), date(2026, 9, 30))

    assert calls == [([7], {"start": "2026-09-01", "end": "2026-10-01"})]
