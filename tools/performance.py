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
