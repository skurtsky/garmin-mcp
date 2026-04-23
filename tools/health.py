# tools/health.py
from garmin_client import get_client
from datetime import date, timedelta

def resolve_date(date_str: str) -> str:
    """Resolve 'today' and 'yesterday' to ISO date strings.

    Note: Garmin files sleep under the wake-up date, so 'last night's sleep'
    should pass today's date, not yesterday's.
    """
    if date_str == 'today':
        return date.today().isoformat()
    if date_str == 'yesterday':
        return (date.today() - timedelta(days=1)).isoformat()
    return date_str

def get_sleep(date: str) -> dict:
    """
    Get sleep quality and recovery metrics for a given date.

    Args:
        date: Date string in YYYY-MM-DD format
    """
    date = resolve_date(date)

    client = get_client()
    sleep_raw = client.get_sleep_data(date)

    dto = sleep_raw['dailySleepDTO']
    scores = dto.get('sleepScores', {})
    sleep_need = dto.get('sleepNeed', {})

    total_secs = dto.get('sleepTimeSeconds') or 0

    def pct(secs):
        return round(secs / total_secs * 100, 1) if total_secs else None

    deep_secs  = dto.get('deepSleepSeconds') or 0
    light_secs = dto.get('lightSleepSeconds') or 0
    rem_secs   = dto.get('remSleepSeconds') or 0
    awake_secs = dto.get('awakeSleepSeconds') or 0

    return {
        'date':                 dto.get('calendarDate'),
        'sleep_score':          scores.get('overall', {}).get('value'),
        'sleep_score_label':    scores.get('overall', {}).get('qualifierKey'),
        'total_sleep_hrs':      round(total_secs / 3600, 2),
        'deep_sleep_hrs':       round(deep_secs / 3600, 2),
        'light_sleep_hrs':      round(light_secs / 3600, 2),
        'rem_sleep_hrs':        round(rem_secs / 3600, 2),
        'awake_hrs':            round(awake_secs / 3600, 2),
        'deep_pct':             pct(deep_secs),
        'light_pct':            pct(light_secs),
        'rem_pct':              pct(rem_secs),
        'awake_count':          dto.get('awakeCount'),
        'avg_hr':               dto.get('avgHeartRate'),
        'resting_hr':           sleep_raw.get('restingHeartRate'),
        'avg_hrv':              sleep_raw.get('avgOvernightHrv'),
        'hrv_status':           sleep_raw.get('hrvStatus'),
        'avg_respiration':      dto.get('averageRespirationValue'),
        'avg_stress':           dto.get('avgSleepStress'),
        'body_battery_change':  sleep_raw.get('bodyBatteryChange'),
        'sleep_need_hrs':       round((sleep_need.get('actual') or 0) / 60, 1),
        'sleep_need_feedback':  sleep_need.get('feedback'),
        'feedback':             dto.get('sleepScoreFeedback'),
    }


def get_daily_readiness(date: str) -> dict:
    """
    Get recovery-focused daily readiness metrics for a given date — HRV,
    body battery (with start/current/highest/lowest levels), and daily
    activity & stress stats (RHR, stress, steps).

    Args:
        date: Date string in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    date = resolve_date(date)

    client = get_client()

    hrv_raw          = client.get_hrv_data(date)
    body_battery_raw = client.get_body_battery(date)
    summary_raw      = client.get_user_summary(date) or {}

    # HRV
    hrv = (hrv_raw or {}).get('hrvSummary', {})
    hrv_clean = {
        'last_night_avg':  hrv.get('lastNightAvg'),
        'weekly_avg':      hrv.get('weeklyAvg'),
        'status':          hrv.get('status'),
        'baseline_low':    hrv.get('baseline', {}).get('balancedLow'),
        'baseline_high':   hrv.get('baseline', {}).get('balancedUpper'),
        'feedback':        hrv.get('feedbackPhrase'),
    }

    # Body battery
    bb = body_battery_raw[0] if body_battery_raw else {}
    bb_events = [
        {
            'type':     e.get('eventType'),
            'impact':   e.get('bodyBatteryImpact'),
            'feedback': e.get('shortFeedback'),
        }
        for e in bb.get('bodyBatteryActivityEvent', [])
    ]

    bb_clean = {
        'charged':       bb.get('charged'),
        'drained':       bb.get('drained'),
        'start_level':   summary_raw.get('bodyBatteryAtWakeTime'),
        'current_level': summary_raw.get('bodyBatteryMostRecentValue'),
        'highest':       summary_raw.get('bodyBatteryHighestValue'),
        'lowest':        summary_raw.get('bodyBatteryLowestValue'),
        'during_sleep':  summary_raw.get('bodyBatteryDuringSleep'),
        'feedback':      (summary_raw.get('bodyBatteryDynamicFeedbackEvent') or {}).get('feedbackShortType'),
        'events':        bb_events,
    }

    # Daily activity & stress stats from user summary
    daily_stats = {
        'resting_hr':          summary_raw.get('restingHeartRate'),
        'resting_hr_7day_avg': summary_raw.get('lastSevenDaysAvgRestingHeartRate'),
        'avg_stress':          summary_raw.get('averageStressLevel'),
        'max_stress':          summary_raw.get('maxStressLevel'),
        'total_steps':         summary_raw.get('totalSteps'),
        'active_seconds':      summary_raw.get('activeSeconds'),
    }

    return {
        'date':         date,
        'hrv':          hrv_clean,
        'body_battery': bb_clean,
        'daily_stats':  daily_stats,
    }


def get_training_status(date: str) -> dict:
    """
    Get training status for a given date — acute:chronic workload ratio,
    training load balance, training status phrase, sport, and VO2max
    (running and cycling).

    Args:
        date: Date string in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    date = resolve_date(date)

    client = get_client()
    training_raw = client.get_training_status(date) or {}

    # Training status (per-device map — take the first device)
    ts = training_raw.get('mostRecentTrainingStatus', {}) \
                     .get('latestTrainingStatusData', {}) or {}
    device_id = next(iter(ts), None) if ts else None
    ts_data = ts.get(device_id, {}) if device_id else {}
    acwr = ts_data.get('acuteTrainingLoadDTO', {}) or {}

    load_balance = training_raw.get('mostRecentTrainingLoadBalance', {}) \
                               .get('metricsTrainingLoadBalanceDTOMap', {}) or {}
    lb_data = load_balance.get(device_id, {}) if device_id else {}

    # VO2max
    vo2 = training_raw.get('mostRecentVO2Max', {}) or {}
    vo2_clean = {
        'running': (vo2.get('generic') or {}).get('vo2MaxPreciseValue'),
        'cycling': (vo2.get('cycling') or {}).get('vo2MaxPreciseValue'),
    }

    return {
        'date':         date,
        'status':       ts_data.get('trainingStatusFeedbackPhrase'),
        'sport':        ts_data.get('sport'),
        'acwr':         acwr.get('dailyAcuteChronicWorkloadRatio'),
        'acwr_status':  acwr.get('acwrStatus'),
        'load_balance': lb_data.get('trainingBalanceFeedbackPhrase'),
        'vo2max':       vo2_clean,
    }


def get_training_readiness(date: str) -> dict:
    """
    Get training readiness for a given date — score, recovery status, and
    feedback phrases, plus the morning readiness snapshot.

    Args:
        date: Date string in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    date = resolve_date(date)

    client = get_client()

    tr_raw  = client.get_training_readiness(date) or []
    mtr_raw = client.get_morning_training_readiness(date) or {}

    # Training readiness response is a list ordered most-recent first.
    tr = tr_raw[0] if tr_raw else {}

    readiness = {
        'score':                         tr.get('score'),
        'level':                         tr.get('level'),
        'feedback_long':                 tr.get('feedbackLong'),
        'feedback_short':                tr.get('feedbackShort'),
        'sleep_score':                   tr.get('sleepScore'),
        'sleep_score_factor_percent':    tr.get('sleepScoreFactorPercent'),
        'sleep_score_factor_feedback':   tr.get('sleepScoreFactorFeedback'),
        'recovery_time':                 tr.get('recoveryTime'),
        'recovery_time_factor_percent':  tr.get('recoveryTimeFactorPercent'),
        'recovery_time_factor_feedback': tr.get('recoveryTimeFactorFeedback'),
        'acwr_factor_percent':           tr.get('acwrFactorPercent'),
        'acwr_factor_feedback':          tr.get('acwrFactorFeedback'),
        'acute_load':                    tr.get('acuteLoad'),
        'stress_history_factor_percent': tr.get('stressHistoryFactorPercent'),
        'stress_history_factor_feedback': tr.get('stressHistoryFactorFeedback'),
        'hrv_factor_percent':            tr.get('hrvFactorPercent'),
        'hrv_factor_feedback':           tr.get('hrvFactorFeedback'),
        'hrv_weekly_average':            tr.get('hrvWeeklyAverage'),
        'sleep_history_factor_percent':  tr.get('sleepHistoryFactorPercent'),
        'sleep_history_factor_feedback': tr.get('sleepHistoryFactorFeedback'),
        'valid_sleep':                   tr.get('validSleep'),
        'input_context':                 tr.get('inputContext'),
        'recovery_time_change_phrase':   tr.get('recoveryTimeChangePhrase'),
        'timestamp_local':               tr.get('timestampLocal'),
    }

    morning = {
        'score':                       mtr_raw.get('score'),
        'level':                       mtr_raw.get('level'),
        'feedback_long':               mtr_raw.get('feedbackLong'),
        'feedback_short':              mtr_raw.get('feedbackShort'),
        'sleep_score':                 mtr_raw.get('sleepScore'),
        'recovery_time':               mtr_raw.get('recoveryTime'),
        'valid_sleep':                 mtr_raw.get('validSleep'),
        'recovery_time_change_phrase': mtr_raw.get('recoveryTimeChangePhrase'),
        'timestamp_local':             mtr_raw.get('timestampLocal'),
    }

    return {
        'date':       date,
        'readiness':  readiness,
        'morning':    morning,
    }
