# server.py
import os
import logging
from fastmcp import FastMCP
from dotenv import load_dotenv

from tools.profile import get_athlete_profile
from tools.activities import get_activities, get_activity
from tools.health import get_sleep, get_daily_readiness

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
def recent_activities(limit: int = 10, sport_type: Optional[str] = None) -> list:
    """
    Get a list of recent Garmin activities with summary metrics.

    Args:
        limit:      Number of activities to return (default 10, max 50)
        sport_type: Optional filter — 'running', 'road_biking', 'lap_swimming'
    """
    return get_activities(limit=limit, sport_type=sport_type)


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
    Get daily readiness metrics for a given date including HRV, body battery,
    training load balance, ACWR, and VO2max trends.

    Args:
        date: Date in YYYY-MM-DD format, or 'today' / 'yesterday'
    """
    return get_daily_readiness(date)


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

