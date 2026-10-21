# tools/plan_tools.py
"""MCP-facing training-plan operations (registered in server.py).

Thin wrappers over tools/plan_service.py that turn its exceptions into a
ToolError with a readable message — FastMCP reports that back to the
assistant as a tool error it can act on (e.g. "pass workout_id").
"""
from functools import wraps

from tools.local_time import local_today

from fastmcp.exceptions import ToolError

from tools import plan_service
from tools.plan_doc import PlanError
from tools.plan_service import PlanStorageUnavailable


def _tool_errors(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (PlanError, PlanStorageUnavailable) as e:
            raise ToolError(str(e)) from None
        except LookupError as e:
            raise ToolError(str(e).strip("'\"")) from None
    return wrapper


@_tool_errors
def get_training_plan(plan_id=None, week_number=None, start_date=None, end_date=None,
                      include_details=True, sections=None, include_weeks=True) -> dict:
    return plan_service.plan_overview(plan_id, week_number, start_date, end_date, include_details,
                                      sections=sections, include_weeks=include_weeks)


@_tool_errors
def list_training_plans() -> list:
    return plan_service.list_plans()


@_tool_errors
def amend_training_plan(operations: list, reason: str, plan_id=None) -> dict:
    if not (reason or "").strip():
        raise PlanError("Give a short reason — it's shown in the plan's history.")
    result = plan_service.apply_operations(plan_id, operations, "mcp", reason)
    row = result["row"]
    touched_weeks = {
        w.get("weekNumber")
        for w in row["plan"].get("weeks") or []
        for d in w.get("days") or []
        for x in d.get("workouts") or []
        if x.get("id") in result["touched"]
    } | {op.get("week_number") for op in operations
         if isinstance(op, dict) and op.get("op") in ("replace_week", "update_week")}
    return {
        "plan_id": row["id"],
        "version": row["version"],
        "changes": result["changes"],
        "workout_ids": result["touched"],
        "warnings": result["warnings"],
        "weeks": [
            {"weekNumber": w["weekNumber"], "targetHours": w.get("targetHours"), "summary": w.get("summary")}
            for w in row["plan"].get("weeks") or [] if w.get("weekNumber") in touched_weeks
        ],
    }


@_tool_errors
def complete_plan_workout(workout_id=None, activity_id=None, activity_date=None, sport=None,
                          notes=None, completed=True, plan_id=None) -> dict:
    return plan_service.complete_from_activity(
        plan_id, activity_id=activity_id, workout_id=workout_id, activity_date=activity_date,
        sport=sport, notes=notes, completed=completed,
    )


@_tool_errors
def skip_plan_workout(workout_id: str, skipped=True, reason=None, plan_id=None) -> dict:
    if not workout_id:
        raise PlanError("Give the workout_id (from get_training_plan).")
    return plan_service.set_skip(plan_id, workout_id, skipped, reason)


@_tool_errors
def link_plan_workout_to_garmin(workout_id: str, garmin_workout_id=None, scheduled_date=None,
                                plan_id=None) -> dict:
    return plan_service.link_garmin_workout(plan_id, workout_id, garmin_workout_id, scheduled_date)


# The coach can queue plans and tidy them away, but which plan is active —
# what Today, the calendar feed and auto-matching follow — is the athlete's
# call, made in the web app (or by a future plan's start date arriving).
_ACTIVE_IS_THE_ATHLETES = ("Only the athlete changes which plan is active — ask them to do it at "
                           "/training-plan/plans (a future plan also takes over by itself on its start date).")


@_tool_errors
def set_training_plan_status(plan_id: str, status: str) -> dict:
    if not plan_id:
        raise PlanError("Give the plan_id (from list_training_plans).")
    if status not in ("future", "archived"):
        raise PlanError(f"status must be 'future' or 'archived'. {_ACTIVE_IS_THE_ATHLETES}"
                        if status == "active" else "status must be 'future' or 'archived'.")
    row = plan_service.require_plan(plan_id)
    previous = row["status"]
    if previous == "active":
        raise PlanError(f"{plan_id!r} is the active plan. {_ACTIVE_IS_THE_ATHLETES}")
    if previous != status:
        plan_service.set_status(plan_id, status)
    meta = row["plan"].get("meta") or {}
    start = meta.get("planStartDate")
    out = {"plan_id": plan_id, "event": meta.get("event"), "status": status,
           "previous_status": previous, "start_date": start, "end_date": meta.get("planEndDate")}
    if status == "future":
        if start and str(start)[:10] > local_today().isoformat():
            out["becomes_active_on"] = str(start)[:10]
        else:
            out["note"] = ("Its start date has passed, so it won't take over by itself — "
                           "the athlete can make it active at /training-plan/plans.")
        out["timeline"] = plan_service.timeline_warnings(plan_id)
    return out


@_tool_errors
def get_training_plan_revisions(plan_id=None, limit: int = 20) -> list:
    return plan_service.revisions(plan_id, max(1, min(int(limit), 200)))


@_tool_errors
def restore_training_plan_revision(version: int, plan_id=None) -> dict:
    row = plan_service.restore_revision(plan_id, int(version), "mcp")
    return {"plan_id": row["id"], "version": row["version"], "restored_from": int(version)}


@_tool_errors
def create_training_plan(plan=None, status="future", validate_only=False, on_conflict="reject",
                         idempotency_key=None, plan_url=None) -> dict:
    return plan_service.create_plan(plan, plan_url, status, validate_only, on_conflict, idempotency_key)


@_tool_errors
def delete_training_plan(plan_id: str, confirm: str) -> dict:
    if not plan_id:
        raise PlanError("Give the plan_id (from list_training_plans).")
    row = plan_service.require_plan(plan_id)
    if confirm != plan_id:
        raise PlanError(f"Deleting {plan_id!r} is permanent — its revisions and completion ticks go too. "
                        "Pass confirm set to the same plan_id, or archive it instead "
                        "(set_training_plan_status).")
    plan_service.delete_plan(plan_id)
    return {"deleted": plan_id, "was": row["status"]}
