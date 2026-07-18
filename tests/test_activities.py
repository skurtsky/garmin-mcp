# tests/test_activities.py
from tools.activities import (
    get_activities,
    get_activity,
    get_activity_summary,
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
    # Garmin's activityType filter is a category filter, not an exact typeKey
    # match — it also returns subtypes like trail_running/indoor_running/etc.,
    # all of which contain "run".
    runs = get_activities(limit=20, sport_type='running')
    assert all('run' in a['type'] for a in runs)

def test_get_activity_returns_expected_structure(run_activity_id):
    result = get_activity(run_activity_id)
    assert 'summary' in result
    assert 'laps' in result
    assert 'intervals' in result
    assert 'interval_summary' in result
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

_KNOWN_PHASES = {'Warmup', 'Active', 'Recovery', 'Rest', 'Cooldown'}

def test_get_activity_intervals_for_interval_workout(run_activity_id):
    """run_activity_id is a 5x1K interval workout — intervals/interval_summary
    should both be populated with recognizable phase labels."""
    result = get_activity(run_activity_id)
    intervals = result['intervals']
    interval_summary = result['interval_summary']
    assert len(intervals) > 0
    assert len(interval_summary) > 0
    for row in intervals:
        assert row['phase'] in _KNOWN_PHASES, f"Unexpected phase: {row['phase']}"
    for row in interval_summary:
        assert row['phase'] in _KNOWN_PHASES, f"Unexpected phase: {row['phase']}"
    # Only 'Active' reps are numbered; other phases leave rep unset
    active_reps = [r['rep'] for r in intervals if r['phase'] == 'Active']
    assert active_reps == list(range(1, len(active_reps) + 1))
    non_active = [r['rep'] for r in intervals if r['phase'] != 'Active']
    assert all(r is None for r in non_active)

def test_get_activity_cycling_has_power_fields(cycling_activity_id):
    result = get_activity(cycling_activity_id)
    summary = result['summary']
    assert summary['type'] == 'road_biking'
    assert 'tss' in summary
    assert 'normalized_power' in summary
    assert 'intensity_factor' in summary

def test_get_activity_cycling_intervals_no_crash(cycling_activity_id):
    """Cycling activity may or may not have structured-workout intervals —
    the keys should always be present as lists, never crash."""
    result = get_activity(cycling_activity_id)
    assert isinstance(result['intervals'], list)
    assert isinstance(result['interval_summary'], list)

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
    # Same category-filter caveat as test_get_activities_sport_filter above.
    assert all('run' in a['type'] for a in result['activities'])


_SUMMARY_TOTAL_KEYS = [
    'count', 'total_distance_km', 'total_duration_min',
    'total_calories', 'total_elevation_m',
    'avg_distance_km', 'avg_duration_min',
]


def test_get_activity_summary_has_required_keys(client, test_date_range_start, test_date):
    result = get_activity_summary(start_date=test_date_range_start, end_date=test_date)
    assert result['period'] == f"{test_date_range_start} to {test_date}"
    assert result['sport_type'] is None
    for key in _SUMMARY_TOTAL_KEYS:
        assert key in result, f"Missing key: {key}"


def test_get_activity_summary_groups_by_sport_when_unfiltered(
    client, test_date_range_start, test_date
):
    result = get_activity_summary(start_date=test_date_range_start, end_date=test_date)
    assert 'by_sport' in result
    assert isinstance(result['by_sport'], dict)
    # Per-sport counts must sum to the overall count
    assert sum(s['count'] for s in result['by_sport'].values()) == result['count']
    for sport in result['by_sport'].values():
        for key in _SUMMARY_TOTAL_KEYS:
            assert key in sport, f"Missing per-sport key: {key}"


def test_get_activity_summary_sport_filter_omits_breakdown(
    client, test_date_range_start, test_date
):
    result = get_activity_summary(
        start_date=test_date_range_start, end_date=test_date, sport_type='running'
    )
    assert result['sport_type'] == 'running'
    assert 'by_sport' not in result
    for key in _SUMMARY_TOTAL_KEYS:
        assert key in result, f"Missing key: {key}"


def test_get_activity_summary_averages_are_consistent(
    client, test_date_range_start, test_date
):
    result = get_activity_summary(start_date=test_date_range_start, end_date=test_date)
    if result['count']:
        expected_avg = round(result['total_distance_km'] / result['count'], 2)
        assert abs(result['avg_distance_km'] - expected_avg) < 0.01
    else:
        assert result['avg_distance_km'] == 0


def test_get_activity_includes_weather(run_activity_id):
    """Activity detail should include a weather key (may be None for indoor activities)."""
    result = get_activity(run_activity_id)
    assert 'weather' in result
    weather = result['weather']
    if weather is not None:
        for key in ('temp_c', 'apparent_temp_c', 'humidity_pct',
                    'wind_speed', 'wind_direction_compass', 'conditions'):
            assert key in weather, f"Missing weather key: {key}"
        # Sanity-check temps are in Celsius range (not Fahrenheit)
        if weather['temp_c'] is not None:
            assert -50 < weather['temp_c'] < 60, f"temp_c {weather['temp_c']} looks like Fahrenheit"

