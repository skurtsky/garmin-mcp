# tools/plan_service.py
"""Training-plan persistence: the PostgreSQL-backed layer shared by the
/training-plan web routes (tools/training_plan.py) and the plan MCP tools
(server.py).

Plan documents are validated and edited by tools/plan_doc.py; this module
stores them (db.py) and joins in the per-workout state kept beside them —
completion (or a skip), the Garmin activity that completed a workout, and the
Garmin Connect workout the coach scheduled for it.

Every function raises ``PlanStorageUnavailable`` when DATABASE_URL isn't set,
``LookupError`` for an unknown plan, and ``plan_doc.PlanError`` for an invalid
upload or edit (its message is safe to show to the user).
"""
import copy
import hashlib
import ipaddress
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import date, datetime, timedelta, timezone
from tools.local_time import local_today

import db
from tools import plan_doc, plan_validation
from tools.plan_doc import PlanError

logger = logging.getLogger(__name__)

STATUSES = ("active", "future", "archived")

# app_settings keys: the last day future plans were checked for a start date
# that has arrived, and the pending threshold carry-over offer.
AUTOSWITCH_KEY = "plan_autoswitch_checked"
CARRYOVER_KEY = "plan_threshold_carryover"

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
    return active_plan() if plan_id is None else db.get_training_plan(plan_id)


def active_plan() -> dict | None:
    """The active plan's row, after switching to a future plan whose start
    date has arrived. Everything that reads "the active plan" goes through
    here, so the switch happens on whichever read comes first that day."""
    advance_plans()
    return db.get_training_plan(None)


def require_plan(plan_id: str | None = None) -> dict:
    row = get_plan(plan_id)
    if row is None:
        raise LookupError("No active training plan." if plan_id is None else f"No plan with id {plan_id!r}.")
    return row


def require_editable(plan_id: str | None = None) -> dict:
    """The active plan or a future one — archived plans are read-only."""
    row = require_plan(plan_id)
    if row["status"] == "archived":
        raise PlanError("Archived plans are read-only — make the plan active or future to change it.")
    return row


def list_plans() -> list[dict]:
    _require_db()
    return [{k: _iso(v) for k, v in row.items()} for row in db.list_training_plans()]


def completed_map(plan_id: str) -> dict[str, bool]:
    return {wid: True for wid, s in db.get_workout_states(plan_id).items() if s.get("completed")}


def workout_states(row: dict, today: date | None = None) -> dict[str, dict]:
    """{workout_id: state} for a plan, after skipping the active plan's
    workouts whose day has passed without them being done."""
    states = db.get_workout_states(row["id"])
    if skip_missed(row, states, today):
        states = db.get_workout_states(row["id"])
    return states


def skip_missed(row: dict, states: dict, today: date | None = None) -> list[str]:
    """Mark the active plan's workouts skipped once their day has gone by
    without being completed — unless a skip was already decided (un-skipping
    by hand sticks). A late sync still completes them. Returns the ids
    skipped. Never raises: a failure here must not break the read."""
    if row.get("status") != "active":
        return []
    today_iso = (today or local_today()).isoformat()
    due = [
        w["id"] for _, d, w in plan_doc.iter_workouts(row["plan"])
        if w.get("id") and w.get("sport") != "rest" and str(d.get("date"))[:10] < today_iso
        and not (states.get(w["id"]) or {}).get("completed")
        and (states.get(w["id"]) or {}).get("skipped") is None
    ]
    if not due:
        return []
    try:
        return db.auto_skip_workouts(row["id"], due)
    except Exception:
        logger.exception("Skipping missed workouts failed")
        return []


def skipped_map(states: dict) -> dict[str, str]:
    """{workout_id: "auto" | "manual"} for the skipped workouts."""
    return {wid: "auto" if s.get("skipped_auto") else "manual"
            for wid, s in states.items() if s.get("skipped") and not s.get("completed")}


def view_payload(row: dict) -> dict:
    """What the viewer needs besides the plan itself: which workouts are
    complete, the Garmin activity each one was matched to, and each
    workout's start time as the calendar feed places it (the viewer orders
    a day's workouts by it)."""
    from concurrent.futures import ThreadPoolExecutor
    from tools import plan_calendar   # it imports this module

    def completions():
        states = workout_states(row)
        return states, db.get_activities_by_ids([s["activity_id"] for s in states.values() if s.get("activity_id")])

    with ThreadPoolExecutor(max_workers=3) as pool:      # independent round trips
        start_times = pool.submit(plan_calendar.start_times, row["plan"])
        neighbors = pool.submit(adjacent_plans, row)
        carryover = pool.submit(pending_carryover, row)
        (states, briefs) = completions()
        start_times, neighbors, carryover = start_times.result(), neighbors.result(), carryover.result()
    skipped = skipped_map(states)
    activities = {}
    for wid, s in states.items():
        act = briefs.get(s.get("activity_id"))
        if s.get("completed") and act:
            activities[wid] = {
                "id": act["garmin_id"], "name": act.get("name"),
                "durationMin": act.get("duration_min"), "distanceKm": act.get("distance_km"),
            }
    return {
        "id": row["id"],
        "status": row["status"],
        "version": row["version"],
        "readOnly": row["status"] == "archived",
        "completed": {wid: True for wid, s in states.items() if s.get("completed")},
        "skipped": skipped,
        "skipReasons": {wid: states[wid]["skip_reason"] for wid in skipped if states[wid].get("skip_reason")},
        "activities": activities,
        "startTimes": start_times,
        "neighbors": neighbors,
        "carryover": carryover,
    }


def adjacent_plans(row: dict) -> dict:
    """The plans just before and after this one in time, among the active and
    future plans — so paging past the end of the active plan in the viewer
    carries on into the next one. Each has its date range and a light copy
    of its workouts (for the month view). Archived plans have none."""
    if row["status"] == "archived":
        return {}
    timeline = sorted(
        (p for p in db.list_training_plans() if p["status"] in ("active", "future")),
        key=lambda p: (p.get("start_date") or "", p["id"]),
    )
    ids = [p["id"] for p in timeline]
    if row["id"] not in ids:
        return {}
    i = ids.index(row["id"])
    out = {}
    for key, j in (("prev", i - 1), ("next", i + 1)):
        if 0 <= j < len(timeline):
            neighbor = db.get_training_plan(timeline[j]["id"])
            if neighbor is not None:
                out[key] = _neighbor_view(neighbor)
    return out


def _neighbor_view(row: dict) -> dict:
    plan = row["plan"]
    meta = plan.get("meta") or {}
    weeks = [w for w in plan.get("weeks") or [] if w.get("startDate") and w.get("endDate")]
    days = {}
    for _, d, w in plan_doc.iter_workouts(plan):
        if w.get("sport") == "rest":
            continue
        days.setdefault(str(d.get("date"))[:10], []).append(
            {k: w.get(k) for k in ("sport", "name", "durationMinutes") if w.get(k) is not None}
        )
    return {
        "id": row["id"],
        "status": row["status"],
        "event": meta.get("event") or row["id"],
        "startDate": str(weeks[0]["startDate"])[:10] if weeks else meta.get("planStartDate"),
        "endDate": str(weeks[-1]["endDate"])[:10] if weeks else meta.get("planEndDate"),
        "firstWeek": weeks[0].get("weekNumber") if weeks else 1,
        "lastWeek": weeks[-1].get("weekNumber") if weeks else meta.get("totalWeeks"),
        "days": days,
    }


# ── AUTOMATIC SWITCH ──────────────────────────────────────────────────────────
# A future plan becomes active when its start date arrives. Only a start date
# that arrives after the last daily check counts, so a plan uploaded as future
# with a start date already past (or moved back to future by hand) stays put.

_checked_day: date | None = None   # this process already checked on this day


def advance_plans(today: date | None = None) -> dict | None:
    """Activate the future plan whose start date arrived since the last check
    (the latest, when several did — the earlier ones are archived). Returns
    the plan listing row it activated, or None. Never raises: a failure here
    must not break the read that triggered it."""
    global _checked_day
    if not db.is_configured():
        return None
    today = today or local_today()
    if _checked_day == today:
        return None
    _checked_day = today                    # at most one attempt per day per process
    try:
        last = db.get_setting(AUTOSWITCH_KEY)
        due = [] if last is None else sorted(   # the first run only notes the day
            (p for p in db.list_training_plans()
             if p["status"] == "future" and last < str(p.get("start_date") or "")[:10] <= today.isoformat()),
            key=lambda p: str(p["start_date"])[:10],
        )
        for skipped in due[:-1]:
            db.set_training_plan_status(skipped["id"], "archived")
        if due:
            activate(due[-1]["id"], auto=True, on=today)
            logger.info("Plan %s started today — made it the active plan", due[-1]["id"])
        # Recorded only once the switch is done, so a failure is retried later.
        if last is None or last < today.isoformat():
            db.set_setting(AUTOSWITCH_KEY, today.isoformat())
        return due[-1] if due else None
    except Exception:
        logger.exception("Switching to a future plan that starts today failed")
        return None


def activate(plan_id: str, auto: bool = False, on: date | None = None) -> None:
    """Make a plan active (archiving the current one), and offer to carry the
    outgoing plan's thresholds over when they differ."""
    previous = db.get_training_plan(None)
    if not db.set_training_plan_status(plan_id, "active"):
        raise LookupError(f"No plan with id {plan_id!r}.")
    _offer_carryover(previous, plan_id, auto, on)


# ── THRESHOLD CARRY-OVER ──────────────────────────────────────────────────────
# A field test during one block updates its FTP / LTHR / pace / CSS; the next
# block's file may still have the older numbers. When a plan becomes active,
# the viewer offers the outgoing plan's values where the two differ.

# (effective_thresholds key, label, sport, set_zones key)
_CARRY = (
    ("ftp", "FTP", "bike", "ftp"),
    ("bikeLthr", "Cycling LTHR", "bike", "bikeLthr"),
    ("runLthr", "Run LTHR", "run", "runLthr"),
    ("thresholdPace", "Threshold pace", "run", "thresholdPace"),
    ("css", "CSS", "swim", "cssLabel"),
)


def carryover_items(new_plan: dict, old_plan: dict) -> list[dict]:
    """The thresholds the outgoing plan has that differ from the new one's.
    ``suggested`` marks the ones the athlete set (or validated by a test) in
    the outgoing plan, rather than numbers that came with its file."""
    old, new = plan_doc.effective_thresholds(old_plan), plan_doc.effective_thresholds(new_plan)
    overrides = old_plan.get("overrides") or {}
    validated = old_plan.get("zonesValidated") or {}
    items = []
    for key, label, sport, op_key in _CARRY:
        was, now = old.get(key), new.get(key)
        if was is None or str(was).strip() == str(now).strip():
            continue
        items.append({
            "key": key, "label": label, "sport": sport, "from": was, "to": now,
            "suggested": op_key in overrides or bool((validated.get(sport) or {}).get("validated")),
        })
    return items


def _offer_carryover(previous: dict | None, plan_id: str, auto: bool, on: date | None) -> None:
    if previous is not None and previous["id"] == plan_id:
        return                              # it was already the active plan
    offer = None
    if previous is not None:
        new = db.get_training_plan(plan_id)
        if auto or carryover_items(new["plan"], previous["plan"]):
            offer = {"plan_id": plan_id, "from_id": previous["id"], "auto": auto,
                     "on": on.isoformat() if on else None}
    db.set_setting(CARRYOVER_KEY, offer)


def pending_carryover(row: dict) -> dict | None:
    """The carry-over offer for the active plan ``row``, or None."""
    if row["status"] != "active":
        return None
    offer = db.get_setting(CARRYOVER_KEY)
    if not offer or offer.get("plan_id") != row["id"]:
        return None
    old = db.get_training_plan(offer["from_id"])
    if old is None:
        return None
    items = carryover_items(row["plan"], old["plan"])
    if not items and not offer.get("auto"):
        return None
    return {
        "from": {"id": old["id"], "event": (old["plan"].get("meta") or {}).get("event") or old["id"]},
        "auto": bool(offer.get("auto")), "on": offer.get("on"), "items": items,
    }


def apply_carryover(keys: list[str], source: str) -> dict:
    """Copy the chosen thresholds (and, for a sport whose values come over,
    its validated flag) from the outgoing plan into the active one, as one
    revision. Clears the offer. Returns the updated row."""
    row = require_plan(None)
    pending = pending_carryover(row)
    if pending is None:
        raise PlanError("There's nothing to carry over.")
    chosen = [i for i in pending["items"] if i["key"] in set(keys or [])]
    if not chosen:
        raise PlanError("Pick at least one threshold to carry over.")
    old = db.get_training_plan(pending["from"]["id"])["plan"]
    op_keys = {key: op_key for key, _, _, op_key in _CARRY}
    operations = [{"op": "set_zones", **{op_keys[i["key"]]: i["from"] for i in chosen}}]
    for sport in sorted({i["sport"] for i in chosen}):
        flag = (old.get("zonesValidated") or {}).get(sport) or {}
        if flag.get("validated"):
            operations.append({"op": "set_zone_validation", "sport": sport, "validated": True, "at": flag.get("at")})
    result = apply_operations(row["id"], operations, source, f"Carried thresholds from {pending['from']['event']}")
    db.set_setting(CARRYOVER_KEY, None)
    return result["row"]


def dismiss_carryover() -> None:
    _require_db()
    db.set_setting(CARRYOVER_KEY, None)


# ── TIMELINE ──────────────────────────────────────────────────────────────────

def timeline_notes(plans: list[dict] | None = None) -> list[dict]:
    """Overlaps and gaps between consecutive active / future plans (by start
    date). A plan starting the day after the previous one ends is neither."""
    rows = sorted(
        (p for p in (plans if plans is not None else db.list_training_plans())
         if p["status"] in ("active", "future") and p.get("start_date") and p.get("end_date")),
        key=lambda p: (str(p["start_date"])[:10], p["id"]),
    )
    notes = []
    for a, b in zip(rows, rows[1:]):
        a_end = plan_doc.parse_date(str(a["end_date"])[:10])
        b_start = plan_doc.parse_date(str(b["start_date"])[:10])
        b_end = plan_doc.parse_date(str(b["end_date"])[:10])
        if b_start <= a_end:
            end = min(a_end, b_end)
            notes.append({"kind": "overlap", "first": a, "second": b, "days": (end - b_start).days + 1,
                          "start": b_start.isoformat(), "end": end.isoformat()})
        elif (b_start - a_end).days > 1:
            notes.append({"kind": "gap", "first": a, "second": b, "days": (b_start - a_end).days - 1,
                          "start": (a_end + timedelta(days=1)).isoformat(),
                          "end": (b_start - timedelta(days=1)).isoformat()})
    return notes


def note_text(note: dict) -> str:
    a, b = note["first"], note["second"]
    name = lambda p: p.get("event") or p["id"]
    days = f"{note['days']} day{'s' if note['days'] != 1 else ''}"
    if note["kind"] == "overlap":
        text = (f"{name(b)} overlaps {name(a)} by {days} ({note['start']} – {note['end']}).")
        if b["status"] == "future":
            text += f" It becomes the active plan on {note['start']}, cutting {name(a)} short."
        return text
    return (f"{days} with no plan between {name(a)} and {name(b)} "
            f"({note['start']} – {note['end']}).")


def timeline_warnings(plan_id: str) -> list[str]:
    """The overlap / gap notes that involve one plan."""
    return [note_text(n) for n in timeline_notes() if plan_id in (n["first"]["id"], n["second"]["id"])]


# ── SEASON ────────────────────────────────────────────────────────────────────

SEASON_HISTORY_DAYS = 365   # archived plans that ended longer ago than this are left out


def season(today: date | None = None) -> dict:
    """Every plan on one timeline: per week the planned and completed minutes,
    phase and recovery flag; the phase spans; and the races. The active and
    future plans, plus archived ones from the last year."""
    _require_db()
    today = today or local_today()
    cutoff = (today - timedelta(days=SEASON_HISTORY_DAYS)).isoformat()
    # Two queries for the whole page, however many plans there are.
    rows, done = db.get_season_plans(cutoff)
    rows.sort(key=lambda r: (str((r["plan"].get("meta") or {}).get("planStartDate") or ""), r["id"]))
    plans = [_season_plan(row, done.get(row["id"], set())) for row in rows]
    return {"today": today.isoformat(), "plans": [p for p in plans if p["weeks"]]}


def _season_plan(row: dict, done_ids: set[str]) -> dict:
    plan = row["plan"]
    meta = plan.get("meta") or {}
    phase_of = {}
    for ph in plan.get("phases") or []:
        for n in range(int(ph.get("startWeek") or 0), int(ph.get("endWeek") or -1) + 1):
            phase_of.setdefault(n, ph.get("name"))
    weeks, races = [], []
    for w in sorted((w for w in plan.get("weeks") or [] if w.get("startDate") and w.get("endDate")),
                    key=lambda w: str(w["startDate"])):
        planned = done = 0
        for d in w.get("days") or []:
            for x in d.get("workouts") or []:
                if x.get("sport") == "rest":
                    continue
                minutes = float(x.get("durationMinutes") or 0)
                planned += minutes
                if x.get("id") in done_ids:
                    done += minutes
                if x.get("sport") == "race":
                    races.append({"date": str(d.get("date"))[:10], "name": x.get("name") or "Race"})
        weeks.append({
            "weekNumber": w.get("weekNumber"), "start": str(w["startDate"])[:10], "end": str(w["endDate"])[:10],
            "phase": w.get("phase") or phase_of.get(w.get("weekNumber")) or "",
            "recovery": bool(w.get("isRecoveryWeek")), "plannedMin": round(planned), "doneMin": round(done),
        })
    event = (plan.get("raceStrategy") or {}).get("event") or {}
    if event.get("date") and str(event["date"])[:10] not in {r["date"] for r in races}:
        races.append({"date": str(event["date"])[:10], "name": event.get("name") or meta.get("event") or "Race"})
    phases = []
    for w in weeks:
        if phases and phases[-1]["name"] == w["phase"]:
            phases[-1]["end"] = w["end"]
        else:
            phases.append({"name": w["phase"], "start": w["start"], "end": w["end"]})
    return {
        "id": row["id"], "status": row["status"], "event": meta.get("event") or row["id"],
        "start": weeks[0]["start"] if weeks else None, "end": weeks[-1]["end"] if weeks else None,
        "weeks": weeks, "phases": phases, "races": sorted(races, key=lambda r: r["date"]),
    }


# ── CREATE ────────────────────────────────────────────────────────────────────

CREATE_STATUSES = ("future", "archived")
CONFLICT_MODES = ("reject", "replace")
URL_TIMEOUT_S = 20


class PlanRejected(PlanError):
    """A plan that failed validation; ``errors``/``warnings`` are {"path", "message"}."""

    def __init__(self, errors: list[dict], warnings: list[dict]):
        self.errors, self.warnings = errors, warnings
        n = len(errors)
        super().__init__(f"Plan rejected, nothing was saved ({n} error{'s' if n != 1 else ''}):\n"
                         + plan_validation.format_issues(errors, warnings))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_plan_url(url: str) -> bytes:
    """The body behind an https URL, for ``create_training_plan(plan_url=…)``.

    Public hosts only (no loopback / private / link-local addresses), no
    redirects, size-capped; TRAINING_PLAN_URL_HOSTS (comma-separated) narrows it
    to named hosts.
    """
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or not parts.hostname:
        raise PlanError("plan_url must be an https:// URL.")
    allowed = [h.strip().lower() for h in os.environ.get("TRAINING_PLAN_URL_HOSTS", "").split(",") if h.strip()]
    if allowed and parts.hostname.lower() not in allowed:
        raise PlanError(f"plan_url host {parts.hostname!r} isn't allowed (allowed: {', '.join(allowed)}).")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443,
                                                               proto=socket.IPPROTO_TCP)}
    except OSError as e:
        raise PlanError(f"Couldn't resolve plan_url host {parts.hostname!r} ({e}).") from None
    if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise PlanError("plan_url must point at a public host.")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "garmin-mcp"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=URL_TIMEOUT_S) as response:
            body = response.read(MAX_UPLOAD_BYTES + 1)
    except (urllib.error.URLError, OSError) as e:
        raise PlanError(f"Couldn't fetch plan_url ({e}).") from None
    if len(body) > MAX_UPLOAD_BYTES:
        raise PlanError(f"plan_url is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    return body


def plan_counts(plan: dict) -> dict:
    weeks = plan.get("weeks") or []
    return {
        "weeks": len(weeks),
        "workouts": sum(1 for _ in plan_doc.iter_workouts(plan)),
        "skeleton_weeks": sum(1 for w in weeks if isinstance(w, dict) and plan_doc.is_skeleton_week(plan, w)
                              and not plan_doc.week_workouts(w)),
    }


def _overlap_warnings(plan_id: str, plan: dict) -> list[dict]:
    """Other future/active plans whose dates overlap this one — worth knowing,
    not a reason to refuse."""
    meta = plan.get("meta") or {}
    try:
        start, end = plan_doc.parse_date(meta.get("planStartDate")), plan_doc.parse_date(meta.get("planEndDate"))
    except PlanError:
        return []
    out = []
    for other in db.list_training_plans():
        if other["id"] == plan_id or other["status"] not in ("active", "future"):
            continue
        try:
            o_start = plan_doc.parse_date(other.get("start_date"))
            o_end = plan_doc.parse_date(other.get("end_date"))
        except PlanError:
            continue
        if start <= o_end and o_start <= end:
            days = (min(end, o_end) - max(start, o_start)).days + 1
            out.append({"path": "meta.planStartDate" if start >= o_start else "meta.planEndDate",
                        "message": f"Overlaps {other.get('event') or other['id']!r} ({other['status']}, "
                                   f"{o_start} – {o_end}) by {days} day{'s' if days != 1 else ''}."})
    return out


def create_plan(plan=None, plan_url: str | None = None, status: str = "future",
                validate_only: bool = False, on_conflict: str = "reject",
                idempotency_key: str | None = None) -> dict:
    """Validate, then atomically store, a new plan as revision 1.

    Never activates a plan, never touches the active one, and has no Garmin
    side effects. ``validate_only`` runs everything except the write.
    ``on_conflict="replace"`` swaps the content of an existing future or
    archived plan with the same id (as a new revision). With an
    ``idempotency_key`` a retried call returns the first call's result.
    """
    _require_db()
    if status == "active":
        raise PlanError("A plan can't be created as active — only the athlete activates a plan "
                        "(at /training-plan/plans, or a future plan takes over on its start date). "
                        "Create it as 'future'.")
    if status not in CREATE_STATUSES:
        raise PlanError(f"status must be one of {', '.join(CREATE_STATUSES)}.")
    if on_conflict not in CONFLICT_MODES:
        raise PlanError(f"on_conflict must be one of {', '.join(CONFLICT_MODES)}.")
    if idempotency_key is not None and not (isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 200):
        raise PlanError("idempotency_key must be a string of 1–200 characters.")
    if (plan is None) == (plan_url is None):
        raise PlanError("Give exactly one of plan (the JSON) or plan_url.")
    if plan_url is not None:
        plan = _parse_json(fetch_plan_url(plan_url))
    elif isinstance(plan, str):
        plan = _parse_json(plan.encode("utf-8"))
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object.")

    request_hash = hashlib.sha256(json.dumps(
        {"plan": plan, "status": status, "on_conflict": on_conflict}, sort_keys=True, default=str,
    ).encode()).hexdigest()
    if idempotency_key and not validate_only:
        recorded = db.get_idempotent_result(idempotency_key)
        if recorded:
            if recorded["request_hash"] != request_hash:
                raise PlanError(f"idempotency_key {idempotency_key!r} was already used for a different request.")
            return {**recorded["result"], "idempotent_replay": True}

    if len(json.dumps(plan, default=str)) > MAX_UPLOAD_BYTES:
        raise PlanError(f"The plan exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    errors, warnings = plan_validation.validate_document(plan)
    plan_id = (plan.get("meta") or {}).get("id")
    existing = None
    if isinstance(plan_id, str) and plan_doc.PLAN_ID_RE.match(plan_id):
        existing = db.get_training_plan(plan_id)   # not get_plan(): reading must not switch plans
        if existing and existing["status"] == "active":
            errors.append({"path": "meta.id", "message": f"{plan_id!r} is the active plan; creating a plan "
                                                          "never touches the active one."})
        elif existing and on_conflict == "reject":
            errors.append({"path": "meta.id", "message": (
                f"A plan with this id already exists ({existing['status']}, version {existing['version']}). "
                "Pass on_conflict='replace' to overwrite it, or use a new id.")})
        elif existing:
            new_ids = {w.get("id") for _, _, w in plan_doc.iter_workouts(plan)}
            lost = sorted(wid for wid in completed_map(plan_id) if wid not in new_ids)
            warnings.append({"path": "meta.id", "message": (
                f"Replacing the stored plan (version {existing['version']}; it stays in the history)."
                + (f" {len(lost)} completed workout(s) aren't in the new plan: {', '.join(lost[:5])}"
                   f"{'…' if len(lost) > 5 else ''}." if lost else ""))})
        if not errors:
            warnings += _overlap_warnings(plan_id, plan)
    counts = plan_counts(plan)

    if validate_only:
        return {"valid": not errors, "validate_only": True, "plan_id": plan_id, "status": status,
                "would": None if errors else "replace" if existing else "create",
                "counts": counts, "errors": errors, "warnings": warnings}
    if errors:
        raise PlanRejected(errors, warnings)

    plan = copy.deepcopy(plan)
    if existing and "unit" not in plan and existing["plan"].get("unit"):
        plan["unit"] = existing["plan"]["unit"]
    plan_doc.normalize_plan(plan)
    n = counts
    summary = (f"Created plan ({n['weeks']} weeks, {n['workouts']} workouts)" if not existing
               else f"Replaced plan content ({n['weeks']} weeks, {n['workouts']} workouts)")

    def build(row, outcome):
        return {"plan_id": row["id"], "revision": row["version"], "version": row["version"],
                "status": row["status"], "outcome": outcome, "counts": counts, "warnings": warnings,
                "idempotent_replay": False}

    try:
        _, result, replayed = db.create_training_plan(
            plan_id, plan, status, summary, on_conflict == "replace", build, idempotency_key, request_hash)
    except db.PlanExists as e:   # lost a race with another create
        raise PlanError(f"A plan with id {plan_id!r} already exists ({e.status}). Pass "
                        "on_conflict='replace' to overwrite it, or use a new id.") from None
    except db.PlanIsActive:
        raise PlanError(f"{plan_id!r} is the active plan; creating a plan never touches the active one.") from None
    except db.IdempotencyKeyReused:
        raise PlanError(f"idempotency_key {idempotency_key!r} was already used for a different request.") from None
    return {**result, "idempotent_replay": True} if replayed else result


def _parse_json(raw: bytes):
    if not raw or not raw.strip():
        raise PlanError("The plan is empty.")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise PlanError("The plan is not UTF-8 text.") from None
    except ValueError as e:
        raise PlanError(f"The plan is not valid JSON ({e}).") from None


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


def save_upload(plan: dict, activate: bool = True) -> dict:
    """Store an uploaded plan (replacing same-id content) — as the active
    plan, or with ``activate=False`` as a future plan (a stored plan keeps its
    status then; an archived one becomes a future plan).

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
    previous = db.get_training_plan(None) if activate else None
    row = db.save_uploaded_training_plan(plan_id, plan, summary, activate)
    if activate:
        _offer_carryover(previous, plan_id, False, None)
    return row


def set_status(plan_id: str, status: str) -> None:
    _require_db()
    if status not in STATUSES:
        raise PlanError(f"Unknown plan status {status!r}.")
    if status == "active":
        return activate(plan_id)
    if not db.set_training_plan_status(plan_id, status):
        raise LookupError(f"No plan with id {plan_id!r}.")


def delete_plan(plan_id: str) -> None:
    """Permanently delete a future or archived plan (the active one can't be deleted)."""
    row = require_plan(plan_id)
    if row["status"] == "active":
        raise PlanError("Archive the active plan before deleting it.")
    db.delete_training_plan(plan_id)


# ── EDIT ──────────────────────────────────────────────────────────────────────

def apply_operations(plan_id: str | None, operations: list, source: str,
                     reason: str | None = None) -> dict:
    """Apply edits atomically to a plan (default: the active one).

    Archived plans are read-only. Returns the new row plus the change
    messages, the ids of the workouts touched and any warnings.
    """
    row = require_editable(plan_id)
    result = {}
    old_dates = {w.get("id"): str(d.get("date"))[:10] for _, d, w in plan_doc.iter_workouts(row["plan"])}
    old_ids = {x.get("id"): wk.get("weekNumber") for wk, _, x in plan_doc.iter_workouts(row["plan"])}

    def mutate(plan):
        warnings = []
        new_plan, messages, touched = plan_doc.apply_operations(plan, operations, warnings)
        result["changes"], result["touched"], result["warnings"] = messages, touched, warnings
        summary = "; ".join(messages) or "No changes"
        if reason:
            summary = f"{reason.strip()} — {summary}"
        return new_plan, summary

    new_row = db.update_training_plan(row["id"], mutate, source)
    _unskip_rescheduled(new_row, old_dates, result.get("touched") or [])
    replaced = {op.get("week_number") for op in operations if isinstance(op, dict) and op.get("op") == "replace_week"}
    if replaced:
        now_ids = {w.get("id") for _, _, w in plan_doc.iter_workouts(new_row["plan"])}
        lost = sorted(wid for wid, state in db.get_workout_states(row["id"]).items()
                      if state.get("completed") and wid not in now_ids and old_ids.get(wid) in replaced)
        if lost:
            result["warnings"].append({"path": "weeks", "message": (
                f"{len(lost)} completed workout(s) from the replaced week(s) are no longer in the plan: "
                f"{', '.join(lost[:5])}{'…' if len(lost) > 5 else ''}.")})
    return {"row": new_row, **result}


def _unskip_rescheduled(row: dict, old_dates: dict, touched: list) -> None:
    """A skipped workout moved to today or later is back on the schedule."""
    today = local_today().isoformat()
    moved = [w["id"] for _, d, w in plan_doc.iter_workouts(row["plan"])
             if w.get("id") in touched and str(d.get("date"))[:10] >= today
             and str(d.get("date"))[:10] != old_dates.get(w["id"])]
    if not moved:
        return
    states = db.get_workout_states(row["id"])
    for wid in moved:
        if (states.get(wid) or {}).get("skipped") is not None:
            db.upsert_workout_state(row["id"], wid, **_NOT_SKIPPED)


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
        "skipped": bool(state.get("skipped")) and not state.get("completed"),
    }
    if out["skipped"]:
        out["skippedAt"] = _iso(state.get("skipped_at"))
        out["skippedAuto"] = bool(state.get("skipped_auto"))
        out["skipReason"] = state.get("skip_reason")
    return {k: v for k, v in out.items() if v not in (None, False)}


# Clears a skip, leaving it to the automatic rule again (a workout whose day
# has passed without being done is skipped on the next read).
_NOT_SKIPPED = {"skipped": None, "skipped_at": None, "skipped_auto": False, "skip_reason": None}


def set_completion(plan_id: str | None, workout_id: str, completed: bool = True,
                   activity_id: int | None = None, notes: str | None = None) -> dict:
    row = require_editable(plan_id)
    hit = plan_doc.find_workout(row["plan"], workout_id)
    if hit is None:
        raise PlanError(f"No workout with id {workout_id!r} in this plan.")
    fields = {
        "completed": bool(completed),
        "completed_at": datetime.now(timezone.utc) if completed else None,
        **_NOT_SKIPPED,
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


MAX_SKIP_REASON = 200


def set_skip(plan_id: str | None, workout_id: str, skipped: bool = True,
             reason: str | None = None, today: date | None = None) -> dict:
    """Mark a planned workout skipped (clearing any completion), or un-skip
    it. Un-skipping a workout whose day has passed keeps it un-skipped — the
    automatic skip leaves it alone; a later one goes back to the schedule.

    ``reason`` says why (None leaves it as is, "" clears it). Skipping a
    workout that's already skipped only changes the reason, so a missed
    workout explained afterwards still reads as missed."""
    row = require_editable(plan_id)
    hit = plan_doc.find_workout(row["plan"], workout_id)
    if hit is None:
        raise PlanError(f"No workout with id {workout_id!r} in this plan.")
    week, day, workout = hit
    if workout.get("sport") == "rest":
        raise PlanError("Rest days can't be skipped.")
    reason = reason.strip() if isinstance(reason, str) else reason
    if reason and len(reason) > MAX_SKIP_REASON:
        raise PlanError(f"Keep the reason under {MAX_SKIP_REASON} characters.")
    current = db.get_workout_states(row["id"]).get(workout_id) or {}
    if skipped and current.get("skipped") and not current.get("completed"):
        fields = {}
    elif skipped:
        fields = {"skipped": True, "skipped_at": datetime.now(timezone.utc), "skipped_auto": False,
                  "completed": False, "completed_at": None, "activity_id": None}
    else:
        past = str(day.get("date"))[:10] < (today or local_today()).isoformat()
        fields = {**_NOT_SKIPPED, "skipped": False if past else None}
    if skipped and reason is not None:
        fields["skip_reason"] = reason or None
    state = db.upsert_workout_state(row["id"], workout_id, **fields) if fields else current
    return {
        "plan_id": row["id"],
        "workout_id": workout_id,
        "name": workout.get("name"),
        "date": day.get("date"),
        "week": week.get("weekNumber"),
        **_state_dict(state),
        "skipped": bool(skipped),
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


# ── AUTOMATIC MATCHING ────────────────────────────────────────────────────────
# Linking each planned workout to the Garmin activity that did it is what lets
# the dashboard's Today screen and the plan show the two together.

def activity_day(activity: dict) -> str:
    """An activity's local calendar date (YYYY-MM-DD)."""
    local = ((activity.get("summary") or {}).get("date")) if isinstance(activity.get("summary"), dict) else None
    return str(local or _iso(activity.get("activity_date")) or "")[:10]


def _closest(hits: list[tuple], activity: dict) -> tuple:
    """Of several candidate workouts, the one whose planned duration is
    closest to what the activity actually took."""
    took = activity.get("duration_min") or 0
    return min(hits, key=lambda h: abs((h[2].get("durationMinutes") or 0) - took))


def match_new_activities(activity_ids: list[int], plan_id: str | None = None) -> list[dict]:
    """Tick off the planned workouts that newly synced activities completed.

    Each activity completes the unticked workout on its date with the same
    sport (the closest in duration when there are several) — a skipped one
    too, so a sync that lands after the day passed still ticks it. Only
    activities synced for the first time are passed in, so un-ticking a workout in the
    plan is never undone by the next sync. Returns one entry per tick.
    """
    if not activity_ids or not db.is_configured():
        return []
    row = active_plan() if plan_id is None else db.get_training_plan(plan_id)
    if row is None or row["status"] != "active":
        return []
    states = db.get_workout_states(row["id"])
    linked = {s.get("activity_id") for s in states.values() if s.get("activity_id")}
    activities = sorted(db.get_activities_by_ids(list(activity_ids)).values(), key=activity_day)
    matched = []
    for act in activities:
        sport = sport_for_activity_type(act.get("activity_type"))
        if act["garmin_id"] in linked or sport is None:
            continue
        try:
            day = plan_doc.parse_date(activity_day(act))
        except PlanError:
            continue
        hits = [h for h in match_activity(row["plan"], states, day, sport)
                if not (states.get(h[2]["id"]) or {}).get("completed")]
        if not hits:
            continue
        workout = _closest(hits, act)[2]
        states[workout["id"]] = db.upsert_workout_state(
            row["id"], workout["id"], completed=True,
            completed_at=datetime.now(timezone.utc), activity_id=act["garmin_id"], **_NOT_SKIPPED,
        )
        linked.add(act["garmin_id"])
        matched.append({"workout_id": workout["id"], "activity_id": act["garmin_id"]})
    return matched


def link_completed_workouts(plan_id: str | None = None, workout_ids: list[str] | None = None,
                            today: date | None = None) -> list[dict]:
    """Attach a Garmin activity to workouts ticked by hand (in the viewer, or
    before matching existed) that have none yet — the same date and sport,
    not already linked elsewhere. Only fills in the link; never ticks or
    un-ticks anything. ``workout_ids`` limits it to those workouts."""
    if not db.is_configured():
        return []
    row = active_plan() if plan_id is None else db.get_training_plan(plan_id)
    if row is None or row["status"] != "active":
        return []
    today = today or local_today()
    states = db.get_workout_states(row["id"])
    pending = [
        (d, w) for _, d, w in plan_doc.iter_workouts(row["plan"])
        if (states.get(w.get("id")) or {}).get("completed") and not states[w["id"]].get("activity_id")
        and (workout_ids is None or w.get("id") in workout_ids)
        and str(d.get("date"))[:10] <= today.isoformat()
    ]
    if not pending:
        return []
    days = sorted(str(d.get("date"))[:10] for d, _ in pending)
    end = (plan_doc.parse_date(days[-1]) + timedelta(days=1)).isoformat()
    by_day: dict[str, list[dict]] = {}
    for act in db.get_activities_in_range(days[0], end):
        by_day.setdefault(activity_day(act), []).append(act)
    linked = {s.get("activity_id") for s in states.values() if s.get("activity_id")}
    done = []
    for d, w in pending:
        candidates = [
            a for a in by_day.get(str(d.get("date"))[:10], [])
            if a["garmin_id"] not in linked and _sport_fits(sport_for_activity_type(a.get("activity_type")), w.get("sport"))
        ]
        if not candidates:
            continue
        took = w.get("durationMinutes") or 0
        act = min(candidates, key=lambda a: abs((a.get("duration_min") or 0) - took))
        db.upsert_workout_state(row["id"], w["id"], activity_id=act["garmin_id"])
        linked.add(act["garmin_id"])
        done.append({"workout_id": w["id"], "activity_id": act["garmin_id"]})
    return done


def _sport_fits(activity_sport: str | None, workout_sport: str | None) -> bool:
    if activity_sport is None:
        return False
    return activity_sport == workout_sport or "brick" in (activity_sport, workout_sport)


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


def send_to_garmin(plan_id: str | None, workout_id: str) -> dict:
    """Create a planned workout in Garmin Connect from its structured steps,
    schedule it on its plan day, and record the link (as
    :func:`link_garmin_workout` does). Refuses a workout already linked, so a
    second tap can't make a duplicate."""
    from tools import workout_steps
    from tools.workout import create_workout
    row = require_editable(plan_id)
    hit = plan_doc.find_workout(row["plan"], workout_id)
    if hit is None:
        raise PlanError(f"No workout with id {workout_id!r} in this plan.")
    _, day, workout = hit
    state = db.get_workout_states(row["id"]).get(workout_id) or {}
    if state.get("garmin_workout_id"):
        raise PlanError(f"Already on Garmin (workout {state['garmin_workout_id']}).")
    sport = workout.get("sport")
    steps = workout_steps.clean_steps(sport, workout.get("steps")) if workout.get("steps") else None
    sport_type, garmin_steps = workout_steps.to_garmin(row["plan"], sport, steps)
    on = str(day.get("date"))[:10]
    try:
        result = create_workout(name=workout.get("name") or "Workout", sport_type=sport_type,
                                steps=garmin_steps, schedule_date=on)
    except ValueError as e:     # the builder's own checks
        raise PlanError(f"Garmin workout not built: {e}") from None
    if result.get("workout_id"):
        link_garmin_workout(row["id"], workout_id, result["workout_id"], on)
    return {"workout_id": workout_id, "garmin_workout_id": result.get("workout_id"),
            "scheduled_date": result.get("scheduled_date"), "warnings": result.get("warnings") or []}


# ── MCP READ VIEW ─────────────────────────────────────────────────────────────

def _workout_view(workout: dict, day: dict, state: dict, include_details: bool) -> dict:
    keys = ["id", "sport", "type", "name", "durationMinutes", "distanceKm", "distanceMeters",
            "distanceKmRange", "primaryZone", "keyTargets", "terrain", "difficulty", "trainingEffect",
            "startTime"]
    if include_details:
        keys += ["description", "humanReadable", "steps"]
    out = {k: workout[k] for k in keys if workout.get(k) not in (None, "")}
    out["date"] = day.get("date")
    out.update(_state_dict(state))
    out["completed"] = bool(state and state.get("completed"))
    return out


def _week_drift(plan: dict, week: dict) -> list[str]:
    """Messages where a filled week's workouts stray from its targetHours /
    targetRunKm by more than the validator's tolerance."""
    if not plan_doc.week_workouts(week):
        return []
    report = plan_validation.Report()
    plan_validation._check_targets(report, {**week, "summary": None}, "week")
    return [w["message"] for w in report.warnings]


def _provisional_zones(plan: dict, today: date) -> dict | None:
    """The zones block's ``provisional`` flag, with whether its re-anchor date
    has come."""
    zones = plan.get("zones")
    if not isinstance(zones, dict) or zones.get("provisional") is not True:
        return None
    out = {"reanchorBy": zones.get("reanchorBy")}
    try:
        due = plan_doc.parse_date(zones.get("reanchorBy"))
    except PlanError:
        return out
    out["daysLeft"] = (due - today).days
    out["overdue"] = due < today
    return out


def plan_overview(plan_id: str | None = None, week_number: int | None = None,
                  start_date: str | None = None, end_date: str | None = None,
                  include_details: bool = True, today: date | None = None,
                  sections: list | None = None, include_weeks: bool = True) -> dict:
    """A token-friendly read of a plan for the coach.

    Always: meta, status/version, current thresholds, phases and a one-line
    summary per week. Workouts: the requested week, or the weeks overlapping
    start_date..end_date, or by default the current and next week.
    ``sections`` adds those top-level parts of the document verbatim (e.g.
    raceStrategy, assessment, zones) under ``sections`` — what set_field edits.
    ``include_weeks=False`` leaves out every week (detail and one-liners) for a
    plan-level read.
    """
    row = require_plan(plan_id)
    plan = row["plan"]
    today = today or local_today()
    states = workout_states(row, today)

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

    skipped = skipped_map(states)

    def week_line(w):
        ids = [x.get("id") for d in w.get("days") or [] for x in d.get("workouts") or [] if x.get("sport") != "rest"]
        line = {
            "weekNumber": w.get("weekNumber"), "startDate": w.get("startDate"), "endDate": w.get("endDate"),
            "phase": w.get("phase"), "isRecoveryWeek": w.get("isRecoveryWeek", False),
            "targetHours": w.get("targetHours"), "plannedHours": (w.get("summary") or {}).get("totalHours"),
            "workouts": len(ids), "completed": sum(1 for i in ids if (states.get(i) or {}).get("completed")),
            "skipped": sum(1 for i in ids if i in skipped),
        }
        if plan_doc.is_skeleton_week(plan, w) and not plan_doc.week_workouts(w):
            line["status"] = "skeleton"
        if w.get("targetRunKm") is not None:
            line["targetRunKm"] = w["targetRunKm"]
            line["plannedRunKm"] = (((w.get("summary") or {}).get("bySport") or {}).get("run") or {}).get("km", 0)
        drift = _week_drift(plan, w)
        if drift:
            line["drift"] = drift
        return line

    if sections is not None and (not isinstance(sections, list)
                                 or not all(isinstance(k, str) for k in sections)):
        raise PlanError("sections must be a list of top-level keys, e.g. ['raceStrategy', 'zones'].")
    unknown = [k for k in sections or [] if k == "weeks" or k not in plan]
    if unknown:
        available = ", ".join(k for k in plan if k != "weeks")
        raise PlanError(f"No section {', '.join(map(repr, unknown))} in this plan (available: {available}).")

    detail = []
    for w in selected if include_weeks else []:
        detail.append({
            # Any extra week fields the coach added, then the standard view.
            **{k: v for k, v in w.items() if k not in ("days", "summary")},
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
        **({"weekSummaries": [week_line(w) for w in weeks], "weeks": detail} if include_weeks else {}),
        **({"zonesProvisional": prov} if (prov := _provisional_zones(plan, today)) else {}),
        **({"sections": {k: plan[k] for k in sections}} if sections else {}),
        **({"thresholdCarryover": carry} if (carry := pending_carryover(row)) else {}),
    }
