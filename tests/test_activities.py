# tests/test_activities.py
from tools.activities import (
    get_activities,
    get_activity,
    get_weekly_summary,
)


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

def test_get_activities_with_date_range(client, test_date):
    from datetime import date, timedelta
    d = date.fromisoformat(test_date)
    start = (d - timedelta(days=6)).isoformat()
    result = get_activities(start_date=start, end_date=test_date)
    assert isinstance(result, list)
    # All activities should fall within the requested range
    for a in result:
        assert a['date'][:10] >= start
        assert a['date'][:10] <= test_date

def test_get_weekly_summary_returns_dict(client):
    result = get_weekly_summary(week_offset=1)
    assert isinstance(result, dict)

def test_get_weekly_summary_has_required_keys(client):
    result = get_weekly_summary(week_offset=1)
    for key in ['week_start', 'week_end', 'total_activities',
                'total_distance_km', 'total_duration_min', 'by_type', 'activities']:
        assert key in result, f"Missing key: {key}"

def test_get_weekly_summary_dates_are_monday_and_sunday(client):
    from datetime import date
    result = get_weekly_summary(week_offset=1)
    start = date.fromisoformat(result['week_start'])
    end   = date.fromisoformat(result['week_end'])
    assert start.weekday() == 0, "week_start should be a Monday"
    assert end.weekday() == 6, "week_end should be a Sunday"

def test_get_weekly_summary_totals_are_consistent(client):
    result = get_weekly_summary(week_offset=1)
    assert result['total_activities'] == len(result['activities'])
    computed_dist = round(sum(a['distance_km'] for a in result['activities']), 2)
    assert abs(result['total_distance_km'] - computed_dist) < 0.01

def test_get_weekly_summary_sport_filter(client):
    result = get_weekly_summary(week_offset=1, sport_type='running')
    assert result['sport_type_filter'] == 'running'
    assert all(a['type'] == 'running' for a in result['activities'])

