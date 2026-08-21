# tools/activity_route.py
"""GPS route + elevation profile for a single activity (issue #74).

python-garminconnect can download an activity's original file directly, but
the coordinate/elevation samples it carries aren't part of any of the JSON
endpoints the rest of tools/activities.py uses — this pulls the GPX export
specifically for the map polyline and elevation chart on the Activity Detail
page.

The GPX download is a separate Garmin API call per activity, and a past
activity's track never changes once synced, so a successful parse is cached
in-process indefinitely (keyed by activity_id) rather than re-fetched on
every page load.
"""
import logging
import threading

import gpxpy

from garmin_client import get_client

logger = logging.getLogger(__name__)

# Long tracks (a century ride, an ultra) can carry tens of thousands of GPX
# trackpoints — more resolution than an SVG polyline/elevation chart can show
# and more markup than a page needs to ship. Points are stride-sampled down
# to roughly this many.
_MAX_POINTS = 400

_route_cache: dict[int, dict | None] = {}
_route_cache_lock = threading.Lock()


def _downhill_uphill(gpx) -> tuple[float | None, float | None]:
    try:
        updown = gpx.get_uphill_downhill()
        return updown.uphill, updown.downhill
    except Exception:  # noqa: BLE001 — elevation gain is a nice-to-have, not essential
        return None, None


def _parse_gpx(raw: bytes | str) -> dict | None:
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    gpx = gpxpy.parse(text)

    points = [
        p
        for track in gpx.tracks
        for segment in track.segments
        for p in segment.points
    ]
    if len(points) < 2:
        return None

    cum_km = [0.0]
    for prev, cur in zip(points, points[1:]):
        d = prev.distance_2d(cur) or prev.distance_3d(cur) or 0.0
        cum_km.append(cum_km[-1] + d / 1000)

    step = max(1, len(points) // _MAX_POINTS)
    indexes = list(range(0, len(points), step))
    if indexes[-1] != len(points) - 1:
        indexes.append(len(points) - 1)

    coords = [
        {"lat": round(points[i].latitude, 5), "lon": round(points[i].longitude, 5)}
        for i in indexes
    ]
    elevation_profile = [
        {
            "distance_km": round(cum_km[i], 3),
            "elevation_m": round(points[i].elevation, 1) if points[i].elevation is not None else None,
        }
        for i in indexes
    ]

    gain_m, loss_m = _downhill_uphill(gpx)

    return {
        "points": coords,
        "elevation_profile": elevation_profile,
        "elevation_gain_m": round(gain_m, 1) if gain_m is not None else None,
        "elevation_loss_m": round(loss_m, 1) if loss_m is not None else None,
    }


def _fetch_and_parse(activity_id: int) -> dict | None:
    client = get_client()
    try:
        raw = client.download_activity(
            activity_id, dl_fmt=client.ActivityDownloadFormat.GPX
        )
    except Exception as e:  # noqa: BLE001 — no GPS track (pool swim, strength,
        # indoor trainer, ...) is the expected shape of this failure, not a bug
        logger.info(f"No GPX available for activity {activity_id}: {e}")
        return None
    if not raw:
        return None

    try:
        return _parse_gpx(raw)
    except Exception as e:  # noqa: BLE001 — a malformed/empty track shouldn't break the page
        logger.warning(f"Failed to parse GPX for activity {activity_id}: {e}")
        return None


def get_activity_route(activity_id: int) -> dict | None:
    """The GPS route and elevation profile for an activity, or None when the
    activity has no GPS track (pool swim, strength, indoor trainer, ...) or
    the download/parse otherwise fails.

    Returns a dict with:
      points:            [{lat, lon}, ...] simplified polyline coordinates
      elevation_profile: [{distance_km, elevation_m}, ...] matching the same
                          sampled points
      elevation_gain_m:  total ascent over the full (unsampled) track
      elevation_loss_m:  total descent over the full (unsampled) track

    Cached in-process per activity_id — see the module docstring.
    """
    with _route_cache_lock:
        if activity_id in _route_cache:
            return _route_cache[activity_id]

    result = _fetch_and_parse(activity_id)

    with _route_cache_lock:
        _route_cache[activity_id] = result
    return result
