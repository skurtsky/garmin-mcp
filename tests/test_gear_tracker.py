# tests/test_gear_tracker.py
"""Tests for the gear tracker (tools/gear_tracker.py, issue 53).

Fully offline: storage is redirected to a tmp JSON file via
GEAR_TRACKER_DATA_PATH and Garmin gear data is monkeypatched on
tools.profile.get_gear, so no live Garmin session is needed. Routes are
exercised with Starlette's TestClient.
"""
import json
import threading
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from tools import activities, gear_tracker, profile


@pytest.fixture(autouse=True)
def gear_db(tmp_path, monkeypatch):
    """Redirect gear-tracker storage at a tmp file for every test."""
    target = tmp_path / "gear-tracker" / "gear_data.json"
    monkeypatch.setenv("GEAR_TRACKER_DATA_PATH", str(target))
    return target


SAMPLE_GEAR = [
    {"name": "Canyon Ultimate", "model": None, "uuid": "bike-1", "activity_type": "Bike",
     "status": "active", "distance_km": 1000.0, "duration_min": 3000.0,
     "total_activities": 40, "max_distance_km": None,
     "date_begin": "2025-01-01", "date_end": None},
    {"name": "Nike Vaporfly", "model": None, "uuid": "shoe-1", "activity_type": "Shoes",
     "status": "active", "distance_km": 300.0, "duration_min": None,
     "total_activities": 20, "max_distance_km": 800.0,
     "date_begin": "2025-01-01", "date_end": None},
]


def _patch_gear(monkeypatch, gear=None):
    monkeypatch.setattr(profile, "get_gear", lambda: gear if gear is not None else SAMPLE_GEAR)


# A Garmin gear item registered as its own "Other"-typed part (issue 63) —
# e.g. a chain tracked separately from the bike it's mounted on, with its own
# real distance and replace-at threshold.
GEAR_WITH_LINKABLE_CHAIN = SAMPLE_GEAR + [
    {"name": "Shimano 12s Chain", "model": None, "uuid": "chain-1", "activity_type": "Other",
     "status": "active", "distance_km": 1969.68, "duration_min": None,
     "total_activities": 36, "max_distance_km": 3000.0,
     "date_begin": "2026-02-22", "date_end": None},
]


# ── COMPONENT STORAGE ─────────────────────────────────────────────────────────

def test_upsert_component_creates_with_default_interval():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain", bike_name="Canyon Ultimate")
    assert c["bike_uuid"] == "bike-1"
    assert c["bike_name"] == "Canyon Ultimate"
    assert c["maintenance_interval_km"] == 400


def test_upsert_component_unknown_name_has_no_default_interval():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Seat post")
    assert c["maintenance_interval_km"] is None


def test_upsert_component_explicit_none_overrides_default():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain", maintenance_interval_km=None)
    assert c["maintenance_interval_km"] is None


def test_upsert_component_matches_default_case_insensitively():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="CHAIN")
    assert c["maintenance_interval_km"] == 400


def test_upsert_component_updates_existing_by_name_instead_of_duplicating():
    first = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    second = gear_tracker.upsert_component(bike_uuid="bike-1", name="chain",
                                           maintenance_interval_km=500)
    assert second["id"] == first["id"]
    assert second["maintenance_interval_km"] == 500
    assert len(gear_tracker.list_components("bike-1")) == 1


def test_upsert_component_edit_by_id():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    edited = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain XT",
                                           component_id=c["id"])
    assert edited["id"] == c["id"]
    assert edited["name"] == "Chain XT"
    assert gear_tracker.get_component(c["id"])["name"] == "Chain XT"


def test_upsert_component_edit_keeps_interval_when_unset():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain",
                                      maintenance_interval_km=350)
    edited = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain XT",
                                           component_id=c["id"])
    assert edited["maintenance_interval_km"] == 350


def test_upsert_component_requires_bike_uuid():
    with pytest.raises(ValueError):
        gear_tracker.upsert_component(bike_uuid="", name="Chain")


def test_upsert_component_requires_name():
    with pytest.raises(ValueError):
        gear_tracker.upsert_component(bike_uuid="bike-1", name="   ")


def test_upsert_component_rejects_bad_install_date():
    with pytest.raises(ValueError):
        gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain", install_date="not-a-date")


def test_upsert_component_edit_unknown_id_raises():
    with pytest.raises(ValueError):
        gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain", component_id=999)


def test_find_component_is_case_insensitive():
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    assert gear_tracker.find_component("bike-1", "CHAIN") is not None
    assert gear_tracker.find_component("bike-1", "Tires") is None


def test_list_components_scoped_to_bike():
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    gear_tracker.upsert_component(bike_uuid="bike-2", name="Chain")
    assert len(gear_tracker.list_components("bike-1")) == 1
    assert len(gear_tracker.list_components()) == 2


# ── LINKING TO GARMIN-TRACKED GEAR (issue 63) ───────────────────────────────

def test_upsert_component_links_to_garmin_gear_and_defaults_name(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    c = gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")
    assert c["name"] == "Shimano 12s Chain"
    assert c["linked_gear_uuid"] == "chain-1"


def test_upsert_component_link_explicit_name_overrides_default(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Drivetrain chain",
                                      linked_gear_uuid="chain-1")
    assert c["name"] == "Drivetrain chain"


def test_upsert_component_no_name_and_no_link_still_requires_name():
    with pytest.raises(ValueError):
        gear_tracker.upsert_component(bike_uuid="bike-1")


def test_upsert_component_edit_keeps_link_when_field_omitted(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    c = gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")
    edited = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain (renamed)",
                                           component_id=c["id"])
    assert edited["linked_gear_uuid"] == "chain-1"


def test_upsert_component_edit_can_unlink_explicitly(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    c = gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")
    edited = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain",
                                           component_id=c["id"], linked_gear_uuid=None)
    assert edited["linked_gear_uuid"] is None


def test_upsert_component_unlinked_by_default():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    assert c["linked_gear_uuid"] is None


# ── COMPONENT TYPE DEFAULTS (issue 63 follow-up) ────────────────────────────

def test_upsert_component_type_used_as_name_when_no_link_or_name():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", component_type="Cassette")
    assert c["name"] == "Cassette"


def test_upsert_component_type_seeds_default_interval_when_unset():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", component_type="Cassette")
    assert c["maintenance_interval_km"] == 8000


@pytest.mark.parametrize("type_, interval", [
    ("Chain", 400), ("Cassette", 8000), ("Tire", 5000), ("Brakes", 2000),
])
def test_upsert_component_type_interval_matches_each_type(type_, interval):
    c = gear_tracker.upsert_component(bike_uuid="bike-1", component_type=type_)
    assert c["maintenance_interval_km"] == interval


def test_upsert_component_explicit_interval_overrides_type_default():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", component_type="Chain",
                                      maintenance_interval_km=999)
    assert c["maintenance_interval_km"] == 999


def test_upsert_component_explicit_name_overrides_type_default():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="My Chain", component_type="Chain")
    assert c["name"] == "My Chain"


def test_upsert_component_linked_gear_name_wins_over_type_for_name(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    c = gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1",
                                      component_type="Chain")
    assert c["name"] == "Shimano 12s Chain"


# ── JSON STORAGE (issue #60) ────────────────────────────────────────────────

def test_data_file_created_on_first_write(gear_db):
    assert not gear_db.exists()
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    assert gear_db.exists()
    with open(gear_db) as f:
        data = json.load(f)
    assert data["components"][0]["name"] == "Chain"


def test_data_persists_across_separate_reads(gear_db):
    """Nothing is cached in-process — a fresh read sees what an earlier
    write left on disk, simulating surviving a container restart."""
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    gear_tracker.log_maintenance_entry(component_id=c["id"], action="lubed",
                                       distance_at_service_km=100)

    assert gear_tracker.get_component(c["id"])["name"] == "Chain"
    assert len(gear_tracker.list_maintenance_log()) == 1


def test_write_leaves_no_tmp_file_behind(gear_db):
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    assert not (gear_db.parent / (gear_db.name + ".tmp")).exists()


def test_missing_data_file_reads_as_empty():
    assert gear_tracker.list_components() == []
    assert gear_tracker.list_maintenance_log() == []


def test_concurrent_maintenance_log_writes_dont_corrupt_or_lose_entries(gear_db):
    """Several maintenance-log submissions arriving at once (issue #60's
    'rapid-fire' scenario) must all land, and the file must stay valid JSON —
    the in-process _lock serializes the read-modify-write cycles."""
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")

    def log_one(i):
        gear_tracker.log_maintenance_entry(component_id=c["id"], action="lubed",
                                           distance_at_service_km=100 + i)

    threads = [threading.Thread(target=log_one, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(gear_db) as f:
        data = json.load(f)  # raises if the file was left corrupt
    assert len(data["maintenance_log"]) == 20
    assert len(gear_tracker.list_maintenance_log(component_id=c["id"])) == 20


# ── MAINTENANCE LOG ───────────────────────────────────────────────────────────

def test_log_maintenance_entry_requires_action():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    with pytest.raises(ValueError):
        gear_tracker.log_maintenance_entry(component_id=c["id"], action="",
                                           distance_at_service_km=100)


def test_log_maintenance_entry_unknown_component_raises():
    with pytest.raises(ValueError):
        gear_tracker.log_maintenance_entry(component_id=999, action="lubed",
                                           distance_at_service_km=100)


def test_log_maintenance_entry_rejects_bad_date():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    with pytest.raises(ValueError):
        gear_tracker.log_maintenance_entry(component_id=c["id"], action="lubed",
                                           distance_at_service_km=100, date_="08/01/2026")


def test_log_maintenance_entry_records_and_lists_newest_first():
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    gear_tracker.log_maintenance_entry(component_id=c["id"], action="lubed",
                                       distance_at_service_km=100, date_="2026-01-01")
    gear_tracker.log_maintenance_entry(component_id=c["id"], action="replaced",
                                       distance_at_service_km=500, date_="2026-06-01")

    entries = gear_tracker.list_maintenance_log(component_id=c["id"])
    assert [e["action"] for e in entries] == ["replaced", "lubed"]
    assert entries[0]["component_name"] == "Chain"
    assert entries[0]["distance_at_service_km"] == 500


# ── STATUS COMPUTATION ────────────────────────────────────────────────────────

@pytest.mark.parametrize("distance_km, expected", [
    (100, "green"), (499, "green"), (500, "yellow"), (600, "yellow"),
    (650, "orange"), (700, "orange"), (750, "red"), (900, "red"), (None, "unknown"),
])
def test_shoe_status_thresholds(distance_km, expected):
    assert gear_tracker._shoe_status(distance_km) == expected


@pytest.mark.parametrize("distance_since, interval, expected", [
    (60, 400, "green"), (240, 400, "green"), (241, 400, "yellow"),
    (399, 400, "yellow"), (400, 400, "red"), (500, 400, "red"),
    (100, None, "unknown"), (None, 400, "unknown"),
])
def test_component_status_thresholds(distance_since, interval, expected):
    assert gear_tracker._component_status(distance_since, interval) == expected


def test_build_gear_status_computes_distance_since_from_last_service(monkeypatch):
    _patch_gear(monkeypatch)
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain",
                                      maintenance_interval_km=400)
    gear_tracker.log_maintenance_entry(component_id=c["id"], action="lubed",
                                       distance_at_service_km=940)  # bike is at 1000km

    status = gear_tracker.build_gear_status()
    bike = next(g for g in status["gear"] if g["uuid"] == "bike-1")
    chain = bike["components"][0]
    assert chain["component_usage_km"] == 1000.0
    assert chain["services"][0]["distance_since_km"] == 60.0
    assert chain["ever_serviced"] is True


def test_build_gear_status_falls_back_to_install_distance_when_never_serviced(monkeypatch):
    _patch_gear(monkeypatch)
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain",
                                  install_distance_km=900, maintenance_interval_km=400)

    status = gear_tracker.build_gear_status()
    chain = status["gear"][0]["components"][0]
    assert chain["distance_since_km"] == 100.0
    assert chain["ever_serviced"] is False


def test_build_gear_status_bike_rolls_up_worst_component_status(monkeypatch):
    _patch_gear(monkeypatch)
    # bike is at 1000km (SAMPLE_GEAR): distance_since = 1000 - install_distance_km.
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain",
                                  install_distance_km=999, maintenance_interval_km=400)  # 1/400 -> green
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Cassette",
                                  install_distance_km=200, maintenance_interval_km=1000)  # 800/1000 -> yellow

    status = gear_tracker.build_gear_status()
    bike = next(g for g in status["gear"] if g["uuid"] == "bike-1")
    chain = next(c for c in bike["components"] if c["name"] == "Chain")
    cassette = next(c for c in bike["components"] if c["name"] == "Cassette")
    assert chain["status"] == "green"
    assert cassette["status"] == "yellow"
    assert bike["status_indicator"] == "yellow"


def test_build_gear_status_shoe_uses_distance_thresholds(monkeypatch):
    _patch_gear(monkeypatch)
    status = gear_tracker.build_gear_status()
    shoe = next(g for g in status["gear"] if g["uuid"] == "shoe-1")
    assert shoe["is_shoe"] is True
    assert shoe["status_indicator"] == "green"  # 300km < 500


def test_build_gear_status_filters_by_gear_name(monkeypatch):
    _patch_gear(monkeypatch)
    status = gear_tracker.build_gear_status(gear_name="Nike Vaporfly")
    assert [g["name"] for g in status["gear"]] == ["Nike Vaporfly"]


def test_build_gear_status_gear_name_is_case_insensitive(monkeypatch):
    _patch_gear(monkeypatch)
    status = gear_tracker.build_gear_status(gear_name="nike vaporfly")
    assert len(status["gear"]) == 1


def test_build_gear_status_untracked_bike_has_unknown_status(monkeypatch):
    _patch_gear(monkeypatch)
    status = gear_tracker.build_gear_status()
    bike = next(g for g in status["gear"] if g["uuid"] == "bike-1")
    assert bike["components"] == []
    assert bike["status_indicator"] == "unknown"


def test_build_gear_status_includes_maintenance_log(monkeypatch):
    """The dashboard's Gear tab needs both the component status and the
    maintenance log; build_gear_status returns both from the same
    connection/round trip instead of the caller making a second query
    (issue #58)."""
    _patch_gear(monkeypatch)
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain",
                                      bike_name="Canyon Ultimate")
    gear_tracker.log_maintenance_entry(component_id=c["id"], action="lubed",
                                       distance_at_service_km=100, date_="2026-01-01")

    status = gear_tracker.build_gear_status()
    assert len(status["maintenance_log"]) == 1
    entry = status["maintenance_log"][0]
    assert entry["action"] == "lubed"
    assert entry["component_name"] == "Chain"
    assert entry["bike_name"] == "Canyon Ultimate"


def test_build_gear_status_maintenance_log_respects_limit(monkeypatch):
    _patch_gear(monkeypatch)
    c = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    for i in range(3):
        gear_tracker.log_maintenance_entry(component_id=c["id"], action="lubed",
                                           distance_at_service_km=100 + i,
                                           date_=f"2026-01-0{i+1}")

    status = gear_tracker.build_gear_status(log_limit=2)
    assert len(status["maintenance_log"]) == 2


# ── LINKED COMPONENT STATUS (issue 63) ──────────────────────────────────────

def test_build_gear_status_linked_component_uses_linked_gears_own_distance(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")

    status = gear_tracker.build_gear_status()
    bike = next(g for g in status["gear"] if g["uuid"] == "bike-1")
    chain = bike["components"][0]
    # The linked gear's own distance (1969.68), not the bike's (1000.0).
    assert chain["distance_since_km"] == 1969.7


def test_build_gear_status_linked_component_interval_is_not_inherited_from_gear(monkeypatch):
    """The service interval is always the plain stored value — never
    re-derived from the linked gear's own max_distance_km (issue 63
    follow-up: it's a service interval, not an override)."""
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")

    status = gear_tracker.build_gear_status()
    chain = status["gear"][0]["components"][0]
    assert chain["maintenance_interval_km"] is None  # no component lifespan override
    assert chain["services"] == []  # service intervals are not inherited from Garmin
    assert chain["lifespan_km"] == 3000.0  # component lifespan comes from the linked gear
    assert "effective_interval_km" not in chain


def test_build_gear_status_linked_component_uses_its_own_stored_interval(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1",
                                  maintenance_interval_km=2500)

    status = gear_tracker.build_gear_status()
    chain = status["gear"][0]["components"][0]
    assert chain["maintenance_interval_km"] == 2500


def test_build_gear_status_linkable_gear_lists_unlinked_other_gear(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    status = gear_tracker.build_gear_status()
    assert [g["uuid"] for g in status["linkable_gear"]] == ["chain-1"]


def test_build_gear_status_linkable_gear_includes_date_begin(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    status = gear_tracker.build_gear_status()
    assert status["linkable_gear"][0]["date_begin"] == "2026-02-22"


def test_build_gear_status_linkable_gear_date_begin_none_when_missing(monkeypatch):
    gear = GEAR_WITH_LINKABLE_CHAIN[:2] + [{**GEAR_WITH_LINKABLE_CHAIN[2], "date_begin": None}]
    _patch_gear(monkeypatch, gear)
    status = gear_tracker.build_gear_status()
    assert status["linkable_gear"][0]["date_begin"] is None


# ── _iso_date_only (issue 63 follow-up) ─────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("2026-02-22", "2026-02-22"),
    ("2026-02-22T00:00:00.0", "2026-02-22"),
    (None, None),
    ("", None),
    ("not a date", None),
    ("26-2-22", None),
])
def test_iso_date_only(value, expected):
    assert gear_tracker._iso_date_only(value) == expected


def test_build_gear_status_linkable_gear_excludes_already_linked(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")
    status = gear_tracker.build_gear_status()
    assert status["linkable_gear"] == []


def test_build_gear_status_linkable_gear_excludes_retired(monkeypatch):
    retired = GEAR_WITH_LINKABLE_CHAIN[:2] + [{**GEAR_WITH_LINKABLE_CHAIN[2], "status": "retired"}]
    _patch_gear(monkeypatch, retired)
    status = gear_tracker.build_gear_status()
    assert status["linkable_gear"] == []


def test_build_gear_status_linkable_gear_excludes_bikes_and_shoes(monkeypatch):
    _patch_gear(monkeypatch)  # SAMPLE_GEAR: only a bike and a shoe
    status = gear_tracker.build_gear_status()
    assert status["linkable_gear"] == []


# ── MCP TOOL FUNCTIONS ────────────────────────────────────────────────────────

def test_log_maintenance_auto_creates_untracked_component(monkeypatch):
    _patch_gear(monkeypatch)
    result = gear_tracker.log_maintenance(gear_name="Canyon Ultimate", component="Chain",
                                          action="lubed", notes="squeaky")
    assert result == {
        "gear_name": "Canyon Ultimate", "component": "Chain", "action": "lubed",
        "date": result["date"], "distance_at_service_km": 1000.0, "notes": "squeaky",
    }
    comp = gear_tracker.find_component("bike-1", "Chain")
    assert comp["maintenance_interval_km"] == 400  # picked up the known default


def test_log_maintenance_reuses_existing_component(monkeypatch):
    _patch_gear(monkeypatch)
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain", maintenance_interval_km=999)
    gear_tracker.log_maintenance(gear_name="Canyon Ultimate", component="chain", action="lubed")

    assert len(gear_tracker.list_components("bike-1")) == 1
    comp = gear_tracker.find_component("bike-1", "Chain")
    assert comp["maintenance_interval_km"] == 999  # untouched, not reset to the default


def test_log_maintenance_unknown_gear_raises(monkeypatch):
    _patch_gear(monkeypatch)
    with pytest.raises(ValueError):
        gear_tracker.log_maintenance(gear_name="Nonexistent Bike", component="Chain", action="lubed")


def test_log_maintenance_uses_linked_gears_own_distance(monkeypatch):
    """An existing component that's linked to its own Garmin gear item (issue
    63) is judged against that item's distance — the logged service point has
    to use the same basis, or future 'distance since' would be nonsense."""
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain", bike_name="Canyon Ultimate",
                                  linked_gear_uuid="chain-1")

    result = gear_tracker.log_maintenance(gear_name="Canyon Ultimate", component="Chain", action="lubed")
    assert result["distance_at_service_km"] == 1969.68  # chain-1's own distance, not the bike's 1000.0


def test_get_maintenance_status_scopes_to_one_gear(monkeypatch):
    _patch_gear(monkeypatch)
    result = gear_tracker.get_maintenance_status(gear_name="Nike Vaporfly")
    assert len(result["gear"]) == 1
    assert result["gear"][0]["name"] == "Nike Vaporfly"


def test_get_maintenance_status_defaults_to_all_gear(monkeypatch):
    _patch_gear(monkeypatch)
    result = gear_tracker.get_maintenance_status()
    assert len(result["gear"]) == 2


# ── ROUTES ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    _patch_gear(monkeypatch)
    return TestClient(gear_tracker.create_app())


def test_gear_page_redirects_to_dashboard_gear_tab(client):
    resp = client.get(gear_tracker.PAGE_PATH, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard?tab=gear"


def test_gear_page_redirect_preserves_token(client):
    resp = client.get(gear_tracker.PAGE_PATH, params={"token": "t0k"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard?tab=gear&token=t0k"


def test_get_components_api_returns_json(client):
    resp = client.get(f"{gear_tracker.API_PREFIX}/components")
    assert resp.status_code == 200
    names = {g["name"] for g in resp.json()["gear"]}
    assert names == {"Canyon Ultimate", "Nike Vaporfly"}


def test_get_components_api_filters_by_gear_name(client):
    resp = client.get(f"{gear_tracker.API_PREFIX}/components", params={"gear_name": "Nike Vaporfly"})
    assert [g["name"] for g in resp.json()["gear"]] == ["Nike Vaporfly"]


def test_post_component_json_creates(client):
    resp = client.post(f"{gear_tracker.API_PREFIX}/components", json={
        "bike_uuid": "bike-1", "name": "Chain", "maintenance_interval_km": 350,
    })
    assert resp.status_code == 200
    assert resp.json()["maintenance_interval_km"] == 350


def test_post_component_form_redirects_to_dashboard_gear_tab(client):
    resp = client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "bike_uuid": "bike-1", "name": "Tires",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard?tab=gear"
    assert gear_tracker.find_component("bike-1", "Tires") is not None


def test_post_component_missing_bike_uuid_errors(client):
    resp = client.post(f"{gear_tracker.API_PREFIX}/components", json={"name": "Chain"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_component_json_links_to_garmin_gear(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    link_client = TestClient(gear_tracker.create_app())
    resp = link_client.post(f"{gear_tracker.API_PREFIX}/components", json={
        "bike_uuid": "bike-1", "linked_gear_uuid": "chain-1",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Shimano 12s Chain"
    assert body["linked_gear_uuid"] == "chain-1"


def test_post_component_edit_form_without_linked_field_keeps_link(monkeypatch):
    """The plain 'Edit component' form doesn't carry a linked_gear_uuid field
    at all — an absent key must keep the existing link, not clear it."""
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    link_client = TestClient(gear_tracker.create_app())
    comp = gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")

    resp = link_client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "component_id": comp["id"], "bike_uuid": "bike-1", "name": "Chain (renamed)",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert gear_tracker.get_component(comp["id"])["linked_gear_uuid"] == "chain-1"


def test_post_component_form_uuid_colon_name_value_needs_no_live_gear_lookup(monkeypatch):
    """The dashboard's Link form encodes each option as '<uuid>:<name>' so the
    submission carries the name it already rendered — the Link button was
    slow/stuck because upsert_component used to make a live get_gear() call
    just to resolve a name the page already had. Proven here by making any
    get_gear() call blow up: the POST must still succeed."""
    def _boom():
        raise AssertionError("get_gear() should not be called for this submission")
    monkeypatch.setattr(profile, "get_gear", _boom)
    link_client = TestClient(gear_tracker.create_app())

    resp = link_client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "bike_uuid": "bike-1", "linked_gear_uuid": "chain-1:Shimano 12s Chain",
    }, follow_redirects=False)
    assert resp.status_code == 303
    comp = gear_tracker.find_component("bike-1", "Shimano 12s Chain")
    assert comp is not None
    assert comp["linked_gear_uuid"] == "chain-1"


def test_post_component_form_custom_uses_type_for_name_and_interval(client):
    """The Link form's Type dropdown replaces free-text Name for the
    'Custom' (no Garmin link) path: it becomes the name, and seeds the
    Service interval default when that field is left blank."""
    resp = client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "bike_uuid": "bike-1", "component_type": "Tire",
    }, follow_redirects=False)
    assert resp.status_code == 303
    comp = gear_tracker.find_component("bike-1", "Tire")
    assert comp is not None
    assert comp["maintenance_interval_km"] == 5000


def test_post_component_form_blank_interval_on_create_does_not_force_none(client):
    """Regression check: a blank Service interval field on create used to be
    sent as an explicit None, which permanently skipped any default (Type's
    or the name-matched DEFAULT_INTERVALS_KM). It must default instead."""
    resp = client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "bike_uuid": "bike-1", "name": "Chain", "maintenance_interval_km": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    comp = gear_tracker.find_component("bike-1", "Chain")
    assert comp["maintenance_interval_km"] == 400  # DEFAULT_INTERVALS_KM["chain"]


def test_post_component_form_empty_linked_field_clears_link(monkeypatch):
    """The 'Link component' form's 'Custom' option submits an empty
    linked_gear_uuid — present-but-empty must explicitly clear the link."""
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    link_client = TestClient(gear_tracker.create_app())
    comp = gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")

    resp = link_client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "component_id": comp["id"], "bike_uuid": "bike-1", "name": "Chain",
        "linked_gear_uuid": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert gear_tracker.get_component(comp["id"])["linked_gear_uuid"] is None


def test_post_component_form_unlink_removes_component(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    link_client = TestClient(gear_tracker.create_app())
    comp = gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")

    resp = link_client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "component_id": comp["id"], "bike_uuid": "bike-1", "name": comp["name"],
        "unlink": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert gear_tracker.get_component(comp["id"]) is None


def test_post_component_json_uses_linked_gears_date_begin_as_install_date(monkeypatch):
    """The Link form has no Install date field (issue 63 follow-up) — it
    comes from the linked Garmin gear's own dateBegin, carried in the
    <select> option's encoded value."""
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    link_client = TestClient(gear_tracker.create_app())
    resp = link_client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "bike_uuid": "bike-1", "linked_gear_uuid": "chain-1:Shimano 12s Chain:2026-02-22",
    }, follow_redirects=False)
    assert resp.status_code == 303
    comp = gear_tracker.find_component("bike-1", "Shimano 12s Chain")
    assert comp["install_date"] == "2026-02-22"


def test_post_component_form_missing_date_begin_falls_back_to_today(monkeypatch):
    """No dateBegin on the Garmin item ('ignored if not found on the API') —
    falls back to the same 'today' default as before, not an error."""
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    link_client = TestClient(gear_tracker.create_app())
    resp = link_client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "bike_uuid": "bike-1", "linked_gear_uuid": "chain-1:Shimano 12s Chain:",
    }, follow_redirects=False)
    assert resp.status_code == 303
    comp = gear_tracker.find_component("bike-1", "Shimano 12s Chain")
    assert comp["install_date"] == date.today().isoformat()


def test_post_component_form_error_redirects_with_message(client):
    resp = client.post(f"{gear_tracker.API_PREFIX}/components", data={"name": "Chain"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def test_post_maintenance_json_captures_current_gear_distance(client):
    comp = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    resp = client.post(f"{gear_tracker.API_PREFIX}/maintenance", json={
        "component_id": comp["id"], "action": "lubed", "notes": "chain squeak",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["distance_at_service_km"] == 1000.0
    assert body["notes"] == "chain squeak"


def test_post_maintenance_form_redirects_and_persists(client):
    comp = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    resp = client.post(f"{gear_tracker.API_PREFIX}/maintenance", data={
        "component_id": comp["id"], "action": "replaced",
    }, follow_redirects=False)
    assert resp.status_code == 303

    entries = gear_tracker.list_maintenance_log(component_id=comp["id"])
    assert len(entries) == 1
    assert entries[0]["action"] == "replaced"
    assert entries[0]["distance_at_service_km"] == 1000.0


def test_post_maintenance_missing_component_id_errors(client):
    resp = client.post(f"{gear_tracker.API_PREFIX}/maintenance", json={"action": "lubed"})
    assert resp.status_code == 400


def test_post_maintenance_unknown_component_errors(client):
    resp = client.post(f"{gear_tracker.API_PREFIX}/maintenance", json={
        "component_id": 9999, "action": "lubed",
    })
    assert resp.status_code == 400


def test_post_maintenance_linked_component_uses_linked_gears_distance(monkeypatch):
    _patch_gear(monkeypatch, GEAR_WITH_LINKABLE_CHAIN)
    link_client = TestClient(gear_tracker.create_app())
    comp = gear_tracker.upsert_component(bike_uuid="bike-1", linked_gear_uuid="chain-1")

    resp = link_client.post(f"{gear_tracker.API_PREFIX}/maintenance", json={
        "component_id": comp["id"], "action": "lubed",
    })
    assert resp.status_code == 200
    assert resp.json()["distance_at_service_km"] == 1969.68


# ── BACKDATED MAINTENANCE (issue 63 follow-up) ──────────────────────────────
# Logging a service with a past date must anchor "distance since" at what the
# gear's distance was *on that day*, not today's live total — otherwise
# "distance since" reads as 0 right after logging even though rides happened
# between the service date and today.

def test_distance_ridden_since_returns_zero_for_today_or_future():
    assert gear_tracker._distance_ridden_since("bike-1", date.today().isoformat()) == 0.0
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert gear_tracker._distance_ridden_since("bike-1", tomorrow) == 0.0


def test_distance_ridden_since_sums_only_matching_gear_activities(monkeypatch):
    since = (date.today() - timedelta(days=5)).isoformat()

    def fake_get_activities(start_date=None, end_date=None, **kwargs):
        return [
            {"id": 1, "distance_km": 10.0},
            {"id": 2, "distance_km": 5.0},   # different gear — excluded
            {"id": 3, "distance_km": 7.0},
        ]

    def fake_get_activity_gear(activity_id):
        return [{"uuid": "bike-1"}] if activity_id in (1, 3) else [{"uuid": "other-bike"}]

    monkeypatch.setattr(activities, "get_activities", fake_get_activities)
    monkeypatch.setattr(profile, "get_activity_gear", fake_get_activity_gear)

    assert gear_tracker._distance_ridden_since("bike-1", since) == 17.0


def test_distance_ridden_since_falls_back_to_multisport_leg_for_hidden_gear(monkeypatch):
    """Garmin hangs gear off the individual legs of a multisport activity
    (triathlon, ...), not the parent — a bike ridden inside one must still
    be counted, via the leg-aware fallback for type == 'multi_sport'."""
    since = (date.today() - timedelta(days=5)).isoformat()

    def fake_get_activities(start_date=None, end_date=None, **kwargs):
        return [{"id": 1, "distance_km": 51.5, "type": "multi_sport"}]

    def fake_get_activity_gear(activity_id):
        return []  # gear never sits on the multisport parent itself

    def fake_leg_distance(activity_id, gear_uuid):
        assert activity_id == 1
        assert gear_uuid == "bike-1"
        return 40.2  # just the bike leg, not the whole triathlon's distance

    monkeypatch.setattr(activities, "get_activities", fake_get_activities)
    monkeypatch.setattr(profile, "get_activity_gear", fake_get_activity_gear)
    monkeypatch.setattr(activities, "get_multisport_leg_distance_for_gear", fake_leg_distance)

    assert gear_tracker._distance_ridden_since("bike-1", since) == 40.2


def test_distance_ridden_since_multisport_fallback_finds_no_matching_leg(monkeypatch):
    since = (date.today() - timedelta(days=5)).isoformat()

    monkeypatch.setattr(activities, "get_activities", lambda **kw: [
        {"id": 1, "distance_km": 51.5, "type": "multi_sport"},
    ])
    monkeypatch.setattr(profile, "get_activity_gear", lambda activity_id: [])
    monkeypatch.setattr(activities, "get_multisport_leg_distance_for_gear",
                        lambda activity_id, gear_uuid: None)  # no leg used this gear

    assert gear_tracker._distance_ridden_since("bike-1", since) == 0.0


def test_distance_ridden_since_skips_multisport_fallback_when_already_matched(monkeypatch):
    """No need to pay for the leg-aware fallback when the cheap top-level
    check already found the gear."""
    since = (date.today() - timedelta(days=5)).isoformat()

    def _boom(*a, **kw):
        raise AssertionError("multisport fallback should not run when already matched")

    monkeypatch.setattr(activities, "get_activities", lambda **kw: [
        {"id": 1, "distance_km": 51.5, "type": "multi_sport"},
    ])
    monkeypatch.setattr(profile, "get_activity_gear", lambda activity_id: [{"uuid": "bike-1"}])
    monkeypatch.setattr(activities, "get_multisport_leg_distance_for_gear", _boom)

    assert gear_tracker._distance_ridden_since("bike-1", since) == 51.5


def test_distance_ridden_since_skips_multisport_fallback_for_non_multisport(monkeypatch):
    """An ordinary (non-multisport) activity that doesn't match must not pay
    for the leg-aware fallback either — it's scoped to type == 'multi_sport'."""
    since = (date.today() - timedelta(days=5)).isoformat()

    def _boom(*a, **kw):
        raise AssertionError("multisport fallback should not run for a non-multisport activity")

    monkeypatch.setattr(activities, "get_activities", lambda **kw: [
        {"id": 1, "distance_km": 10.0, "type": "road_biking"},
    ])
    monkeypatch.setattr(profile, "get_activity_gear", lambda activity_id: [{"uuid": "other-bike"}])
    monkeypatch.setattr(activities, "get_multisport_leg_distance_for_gear", _boom)

    assert gear_tracker._distance_ridden_since("bike-1", since) == 0.0


def test_distance_ridden_since_caps_lookback_without_calling_garmin(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("get_activities should not be called past the lookback cap")
    monkeypatch.setattr(activities, "get_activities", _boom)

    far_past = (date.today() - timedelta(days=gear_tracker._BACKDATE_LOOKBACK_CAP_DAYS + 5)).isoformat()
    assert gear_tracker._distance_ridden_since("bike-1", far_past) == 0.0


def test_post_maintenance_backdated_date_subtracts_rides_since(monkeypatch):
    _patch_gear(monkeypatch)  # SAMPLE_GEAR: bike-1 currently at 1000.0 km
    service_date = (date.today() - timedelta(days=3)).isoformat()

    def fake_get_activities(start_date=None, end_date=None, **kwargs):
        return [{"id": 1, "distance_km": 30.0}, {"id": 2, "distance_km": 20.0}]

    def fake_get_activity_gear(activity_id):
        return [{"uuid": "bike-1"}]

    monkeypatch.setattr(activities, "get_activities", fake_get_activities)
    monkeypatch.setattr(profile, "get_activity_gear", fake_get_activity_gear)

    comp = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    maint_client = TestClient(gear_tracker.create_app())
    resp = maint_client.post(f"{gear_tracker.API_PREFIX}/maintenance", json={
        "component_id": comp["id"], "action": "lubed", "date": service_date,
    })
    assert resp.status_code == 200
    # 1000 (current) - 50 (30 + 20 ridden since the service date) = 950
    assert resp.json()["distance_at_service_km"] == 950.0

    status = gear_tracker.build_gear_status()
    chain = status["gear"][0]["components"][0]
    # distance since the (backdated) service = current 1000 - anchor 950 = 50,
    # matching the rides that happened after it — not 0.
    assert chain["services"][0]["distance_since_km"] == 50.0


def test_post_maintenance_todays_date_is_unaffected_by_backdating_logic(monkeypatch):
    """A same-day submission must not go through the ridden-since correction
    at all (and shouldn't need get_activities called for it)."""
    def _boom(*a, **kw):
        raise AssertionError("get_activities should not be called for a same-day entry")
    monkeypatch.setattr(activities, "get_activities", _boom)
    _patch_gear(monkeypatch)

    comp = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    maint_client = TestClient(gear_tracker.create_app())
    resp = maint_client.post(f"{gear_tracker.API_PREFIX}/maintenance", json={
        "component_id": comp["id"], "action": "lubed", "date": date.today().isoformat(),
    })
    assert resp.status_code == 200
    assert resp.json()["distance_at_service_km"] == 1000.0


def test_post_maintenance_redirect_preserves_token(client):
    comp = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    resp = client.post(
        f"{gear_tracker.API_PREFIX}/maintenance?token=t0k",
        data={"component_id": comp["id"], "action": "lubed"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard?tab=gear&token=t0k"


def test_dashboard_gear_url():
    assert gear_tracker._dashboard_gear_url(None) == "/dashboard?tab=gear"
    assert gear_tracker._dashboard_gear_url("t0k") == "/dashboard?tab=gear&token=t0k"


def test_error_redirect_url():
    assert gear_tracker._error_redirect_url(None, "boom") == "/dashboard?tab=gear&error=boom"
    assert gear_tracker._error_redirect_url("t0k", "boom") == "/dashboard?tab=gear&error=boom&token=t0k"


def test_owns_path():
    assert gear_tracker.owns_path("/dashboard/gear") is True
    assert gear_tracker.owns_path("/api/gear/components") is True
    assert gear_tracker.owns_path("/api/gear/maintenance") is True
    assert gear_tracker.owns_path("/dashboard") is False
    assert gear_tracker.owns_path("/training-plan") is False
