"""Phase 0 research probe for strength-workout editing (read-mostly, TEST-only writes).

Run locally with your Garmin credentials/tokens (same env as the server):

    python scripts/phase0_probe.py inspect <workout_id>        # B: raw JSON of an existing workout (read-only)
    python scripts/phase0_probe.py sets <activity_id>          # D: raw exerciseSets of a strength activity (read-only)
    python scripts/phase0_probe.py put                         # A: in-place PUT + schedule survival (TEST workout)
    python scripts/phase0_probe.py endconds                    # B/C: try time/reps end conditions (TEST workout)

Every write creates a workout named "TEST probe ..." and deletes it (and any
schedule entry it made) in a ``finally`` block. Nothing else is touched.
Paste the output back into the PR / chat.
"""
import copy
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `garmin_client` imports

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from garmin_client import get_client  # noqa: E402
from tools.workout import build_workout_payload, _extract_uploaded_workout_id  # noqa: E402

SCHEDULE_DATE = (date.today() + timedelta(days=400)).isoformat()


def dump(label, obj):
    print(f"\n=== {label} ===")
    print(json.dumps(obj, indent=2, default=str))


def _steps(workout):
    return workout["workoutSegments"][0]["workoutSteps"]


def _cleanup(client, workout_id, schedule_ids):
    for sid in schedule_ids:
        try:
            client.unschedule_workout(sid)
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup: unschedule {sid} failed: {exc}")
    if workout_id:
        try:
            client.delete_workout(workout_id)
            print(f"cleanup: deleted TEST workout {workout_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup: delete {workout_id} failed: {exc}")


def probe_put(client):
    payload = build_workout_payload("TEST probe put", "strength_training", [
        {"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT", "weight_kg": 20},
    ])
    wid, sched = None, []
    try:
        wid = _extract_uploaded_workout_id(client.upload_workout(payload) or {})
        print("created", wid)
        res = client.schedule_workout(wid, SCHEDULE_DATE)
        sched.append(res.get("workoutScheduleId"))
        full = client.get_workout_by_id(wid)
        edited = copy.deepcopy(full)
        for s in _steps(edited)[0]["workoutSteps"]:
            if s.get("exerciseName"):
                s["weightValue"] = 25.0
        url = f"/workout-service/workout/{wid}"
        try:
            put_res = client.client.put("connectapi", url, json=edited, api=True)
            dump("PUT response", put_res)
        except Exception as exc:  # noqa: BLE001
            print("PUT FAILED:", repr(exc))
            return
        after = client.get_workout_by_id(wid)
        w = [s for s in _steps(after)[0]["workoutSteps"] if s.get("exerciseName")][0]["weightValue"]
        print(f"id unchanged: {after.get('workoutId') == wid}; weight now {w} (expected 25.0)")
        y, m, _ = SCHEDULE_DATE.split("-")
        cal = client.get_scheduled_workouts(int(y), int(m))
        hits = [i for i in cal.get("calendarItems", []) if i.get("workoutId") == wid]
        print(f"schedule entries still pointing at {wid}: {len(hits)}")
        dump("scheduled hits", hits)
    finally:
        _cleanup(client, wid, [s for s in sched if s])


def probe_endconds(client):
    """Upload TEST steps with candidate end conditions and show what Garmin stores."""
    candidates = {
        "time": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
        "reps": {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True},
        "lap": {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True},
    }
    for exercise in ("SIDE_PLANK", "WEIGHTED_SIDE_PLANK", "PUSH_UP"):
        for label, cond in candidates.items():
            value = {"time": 30.0, "reps": 10.0, "lap": 0.0}[label]
            payload = build_workout_payload(f"TEST probe {exercise} {label}", "strength_training", [
                {"type": "repeat", "sets": 2, "exercise_name": exercise},
            ])
            inner = _steps(payload)[0]["workoutSteps"]
            inner[0]["endCondition"], inner[0]["endConditionValue"] = cond, value
            wid = None
            try:
                wid = _extract_uploaded_workout_id(client.upload_workout(payload) or {})
                got = _steps(client.get_workout_by_id(wid))[0]["workoutSteps"][0]
                print(f"{exercise:22} {label:5} -> stored endCondition="
                      f"{(got.get('endCondition') or {}).get('conditionTypeKey')} value={got.get('endConditionValue')}")
            except Exception as exc:  # noqa: BLE001
                print(f"{exercise:22} {label:5} -> REJECTED: {exc!r}")
            finally:
                _cleanup(client, wid, [])


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    client = get_client()
    cmd = sys.argv[1]
    if cmd == "inspect":
        dump("raw workout", client.get_workout_by_id(int(sys.argv[2])))
    elif cmd == "sets":
        dump("exerciseSets", client.get_activity_exercise_sets(int(sys.argv[2])))
    elif cmd == "put":
        probe_put(client)
    elif cmd == "endconds":
        probe_endconds(client)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
