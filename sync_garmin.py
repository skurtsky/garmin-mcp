"""Scheduled Garmin → PostgreSQL sync.

Fetches new/updated data from Garmin Connect and upserts it into PostgreSQL.
Designed to run as a Container Apps Job on a schedule (every 5–15 minutes).
"""
import logging
import sys
from argparse import ArgumentParser, ArgumentTypeError
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ArgumentTypeError("expected date in YYYY-MM-DD format") from exc


def _date_range(start_date: date, end_date: date) -> list[date]:
    if start_date > end_date:
        raise ValueError("start date must be on or before end date")
    days = (end_date - start_date).days + 1
    return [start_date + timedelta(days=offset) for offset in range(days)]


def _daily_metric_dates(days: int, start_date: date | None,
                        end_date: date | None) -> list[date]:
    today = date.today()
    if start_date or end_date:
        end = end_date or today
        start = start_date or end
        return _date_range(start, end)
    if days < 1:
        raise ValueError("days must be at least 1")
    return [today - timedelta(days=offset) for offset in range(days)]


def sync_daily_metrics(days: int = 3, start_date: date | None = None,
                       end_date: date | None = None):
    """Sync daily health metrics for a rolling window or explicit date range."""
    from tools.health import (
        get_sleep, get_daily_health, get_daily_readiness,
        get_training_readiness, get_training_status,
    )
    import db

    dates = _daily_metric_dates(days, start_date, end_date)
    logger.info(
        "Syncing daily metrics for %s day(s), %s through %s",
        len(dates), dates[0].isoformat(), dates[-1].isoformat(),
    )

    for current_date in dates:
        d = current_date.isoformat()
        logger.info(f"Syncing daily metrics for {d}")

        sleep_data = _safe_fetch(get_sleep, d)
        health_data = _safe_fetch(get_daily_health, d)
        readiness_data = _safe_fetch(get_daily_readiness, d)
        training_data = _safe_fetch(get_training_readiness, d)
        training_status_data = _safe_fetch(get_training_status, d)

        rhr = (health_data or {}).get("heart_rate", {}).get("resting_hr")
        hrv = (sleep_data or {}).get("avg_hrv")
        sleep_score = (sleep_data or {}).get("sleep_score")
        stress = (health_data or {}).get("stress", {}).get("avg_stress")
        steps = ((readiness_data or {}).get("daily_stats") or {}).get("total_steps")
        training_load = (
            ((training_data or {}).get("readiness") or {}).get("acute_load")
        )

        db.upsert_daily_metric(
            d, rhr=rhr, hrv=hrv, sleep_score=sleep_score,
            stress=stress, steps=steps, training_load=training_load,
            sleep_data=sleep_data, health_data=health_data,
            readiness_data=readiness_data, training_data=training_data,
            training_status_data=training_status_data,
        )

    db.update_sync_state("daily_metrics", max(dates).isoformat())
    logger.info("Daily metrics sync complete")


def sync_activities(start_date: date | None = None, end_date: date | None = None):
    """Sync activities: the most recent 20 by default, or every activity in
    an explicit date range when start_date is given (Garmin's date-range
    endpoint returns everything in range regardless of a limit)."""
    from tools.activities import get_activities
    import db

    if start_date:
        end = end_date or date.today()
        logger.info(f"Syncing activities from {start_date.isoformat()} to {end.isoformat()}")
        activities = get_activities(start_date=start_date.isoformat(), end_date=end.isoformat())
    else:
        logger.info("Syncing activities")
        activities = get_activities(limit=20)
    for act in activities:
        db.upsert_activity(
            garmin_id=act["id"],
            activity_date=act.get("date"),
            activity_type=act.get("type"),
            name=act.get("name"),
            distance_km=act.get("distance_km"),
            duration_min=act.get("duration_min"),
            avg_hr=act.get("avg_hr"),
            training_load=act.get("training_load"),
            summary=act,
        )
    db.update_sync_state("activities", date.today().isoformat())
    logger.info(f"Synced {len(activities)} activities")


def sync_activity_details(limit: int = 10, overwrite: bool = False):
    """Sync activity_details (detail + route JSONB) for activities missing it
    or gone stale relative to their parent activity row.

    Each activity costs several Garmin API calls (splits, weather, the raw
    detail/polyline endpoint), so this is capped per run rather than backfilling
    everything at once — a fresh install catches up gradually across runs.

    ``overwrite`` re-fetches the most recent ``limit`` activities regardless
    of whether they already have a detail row — get_activity_ids_needing_detail
    only catches a *missing* or *stale* row, so it never revisits an activity
    whose detail was already synced under an older code path that extracted
    fewer fields (e.g. a schema change adding hr_zones or sub_activities to
    activity_details.detail). Use this for a one-time backfill after such a
    change: `python sync_garmin.py --details-only --detail-limit 999 --overwrite`.
    """
    from tools.activities import get_activity_detail_row
    import db

    activity_ids = (
        db.get_recent_activity_ids(limit=limit) if overwrite
        else db.get_activity_ids_needing_detail(limit=limit)
    )
    logger.info(f"Syncing detail for {len(activity_ids)} activity(ies)")
    for activity_id in activity_ids:
        try:
            detail, route = get_activity_detail_row(activity_id)
        except Exception:
            logger.exception(f"Failed to build detail for activity {activity_id}")
            continue
        db.upsert_activity_detail(activity_id, detail, route)
    db.update_sync_state("activity_details", date.today().isoformat())
    logger.info("Activity detail sync complete")


def sync_personal_records():
    """Sync personal records."""
    from tools.performance import get_personal_records
    import db

    logger.info("Syncing personal records")
    records = get_personal_records()
    db.upsert_personal_records(records)
    db.update_sync_state("personal_records", date.today().isoformat())
    logger.info("Personal records sync complete")


def sync_athlete_profile():
    """Sync athlete profile."""
    from tools.profile import get_athlete_profile
    import db

    logger.info("Syncing athlete profile")
    profile = get_athlete_profile()
    if profile:
        db.upsert_athlete_profile(profile)
    db.update_sync_state("athlete_profile", date.today().isoformat())
    logger.info("Athlete profile sync complete")


def sync_active_goals():
    """Sync active goals."""
    from tools.challenges import get_active_goals
    import db

    logger.info("Syncing active goals")
    goals = get_active_goals()
    if goals:
        db.upsert_active_goals(goals)
    db.update_sync_state("active_goals", date.today().isoformat())
    logger.info("Active goals sync complete")


def _safe_fetch(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f"{fn.__name__}({args}) failed: {e}")
        return None


def parse_args(argv: list[str] | None = None):
    parser = ArgumentParser(description="Sync Garmin data into PostgreSQL")
    parser.add_argument(
        "--days", type=int, default=3,
        help="number of recent days of daily metrics to sync when no date range is provided",
    )
    parser.add_argument(
        "--since", "--start-date", dest="start_date", type=_parse_date,
        help="first daily metrics date to sync, inclusive (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date", type=_parse_date,
        help="last daily metrics date to sync, inclusive (defaults to today)",
    )
    parser.add_argument(
        "--daily-only", action="store_true",
        help="only sync daily metrics; useful for historical trend backfills",
    )
    parser.add_argument(
        "--activities-since", "--activities-start-date", dest="activities_start_date",
        type=_parse_date,
        help="sync every activity from this date onward (inclusive), instead "
             "of just the most recent 20 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--activities-end-date", type=_parse_date,
        help="last activity date to sync, inclusive, when --activities-since "
             "is given (defaults to today)",
    )
    parser.add_argument(
        "--activities-only", action="store_true",
        help="only sync activities; combine with --activities-since for a "
             "one-time activity history backfill",
    )
    parser.add_argument(
        "--detail-limit", type=int, default=10,
        help="max number of activities to sync detail/route JSONB for per run",
    )
    parser.add_argument(
        "--details-only", action="store_true",
        help="only sync activity_details (detail/route JSONB); useful for a "
             "one-time backfill, e.g. --details-only --detail-limit 999",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="force-refresh activity_details for the most recent "
             "--detail-limit activities even if their detail row is already "
             "current — use after a schema change adds fields older detail "
             "rows don't have yet, e.g. "
             "--details-only --detail-limit 999 --overwrite",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    args = parse_args(argv)

    import db

    logger.info("Starting Garmin sync job")
    db.ensure_schema()

    sync_activities_step = lambda: sync_activities(
        start_date=args.activities_start_date,
        end_date=args.activities_end_date,
    )

    errors = []
    if args.details_only:
        sync_plan = [lambda: sync_activity_details(limit=args.detail_limit, overwrite=args.overwrite)]
    elif args.activities_only:
        sync_plan = [sync_activities_step]
    else:
        sync_plan = [
            lambda: sync_daily_metrics(
                days=args.days,
                start_date=args.start_date,
                end_date=args.end_date,
            )
        ]
        if not args.daily_only:
            sync_plan.extend([
                sync_activities_step,
                lambda: sync_activity_details(limit=args.detail_limit, overwrite=args.overwrite),
                sync_personal_records,
                sync_athlete_profile,
                sync_active_goals,
            ])

    for sync_fn in sync_plan:
        try:
            sync_fn()
        except Exception as e:
            logger.exception(f"{getattr(sync_fn, '__name__', 'sync_daily_metrics')} failed")
            errors.append(str(e))

    if errors:
        logger.error(f"Sync job completed with {len(errors)} error(s)")
        sys.exit(1)
    else:
        logger.info("Sync job completed successfully")


if __name__ == "__main__":
    main()
