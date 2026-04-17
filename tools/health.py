# tools/health.py
from garmin_client import get_client


def get_sleep(date: str) -> dict:
    """
    Get sleep quality and recovery metrics for a given date.

    Args:
        date: Date string in YYYY-MM-DD format
    """
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
    Get readiness metrics for a given date — HRV, body battery, and training status.

    Args:
        date: Date string in YYYY-MM-DD format
    """
    client = get_client()

    hrv_raw          = client.get_hrv_data(date)
    body_battery_raw = client.get_body_battery(date)
    training_raw     = client.get_training_status(date)

    # HRV
    hrv = hrv_raw.get('hrvSummary', {})
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
        'charged':  bb.get('charged'),
        'drained':  bb.get('drained'),
        'events':   bb_events,
    }

    # Training status
    ts = training_raw.get('mostRecentTrainingStatus', {}) \
                     .get('latestTrainingStatusData', {})
    device_id = list(ts.keys())[0] if ts else None
    ts_data = ts.get(device_id, {}) if device_id else {}
    acwr = ts_data.get('acuteTrainingLoadDTO', {})

    load_balance = training_raw.get('mostRecentTrainingLoadBalance', {}) \
                               .get('metricsTrainingLoadBalanceDTOMap', {})
    lb_data = load_balance.get(device_id, {}) if device_id else {}

    training_clean = {
        'status':        ts_data.get('trainingStatusFeedbackPhrase'),
        'sport':         ts_data.get('sport'),
        'acwr':          acwr.get('dailyAcuteChronicWorkloadRatio'),
        'acwr_status':   acwr.get('acwrStatus'),
        'load_balance':  lb_data.get('trainingBalanceFeedbackPhrase'),
    }

    # VO2max
    vo2 = training_raw.get('mostRecentVO2Max', {})
    vo2_clean = {
        'running': vo2.get('generic', {}).get('vo2MaxPreciseValue'),
        'cycling': vo2.get('cycling', {}).get('vo2MaxPreciseValue'),
    }

    return {
        'date':             date,
        'hrv':              hrv_clean,
        'body_battery':     bb_clean,
        'training_status':  training_clean,
        'vo2max':           vo2_clean,
    }