# tests/test_health.py
from tools.health import get_sleep, get_daily_readiness

def test_get_sleep_returns_dict(test_date):
    result = get_sleep(test_date)
    assert isinstance(result, dict)

def test_get_sleep_has_required_keys(test_date):
    result = get_sleep(test_date)
    expected = [
        'date', 'sleep_score', 'total_sleep_hrs', 'deep_sleep_hrs',
        'rem_sleep_hrs', 'awake_count', 'avg_hr', 'resting_hr',
        'avg_hrv', 'hrv_status', 'body_battery_change'
    ]
    for key in expected:
        assert key in result, f"Missing key: {key}"

def test_get_sleep_values_are_reasonable(test_date):
    result = get_sleep(test_date)
    assert 0 < result['total_sleep_hrs'] < 14
    assert 0 <= result['deep_pct'] <= 100
    assert 0 <= result['rem_pct'] <= 100
    assert 0 <= result['light_pct'] <= 100
    assert 30 < result['avg_hr'] < 100

def test_get_daily_readiness_returns_dict(test_date):
    result = get_daily_readiness(test_date)
    assert isinstance(result, dict)

def test_get_daily_readiness_has_required_keys(test_date):
    result = get_daily_readiness(test_date)
    assert 'hrv' in result
    assert 'body_battery' in result
    assert 'training_status' in result
    assert 'vo2max' in result

def test_get_daily_readiness_hrv_is_reasonable(test_date):
    result = get_daily_readiness(test_date)
    hrv = result['hrv']
    assert hrv['last_night_avg'] is not None
    assert 20 < hrv['last_night_avg'] < 120
    assert hrv['status'] in ('BALANCED', 'UNBALANCED', 'LOW', 'POOR')

def test_get_daily_readiness_training_status_has_acwr(test_date):
    result = get_daily_readiness(test_date)
    ts = result['training_status']
    assert ts['acwr'] is not None
    assert 0 < ts['acwr'] < 3

def test_get_daily_readiness_body_battery_has_start_level_key(test_date):
    result = get_daily_readiness(test_date)
    bb = result['body_battery']
    # start_level key must exist; value may be None if Garmin doesn't provide it
    assert 'start_level' in bb