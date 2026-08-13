# TEMPORARY DIAGNOSTIC — remove before merge.
import json
from garmin_client import get_client

CHILD_IDS = [23901176862, 23901176863, 23901176865, 23901176868, 23901176869]


def test_diag_multisport_children():
    client = get_client()
    out = []
    for cid in CHILD_IDS:
        entry = {'id': cid}
        try:
            raw = client.get_activity(cid)
            s = raw.get('summaryDTO') or {}
            entry['name'] = raw.get('activityName')
            entry['type'] = (raw.get('activityTypeDTO') or {}).get('typeKey')
            entry['summary_keys'] = sorted(s.keys())
            entry['summary'] = {k: s.get(k) for k in (
                'distance', 'duration', 'movingDuration', 'averageSpeed',
                'averageHR', 'maxHR', 'calories', 'averagePower',
                'normalizedPower', 'elevationGain', 'startTimeLocal',
                'poolLength', 'numberOfActiveLengths', 'averageSwimCadence',
                'averageSWOLF', 'totalNumberOfStrokes')}
        except Exception as e:
            entry['activity_error'] = repr(e)
        try:
            splits = client.get_activity_splits(cid) or {}
            laps = splits.get('lapDTOs') or []
            entry['lap_count'] = len(laps)
            entry['first_lap_keys'] = sorted(laps[0].keys()) if laps else []
        except Exception as e:
            entry['splits_error'] = repr(e)
        out.append(entry)
    raise AssertionError("DIAG>>> " + json.dumps(out, default=str, indent=1))
