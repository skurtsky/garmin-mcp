# server.py
import os
import logging
from fastmcp import FastMCP
from dotenv import load_dotenv

from tools.profile import get_athlete_profile
from tools.activities import get_activities, get_activity, get_weekly_summary
from tools.health import get_sleep, get_daily_readiness
from tools.trends import get_performance_predictions, get_performance_trends

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

# class TokenAuthMiddleware:
#     def __init__(self, app: ASGIApp):
#         self.app = app

#     async def __call__(self, scope, receive, send):
#         if scope["type"] in ("http", "websocket"):
#             query_string = scope.get("query_string", b"").decode()
#             params = dict(p.split("=") for p in query_string.split("&") if "=" in p)
#             token = params.get("token", "")
#             if BEARER_TOKEN and not secrets.compare_digest(token, BEARER_TOKEN):
#                 from starlette.responses import Response
#                 response = Response("Unauthorized", status_code=401)
#                 await response(scope, receive, send)
#                 return
#         await self.app(scope, receive, send)


# ── TOOLS ─────────────────────────────────────────────────────────────────────

from typing import Optional

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
    per-lap splits with running dynamics, and HR time-in-zones.

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
    Get daily readiness metrics for a given date including HRV, body battery
    (with start-of-day level), training load balance, ACWR, and VO2max trends.

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return get_daily_readiness(date)

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
def weekly_summary(week_offset: int = 0, sport_type: Optional[str] = None) -> dict:
    """
    Get an aggregated summary of activities for a Monday-to-Sunday week,
    with per-type breakdowns and a full activity list.
    Args:
        week_offset: 0 = current week, 1 = last week, 2 = two weeks ago, …
        sport_type:  Optional filter — 'running', 'road_biking', 'lap_swimming', etc.
    """
    return get_weekly_summary(week_offset=week_offset, sport_type=sport_type)


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = mcp.http_app()

    import uvicorn
    from starlette.routing import Route
    from starlette.responses import Response as StarletteResponse

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
        await app(scope, receive, send)

    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Garmin MCP server on port {port}")
    uvicorn.run(auth_app, host="0.0.0.0", port=port)

