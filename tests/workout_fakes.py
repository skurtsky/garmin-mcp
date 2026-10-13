"""In-memory stand-in for the Garmin workout API used by the strength tests."""
import copy
import re


def drop_end_conditions(workout: dict) -> dict:
    """Simulate Garmin silently resetting every exercise step to an open (lap button) step."""
    def walk(steps):
        for st in steps:
            if st.get("type") == "RepeatGroupDTO":
                walk(st["workoutSteps"])
            elif st.get("exerciseName"):
                st["endCondition"] = {"conditionTypeId": 1, "conditionTypeKey": "lap.button",
                                      "displayOrder": 1, "displayable": True}
                st["endConditionValue"] = 0.0
    walk(workout["workoutSegments"][0]["workoutSteps"])
    return workout


def drop_targets(workout: dict) -> dict:
    """Simulate Garmin silently dropping pace/HR/power targets."""
    def walk(steps):
        for st in steps:
            if st.get("type") == "RepeatGroupDTO":
                walk(st["workoutSteps"])
            else:
                st["targetType"] = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
                st["targetValueOne"] = st["targetValueTwo"] = None
    walk(workout["workoutSegments"][0]["workoutSteps"])
    return workout


class _Session:
    def __init__(self, fake):
        self.fake = fake
        self.put_calls = []

    def put(self, domain, path, **kwargs):
        m = re.fullmatch(r"/workout-service/workout/(\d+)", path)
        assert m, path
        wid = int(m.group(1))
        assert wid in self.fake.workouts, "PUT for unknown workout"
        body = copy.deepcopy(kwargs["json"])
        assert body.get("workoutId") == wid
        self.put_calls.append((wid, body))
        self.fake.workouts[wid] = self.fake.transform(body)
        return {}


class FakeGarmin:
    def __init__(self, transform=None):
        self.workouts: dict[int, dict] = {}
        self.schedule: list[dict] = []
        self.transform = transform or (lambda w: w)
        self.next_id = 1000
        self.client = _Session(self)
        self.uploads = 0
        self.deletes = []

    def add(self, workout: dict) -> int:
        wid = self.next_id = self.next_id + 1
        stored = copy.deepcopy(workout)
        stored["workoutId"] = wid
        self.workouts[wid] = stored
        return wid

    def upload_workout(self, payload):
        self.uploads += 1
        return {"workoutId": self.add(self.transform(copy.deepcopy(payload)))}

    def get_workouts(self, start=0, limit=100):
        return [{"workoutId": i, "workoutName": w["workoutName"]} for i, w in self.workouts.items()]

    def get_workout_by_id(self, workout_id):
        return copy.deepcopy(self.workouts[int(workout_id)])

    def delete_workout(self, workout_id):
        self.deletes.append(workout_id)
        self.workouts.pop(int(workout_id), None)

    def schedule_workout(self, workout_id, date):
        self.schedule.append({"workoutId": workout_id, "date": date})
        return {"workoutScheduleId": len(self.schedule)}
