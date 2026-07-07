# tests/test_health.py
from tools.health import (
    get_sleep,
    get_daily_readiness,
    get_daily_health,
    get_training_status,
    get_training_readiness,
)

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
    assert 'date' in result
    assert 'hrv' in result
    assert 'body_battery' in result
    assert 'daily_stats' in result

def test_get_daily_readiness_hrv_is_reasonable(test_date):
    result = get_daily_readiness(test_date)
    hrv = result['hrv']
    assert hrv['last_night_avg'] is not None
    assert 20 < hrv['last_night_avg'] < 120
    assert hrv['status'] in ('BALANCED', 'UNBALANCED', 'LOW', 'POOR')


def test_get_daily_readiness_body_battery_has_start_level_key(test_date):
    result = get_daily_readiness(test_date)
    bb = result['body_battery']
    # keys must exist; values may be None if Garmin doesn't provide them
    assert 'start_level' in bb
    assert 'current_level' in bb
    assert 'highest' in bb

def test_get_daily_readiness_has_daily_stats(test_date):
    result = get_daily_readiness(test_date)
    assert 'daily_stats' in result
    stats = result['daily_stats']
    assert 'resting_hr' in stats
    assert 'avg_stress' in stats

def test_get_training_readiness_returns_dict(test_date):
    result = get_training_readiness(test_date)
    assert isinstance(result, dict)

def test_get_training_readiness_has_required_keys(test_date):
    result = get_training_readiness(test_date)
    assert 'date' in result
    assert 'readiness' in result
    assert 'morning' in result
    readiness = result['readiness']
    for key in ('score', 'level', 'feedback_long', 'feedback_short'):
        assert key in readiness, f"Missing readiness key: {key}"

def test_get_training_readiness_score_in_range(test_date):
    result = get_training_readiness(test_date)
    score = result['readiness'].get('score')
    if score is not None:
        assert 0 <= score <= 100

def test_get_training_status_returns_dict(test_date):
    result = get_training_status(test_date)
    assert isinstance(result, dict)

def test_get_training_status_has_required_keys(test_date):
    result = get_training_status(test_date)
    for key in ('acwr', 'load_balance', 'status', 'vo2max'):
        assert key in result, f"Missing key: {key}"

def test_get_training_status_acwr_is_reasonable(test_date):
    result = get_training_status(test_date)
    assert result['acwr'] is not None
    assert 0 < result['acwr'] < 3

def test_get_daily_health_returns_dict(test_date):
    result = get_daily_health(test_date)
    assert isinstance(result, dict)

def test_get_daily_health_has_required_keys(test_date):
    result = get_daily_health(test_date)
    for key in ('date', 'heart_rate', 'stress', 'body_battery', 'respiration'):
        assert key in result, f"Missing key: {key}"

def test_get_daily_health_heart_rate_is_reasonable(test_date):
    result = get_daily_health(test_date)
    hr = result['heart_rate']
    for key in ('resting_hr', 'max_hr', 'min_hr', 'seven_day_avg_resting_hr'):
        assert key in hr, f"Missing heart_rate key: {key}"
    assert 30 < hr['resting_hr'] < 100
    assert hr['min_hr'] <= hr['resting_hr'] <= hr['max_hr']

def test_get_daily_health_stress_is_reasonable(test_date):
    result = get_daily_health(test_date)
    stress = result['stress']
    for key in ('avg_stress', 'max_stress', 'rest_stress_mins',
                'low_stress_mins', 'medium_stress_mins', 'high_stress_mins'):
        assert key in stress, f"Missing stress key: {key}"
    assert 0 <= stress['avg_stress'] <= 100
    assert 0 <= stress['max_stress'] <= 100

def test_get_daily_health_body_battery_has_keys(test_date):
    result = get_daily_health(test_date)
    bb = result['body_battery']
    assert 'charged' in bb
    assert 'drained' in bb

def test_get_daily_health_respiration_is_reasonable(test_date):
    result = get_daily_health(test_date)
    resp = result['respiration']
    for key in ('avg_waking', 'avg_sleep', 'highest', 'lowest'):
        assert key in resp, f"Missing respiration key: {key}"
    assert 5 < resp['avg_waking'] < 30
