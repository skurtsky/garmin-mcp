# tests/test_activities.py
from tools.activities import get_activities, get_activity

def test_get_activities_returns_list(client):
    activities = get_activities(limit=5)
    assert isinstance(activities, list)
    assert len(activities) <= 5

def test_get_activities_has_required_keys(client):
    activities = get_activities(limit=3)
    assert len(activities) > 0
    expected = ['id', 'name', 'type', 'date', 'distance_km', 'duration_min']
    for key in expected:
        assert key in activities[0], f"Missing key: {key}"

def test_get_activities_sport_filter(client):
    runs = get_activities(limit=20, sport_type='running')
    assert all(a['type'] == 'running' for a in runs)

def test_get_activity_returns_expected_structure(run_activity_id):
    result = get_activity(run_activity_id)
    assert 'summary' in result
    assert 'laps' in result
    assert 'hr_zones' in result

def test_get_activity_summary_has_run_fields(run_activity_id):
    result = get_activity(run_activity_id)
    summary = result['summary']
    assert summary['type'] == 'running'
    assert 'avg_pace_min_km' in summary
    assert 'normalized_power' in summary
    assert 'avg_cadence' in summary

def test_get_activity_laps_not_empty(run_activity_id):
    result = get_activity(run_activity_id)
    assert len(result['laps']) > 0

def test_get_activity_hr_zones_has_five_zones(run_activity_id):
    result = get_activity(run_activity_id)
    assert len(result['hr_zones']) == 5

def test_get_activity_cycling_has_power_fields(cycling_activity_id):
    result = get_activity(cycling_activity_id)
    summary = result['summary']
    assert summary['type'] == 'road_biking'
    assert 'tss' in summary
    assert 'normalized_power' in summary
    assert 'intensity_factor' in summary