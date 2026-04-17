# tools/profile.py
from garmin_client import get_client

def get_athlete_profile() -> dict:
    """
    Fetch and return key athlete attributes from Garmin user profile.
    
    Returns weight, height, VO2max (running and cycling), lactate threshold
    heart rate and pace, and FTP.
    """
    client = get_client()
    profile = client.get_user_profile()
    data = profile['userData']

    lt_speed_ms = (data.get('lactateThresholdSpeed') or 0) * 10  # convert dm/s to m/s

    return {
        'weight_kg':              round((data.get('weight') or 0) / 1000, 1),
        'height_cm':              data.get('height'),
        'vo2max_running':         data.get('vo2MaxRunning'),
        'vo2max_cycling':         data.get('vo2MaxCycling'),
        'lactate_threshold_hr':   data.get('lactateThresholdHeartRate'),
        'lactate_threshold_pace': round(1 / (lt_speed_ms * 0.06), 2) if lt_speed_ms > 0 else None,
        'ftp':                    data.get('functionalThresholdPower'),
    }