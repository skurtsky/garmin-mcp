# tools/season_service.py
"""Season persistence: the PostgreSQL-backed layer shared by the Seasons pages
and the Settings goal-race dropdown (tools/training_plan.py) and, later, the
season MCP tools (server.py).

tools/season_doc.py validates; this module stores (db.py) and shapes the
payload everything else reads. Every function returns the season it changed
as a payload (see ``season_payload``), whose ``warnings`` are the soft
problems — Friel's limits, plans or races outside the season's dates.

Raises ``PlanStorageUnavailable`` without DATABASE_URL, ``LookupError`` for an
unknown season, race or plan, and ``SeasonError`` for invalid input (its
message is safe to show to the user).
"""
import db
from tools import season_doc
from tools.plan_service import PlanStorageUnavailable
from tools.season_doc import SeasonError


def _require_db() -> None:
    if not db.is_configured():
        raise PlanStorageUnavailable("Seasons are stored in PostgreSQL — set DATABASE_URL.")


# ── SHAPES ────────────────────────────────────────────────────────────────────

def race_payload(row: dict) -> dict:
    return {"id": row["id"], "seasonId": row["season_id"], "name": row["name"], "date": str(row["race_date"])[:10],
            "priority": row["priority"], "type": row["race_type"], "goal": row["goal"],
            "location": row["location"], "url": row["url"], "notes": row["notes"]}


def _plan_index() -> dict[str, dict]:
    """{plan_id: {id, event, start, end, status}} for the date checks."""
    return {p["id"]: {"id": p["id"], "event": p.get("event") or p["id"], "start": p.get("start_date"),
                      "end": p.get("end_date"), "status": p["status"]} for p in db.list_training_plans()}


def season_payload(row: dict, plans: dict[str, dict] | None = None) -> dict:
    """A stored season as the API shape: camelCase, with ``raceIds`` /
    ``planIds``, the races and plans themselves, each plan's goal race and the
    soft ``warnings``."""
    plans = _plan_index() if plans is None else plans
    races = [race_payload(r) for r in row["races"]]
    linked = [plans[pid] for pid in row["plan_ids"] if pid in plans]
    linked.sort(key=lambda p: (p["start"] or "", p["id"]))
    season = {
        "id": row["id"], "name": row["name"], "startDate": str(row["start_date"])[:10],
        "endDate": str(row["end_date"])[:10], "status": row["status"],
        "goals": row["goals"] or [], "objectives": row["objectives"] or [],
        "raceIds": [r["id"] for r in races], "planIds": [p["id"] for p in linked],
    }
    race_ids = set(season["raceIds"])
    season.update(
        races=races, plans=linked,
        planGoalRaces={pid: rid for pid, rid in (row["plan_goal_races"] or {}).items()
                       if pid in season["planIds"] and rid in race_ids},
    )
    season["warnings"] = season_doc.season_warnings(season, races, linked)
    return season


def _row(season: dict) -> dict:
    """The db row for a payload's own fields (races and plan links are stored apart)."""
    return {"id": season["id"], "name": season["name"], "start_date": season["startDate"],
            "end_date": season["endDate"], "status": season["status"], "goals": season["goals"],
            "objectives": season["objectives"], "plan_goal_races": season["planGoalRaces"]}


def _load(season_id: str) -> dict:
    row = db.get_season(season_id)
    if row is None:
        raise LookupError(f"No season with id {season_id!r}.")
    return row


def _save(season: dict) -> dict:
    db.save_season(_row(season))
    return season_payload(_load(season["id"]))


# ── READ ──────────────────────────────────────────────────────────────────────

def list_seasons() -> list[dict]:
    _require_db()
    plans = _plan_index()
    return [season_payload(r, plans) for r in db.list_seasons()]


def get_season(season_id: str) -> dict:
    _require_db()
    return season_payload(_load(season_id))


def season_for_plan(plan_id: str) -> dict | None:
    """The season a plan is linked to, or None."""
    _require_db()
    season_id = db.get_plan_seasons().get(plan_id)
    return season_payload(_load(season_id)) if season_id else None


# ── SEASONS ───────────────────────────────────────────────────────────────────

def create_season(data: dict) -> dict:
    """A new season from {name, startDate, endDate[, status, goals, objectives]}."""
    _require_db()
    fields = season_doc.season_fields(data)
    season = {"id": season_doc.new_id("s"), **fields, "planGoalRaces": {},
              "goals": season_doc.goals_list(data.get("goals"), set()),
              "objectives": season_doc.objectives_list(data.get("objectives"), set())}
    return _save(season)


def update_season(season_id: str, data: dict) -> dict:
    """Change a season's name, dates or status (archive / restore)."""
    _require_db()
    season = get_season(season_id)
    season.update(season_doc.season_fields(data, season))
    return _save(season)


def set_goals(season_id: str, goals) -> dict:
    """Replace the season's goals. Friel's limit of three is a warning only."""
    _require_db()
    season = get_season(season_id)
    season["goals"] = season_doc.goals_list(goals, set(season["raceIds"]))
    return _save(season)


def set_objectives(season_id: str, objectives) -> dict:
    """Replace the season's objectives (each with a hand-set status)."""
    _require_db()
    season = get_season(season_id)
    season["objectives"] = season_doc.objectives_list(objectives, set(season["raceIds"]))
    return _save(season)


# ── RACES ─────────────────────────────────────────────────────────────────────

def _race_row(season_id: str, race_id: str, fields: dict) -> dict:
    return {"id": race_id, "season_id": season_id, "name": fields["name"], "race_date": fields["date"],
            "priority": fields["priority"], "race_type": fields["type"], "goal": fields["goal"],
            "location": fields["location"], "url": fields["url"], "notes": fields["notes"]}


def add_race(season_id: str, data: dict) -> dict:
    _require_db()
    _load(season_id)
    fields = season_doc.race_fields(data)
    db.save_season_race(_race_row(season_id, season_doc.new_id("r"), fields))
    return season_payload(_load(season_id))


def update_race(season_id: str, race_id: str, data: dict) -> dict:
    _require_db()
    season = get_season(season_id)
    current = next((r for r in season["races"] if r["id"] == race_id), None)
    if current is None:
        raise LookupError(f"Season {season_id!r} has no race {race_id!r}.")
    db.save_season_race(_race_row(season_id, race_id, season_doc.race_fields(data, current)))
    return season_payload(_load(season_id))


def delete_race(season_id: str, race_id: str) -> dict:
    """Remove a race, and every goal, objective and goal-race choice that pointed at it."""
    _require_db()
    season = get_season(season_id)
    if race_id not in season["raceIds"]:
        raise LookupError(f"Season {season_id!r} has no race {race_id!r}.")
    db.delete_season_race(season_id, race_id)
    for key in ("goals", "objectives"):
        season[key] = [{k: v for k, v in item.items() if not (k == "raceId" and v == race_id)} for item in season[key]]
    season["planGoalRaces"] = {p: r for p, r in season["planGoalRaces"].items() if r != race_id}
    return _save(season)


# ── PLANS ─────────────────────────────────────────────────────────────────────

def link_plan(season_id: str, plan_id: str) -> dict:
    """Put a plan in a season; a plan is in at most one, so it leaves any other.
    A plan outside the season's dates is linked anyway, with a warning."""
    _require_db()
    season = get_season(season_id)
    if season["status"] == "archived":
        raise SeasonError("This season is archived — restore it before linking plans.")
    previous = db.get_plan_seasons().get(plan_id)
    if not db.set_plan_season(plan_id, season_id):
        raise LookupError(f"No plan with id {plan_id!r}.")
    if previous and previous != season_id:
        _forget_goal_race(previous, plan_id)
    return get_season(season_id)


def unlink_plan(plan_id: str) -> dict | None:
    """Take a plan out of its season. Returns that season, or None when it had none."""
    _require_db()
    previous = db.get_plan_seasons().get(plan_id)
    if not db.set_plan_season(plan_id, None):
        raise LookupError(f"No plan with id {plan_id!r}.")
    if not previous:
        return None
    _forget_goal_race(previous, plan_id)
    return get_season(previous)


def _forget_goal_race(season_id: str, plan_id: str) -> None:
    season = get_season(season_id)
    if plan_id in season["planGoalRaces"]:
        season["planGoalRaces"] = {p: r for p, r in season["planGoalRaces"].items() if p != plan_id}
        _save(season)


def set_plan_goal_race(plan_id: str, race_id: str | None) -> dict:
    """Choose which race of its season is this plan's goal race (None clears it)."""
    _require_db()
    season_id = db.get_plan_seasons().get(plan_id)
    if not season_id:
        raise SeasonError("Link this plan to a season first.")
    season = get_season(season_id)
    if race_id and race_id not in season["raceIds"]:
        raise SeasonError(f"Race {race_id!r} isn't in the season “{season['name']}”.")
    goal_races = {p: r for p, r in season["planGoalRaces"].items() if p != plan_id}
    if race_id:
        goal_races[plan_id] = race_id
    season["planGoalRaces"] = goal_races
    return _save(season)


# ── GOAL RACE ─────────────────────────────────────────────────────────────────

def active_goal_race() -> dict | None:
    """The app-wide goal race (tools/goal_race.py shape) when the active plan's
    season names one for it and its type has a goal-race distance; else None."""
    if not db.is_configured():
        return None
    row = db.get_active_plan_goal_race()
    return season_doc.derived_goal_race(race_payload(row)) if row else None


def goal_race_choice(plan_id: str) -> dict:
    """What the Settings goal-race dropdown needs for one plan. Linked: its
    season, that season's races and the one chosen. Not linked: ``season`` is
    None and ``seasons`` lists the active seasons it could join."""
    _require_db()
    season = season_for_plan(plan_id)
    if season is None:
        return {"season": None,
                "seasons": [{"id": s["id"], "name": s["name"]} for s in list_seasons() if s["status"] == "active"]}
    return {"season": {"id": season["id"], "name": season["name"], "startDate": season["startDate"],
                       "endDate": season["endDate"]},
            "races": sorted(season["races"], key=lambda r: r["date"]),
            "selectedRaceId": season["planGoalRaces"].get(plan_id)}
