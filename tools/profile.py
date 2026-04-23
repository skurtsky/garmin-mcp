# tools/profile.py
from garmin_client import get_client
from datetime import date as _date

def get_athlete_profile() -> dict:
    """
    Fetch and return key athlete attributes from Garmin user profile.

    Returns weight, height, VO2max (running and cycling), lactate threshold
    heart rate and pace, FTP (from cycling FTP endpoint), and 7-day average
    resting heart rate.
    """
    client = get_client()
    profile = client.get_user_profile()
    data = profile['userData']

    lt_speed_ms = (data.get('lactateThresholdSpeed') or 0) * 10  # convert dm/s to m/s

    # FTP from the dedicated cycling FTP endpoint — userData.functionalThresholdPower is null
    try:
        ftp_data = client.get_cycling_ftp() or {}
        ftp = ftp_data.get('functionalThresholdPower')
    except Exception:
        ftp = None

    # 7-day average resting HR from today's user summary
    try:
        summary = client.get_user_summary(_date.today().isoformat()) or {}
        resting_hr_7day_avg = summary.get('lastSevenDaysAvgRestingHeartRate')
    except Exception:
        resting_hr_7day_avg = None

    return {
        'weight_kg':              round((data.get('weight') or 0) / 1000, 1),
        'height_cm':              data.get('height'),
        'vo2max_running':         data.get('vo2MaxRunning'),
        'vo2max_cycling':         data.get('vo2MaxCycling'),
        'lactate_threshold_hr':   data.get('lactateThresholdHeartRate'),
        'lactate_threshold_pace': round(1 / (lt_speed_ms * 0.06), 2) if lt_speed_ms > 0 else None,
        'ftp':                    ftp,
        'resting_hr_7day_avg':    resting_hr_7day_avg,
    }


def get_gear() -> list[dict]:
    """
    Fetch the athlete's registered gear (shoes, bikes, etc.) with name,
    model, activity type, total distance and activity count, max distance
    threshold, and current status.
    """
    client = get_client()
    device_info = client.get_device_last_used() or {}
    user_profile_number = device_info.get('userProfileNumber')

    if not user_profile_number:
        return []

    gear_raw = client.get_gear(user_profile_number) or []

    items = []
    for g in gear_raw:
        uuid = g.get('uuid')
        stats = (client.get_gear_stats(uuid) or {}) if uuid else {}

        name = g.get('displayName') or g.get('customMakeModel')
        model = g.get('customMakeModel') if g.get('displayName') else None
        max_meters = g.get('maximumMeters') or 0
        total_distance = stats.get('totalDistance') or 0  # metres

        items.append({
            'name':             name,
            'model':            model,
            'activity_type':    g.get('gearTypeName'),
            'status':           g.get('gearStatusName'),
            'distance_km':      round(total_distance / 1000, 2),
            'total_activities': stats.get('totalActivities'),
            'max_distance_km':  round(max_meters / 1000, 2) if max_meters else None,
            'date_begin':       g.get('dateBegin'),
            'date_end':         g.get('dateEnd'),
        })

    return items
