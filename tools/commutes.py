# tools/commutes.py
"""Commute detection for the Activity tab.

A ride is a commute when it's point-to-point — it finished somewhere else
than it started — and the same trip (either direction: start and end within
ENDPOINT_MATCH_M of each other, distance within DISTANCE_TOLERANCE) happens
at least MIN_TRIPS times, this ride included, within WINDOW_DAYS either side
of it. A manual override (activity_overrides.is_commute) always wins, for the
odd one-off commute or a regular point-to-point ride that isn't one.
"""
import math
from datetime import date, timedelta

import db

POINT_TO_POINT_M = 500      # start and end at least this far apart
ENDPOINT_MATCH_M = 300      # two rides share an endpoint within this distance
DISTANCE_TOLERANCE = 0.15   # ±15% ride distance
MIN_TRIPS = 3
WINDOW_DAYS = 28


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _endpoints(ride: dict):
    s = ride.get("summary") or {}
    pts = (s.get("start_lat"), s.get("start_lon"), s.get("end_lat"), s.get("end_lon"))
    return None if any(p is None for p in pts) else pts


def _day(ride: dict) -> date | None:
    text = str((ride.get("summary") or {}).get("date") or ride.get("activity_date") or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def is_point_to_point(ride: dict) -> bool:
    pts = _endpoints(ride)
    return pts is not None and haversine_m(*pts) >= POINT_TO_POINT_M


def same_trip(a: dict, b: dict) -> bool:
    """Same endpoints (in either direction) and a similar distance."""
    pa, pb = _endpoints(a), _endpoints(b)
    if pa is None or pb is None:
        return False
    da, db_ = a.get("distance_km") or 0, b.get("distance_km") or 0
    if not da or not db_ or abs(da - db_) > DISTANCE_TOLERANCE * max(da, db_):
        return False
    (sa, sa2, ea, ea2), (sb, sb2, eb, eb2) = pa, pb
    forward = haversine_m(sa, sa2, sb, sb2) <= ENDPOINT_MATCH_M and haversine_m(ea, ea2, eb, eb2) <= ENDPOINT_MATCH_M
    reverse = haversine_m(sa, sa2, eb, eb2) <= ENDPOINT_MATCH_M and haversine_m(ea, ea2, sb, sb2) <= ENDPOINT_MATCH_M
    return forward or reverse


def detect(rides: list[dict], candidates: list[dict] | None = None) -> dict[int, bool]:
    """{garmin_id: is_commute} for ``candidates`` (default: every ride),
    comparing against all of ``rides`` — which should reach WINDOW_DAYS
    either side of the candidates."""
    p2p = [r for r in rides if is_point_to_point(r)]
    out = {}
    for ride in candidates if candidates is not None else rides:
        day = _day(ride)
        if day is None or not is_point_to_point(ride):
            out[ride["garmin_id"]] = False
            continue
        window = timedelta(days=WINDOW_DAYS)
        trips = sum(1 for other in p2p
                    if other["garmin_id"] != ride["garmin_id"]
                    and (_day(other) is not None and abs(_day(other) - day) <= window)
                    and same_trip(ride, other))
        out[ride["garmin_id"]] = trips + 1 >= MIN_TRIPS
    return out


def apply_overrides(flags: dict[int, bool], overrides: dict[int, dict]) -> dict[int, bool]:
    out = dict(flags)
    for gid, row in overrides.items():
        if gid in out and row.get("is_commute") is not None:
            out[gid] = bool(row["is_commute"])
    return out


def commute_flags(activities: list[dict]) -> dict[int, bool]:
    """{garmin_id: is_commute} for the given activities (rides are checked;
    anything else is never a commute unless overridden)."""
    if not activities:
        return {}
    ids = [a["garmin_id"] for a in activities]
    flags = {gid: False for gid in ids}
    days = [d for d in (_day(a) for a in activities) if d]
    if days and db.is_configured():
        lo, hi = min(days) - timedelta(days=WINDOW_DAYS), max(days) + timedelta(days=WINDOW_DAYS + 1)
        rides = db.get_rides_in_range(lo.isoformat(), hi.isoformat())
        wanted = set(ids)
        flags.update(detect(rides, [r for r in rides if r["garmin_id"] in wanted]))
    overrides = db.get_activity_overrides(ids) if db.is_configured() else {}
    return apply_overrides(flags, overrides)
