# tools/trends.py
from garmin_client import get_client
from datetime import date, timedelta
from calendar import monthrange
from typing import Optional

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


# ── ROLLING TRENDS ──────────────────────────────────────────────────────────
# get_trends aggregates per-day health/performance metrics over a window and
# returns per-day values plus rolling 7d/28d averages, start→end deltas, and
# min/max — so a single call replaces dozens of individual per-day lookups.

_PERIOD_DAYS = {
    '7d':  7,
    '14d': 14,
    '1m':  30,
    '42d': 42,
    '3m':  90,
    '6m':  180,
    '1y':  365,
}

# Canonical metric keys the caller can request. 'body_battery' expands into two
# output series (wake value and drain); every other metric is a single series.
_ALL_METRICS = ['rhr', 'hrv', 'sleep_score', 'body_battery',
                'stress', 'steps', 'training_load']

# Accepted aliases → canonical metric key.
_METRIC_ALIASES = {
    'rhr':               'rhr',
    'resting_heart_rate': 'rhr',
    'resting_hr':        'rhr',
    'hrv':               'hrv',
    'sleep_score':       'sleep_score',
    'sleep':             'sleep_score',
    'body_battery':      'body_battery',
    'bodybattery':       'body_battery',
    'stress':            'stress',
    'steps':             'steps',
    'training_load':     'training_load',
    'load':              'training_load',
}

# Units for each output series (body_battery expands to _wake / _drain).
_SERIES_UNITS = {
    'rhr':                 'bpm',
    'hrv':                 'ms',
    'sleep_score':         'score',
    'body_battery_wake':   'level',
    'body_battery_drain':  'level',
    'stress':              'level',
    'steps':               'steps',
    'training_load':       'load',
}

# The 28-day-per-request cap Garmin enforces on ranged endpoints.
_RANGE_CHUNK_DAYS = 28


def _resolve_metrics(metrics: Optional[list]) -> list:
    """Normalise the requested metrics to canonical keys, defaulting to all."""
    if not metrics:
        return list(_ALL_METRICS)
    resolved = []
    for m in metrics:
        key = _METRIC_ALIASES.get(str(m).strip().lower())
        if key is None:
            raise ValueError(
                f"Unknown metric '{m}'. Valid metrics: {', '.join(_ALL_METRICS)}"
            )
        if key not in resolved:
            resolved.append(key)
    return resolved


def _rolling_average(series: list, window: int) -> list:
    """Trailing rolling average over a chronological list of (date, value).

    Each output point averages the non-null values in the trailing `window`
    days (that day plus the preceding window-1). Days with no data in the
    window get None.
    """
    values = [v for _, v in series]
    out = []
    for i, (d, _) in enumerate(series):
        lo = max(0, i - window + 1)
        present = [v for v in values[lo:i + 1] if v is not None]
        out.append({
            'date':  d,
            'value': round(sum(present) / len(present), 1) if present else None,
        })
    return out


def _summarize(series: list) -> dict:
    """Start/end/delta and min/max/avg over the non-null values of a series."""
    present = [(d, v) for d, v in series if v is not None]
    if not present:
        return {'start': None, 'end': None, 'delta': None,
                'min': None, 'max': None, 'avg': None}
    start_val = present[0][1]
    end_val = present[-1][1]
    values = [v for _, v in present]
    return {
        'start': start_val,
        'end':   end_val,
        'delta': round(end_val - start_val, 1),
        'min':   min(values),
        'max':   max(values),
        'avg':   round(sum(values) / len(values), 1),
    }


def _build_series(key: str, dates: list, values_by_date: dict) -> dict:
    """Assemble one metric's output block from a date→value map."""
    series = [(d, values_by_date.get(d)) for d in dates]
    return {
        'unit':        _SERIES_UNITS.get(key),
        'daily':       [{'date': d, 'value': v} for d, v in series],
        'rolling_7d':  _rolling_average(series, 7),
        'rolling_28d': _rolling_average(series, 28),
        **_summarize(series),
    }


def _date_chunks(start: date, end: date, size: int):
    """Yield (chunk_start, chunk_end) ISO pairs no wider than `size` days."""
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=size - 1), end)
        yield cur.isoformat(), chunk_end.isoformat()
        cur = chunk_end + timedelta(days=1)


def _fetch_body_battery(client, start: date, end: date) -> tuple[dict, dict]:
    """Return (wake_by_date, drain_by_date) for the window.

    The wake value is the day's peak body-battery level (body battery is highest
    at wake, after overnight charging); drain is the day's total drop. Chunked to
    stay under Garmin's 28-day-per-request cap.
    """
    wake: dict = {}
    drain: dict = {}
    for chunk_start, chunk_end in _date_chunks(start, end, _RANGE_CHUNK_DAYS):
        try:
            raw = client.get_body_battery(chunk_start, chunk_end) or []
        except Exception:
            raw = []
        for day in raw:
            d = day.get('date') or day.get('calendarDate')
            if not d:
                continue
            drain[d] = day.get('drained')
            arr = day.get('bodyBatteryValuesArray') or []
            levels = [row[2] for row in arr
                      if isinstance(row, (list, tuple)) and len(row) > 2
                      and isinstance(row[2], (int, float))]
            wake[d] = max(levels) if levels else None
    return wake, drain


def _fetch_steps(client, start: date, end: date) -> dict:
    """Return a date→total-steps map (get_daily_steps chunks internally)."""
    steps: dict = {}
    try:
        raw = client.get_daily_steps(start.isoformat(), end.isoformat()) or []
    except Exception:
        raw = []
    for day in raw:
        d = day.get('calendarDate')
        if d:
            steps[d] = day.get('totalSteps')
    return steps


def _extract_training_load(training_raw: dict):
    """Pull the daily acute training load from a get_training_status payload."""
    ts = (training_raw or {}).get('mostRecentTrainingStatus', {}) \
                             .get('latestTrainingStatusData', {}) or {}
    device_id = next(iter(ts), None) if ts else None
    ts_data = ts.get(device_id, {}) if device_id else {}
    acute = ts_data.get('acuteTrainingLoadDTO', {}) or {}
    for load_key in ('dailyTrainingLoadAcute', 'acuteTrainingLoad',
                     'dailyAcuteTrainingLoad'):
        if acute.get(load_key) is not None:
            return acute[load_key]
    return None


def get_trends(period: str = '1m', metrics: Optional[list] = None) -> dict:
    """
    Aggregate health & performance metrics over a trailing window with rolling
    averages, so a trend view needs one call instead of dozens of per-day ones.

    Supported periods: 7d, 14d, 1m (30d), 42d, 3m (90d), 6m (180d), 1y (365d).

    Metrics (all optional, defaults to all):
        rhr           — resting heart rate (from get_heart_rates)
        hrv           — overnight HRV last-night avg (from get_hrv_data)
        sleep_score   — overall sleep score (from get_sleep_data)
        body_battery  — daily peak/wake level and total drain (from get_body_battery)
        stress        — all-day average stress (from get_all_day_stress)
        steps         — daily total steps (from get_daily_steps)
        training_load — daily acute training load (from get_training_status)

    For each output series the result carries per-day values, trailing rolling
    7-day and 28-day averages, the period start→end value and delta, and the
    window min/max/avg. 'body_battery' expands into two series
    ('body_battery_wake', 'body_battery_drain').

    Args:
        period:  One of 7d, 14d, 1m, 42d, 3m, 6m, 1y (default 1m).
        metrics: Optional list of metric names to include (defaults to all).
    """
    if period not in _PERIOD_DAYS:
        raise ValueError(
            f"Unknown period '{period}'. Valid periods: "
            f"{', '.join(_PERIOD_DAYS)}"
        )
    requested = _resolve_metrics(metrics)

    days = _PERIOD_DAYS[period]
    end = date.today()
    start = end - timedelta(days=days - 1)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    client = get_client()

    # Per-day scalar metrics — one dedicated lookup per day, degrading to None.
    per_day_values = {
        'rhr':           {},
        'hrv':           {},
        'sleep_score':   {},
        'stress':        {},
        'training_load': {},
    }
    per_day_active = [m for m in per_day_values if m in requested]

    for d in dates:
        if 'rhr' in requested:
            try:
                hr = client.get_heart_rates(d) or {}
                per_day_values['rhr'][d] = hr.get('restingHeartRate')
            except Exception:
                pass
        if 'hrv' in requested:
            try:
                hrv = (client.get_hrv_data(d) or {}).get('hrvSummary', {}) or {}
                per_day_values['hrv'][d] = hrv.get('lastNightAvg')
            except Exception:
                pass
        if 'sleep_score' in requested:
            try:
                dto = (client.get_sleep_data(d) or {}).get('dailySleepDTO', {}) or {}
                per_day_values['sleep_score'][d] = \
                    (dto.get('sleepScores', {}) or {}).get('overall', {}).get('value')
            except Exception:
                pass
        if 'stress' in requested:
            try:
                stress = client.get_all_day_stress(d) or {}
                per_day_values['stress'][d] = stress.get('avgStressLevel')
            except Exception:
                pass
        if 'training_load' in requested:
            try:
                per_day_values['training_load'][d] = \
                    _extract_training_load(client.get_training_status(d) or {})
            except Exception:
                pass

    out_metrics: dict = {}
    for key in per_day_active:
        out_metrics[key] = _build_series(key, dates, per_day_values[key])

    # Ranged metrics — a handful of chunked calls instead of one per day.
    if 'body_battery' in requested:
        wake, drain = _fetch_body_battery(client, start, end)
        out_metrics['body_battery_wake'] = _build_series('body_battery_wake', dates, wake)
        out_metrics['body_battery_drain'] = _build_series('body_battery_drain', dates, drain)

    if 'steps' in requested:
        out_metrics['steps'] = _build_series('steps', dates, _fetch_steps(client, start, end))

    return {
        'period':     period,
        'start_date': start.isoformat(),
        'end_date':   end.isoformat(),
        'days':       days,
        'metrics':    out_metrics,
    }