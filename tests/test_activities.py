# tests/test_activities.py
from tools.activities import (
    get_activities,
    get_activity,
    get_activity_detail_row,
    get_activity_summary,
    get_weekly_summary,
    get_swim_records,
    _months_ago,
    _fmt_pace_100m,
    _swim_set_from_lap,
    _extract_laps,
    _extract_hr_zones,
    _label_pace,
    _is_transition,
    _discipline_label,
    _child_activity_ids,
    _is_multisport,
    _name_sub_activities,
    _extract_sub_activity,
    _merge_gear,
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
    assert 'gear' in result

def test_get_activity_gear_is_a_list(run_activity_id):
    """Gear is optional on Garmin — always a list, never missing."""
    result = get_activity(run_activity_id)
    assert isinstance(result['gear'], list)
    for item in result['gear']:
        assert set(item) == {'name', 'uuid', 'type', 'distance_km', 'lifespan_km'}

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


# ── SWIM RECORDS ──────────────────────────────────────────────────────────────

from datetime import date


def test_months_ago_basic():
    assert _months_ago(date(2026, 7, 18), 6) == date(2026, 1, 18)


def test_months_ago_crosses_year_boundary():
    assert _months_ago(date(2026, 1, 15), 12) == date(2025, 1, 15)


def test_months_ago_clamps_day_to_month_end():
    # Mar 31 minus one month -> Feb 28 (2026 is not a leap year)
    assert _months_ago(date(2026, 3, 31), 1) == date(2026, 2, 28)


def test_fmt_pace_100m():
    assert _fmt_pace_100m(100, 90) == "1:30"
    assert _fmt_pace_100m(200, 210) == "1:45"
    # zero / missing inputs are safe
    assert _fmt_pace_100m(0, 90) is None
    assert _fmt_pace_100m(100, 0) is None


def test_swim_set_from_lap_skips_rest_laps():
    activity = {'activityId': 1, 'activityName': 'Pool Swim',
                'startTimeLocal': '2026-07-13T18:01:10.0'}
    # zero-distance rest lap -> None
    assert _swim_set_from_lap({'distance': 0, 'duration': 30}, activity) is None
    # real swim set -> populated record
    swim_set = _swim_set_from_lap(
        {'distance': 400, 'duration': 548.8, 'numberOfActiveLengths': 20,
         'averageSWOLF': 39.2, 'swimStroke': 'FREESTYLE', 'averageHR': 151},
        activity,
    )
    assert swim_set['distance_m'] == 400
    assert swim_set['avg_swolf'] == 39
    assert swim_set['lengths'] == 20
    assert swim_set['stroke'] == 'FREESTYLE'
    assert swim_set['date'] == '2026-07-13'
    assert swim_set['activity_id'] == 1


_SWIM_SET_KEYS = ['distance_m', 'duration_s', 'pace_per_100m', 'lengths',
                  'avg_swolf', 'stroke', 'avg_hr', 'activity_name', 'date',
                  'activity_id']


def test_get_swim_records_returns_expected_shape(client):
    result = get_swim_records(months=6, top_n=5)
    assert isinstance(result, dict)
    for key in ('period', 'swims_scanned', 'longest_sets'):
        assert key in result, f"Missing key: {key}"
    assert isinstance(result['longest_sets'], list)
    assert len(result['longest_sets']) <= 5


def test_get_swim_records_period_matches_months(client):
    result = get_swim_records(months=6, top_n=5)
    today = date.today()
    expected_start = _months_ago(today, 6).isoformat()
    assert result['period'] == f"{expected_start} to {today.isoformat()}"


def test_get_swim_records_sets_have_required_keys(client):
    result = get_swim_records(months=12, top_n=5)
    for s in result['longest_sets']:
        for key in _SWIM_SET_KEYS:
            assert key in s, f"Missing swim-set key: {key}"


def test_get_swim_records_sorted_by_distance_desc(client):
    result = get_swim_records(months=12, top_n=10)
    distances = [s['distance_m'] for s in result['longest_sets']]
    assert distances == sorted(distances, reverse=True)
    # all returned sets are real swum sets, never zero-distance rests
    assert all(d > 0 for d in distances)


def test_get_swim_records_respects_top_n(client):
    result = get_swim_records(months=12, top_n=3)
    assert len(result['longest_sets']) <= 3


# ── LAP PACE LABELLING (issue #31) ────────────────────────────────────────────

def test_label_pace_appends_unit():
    assert _label_pace("2:16", "/100m") == "2:16/100m"
    assert _label_pace("5:30", "/km") == "5:30/km"
    # None passes through unchanged
    assert _label_pace(None, "/100m") is None


def test_extract_laps_swim_pace_is_per_100m():
    """Swim lap pace must be reported per-100m with an explicit unit, not the
    ambiguous per-km value that reads as ~22:00 and gets misinterpreted."""
    # 100m in 136s -> 2:16/100m (per-km would be ~22:40, the bug in issue #31)
    laps_data = {'lapDTOs': [{
        'lapIndex': 1, 'distance': 100, 'duration': 136,
        'averageSpeed': 100 / 136, 'intensityType': 'ACTIVE',
    }]}
    rows = _extract_laps(laps_data, weight_kg=70, sport='lap_swimming')
    assert rows[0]['avg_pace'] == "2:16/100m"


def test_extract_laps_run_pace_is_per_km_labelled():
    """Non-swim laps keep per-km pace but now carry an explicit '/km' unit."""
    # 5:00/km == 3.3333 m/s
    laps_data = {'lapDTOs': [{
        'lapIndex': 1, 'distance': 1000, 'duration': 300,
        'averageSpeed': 1000 / 300, 'intensityType': 'ACTIVE',
    }]}
    rows = _extract_laps(laps_data, weight_kg=70, sport='running')
    assert rows[0]['avg_pace'] == "5:00/km"


# ── MULTISPORT SUB-ACTIVITIES (issue #37) ─────────────────────────────────────

def test_is_transition_matches_both_garmin_spellings():
    assert _is_transition('transition_v2')
    assert _is_transition('transition')
    assert not _is_transition('running')
    assert not _is_transition('')


def test_discipline_label_maps_sports_to_short_names():
    assert _discipline_label('open_water_swimming') == 'Swim'
    assert _discipline_label('lap_swimming') == 'Swim'
    assert _discipline_label('road_biking') == 'Bike'
    assert _discipline_label('indoor_cycling') == 'Bike'
    assert _discipline_label('running') == 'Run'
    assert _discipline_label('trail_running') == 'Run'
    # Unknown sports fall back to a title-cased version of the type key
    assert _discipline_label('inline_skating') == 'Inline Skating'


def test_child_activity_ids_reads_metadata_dto():
    activity = {'metadataDTO': {'childIds': [1, 2, 3]}}
    assert _child_activity_ids(activity) == [1, 2, 3]


def test_child_activity_ids_handles_alternate_shapes():
    # Top-level list, string IDs, and dict entries all normalise to ints
    assert _child_activity_ids({'childIds': ['10', '11']}) == [10, 11]
    assert _child_activity_ids(
        {'metadataDTO': {'childIds': [{'activityId': 7}, {'activityId': 8}]}}
    ) == [7, 8]
    # Unparseable entries are dropped rather than blowing up
    assert _child_activity_ids({'childIds': [1, None, 'nope', 2]}) == [1, 2]
    assert _child_activity_ids({}) == []


def test_is_multisport_detection():
    assert _is_multisport({'isMultiSportParent': True})
    assert _is_multisport({'activityTypeDTO': {'typeKey': 'multi_sport'}})
    assert _is_multisport({'metadataDTO': {'childIds': [1, 2]}})
    # A plain run is not multisport
    assert not _is_multisport({
        'activityTypeDTO': {'typeKey': 'running'},
        'metadataDTO': {'childIds': []},
    })


def test_name_sub_activities_triathlon_order():
    sports = ['open_water_swimming', 'transition_v2', 'road_biking',
              'transition_v2', 'running']
    assert _name_sub_activities(sports) == ['Swim', 'T1', 'Bike', 'T2', 'Run']


def test_name_sub_activities_numbers_repeated_disciplines():
    """A duathlon has two runs — they must be distinguishable."""
    sports = ['running', 'transition_v2', 'road_biking', 'transition_v2', 'running']
    assert _name_sub_activities(sports) == ['Run 1', 'T1', 'Bike', 'T2', 'Run 2']


def _child(type_key, **summary):
    return {'activityId': 99, 'activityTypeDTO': {'typeKey': type_key},
            'summaryDTO': summary}


def test_extract_sub_activity_transition_is_timing_only():
    sub = _extract_sub_activity(
        _child('transition_v2', distance=31.44, duration=110.848, averageHR=127.0),
        laps_raw=None, weight_kg=70, name='T2',
    )
    assert sub['name'] == 'T2'
    assert sub['type'] == 'transition_v2'
    assert sub['duration_s'] == 110.8
    assert sub['avg_hr'] == 127.0
    # No discipline metrics or laps on a transition
    for key in ('pace_per_km', 'pace_per_100m', 'avg_power', 'laps'):
        assert key not in sub


def test_extract_sub_activity_swim_reports_per_100m_pace():
    # 750m in 840s -> 1:52/100m
    sub = _extract_sub_activity(
        _child('open_water_swimming', distance=750, duration=840, averageHR=155.0),
        laps_raw=None, weight_kg=70, name='Swim',
    )
    assert sub['pace_per_100m'] == '1:52'
    assert sub['distance_m'] == 750
    assert sub['laps'] == []


def test_extract_sub_activity_bike_reports_power_and_speed():
    sub = _extract_sub_activity(
        _child('road_biking', distance=20000, duration=2400, averageSpeed=8.3333,
               averagePower=195, normalizedPower=210),
        laps_raw=None, weight_kg=70, name='Bike',
    )
    assert sub['distance_km'] == 20.0
    assert sub['avg_speed_kmh'] == 30.0
    assert sub['avg_power'] == 195
    assert sub['normalized_power'] == 210


def test_extract_sub_activity_run_reports_per_km_pace():
    # 4:24/km == 3.7879 m/s
    sub = _extract_sub_activity(
        _child('running', distance=5000, duration=1320, averageSpeed=5000 / 1320,
               averageRunCadence=178.4),
        laps_raw=None, weight_kg=70, name='Run',
    )
    assert sub['pace_per_km'] == '4:24'
    assert sub['distance_km'] == 5.0
    assert sub['avg_cadence'] == 178


def test_extract_sub_activity_includes_laps_when_splits_given():
    laps_raw = {'lapDTOs': [{'lapIndex': 1, 'distance': 1000, 'duration': 300,
                             'averageSpeed': 1000 / 300, 'intensityType': 'ACTIVE'}]}
    sub = _extract_sub_activity(
        _child('running', distance=1000, duration=300, averageSpeed=1000 / 300),
        laps_raw=laps_raw, weight_kg=70, name='Run',
    )
    assert len(sub['laps']) == 1
    assert sub['laps'][0]['avg_pace'] == '5:00/km'


def test_get_activity_multisport_has_sub_activities(multisport_activity_id):
    """A sprint triathlon breaks out as swim / T1 / bike / T2 / run."""
    result = get_activity(multisport_activity_id)
    assert result['summary']['type'] == 'multi_sport'
    subs = result['sub_activities']
    assert [s['name'] for s in subs] == ['Swim', 'T1', 'Bike', 'T2', 'Run']
    assert [s['type'] for s in subs] == [
        'open_water_swimming', 'transition_v2', 'road_biking',
        'transition_v2', 'running',
    ]
    for sub in subs:
        assert sub['activity_id'] is not None
        assert sub['duration_s'] > 0


def test_get_activity_multisport_legs_have_discipline_metrics(multisport_activity_id):
    subs = get_activity(multisport_activity_id)['sub_activities']
    swim, bike, run = subs[0], subs[2], subs[4]

    assert swim['pace_per_100m'] is not None
    assert swim['distance_m'] > 0

    assert bike['avg_power'] is not None
    assert bike['avg_speed_kmh'] > 0
    assert len(bike['laps']) > 0

    assert run['pace_per_km'] is not None
    assert run['distance_km'] > 0
    assert len(run['laps']) > 0


def test_merge_gear_rolls_up_legs_and_dedupes():
    """Leg gear should roll up to the parent, deduped by uuid, order preserved."""
    subs = [
        {'gear': [{'name': 'Wetsuit', 'uuid': 'w1'}]},
        {},  # transition — no gear key at all
        {'gear': [{'name': 'Canyon Endurace', 'uuid': 'b1'}]},
        {'gear': []},
        {'gear': [{'name': 'Novablast 5', 'uuid': 's1'},
                  {'name': 'Canyon Endurace', 'uuid': 'b1'}]},
    ]
    merged = _merge_gear([], subs)
    assert [g['uuid'] for g in merged] == ['w1', 'b1', 's1']

def test_merge_gear_keeps_parent_gear_first():
    """Gear already on the parent stays, and isn't duplicated by a leg."""
    parent = [{'name': 'Novablast 5', 'uuid': 's1'}]
    merged = _merge_gear(parent, [{'gear': [{'name': 'Novablast 5', 'uuid': 's1'},
                                            {'name': 'Wetsuit', 'uuid': 'w1'}]}])
    assert [g['uuid'] for g in merged] == ['s1', 'w1']

def test_get_activity_multisport_legs_have_gear(multisport_activity_id):
    """Gear hangs off the legs, not the multisport parent — each leg gets its own."""
    result = get_activity(multisport_activity_id)
    subs = result['sub_activities']
    swim, t1, bike, t2, run = subs

    # Transitions never carry gear and shouldn't pay for the extra request.
    assert 'gear' not in t1
    assert 'gear' not in t2

    for leg in (swim, bike, run):
        assert isinstance(leg['gear'], list)
        for item in leg['gear']:
            assert set(item) == {'name', 'uuid', 'type', 'distance_km', 'lifespan_km'}

    # The run leg has shoes assigned — the case that motivated the roll-up.
    assert run['gear'], "expected shoes on the run leg"
    assert any(g['type'] == 'Shoes' for g in run['gear'])

def test_get_activity_multisport_parent_gear_is_union_of_legs(multisport_activity_id):
    """Top-level gear should cover every leg's gear, deduped."""
    result = get_activity(multisport_activity_id)
    parent_uuids = [g['uuid'] for g in result['gear']]

    assert len(parent_uuids) == len(set(parent_uuids)), "duplicate gear at top level"
    for leg in result['sub_activities']:
        for g in leg.get('gear') or []:
            assert g['uuid'] in parent_uuids, f"{g['name']} missing from top-level gear"

def test_get_activity_multisport_legs_sum_to_parent(multisport_activity_id):
    """Leg durations should account for the whole race, within rounding."""
    result = get_activity(multisport_activity_id)
    leg_total_min = sum(s['duration_s'] for s in result['sub_activities']) / 60
    assert abs(leg_total_min - result['summary']['duration_min']) < 1


def test_get_activity_non_multisport_has_no_sub_activities(run_activity_id):
    """Behaviour for ordinary activities is unchanged — no sub_activities key."""
    assert 'sub_activities' not in get_activity(run_activity_id)


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


# ── get_activity_detail_row (issue 74's activity_details.detail/.route payload) ─
# Unlike the rest of this file these don't hit live Garmin — get_activity_detail_row
# is wired against a fake client so the activity-detail page's extraction logic can
# be tested without credentials.

class _FakeDetailClient:
    def __init__(self, activity, splits=None, weather=None, details=None,
                 hr_zones=None, children=None):
        self._activity = activity
        self._splits = splits
        self._weather = weather
        self._details = details
        self._hr_zones = hr_zones or []
        self._children = children or {}

    def get_activity(self, activity_id):
        return self._children.get(activity_id, self._activity)

    def get_activity_splits(self, activity_id):
        return self._splits

    def get_activity_weather(self, activity_id):
        return self._weather

    def get_activity_details(self, activity_id):
        return self._details

    def get_activity_hr_in_timezones(self, activity_id):
        return self._hr_zones


def test_get_activity_detail_row_includes_hr_zones(monkeypatch):
    activity = {
        'activityId': 42,
        'activityTypeDTO': {'typeKey': 'running'},
        'summaryDTO': {'duration': 1800, 'movingDuration': 1750, 'averageSpeed': 3.0},
    }
    hr_zones_raw = [
        {'zoneNumber': 1, 'zoneLowBoundary': 100, 'secsInZone': 900},
        {'zoneNumber': 2, 'zoneLowBoundary': 140, 'secsInZone': 900},
    ]
    fake = _FakeDetailClient(activity, hr_zones=hr_zones_raw)
    monkeypatch.setattr('tools.activities.get_client', lambda: fake)
    monkeypatch.setattr('tools.activities.get_activity_gear', lambda activity_id: [])

    detail, route = get_activity_detail_row(42)

    assert detail['hr_zones'] == [
        {'zone': 1, 'min_hr': 100, 'time_min': 15.0, 'pct_time': 50.0},
        {'zone': 2, 'min_hr': 140, 'time_min': 15.0, 'pct_time': 50.0},
    ]
    assert route is None
    # Garmin's own elapsed vs moving time, distinct from each other — the
    # activity-detail page's Total vs Active/Swim Duration stats read these
    # directly rather than reconstructing "elapsed" from a pause-detection
    # heuristic that isn't reliable for every sport (e.g. a pool swim).
    assert detail['duration_elapsed_sec'] == 1800
    assert detail['duration_active_sec'] == 1750


def test_extract_hr_zones_skips_malformed_entries_instead_of_raising():
    """Garmin's hrTimeInZones response has been observed to vary by
    activity — a zone entry missing zoneNumber/zoneLowBoundary is skipped
    rather than raising a KeyError that would abort the whole detail sync."""
    raw = [
        {'zoneNumber': 1, 'zoneLowBoundary': 100, 'secsInZone': 600},
        {'secsInZone': 300},  # missing both keys
        "not even a dict",
        {'zoneNumber': 2, 'zoneLowBoundary': 140, 'secsInZone': 600},
    ]
    zones = _extract_hr_zones(raw, 1500)
    assert [z['zone'] for z in zones] == [1, 2]


def test_extract_hr_zones_handles_none_and_empty():
    assert _extract_hr_zones(None, 1000) == []
    assert _extract_hr_zones([], 1000) == []


def test_get_activity_detail_row_hr_zones_failure_does_not_abort_detail(monkeypatch):
    """A hrTimeInZones fetch/parse failure for one activity used to bubble
    up and abort the entire get_activity_detail_row call — now it degrades
    to an empty hr_zones list so the rest of the detail (laps, gear, ...)
    still syncs."""
    activity = {
        'activityId': 7,
        'activityTypeDTO': {'typeKey': 'road_biking'},
        'summaryDTO': {'duration': 3600, 'movingDuration': 3500, 'averageSpeed': 8.0},
    }

    class _BrokenHrZonesClient(_FakeDetailClient):
        def get_activity_hr_in_timezones(self, activity_id):
            raise RuntimeError("boom")

    fake = _BrokenHrZonesClient(activity)
    monkeypatch.setattr('tools.activities.get_client', lambda: fake)
    monkeypatch.setattr('tools.activities.get_activity_gear', lambda activity_id: [])

    detail, route = get_activity_detail_row(7)

    assert detail['hr_zones'] == []
    assert detail['avg_speed_kph'] == round(8.0 * 3.6, 1)
    assert 'sub_activities' not in detail


def test_get_activity_detail_row_multisport_propagates_sub_activities(monkeypatch):
    """#37 extracts sub_activities for get_activity(); get_activity_detail_row
    (added in #78) needs the same breakdown for the activity-detail page's
    multisport section (issue 74)."""
    parent = {
        'activityId': 1,
        'activityTypeDTO': {'typeKey': 'multi_sport'},
        'summaryDTO': {'duration': 3600},
        'metadataDTO': {'childIds': [2, 3]},
    }
    children = {
        2: {'activityId': 2, 'activityTypeDTO': {'typeKey': 'open_water_swimming'},
            'summaryDTO': {'duration': 600, 'distance': 750, 'averageHR': 140}},
        3: {'activityId': 3, 'activityTypeDTO': {'typeKey': 'running'},
            'summaryDTO': {'duration': 1200, 'distance': 5000, 'averageHR': 150,
                           'averageSpeed': 5000 / 1200}},
    }
    fake = _FakeDetailClient(parent, children=children)
    monkeypatch.setattr('tools.activities.get_client', lambda: fake)
    monkeypatch.setattr('tools.activities.get_athlete_profile', lambda: {'weight_kg': 70})
    monkeypatch.setattr('tools.activities.get_activity_gear', lambda activity_id: [])

    detail, route = get_activity_detail_row(1)

    subs = detail['sub_activities']
    assert [s['name'] for s in subs] == ['Swim', 'Run']
    assert [s['type'] for s in subs] == ['open_water_swimming', 'running']
    assert detail['gear'] == []

