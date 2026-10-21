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
        self.calendar = None  # the calendar_settings row
        self.settings = {}    # app_settings key -> JSON value
        self.idempotency = {}  # key -> {plan_id, request_hash, result}
        self.seasons = {}      # id -> season row (no races / plan ids)
        self.season_races = {}  # id -> race row
        self.plan_seasons = {}  # plan id -> season id
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
        order = {"active": 0, "future": 1, "archived": 2}
        rows = sorted(self.plans.values(), key=lambda r: (
            order[r["status"]],
            r["plan"]["meta"].get("planStartDate") or "" if r["status"] == "future" else "",
            -r["updated_at"].timestamp()))
        return [{
            "id": r["id"], "status": r["status"], "version": r["version"],
            "created_at": r["created_at"], "updated_at": r["updated_at"], "archived_at": r["archived_at"],
            "event": r["plan"]["meta"].get("event"), "athlete": r["plan"]["meta"].get("athlete"),
            "start_date": r["plan"]["meta"].get("planStartDate"), "end_date": r["plan"]["meta"].get("planEndDate"),
        } for r in rows]

    def save_uploaded_training_plan(self, plan_id, plan, summary, activate=True):
        if activate:
            self._archive_others(plan_id)
        now = self._now()
        row = self.plans.get(plan_id)
        if row is None:
            row = {"id": plan_id, "version": 0, "created_at": now, "archived_at": None,
                   "status": "active" if activate else "future"}
            self.plans[plan_id] = row
        row["version"] += 1
        if activate:
            row["status"] = "active"
        elif row["status"] == "archived":
            row["status"] = "future"
        row.update(plan=copy.deepcopy(plan), updated_at=now, archived_at=None)
        self._revision(row, "upload", summary)
        return copy.deepcopy(row)

    def get_idempotent_result(self, key):
        found = self.idempotency.get(key)
        return copy.deepcopy({"request_hash": found["request_hash"], "result": found["result"]}) if found else None

    def create_training_plan(self, plan_id, plan, status, summary, replace, build_result,
                             idempotency_key=None, request_hash=None):
        found = self.idempotency.get(idempotency_key) if idempotency_key else None
        if found:
            if found["request_hash"] != request_hash:
                raise db.IdempotencyKeyReused(idempotency_key)
            return None, copy.deepcopy(found["result"]), True
        row = self.plans.get(plan_id)
        if row is not None and not replace:
            raise db.PlanExists(row["status"])
        if row is not None and row["status"] == "active":
            raise db.PlanIsActive(plan_id)
        now = self._now()
        if row is None:
            row = self.plans[plan_id] = {"id": plan_id, "version": 0, "created_at": now}
        row["version"] += 1
        row.update(plan=copy.deepcopy(plan), status=status, updated_at=now,
                   archived_at=now if status == "archived" else None)
        self._revision(row, "upload", summary)
        result = build_result(copy.deepcopy(row), "created" if row["version"] == 1 else "replaced")
        if idempotency_key:
            self.idempotency[idempotency_key] = {"plan_id": plan_id, "request_hash": request_hash,
                                                 "result": copy.deepcopy(result)}
        return copy.deepcopy(row), result, False

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
        elif status == "future":
            row.update(status="future", archived_at=None)
        else:
            row.update(status="archived", archived_at=row["archived_at"] or self._now())
        return True

    def delete_training_plan(self, plan_id):
        self.revisions.pop(plan_id, None)
        self.idempotency = {k: v for k, v in self.idempotency.items() if v["plan_id"] != plan_id}
        self.states = {k: v for k, v in self.states.items() if k[0] != plan_id}
        self.plan_seasons.pop(plan_id, None)
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

    def get_plans_with_completion(self, plan_ids):
        rows = [copy.deepcopy(self.plans[i]) for i in plan_ids if i in self.plans]
        done = {}
        for (pid, wid), st in self.states.items():
            if st.get("completed") and pid in plan_ids:
                done.setdefault(pid, set()).add(wid)
        return rows, done

    def get_workout_states(self, plan_id):
        return {wid: dict(s) for (pid, wid), s in self.states.items() if pid == plan_id}

    def upsert_workout_state(self, plan_id, workout_id, **fields):
        state = self.states.setdefault((plan_id, workout_id), {
            "workout_id": workout_id, "completed": False, "completed_at": None, "activity_id": None,
            "notes": None, "garmin_workout_id": None, "garmin_scheduled_date": None,
            "skipped": None, "skipped_at": None, "skipped_auto": False, "skip_reason": None,
        })
        state.update(fields, updated_at=self._now())
        return dict(state)

    def auto_skip_workouts(self, plan_id, workout_ids):
        done = []
        for wid in workout_ids:
            state = self.states.get((plan_id, wid))
            if state and (state.get("skipped") is not None or state.get("completed")):
                continue
            self.upsert_workout_state(plan_id, wid, skipped=True, skipped_at=self._now(), skipped_auto=True)
            done.append(wid)
        return done

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

    def get_calendar_settings(self):
        return copy.deepcopy(self.calendar)

    def _calendar_row(self):
        if self.calendar is None:
            self.calendar = {"default_time": "06:00", "slots": [], "feed_token": None,
                             "token_created_at": None}
        self.calendar["updated_at"] = self._now()
        return self.calendar

    def save_calendar_schedule(self, default_time, slots):
        self._calendar_row().update(default_time=default_time, slots=copy.deepcopy(slots))
        return copy.deepcopy(self.calendar)

    def set_calendar_token(self, token):
        self._calendar_row().update(feed_token=token, token_created_at=self._now() if token else None)
        return copy.deepcopy(self.calendar)

    def get_activities_by_ids(self, garmin_ids):
        return {i: dict(self.activities[i]) for i in garmin_ids if i in self.activities}

    def get_activities_in_range(self, start_date, end_date):
        def day(a):
            return str((a.get("summary") or {}).get("date") or a["activity_date"].isoformat())[:10]
        return sorted((dict(a) for a in self.activities.values() if start_date <= day(a) < end_date), key=day)

    def delete_activities_not_in(self, keep_ids, start=None, end=None, after=None):
        def stamp(a):
            return str((a.get("summary") or {}).get("date") or a["activity_date"].isoformat())
        removed = [
            gid for gid, a in self.activities.items()
            if gid not in keep_ids and (start is None or stamp(a) >= start)
            and (end is None or stamp(a) < end) and (after is None or stamp(a) > after)
        ]
        for gid in removed:
            del self.activities[gid]
        for state in self.states.values():
            if state.get("activity_id") in removed:
                state["activity_id"] = None
        return removed

    # — seasons —
    def _season(self, row):
        races = sorted((copy.deepcopy(r) for r in self.season_races.values() if r["season_id"] == row["id"]),
                       key=lambda r: (r["race_date"], r["id"]))
        plan_ids = sorted(p for p, s in self.plan_seasons.items() if s == row["id"])
        return {**copy.deepcopy(row), "races": races, "plan_ids": plan_ids}

    def list_seasons(self):
        rows = sorted(self.seasons.values(), key=lambda r: r["start_date"], reverse=True)
        rows.sort(key=lambda r: r["status"] == "archived")   # stable: newest first within each group
        return [self._season(r) for r in rows]

    def get_season(self, season_id):
        row = self.seasons.get(season_id)
        return self._season(row) if row else None

    def save_season(self, row):
        now = self._now()
        old = self.seasons.get(row["id"], {})
        self.seasons[row["id"]] = {**copy.deepcopy(row), "created_at": old.get("created_at", now), "updated_at": now}

    def save_season_race(self, row):
        self.season_races[row["id"]] = copy.deepcopy(row)

    def delete_season_race(self, season_id, race_id):
        race = self.season_races.get(race_id)
        if race is None or race["season_id"] != season_id:
            return False
        del self.season_races[race_id]
        return True

    def set_plan_season(self, plan_id, season_id):
        if plan_id not in self.plans:
            return False
        if season_id is None:
            self.plan_seasons.pop(plan_id, None)
        else:
            self.plan_seasons[plan_id] = season_id
        return True

    def get_plan_seasons(self):
        return dict(self.plan_seasons)

    def get_active_plan_goal_race(self):
        plan = next((r for r in self.plans.values() if r["status"] == "active"), None)
        season = self.seasons.get(self.plan_seasons.get(plan["id"])) if plan else None
        race = self.season_races.get((season["plan_goal_races"] or {}).get(plan["id"])) if season else None
        return copy.deepcopy(race)

    def get_setting(self, key):
        return copy.deepcopy(self.settings.get(key))

    def set_setting(self, key, value):
        if value is None:
            self.settings.pop(key, None)
        else:
            self.settings[key] = copy.deepcopy(value)


PATCHED = [
    "get_training_plan", "list_training_plans", "save_uploaded_training_plan",
    "update_training_plan", "create_training_plan", "get_idempotent_result", "set_training_plan_status", "delete_training_plan",
    "list_training_plan_revisions", "last_upload_version", "get_training_plan_revision",
    "get_workout_states", "get_plans_with_completion", "upsert_workout_state", "auto_skip_workouts", "get_activity_brief",
    "get_activities_by_ids", "get_activities_in_range", "delete_activities_not_in", "get_setting", "set_setting",
    "get_calendar_settings", "save_calendar_schedule", "set_calendar_token",
    "list_seasons", "get_season", "save_season", "save_season_race", "delete_season_race",
    "set_plan_season", "get_plan_seasons", "get_active_plan_goal_race",
]


@pytest.fixture
def fake_db(monkeypatch):
    from tools import plan_service
    fake = FakePlanDB()
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(plan_service, "_checked_day", None)
    for name in PATCHED:
        monkeypatch.setattr(db, name, getattr(fake, name))
    return fake
