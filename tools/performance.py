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


def _latest_entry(raw):
    """Return the latest dict from a possibly list-shaped Garmin response."""
    if isinstance(raw, list):
        return raw[-1] if raw else {}
    return raw or {}


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

    # Endurance score may live at top level or under a nested key — prefer
    # a 'groupMap' / 'monthly' / latest-entry shape, falling back to top level.
    latest = _latest_entry(raw)
    contrib = latest.get('contributors') or {}

    score_clean = {
        'endurance_score':   latest.get('overallScore') or latest.get('enduranceScore'),
        'classification':    latest.get('classification'),
        'feedback_phrase':   latest.get('feedbackPhrase'),
        'gauge_lower_limit': latest.get('gaugeLowerLimit'),
        'gauge_upper_limit': latest.get('gaugeUpperLimit'),
    }

    contributors_clean = {
        'aerobic_base': contrib.get('aerobicBase') if isinstance(contrib, dict) else None,
        'aerobic_high': contrib.get('aerobicHigh') if isinstance(contrib, dict) else None,
        'anaerobic':    contrib.get('anaerobic')   if isinstance(contrib, dict) else None,
    }

    return {
        'start_date':      start_date,
        'end_date':        end_date,
        'endurance_score': score_clean,
        'contributors':    contributors_clean,
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
    raw = client.get_running_tolerance(start_date, end_date) or {}

    latest = _latest_entry(raw)

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
