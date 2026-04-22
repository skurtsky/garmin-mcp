# tools/trends.py
from garmin_client import get_client
from datetime import date, timedelta
from calendar import monthrange

def _fmt_race_time(seconds) -> str | None:
    if not seconds:
        return None
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def _extract_predictions(raw: dict) -> list[dict]:
    mapping = {
        'time5K':           '5K',
        'time10K':          '10K',
        'timeHalfMarathon': 'half_marathon',
        'timeMarathon':     'marathon',
    }
    results = []
    for key, label in mapping.items():
        secs = raw.get(key)
        results.append({
            'distance':               label,
            'predicted_time':         _fmt_race_time(secs),
            'predicted_time_seconds': secs,
        })
    return results

def get_performance_predictions() -> dict:
    """
    Get current race time predictions for 5K, 10K, half marathon, and marathon.
    Predictions are based on recent training data and VO2max estimates.
    """
    client = get_client()
    raw = client.get_race_predictions()
    return {
        'date':        raw.get('calendarDate'),
        'predictions': _extract_predictions(raw),
    }

# ── TRENDS ────────────────────────────────────────────────────────────────────
def _period_end_dates(period: str, lookback: int) -> list[date]:
    today = date.today()
    if period == 'weekly':
        # Most recent Sunday <= today
        days_since_sunday = (today.weekday() + 1) % 7
        last_sunday = today - timedelta(days=days_since_sunday)
        return [last_sunday - timedelta(weeks=i) for i in range(lookback)]
    # monthly
    dates = []
    year, month = today.year, today.month
    for _ in range(lookback):
        if year == today.year and month == today.month:
            dates.append(today)
        else:
            last_day = monthrange(year, month)[1]
            dates.append(date(year, month, last_day))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return dates

def _extract_vo2max(training_raw: dict) -> dict:
    vo2 = (training_raw or {}).get('mostRecentVO2Max') or {}
    return {
        'running': (vo2.get('generic') or {}).get('vo2MaxPreciseValue'),
        'cycling': (vo2.get('cycling') or {}).get('vo2MaxPreciseValue'),
    }

def get_performance_trends(period: str = 'weekly', lookback: int = 4) -> list[dict]:
    """
    Get trends for HRV and VO2max over recent weeks or months.
    Each data point covers one period and reports the HRV weekly average
    (as recorded on the period's last day) and the most recent VO2max values.
    Args:
        period:   'weekly' or 'monthly'
        lookback: Number of periods to return (max 26 weekly, 12 monthly)
    """
    if period not in ('weekly', 'monthly'):
        raise ValueError("period must be 'weekly' or 'monthly'")
    max_lookback = 26 if period == 'weekly' else 12
    lookback = min(lookback, max_lookback)
    client = get_client()
    results = []
    for d in _period_end_dates(period, lookback):
        date_str = d.isoformat()
        try:
            hrv_raw = client.get_hrv_data(date_str) or {}
        except Exception:
            hrv_raw = {}
        try:
            training_raw = client.get_training_status(date_str) or {}
        except Exception:
            training_raw = {}
        hrv_summary = hrv_raw.get('hrvSummary', {})
        hrv_readings = hrv_raw.get('hrvReadings') or []
        results.append({
            'period_end': date_str,
            'hrv': {
                'weekly_avg':    hrv_summary.get('weeklyAvg'),
                'last_night':    hrv_summary.get('lastNightAvg'),
                'status':        hrv_summary.get('status'),
                'baseline_low':  hrv_summary.get('baseline', {}).get('balancedLow'),
                'baseline_high': hrv_summary.get('baseline', {}).get('balancedUpper'),
                'readings_count': len(hrv_readings),
            },
            'vo2max': _extract_vo2max(training_raw),
        })
    return results