# tools/plan_tools.py
"""MCP-facing training-plan operations (registered in server.py).

Thin wrappers over tools/plan_service.py that turn its exceptions into a
ToolError with a readable message — FastMCP reports that back to the
assistant as a tool error it can act on (e.g. "pass workout_id").
"""
from functools import wraps

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
                      include_details=True) -> dict:
    return plan_service.plan_overview(plan_id, week_number, start_date, end_date, include_details)


@_tool_errors
def list_training_plans() -> list:
    return plan_service.list_plans()


@_tool_errors
def amend_training_plan(operations: list, reason: str, plan_id=None) -> dict:
    if not (reason or "").strip():
        raise PlanError("Give a short reason — it's shown in the plan's history.")
    result = plan_service.apply_operations(plan_id, operations, "mcp", reason)
    row = result["row"]
    touched_weeks = sorted({
        w.get("weekNumber")
        for w in row["plan"].get("weeks") or []
        for d in w.get("days") or []
        for x in d.get("workouts") or []
        if x.get("id") in result["touched"]
    })
    return {
        "plan_id": row["id"],
        "version": row["version"],
        "changes": result["changes"],
        "workout_ids": result["touched"],
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
def link_plan_workout_to_garmin(workout_id: str, garmin_workout_id=None, scheduled_date=None,
                                plan_id=None) -> dict:
    return plan_service.link_garmin_workout(plan_id, workout_id, garmin_workout_id, scheduled_date)


@_tool_errors
def get_training_plan_revisions(plan_id=None, limit: int = 20) -> list:
    return plan_service.revisions(plan_id, max(1, min(int(limit), 200)))


@_tool_errors
def restore_training_plan_revision(version: int, plan_id=None) -> dict:
    row = plan_service.restore_revision(plan_id, int(version), "mcp")
    return {"plan_id": row["id"], "version": row["version"], "restored_from": int(version)}
