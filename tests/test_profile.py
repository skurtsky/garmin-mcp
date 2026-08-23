# tests/test_profile.py
from tools.profile import (
    get_athlete_profile,
    get_gear,
    get_activity_gear,
    _activity_gear_item,
)

def test_get_athlete_profile_returns_dict():
    """Profile should return a dict with expected keys."""
    profile = get_athlete_profile()
    assert isinstance(profile, dict)

def test_get_athlete_profile_has_required_keys():
    """Profile should contain all expected fields."""
    profile = get_athlete_profile()
    expected_keys = [
        'weight_kg', 'height_cm', 'vo2max_running', 'vo2max_cycling',
        'lactate_threshold_hr', 'lactate_threshold_pace', 'ftp',
        'resting_hr_7day_avg',
    ]
    for key in expected_keys:
        assert key in profile, f"Missing key: {key}"

def test_get_athlete_profile_weight_is_reasonable():
    """Weight should be a plausible value in kg."""
    profile = get_athlete_profile()
    assert profile['weight_kg'] is not None
    assert 30 < profile['weight_kg'] < 200

def test_get_athlete_profile_vo2max_is_reasonable():
    """VO2max values should be in a plausible range."""
    profile = get_athlete_profile()
    assert 20 < profile['vo2max_running'] < 90
    assert 20 < profile['vo2max_cycling'] < 90

def test_get_athlete_profile_lt_pace_is_reasonable():
    """LT pace should be a plausible min/km value."""
    profile = get_athlete_profile()
    assert profile['lactate_threshold_pace'] is not None
    assert 3.0 < profile['lactate_threshold_pace'] < 8.0

def test_get_gear_returns_list():
    """Gear should return a list."""
    gear = get_gear()
    assert isinstance(gear, list)

def test_get_gear_items_have_required_keys():
    """Each gear item should have the expected fields."""
    gear = get_gear()
    if not gear:
        return  # account may have no registered gear
    expected = ['name', 'model', 'activity_type', 'status',
                'distance_km', 'total_activities', 'max_distance_km']
    for item in gear:
        for key in expected:
            assert key in item, f"Missing key: {key}"

def test_get_gear_distance_is_non_negative():
    """Reported distance should not be negative."""
    gear = get_gear()
    for item in gear:
        if item['distance_km'] is not None:
            assert item['distance_km'] >= 0


def test_activity_gear_item_shape():
    """Raw gear DTO should map to name/uuid/type/distance_km/lifespan_km."""
    item = _activity_gear_item(
        {
            'displayName': 'Nike Vaporfly 3', 'uuid': 'abc-123',
            'gearTypeName': 'Shoes', 'maximumMeters': 500000.0,
        },
        {'totalDistance': 423512.0},
    )
    assert item == {
        'name': 'Nike Vaporfly 3',
        'uuid': 'abc-123',
        'type': 'Shoes',
        'distance_km': 423.51,
        'lifespan_km': 500.0,
    }

def test_activity_gear_item_falls_back_to_make_model():
    """Gear without a custom display name should fall back to make/model."""
    item = _activity_gear_item({'customMakeModel': 'Canyon Ultimate'}, {})
    assert item['name'] == 'Canyon Ultimate'
    assert item['distance_km'] == 0

def test_get_activity_gear_returns_list(run_activity_id):
    """Activity gear should be a list, empty when nothing is assigned."""
    gear = get_activity_gear(run_activity_id)
    assert isinstance(gear, list)

def test_get_activity_gear_items_have_required_keys(run_activity_id):
    """Each activity gear item should carry the expected fields."""
    gear = get_activity_gear(run_activity_id)
    if not gear:
        return  # activity may have no gear assigned
    for item in gear:
        for key in ['name', 'uuid', 'type', 'distance_km']:
            assert key in item, f"Missing key: {key}"
        assert item['distance_km'] >= 0


def test_get_athlete_profile_ftp_is_populated():
    """FTP should be fetched from the cycling FTP endpoint (not null)."""
    profile = get_athlete_profile()
    assert profile['ftp'] is not None
    assert 50 < profile['ftp'] < 600, f"FTP {profile['ftp']} outside expected range"


def test_get_athlete_profile_resting_hr_7day_avg_is_reasonable():
    """7-day average resting HR should be a plausible value."""
    profile = get_athlete_profile()
    assert profile['resting_hr_7day_avg'] is not None
    assert 30 < profile['resting_hr_7day_avg'] < 100, (
        f"Resting HR 7-day avg {profile['resting_hr_7day_avg']} outside expected range"
    )
