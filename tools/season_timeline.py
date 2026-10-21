# tools/season_timeline.py
"""The season timeline: every plan of a season laid on one calendar, a column
per week (Monday to Sunday). Pure calculation — no database, no HTML — so the
rules are testable on their own; tools/season_view.py draws it.

A column is one of:

    planned    the plan week has workouts: hours = their planned minutes
    target     the plan week has no workouts yet (a skeleton week) but a
               targetHours: hours = that target
    empty      the plan week has neither: an empty slot, no hours
    unplanned  no plan covers the week (a gap between plans, the week after an
               A race, or time before / after the plans inside the season)

``hours`` is None for empty and unplanned columns. The season totals add up
``hours`` of every column that has one, so planned and target weeks count the
same way, and ``includesTargets`` says when any of them is a target.

Races: the season's own races, plus races found inside plans (race workouts,
the plan's event) that no season race covers on the same date — those show as
small C markers marked ``fromPlan``.
"""
from datetime import date, timedelta

KINDS = ("planned", "target", "empty", "unplanned")


def _day(value) -> date:
    return date.fromisoformat(str(value)[:10])


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def fmt_hours(hours: float) -> str:
    return f"{hours:.1f} h"


def week_text(col: dict) -> str:
    """The hours line of a week's tooltip: planned, target-only, empty or unplanned."""
    kind = col["kind"]
    if kind == "planned":
        return (f"Planned {fmt_hours(col['hours'])}, "
                f"{col['doneWorkouts']} of {col['workouts']} workouts done")
    if kind == "target":
        return f"Target {fmt_hours(col['hours'])} (no workouts yet)"
    if kind == "empty":
        return "No workouts or target hours yet"
    return "Unplanned week" + (f" (after {col['afterRace']})" if col.get("afterRace") else "")


def _one_based(i: int | None) -> int | None:
    return None if i is None else i + 1


def _column(index: int, start: date) -> dict:
    return {"index": index, "start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat(),
            "kind": "unplanned", "planId": None, "planEvent": None, "weekNumber": None, "phase": "",
            "recovery": False, "hours": None, "plannedHours": None, "targetHours": None,
            "workouts": 0, "doneWorkouts": 0, "overlap": False}


def _fill(col: dict, plan: dict, week: dict) -> None:
    """Make ``col`` the plan week ``week``: planned, target or empty, by the rule above."""
    planned_h = week["plannedMin"] / 60
    target = week.get("targetHours")
    if week.get("workouts", 0) > 0:
        kind, hours = "planned", planned_h
    elif target is not None:
        kind, hours = "target", float(target)
    else:
        kind, hours = "empty", None
    col.update(kind=kind, hours=hours, planId=plan["id"], planEvent=plan["event"], planStatus=plan["status"],
               weekNumber=week["weekNumber"], phase=week["phase"], recovery=week["recovery"],
               plannedHours=planned_h if kind == "planned" else None, targetHours=target,
               workouts=week.get("workouts", 0), doneWorkouts=week.get("doneWorkouts", 0))


def build(season: dict, plans: list[dict], today: date) -> dict:
    """The timeline of ``season`` (a season payload: dates and races) over its
    ``plans`` (plan_service.timeline_plans) as of ``today``."""
    season_start, season_end = _day(season["startDate"]), _day(season["endDate"])
    weeks = [(p, w) for p in plans for w in p["weeks"]]
    first = min([season_start] + [_day(w["start"]) for _, w in weeks])
    last = max([season_end] + [_day(w["end"]) for _, w in weeks])
    origin = monday(first)
    count = (monday(last) - origin).days // 7 + 1
    columns = [_column(i + 1, origin + timedelta(days=7 * i)) for i in range(count)]

    def col_of(d: date) -> int | None:
        i = (monday(d) - origin).days // 7
        return i if 0 <= i < count else None

    for plan, week in weeks:
        col = columns[col_of(_day(week["start"]))]
        if col["planId"] is not None:   # two plans claim this week: the earlier one keeps it
            col["overlap"] = True
            continue
        _fill(col, plan, week)

    races = _races(season, plans, col_of)
    for col in columns:
        if col["kind"] == "unplanned" and col["index"] > 1:
            prev = columns[col["index"] - 2]
            hit = next((r for r in races if r["priority"] == "A" and r["col"] == prev["index"]), None)
            if hit:
                col["afterRace"] = hit["name"]
        col["isToday"] = col["start"] <= today.isoformat() <= col["end"]
        col["text"] = week_text(col)

    return {
        "start": columns[0]["start"], "end": columns[-1]["end"], "weeks": columns,
        "bands": _bands(columns), "plans": _plan_spans(plans, columns, col_of),
        "races": races, "totals": _totals(columns), "nextARace": _next_a_race(races, today),
        "today": {"date": today.isoformat(), "col": _one_based(col_of(today))},
    }


def _bands(columns: list[dict]) -> list[dict]:
    """Runs of weeks in the same plan and phase (or the same unplanned gap), for the phase band."""
    bands: list[dict] = []
    for c in columns:
        key = (c["planId"], c["phase"] if c["planId"] else None)
        if bands and bands[-1]["key"] == key:
            bands[-1]["to"] = c["index"]
        else:
            bands.append({"key": key, "kind": "phase" if c["planId"] else "unplanned", "planId": c["planId"],
                          "name": c["phase"] if c["planId"] else "Unplanned", "from": c["index"], "to": c["index"]})
    for b in bands:
        del b["key"]
    return bands


def _plan_spans(plans: list[dict], columns: list[dict], col_of) -> list[dict]:
    spans = []
    for p in plans:
        mine = [c["index"] for c in columns if c["planId"] == p["id"]]
        if mine:
            spans.append({"planId": p["id"], "event": p["event"], "status": p["status"],
                          "from": min(mine), "to": max(mine), "start": p["start"], "end": p["end"]})
    return spans


def _races(season: dict, plans: list[dict], col_of) -> list[dict]:
    """Season races first, then plan races on dates no season race covers (as C, ``fromPlan``)."""
    out = []
    for r in season.get("races") or []:
        i = col_of(_day(r["date"]))
        out.append({"id": r["id"], "name": r["name"], "date": r["date"], "priority": r["priority"],
                    "type": r["type"], "goal": r.get("goal", ""), "location": r.get("location", ""),
                    "col": _one_based(i), "fromPlan": False})
    taken = {r["date"] for r in out}
    seen = set()
    for p in plans:
        for r in p["races"]:
            key = (r["date"], r["name"])
            if r["date"] in taken or key in seen:
                continue
            seen.add(key)
            i = col_of(_day(r["date"]))
            out.append({"id": None, "name": r["name"], "date": r["date"], "priority": "C", "type": "other",
                        "goal": "", "location": "", "col": _one_based(i), "fromPlan": True,
                        "planId": p["id"]})
    return sorted(out, key=lambda r: (r["date"], r["name"]))


def _totals(columns: list[dict]) -> dict:
    """Hours across the season, planned and target weeks counted alike."""
    with_hours = [c for c in columns if c["hours"] is not None]
    total = sum(c["hours"] for c in with_hours)
    return {
        "hours": round(total, 2),
        "averagePerWeek": round(total / len(with_hours), 2) if with_hours else None,
        "weeksWithHours": len(with_hours),
        "plannedWeeks": sum(1 for c in columns if c["kind"] == "planned"),
        "targetWeeks": sum(1 for c in columns if c["kind"] == "target"),
        "emptyWeeks": sum(1 for c in columns if c["kind"] == "empty"),
        "unplannedWeeks": sum(1 for c in columns if c["kind"] == "unplanned"),
        "includesTargets": any(c["kind"] == "target" for c in columns),
    }


def _next_a_race(races: list[dict], today: date) -> dict | None:
    """The soonest A race of the season that hasn't passed, with whole weeks and days to it."""
    ahead = [r for r in races if r["priority"] == "A" and not r["fromPlan"] and _day(r["date"]) >= today]
    if not ahead:
        return None
    race = min(ahead, key=lambda r: r["date"])
    days = (_day(race["date"]) - today).days
    return {"id": race["id"], "name": race["name"], "date": race["date"], "days": days, "weeks": days // 7}
