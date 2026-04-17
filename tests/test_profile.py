# tests/test_profile.py
from tools.profile import get_athlete_profile

def test_get_athlete_profile_returns_dict():
    """Profile should return a dict with expected keys."""
    profile = get_athlete_profile()
    assert isinstance(profile, dict)

def test_get_athlete_profile_has_required_keys():
    """Profile should contain all expected fields."""
    profile = get_athlete_profile()
    expected_keys = [
        'weight_kg', 'height_cm', 'vo2max_running', 'vo2max_cycling',
        'lactate_threshold_hr', 'lactate_threshold_pace', 'ftp'
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