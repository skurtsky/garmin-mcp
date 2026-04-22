# tests/test_trends.py
from tools.trends import get_performance_predictions, get_performance_trends
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
    import pytest
    with pytest.raises(ValueError):
        get_performance_trends(period='daily', lookback=4)