# tests/plan_fakes.py
"""An in-memory stand-in for the training-plan functions in db.py, so the
plan service, routes and MCP tools can be tested without PostgreSQL.
tests/test_training_plan_db.py runs the real SQL when TEST_DATABASE_URL is set.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

import db

SAMPLE_PLAN = {
    "version": "1.0",
    "meta": {
        "id": "test-block-2026", "athlete": "Alex", "event": "Test Block",
        "planStartDate": "2026-09-14", "planEndDate": "2026-09-27", "totalWeeks": 2,
        "createdAt": "2026-09-01T00:00:00Z", "updatedAt": "2026-09-01T00:00:00Z",
    },
    "preferences": {"swim": "meters", "bike": "kilometers", "run": "kilometers"},
    "zones": {
        "bike": {"power": {"ftp": 250}, "hr": {"lthr": 160}},
        "run": {"hr": {"lthr": 170}, "pace": {"thresholdPace": "4:17/km"}},
        "swim": {"css": "2:05/100m", "cssSeconds": 125},
    },
    "phases": [{"name": "Base", "startWeek": 1, "endWeek": 2, "focus": "Aerobic"}],
    "weeks": [
        {
            "weekNumber": 1, "startDate": "2026-09-14", "endDate": "2026-09-20",
            "phase": "Base", "focus": "Start easy", "targetHours": 3, "isRecoveryWeek": False,
            "summary": {"totalHours": 99, "bySport": {}},  # deliberately wrong
            "days": [
                {"date": "2026-09-14", "dayOfWeek": "Monday", "workouts": [
                    {"id": "w1-mon-swim", "sport": "swim", "name": "Easy swim",
                     "durationMinutes": 45, "distanceMeters": 2000},
                ]},
                {"date": "2026-09-15", "dayOfWeek": "Tuesday", "workouts": [
                    {"id": "w1-tue-run", "sport": "run", "name": "Tempo run",
                     "durationMinutes": 50, "distanceKm": 10},
                    {"id": "w1-tue-strength", "sport": "strength", "name": "Strength A",
                     "durationMinutes": 45},
                ]},
                {"date": "2026-09-17", "dayOfWeek": "Thursday", "workouts": [
                    {"id": "w1-thu-bike", "sport": "bike", "name": "Endurance ride",
                     "durationMinutes": 90, "distanceKm": 45},
                ]},
            ],
        },
        {
            "weekNumber": 2, "startDate": "2026-09-21", "endDate": "2026-09-27",
            "phase": "Base", "focus": "Build", "targetHours": 4, "isRecoveryWeek": False,
            "days": [
                {"date": "2026-09-22", "dayOfWeek": "Tuesday", "workouts": [
                    {"id": "w2-tue-run", "sport": "run", "name": "Intervals",
                     "durationMinutes": 55, "distanceKm": 11},
                ]},
            ],
        },
    ],
}


def sample_plan(**meta):
    plan = copy.deepcopy(SAMPLE_PLAN)
    plan["meta"].update(meta)
    return plan


class FakePlanDB:
    def __init__(self):
        self.plans = {}       # id -> row
        self.revisions = {}   # id -> [revision]
        self.states = {}      # (plan_id, workout_id) -> state
        self.activities = {}  # garmin_id -> brief
        self._clock = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)

    def _now(self):
        self._clock += timedelta(seconds=1)
        return self._clock

    def _revision(self, row, source, summary):
        self.revisions.setdefault(row["id"], []).append({
            "version": row["version"], "plan": copy.deepcopy(row["plan"]),
            "source": source, "summary": summary, "created_at": self._now(),
        })

    def _archive_others(self, plan_id):
        for other in self.plans.values():
            if other["status"] == "active" and other["id"] != plan_id:
                other["status"], other["archived_at"] = "archived", self._now()

    # — the db.py API —
    def get_training_plan(self, plan_id=None):
        if plan_id is None:
            row = next((r for r in self.plans.values() if r["status"] == "active"), None)
        else:
            row = self.plans.get(plan_id)
        return copy.deepcopy(row)

    def list_training_plans(self):
        rows = sorted(self.plans.values(), key=lambda r: (r["status"] != "active", -r["updated_at"].timestamp()))
        return [{
            "id": r["id"], "status": r["status"], "version": r["version"],
            "created_at": r["created_at"], "updated_at": r["updated_at"], "archived_at": r["archived_at"],
            "event": r["plan"]["meta"].get("event"), "athlete": r["plan"]["meta"].get("athlete"),
            "start_date": r["plan"]["meta"].get("planStartDate"), "end_date": r["plan"]["meta"].get("planEndDate"),
        } for r in rows]

    def save_uploaded_training_plan(self, plan_id, plan, summary):
        self._archive_others(plan_id)
        now = self._now()
        row = self.plans.get(plan_id)
        if row is None:
            row = {"id": plan_id, "version": 1, "created_at": now, "archived_at": None}
            self.plans[plan_id] = row
        else:
            row["version"] += 1
        row.update(status="active", plan=copy.deepcopy(plan), updated_at=now, archived_at=None)
        self._revision(row, "upload", summary)
        return copy.deepcopy(row)

    def update_training_plan(self, plan_id, mutate, source):
        row = self.plans.get(plan_id)
        if row is None:
            raise LookupError(plan_id)
        new_plan, summary = mutate(copy.deepcopy(row["plan"]))
        row.update(plan=copy.deepcopy(new_plan), version=row["version"] + 1, updated_at=self._now())
        self._revision(row, source, summary)
        return copy.deepcopy(row)

    def set_training_plan_status(self, plan_id, status):
        row = self.plans.get(plan_id)
        if row is None:
            return False
        if status == "active":
            self._archive_others(plan_id)
            row.update(status="active", archived_at=None)
        else:
            row.update(status="archived", archived_at=row["archived_at"] or self._now())
        return True

    def delete_training_plan(self, plan_id):
        self.revisions.pop(plan_id, None)
        self.states = {k: v for k, v in self.states.items() if k[0] != plan_id}
        return self.plans.pop(plan_id, None) is not None

    def list_training_plan_revisions(self, plan_id, limit=100):
        revs = sorted(self.revisions.get(plan_id, []), key=lambda r: -r["version"])[:limit]
        return [{k: r[k] for k in ("version", "source", "summary", "created_at")} for r in revs]

    def last_upload_version(self, plan_id):
        versions = [r["version"] for r in self.revisions.get(plan_id, []) if r["source"] == "upload"]
        return max(versions) if versions else None

    def get_training_plan_revision(self, plan_id, version):
        rev = next((r for r in self.revisions.get(plan_id, []) if r["version"] == version), None)
        return copy.deepcopy(rev)

    def get_workout_states(self, plan_id):
        return {wid: dict(s) for (pid, wid), s in self.states.items() if pid == plan_id}

    def upsert_workout_state(self, plan_id, workout_id, **fields):
        state = self.states.setdefault((plan_id, workout_id), {
            "workout_id": workout_id, "completed": False, "completed_at": None, "activity_id": None,
            "notes": None, "garmin_workout_id": None, "garmin_scheduled_date": None,
        })
        state.update(fields, updated_at=self._now())
        return dict(state)

    def get_activity_brief(self, garmin_id):
        return self.activities.get(garmin_id)

    def add_activity(self, garmin_id, day, activity_type, name="Activity", duration_min=60,
                     distance_km=None, **summary):
        """A synced activity, shaped like an ``activities`` row."""
        self.activities[garmin_id] = {
            "garmin_id": garmin_id, "activity_date": datetime.fromisoformat(f"{day}T07:00:00"),
            "activity_type": activity_type, "name": name, "duration_min": duration_min,
            "distance_km": distance_km, "summary": {"date": f"{day}T07:00:00", **summary},
        }
        return self.activities[garmin_id]

    def get_activities_by_ids(self, garmin_ids):
        return {i: dict(self.activities[i]) for i in garmin_ids if i in self.activities}

    def get_activities_in_range(self, start_date, end_date):
        def day(a):
            return str((a.get("summary") or {}).get("date") or a["activity_date"].isoformat())[:10]
        return sorted((dict(a) for a in self.activities.values() if start_date <= day(a) < end_date), key=day)


PATCHED = [
    "get_training_plan", "list_training_plans", "save_uploaded_training_plan",
    "update_training_plan", "set_training_plan_status", "delete_training_plan",
    "list_training_plan_revisions", "last_upload_version", "get_training_plan_revision",
    "get_workout_states", "upsert_workout_state", "get_activity_brief",
    "get_activities_by_ids", "get_activities_in_range",
]


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakePlanDB()
    monkeypatch.setattr(db, "is_configured", lambda: True)
    for name in PATCHED:
        monkeypatch.setattr(db, name, getattr(fake, name))
    return fake
