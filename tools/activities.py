# tools/activities.py
from garmin_client import get_client
from tools.profile import get_athlete_profile
from datetime import date, timedelta

# ── HELPERS ──────────────────────────────────────────────────────────────────

def _fmt_pace(speed_ms: float) -> str | None:
    """Convert m/s to MM:SS min/km string."""
    if not speed_ms or speed_ms <= 0:
        return None
    pace_secs = 1000 / speed_ms
    m = int(pace_secs // 60)
    s = int(pace_secs % 60)
    return f"{m}:{s:02d}"

def _fmt_time(secs: float) -> str | None:
    """Convert seconds to M:SS.s string."""
    if not secs:
        return None
    m = int(secs // 60)
    s = secs % 60
    return f"{m}:{s:04.1f}"

# ── EXTRACTION ────────────────────────────────────────────────────────────────

def _extract_activity_summary(activity: dict) -> dict:
    """Extract a sport-aware activity summary from a raw Garmin activity."""
    summary = activity['summaryDTO']
    sport = activity['activityTypeDTO']['typeKey']

    base = {
        'id':                    activity['activityId'],
        'name':                  activity['activityName'],
        'type':                  sport,
        'date':                  summary['startTimeLocal'],
        'location':              activity.get('locationName'),
        'distance_km':           round((summary.get('distance') or 0) / 1000, 2),
        'duration_min':          round((summary.get('duration') or 0) / 60, 1),
        'elevation_gain_m':      summary.get('elevationGain'),
        'avg_hr':                summary.get('averageHR'),
        'max_hr':                summary.get('maxHR'),
        'calories':              summary.get('calories'),
        'training_effect':       round(summary.get('trainingEffect') or 0, 1),
        'training_load':         round(summary.get('activityTrainingLoad') or 0, 1),
        'training_effect_label': summary.get('trainingEffectLabel'),
        'avg_respiration':       round(summary.get('avgRespirationRate') or 0, 1),
        'stamina_end':           summary.get('endPotentialStamina'),
    }

    if sport in ('road_biking', 'cycling', 'indoor_cycling'):
        base.update({
            'avg_speed_kph':    round((summary.get('averageSpeed') or 0) * 3.6, 1),
            'avg_power':        summary.get('averagePower'),
            'normalized_power': summary.get('normalizedPower'),
            'tss':              round(summary.get('trainingStressScore') or 0, 1),
            'intensity_factor': round(summary.get('intensityFactor') or 0, 2),
            'ftp':              summary.get('functionalThresholdPower'),
            'avg_cadence':      summary.get('averageBikeCadence'),
            'seated_time_min':  round((summary.get('seatedTime') or 0) / 60, 1),
        })

    elif sport == 'running':
        avg_speed = summary.get('averageSpeed') or 0
        gap_speed = summary.get('avgGradeAdjustedSpeed') or 0
        base.update({
            'avg_pace_min_km':          _fmt_pace(avg_speed),
            'grade_adjusted_pace':      _fmt_pace(gap_speed),
            'avg_power':                summary.get('averagePower'),
            'normalized_power':         summary.get('normalizedPower'),
            'avg_cadence':              round(summary.get('averageRunCadence') or 0),
            'ground_contact_ms':        round(summary.get('groundContactTime') or 0, 1),
            'stride_length_cm':         round(summary.get('strideLength') or 0, 1),
            'vertical_oscillation_cm':  round(summary.get('verticalOscillation') or 0, 2),
            'vertical_ratio':           round(summary.get('verticalRatio') or 0, 2),
            'rpe':                      summary.get('directWorkoutRpe'),
            'body_battery_change':      summary.get('differenceBodyBattery'),
        })

    elif sport == 'lap_swimming':
        pool_length = summary.get('poolLength') or 0
        active_lengths = summary.get('numberOfActiveLengths') or 0
        base.update({
            'pool_length_m':            pool_length,
            'active_lengths':           active_lengths,
            'active_distance_km':       round((pool_length * active_lengths) / 1000, 2),
            'moving_duration_min':      round((summary.get('movingDuration') or 0) / 60, 1),
            'avg_cadence':              summary.get('averageSwimCadence'),
            'avg_strokes_per_length':   round(summary.get('averageStrokes') or 0, 1),
            'swolf':                    summary.get('averageSWOLF'),
            'total_strokes':            summary.get('totalNumberOfStrokes'),
            'anaerobic_training_effect':round(summary.get('anaerobicTrainingEffect') or 0, 1),
            'rpe':                      summary.get('directWorkoutRpe'),
        })

    return base


def _extract_laps(laps_data: dict, weight_kg: float) -> list[dict]:
    """Extract per-lap data from Garmin splits response."""
    rows = []
    interval_counter = 0
    cumulative_secs = 0

    for lap in laps_data['lapDTOs']:
        intensity = lap.get('intensityType', '')
        distance = lap.get('distance') or 0
        duration = lap.get('duration') or 0
        moving_duration = lap.get('movingDuration') or 0
        avg_speed = lap.get('averageSpeed') or 0
        avg_moving_speed = lap.get('averageMovingSpeed') or 0
        gap_speed = lap.get('avgGradeAdjustedSpeed') or 0
        avg_power = lap.get('averagePower') or 0
        cumulative_secs += duration

        if intensity == 'ACTIVE':
            interval_counter += 1
            interval_label = str(interval_counter)
        else:
            interval_label = ''

        rows.append({
            'interval':              interval_label,
            'step_type':             intensity.replace('_', ' ').title(),
            'lap':                   lap.get('lapIndex'),
            'time':                  _fmt_time(duration),
            'cumulative_time':       _fmt_time(cumulative_secs),
            'distance_km':           round(distance / 1000, 2),
            'avg_pace':              _fmt_pace(avg_speed),
            'avg_gap':               _fmt_pace(gap_speed),
            'moving_time':           _fmt_time(moving_duration),
            'avg_moving_pace':       _fmt_pace(avg_moving_speed),
            'avg_hr':                lap.get('averageHR'),
            'max_hr':                lap.get('maxHR'),
            'avg_cadence':           round(lap.get('averageRunCadence') or 0),
            'avg_power_w':           avg_power,
            'normalized_power_w':    lap.get('normalizedPower'),
            'max_power_w':           lap.get('maxPower'),
            'avg_w_per_kg':          round(avg_power / weight_kg, 2) if avg_power else None,
            'gct_ms':                round(lap.get('groundContactTime') or 0, 1),
            'stride_length_m':       round((lap.get('strideLength') or 0) / 100, 2),
            'vert_oscillation_cm':   round(lap.get('verticalOscillation') or 0, 1),
            'vert_ratio':            round(lap.get('verticalRatio') or 0, 1),
            'elevation_gain_m':      lap.get('elevationGain'),
            'avg_respiration':       round(lap.get('avgRespirationRate') or 0, 1),
            'avg_temp_c':            lap.get('averageTemperature'),
            'calories':              lap.get('calories'),
            'compliance_score':      lap.get('directWorkoutComplianceScore'),
        })

    return rows


def _extract_hr_zones(hr_zones_data: list, total_duration_secs: float) -> list[dict]:
    """Extract HR zone breakdown with time and percentage."""
    zones = []
    for z in hr_zones_data:
        secs = z.get('secsInZone') or 0
        zones.append({
            'zone':      z['zoneNumber'],
            'min_hr':    z['zoneLowBoundary'],
            'time_min':  round(secs / 60, 1),
            'pct_time':  round(secs / total_duration_secs * 100, 1) if total_duration_secs else None,
        })
    return zones


# ── PUBLIC TOOL FUNCTIONS ─────────────────────────────────────────────────────

def get_activities(limit: int = 10, sport_type: str | None = None) -> list[dict]:
    """
    Get a list of recent activities with clean summaries.

    Args:
        limit:      Number of activities to return (default 10, max 50)
        sport_type: Optional filter e.g. 'running', 'road_biking', 'lap_swimming'
    """
    client = get_client()
    limit = min(limit, 50)

    if sport_type:
        activities = client.get_activities(0, limit, activitytype=sport_type)
    else:
        activities = client.get_activities(0, limit)

    summaries = []
    for a in activities:
        sport = a.get('activityType', {}).get('typeKey', '')
        summaries.append({
            'id':            a.get('activityId'),
            'name':          a.get('activityName'),
            'type':          sport,
            'date':          a.get('startTimeLocal'),
            'distance_km':   round((a.get('distance') or 0) / 1000, 2),
            'duration_min':  round((a.get('duration') or 0) / 60, 1),
            'avg_hr':        a.get('averageHR'),
            'training_load': round(a.get('activityTrainingLoad') or 0, 1),
        })

    return summaries


def get_activity(activity_id: int) -> dict:
    """
    Get full detail for a single activity including lap splits and HR zones.

    Args:
        activity_id: Garmin activity ID
    """
    client = get_client()
    athlete = get_athlete_profile()

    activity_raw = client.get_activity(activity_id)
    laps_raw     = client.get_activity_splits(activity_id)
    hr_zones_raw = client.get_activity_hr_in_timezones(activity_id)

    summary  = _extract_activity_summary(activity_raw)
    laps     = _extract_laps(laps_raw, weight_kg=athlete['weight_kg'])
    hr_zones = _extract_hr_zones(hr_zones_raw, summary['duration_min'] * 60)

    return {
        'summary':  summary,
        'laps':     laps,
        'hr_zones': hr_zones,
    }

def _activity_summary_from_list(a: dict) -> dict:
    """Extract compact summary fields from a Garmin activities-list entry."""
    return {
        'id':            a.get('activityId'),
        'name':          a.get('activityName'),
        'type':          a.get('activityType', {}).get('typeKey', ''),
        'date':          a.get('startTimeLocal'),
        'distance_km':   round((a.get('distance') or 0) / 1000, 2),
        'duration_min':  round((a.get('duration') or 0) / 60, 1),
        'avg_hr':        a.get('averageHR'),
        'training_load': round(a.get('activityTrainingLoad') or 0, 1),
    }
def get_activities(
    limit: int = 10,
    sport_type: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """
    Get a list of activities with summary metrics.
    When start_date is supplied the date-range endpoint is used (returns all
    matching activities regardless of limit).  When omitted the most-recent
    endpoint is used with the given limit.
    Args:
        limit:      Number of activities when no date range is given (default 10, max 50)
        sport_type: Optional filter — 'running', 'road_biking', 'lap_swimming', etc.
        start_date: Optional start date YYYY-MM-DD (inclusive)
        end_date:   Optional end date YYYY-MM-DD (inclusive, defaults to today)
    """
    client = get_client()
    if start_date:
        activities = client.get_activities_by_date(
            startdate=start_date,
            enddate=end_date,
            activitytype=sport_type,
        )
    else:
        limit = min(limit, 50)
        activities = client.get_activities(0, limit, activitytype=sport_type) \
            if sport_type else client.get_activities(0, limit)
    return [_activity_summary_from_list(a) for a in activities]

def get_weekly_summary(week_offset: int = 0, sport_type: str | None = None) -> dict:
    """
    Get an aggregated summary of activities for a Monday-to-Sunday week.
    Args:
        week_offset: 0 = current week, 1 = last week, 2 = two weeks ago, …
        sport_type:  Optional filter — 'running', 'road_biking', 'lap_swimming', etc.
    """
    today = date.today()
    week_monday = today - timedelta(days=today.weekday() + week_offset * 7)
    week_sunday = week_monday + timedelta(days=6)
    # Don't ask for future dates
    if week_sunday > today:
        week_sunday = today
    client = get_client()
    activities = client.get_activities_by_date(
        startdate=week_monday.isoformat(),
        enddate=week_sunday.isoformat(),
        activitytype=sport_type,
    )
    summaries = [_activity_summary_from_list(a) for a in activities]
    # Aggregate totals
    total_distance_km = round(sum(a['distance_km'] for a in summaries), 2)
    total_duration_min = round(sum(a['duration_min'] for a in summaries), 1)
    total_training_load = round(sum(a['training_load'] for a in summaries), 1)
    # Breakdown by type
    by_type: dict[str, dict] = {}
    for a in summaries:
        t = a['type']
        if t not in by_type:
            by_type[t] = {'count': 0, 'distance_km': 0.0, 'duration_min': 0.0}
        by_type[t]['count'] += 1
        by_type[t]['distance_km'] = round(by_type[t]['distance_km'] + a['distance_km'], 2)
        by_type[t]['duration_min'] = round(by_type[t]['duration_min'] + a['duration_min'], 1)
    return {
        'week_start': week_monday.isoformat(),
        'week_end':   week_sunday.isoformat(),
        'sport_type_filter': sport_type,
        'total_activities':  len(summaries),
        'total_distance_km': total_distance_km,
        'total_duration_min': total_duration_min,
        'total_training_load': total_training_load,
        'by_type': by_type,
        'activities': summaries,
    }