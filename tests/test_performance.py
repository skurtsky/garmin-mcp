from tools.performance import (
    get_endurance_score,
    get_running_tolerance,
    get_personal_records,
)


def test_get_endurance_score_returns_dict_default_range():
    result = get_endurance_score()
    assert isinstance(result, dict)

def test_get_endurance_score_has_required_keys(test_month_range_start, test_date_range_end):
    result = get_endurance_score(
        start_date=test_month_range_start,
        end_date=test_date_range_end,
    )
    for key in ('start_date', 'end_date', 'endurance_score', 'classification',
                'gauge_lower', 'gauge_upper', 'period_avg', 'period_max', 'contributors'):
        assert key in result, f"Missing key: {key}"
    assert result['start_date'] == test_month_range_start
    assert result['end_date'] == test_date_range_end
    assert isinstance(result['contributors'], list)

def test_get_endurance_score_supports_today_yesterday():
    result = get_endurance_score(start_date='yesterday', end_date='today')
    assert isinstance(result, dict)
    assert 'endurance_score' in result

def test_get_running_tolerance_returns_dict_default_range():
    result = get_running_tolerance()
    assert isinstance(result, dict)

def test_get_running_tolerance_has_required_keys(test_date_range_start, test_date_range_end):
    result = get_running_tolerance(
        start_date=test_date_range_start,
        end_date=test_date_range_end,
    )
    for key in ('start_date', 'end_date', 'running_tolerance'):
        assert key in result, f"Missing key: {key}"
    assert isinstance(result['running_tolerance'], dict)

def test_get_running_tolerance_supports_today_yesterday():
    result = get_running_tolerance(start_date='yesterday', end_date='today')
    assert isinstance(result, dict)
    assert 'running_tolerance' in result


# ── PERSONAL RECORDS ──────────────────────────────────────────────────────────

def test_get_personal_records_returns_dict():
    result = get_personal_records()
    assert isinstance(result, dict)


def test_get_personal_records_has_sport_groups():
    result = get_personal_records()
    for category in ('running', 'cycling', 'swimming'):
        assert category in result
        assert isinstance(result[category], list)


def test_get_personal_records_only_target_sports():
    """No yoga, null-type, or other non-sport PRs should appear."""
    allowed = {
        'running', 'road_biking', 'virtual_ride', 'cycling',
        'indoor_cycling', 'lap_swimming', 'open_water_swimming', 'swimming',
    }
    result = get_personal_records()
    for records in result.values():
        for pr in records:
            assert pr['activity_type'] in allowed, f"Unexpected sport: {pr['activity_type']}"


def test_get_personal_records_entries_have_required_fields():
    result = get_personal_records()
    required = ('label', 'value_formatted', 'value_raw', 'activity_name',
                'activity_type', 'date', 'activity_id')
    for records in result.values():
        for pr in records:
            for key in required:
                assert key in pr, f"Missing key: {key}"
