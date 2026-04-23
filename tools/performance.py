# tools/performance.py
from garmin_client import get_client
from tools.health import resolve_date
from datetime import date, timedelta


def _default_range(days: int) -> tuple[str, str]:
    """Return (start_date, end_date) ISO strings for the trailing N days."""
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _resolve_range(start_date: str | None,
                   end_date: str | None,
                   default_days: int) -> tuple[str, str]:
    """Resolve a (start, end) range with 'today'/'yesterday' shortcuts and a default span."""
    default_start, default_end = _default_range(default_days)
    start = resolve_date(start_date) if start_date else default_start
    end   = resolve_date(end_date)   if end_date   else default_end
    return start, end


def get_endurance_score(start_date: str | None = None,
                        end_date: str | None = None) -> dict:
    """
    Get endurance score and contribution breakdown for a date range.

    Args:
        start_date: Optional start date YYYY-MM-DD or 'today' / 'yesterday'
                    (defaults to 30 days ago)
        end_date:   Optional end date YYYY-MM-DD or 'today' / 'yesterday'
                    (defaults to today)
    """
    start_date, end_date = _resolve_range(start_date, end_date, default_days=30)

    client = get_client()
    raw = client.get_endurance_score(start_date, end_date) or {}

    # Score and contributor data live under 'enduranceScoreDTO' at the top level.
    # 'groupMap' contains per-week rolling averages which we don't need here.
    dto = raw.get('enduranceScoreDTO') or {}

    # Build a full typeId -> typeKey lookup (all types, not just top-level)
    # so sub-types like street_running (typeId 7) resolve correctly.
    # group index is 0-based: group + 1 == typeId.
    try:
        types = client.get_activity_types() or []
        type_map = {t['typeId']: t['typeKey'] for t in types}
    except Exception:
        type_map = {}

    # Classification numeric codes map to these labels (inferred from the gauge
    # lower limits returned in the DTO alongside the classification value).
    _CLASSIFICATION = {
        1: 'beginner', 2: 'intermediate', 3: 'trained',
        4: 'well_trained', 5: 'expert', 6: 'superior', 7: 'elite',
    }

    contributors = [
        {
            'sport':            type_map.get(c.get('group', 0) + 1, f'group_{c.get("group")}'),
            'contribution_pct': round(c.get('contribution') or 0, 1),
        }
        for c in (dto.get('contributors') or [])
    ]

    return {
        'start_date':      start_date,
        'end_date':        end_date,
        'endurance_score': dto.get('overallScore'),
        'classification':  _CLASSIFICATION.get(dto.get('classification'), dto.get('classification')),
        'gauge_lower':     dto.get('gaugeLowerLimit'),
        'gauge_upper':     dto.get('gaugeUpperLimit'),
        'period_avg':      raw.get('avg'),
        'period_max':      raw.get('max'),
        'contributors':    contributors,
    }


def get_running_tolerance(start_date: str | None = None,
                          end_date: str | None = None) -> dict:
    """
    Get running load tolerance metrics for a date range.

    Args:
        start_date: Optional start date YYYY-MM-DD or 'today' / 'yesterday'
                    (defaults to 7 days ago)
        end_date:   Optional end date YYYY-MM-DD or 'today' / 'yesterday'
                    (defaults to today)
    """
    start_date, end_date = _resolve_range(start_date, end_date, default_days=7)

    client = get_client()
    raw = client.get_running_tolerance(start_date, end_date) or []

    # API returns a list of weekly entries ordered oldest-first; take the latest.
    latest = raw[-1] if raw else {}

    tolerance_clean = {
        'running_tolerance':         latest.get('runningTolerance') or latest.get('tolerance'),
        'level':                     latest.get('level') or latest.get('toleranceLevel'),
        'feedback_phrase':           latest.get('feedbackPhrase'),
        'weekly_running_load':       latest.get('weeklyRunningLoad'),
        'weekly_running_load_lower': latest.get('weeklyRunningLoadLower') or latest.get('lowerLimit'),
        'weekly_running_load_upper': latest.get('weeklyRunningLoadUpper') or latest.get('upperLimit'),
        'acute_load':                latest.get('acuteLoad'),
        'chronic_load':              latest.get('chronicLoad'),
    }

    return {
        'start_date':        start_date,
        'end_date':          end_date,
        'running_tolerance': tolerance_clean,
    }


# ── PERSONAL RECORDS ──────────────────────────────────────────────────────────

_ALLOWED_PR_SPORTS = {
    'running',
    'road_biking',
    'virtual_ride',
    'cycling',
    'indoor_cycling',
    'lap_swimming',
    'open_water_swimming',
    'swimming',
}

_SPORT_CATEGORY = {
    'running':             'running',
    'road_biking':         'cycling',
    'virtual_ride':        'cycling',
    'cycling':             'cycling',
    'indoor_cycling':      'cycling',
    'lap_swimming':        'swimming',
    'open_water_swimming': 'swimming',
    'swimming':            'swimming',
}

_PR_TYPES: dict[int, dict] = {
    1:  {'label': 'Fastest 1K',           'value_type': 'time_s'},
    2:  {'label': 'Fastest Mile',          'value_type': 'time_s'},
    3:  {'label': 'Fastest 5K',           'value_type': 'time_s'},
    4:  {'label': 'Fastest 10K',          'value_type': 'time_s'},
    5:  {'label': 'Fastest Half Marathon', 'value_type': 'time_s'},
    6:  {'label': 'Fastest Marathon',      'value_type': 'time_s'},
    7:  {'label': 'Longest Run',           'value_type': 'distance_m'},
    8:  {'label': 'Longest Ride',          'value_type': 'distance_m'},
    9:  {'label': 'Best Climb',            'value_type': 'distance_m'},
    10: {'label': 'Best 20-Min Power',     'value_type': 'power_w'},
    11: {'label': 'Longest Virtual Ride',  'value_type': 'distance_m'},
    17: {'label': 'Longest Swim',          'value_type': 'distance_m'},
    18: {'label': 'Fastest 100m Swim',     'value_type': 'time_s'},
}


def _fmt_pr_time(secs: float) -> str:
    """Format seconds as H:MM:SS or M:SS."""
    total = int(round(secs))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_pr_value(value: float, value_type: str) -> str:
    """Format a PR value for display."""
    if value_type == 'time_s':
        return _fmt_pr_time(value)
    if value_type == 'distance_m':
        return f"{round(value / 1000, 2)} km"
    if value_type == 'power_w':
        return f"{round(value)} W"
    return str(round(value, 2))


def get_personal_records() -> dict:
    """
    Get personal records for running, cycling, and swimming.

    Filters out non-sport PRs (wellness streaks, etc.) and groups records
    by sport category (running, cycling, swimming).
    """
    client = get_client()
    raw = client.get_personal_record() or []

    result: dict[str, list] = {'running': [], 'cycling': [], 'swimming': []}

    for pr in raw:
        activity_type = pr.get('activityType')
        if activity_type not in _ALLOWED_PR_SPORTS:
            continue

        type_id = pr.get('typeId')
        pr_type = _PR_TYPES.get(type_id, {
            'label':      f'PR Type {type_id}',
            'value_type': 'raw',
        })

        category = _SPORT_CATEGORY.get(activity_type, activity_type)
        value_raw = pr.get('value')
        entry = {
            'label':           pr_type['label'],
            'value_formatted': _fmt_pr_value(value_raw, pr_type['value_type']) if value_raw is not None else None,
            'value_raw':       value_raw,
            'activity_name':   pr.get('activityName'),
            'activity_type':   activity_type,
            'date':            pr.get('actStartDateTimeInGMTFormatted'),
            'activity_id':     pr.get('activityId'),
        }

        if category in result:
            result[category].append(entry)
        else:
            result[category] = [entry]

    return result
