# tools/plan_service.py
"""Training-plan persistence: the PostgreSQL-backed layer shared by the
/training-plan web routes (tools/training_plan.py) and the plan MCP tools
(server.py).

Plan documents are validated and edited by tools/plan_doc.py; this module
stores them (db.py) and joins in the per-workout state kept beside them —
completion, the Garmin activity that completed a workout, and the Garmin
Connect workout the coach scheduled for it.

Every function raises ``PlanStorageUnavailable`` when DATABASE_URL isn't set,
``LookupError`` for an unknown plan, and ``plan_doc.PlanError`` for an invalid
upload or edit (its message is safe to show to the user).
"""
import json
import os
from datetime import date, datetime, timezone

import db
from tools import plan_doc
from tools.plan_doc import PlanError

MAX_UPLOAD_BYTES = int(os.environ.get("TRAINING_PLAN_MAX_BYTES", 20 * 1024 * 1024))

# Garmin activityType.typeKey → plan sport, for matching an activity to the
# planned workout it completes.
_SPORT_KEYWORDS = (
    ("multi_sport", "brick"), ("triathlon", "brick"), ("duathlon", "brick"),
    ("swim", "swim"),
    ("cycling", "bike"), ("biking", "bike"), ("ride", "bike"), ("bike", "bike"),
    ("running", "run"), ("run", "run"),
    ("strength", "strength"),
)


class PlanStorageUnavailable(RuntimeError):
    """Training plans need PostgreSQL (DATABASE_URL)."""


def _require_db() -> None:
    if not db.is_configured():
        raise PlanStorageUnavailable(
            "Training plans are stored in PostgreSQL — set DATABASE_URL."
        )


def sport_for_activity_type(activity_type: str | None) -> str | None:
    key = (activity_type or "").lower()
    for keyword, sport in _SPORT_KEYWORDS:
        if keyword in key:
            return sport
    return None


def _iso(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


# ── READ ──────────────────────────────────────────────────────────────────────

def get_plan(plan_id: str | None = None) -> dict | None:
    """The stored row for a plan (or the active one), or None."""
    _require_db()
    return db.get_training_plan(plan_id)


def require_plan(plan_id: str | None = None) -> dict:
    row = get_plan(plan_id)
    if row is None:
        raise LookupError("No active training plan." if plan_id is None else f"No plan with id {plan_id!r}.")
    return row


def require_editable(plan_id: str | None = None) -> dict:
    row = require_plan(plan_id)
    if row["status"] != "active":
        raise PlanError("Archived plans are read-only — activate the plan to change it.")
    return row


def list_plans() -> list[dict]:
    _require_db()
    return [{k: _iso(v) for k, v in row.items()} for row in db.list_training_plans()]


def completed_map(plan_id: str) -> dict[str, bool]:
    return {wid: True for wid, s in db.get_workout_states(plan_id).items() if s.get("completed")}


def view_payload(row: dict) -> dict:
    """What the viewer needs besides the plan itself."""
    return {
        "id": row["id"],
        "status": row["status"],
        "version": row["version"],
        "readOnly": row["status"] != "active",
        "completed": completed_map(row["id"]),
    }


# ── UPLOAD ────────────────────────────────────────────────────────────────────

def parse_upload(raw: bytes) -> tuple[dict, list[str]]:
    """Decode and validate an uploaded plan file. Returns (plan, warnings);
    raises PlanError listing every error."""
    if not raw or not raw.strip():
        raise PlanError("The file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise PlanError(f"The file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    try:
        plan = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise PlanError("The file is not UTF-8 text.") from None
    except ValueError as e:
        raise PlanError(f"The file is not valid JSON ({e}).") from None
    errors, warnings = plan_doc.validate_plan(plan)
    if errors:
        raise PlanError(" ".join(errors))
    return plan, warnings


def upload_preview(plan: dict) -> dict | None:
    """What replacing an existing plan with the same id would change, or None
    when the id is new (nothing to confirm)."""
    _require_db()
    plan_id = plan["meta"]["id"]
    row = db.get_training_plan(plan_id)
    if row is None:
        return None

    new_ids = {w.get("id") for _, _, w in plan_doc.iter_workouts(plan)}
    old_names = {w.get("id"): w.get("name") for _, _, w in plan_doc.iter_workouts(row["plan"])}
    completed = completed_map(plan_id)
    orphaned = sorted(wid for wid in completed if wid not in new_ids)
    last_upload = db.last_upload_version(plan_id) or 0
    revisions = db.list_training_plan_revisions(plan_id, limit=1)
    return {
        "id": plan_id,
        "status": row["status"],
        "version": row["version"],
        "updated_at": _iso(row["updated_at"]),
        "last_change": revisions[0]["summary"] if revisions else None,
        "last_source": revisions[0]["source"] if revisions else None,
        "edits_since_upload": max(0, row["version"] - last_upload),
        "completed_kept": len(completed) - len(orphaned),
        "completed_orphaned": [{"id": wid, "name": old_names.get(wid)} for wid in orphaned],
        "zone_overrides_reset": bool(row["plan"].get("overrides") or row["plan"].get("zonesValidated")),
    }


def save_upload(plan: dict) -> dict:
    """Store an uploaded plan as the active plan (replacing same-id content).

    The unit choice carries over from the stored copy; zone overrides and
    validation flags do not — the uploaded file's zones are authoritative.
    """
    _require_db()
    plan_id = plan["meta"]["id"]
    existing = db.get_training_plan(plan_id)
    if existing and "unit" not in plan and existing["plan"].get("unit"):
        plan["unit"] = existing["plan"]["unit"]
    plan_doc.normalize_plan(plan)
    summary = "Uploaded plan" if existing is None else "Replaced plan content (upload)"
    return db.save_uploaded_training_plan(plan_id, plan, summary)


def set_status(plan_id: str, status: str) -> None:
    _require_db()
    if not db.set_training_plan_status(plan_id, status):
        raise LookupError(f"No plan with id {plan_id!r}.")


def delete_plan(plan_id: str) -> None:
    """Permanently delete an archived plan (the active one can't be deleted)."""
    row = require_plan(plan_id)
    if row["status"] == "active":
        raise PlanError("Archive the active plan before deleting it.")
    db.delete_training_plan(plan_id)


# ── EDIT ──────────────────────────────────────────────────────────────────────

def apply_operations(plan_id: str | None, operations: list, source: str,
                     reason: str | None = None) -> dict:
    """Apply edits atomically to a plan (default: the active one).

    Archived plans are read-only. Returns the new row plus the change
    messages and the ids of the workouts touched.
    """
    row = require_editable(plan_id)
    result = {}

    def mutate(plan):
        new_plan, messages, touched = plan_doc.apply_operations(plan, operations)
        result["changes"], result["touched"] = messages, touched
        summary = "; ".join(messages) or "No changes"
        if reason:
            summary = f"{reason.strip()} — {summary}"
        return new_plan, summary

    new_row = db.update_training_plan(row["id"], mutate, source)
    return {"row": new_row, **result}


def restore_revision(plan_id: str | None, version: int, source: str) -> dict:
    """Make a past revision's content current again (as a new revision)."""
    row = require_editable(plan_id)
    revision = db.get_training_plan_revision(row["id"], version)
    if revision is None:
        raise LookupError(f"Plan {row['id']!r} has no revision {version}.")

    def mutate(_plan):
        restored = plan_doc.normalize_plan(revision["plan"])
        return restored, f"Restored revision {version}"

    return db.update_training_plan(row["id"], mutate, source)


def revisions(plan_id: str | None, limit: int = 100) -> list[dict]:
    row = require_plan(plan_id)
    return [{k: _iso(v) for k, v in r.items()} for r in db.list_training_plan_revisions(row["id"], limit)]


# ── WORKOUT STATE ─────────────────────────────────────────────────────────────

def _state_dict(state: dict | None) -> dict:
    if not state:
        return {}
    out = {
        "completed": state.get("completed", False),
        "completedAt": _iso(state.get("completed_at")),
        "activityId": state.get("activity_id"),
        "notes": state.get("notes"),
        "garminWorkoutId": state.get("garmin_workout_id"),
        "garminScheduledDate": _iso(state.get("garmin_scheduled_date")),
    }
    return {k: v for k, v in out.items() if v not in (None, False)}


def set_completion(plan_id: str | None, workout_id: str, completed: bool = True,
                   activity_id: int | None = None, notes: str | None = None) -> dict:
    row = require_editable(plan_id)
    hit = plan_doc.find_workout(row["plan"], workout_id)
    if hit is None:
        raise PlanError(f"No workout with id {workout_id!r} in this plan.")
    fields = {
        "completed": bool(completed),
        "completed_at": datetime.now(timezone.utc) if completed else None,
    }
    if activity_id is not None or not completed:
        fields["activity_id"] = activity_id if completed else None
    if notes is not None:
        fields["notes"] = notes or None
    state = db.upsert_workout_state(row["id"], workout_id, **fields)
    week, day, workout = hit
    return {
        "plan_id": row["id"],
        "workout_id": workout_id,
        "name": workout.get("name"),
        "date": day.get("date"),
        "week": week.get("weekNumber"),
        **_state_dict(state),
        "completed": bool(completed),
    }


def match_activity(plan: dict, states: dict, day: date, sport: str | None) -> list[tuple]:
    """Planned workouts on a date that an activity of ``sport`` could complete,
    uncompleted ones first."""
    hits = []
    for week, d, w in plan_doc.iter_workouts(plan):
        if str(d.get("date"))[:10] != day.isoformat() or w.get("sport") == "rest":
            continue
        if sport and w.get("sport") != sport and not (sport == "brick" or w.get("sport") == "brick"):
            continue
        hits.append((week, d, w))
    hits.sort(key=lambda h: bool((states.get(h[2]["id"]) or {}).get("completed")))
    return hits


def complete_from_activity(plan_id: str | None, activity_id: int | None = None,
                           workout_id: str | None = None, activity_date: str | None = None,
                           sport: str | None = None, notes: str | None = None,
                           completed: bool = True) -> dict:
    """Mark a planned workout complete, finding it from a Garmin activity when
    no workout id is given (same date, matching sport)."""
    row = require_editable(plan_id)
    if workout_id:
        return set_completion(row["id"], workout_id, completed, activity_id, notes)

    if activity_id is not None and (activity_date is None or sport is None):
        brief = db.get_activity_brief(activity_id)
        if brief is None and activity_date is None:
            raise PlanError(
                f"Activity {activity_id} isn't synced yet — pass activity_date and sport, or workout_id."
            )
        if brief is not None:
            activity_date = activity_date or str(_iso(brief["activity_date"]))[:10]
            sport = sport or sport_for_activity_type(brief.get("activity_type"))
    if activity_date is None:
        raise PlanError("Give workout_id, or activity_id / activity_date to find the planned workout.")

    day = plan_doc.parse_date(activity_date)
    states = db.get_workout_states(row["id"])
    hits = match_activity(row["plan"], states, day, sport)
    if not hits:
        nearby = [
            {"id": w["id"], "date": d.get("date"), "sport": w.get("sport"), "name": w.get("name")}
            for _, d, w in plan_doc.iter_workouts(row["plan"])
            if abs((plan_doc.parse_date(d.get("date")) - day).days) <= 2 and w.get("sport") != "rest"
        ]
        raise PlanError(
            f"No planned {sport or ''} workout on {day.isoformat()}. "
            f"Nearby workouts: {json.dumps(nearby)} — pass workout_id to pick one."
        )
    done = lambda h: bool((states.get(h[2]["id"]) or {}).get("completed"))
    candidates = [h for h in hits if done(h) != completed] or hits
    if len(candidates) > 1:
        choices = [{"id": w["id"], "sport": w.get("sport"), "name": w.get("name")} for _, _, w in candidates]
        raise PlanError(f"Several planned workouts match on {day.isoformat()}: {json.dumps(choices)} — pass workout_id.")
    target = candidates[0][2]["id"]
    return set_completion(row["id"], target, completed, activity_id, notes)


def link_garmin_workout(plan_id: str | None, workout_id: str, garmin_workout_id: int | None,
                        scheduled_date: str | None = None) -> dict:
    """Record (or clear, with garmin_workout_id=None) the Garmin Connect
    workout scheduled for a planned workout."""
    row = require_editable(plan_id)
    hit = plan_doc.find_workout(row["plan"], workout_id)
    if hit is None:
        raise PlanError(f"No workout with id {workout_id!r} in this plan.")
    if scheduled_date:
        plan_doc.parse_date(scheduled_date)
    state = db.upsert_workout_state(
        row["id"], workout_id,
        garmin_workout_id=garmin_workout_id,
        garmin_scheduled_date=(scheduled_date or hit[1].get("date")) if garmin_workout_id else None,
    )
    return {"plan_id": row["id"], "workout_id": workout_id, **_state_dict(state)}


# ── MCP READ VIEW ─────────────────────────────────────────────────────────────

def _workout_view(workout: dict, day: dict, state: dict, include_details: bool) -> dict:
    keys = ["id", "sport", "type", "name", "durationMinutes", "distanceKm", "distanceMeters",
            "distanceKmRange", "primaryZone", "keyTargets", "terrain", "difficulty", "trainingEffect"]
    if include_details:
        keys += ["description", "humanReadable"]
    out = {k: workout[k] for k in keys if workout.get(k) not in (None, "")}
    out["date"] = day.get("date")
    out.update(_state_dict(state))
    out["completed"] = bool(state and state.get("completed"))
    return out


def plan_overview(plan_id: str | None = None, week_number: int | None = None,
                  start_date: str | None = None, end_date: str | None = None,
                  include_details: bool = True, today: date | None = None) -> dict:
    """A token-friendly read of a plan for the coach.

    Always: meta, status/version, current thresholds, phases and a one-line
    summary per week. Workouts: the requested week, or the weeks overlapping
    start_date..end_date, or by default the current and next week.
    """
    row = require_plan(plan_id)
    plan = row["plan"]
    states = db.get_workout_states(row["id"])
    today = today or date.today()

    weeks = plan.get("weeks") or []
    if week_number is not None:
        selected = [w for w in weeks if w.get("weekNumber") == week_number]
        if not selected:
            raise PlanError(f"No week {week_number} in this plan.")
    elif start_date or end_date:
        lo = plan_doc.parse_date(start_date) if start_date else date.min
        hi = plan_doc.parse_date(end_date) if end_date else date.max
        selected = [w for w in weeks
                    if plan_doc.parse_date(w["startDate"]) <= hi and plan_doc.parse_date(w["endDate"]) >= lo]
    else:
        current = plan_doc.current_week_number(plan, today)
        selected = [w for w in weeks if current is not None and w.get("weekNumber") in (current, current + 1)]

    def week_line(w):
        ids = [x.get("id") for d in w.get("days") or [] for x in d.get("workouts") or [] if x.get("sport") != "rest"]
        return {
            "weekNumber": w.get("weekNumber"), "startDate": w.get("startDate"), "endDate": w.get("endDate"),
            "phase": w.get("phase"), "isRecoveryWeek": w.get("isRecoveryWeek", False),
            "targetHours": w.get("targetHours"), "plannedHours": (w.get("summary") or {}).get("totalHours"),
            "workouts": len(ids), "completed": sum(1 for i in ids if (states.get(i) or {}).get("completed")),
        }

    detail = []
    for w in selected:
        detail.append({
            **week_line(w),
            "focus": w.get("focus"),
            "summary": w.get("summary"),
            "days": [
                {"date": d.get("date"), "dayOfWeek": d.get("dayOfWeek"),
                 "workouts": [_workout_view(x, d, states.get(x.get("id")), include_details)
                              for x in d.get("workouts") or []]}
                for d in w.get("days") or []
            ],
        })

    return {
        "id": row["id"],
        "status": row["status"],
        "version": row["version"],
        "updated_at": _iso(row["updated_at"]),
        "meta": plan.get("meta"),
        "currentWeek": plan_doc.current_week_number(plan, today),
        "thresholds": plan_doc.effective_thresholds(plan),
        "unit": plan.get("unit"),
        "phases": [{k: p.get(k) for k in ("name", "startWeek", "endWeek", "focus")} for p in plan.get("phases") or []],
        "weekSummaries": [week_line(w) for w in weeks],
        "weeks": detail,
    }
