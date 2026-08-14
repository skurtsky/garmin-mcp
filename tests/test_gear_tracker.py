# tests/test_gear_tracker.py
"""Tests for the gear tracker (tools/gear_tracker.py, issue 53).

Fully offline: storage is redirected to a tmp SQLite file via
GEAR_TRACKER_DB_PATH and Garmin gear data is monkeypatched on
tools.profile.get_gear, so no live Garmin session is needed. Routes are
exercised with Starlette's TestClient.
"""
import pytest
from starlette.testclient import TestClient

from tools import gear_tracker, profile


@pytest.fixture(autouse=True)
def gear_db(tmp_path, monkeypatch):
    """Redirect gear-tracker storage at a tmp file for every test."""
    target = tmp_path / "gear-tracker.db"
    monkeypatch.setenv("GEAR_TRACKER_DB_PATH", str(target))
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
    assert chain["distance_since_km"] == 60.0
    assert chain["status"] == "green"
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


def test_gear_page_renders(client):
    resp = client.get(gear_tracker.PAGE_PATH)
    assert resp.status_code == 200
    assert "Gear tracker" in resp.text
    assert "Canyon Ultimate" in resp.text
    assert "Nike Vaporfly" in resp.text


def test_gear_page_shows_error_banner(client):
    resp = client.get(gear_tracker.PAGE_PATH, params={"error": "Something went wrong"})
    assert "Something went wrong" in resp.text


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


def test_post_component_form_redirects_to_gear_page(client):
    resp = client.post(f"{gear_tracker.API_PREFIX}/components", data={
        "bike_uuid": "bike-1", "name": "Tires",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(gear_tracker.PAGE_PATH)
    assert gear_tracker.find_component("bike-1", "Tires") is not None


def test_post_component_missing_bike_uuid_errors(client):
    resp = client.post(f"{gear_tracker.API_PREFIX}/components", json={"name": "Chain"})
    assert resp.status_code == 400
    assert "error" in resp.json()


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


def test_post_maintenance_redirect_preserves_token(client):
    comp = gear_tracker.upsert_component(bike_uuid="bike-1", name="Chain")
    resp = client.post(
        f"{gear_tracker.API_PREFIX}/maintenance?token=t0k",
        data={"component_id": comp["id"], "action": "lubed"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "token=t0k" in resp.headers["location"]


def test_owns_path():
    assert gear_tracker.owns_path("/dashboard/gear") is True
    assert gear_tracker.owns_path("/api/gear/components") is True
    assert gear_tracker.owns_path("/api/gear/maintenance") is True
    assert gear_tracker.owns_path("/dashboard") is False
    assert gear_tracker.owns_path("/training-plan") is False
