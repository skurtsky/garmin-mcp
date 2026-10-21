# tools/plan_validation.py
"""Strict validation of a training-plan document, or of one week of it (no I/O).

``create_training_plan`` runs :func:`validate_document` before anything is
written; ``amend_training_plan``'s ``replace_week`` runs :func:`validate_week`
on the incoming week against the plan around it. Both report issues as
``{"path", "message"}`` with a JSON path into the document
(``weeks[3].startDate``). Errors are fatal, warnings are not.

The older :func:`tools.plan_doc.validate_plan` stays as the lenient check the
web upload uses; this one is for plans that are written, not just stored.
"""
import re
from datetime import date, timedelta

from tools import plan_doc, workout_steps
from tools.plan_doc import PlanError

SCHEMA_MAJOR = 1
# A week's hand-set targets may differ from what its workouts add up to by
# this fraction before it is worth a warning.
DRIFT = 0.10
WEEK_STATUSES = ("skeleton", "planned")

_DOW = plan_doc._DOW
_TOKEN_ANY_RE = re.compile(r"\{\{.*?\}\}|\{\{")
_LOOSE_TOKEN_RE = re.compile(r"\{\{\s*([\w-]+)\s*:\s*([\w-]+)\s*\}\}")
_VERSION_RE = re.compile(r"^(\d+)(?:\.\d+)*$")


class Report:
    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append({"path": path, "message": message})

    def warn(self, path: str, message: str) -> None:
        self.warnings.append({"path": path, "message": message})


def format_issues(errors: list[dict], warnings: list[dict] | None = None, limit: int = 40) -> str:
    lines = [f"{i['path']}: {i['message']}" for i in errors]
    lines += [f"(warning) {i['path']}: {i['message']}" for i in warnings or []]
    extra = len(lines) - limit
    return "\n".join(lines[:limit] + ([f"…and {extra} more"] if extra > 0 else []))


# ── SMALL CHECKS ──────────────────────────────────────────────────────────────

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _date(report: Report, path: str, value, required=True) -> date | None:
    if value in (None, ""):
        if required:
            report.error(path, "Missing — expected a YYYY-MM-DD date.")
        return None
    try:
        return _strict_date(value)
    except ValueError:
        report.error(path, f"{value!r} isn't a YYYY-MM-DD date.")
        return None


def _strict_date(value) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(value)
    return date.fromisoformat(value)


def _ranges(numbers: list[int]) -> str:
    out, start, prev = [], None, None
    for n in sorted(numbers):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev))
            start = prev = n
    if start is not None:
        out.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in out)


def _strings(value, path: str):
    """(path, text) for every string inside a JSON value."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")


# ── TOKENS ────────────────────────────────────────────────────────────────────

def _codes(table, extra=()) -> tuple:
    return tuple(r[0] for r in table) + tuple(extra)


# kind → (valid codes, threshold key it needs, what to set to get it)
_TOKEN_KINDS = {
    "run-pace": (_codes(plan_doc.RUN_PACE_OFFSET_TABLE, ("strides",)), "thresholdPace",
                 "zones.run.pace.thresholdPace"),
    "run-hr": (_codes(plan_doc.HR_PCT_TABLE), "runLthr", "zones.run.hr.lthr"),
    "bike-watts": (_codes(plan_doc.BIKE_POWER_PCT_TABLE, ("ss",)), "ftp", "zones.bike.power.ftp"),
    "bike-hr": (_codes(plan_doc.HR_PCT_TABLE), "bikeLthr", "zones.bike.hr.lthr"),
    "swim-pace": (_codes(plan_doc.SWIM_PACE_OFFSET_TABLE), "css", "zones.swim.css"),
}


def _threshold_ok(key: str, value) -> bool:
    if key in ("thresholdPace", "css"):
        return plan_doc.single_pace_seconds(value) is not None
    return _is_number(value) and value > 0


def token_problem(token: str, thresholds: dict) -> str | None:
    """Why a ``{{…}}`` token wouldn't render on the watch/viewer, or None."""
    ftp = plan_doc._FTP_TOKEN_RE.fullmatch(token)
    if ftp is None and re.match(r"\{\{\s*bike-ftp\b", token, re.IGNORECASE):
        return f"malformed token — expected {plan_doc.FTP_TOKEN_SYNTAX}"
    if ftp:
        try:
            plan_doc._parse_ftp_token(ftp)
        except ValueError as e:
            return f"{e}"
        if not _threshold_ok("ftp", thresholds.get("ftp")):
            return "needs an FTP — set zones.bike.power.ftp"
        return None
    m = plan_doc._TOKEN_RE.fullmatch(token)
    if m is None:
        loose = _LOOSE_TOKEN_RE.fullmatch(token)
        if loose and loose.group(1).lower() in _TOKEN_KINDS:
            kind = loose.group(1).lower()
            return f"{kind} has no zone {loose.group(2).lower()!r} (use {', '.join(_TOKEN_KINDS[kind][0])})"
        return ("unrecognised token — use run-pace, run-hr, bike-watts, bike-hr, swim-pace "
                "(e.g. {{run-pace:5a}}) or bike-ftp")
    kind, code = m.group(1).lower(), m.group(2).lower()
    codes, key, where = _TOKEN_KINDS[kind]
    if code not in codes:
        return f"{kind} has no zone {code!r} (use {', '.join(codes)})"
    if not _threshold_ok(key, thresholds.get(key)):
        return f"needs {where}, which this plan doesn't set"
    return None


def _check_tokens(report: Report, path: str, workout: dict, thresholds: dict) -> None:
    for text_path, text in _strings(workout, path):
        for m in _TOKEN_ANY_RE.finditer(text):
            token = m.group(0)
            problem = ("unterminated token (missing }})" if token == "{{"
                       else token_problem(token, thresholds))
            if problem:
                report.error(text_path, f"{token.strip()} — {problem}; it would render blank or broken.")


# ── WEEK ──────────────────────────────────────────────────────────────────────

def phase_map(plan: dict) -> dict[int, str]:
    """{week number: phase name} from the phases block (first phase wins)."""
    out = {}
    for ph in plan.get("phases") or []:
        if not isinstance(ph, dict) or not _is_int(ph.get("startWeek")) or not _is_int(ph.get("endWeek")):
            continue
        for n in range(ph["startWeek"], min(ph["endWeek"], ph["startWeek"] + 1000) + 1):
            out.setdefault(n, ph.get("name"))
    return out


def _check_workout(report, plan, w, path, seen_ids, thresholds):
    if not isinstance(w, dict):
        report.error(path, "A workout must be an object.")
        return
    wid = w.get("id")
    if not wid or not isinstance(wid, str):
        report.error(f"{path}.id", "Every workout needs a string id.")
    elif wid in seen_ids:
        report.error(f"{path}.id", f"Duplicate workout id {wid!r} (also at {seen_ids[wid]}) — "
                                   "completion ticks would collide.")
    else:
        seen_ids[wid] = path
    sport = w.get("sport")
    if sport not in plan_doc.SPORTS:
        report.error(f"{path}.sport", f"{sport!r} isn't a sport — use one of {', '.join(plan_doc.SPORTS)}.")
    if not str(w.get("name") or "").strip():
        report.warn(f"{path}.name", "Workout has no name.")
    for key in ("durationMinutes", "distanceKm", "distanceMeters"):
        if w.get(key) is not None and (not _is_number(w[key]) or w[key] < 0):
            report.error(f"{path}.{key}", f"{w[key]!r} must be a non-negative number.")
    for field, budget in plan_doc.FIELD_BUDGETS.items():
        text = w.get(field)
        if isinstance(text, str) and len(text) > budget:
            report.warn(f"{path}.{field}", f"{len(text)} characters (budget {budget}).")
    if w.get("steps") not in (None, []):
        try:
            workout_steps.clean_steps(sport, w["steps"])
        except PlanError as e:
            report.error(f"{path}.steps", str(e))
    if w.get("startTime") not in (None, ""):
        try:
            plan_doc.parse_clock(w["startTime"])
        except PlanError:
            report.warn(f"{path}.startTime", f"{w['startTime']!r} isn't HH:MM — the calendar feed "
                                             "uses the weekly schedule instead.")
    _check_tokens(report, path, w, thresholds)


def _hours_km(summary: dict, sport: str) -> tuple[float, float]:
    row = (summary.get("bySport") or {}).get(sport) or {}
    return row.get("hours") or 0, row.get("km") or 0


def _check_targets(report, week, path):
    """Compare the week's hand-set numbers with what its workouts add up to."""
    computed = plan_doc.week_summary(week)
    planned_h = computed["totalHours"]
    target = week.get("targetHours")
    if _is_number(target) and target > 0 and abs(planned_h - target) / target > DRIFT:
        report.warn(f"{path}.targetHours", f"targetHours {target} but the workouts add up to {planned_h} h "
                                           f"({(planned_h - target) / target:+.0%}).")
    target_km = week.get("targetRunKm")
    if _is_number(target_km) and target_km > 0:
        _, planned_km = _hours_km(computed, "run")
        if abs(planned_km - target_km) / target_km > DRIFT:
            unmeasured = sum(1 for w in plan_doc.week_workouts(week)
                             if w.get("sport") == "run" and plan_doc._workout_km(w) == 0)
            note = f" ({unmeasured} run workout{'s' if unmeasured != 1 else ''} with no distance)" if unmeasured else ""
            report.warn(f"{path}.targetRunKm", f"targetRunKm {target_km} but the planned runs add up to "
                                               f"{planned_km} km{note}.")
    given = week.get("summary")
    if isinstance(given, dict):
        stale = []
        if _is_number(given.get("totalHours")) and abs(given["totalHours"] - planned_h) > 0.05:
            stale.append(f"totalHours {given['totalHours']} (computed {planned_h})")
        for sport, row in (given.get("bySport") or {}).items():
            if not isinstance(row, dict):
                continue
            hours, km = _hours_km(computed, sport)
            if (_is_number(row.get("hours")) and abs(row["hours"] - hours) > 0.05) or \
                    (_is_number(row.get("km")) and abs(row["km"] - km) > 0.5) or \
                    (not row.get("km") and km):
                stale.append(f"{sport} {row.get('hours')} h / {row.get('km')} km (computed {hours} h / {km} km)")
        if stale:
            report.warn(f"{path}.summary", "Hand-written summary differs from the workouts and is replaced "
                                          "by the computed one: " + "; ".join(stale) + ".")


def check_week(report: Report, plan: dict, week, index: int, seen_ids: dict, thresholds: dict,
               phases: dict) -> None:
    path = f"weeks[{index}]"
    if not isinstance(week, dict):
        report.error(path, "A week must be an object.")
        return
    number = week.get("weekNumber")
    if not _is_int(number):
        report.error(f"{path}.weekNumber", "Must be a whole number.")
    elif number != index + 1:
        report.error(f"{path}.weekNumber", f"Is {number}; weeks are numbered 1..N in order, so this one is "
                                           f"{index + 1}.")

    start = _date(report, f"{path}.startDate", week.get("startDate"))
    end = _date(report, f"{path}.endDate", week.get("endDate"))
    if start and start.weekday() != 0:
        report.error(f"{path}.startDate", f"{start} is a {_DOW[start.weekday()]}; weeks start on a Monday.")
    if end and end.weekday() != 6:
        report.error(f"{path}.endDate", f"{end} is a {_DOW[end.weekday()]}; weeks end on a Sunday.")
    if start and end and start.weekday() == 0 and end.weekday() == 6 and (end - start).days != 6:
        report.error(f"{path}.endDate", f"{end} isn't the Sunday of the week starting {start} "
                                        f"({start + timedelta(days=6)}).")

    status = week.get("status")
    if status is not None and status not in WEEK_STATUSES:
        report.error(f"{path}.status", f"{status!r} isn't a week status — use {' or '.join(WEEK_STATUSES)}.")
    if "isRecoveryWeek" in week and not isinstance(week["isRecoveryWeek"], bool):
        report.error(f"{path}.isRecoveryWeek", "Must be true or false.")
    for key in ("targetHours", "targetRunKm"):
        if key in week and (not _is_number(week[key]) or week[key] < 0):
            report.error(f"{path}.{key}", f"{week[key]!r} must be a non-negative number.")
    expected = phases.get(number) if _is_int(number) else None
    if week.get("phase") is not None and expected and week["phase"] != expected:
        report.error(f"{path}.phase", f"{week['phase']!r}, but the phases block puts week {number} "
                                      f"in {expected!r}.")

    days = week.get("days")
    if days is None:
        days = []
    elif not isinstance(days, list):
        report.error(f"{path}.days", "Must be a list.")
        days = []
    workouts = plan_doc.week_workouts({"days": days})
    skeleton = plan_doc.is_skeleton_week(plan, week)
    if not workouts and not skeleton:
        report.error(f"{path}.days", "The week has no workouts. If it is meant to be filled in later, "
                                     'flag it with "status": "skeleton" (or "skeleton": true on the plan).')
    elif workouts and status == "skeleton":
        report.warn(f"{path}.status", f"Flagged skeleton but has {len(workouts)} workouts — "
                                      'set "status": "planned".')

    dates_seen = set()
    for j, day in enumerate(days):
        dpath = f"{path}.days[{j}]"
        if not isinstance(day, dict):
            report.error(dpath, "A day must be an object.")
            continue
        when = _date(report, f"{dpath}.date", day.get("date"))
        if when:
            if start and end and not start <= when <= end:
                report.error(f"{dpath}.date", f"{when} is outside the week ({start} – {end}).")
            if when in dates_seen:
                report.warn(f"{dpath}.date", f"{when} appears twice in the week.")
            dates_seen.add(when)
            name = day.get("dayOfWeek")
            if name is not None and str(name).strip().lower() not in (
                    _DOW[when.weekday()].lower(), _DOW[when.weekday()][:3].lower()):
                report.error(f"{dpath}.dayOfWeek", f"{name!r}, but {when} is a {_DOW[when.weekday()]}.")
        wlist = day.get("workouts")
        if wlist is not None and not isinstance(wlist, list):
            report.error(f"{dpath}.workouts", "Must be a list.")
            continue
        for k, w in enumerate(wlist or []):
            _check_workout(report, plan, w, f"{dpath}.workouts[{k}]", seen_ids, thresholds)

    if workouts:
        _check_targets(report, week, path)


def validate_week(plan: dict, week: dict, index: int) -> tuple[list[dict], list[dict]]:
    """(errors, warnings) for ``week`` as the plan's ``weeks[index]``. Only the
    week itself is judged, but its dates must fit between its neighbours and
    its workout ids must not collide with the rest of the plan."""
    report = Report()
    weeks = plan.get("weeks") or []
    seen_ids = {w["id"]: f"weeks[{i}]" for i, wk in enumerate(weeks) if i != index and isinstance(wk, dict)
                for w in plan_doc.week_workouts(wk) if isinstance(w.get("id"), str)}
    check_week(report, plan, week, index, seen_ids, plan_doc.effective_thresholds(plan), phase_map(plan))
    start, end = _safe_date(week.get("startDate")), _safe_date(week.get("endDate"))
    for neighbour, prev in ((index - 1, True), (index + 1, False)):
        if 0 <= neighbour < len(weeks) and isinstance(weeks[neighbour], dict):
            other = _safe_date(weeks[neighbour].get("endDate" if prev else "startDate"))
            mine = start if prev else end
            if other and mine and (mine - other).days != (1 if prev else -1):
                report.error(f"weeks[{index}].{'startDate' if prev else 'endDate'}",
                             f"Doesn't line up with week {neighbour + 1} "
                             f"({'ends' if prev else 'starts'} {other}).")
    return report.errors, report.warnings


def _safe_date(value) -> date | None:
    try:
        return _strict_date(value)
    except ValueError:
        return None


# ── DOCUMENT ──────────────────────────────────────────────────────────────────

def _check_meta(report, plan):
    meta = plan.get("meta")
    if not isinstance(meta, dict):
        report.error("meta", "Missing 'meta' object.")
        return {}
    plan_id = meta.get("id")
    if not plan_id:
        report.error("meta.id", "Missing — every plan needs an id.")
    elif not isinstance(plan_id, str) or not plan_doc.PLAN_ID_RE.match(plan_id):
        report.error("meta.id", f"{plan_id!r} must be 1–120 letters, digits, '.', '_' or '-'.")
    if not str(meta.get("event") or "").strip():
        report.error("meta.event", "Missing — name the event or block.")
    return meta


def _check_phases(report, plan, weeks):
    phases = plan.get("phases")
    if phases in (None, []):
        report.warn("phases", "No phases — the phase selector and badges will be empty.")
        return
    if not isinstance(phases, list):
        report.error("phases", "Must be a list of phases.")
        return
    covered: dict[int, int] = {}
    total = len(weeks)
    for i, ph in enumerate(phases):
        path = f"phases[{i}]"
        if not isinstance(ph, dict):
            report.error(path, "A phase must be an object.")
            continue
        if not str(ph.get("name") or "").strip():
            report.error(f"{path}.name", "Missing.")
        first, last = ph.get("startWeek"), ph.get("endWeek")
        if not _is_int(first):
            report.error(f"{path}.startWeek", "Must be a whole week number.")
        if not _is_int(last):
            report.error(f"{path}.endWeek", "Must be a whole week number.")
        if not (_is_int(first) and _is_int(last)):
            continue
        if first > last:
            report.error(f"{path}.endWeek", f"Ends (week {last}) before it starts (week {first}).")
            continue
        if first < 1:
            report.error(f"{path}.startWeek", f"Week {first} doesn't exist; weeks start at 1.")
        if last > total:
            report.error(f"{path}.endWeek", f"Week {last} doesn't exist; the plan has {total} weeks.")
        clash = sorted({covered[n] for n in range(max(first, 1), min(last, total) + 1) if n in covered})
        if clash:
            report.error(f"{path}.startWeek", f"Overlaps phases[{clash[0]}] — a week belongs to one phase.")
        for n in range(max(first, 1), min(last, total) + 1):
            covered.setdefault(n, i)
        for key, edge, field in (("startDate", first, "startDate"), ("endDate", last, "endDate")):
            if ph.get(key) in (None, ""):
                continue
            given = _date(report, f"{path}.{key}", ph[key])
            if given and 1 <= edge <= total and isinstance(weeks[edge - 1], dict):
                actual = _safe_date(weeks[edge - 1].get(field))
                if actual and given != actual:
                    report.error(f"{path}.{key}", f"{given}, but week {edge}'s {field} is {actual}.")
    missing = [n for n in range(1, total + 1) if n not in covered]
    if missing:
        report.error("phases", f"Weeks {_ranges(missing)} aren't in any phase.")


def _check_zones(report, plan, start):
    zones = plan.get("zones")
    if zones is None:
        report.warn("zones", "No zones — zone tokens in workouts can't resolve.")
        return
    if not isinstance(zones, dict):
        report.error("zones", "Must be an object.")
        return
    provisional, anchor = zones.get("provisional"), zones.get("reanchorBy")
    if provisional is not None and not isinstance(provisional, bool):
        report.error("zones.provisional", "Must be true or false.")
        provisional = None
    when = _date(report, "zones.reanchorBy", anchor, required=False)
    if provisional and anchor in (None, ""):
        report.warn("zones.reanchorBy", "Zones are provisional but say nothing about when to re-anchor them.")
    elif anchor not in (None, "") and provisional is not True:
        report.warn("zones.provisional", "reanchorBy is set but the zones aren't flagged provisional.")
    if provisional and when and start and when > start:
        report.warn("zones.reanchorBy", f"{when} is after the plan starts ({start}) — week 1 would run on "
                                        "unanchored zones.")


def validate_document(plan) -> tuple[list[dict], list[dict]]:
    """(errors, warnings) for a whole plan. Nothing here needs the database."""
    report = Report()
    if not isinstance(plan, dict):
        report.error("", "The plan must be a JSON object.")
        return report.errors, report.warnings

    version = plan.get("version")
    match = _VERSION_RE.match(str(version)) if version is not None else None
    if version is None:
        report.error("version", f'Missing schema version — add "version": "{SCHEMA_MAJOR}.0".')
    elif not match:
        report.error("version", f"{version!r} isn't a schema version like \"{SCHEMA_MAJOR}.0\".")
    elif int(match.group(1)) != SCHEMA_MAJOR:
        report.error("version", f"Schema version {version} isn't supported; this server reads "
                                f"{SCHEMA_MAJOR}.x plans.")
    if "skeleton" in plan and not isinstance(plan["skeleton"], bool):
        report.error("skeleton", "Must be true or false.")

    meta = _check_meta(report, plan)
    weeks = plan.get("weeks")
    if not isinstance(weeks, list) or not weeks:
        report.error("weeks", "Missing — a plan needs a non-empty 'weeks' list.")
        weeks = []

    thresholds = plan_doc.effective_thresholds(plan)
    phases = phase_map(plan)
    seen_ids: dict = {}
    for i, week in enumerate(weeks):
        check_week(report, plan, week, i, seen_ids, thresholds, phases)

    # Weeks run back to back.
    for i in range(1, len(weeks)):
        a, b = weeks[i - 1], weeks[i]
        if not (isinstance(a, dict) and isinstance(b, dict)):
            continue
        prev_end, start = _safe_date(a.get("endDate")), _safe_date(b.get("startDate"))
        if prev_end and start:
            gap = (start - prev_end).days - 1
            if gap > 0:
                report.error(f"weeks[{i}].startDate", f"Gap of {gap} day{'s' if gap != 1 else ''} after "
                                                      f"week {i} (ends {prev_end}).")
            elif gap < 0:
                report.error(f"weeks[{i}].startDate", f"Overlaps week {i} (ends {prev_end}) by {-gap} "
                                                      f"day{'s' if gap != -1 else ''}.")

    # The header agrees with the weeks.
    first = _safe_date(weeks[0].get("startDate")) if weeks and isinstance(weeks[0], dict) else None
    last = _safe_date(weeks[-1].get("endDate")) if weeks and isinstance(weeks[-1], dict) else None
    plan_start = _date(report, "meta.planStartDate", meta.get("planStartDate")) if meta else None
    plan_end = _date(report, "meta.planEndDate", meta.get("planEndDate")) if meta else None
    if weeks and plan_start and first and plan_start != first:
        report.error("meta.planStartDate", f"{plan_start}, but week 1 starts {first}.")
    if weeks and plan_end and last and plan_end != last:
        report.error("meta.planEndDate", f"{plan_end}, but the last week ends {last}.")
    if meta:
        total = meta.get("totalWeeks")
        if not _is_int(total):
            report.error("meta.totalWeeks", "Missing or not a whole number.")
        elif weeks and total != len(weeks):
            report.error("meta.totalWeeks", f"{total}, but weeks has {len(weeks)} entries.")
        low, high = first or plan_start, last or plan_end
        race = ((plan.get("raceStrategy") or {}).get("event") or {}) if isinstance(plan.get("raceStrategy"), dict) else {}
        for path, raw in (("meta.eventDate", meta.get("eventDate")),
                          ("raceStrategy.event.date", race.get("date") if isinstance(race, dict) else None)):
            when = _date(report, path, raw, required=False)
            if when and low and high and not low <= when <= high:
                report.error(path, f"{when} falls outside the plan ({low} – {high}).")

    _check_phases(report, plan, weeks)
    _check_zones(report, plan, first or plan_start)

    empty = [i for i, w in enumerate(weeks) if isinstance(w, dict) and not plan_doc.week_workouts(w)]
    if plan.get("skeleton") is True and weeks and not empty:
        report.warn("skeleton", "Flagged skeleton but every week has workouts — drop the flag.")
    return report.errors, report.warnings
