# tests/test_trends.py
import pytest
from tools.trends import (
    get_performance_predictions,
    get_performance_trends,
    get_trends,
)
def test_get_performance_predictions_returns_dict(client):
    result = get_performance_predictions()
    assert isinstance(result, dict)

def test_get_performance_predictions_has_predictions_list(client):
    result = get_performance_predictions()
    assert 'predictions' in result
    assert isinstance(result['predictions'], list)

def test_get_performance_predictions_items_have_required_keys(client):
    result = get_performance_predictions()
    for p in result['predictions']:
        assert 'distance' in p
        assert 'predicted_time' in p


def test_get_performance_trends_weekly_returns_list(client):
    result = get_performance_trends(period='weekly', lookback=4)
    assert isinstance(result, list)
    assert len(result) == 4

def test_get_performance_trends_monthly_returns_list(client):
    result = get_performance_trends(period='monthly', lookback=3)
    assert isinstance(result, list)
    assert len(result) == 3

def test_get_performance_trends_item_structure(client):
    result = get_performance_trends(period='weekly', lookback=2)
    assert len(result) > 0
    item = result[0]
    assert 'period_end' in item
    assert 'hrv' in item
    assert 'vo2max' in item
    assert 'weekly_avg' in item['hrv']
    assert 'running' in item['vo2max']
    assert 'cycling' in item['vo2max']

def test_get_performance_trends_respects_lookback_cap(client):
    result = get_performance_trends(period='weekly', lookback=100)
    assert len(result) <= 26
    result = get_performance_trends(period='monthly', lookback=100)
    assert len(result) <= 12

def test_get_performance_trends_invalid_period(client):
    with pytest.raises(ValueError):
        get_performance_trends(period='daily', lookback=4)


# ── get_trends ──────────────────────────────────────────────────────────────

def test_get_trends_returns_dict(client):
    result = get_trends(period='7d')
    assert isinstance(result, dict)

def test_get_trends_has_top_level_keys(client):
    result = get_trends(period='7d')
    for key in ('period', 'start_date', 'end_date', 'days', 'metrics'):
        assert key in result, f"Missing key: {key}"
    assert result['period'] == '7d'
    assert result['days'] == 7

def test_get_trends_defaults_to_all_metrics(client):
    result = get_trends(period='7d')
    metrics = result['metrics']
    # body_battery expands into two series
    for key in ('rhr', 'hrv', 'sleep_score', 'body_battery_wake',
                'body_battery_drain', 'stress', 'steps', 'training_load'):
        assert key in metrics, f"Missing metric series: {key}"

def test_get_trends_series_structure(client):
    result = get_trends(period='7d', metrics=['rhr'])
    assert list(result['metrics'].keys()) == ['rhr']
    rhr = result['metrics']['rhr']
    for key in ('unit', 'daily', 'rolling_7d', 'rolling_28d',
                'start', 'end', 'delta', 'min', 'max', 'avg'):
        assert key in rhr, f"Missing series key: {key}"
    # one daily point per day in the window
    assert len(rhr['daily']) == 7
    assert len(rhr['rolling_7d']) == 7
    assert len(rhr['rolling_28d']) == 7
    for point in rhr['daily']:
        assert 'date' in point and 'value' in point

def test_get_trends_metric_subset(client):
    result = get_trends(period='7d', metrics=['rhr', 'steps'])
    assert set(result['metrics'].keys()) == {'rhr', 'steps'}

def test_get_trends_metric_aliases(client):
    result = get_trends(period='7d', metrics=['resting_hr', 'sleep'])
    assert 'rhr' in result['metrics']
    assert 'sleep_score' in result['metrics']

def test_get_trends_min_le_max(client):
    result = get_trends(period='7d', metrics=['rhr'])
    rhr = result['metrics']['rhr']
    if rhr['min'] is not None and rhr['max'] is not None:
        assert rhr['min'] <= rhr['max']

def test_get_trends_rhr_values_reasonable(client):
    result = get_trends(period='7d', metrics=['rhr'])
    for point in result['metrics']['rhr']['daily']:
        if point['value'] is not None:
            assert 30 < point['value'] < 100

def test_get_trends_invalid_period(client):
    with pytest.raises(ValueError):
        get_trends(period='99d')

def test_get_trends_invalid_metric(client):
    with pytest.raises(ValueError):
        get_trends(period='7d', metrics=['bogus'])