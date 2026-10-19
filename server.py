# server.py
import os
import logging
import zlib
from fastmcp import FastMCP
from dotenv import load_dotenv

from tools.profile import get_athlete_profile, get_gear
from tools.challenges import (
    get_active_goals,
    get_earned_badges,
    get_adhoc_challenges,
)
from tools.activities import (
    get_activities,
    get_activity,
    get_activity_summary,
    get_weekly_summary,
    get_swim_records,
)
from tools.health import (
    get_sleep,
    get_daily_readiness,
    get_daily_health,
    get_training_status,
    get_training_readiness,
)
from tools.trends import (
    get_performance_predictions,
    get_performance_trends,
)
from tools.performance import (
    get_endurance_score,
    get_running_tolerance,
    get_personal_records,
)
from tools import db_first
from tools.weekly_summaries import save_summary as save_weekly_summary
from tools.workout import (
    get_scheduled_workouts as get_scheduled_workouts_impl,
    get_saved_workouts as get_saved_workouts_impl,
    get_workout_detail as get_workout_detail_impl,
    schedule_workout as schedule_workout_impl,
    unschedule_workout as unschedule_workout_impl,
    create_workout as create_workout_impl,
    delete_workout as delete_workout_impl,
    update_workout_weights as update_workout_weights_impl,
    patch_workout_step as patch_workout_step_impl,
)
from tools.exercise_db import search_exercises as search_exercises_impl
from tools.strength_sets import (
    strength_sets as strength_sets_impl,
    compare_strength_session as compare_strength_session_impl,
)
from tools.gear_tracker import (
    log_maintenance as log_maintenance_impl,
    get_maintenance_status as get_maintenance_status_impl,
)
from tools import plan_tools

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("garmin")

# ── AUTH MIDDLEWARE ───────────────────────────────────────────────────────────

import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN")


# ── TOOLS ─────────────────────────────────────────────────────────────────────

from typing import Literal, Optional

@mcp.tool()
def athlete_profile() -> dict:
    """
    Get the athlete's profile including weight, height, VO2max (running and
    cycling), lactate threshold heart rate and pace, and FTP.
    """
    return get_athlete_profile()


@mcp.tool()
def recent_activities(
    limit: int = 10,
    sport_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list:
    """
    Get Garmin activities with summary metrics.
    When start_date is provided a date-range query is used and all matching
    activities are returned (limit is ignored).  Without dates, the most
    recent activities up to limit are returned.
    Args:
        limit:      Activities to return when no date range given (default 10, max 50)
        sport_type: Optional filter — 'running', 'road_biking', 'lap_swimming', etc.
        start_date: Optional start date YYYY-MM-DD (inclusive)
        end_date:   Optional end date YYYY-MM-DD (inclusive, defaults to today)
    """
    return get_activities(limit=limit, sport_type=sport_type,
                          start_date=start_date, end_date=end_date)


@mcp.tool()
def activity_summary(
    start_date: str,
    end_date: str,
    sport_type: Optional[str] = None,
) -> dict:
    """
    Get aggregated training stats over a date range in a single call — answers
    questions like "how much did I run in 2025?" or "what was my cycling volume
    in May?". Aggregates count, total & average distance/duration, total
    calories, and total elevation gain.

    When sport_type is given, the totals cover just that sport. When omitted,
    the top-level totals cover all sports and a per-sport breakdown is returned
    under 'by_sport' (keyed by Garmin sport typeKey, each with the same totals/
    averages).

    Args:
        start_date: Start date YYYY-MM-DD (inclusive)
        end_date:   End date YYYY-MM-DD (inclusive)
        sport_type: Optional filter — 'running', 'road_biking', 'lap_swimming',
                    etc. Omit for all sports with a per-sport breakdown.
    """
    return get_activity_summary(
        start_date=start_date, end_date=end_date, sport_type=sport_type
    )


@mcp.tool()
def activity_detail(activity_id: int) -> dict:
    """
    Get full detail for a single activity including summary metrics,
    per-lap splits with running dynamics, structured-workout interval/phase
    breakdown (warmup/active/recovery/rest/cooldown — useful for pace-by-rep
    or power-by-rep analysis of interval workouts, empty for non-workout
    activities), and HR time-in-zones.

    For a multisport activity (triathlon, duathlon, …) the result also
    includes 'sub_activities': the swim / T1 / bike / T2 / run legs, each
    with its own discipline metrics (swim pace per 100m, bike power and
    speed, run pace) and lap splits.

    For a strength activity the result also includes 'exercises': a compact
    per-exercise summary of the sets actually performed (sets, reps, top weight
    in kg/lb, volume). Use strength_sets for the full set-by-set data.

    'gear' lists the shoes/bike linked to the activity (name, uuid, type and
    cumulative distance), and is empty when no gear was assigned. For a
    multisport activity each leg carries its own 'gear' — the bike on the
    bike leg, the shoes on the run — and the top-level list is the union
    across all legs.

    Args:
        activity_id: Garmin activity ID (get from recent_activities)
    """
    return get_activity(activity_id)


@mcp.tool()
def sleep(date: str) -> dict:
    """
    Get sleep quality and recovery metrics for a given date.
    Garmin files sleep under the wake-up date — so 'last night's sleep'
    should use today's date, not yesterday's.

    Served from the synced database when a copy is available (final for a
    past day, or synced within the last ~15 min), else live from Garmin;
    'data_source' says which ('db' or 'live', with 'synced_at').

    Args:
        date: Date in YYYY-MM-DD format. Use today's date for last night's sleep.
    """
    return db_first.daily_payload("sleep_data", date, get_sleep)


@mcp.tool()
def daily_readiness(date: str) -> dict:
    """
    Get recovery-focused daily readiness for a given date — HRV, body
    battery (start/current/highest/lowest levels and sleep gain), and
    daily activity & stress stats (RHR, 7-day RHR average, average and
    max stress, steps, active seconds).

    Served from the synced database when a copy is available (final for a
    past day, or synced within the last ~15 min), else live from Garmin;
    'data_source' says which ('db' or 'live', with 'synced_at').

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return db_first.daily_payload("readiness_data", date, get_daily_readiness)


@mcp.tool()
def daily_health(date: str = 'today') -> dict:
    """
    Get a daily health snapshot for a given date — resting/max/min heart
    rate, all-day stress zones (avg/max stress level and time-in-zone
    minutes), body battery charged/drained, and respiration rate
    (waking/sleep averages and range).

    Served from the synced database when a copy is available (final for a
    past day, or synced within the last ~15 min), else live from Garmin;
    'data_source' says which ('db' or 'live', with 'synced_at').

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return db_first.daily_payload("health_data", date, get_daily_health)


@mcp.tool()
def training_status(date: str) -> dict:
    """
    Get training status for a given date — acute:chronic workload ratio
    (ACWR) and status, training load balance phrase, training status
    feedback phrase and sport, and current VO2max for running and cycling.

    Served from the synced database when a copy is available (final for a
    past day, or synced within the last ~15 min), else live from Garmin;
    'data_source' says which ('db' or 'live', with 'synced_at').

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return db_first.daily_payload("training_status_data", date, get_training_status)

@mcp.tool()
def performance_predictions() -> dict:
    """
    Get current race time predictions for 5K, 10K, half marathon, and marathon
    based on recent training data and VO2max estimates.
    """
    return get_performance_predictions()

@mcp.tool()
def performance_trends(period: str = 'weekly', lookback: int = 4) -> list:
    """
    Get trends for HRV and VO2max over recent weeks or months.
    Each entry covers one period end-date and reports the HRV weekly average
    and baseline, plus the most recent VO2max (running & cycling).
    Args:
        period:   'weekly' or 'monthly'
        lookback: Number of periods to include (max 26 weekly, 12 monthly)
    """
    return get_performance_trends(period=period, lookback=lookback)

@mcp.tool()
def get_trends(period: str = '1m', metrics: Optional[list] = None) -> dict:
    """
    Get pre-aggregated health & performance trends over a trailing window, so a
    trend view needs one call instead of dozens of individual per-day lookups.

    Supported periods: 7d, 14d, 1m (30d), 42d, 3m (90d), 6m (180d), 1y (365d).

    Metrics (all optional, defaults to all):
        rhr           — resting heart rate
        hrv           — overnight HRV last-night average
        sleep_score   — overall sleep score
        body_battery  — daily peak/wake level and total drain
        stress        — all-day average stress
        steps         — daily total steps
        training_load — daily acute training load

    For each metric series the result returns per-day values, trailing rolling
    7-day and 28-day averages, the period start→end value and delta, and the
    window min/max/avg. 'body_battery' expands into two series
    ('body_battery_wake', 'body_battery_drain').

    Days already synced to the database are read from there and only the
    rest are fetched live; 'data_source' reports the split ('db', 'live' or
    'db+live', with db_days / live_days and 'synced_at').

    Args:
        period:  One of 7d, 14d, 1m, 42d, 3m, 6m, 1y (default 1m).
        metrics: Optional list of metric names to include (defaults to all).
    """
    return db_first.get_trends(period=period, metrics=metrics)

@mcp.tool()
def training_readiness(date: str = 'today') -> dict:
    """
    Get training readiness for a given date — a composite score (0–100)
    indicating whether to train hard today, plus level, feedback phrases,
    contributing factors (sleep, recovery, ACWR, stress, HRV), and the
    morning readiness snapshot.

    Served from the synced database when a copy is available (final for a
    past day, or synced within the last ~15 min), else live from Garmin;
    'data_source' says which ('db' or 'live', with 'synced_at').

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return db_first.daily_payload("training_data", date, get_training_readiness)


@mcp.tool()
def endurance_score(start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> dict:
    """
    Get endurance score, classification (beginner → elite), gauge limits,
    period average/max, and per-sport contribution breakdown for a date range.
    Defaults to the trailing 30 days.

    Args:
        start_date: Optional start date YYYY-MM-DD or 'today' / 'yesterday'
        end_date:   Optional end date YYYY-MM-DD or 'today' / 'yesterday'
    """
    return get_endurance_score(start_date=start_date, end_date=end_date)


@mcp.tool()
def running_tolerance(start_date: Optional[str] = None,
                      end_date: Optional[str] = None) -> dict:
    """
    Get running load tolerance metrics for a date range — current tolerance,
    weekly running load and its lower/upper bounds, plus acute and chronic
    load. Defaults to the trailing 7 days.

    Args:
        start_date: Optional start date YYYY-MM-DD or 'today' / 'yesterday'
        end_date:   Optional end date YYYY-MM-DD or 'today' / 'yesterday'
    """
    return get_running_tolerance(start_date=start_date, end_date=end_date)


@mcp.tool()
def gear() -> list:
    """
    Get the athlete's registered gear (shoes, bikes, etc.) with name,
    activity type, distance and time used, and current status.
    """
    return get_gear()


@mcp.tool()
def log_maintenance(gear_name: str, component: str, action: str,
                    notes: Optional[str] = None) -> dict:
    """
    Log a maintenance action against a tracked bike component (chain lubed,
    tires replaced, etc.) — also viewable/loggable in the Gear tab at /dashboard.

    Resolves the bike by its Garmin gear name and the component by name
    (case-insensitive), auto-creating the component on this bike if it
    hasn't been tracked yet (picking up a default maintenance interval when
    the name matches a known one — chain, brake pads, tires, chain ring,
    cassette). The bike's current cumulative distance (from Garmin) is
    recorded as the service point automatically.

    Args:
        gear_name: Exact gear name as registered in Garmin Connect (see gear).
        component: Component name, e.g. "Chain", "Brake pads", "Tires".
        action:    What was done — "lubed", "replaced", "serviced",
                   "adjusted", or a short free-form description.
        notes:     Optional free-form notes.
    """
    return log_maintenance_impl(gear_name=gear_name, component=component,
                                action=action, notes=notes)


@mcp.tool()
def get_maintenance_status(gear_name: Optional[str] = None) -> dict:
    """
    Get bike component maintenance status — last serviced date, distance
    since service, maintenance interval, and a status ("green"/"yellow"/
    "red"/"unknown" — unknown means no interval is set) for every tracked
    component, so overdue maintenance can be flagged proactively.

    Args:
        gear_name: Optional exact gear name to scope to one bike (see gear).
                   Omit to get every registered piece of gear.
    """
    return get_maintenance_status_impl(gear_name=gear_name)


# ── TRAINING PLAN (Claude Coach, stored in PostgreSQL) ───────────────────────

@mcp.tool()
def get_training_plan(
    plan_id: Optional[str] = None,
    week_number: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    include_details: bool = True,
    sections: Optional[list[str]] = None,
) -> dict:
    """
    Read the athlete's training plan (the Claude Coach plan uploaded at
    /training-plan). Defaults to the active plan; pass plan_id to read a
    future plan (planned ahead) or an archived one.

    Always returns the plan meta, version, current thresholds (FTP, LTHRs,
    threshold pace, CSS — including any updated in the viewer), phases, and a
    one-line summary per week (planned vs target hours, completed and
    skipped counts). Workout detail is returned for week_number, or for the
    weeks overlapping start_date..end_date, or by default for the current and
    next week. Each workout carries its id, date, completion state, whether
    it was skipped (skippedAuto: missed, its day passed undone), the Garmin
    activity that completed it, and any Garmin Connect workout already scheduled for it
    (garminWorkoutId) — check that before scheduling to avoid duplicates.
    When the active plan just took over from another and the two plans'
    thresholds differ, thresholdCarryover lists them (the athlete picks which
    to keep in the viewer).

    Args:
        plan_id: Plan id (meta.id). Omit for the active plan.
        week_number: One week of detail.
        start_date / end_date: YYYY-MM-DD range of weeks to detail.
        include_details: False omits description/humanReadable to save tokens.
        sections: Top-level parts of the plan document to return in full
            under "sections" — e.g. ["raceStrategy", "assessment", "zones",
            "phases", "preferences"] — before editing them with
            amend_training_plan's set_field/append_to_list.
    """
    return plan_tools.get_training_plan(plan_id, week_number, start_date, end_date, include_details,
                                        sections)


@mcp.tool()
def list_training_plans() -> list:
    """
    List every stored training plan (id, event, athlete, dates, status,
    version, last update): the active plan first, then future plans by start
    date, then archived ones. Only one plan is active; future plans are
    planned ahead, can be amended (pass their plan_id), and become active by
    themselves on their start date; archived plans are read-only.
    """
    return plan_tools.list_training_plans()


@mcp.tool()
def amend_training_plan(operations: list[dict], reason: str, plan_id: Optional[str] = None) -> dict:
    """
    Change any part of the active training plan — or of a future plan,
    with its plan_id — without regenerating it. All operations apply
    atomically (all or none), are saved as one revision labelled with
    `reason` (restorable from the plan's history), and weekly totals are
    recomputed. Archived plans are read-only.

    Operations (each a dict with "op"):
      {"op": "add_workout", "date": "YYYY-MM-DD", "workout": {"sport": "run",
          "name": "...", "type": "...", "durationMinutes": 45, "distanceKm": 8,
          "primaryZone": "Zone 2", "description": "...", "keyTargets": "...",
          "humanReadable": "..."}}          — id is generated (w{week}-{day}-{sport})
      {"op": "update_workout", "workout_id": "w5-thu-run", "fields": {...}}
          — only the given fields change; null/"" removes a field; a "date"
            field moves the workout
      {"op": "move_workout", "workout_id": "...", "date": "YYYY-MM-DD"}
      {"op": "remove_workout", "workout_id": "..."}
      {"op": "update_week", "week_number": 5, "fields": {"focus": "...",
          "targetHours": 8.5, "isRecoveryWeek": true, "phase": "..."}}
          — any week field except weekNumber, startDate, endDate, days and
            summary; null removes one
      {"op": "set_zones", "ftp": 262, "bikeLthr": 164, "runLthr": 170,
          "thresholdPace": "4:10", "cssLabel": "2:05/100m"}  — any subset;
            null resets one to the plan's original value
      {"op": "set_zone_validation", "sport": "bike", "validated": true}
      {"op": "set_unit", "unit": "metric" | "imperial"}
      {"op": "set_field", "path": "raceStrategy.pacing.bike.targetPower",
          "value": "NP 210W"}  — set anything else in the plan document by
            dotted path (list items by index: "phases.2.focus" or
            "phases[2].focus"; -1 is the last); missing objects are created;
            a value may be a whole object or list; null removes the field or
            list item. Covers meta (event, eventDate, athlete, planEndDate,
            totalWeeks…), preferences, assessment, zones (the plan's own
            zone tables and source notes), phases and raceStrategy.
      {"op": "append_to_list", "path": "raceStrategy.keyRisks",
          "value": "...", "index": 0}  — add an item to a list (created if
            missing); index is optional (default: the end)
    set_field/append_to_list can't touch meta.id, meta.updatedAt or weeks
    (use update_week and the workout ops), nor overrides/zonesValidated/unit
    (use set_zones/set_zone_validation/set_unit). Changing meta or phase
    dates doesn't move any weeks or workouts. Read the plan first
    (get_training_plan) to see the current structure.

    sport is one of swim, bike, run, brick, strength, race, rest, other.
    A workout may carry "startTime": "HH:MM" (24-hour) — the calendar feed
    uses it instead of the athlete's weekly schedule; set it only for one-off
    times (a race start, a group ride), and "" removes it.
    Follow the coach skill's field budgets (description/keyTargets ≤ 120
    characters) and humanReadable template with zone tokens — live, they
    follow the plan's thresholds: {{run-pace:N}}, {{run-hr:N}},
    {{bike-watts:N}}, {{bike-hr:N}} (N = 1-4, 5a, 5b, 5c), {{swim-pace:N}}
    (N = 1-5), plus {{bike-watts:ss}} (sweet spot, 88-94% of FTP) and
    {{run-pace:strides}} (stride pace, threshold pace -25 to -10 s/km).
    Prefer {{bike-ftp:75-77}} (or {{bike-ftp:75}}) over typed watts for bike
    targets: a whole-number % of FTP (30-200) that renders as "75–77% FTP
    (186–191 W)" from the plan's current FTP; add :w for watts only or :pct
    for percent only. Without an FTP it shows the percent. A malformed
    {{bike-ftp…}} token is rejected with an error.

    Args:
        operations: The edits, applied in order.
        reason: Short why, shown in the plan history (e.g. "Travel Thu–Fri").
        plan_id: Omit for the active plan; a future plan's id to amend it.

    Returns the new version, one message per change, the touched workout ids,
    and the recomputed summaries of the affected weeks.
    """
    return plan_tools.amend_training_plan(operations, reason, plan_id)


@mcp.tool()
def complete_plan_workout(
    workout_id: Optional[str] = None,
    activity_id: Optional[int] = None,
    activity_date: Optional[str] = None,
    sport: Optional[str] = None,
    notes: Optional[str] = None,
    completed: bool = True,
    plan_id: Optional[str] = None,
) -> dict:
    """
    Mark a planned workout complete (or not) — e.g. after post-workout
    analysis. Give workout_id directly, or give the Garmin activity_id and the
    planned workout is found by date and sport (from the synced activity, or
    from activity_date/sport when it isn't synced yet). If several workouts
    match, or none does, the error lists the candidates to pick a workout_id.

    Args:
        workout_id: Planned workout id (from get_training_plan).
        activity_id: Garmin activity that completed it (stored with the tick).
        activity_date: YYYY-MM-DD, when the activity isn't synced yet.
        sport: swim / bike / run / strength / brick, to narrow the match.
        notes: Optional short note stored with the completion.
        completed: False clears the tick.
        plan_id: Omit for the active plan.
    """
    return plan_tools.complete_plan_workout(workout_id, activity_id, activity_date, sport,
                                           notes, completed, plan_id)


@mcp.tool()
def skip_plan_workout(
    workout_id: str,
    skipped: bool = True,
    reason: Optional[str] = None,
    plan_id: Optional[str] = None,
) -> dict:
    """
    Mark a planned workout skipped — the athlete isn't doing it (sick,
    travelling, swapped for rest) — or un-skip it. Skipping clears any
    completion. Workouts whose day passes without being completed are
    skipped automatically (skippedAuto in get_training_plan); a late-synced
    activity still completes them. Un-skipping a past workout keeps it
    un-skipped; moving a skipped workout to today or later (amend_training_plan)
    puts it back on the schedule.

    Args:
        workout_id: Planned workout id (from get_training_plan).
        skipped: False un-skips it.
        reason: Optional short why (e.g. "Sick — head cold"), shown in the
            plan and calendar (skipReason). On an already-skipped workout
            (including one skipped automatically) it just records the why;
            "" clears it.
        plan_id: Omit for the active plan.
    """
    return plan_tools.skip_plan_workout(workout_id, skipped, reason, plan_id)


@mcp.tool()
def link_plan_workout_to_garmin(
    workout_id: str,
    garmin_workout_id: Optional[int] = None,
    scheduled_date: Optional[str] = None,
    plan_id: Optional[str] = None,
) -> dict:
    """
    Record the Garmin Connect workout scheduled for a planned workout (after
    create_workout / schedule_workout), so later reads of the plan show it's
    already on the Garmin calendar. Pass garmin_workout_id=None to clear it.

    Args:
        workout_id: Planned workout id.
        garmin_workout_id: The Garmin workout id that was scheduled.
        scheduled_date: YYYY-MM-DD it was scheduled on (default: the planned date).
        plan_id: Omit for the active plan.
    """
    return plan_tools.link_plan_workout_to_garmin(workout_id, garmin_workout_id, scheduled_date, plan_id)


@mcp.tool()
def set_training_plan_status(plan_id: str, status: str) -> dict:
    """
    Queue a stored plan as a future plan, or archive one — e.g. bring an
    archived block back as the next one, or shelve a future plan that's no
    longer wanted.

    A future plan stays editable (amend_training_plan with its plan_id) and
    becomes the active plan by itself on its start date. Which plan is
    active is the athlete's call: this tool can't activate a plan or change
    the active one — they do that at /training-plan/plans. New plans are
    uploaded there too (as future unless they choose to activate them).

    Returns the new status, the plan's dates, when it takes over
    (becomes_active_on) and any overlap or gap with the plans around it
    (timeline) — mention those to the athlete.

    Args:
        plan_id: The plan's id (from list_training_plans).
        status: "future" or "archived".
    """
    return plan_tools.set_training_plan_status(plan_id, status)


@mcp.tool()
def get_training_plan_revisions(plan_id: Optional[str] = None, limit: int = 20) -> list:
    """
    List a plan's revision history, newest first: version, source (upload /
    web / mcp), summary of the change, and when it happened.
    """
    return plan_tools.get_training_plan_revisions(plan_id, limit)


@mcp.tool()
def restore_training_plan_revision(version: int, plan_id: Optional[str] = None) -> dict:
    """
    Restore a past revision's workouts and zones as the current plan. The
    restore is itself a new revision, so it can be undone; completion ticks
    are never rolled back.
    """
    return plan_tools.restore_training_plan_revision(version, plan_id)


@mcp.tool()
def personal_records() -> dict:
    """
    Get personal records for running, cycling, and swimming.
    Results are grouped by sport category (running, cycling, swimming).
    Each record includes a human-readable label, formatted value,
    the activity it was set in, and the date it was achieved.
    Records for other activity types (yoga, wellness streaks, etc.) are excluded.
    """
    return get_personal_records()


@mcp.tool()
def active_goals(goal_type: str = "active") -> list:
    """
    Get the athlete's goals (step / distance / activity goals) with target,
    current progress, and progress percentage.

    Args:
        goal_type: Which goals to fetch — 'active' (current, default),
                   'future' (upcoming), or 'past' (completed/expired).
    """
    return get_active_goals(goal_type=goal_type)


@mcp.tool()
def earned_badges() -> list:
    """
    Get the athlete's earned challenge/achievement badges — name, points,
    category, date earned, and how many times each was earned.
    """
    return get_earned_badges()


@mcp.tool()
def adhoc_challenges(limit: int = 100) -> list:
    """
    Get the athlete's ad-hoc / community challenges — name, description,
    date range, personal ranking, and number of players.

    Note: current race time predictions are available via the separate
    performance_predictions tool.

    Args:
        limit: Maximum number of challenges to return (default 100).
    """
    return get_adhoc_challenges(limit=limit)


@mcp.tool()
def weekly_summary(week_offset: int = 0, sport_type: Optional[str] = None) -> dict:
    """
    Get an aggregated summary of activities for a Monday-to-Sunday week,
    with per-type breakdowns and a full activity list.
    Args:
        week_offset: 0 = current week, 1 = last week, 2 = two weeks ago, …
        sport_type:  Optional filter — 'running', 'road_biking', 'lap_swimming', etc.
    """
    return get_weekly_summary(week_offset=week_offset, sport_type=sport_type)


@mcp.tool()
def upload_weekly_summary(week_start_date: str, html_content: str) -> dict:
    """
    Publish a weekly training report so it can be read in the browser at
    /weekly-summary, alongside the dashboard.

    The report is a complete, self-contained styled HTML page (inline CSS/JS —
    external files are not hosted). It is stored as-is under the Monday of the
    training week it covers; previous/next-week navigation is added by the
    server when the page is served, so the HTML should not include its own.

    Uploading the same week again overwrites that week's report, so a report
    can be regenerated freely. Older weeks are kept indefinitely.

    Args:
        week_start_date: Monday of the training week, YYYY-MM-DD. Must be a
                         Monday — any other weekday is rejected.
        html_content:    The full HTML report.

    Returns:
        {"url": "/weekly-summary/YYYYMMDD", "week": "YYYY-MM-DD"} — append the
        usual ?token= to the URL to open it.
    """
    return save_weekly_summary(week_start_date=week_start_date,
                               html_content=html_content)


@mcp.tool()
def swim_records(months: int = 6, top_n: int = 5) -> dict:
    """
    Find the longest unbroken swim sets across recent swim activities — the
    key triathlon metric is continuous distance (a single unbroken set), not
    total session distance.

    Scans swim activities in the trailing window, treats each swim lap as one
    continuous set, ranks all sets by distance, and returns the top N with
    duration, per-100m pace, length count, average SWOLF, stroke, average HR,
    and the source activity (name, date, id).

    Default 6 months keeps API calls manageable (~10-20 swims). Use months=12
    for a full-year review or months=3 for a recent trend.

    Args:
        months: Look back this many months from today (default 6).
        top_n:  Number of longest sets to return (default 5).
    """
    return get_swim_records(months=months, top_n=top_n)


@mcp.tool()
def get_scheduled_workouts(months_ahead: int = 3) -> list:
    """
    Get upcoming scheduled running workouts from Garmin calendar.

    Args:
        months_ahead: Number of months to scan ahead, inclusive of current month.
    """
    return get_scheduled_workouts_impl(months_ahead=months_ahead)


@mcp.tool()
def get_saved_workouts(sport_type: Optional[str] = None) -> list:
    """
    Get saved workouts from Garmin workout library (summary metadata only —
    no step detail; use get_workout_detail for the full steps).

    Args:
        sport_type: Optional sport filter (e.g. running, cycling).
    """
    return get_saved_workouts_impl(sport_type=sport_type)


@mcp.tool()
def get_workout_detail(workout_id: int) -> dict:
    """
    Get full step-by-step detail for a saved workout by ID — warmup/interval/
    cooldown/rest and repeat-group steps with distance/duration, pace/power/HR
    targets, and strength exercise/weight fields, in the same shape
    create_workout's steps argument accepts.

    Args:
        workout_id: Garmin workout ID (from get_saved_workouts).
    """
    return get_workout_detail_impl(workout_id=workout_id)


@mcp.tool()
def schedule_workout(workout_id: int, date: str) -> dict:
    """
    Schedule an existing workout on a given date.

    Args:
        workout_id: Garmin workout ID.
        date: Date in YYYY-MM-DD format.
    """
    return schedule_workout_impl(workout_id=workout_id, date=date)


@mcp.tool()
def unschedule_workout(schedule_id: int) -> dict:
    """
    Remove a scheduled workout from calendar.

    Args:
        schedule_id: Scheduled workout ID from get_scheduled_workouts.
    """
    return unschedule_workout_impl(schedule_id=schedule_id)


@mcp.tool()
def create_workout(
    name: str,
    sport_type: Literal["running", "cycling", "strength_training"],
    steps: list,
    schedule_date: Optional[str] = None,
) -> dict:
    """
    Create a workout and optionally schedule it. Supports "running", "cycling",
    and "strength_training".

    For running/cycling: every interval/repeat step MUST carry a target —
    running takes EITHER a pace range OR an HR range (never both), cycling takes
    a power range. Untargeted "no effort" steps are only valid for
    warmup/cooldown/rest.

    For ANY repeated effort (e.g. "6 x 400m" or "5 x 3min"), use ONE "repeat"
    step with "sets" — do NOT emit the same interval step multiple times.

    For strength_training: each set carries a Garmin exercise_name (validated
    against the exercise reference — a typo fails with "did you mean ..."
    suggestions; use search_exercises to look names up), a load, and optional
    wave notation for ascending loads. Use "repeat" for working sets and
    "warmup" for warm-up sets (both take "sets"), and "rest" for standalone
    rest between exercises:

        {"type": "warmup", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT",
         "weight_lb": 50, "description": "50 - 50 - 90"}
        {"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT",
         "weight_lb": 115, "reps": 8, "rest_duration_s": 120,
         "description": "115 - 125 - 135 (6-8 reps)"}
        {"type": "rest"}                       # or {"type": "rest", "duration_s": 90}

    Strength step fields (anything else is rejected, never silently dropped):
      sets            number of sets (repeat/warmup/cooldown)
      reps            rep target per set -> the watch counts reps (int)
      duration_s      timed hold per set in seconds -> countdown + vibration
                      (reps and duration_s are mutually exclusive; neither = open
                      step, press lap when done)
      rest_duration_s timed rest after each set (omit = press lap to rest)
      weight_kg | weight_lb   load (pounds are converted to kg, rounded to 2 dp:
                      115 lb = 52.16 kg). Giving both is only OK if they agree.
      set_weights | set_weights_lb   per-set wave, e.g. [115, 125, 135]; the
                      description is synthesized from it. set_weights_lb also sets
                      the base load (first entry, lb->kg); legacy set_weights
                      takes the first entry as kg.
      description     free text shown on the watch (wave "115 - 125 - 135", or
                      notes such as "6-8 reps"). Garmin cannot use a rep RANGE as
                      a target: set reps to the upper bound (or omit it) and keep
                      the range in the description.
      category        derived from exercise_name (override only if needed)
    Omit the weight for bodyweight exercises (stored as -1).

    Strength examples:
        Timed hold, 6 sets of 30 s side plank with 15 s rest:
            {"type": "repeat", "sets": 6, "exercise_name": "SIDE_PLANK",
             "duration_s": 30, "rest_duration_s": 15}
        Reps, bodyweight:
            {"type": "repeat", "sets": 3, "exercise_name": "PUSH_UP", "reps": 10}
        Pounds, with wave:
            {"type": "repeat", "sets": 3, "exercise_name": "BARBELL_BACK_SQUAT",
             "set_weights_lb": [115, 125, 135], "reps": 8}

    After upload the workout is re-fetched and compared with what was sent. The
    result includes "warnings" (anything Garmin did not store: timer, reps,
    weight, description, pace/HR/power target) and "verified". If warnings is
    non-empty, tell the user. "notes" flags exercises with no Garmin Connect page
    (they still upload) and suggests alternatives such as SIDE_PLANK for
    WEIGHTED_SIDE_PLANK.

    steps is a list of dicts, each with a "type" field (running/cycling shapes):

        warmup / cooldown / recovery / rest (target optional):
            {"type": "warmup"|"cooldown"|"recovery"|"rest",
             "description": str | None,
             "distance_m": float,   # optional end condition — omit for lap-button end
             "duration_s": int,     # optional end condition — omit for lap-button end
             "pace_min_per_km": float, "pace_max_per_km": float,   # optional, running
             "hr_min": int, "hr_max": int,                        # optional, running
             "power_watts_min": int, "power_watts_max": int}      # optional, cycling

        interval (standalone single effort — target REQUIRED):
            {"type": "interval",
             "distance_m": float,   # exactly one of distance_m/duration_s
             "duration_s": int,
             "pace_min_per_km": float, "pace_max_per_km": float,  # running: pace ...
             "hr_min": int, "hr_max": int,                        # ... or HR
             "power_watts_min": int, "power_watts_max": int,      # cycling: power
             "description": str | None}

        repeat (a block of N identical efforts — target REQUIRED):
            {"type": "repeat", "sets": int,
             "distance_m": float,   # exactly one of distance_m/duration_s — per rep
             "duration_s": int,
             "rest_duration_s": int,  # optional recovery between reps — omit for lap-button rest
             "pace_min_per_km": float, "pace_max_per_km": float,  # running: pace ...
             "hr_min": int, "hr_max": int,                        # ... or HR
             "power_watts_min": int, "power_watts_max": int,      # cycling: power
             "description": str | None}

    Examples:
        Running, pace:  3 x [400m @ 4:00-3:50/km, 90s recovery]
            {"type": "repeat", "sets": 3, "distance_m": 400, "rest_duration_s": 90,
             "pace_min_per_km": "4:00", "pace_max_per_km": "3:50"}
        Running, HR:  5 x [3min @ 155-170bpm, 2min recovery]
            {"type": "repeat", "sets": 5, "duration_s": 180, "rest_duration_s": 120,
             "hr_min": 155, "hr_max": 170}
        Cycling, power:  4 x [4min @ 264-288W, 3min recovery]
            {"type": "repeat", "sets": 4, "duration_s": 240, "rest_duration_s": 180,
             "power_watts_min": 264, "power_watts_max": 288}

    Args:
        name: Workout name.
        sport_type: "running", "cycling", or "strength_training".
        steps: List of workout step dicts as described above.
        schedule_date: Optional schedule date in YYYY-MM-DD format.
    """
    return create_workout_impl(name=name, sport_type=sport_type, steps=steps, schedule_date=schedule_date)


@mcp.tool()
def delete_workout(workout_id: int) -> dict:
    """
    Delete a saved workout by ID.

    Args:
        workout_id: Garmin workout ID to delete.
    """
    return delete_workout_impl(workout_id=workout_id)


@mcp.tool()
def update_workout_weights(
    workout_name: str,
    weight_updates: dict,
    workout_description: str | None = None,
    include_warmup: bool = False,
    warmup_updates: dict | None = None,
) -> dict:
    """
    Update exercise weights (and optionally per-set notes) in a strength
    workout by name.

    Updates the workout IN PLACE: its Garmin ID does not change, so calendar
    schedule entries and plan links (link_plan_workout_to_garmin) stay valid.
    Finds the workout, updates weightValue and/or the per-exercise
    description text for matching exercises (working steps only by default —
    see include_warmup / warmup_updates), optionally replaces the
    workout-level description, then re-fetches it and reports in "warnings"
    anything that was not stored. The result still has old_id/new_id (equal now).

    Args:
        workout_name: Exact workout name as it appears in Garmin Connect.
        weight_updates: Mapping of exerciseName to either a plain number for
            a weight-only update (backward compatible), e.g.
            {"OVERHEAD_BARBELL_PRESS": 32.5}, or a dict with optional
            "weight_kg" and/or "description" keys, e.g.
            {"BARBELL_BACK_SQUAT": {"weight_lb": 125,
                                     "description": "115 - 125 - 135"}}.
            Keys: "weight_kg" or "weight_lb", "description", or a wave via
            "set_weights"/"set_weights_lb". Omitting a key leaves that field
            unchanged (no accidental blanking).
        workout_description: Optional new workout-level description (e.g.
            "Next weight progression day: July 30"). Omit to leave it
            unchanged.
        include_warmup: Also apply weight_updates to warm-up steps (default
            False: warm-ups are untouched).
        warmup_updates: Same shape as weight_updates but applied only to
            warm-up steps, e.g. {"BARBELL_BACK_SQUAT": {"weight_lb": 55}}.
    """
    return update_workout_weights_impl(
        workout_name=workout_name,
        weight_updates=weight_updates,
        workout_description=workout_description,
        include_warmup=include_warmup,
        warmup_updates=warmup_updates,
    )


@mcp.tool()
def patch_workout_step(
    workout_id: int,
    exercise_name: str,
    fields: dict,
    occurrence: int = 1,
    step_type: Optional[str] = None,
) -> dict:
    """
    Change ONE step of a saved strength workout in place (same Garmin ID, same
    calendar entries) without rebuilding the workout.

    The step is picked by exercise_name + occurrence (1-based, in workout
    order; a warm-up block of the same exercise counts as an occurrence, or pass
    step_type="warmup"/"repeat"/"cooldown" to count only that kind).

    fields (any of): sets, reps, duration_s (hold, seconds), rest_duration_s,
    weight_kg | weight_lb, set_weights | set_weights_lb (per-set wave), description.
    None for reps/duration_s makes the step open (press lap); None for
    rest_duration_s makes rest open; None weight = bodyweight. A rep target and a
    timer replace each other. After saving, the workout is re-fetched and
    "warnings" lists anything not stored.

    Examples:
        Make the side plank a 40 s hold:
            workout_id=..., exercise_name="SIDE_PLANK", fields={"duration_s": 40}
        Working-set squat to 8 reps at 135 lb (warm-up is occurrence 1):
            exercise_name="BARBELL_BACK_SQUAT", occurrence=2,
            fields={"reps": 8, "weight_lb": 135}   # or step_type="repeat"

    Returns the matched step, "changes" ({field: {old, new}}), the decoded step,
    "warnings" and "verified".

    Args:
        workout_id: Garmin workout ID (from get_saved_workouts).
        exercise_name: Garmin exercise name (see search_exercises).
        fields: What to change (see above).
        occurrence: Which matching step, 1-based.
        step_type: Optional filter: "warmup", "repeat" or "cooldown".
    """
    return patch_workout_step_impl(
        workout_id=workout_id, exercise_name=exercise_name,
        occurrence=occurrence, fields=fields, step_type=step_type,
    )


@mcp.tool()
def search_exercises(
    query: str = "",
    category: Optional[str] = None,
    equipment: Optional[str] = None,
    muscle: Optional[str] = None,
    found_only: bool = False,
    limit: int = 25,
) -> list:
    """
    Search Garmin's strength exercise database for the exact exercise_name
    (and category) to use in create_workout / patch_workout_step.

    Each result has exercise_name, category, display_name,
    found_on_garmin_connect, difficulty, muscles (0-2 scores), equipment, url.
    found_on_garmin_connect=False means Garmin Connect has no page for it; it
    usually still uploads, but a plain variant is often better (SIDE_PLANK rather
    than WEIGHTED_SIDE_PLANK). Some names exist under several categories
    (e.g. PUSH_UP in PUSH_UP and SUSPENSION).

    Args:
        query: Words to match in the name, e.g. "side plank" or "romanian deadlift".
            Typos fall back to fuzzy matching.
        category: Garmin category filter, e.g. "PLANK", "SQUAT".
        equipment: e.g. "BARBELL", "DUMBBELL", "KETTLEBELL", "BENCH".
        muscle: e.g. "GLUTES", "CHEST", "LATS" (requires a score >= 1).
        found_only: Only exercises with a Garmin Connect page.
        limit: Max results (default 25).
    """
    return search_exercises_impl(query=query, category=category, equipment=equipment,
                                 muscle=muscle, found_only=found_only, limit=limit)


@mcp.tool()
def strength_sets(activity_id: int, workout_id: Optional[int] = None,
                  compare_to_workout: bool = False) -> dict:
    """
    What was actually lifted in a strength activity, set by set: exercise
    (category + name), reps, weight (kg and lb), duration, set type
    ("warm-up" | "active" | "rest") and order, plus a per-exercise summary
    (sets, reps, top weight, volume).

    Garmin records warm-ups as ordinary active sets; pass workout_id (the saved
    workout the session followed) so they can be labelled "warm-up".

    With compare_to_workout=True (requires workout_id) the result is a
    planned-vs-actual comparison instead: for every performed set the planned
    reps/time/load (waves honoured) and flags where the load, reps, time or
    exercise differ, plus planned sets that were never performed ("missed").

    Args:
        activity_id: Garmin activity ID of a strength session.
        workout_id: Saved workout the session followed (optional).
        compare_to_workout: Return planned-vs-actual instead.
    """
    if compare_to_workout:
        if workout_id is None:
            raise ValueError("compare_to_workout requires workout_id")
        return compare_strength_session_impl(activity_id, workout_id)
    return strength_sets_impl(activity_id, workout_id)


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

def _week_offset(query: dict) -> int:
    """The dashboard's ?week= (issue 85): 0 = current week, 1 = last week,
    etc. Clamped to >=0 — there's no data for future weeks, so a
    negative/garbage value just falls back to the current week."""
    try:
        return max(0, int(query.get("week", ["0"])[0]))
    except ValueError:
        return 0


def _accepts_gzip(scope) -> bool:
    for name, value in scope.get("headers", []):
        if name == b"accept-encoding":
            return b"gzip" in value.lower()
    return False


async def _gzip_flushing(chunks):
    """Gzip a streamed response, flushing after every chunk.

    Starlette's GZipMiddleware holds streamed output in its compressor until
    the response ends, which would keep the dashboard's loading skeleton from
    reaching the browser until the whole page was ready. A response that
    already has a Content-Encoding is passed through by that middleware.
    """
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)   # wbits=31 → gzip framing
    async for chunk in chunks:
        yield compressor.compress(chunk.encode()) + compressor.flush(zlib.Z_SYNC_FLUSH)
    yield compressor.flush()


def build_asgi_app():
    """The full HTTP surface: bearer-token auth in front of the MCP app plus
    the server-rendered HTML routes (/dashboard, /training-plan,
    /weekly-summary)."""
    from urllib.parse import parse_qs

    from starlette.responses import Response as StarletteResponse
    from starlette.responses import FileResponse, HTMLResponse, JSONResponse

    from starlette.concurrency import run_in_threadpool
    from starlette.responses import StreamingResponse

    from tools.dashboard import (get_activity_week_data, render_activity_calendar, render_activity_panel,
                                 stream_dashboard)
    from tools import training_plan
    from tools import plan_calendar
    from tools import weekly_summaries
    from tools import gear_tracker
    from tools import activity_detail
    from tools import recovery
    import db

    db.ensure_schema()

    app = mcp.http_app()
    training_plan_app = training_plan.create_app()
    weekly_summary_app = weekly_summaries.create_app()
    gear_tracker_app = gear_tracker.create_app()
    activity_detail_app = activity_detail.create_app()

    # Wrap the app with a simple ASGI auth wrapper
    bearer = BEARER_TOKEN

    # App icons are public: iOS fetches the home-screen icon (and browsers the
    # favicon) without the ?token= query, so these skip the bearer check.
    icons_dir = os.path.join(os.path.dirname(__file__), "static", "icons")
    icon_files = {
        "/favicon.ico": ("favicon.ico", "image/x-icon"),
        "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
        "/favicon-dark.ico": ("favicon-dark.ico", "image/x-icon"),
        "/icons/favicon.svg": ("favicon.svg", "image/svg+xml"),
        "/icons/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
        "/icons/icon-192.png": ("icon-192.png", "image/png"),
        "/icons/icon-512.png": ("icon-512.png", "image/png"),
    }

    async def auth_app(scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in icon_files:
            filename, media_type = icon_files[scope["path"]]
            response = FileResponse(os.path.join(icons_dir, filename), media_type=media_type,
                                    headers={"Cache-Control": "public, max-age=86400"})
            await response(scope, receive, send)
            return

        # The calendar feed is fetched by calendar apps and their servers, so
        # it takes its own read-only ?key= (checked by the route) instead of
        # the bearer token, which must never end up stored in a calendar.
        if scope["type"] == "http" and scope.get("path") == plan_calendar.FEED_PATH:
            await training_plan_app(scope, receive, send)
            return

        if bearer and scope["type"] == "http":
            query_string = scope.get("query_string", b"").decode()
            params = dict(p.split("=") for p in query_string.split("&") if "=" in p)
            token = params.get("token", "")
            if not secrets.compare_digest(token, bearer):
                response = StarletteResponse("Unauthorized", status_code=401)
                await response(scope, receive, send)
                return

        if scope["type"] == "http" and scope.get("path") == "/manifest.webmanifest":
            query = parse_qs(scope.get("query_string", b"").decode())
            token = query.get("token", [None])[0]
            start_url = "/dashboard" + (f"?token={token}" if token else "")
            response = JSONResponse({
                "name": "Ridgeline",
                "short_name": "Ridgeline",
                "start_url": start_url,
                "scope": "/",
                "display": "standalone",
                "orientation": "any",
                "background_color": "#161826",
                "theme_color": "#161826",
                "description": "A health dashboard and MCP server that turns Garmin Connect into something Claude can read.",
                "icons": [
                    {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png",
                     "purpose": "maskable"},
                ],
            })
            await response(scope, receive, send)
            return

        if scope["type"] == "http" and scope.get("path") == "/sw.js":
            response = FileResponse(os.path.join(os.path.dirname(__file__), "sw.js"),
                                    media_type="application/javascript")
            await response(scope, receive, send)
            return

        # Server-rendered health dashboard — same container, same bearer-token
        # auth (via ?token=), just a different route. Data is fetched live on
        # each request.
        if scope["type"] == "http" and scope.get("path") == "/dashboard":
            # The token is threaded into the nav links so navigating to the
            # training plan or the weekly reports keeps the ?token= auth.
            # ?tab= opens a specific tab (e.g. "gear" — used by gear-tracker
            # form submissions redirecting back here) instead of Today, and
            # ?filter= the Activity tab's sport filter.
            query = parse_qs(scope.get("query_string", b"").decode())
            token = query.get("token", [None])[0]
            initial_tab = query.get("tab", [None])[0]
            error = query.get("error", [None])[0]
            activity_filter = query.get("filter", [None])[0]
            week_offset = _week_offset(query)
            # Streamed: the head and a loading skeleton go out at once, the
            # rest once the data is in (tools.dashboard.stream_dashboard).
            chunks = stream_dashboard(week_offset, token, initial_tab, error, activity_filter)
            headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            if _accepts_gzip(scope):
                chunks = _gzip_flushing(chunks)
                headers["Content-Encoding"] = "gzip"
                headers["Vary"] = "Accept-Encoding"
            response = StreamingResponse(chunks, media_type="text/html; charset=utf-8", headers=headers)
            await response(scope, receive, send)
            return

        # Recovery detail page — opened from the Today tab's In Focus Recovery
        # card; ?date= picks the day (default today).
        if scope["type"] == "http" and scope.get("path") == recovery.PATH:
            query = parse_qs(scope.get("query_string", b"").decode())
            markup = await run_in_threadpool(recovery.render_recovery_page,
                                             query.get("date", [None])[0], query.get("token", [None])[0])
            response = HTMLResponse(markup, headers={"Cache-Control": "no-cache"})
            await response(scope, receive, send)
            return

        # One week of the dashboard's Activity tab, as just its panel — the
        # tab swaps this in to change week without reloading the page.
        if scope["type"] == "http" and scope.get("path") == "/dashboard/activity-week":
            query = parse_qs(scope.get("query_string", b"").decode())
            token = query.get("token", [None])[0]
            try:
                data = await run_in_threadpool(get_activity_week_data, _week_offset(query))
                response = HTMLResponse(render_activity_panel(data, token),
                                        headers={"Cache-Control": "no-store"})
            except Exception as e:  # pragma: no cover — defensive
                logger.exception("Activity week render failed")
                response = HTMLResponse(f"Activity week error: {e}", status_code=500)
            await response(scope, receive, send)
            return

        # The Activity tab's calendar: one month, as the modal's content.
        if scope["type"] == "http" and scope.get("path") == "/dashboard/activity-calendar":
            query = parse_qs(scope.get("query_string", b"").decode())
            try:
                markup = await run_in_threadpool(
                    render_activity_calendar, query.get("month", [None])[0],
                    query.get("filter", [None])[0], _week_offset(query))
                response = HTMLResponse(markup, headers={"Cache-Control": "no-store"})
            except Exception as e:  # pragma: no cover — defensive
                logger.exception("Activity calendar render failed")
                response = HTMLResponse(f"Activity calendar error: {e}", status_code=500)
            await response(scope, receive, send)
            return

        # Gear tracker API (issue 53) — the page itself is the Gear tab above
        # (/dashboard?tab=gear); this only serves the JSON/form API those
        # forms post to, plus a redirect for the old standalone page URL.
        if scope["type"] == "http" and gear_tracker.owns_path(scope.get("path", "")):
            await gear_tracker_app(scope, receive, send)
            return

        # Activity-detail modal (issue 74) — fetched by the dashboard's
        # activity rows (Today and Activities tabs) and dropped into the
        # persistent #activity-modal container; reads activity_details from
        # PostgreSQL, no live Garmin call per open.
        if scope["type"] == "http" and activity_detail.owns_path(scope.get("path", "")):
            await activity_detail_app(scope, receive, send)
            return

        # Training-plan viewer — the Claude Coach plan stored in PostgreSQL,
        # served inside the viewer template, same bearer-token auth.
        if scope["type"] == "http" and training_plan.owns_path(scope.get("path", "")):
            await training_plan_app(scope, receive, send)
            return

        # Weekly training reports — uploaded via the upload_weekly_summary MCP
        # tool, served from the same file share with server-injected navigation.
        if scope["type"] == "http" and weekly_summaries.owns_path(scope.get("path", "")):
            await weekly_summary_app(scope, receive, send)
            return

        await app(scope, receive, send)

    from starlette.middleware.gzip import GZipMiddleware
    return GZipMiddleware(auth_app, minimum_size=500)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Ridgeline server on port {port}")
    uvicorn.run(build_asgi_app(), host="0.0.0.0", port=port)
