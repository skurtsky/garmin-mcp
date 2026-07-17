# server.py
import os
import logging
from fastmcp import FastMCP
from dotenv import load_dotenv

from tools.profile import get_athlete_profile, get_gear
from tools.activities import (
    get_activities,
    get_activity,
    get_weekly_summary,
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
    get_trends as get_trends_impl,
)
from tools.performance import (
    get_endurance_score,
    get_running_tolerance,
    get_personal_records,
)
from tools.workout import (
    get_scheduled_workouts as get_scheduled_workouts_impl,
    get_saved_workouts as get_saved_workouts_impl,
    get_workout_detail as get_workout_detail_impl,
    schedule_workout as schedule_workout_impl,
    unschedule_workout as unschedule_workout_impl,
    create_workout as create_workout_impl,
    delete_workout as delete_workout_impl,
    update_workout_weights as update_workout_weights_impl,
)

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
def activity_detail(activity_id: int) -> dict:
    """
    Get full detail for a single activity including summary metrics,
    per-lap splits with running dynamics, structured-workout interval/phase
    breakdown (warmup/active/recovery/rest/cooldown — useful for pace-by-rep
    or power-by-rep analysis of interval workouts, empty for non-workout
    activities), and HR time-in-zones.

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

    Args:
        date: Date in YYYY-MM-DD format. Use today's date for last night's sleep.
    """
    return get_sleep(date)


@mcp.tool()
def daily_readiness(date: str) -> dict:
    """
    Get recovery-focused daily readiness for a given date — HRV, body
    battery (start/current/highest/lowest levels and sleep gain), and
    daily activity & stress stats (RHR, 7-day RHR average, average and
    max stress, steps, active seconds).

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return get_daily_readiness(date)


@mcp.tool()
def daily_health(date: str = 'today') -> dict:
    """
    Get a daily health snapshot for a given date — resting/max/min heart
    rate, all-day stress zones (avg/max stress level and time-in-zone
    minutes), body battery charged/drained, and respiration rate
    (waking/sleep averages and range).

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return get_daily_health(date)


@mcp.tool()
def training_status(date: str) -> dict:
    """
    Get training status for a given date — acute:chronic workload ratio
    (ACWR) and status, training load balance phrase, training status
    feedback phrase and sport, and current VO2max for running and cycling.

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return get_training_status(date)

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

    Args:
        period:  One of 7d, 14d, 1m, 42d, 3m, 6m, 1y (default 1m).
        metrics: Optional list of metric names to include (defaults to all).
    """
    return get_trends_impl(period=period, metrics=metrics)

@mcp.tool()
def training_readiness(date: str = 'today') -> dict:
    """
    Get training readiness for a given date — a composite score (0–100)
    indicating whether to train hard today, plus level, feedback phrases,
    contributing factors (sleep, recovery, ACWR, stress, HRV), and the
    morning readiness snapshot.

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return get_training_readiness(date)


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
    sport_type: Literal["running", "cycling"],
    steps: list,
    schedule_date: Optional[str] = None,
) -> dict:
    """
    Create a workout and optionally schedule it. Only "running" and "cycling"
    are supported.

    Every interval/repeat step MUST carry a target — running takes EITHER a
    pace range OR an HR range (never both), cycling takes a power range.
    Untargeted "no effort" steps are only valid for warmup/cooldown/rest.

    For ANY repeated effort (e.g. "6 x 400m" or "5 x 3min"), use ONE "repeat"
    step with "sets" — do NOT emit the same interval step multiple times.

    steps is a list of dicts, each with a "type" field:

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
        sport_type: "running" or "cycling".
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
) -> dict:
    """
    Update exercise weights (and optionally per-set notes) in a strength
    workout by name.

    Finds the workout, updates weightValue and/or the per-exercise
    description text for matching exercises (interval steps only — warmup
    steps are not changed), optionally replaces the workout-level
    description, uploads as a new workout, and deletes the old one.

    Args:
        workout_name: Exact workout name as it appears in Garmin Connect.
        weight_updates: Mapping of exerciseName to either a plain number for
            a weight-only update (backward compatible), e.g.
            {"OVERHEAD_BARBELL_PRESS": 32.5}, or a dict with optional
            "weight_kg" and/or "description" keys, e.g.
            {"BARBELL_BACK_SQUAT": {"weight_kg": 56.7,
                                     "description": "125 - 145 - 155"}}.
            Omitting "weight_kg" or "description" leaves that field
            unchanged (no accidental blanking).
        workout_description: Optional new workout-level description (e.g.
            "Next weight progression day: July 30"). Omit to leave it
            unchanged.
    """
    return update_workout_weights_impl(
        workout_name=workout_name,
        weight_updates=weight_updates,
        workout_description=workout_description,
    )


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = mcp.http_app()

    import uvicorn
    from starlette.routing import Route
    from starlette.responses import Response as StarletteResponse
    from starlette.responses import HTMLResponse

    from tools.dashboard import build_dashboard_data, render_dashboard_html

    # Wrap the app with a simple ASGI auth wrapper
    bearer = BEARER_TOKEN

    async def auth_app(scope, receive, send):
        if bearer and scope["type"] == "http":
            query_string = scope.get("query_string", b"").decode()
            params = dict(p.split("=") for p in query_string.split("&") if "=" in p)
            token = params.get("token", "")
            if not secrets.compare_digest(token, bearer):
                response = StarletteResponse("Unauthorized", status_code=401)
                await response(scope, receive, send)
                return

        # Server-rendered health dashboard — same container, same bearer-token
        # auth (via ?token=), just a different route. Data is fetched live on
        # each request.
        if scope["type"] == "http" and scope.get("path") == "/dashboard":
            try:
                page = render_dashboard_html(build_dashboard_data())
                response = HTMLResponse(page)
            except Exception as e:  # pragma: no cover — defensive
                logger.exception("Dashboard render failed")
                response = HTMLResponse(f"Dashboard error: {e}", status_code=500)
            await response(scope, receive, send)
            return

        await app(scope, receive, send)

    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Garmin MCP server on port {port}")
    uvicorn.run(auth_app, host="0.0.0.0", port=port)
