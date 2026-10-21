# tools/season_tools.py
"""MCP-facing season operations (registered in server.py).

Thin wrappers over tools/season_service.py, like tools/plan_tools.py: they
turn its exceptions into a ToolError with a readable message the assistant can
act on, take snake_case arguments (``start_date``, ``race_id``), and answer in
the season's own camelCase shape (tools/season_doc.py). Every write returns the
whole season it changed, with its soft ``warnings`` (Friel's limits, plans or
races outside the season's dates) — warnings never block a write.
"""
import logging
from functools import wraps

from fastmcp.exceptions import ToolError

from tools import season_service
from tools.plan_doc import PlanError
from tools.plan_service import PlanStorageUnavailable
from tools.season_doc import SeasonError

logger = logging.getLogger(__name__)


def _tool_errors(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (SeasonError, PlanError, PlanStorageUnavailable) as e:
            raise ToolError(str(e)) from None
        except LookupError as e:
            raise ToolError(str(e).strip("'\"")) from None
    return wrapper


def _given(**fields) -> dict:
    """Only the fields the caller passed: an update leaves the others alone."""
    return {k: v for k, v in fields.items() if v is not None}


def _items(items):
    """Goals / objectives from the assistant: text, or {text, target_date, race_id[, status, id]}."""
    if not isinstance(items, list):
        return items   # the service reports "must be a list"
    renamed = {"target_date": "targetDate", "race_id": "raceId"}
    return [{renamed.get(k, k): v for k, v in item.items()} if isinstance(item, dict) else item for item in items]


def _resolve(season_id):
    if season_id:
        return season_id
    found = season_service.default_season_id()
    if found is None:
        raise SeasonError("No season yet — create one with create_season.")
    return found


def _summary(season: dict) -> dict:
    """A season in one line for list_seasons."""
    return {
        "id": season["id"], "name": season["name"], "startDate": season["startDate"],
        "endDate": season["endDate"], "status": season["status"], "planIds": season["planIds"],
        "raceCount": len(season["races"]), "goalCount": len(season["goals"]),
        "objectiveCount": len(season["objectives"]), "warnings": season["warnings"],
    }


@_tool_errors
def list_seasons(include_archived: bool = True) -> list:
    return [_summary(s) for s in season_service.list_seasons() if include_archived or s["status"] == "active"]


@_tool_errors
def get_season(season_id=None, include_weeks: bool = False) -> dict:
    data = season_service.timeline(_resolve(season_id))
    tl = data["timeline"]
    out = {**data["season"], "totals": tl["totals"], "nextARace": tl["nextARace"],
           "timeline": {"start": tl["start"], "end": tl["end"], "weekCount": len(tl["weeks"]),
                        "today": tl["today"]}}
    if include_weeks:
        out["timeline"]["weeks"] = [
            {"week": c["index"], "start": c["start"], "kind": c["kind"],
             "hours": None if c["hours"] is None else round(c["hours"], 2), "plan": c["planEvent"],
             "planId": c["planId"], "planWeek": c["weekNumber"], "phase": c["phase"] or None,
             "recovery": c["recovery"], "workouts": c["workouts"], "doneWorkouts": c["doneWorkouts"]}
            for c in tl["weeks"]]
    return out


@_tool_errors
def create_season(name, start_date, end_date, goals=None, objectives=None) -> dict:
    return season_service.create_season({"name": name, "startDate": start_date, "endDate": end_date,
                                         "goals": _items(goals), "objectives": _items(objectives)})


@_tool_errors
def update_season(season_id, name=None, start_date=None, end_date=None, status=None) -> dict:
    return season_service.update_season(season_id, _given(name=name, startDate=start_date,
                                                          endDate=end_date, status=status))


@_tool_errors
def link_plan_to_season(plan_id, season_id=None, goal_race_id=None) -> dict:
    if not season_id:
        if goal_race_id:
            raise SeasonError("goal_race_id needs a season_id.")
        previous = season_service.unlink_plan(plan_id)
        return {"plan_id": plan_id, "season": previous, "linked": False,
                "message": f"Unlinked from “{previous['name']}”." if previous else "The plan was in no season."}
    season = season_service.link_plan(season_id, plan_id)
    if goal_race_id is not None:
        season = season_service.set_plan_goal_race(plan_id, goal_race_id or None)
    return {"plan_id": plan_id, "season": season, "linked": True,
            "goalRaceId": season["planGoalRaces"].get(plan_id)}


@_tool_errors
def add_race(season_id, name, date, priority, type="other", goal=None, location=None, url=None, notes=None) -> dict:
    return season_service.add_race(season_id, {"name": name, "date": date, "priority": priority, "type": type,
                                               **_given(goal=goal, location=location, url=url, notes=notes)})


@_tool_errors
def update_race(season_id, race_id, name=None, date=None, priority=None, type=None, goal=None,
                location=None, url=None, notes=None) -> dict:
    return season_service.update_race(season_id, race_id, _given(
        name=name, date=date, priority=priority, type=type, goal=goal, location=location, url=url, notes=notes))


@_tool_errors
def delete_race(season_id, race_id) -> dict:
    race = next((r for r in season_service.get_season(season_id)["races"] if r["id"] == race_id), None)
    season = season_service.delete_race(season_id, race_id)
    return {"deleted": race, "season": season}


@_tool_errors
def set_season_goals(season_id, goals) -> dict:
    return season_service.set_goals(season_id, _items(goals))


@_tool_errors
def set_season_objectives(season_id, objectives) -> dict:
    return season_service.set_objectives(season_id, _items(objectives))


def season_summary(plan_id: str) -> dict | None:
    """The short season block ``get_training_plan`` adds for a linked plan, or
    None. Seasons are optional there: a failure leaves the block out."""
    try:
        return season_service.plan_summary(plan_id)
    except Exception:  # noqa: BLE001
        logger.exception("Season summary unavailable")
        return None
