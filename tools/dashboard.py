# tools/dashboard.py
"""Server-rendered health dashboard — "Nocturne" design.

Gathers a live overview from the Garmin client and renders it as a single,
self-contained HTML page (inline CSS, no build step, no external requests —
the Phosphor icon font is embedded as a subsetted base64 woff2, and the small
amount of interactivity below is a handful of small inline `<script>` tags,
no CDN/library). Each request pulls fresh data server-side.

The four tabs (Today / Trends / Activity / Fitness) are all rendered into the
page on every load; switching tabs, the trend range (7d/14d/30d), and the
personal-records sport filter are pure CSS (`:checked` radio inputs driving
sibling visibility) — no JS is involved there.

Line/area trend charts and bar charts are touch/click-interactive: a small
inline JS module (`_CHART_JS`) reads each chart's data points off its markup
(a `data-points` JSON attribute on line charts, `data-date`/`data-value`
attributes on bar segments) and drives a shared tooltip + crosshair overlay
on mousemove/click/touch. The data itself stays server-rendered; JS only
handles pointer interaction.

The data-gathering entrypoint (`build_dashboard_data`) lazily imports the
underlying tool functions so that `render_dashboard_html` — a pure function of
a data dict — can be imported and exercised without a live Garmin session.
"""
import base64
import html
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from tools.navbar import render_nav_html

# Auto-refresh the browser page this often (seconds). 0 disables refresh.
REFRESH_SECONDS = int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "300"))

# The Trends tab's range toggle (7d/14d/30d) is backed by one get_trends() call
# fetched at this period; get_trends fetches its per-day metrics concurrently,
# but rhr/hrv/sleep_score/stress/training_load have no batch endpoint in the
# Garmin client, so each extra day still costs one more round-trip per metric.
# Defaults to 14d (2/3 of the toggle) rather than the full 30d/1m so a cold
# page load stays fast; bump to "1m" for the full 30d option if you don't mind
# the extra latency. Any of get_trends' periods works; only ranges <= the
# fetched window actually show a toggle button.
TREND_PERIOD = os.environ.get("DASHBOARD_TREND_PERIOD", "14d")

# Fallback daily step goal when the athlete has no active Garmin step goal.
DEFAULT_STEP_GOAL = int(os.environ.get("DASHBOARD_STEP_GOAL", "10000"))
DASHBOARD_CACHE_SECONDS = int(os.environ.get("DASHBOARD_CACHE_SECONDS", "60"))

_TREND_METRICS = ["rhr", "hrv", "sleep_score", "stress", "steps", "training_load"]
_dashboard_cache: tuple[float, dict] | None = None
_dashboard_cache_lock = threading.Lock()


def _tz_offset_hours() -> float:
    """Local-time offset from UTC, from DASHBOARD_TZ_OFFSET_HOURS (default 0)."""
    try:
        return float(os.environ.get("DASHBOARD_TZ_OFFSET_HOURS", "0"))
    except ValueError:
        return 0.0


def _local_now() -> datetime:
    """Current time in the configured local zone."""
    return datetime.now(timezone.utc) + timedelta(hours=_tz_offset_hours())


def _safe(fn, *args, **kwargs):
    """Run a data fetch, returning (result, error_message). Never raises."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 — a failed section must not break the page
        return None, f"{type(e).__name__}: {e}"


def _fetch_last_sync() -> dict:
    """Last device-sync info from Garmin (upload time + device name).

    Kept as a module-level helper so it can be patched in tests and so the
    live client import stays lazy.
    """
    from garmin_client import get_client

    info = get_client().get_device_last_used() or {}
    return {
        "device_name": info.get("lastUsedDeviceName"),
        "upload_time": info.get("lastUsedDeviceUploadTime"),
    }


def _fetch_parallel(tasks: dict) -> dict:
    """Run independent ``_safe()`` fetches concurrently.

    Each dashboard section is its own blocking network call (or, for trends/
    gear, a batch of them) with no dependency on any other section — running
    them one after another just adds up their latencies instead of
    overlapping them. ``tasks`` maps a result key to ``(fn, args, kwargs)``;
    returns a dict of the same keys to ``(result, error_message)`` pairs.
    """
    with ThreadPoolExecutor(max_workers=len(tasks) or 1) as pool:
        futures = {
            key: pool.submit(_safe, fn, *args, **kwargs)
            for key, (fn, args, kwargs) in tasks.items()
        }
        return {key: future.result() for key, future in futures.items()}


def build_dashboard_data() -> dict:
    """Fetch every dashboard section from the Garmin client, concurrently.

    Each section is fetched independently and its failure is captured rather
    than raised, so one unavailable metric never blanks the whole page.
    """
    # Imported lazily so render_dashboard_html stays importable without a
    # configured Garmin client.
    from tools.health import (
        get_daily_health,
        get_daily_readiness,
        get_sleep,
        get_training_readiness,
        get_training_status,
    )
    from tools.activities import get_activities, get_weekly_summary
    from tools.trends import get_trends
    from tools.performance import get_personal_records
    from tools.profile import get_athlete_profile
    from tools.challenges import get_active_goals
    from tools.gear_tracker import build_gear_status

    now = _local_now()
    today = now.date().isoformat()

    tasks = {
        "readiness":        (get_daily_readiness, (today,), {}),
        "health":           (get_daily_health, (today,), {}),
        "sleep":            (get_sleep, (today,), {}),
        "training":         (get_training_readiness, (today,), {}),
        "training_status":  (get_training_status, (today,), {}),
        "activities":       (get_activities, (), {"limit": 20}),
        "week":             (get_weekly_summary, (), {}),
        "trends":           (get_trends, (), {"period": TREND_PERIOD, "metrics": _TREND_METRICS}),
        "personal_records": (get_personal_records, (), {}),
        "active_goals":     (get_active_goals, (), {}),
        "athlete":          (get_athlete_profile, (), {}),
        "last_sync":        (_fetch_last_sync, (), {}),
        "gear_status":      (build_gear_status, (), {}),
    }
    results = _fetch_parallel(tasks)

    data = {
        "date": today,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "tz_offset_hours": _tz_offset_hours(),
    }
    for key, (value, err) in results.items():
        data[key] = value
        data[f"{key}_err"] = err
    return data


def get_dashboard_data() -> dict:
    """Return a short-lived snapshot so route hops do not refetch Garmin data."""
    global _dashboard_cache
    now = time.monotonic()
    with _dashboard_cache_lock:
        if (_dashboard_cache is not None
                and now - _dashboard_cache[0] < DASHBOARD_CACHE_SECONDS):
            return _dashboard_cache[1]
        data = build_dashboard_data()
        _dashboard_cache = (now, data)
        return data


# ── FORMATTING HELPERS ──────────────────────────────────────────────────────

_ENUM_LABELS = {
    "GOOD_SLEEP_LAST_NIGHT": "Good sleep last night",
    "DAY_STRESSFUL_AND_INTENSIVE_EXERCISE": "Stressful day with intensive exercise",
}


def _humanize(value):
    """Map a raw Garmin enum string to human-readable text.

    Known values use a lookup table; anything that looks enum-like
    (ALL_CAPS and/or underscore_separated) is converted to sentence case.
    Already-readable phrases are returned unchanged.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    if text in _ENUM_LABELS:
        return _ENUM_LABELS[text]
    letters = text.replace("_", "")
    if "_" in text or " " not in text or (letters.isalpha() and letters.isupper()):
        spaced = text.replace("_", " ").strip()
        return spaced[:1].upper() + spaced[1:].lower()
    return text


def _e(value) -> str:
    """HTML-escape a value for safe interpolation, mapping None to an em dash."""
    if value is None:
        return "&mdash;"
    return html.escape(str(value))


def _label(value) -> str:
    """Human-readable, HTML-escaped enum label ('—' when missing)."""
    human = _humanize(value)
    return html.escape(human) if human else "&mdash;"


def _num(value, suffix: str = "") -> str:
    """Render a numeric value with an optional suffix, or an em dash if missing."""
    if value is None:
        return "&mdash;"
    return f"{_e(value)}{html.escape(suffix)}"


def _trim(value: float, decimals: int = 2) -> str:
    """Fixed-point formatting with trailing zeros trimmed (19.20 -> 19.2)."""
    s = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_km(v):
    if v is None:
        return "&mdash;"
    return f"{_trim(v)} km"


def _fmt_dur(minutes):
    """Minutes as 'Xh YY' (>=60) or 'YY min'."""
    if minutes is None:
        return "&mdash;"
    total = round(minutes)
    h, m = divmod(total, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _fmt_hm_clock(hours: float):
    """Decimal hours as '<span>H<sub>h</sub>MM</span>' style pieces -> (h, mm)."""
    if hours is None:
        return None, None
    total_min = round(hours * 60)
    h, m = divmod(total_min, 60)
    return h, m


def _pace_per_km(duration_min, distance_km):
    if not duration_min or not distance_km:
        return None
    secs = duration_min * 60 / distance_km
    m, s = divmod(int(round(secs)), 60)
    return f"{m}:{s:02d} /km"


def _speed_kmh(duration_min, distance_km):
    if not duration_min or not distance_km:
        return None
    kmh = distance_km / (duration_min / 60)
    return f"{_trim(kmh, 1)} km/h"


def _pace_per_100m(duration_min, distance_km):
    if not duration_min or not distance_km:
        return None
    total_m = distance_km * 1000
    if total_m <= 0:
        return None
    secs = duration_min * 60 / (total_m / 100)
    m, s = divmod(int(round(secs)), 60)
    return f"{m}:{s:02d} /100m"


def _short_date(iso_date):
    """'2026-08-13...' -> '13 Aug'."""
    if not iso_date:
        return None
    try:
        d = date.fromisoformat(str(iso_date)[:10])
        return f"{d.day} {d.strftime('%b')}"
    except ValueError:
        return None


def _month_year(value):
    """A date-ish string -> 'Aug 2026'; passthrough on parse failure."""
    if not value:
        return "&mdash;"
    text = str(value)[:10]
    try:
        d = date.fromisoformat(text)
        return d.strftime("%b %Y")
    except ValueError:
        return _e(text)


# ── SPORT ICON / TINT ───────────────────────────────────────────────────────
# (Phosphor glyph, tint colour) per Garmin activity typeKey. The glyph is a
# PUA codepoint into the embedded subsetted Phosphor font (see _ICON_CSS).

_RUN = ""
_BIKE = ""
_SWIM = ""
_WALK = ""
_MOUNTAIN = ""
_BARBELL = ""
_LOTUS = ""
_BOAT = ""
_MEDAL = ""
_PULSE = ""
_WRENCH = ""

_SPORT_STYLE = {
    "running": (_RUN, "#e2734a"), "trail_running": (_RUN, "#e2734a"),
    "treadmill_running": (_RUN, "#e2734a"), "track_running": (_RUN, "#e2734a"),
    "indoor_running": (_RUN, "#e2734a"),
    "road_biking": (_BIKE, "#4fae72"), "cycling": (_BIKE, "#4fae72"),
    "indoor_cycling": (_BIKE, "#4fae72"), "mountain_biking": (_BIKE, "#4fae72"),
    "gravel_cycling": (_BIKE, "#4fae72"), "virtual_ride": (_BIKE, "#4fae72"),
    "lap_swimming": (_SWIM, "#4aa7d8"), "open_water_swimming": (_SWIM, "#4aa7d8"),
    "swimming": (_SWIM, "#4aa7d8"),
    "walking": (_WALK, "#9397ab"), "hiking": (_MOUNTAIN, "#9397ab"),
    "strength_training": (_BARBELL, "#a07fe0"), "yoga": (_LOTUS, "#7fc9b0"),
    "cardio": (_PULSE, "#d9a441"), "rowing": (_BOAT, "#4aa7d8"),
    "indoor_rowing": (_BOAT, "#4aa7d8"), "elliptical": (_PULSE, "#d9a441"),
    "multi_sport": (_MEDAL, "var(--color-accent)"),
}
_DEFAULT_SPORT_STYLE = (_PULSE, "var(--color-neutral-500)")


def _sport_style(sport):
    return _SPORT_STYLE.get(str(sport or "").lower(), _DEFAULT_SPORT_STYLE)


def _sport_label(sport):
    return html.escape(str(sport or "activity").replace("_", " ").title())


def _activity_detail(a: dict) -> list[tuple[str, str]]:
    """(label, value) detail pairs for one activity — pace/speed matched to sport."""
    sport = str(a.get("type") or "").lower()
    dur, dist = a.get("duration_min"), a.get("distance_km")
    detail = []
    if sport in ("lap_swimming", "open_water_swimming", "swimming"):
        pace = _pace_per_100m(dur, dist)
        if pace:
            detail.append(("Pace", pace))
    elif sport in ("road_biking", "cycling", "indoor_cycling", "mountain_biking",
                   "gravel_cycling", "virtual_ride"):
        speed = _speed_kmh(dur, dist)
        if speed:
            detail.append(("Speed", speed))
    elif dist:
        pace = _pace_per_km(dur, dist)
        if pace:
            detail.append(("Pace", pace))
    if a.get("avg_hr") is not None:
        detail.append(("Avg HR", _num(a.get("avg_hr"))))
    if a.get("training_load") is not None:
        detail.append(("Load", _num(round(a.get("training_load")))))
    return detail


def _activity_big_stat(a: dict) -> tuple[str, str]:
    """(headline, sub) for an activity row — distance for most sports, duration
    for sports Garmin doesn't report distance for (strength, yoga, ...)."""
    if a.get("distance_km"):
        return _fmt_km(a["distance_km"]), _fmt_dur(a.get("duration_min"))
    return _fmt_dur(a.get("duration_min")), _sport_label(a.get("type"))


# ── SPARKLINE / CHART GEOMETRY ──────────────────────────────────────────────

def _fill_gaps(vals: list):
    """Forward/back-fill None gaps in a numeric series so a path stays continuous.
    Returns None if every value is missing."""
    present = [v for v in vals if v is not None]
    if not present:
        return None
    filled, last = [], present[0]
    for v in vals:
        if v is not None:
            last = v
        filled.append(last)
    return filled


def _spark(vals: list, w: int = 300, h: int = 78, p: int = 9):
    """Line/area SVG path geometry for a numeric series. Mirrors the design's
    JS spark() helper. Returns None if there's nothing to draw."""
    filled = _fill_gaps(vals)
    if filled is None or len(filled) < 2:
        return None
    vmin, vmax = min(filled), max(filled)
    span = (vmax - vmin) or 1
    n = len(filled)

    def sx(i):
        return w / 2 if n == 1 else p + i * (w - 2 * p) / (n - 1)

    def sy(v):
        return h - p - ((v - vmin) / span) * (h - 2 * p)

    pts = [(sx(i), sy(v)) for i, v in enumerate(filled)]
    line = " ".join(f"{'L' if i else 'M'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(pts))
    area = f"{line} L{pts[-1][0]:.1f} {h} L{pts[0][0]:.1f} {h} Z"
    avg = sum(filled) / n
    return {
        "line": line, "area": area, "min": vmin, "max": vmax, "avg": avg,
        "avgY": round(sy(avg), 1), "last": filled[-1], "first": filled[0],
        "points": [(round(x, 2), round(y, 2)) for x, y in pts],
    }


def _chart(series: dict, label: str, unit: str, lower_better: bool, days: int,
           big: bool = False, stroke: str | None = None, fill: str | None = None,
           chart_id: str | None = None):
    """A Trends-tab metric card's display values, sliced to the trailing `days`."""
    daily = (series or {}).get("daily") or []
    sliced = daily[-days:]
    vals = [p.get("value") for p in sliced]
    s = _spark(vals)
    if s is None:
        return None
    delta = s["last"] - s["first"]
    good = delta <= 0 if lower_better else delta >= 0
    f = (lambda v: f"{round(v):,}") if big else (lambda v: str(round(v)))
    filled = _fill_gaps(vals)
    unit_suffix = f" {unit}" if unit else ""
    points = [
        {"x": x, "y": y, "d": _short_date(sliced[i].get("date")) or "",
         "v": f"{f(filled[i])}{unit_suffix}"}
        for i, (x, y) in enumerate(s["points"])
    ]
    return {
        "label": label, "unit": unit,
        "value": f(s["last"]), "avg": f(s["avg"]), "lo": f(s["min"]), "hi": f(s["max"]),
        "delta": f"{'+' if delta > 0 else ''}{f(delta)}",
        "avgY": s["avgY"], "line": s["line"], "area": s["area"],
        "stroke": stroke or "var(--color-accent)", "fill": fill or "url(#gArea)",
        "deltaColor": "#7fc9b0" if good else "#cf8a80",
        "id": chart_id or "",
        "points_json": html.escape(json.dumps(points), quote=True),
    }


# ── ICON FONT (Phosphor, subsetted to the glyphs this page uses) ───────────

_PHOSPHOR_WOFF2_B64 = (
    "d09GMgABAAAAAA5wAA0AAAAAGhgAAA4fAAIZmgAAAAAAAAAAAAAAAAAAAAAAAAAAGxAcGgZgAIFMEQgKrWCmRQE2AiQDLAssAAQgBQYHIBtuFVGUjlYN4IuEbIpi9nJXPtQRvxpI47J5i7gU7C4bPBkhyaxR/Jy/96K8IMFTTUgqQawvDoWHaBALYknNkYonFVE6BWpGzUnFlFT54lT0B76X3Z7IBJOz2Q+i8IsisCmu3g3lWU3b02QNBF2A31PyTb5akAdw4Jt7VY9KVybbGbQDlbFkw7S4xzcvzVqDoJ8OANw7+39zpU1270qgurJARldW6Cox9+dmNzPZLHByROyKKJHkIubKJE/IGtHny0LJ2jYkcrpG0ZbeacD/Q5zWJYKH/gsEAoCGK9xxIWFh/SABGdpr5HAIUSfncJxkMXgAtYa6DmDR862R38EQF9R7E6M/68CfFo7daV+U8xFsFJiHLENnY1QgB+oTiWTcyMVROQiy/m+JdsAR41joOPyq4PWu12ffyN5Pfn+9tVtrRKuqVdNqaGVbZ7bu+W32nxanE0j/L0lGpOelo6RZnfd1Ku2U1PGXdvPaxUjeSZ5LnkgeSx5I7kvmSvr4zvU6AIILatuI0wkPrS8fxGWsEM5eS0vN3BGuW3ggoCgv18CHABxPeZ1yGCrkn4Za04SwKIvZTJtT4odi7FN8HHxtioAD3ert6u3a+9jYODg7uFxCd4NOSGqeqHzrXs5nW3na+wkzmc2OwAEEuvi7JPrY2XsHPBR2GGBRFJaIUdIs2gPQE3sP2hfoFl2XqIbaeEWtYZc87K2BxHK/ptVBChnsHQALNZdCU5Oin3UlZxZ2QMrcYViAXa07DHRV6FPV9D0AJb5ini6WXhDDO6l336F2BXr5ywvk16A21DH2oT1+TbSRBXfoPl9hFAVo1Q5MNOOiHV2Edq0RuBnHhANM30AcQx60eYIxZOALFYk4sSxNDN80D6hgQILRV0w7bQ9DxgaCwAIQZxNxjyYiQkaRGVWiO5wzSBcBAgNh1Y+gbMQQx1xzshrjIVSDt9U/UfjAOUO5zhPi1FGKmWLDEfCwP7vF3tA3ZYYQMKOotljL+8Dwzkx4G+YsLODvAfFqdipNA32fkfUJS5z+ja8v+wLj5lKBcmnjApo76455NqPyKgJK+U2o96xtxMQVMX6nU/4VxS8Mxc1h6heANDrhhCXVHP1qFBWGukWikCjm5MzT//AZ4dw/kIz2T/zoyV/9gcYkIwRpYzIbPzo5G4MJtBOQCHrxBIccyWIQhDvMUWDqOvNEwme+IhOC/RSPSv+EkKPzhxA1hro9cMYRh00pKFtW+GNWfV6iP9S2LGtXbImnddy++OwLpjAbX5TuCBsg3FxwA0N071s3K2nsGx/NwIAj9b54dEpBJSkzpKbKRWRC7HYqIsQ3p4u7isZFUV7vd/NrV56BSCAC6jR8195i4hvH2CO3Ldk0PHvot4dIb7a+dTNd+n8xeZOSb4sEj1wz4QMUUDHkV4fKvlNy+3t3ARyALKKCnTdKLKvkkYQ7G3YGFmx86aKvSbibPw46BPUPJQZCt9kQi5CDjv3UI3IYNwX0jeBf3CC89cyKz/VvX7/IbfYFdaoYAGsg+lRx2DSCPyq6k78ZZ/dhFq146RyVfK05Uyya+M7NOrKW7qsAumaTscbw+m/orm8gmP4emRBh2hAUGlN+Sb1gvwLiazmVCwlZuACV7vJnoP2JDJ4D23mUbUha0XHaHqrWQJJUbVnRyuO3RQaI8K+J9A6Bi2mnh46ICe1u4lqiJ6dA2sUCoGhlCrSgCvp2g/SP6D9D/1af7h/6imc3a7jQVzHhjFr+9JCrKE7T6CEOQwo7iulLwni4RDt+6GX/O/aaI/+Xfkbph3QPwbRgPG+kMloFglhhs2zTY3tFLyM2bh6V28s4Ys0WSIL//N6LogTpLXMBo1PsFhuGWBNVVUMZK9JvS8IxWc2ySr4fEtIgnD+wZQvq9j+UTi/Bcm+kiFSru+aCPiamCse//x6X9eKyjQMsYLOV5cj53+3xovmO7b3H0TOP2xq8jBowY38w9Qaq9BPOMBhT9PhAGA7b1KX48m7O7mFCfdWOgglDwEG90mWLe0mmWZlm2cI/sDTHxz+JJSaBnY/EyOrkFN2dYFwXrF2CEWO/OuxUiXancAf1RT32rWF5M2aJ6QJ7PlVC9YudvZa8fJ2UXwZ1LIeFh4ngDqQlix5OxSd3xL+N0AmOldgG63RYxbPmnRJx/mBaRVEbbciqv9bXA+dg1Y28h1i67/OjNGd29ANYKEe6+QWEv+trW2kNcZFoTi/aO4SQZc6CNy82igghzB9Yo21oaOfqWg2m9Hm7tHdYzKTdw7PB4U3X6Cp0ZtHE7iZ3w9CVodqvLgAol9+15G2RtZ3uV9qWhVkesiqDGUB/SyPvyib/XwuRUQCmepBUmrBJINiUACCr0gUvvJf88mttZqF2oBocZU1XHYGA6SaaBCCE+oshDpmtQ2IHsLInZunKHHbHu03GNk6bDOJspS/XwM5uHevNFljqnUFFMwhuq1WWKYYBTEKrl8MbjLu6l9nI5y1lyG97/7jJHOS7e7oLKvLhbImqeHVhiWPUQGptmHwvUucmPRIKHyWh76oUVAPAEr3H7PZb62MG9OjY0ceLYc7djY8PjWZA8Si3gBxA4O4Uh8oYMiSjsUwfqfumEQsioWgIX1I5kekR6VPH/vp19uXu61T1lGotc0jg7uXGN6jwWqkvjL5tlwVUc7nVAeidEJgYuH4uW86WDWKiQ+Pj755jAM1dt9jYsePc3MaNjW84a5UbADmUOWyOd6sN0mXVLITMt0eI9kSy3RK7bYaGYuEtrbnJbP7+LS3t2/crQFNzWl7ivMUkEjDacXtFkH3Fe7WsFxehXRDZlDg/D6m36D4FmZlJmuhoTVK8oqAPCRNgOnVWVn5Fs2hJ6mkAhMAr3JPneIeE5PpEUvXFYVNs14WSsDCJ8LptSliIhL4XbPEVFfF3EX2jgbJbWAoKa2wlIhsbzuP5lhXJhsbGD6S3viQ8B7L0/tC4IVm/cvNzcXJsVe+hpUU5RgnWYhNLeyo0MVmJSW/vq9Vv7iUl9YxRw4H6+jRLnxIQeqIFPauflIYdIG2MN1fIJcfpsrp8iS/XwsHnGRu06rj2GKURiTSj0E4d141UzXov5li4vpI8Mo8u28ERcr0LfMTnwkqImnXhCjlYnjG5UlehXecVGupRjHAC4+RNHCFPyGrIrxcILl//oXYEWU8ndSUUgc677IMp97unugIL9ovWZYezAyafO+vhgioy06+R7E7IsuLL4HqxIOppQZIy0eFg2b9vy24m09OnJpPl9t/cCodjaFLB0x07zNrDFBKvBPuxQF3h2tuEDxpFRyjSlU5u5Hy4nhw7oGc5uehShC0hcx1Z7FPLscDF49xrCjI0oNAoM0qrqqSPtbBwCkajRltQGEdhqFtHjAiQ/DeSA9b1lekiSiT9KLYqo/2+VdTTMyU/p1Hn2QhVzt8tKkzLnBteV9cla+mGH2Seqyg1t6ZPGgL/TxzV/4dbj2R/yQQyZnXnkO2ru6Xt2eNPUr/w3AWRJ66HZDr/6afYqs2dsyLbsM4AoDSKpeLE0VcGdtR3SRJ98/s4OzYLAMxmdXq6+s7QVNs7JHYYKORRXZeKJ3QJGi/NDD8vZUO9DI4D4jrDuHQLerbYwUPidDnZDwlD7tF0imLgoEAQ4iKzUXsp16IcCQVkCmZGaRLKI/tfGpkwR0OYjfzk5xEOLYC4qaHCgxWxdKZ6VtkqGrJeHHcvN7p+GrDIGhYXF3Zn6KsbfE74DBSCiqYoA8XKMcoW3o4k1tl2ksY2AHDPKQYOysubM1uVLgAAO3clB1aucNoOq9WNl4wavSz+TNAsldzij3IVBp6wPAGGCnmQrnfQZaNysbwyK345/1+hPB/7srPNLhs39stQLpKbs5ilfMGFRPyxMr9c4B7gxl/WA+asxXKj8nLvIG1wudyQ2yh0kz8SA7eiVPHDhyd5/PTOY2cFg6AXYX8lvQASavJLEI8anZenuDBs5pEji7MUK/5NkKUWF/d7ZgLMVsptztfAHmXwGNOa2c5+9YTlg/KHCQHAgWb47QlFM1Xdv9G4qOXmBKCmiebwvfDon3WSeIb8xFEwG78k6vhGBsyUJaOi4oXxAut20/a1lfGFYxdo4/kRV75lEgvZxjqOxisR3Wy+daxB5dFxPeNWFLSx3rlUBOUsyIVCG4kSKJll+SAAvlXdSO0THPyYK2Y5HFYM8Ojma8aaIU0FhYo/csOKFSvihhjiawMHxQ72wSP4rBnaVc8hI55WTbJ7+hm1XnsXYMDeA+GT7V5e9kmky99eEq6b3M1GovD85aTuVkX9ye3SaS1C5zc9rmrczVOxUMHhqBsjovPzo+8IQVCDNDvgrwB3OYgbrPWJxndfNduqiItaZTx/jhrYdHq/6qIQwFUAgouJ2NfcRA08d964lY1SFDTvoE0mXt72bZSyv0jUX0lt284gVqHf1WfZJLygXrEqrlrBRm0FrGvel3j4INIROQ0AE0F/p2uNvtVQqgOA0R/2yqqqbDCuaikD1a/bLnu4r5x98SJzkA8gQnuxMgB4h37uOF1x8mT3urqs3d3r759HX768fqxX5qhBWTQpBmDMHNsirtb1FdUphjzb6s0B/DN9HQW6qf9EnfjM1NWleHik3Dkz1hMHshVNigkTpE2K7LZb2oFG48DaW208J0XjB5vXXLvMu622dkyLk2ljYts4LWOYqM8Cq3HImPDv06wnGP8RDDPCnzlhnXZEMeBnyQRUu7WrwtinZo/oj0IXblTl6xDXNc+695t+PgjrdHDB2QVACCon/snRiBwSdx9/EUnsFhZ22KkFtbehATLIMBlpoyiA9QlD2OaLVxAeksJl3/ngAAAA"
)

_ICON_CSS = (
    "@font-face{font-family:'PhosphorSub';font-weight:400;font-style:normal;"
    f"src:url(data:font/woff2;base64,{_PHOSPHOR_WOFF2_B64}) format('woff2')}}"
    ".ph{font-family:'PhosphorSub';font-style:normal;speak:never;font-variant:normal;"
    "text-transform:none;line-height:1;-webkit-font-smoothing:antialiased}"
)


# ── DASHBOARD DATA SHAPING (build_dashboard_data() -> display-ready values) ─

_READINESS_COLORS = {
    "PRIME": "#4fae72", "HIGH": "#4fae72", "GOOD": "#4fae72",
    "MODERATE": "#d9a441", "FAIR": "#d9a441", "LOW": "#cf5a4e", "POOR": "#cf5a4e",
}


def _readiness_color(level):
    return _READINESS_COLORS.get(str(level or "").upper(), "var(--color-accent)")


def _step_goal(data: dict) -> int:
    for g in (data.get("active_goals") or []):
        name = f"{g.get('goal_category') or ''} {g.get('goal_type_name') or ''}".lower()
        if "step" in name and g.get("target_value"):
            return int(g["target_value"])
    return DEFAULT_STEP_GOAL


def _acwr_gauge_pct(acwr):
    if acwr is None:
        return None
    lo, hi = 0.5, 1.8
    return round(max(0.0, min(1.0, (acwr - lo) / (hi - lo))) * 100, 1)


# ── CSS ──────────────────────────────────────────────────────────────────────

_STYLE = """
:root {
  color-scheme: dark;
  --color-bg: #161826; --color-surface: #232532; --color-text: #e9e9ed;
  --color-accent: #9184d9; --color-accent-2: #a7a1db;
  --color-divider: color-mix(in srgb, #e9e9ed 16%, transparent);
  --color-neutral-100: #f3f5fe; --color-neutral-200: #e4e7f5; --color-neutral-300: #cfd3e5;
  --color-neutral-400: #b2b6ca; --color-neutral-500: #9397ab; --color-neutral-600: #75798c;
  --color-neutral-700: #595d6c; --color-neutral-800: #3f424d; --color-neutral-900: #292b31;
  --color-accent-100: #f5f4ff; --color-accent-200: #e7e5fe; --color-accent-300: #d2cefd;
  --color-accent-400: #b5abfc; --color-accent-500: #968ae0; --color-accent-600: #796cbf;
  --color-accent-700: #5d5294; --color-accent-800: #423a6a; --color-accent-900: #2b2741;
  --font-heading: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-body: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --radius-md: 8px;
  --shadow-sm: 0 0 0 1px #3f424d;
  --shadow-md: 0 0 0 1px #595d6c, 0 6px 18px rgba(0,0,0,0.55);
}
* { box-sizing: border-box; }
body { margin:0; background:var(--color-bg); color:var(--color-text); font-family:var(--font-body);
       font-size:15px; line-height:1.5; }
h1,h2,h3,h4 { font-family:var(--font-heading); font-weight:500; margin:0; }
.card { display:flex; flex-direction:column; gap:8px; padding:12px; border-radius:var(--radius-md);
        background:var(--color-surface); }
.muted { color:var(--color-neutral-500); }
.kicker { font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--color-neutral-500); }
.section-title { font-size:11px; letter-spacing:.12em; text-transform:uppercase;
                  color:var(--color-neutral-500); margin-bottom:8px; }
.err { color:#cf8a80; font-size:12px; padding:12px; }

/* ── icon-only radio → pill toggle (tabs / range / PR filter) ── */
input.hide { position:absolute; opacity:0; width:0; height:0; pointer-events:none; }
.pillbar { display:inline-flex; gap:4px; padding:3px; border-radius:999px;
           border:1px solid var(--color-divider); }
.pillbar label { border:0; cursor:pointer; font:inherit; font-size:11px; padding:5px 13px;
                 border-radius:999px; color:var(--color-neutral-500); display:inline-flex;
                 align-items:center; gap:5px; }
.tabpanel, .range-set, .pr-group, .activity-filter-section { display:none; }
#tab-today:checked ~ .tabpanels .tp-today,
#tab-trends:checked ~ .tabpanels .tp-trends,
#tab-activity:checked ~ .tabpanels .tp-activity,
#tab-you:checked ~ .tabpanels .tp-you,
#tab-gear:checked ~ .tabpanels .tp-gear { display:flex; }
#range-7:checked ~ .range-body .rs-7,
#range-14:checked ~ .range-body .rs-14,
#range-30:checked ~ .range-body .rs-30 { display:grid; }
#activity-filter-all:checked ~ .tabpanels .activity-filter-all,
#activity-filter-triathlon:checked ~ .tabpanels .activity-filter-triathlon,
#activity-filter-bike:checked ~ .tabpanels .activity-filter-bike,
#activity-filter-run:checked ~ .tabpanels .activity-filter-run,
#activity-filter-strength:checked ~ .tabpanels .activity-filter-strength,
#activity-filter-other:checked ~ .tabpanels .activity-filter-other { display:flex; }
#prf-all:checked ~ .pr-body .pr-group,
#prf-run:checked ~ .pr-body .pr-group.pr-running,
#prf-bike:checked ~ .pr-body .pr-group.pr-cycling,
#prf-swim:checked ~ .pr-body .pr-group.pr-swimming { display:block; }
#range-7:checked ~ .rangebar label[for=range-7],
#range-14:checked ~ .rangebar label[for=range-14],
#range-30:checked ~ .rangebar label[for=range-30],
#activity-filter-all:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-all],
#activity-filter-triathlon:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-triathlon],
#activity-filter-bike:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-bike],
#activity-filter-run:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-run],
#activity-filter-strength:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-strength],
#activity-filter-other:checked ~ .tabpanels .activity-filterbar label[for=activity-filter-other],
#prf-all:checked ~ .prbar label[for=prf-all],
#prf-run:checked ~ .prbar label[for=prf-run],
#prf-bike:checked ~ .prbar label[for=prf-bike],
#prf-swim:checked ~ .prbar label[for=prf-swim] {
  background: color-mix(in srgb, var(--color-accent) 20%, transparent);
  color: var(--color-accent-200);
}
.botnav label { flex:1; border:0; cursor:pointer; font:inherit; color:var(--color-neutral-500);
                border-radius:999px; padding:8px 0; display:flex; flex-direction:column;
                align-items:center; gap:2px; }
.botnav label i { font-size:19px; }
.botnav label span { font-size:9px; letter-spacing:.06em; text-transform:uppercase; }
#tab-today:checked ~ .botnav label[for=tab-today],
#tab-trends:checked ~ .botnav label[for=tab-trends],
#tab-activity:checked ~ .botnav label[for=tab-activity],
#tab-you:checked ~ .botnav label[for=tab-you],
#tab-gear:checked ~ .botnav label[for=tab-gear] {
  background: color-mix(in srgb, var(--color-accent) 20%, transparent);
  color: var(--color-accent-200);
}

/* ── gear tracker forms (issue 53's tab) ── */
.gear-form { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-end; margin-top:10px;
             padding:10px; background:var(--color-neutral-900); border-radius:var(--radius-md); }
.gear-form label { font-size:10px; color:var(--color-neutral-500); display:flex;
                    flex-direction:column; gap:3px; }
.gear-form input, .gear-form select {
  font:inherit; font-size:12px; color:var(--color-text); background:var(--color-bg);
  border:1px solid var(--color-divider); border-radius:6px; padding:.35rem .5rem;
}
.gear-form button { font:inherit; font-size:12px; cursor:pointer; border-radius:6px;
                     border:1px solid var(--color-accent-700); background:transparent;
                     color:var(--color-accent-200); padding:.4rem .8rem; align-self:flex-end; }
.gear-form button:hover { background:color-mix(in srgb, var(--color-accent) 18%, transparent); }
details.gear-actions summary { cursor:pointer; list-style:none; color:var(--color-accent-300);
                                font-size:11px; display:inline-block; margin-right:14px; }
details.gear-actions summary::-webkit-details-marker { display:none; }
.gear-table { width:100%; border-collapse:collapse; }
.gear-table th, .gear-table td { text-align:left; padding:8px 10px; font-size:12px;
                                  border-bottom:1px solid var(--color-divider); }
.gear-table th { color:var(--color-neutral-500); font-weight:500; font-size:10px;
                  text-transform:uppercase; letter-spacing:.05em; }
.gear-table tr:last-child td { border-bottom:none; }
.gear-log-list { max-height:320px; overflow-y:auto; display:flex; flex-direction:column; gap:8px; }

/* ── gear tab (Nocturne mockup match, issue 63) ── */
.gt-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; }
.gt-bike-card { text-decoration:none; color:inherit; }
.gt-bike-card:hover { box-shadow:0 0 0 1px var(--color-accent-700); }
.gt-wearbar { height:5px; border-radius:999px; background:var(--color-neutral-800); overflow:hidden; }
.gt-wearbar > div { height:100%; border-radius:999px; }
.gt-badge { display:inline-flex; align-items:center; gap:5px; font-size:11px; font-weight:500;
            padding:3px 9px; border-radius:999px; border:1px solid; white-space:nowrap; }
details.gt-bike { background:var(--color-surface); border-radius:var(--radius-md);
                   margin-bottom:12px; scroll-margin-top:60px; }
details.gt-bike > summary { display:flex; align-items:center; justify-content:space-between;
                             padding:14px; cursor:pointer; list-style:none; }
details.gt-bike > summary::-webkit-details-marker { display:none; }
details.gt-bike > summary::after { content:'\\25BE'; color:var(--color-neutral-500);
                                    font-size:13px; transition:transform .15s; }
details.gt-bike[open] > summary::after { transform:rotate(180deg); }
.gt-comp-grid { display:grid; grid-template-columns:minmax(150px,1fr) 100px 100px 80px 140px; gap:8px; align-items:center; }
.gt-comp-row { padding:8px 10px; border-radius:6px; text-decoration:none; color:inherit; cursor:pointer; }
.gt-comp-row:hover { filter:brightness(1.1); }
.gt-hist-extra { display:none; }
.gt-history label[for=gt-hist-lim] { display:none; }
.gt-hist-toggle { font-size:12px; color:var(--color-accent-300); cursor:pointer; }
#gt-hist-all:checked ~ .gt-history .gt-hist-extra { display:table-row; }
#gt-hist-all:checked ~ .gt-history label[for=gt-hist-all] { display:none; }
#gt-hist-all:checked ~ .gt-history label[for=gt-hist-lim] { display:inline; }
.gear-modal { display:none; position:fixed; inset:0; z-index:1000; align-items:center;
              justify-content:center; padding:16px; }
.gear-modal:target { display:flex; }
.gear-modal-backdrop { position:absolute; inset:0; background:rgba(10,11,16,.65); }
.gear-modal-dialog { position:relative; z-index:1; width:100%; max-width:420px; max-height:85vh;
                      overflow-y:auto; background:var(--color-surface); border-radius:var(--radius-md);
                      padding:18px; display:flex; flex-direction:column; gap:12px; box-shadow:var(--shadow-md); }

/* ── activity expand ── */
details.actcard { padding:12px; border-radius:var(--radius-md); background:var(--color-surface);
                   box-shadow:var(--shadow-sm); }
details.actcard[open] { box-shadow:0 0 0 1px var(--color-accent-700); }
details.actcard summary { cursor:pointer; list-style:none; }
details.actcard summary::-webkit-details-marker { display:none; }

/* ── interactive charts (crosshair line/area + tappable bars) ── */
.js-bar { cursor:pointer; }
.chart-tooltip { display:none; position:fixed; z-index:100; pointer-events:none;
  background:var(--color-neutral-900); border:1px solid var(--color-divider); border-radius:6px;
  padding:6px 10px; box-shadow:var(--shadow-md); white-space:nowrap; }
.chart-tooltip .tt-date { font-size:10px; color:var(--color-neutral-500); }
.chart-tooltip .tt-val { font-family:var(--font-heading); font-size:13px; margin-top:1px; }
""" + _ICON_CSS


# ── PANEL: TODAY ─────────────────────────────────────────────────────────────

_STRESS_ZONES = [
    ("Rest", "rest_stress_mins", "#4fae72"),
    ("Low", "low_stress_mins", "#7fc9b0"),
    ("Medium", "medium_stress_mins", "#d9a441"),
    ("High", "high_stress_mins", "#cf5a4e"),
]


def _stress_breakdown_card(health: dict) -> str:
    """Today's stress-zone minutes as a tappable bar chart, or '' if unavailable."""
    stress = (health or {}).get("stress") or {}
    zones = [(label, stress.get(key), color) for label, key, color in _STRESS_ZONES]
    if not any(v is not None for _, v, _ in zones):
        return ""
    vmax = max((v or 0) for _, v, _ in zones) or 1
    bars = "".join(
        f'<div class="js-bar" data-date="{html.escape(label)} stress" '
        f'data-value="{_fmt_dur(v) if v is not None else "No data"}" '
        'style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;'
        'align-items:center;gap:6px;height:100%">'
        f'<div style="width:100%;border-radius:5px 5px 2px 2px;height:{max(round((v or 0) / vmax * 100), 3)}px;'
        f'background:{color};min-height:3px"></div>'
        f'<div style="font-size:10px;color:var(--color-neutral-600)">{html.escape(label)}</div></div>'
        for label, v, color in zones
    )
    return f"""
    <div>
      <div class="section-title">Today's stress breakdown</div>
      <div class="card" style="padding:14px">
        <div style="display:flex;align-items:flex-end;gap:6px;height:92px">{bars}</div>
      </div>
    </div>"""


def _factor_bar(label, pct):
    pct = 0 if pct is None else pct
    color = "#4fae72" if pct >= 80 else ("#d9a441" if pct >= 60 else "#cf5a4e")
    return (
        '<div style="display:grid;grid-template-columns:78px 1fr 26px;align-items:center;gap:9px">'
        f'<div style="font-size:11px;color:var(--color-neutral-400)">{html.escape(label)}</div>'
        '<div style="height:5px;border-radius:999px;background:var(--color-neutral-800);overflow:hidden">'
        f'<div style="height:100%;border-radius:999px;width:{pct}%;background:{color}"></div></div>'
        f'<div style="font-size:11px;text-align:right;color:var(--color-neutral-500)">{_num(round(pct) if pct else None, "%")}</div>'
        "</div>"
    )


def _panel_today(data: dict) -> str:
    training = (data.get("training") or {}).get("readiness") or {}
    readiness = data.get("readiness") or {}
    health = data.get("health") or {}
    sleep = data.get("sleep") or {}
    week = data.get("week") or {}
    activities = data.get("activities") or []
    today = data.get("date")

    if not training and not readiness and data.get("training_err") and data.get("readiness_err"):
        hero = f'<div class="card err">Training readiness unavailable — {_e(data.get("training_err"))}</div>'
    else:
        score = training.get("score")
        level = training.get("level")
        color = _readiness_color(level)
        pct = (score / 100 * 326.7) if score is not None else 0
        factors = [
            ("Sleep", training.get("sleep_score_factor_percent")),
            ("Recovery", training.get("recovery_time_factor_percent")),
            ("Load balance", training.get("acwr_factor_percent")),
            ("HRV", training.get("hrv_factor_percent")),
            ("Stress history", training.get("stress_history_factor_percent")),
        ]
        factor_rows = "".join(_factor_bar(l, v) for l, v in factors if v is not None)
        hero = f"""
        <div class="card" style="padding:16px;box-shadow:var(--shadow-sm);
            background:linear-gradient(160deg, color-mix(in srgb, var(--color-accent) 10%, var(--color-surface)), var(--color-surface) 62%);
            display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px;align-items:center">
          <div style="display:flex;align-items:center;gap:16px">
            <div style="position:relative;flex:0 0 auto">
              <svg width="124" height="124" viewBox="0 0 124 124" style="display:block;transform:rotate(-90deg)">
                <circle cx="62" cy="62" r="52" fill="none" stroke="var(--color-neutral-800)" stroke-width="9"></circle>
                <circle cx="62" cy="62" r="52" fill="none" stroke="{color}" stroke-width="9" stroke-linecap="round" stroke-dasharray="{pct:.1f} 326.7"></circle>
              </svg>
              <div style="position:absolute;inset:0;display:grid;place-items:center;text-align:center">
                <div><div style="font-family:var(--font-heading);font-size:38px;line-height:1">{_num(score)}</div>
                <div style="font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--color-neutral-500)">ready</div></div>
              </div>
            </div>
            <div style="min-width:0">
              <div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--color-accent-300)">Training readiness</div>
              <div style="font-family:var(--font-heading);font-size:22px;margin:3px 0 5px;color:{color}">{_label(level)}</div>
              <div style="font-size:12px;color:var(--color-neutral-400);line-height:1.4">{_label(training.get("feedback_short")) or "&mdash;"}</div>
            </div>
          </div>
          <div style="display:flex;flex-direction:column;gap:9px">{factor_rows or '<div class="muted" style="font-size:12px">No factor data.</div>'}</div>
        </div>"""

    bb = readiness.get("body_battery") or {}
    daily_stats = readiness.get("daily_stats") or {}
    hrv = readiness.get("hrv") or {}
    hr = health.get("heart_rate") or {}

    bb_pct = 0
    if bb.get("current_level") is not None and bb.get("highest"):
        bb_pct = max(2, round(bb["current_level"] / max(bb["highest"], 1) * 100))
    step_goal = _step_goal(data)
    steps = daily_stats.get("total_steps")
    step_frac = min(1, (steps or 0) / step_goal) if step_goal else 0
    step_dash = round(step_frac * 169.6, 1)
    step_color = "#4fae72" if steps and steps >= step_goal else "var(--color-accent)"
    step_over = f'{"+" if steps and steps >= step_goal else ""}{round((steps / step_goal - 1) * 100)}% of goal' if steps and step_goal else "&mdash;"
    active_min = round(daily_stats["active_seconds"] / 60) if daily_stats.get("active_seconds") else None

    rhr_series = ((data.get("trends") or {}).get("metrics") or {}).get("rhr") or {}
    rhr_spark = _spark([p.get("value") for p in (rhr_series.get("daily") or [])[-14:]], 300, 60, 7)
    rhr_svg = ""
    if rhr_spark:
        rhr_svg = (f'<svg viewBox="0 0 300 60" preserveAspectRatio="none" style="width:100%;height:44px;display:block">'
                   f'<path d="{rhr_spark["area"]}" fill="url(#gRhr)"></path>'
                   f'<path d="{rhr_spark["line"]}" fill="none" stroke="#cf5a4e" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"></path></svg>')

    hrv_val = hrv.get("weekly_avg")
    hrv_lo, hrv_hi = hrv.get("baseline_low"), hrv.get("baseline_high")
    hrv_marker = 50.0
    if hrv_val is not None and hrv_lo is not None and hrv_hi is not None and hrv_hi > hrv_lo:
        hrv_marker = max(4, min(96, (hrv_val - hrv_lo) / (hrv_hi - hrv_lo) * 100))

    quick_cards = f"""
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px">
      <div class="card" style="padding:14px;gap:10px">
        <div class="kicker">Body battery</div>
        <div style="display:flex;align-items:flex-end;gap:12px">
          <div style="width:26px;height:64px;border-radius:8px;background:var(--color-neutral-800);
              display:flex;flex-direction:column;justify-content:flex-end;overflow:hidden">
            <div style="height:{bb_pct}%;background:linear-gradient(#5b8fd8,#3b5bb5)"></div></div>
          <div><div style="font-family:var(--font-heading);font-size:30px;line-height:1">{_num(bb.get("current_level"))}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">peak {_num(bb.get("highest"))}</div></div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:4px 10px;font-size:10px;color:var(--color-neutral-500)">
          <span style="white-space:nowrap"><i class="ph">&#xe08e;</i> {_num(bb.get("charged"))} charged</span>
          <span style="white-space:nowrap"><i class="ph">&#xe03e;</i> {_num(bb.get("drained"))} drained</span>
        </div>
      </div>
      <div class="card" style="padding:14px;gap:10px">
        <div class="kicker">Steps</div>
        <div style="display:flex;align-items:center;gap:12px">
          <div style="position:relative;flex:0 0 auto">
            <svg width="64" height="64" viewBox="0 0 64 64" style="display:block;transform:rotate(-90deg)">
              <circle cx="32" cy="32" r="27" fill="none" stroke="var(--color-neutral-800)" stroke-width="6"></circle>
              <circle cx="32" cy="32" r="27" fill="none" stroke="{step_color}" stroke-width="6" stroke-linecap="round" stroke-dasharray="{step_dash} 169.6"></circle>
            </svg>
            <div style="position:absolute;inset:0;display:grid;place-items:center;color:{step_color};font-size:18px"><i class="ph">&#xea88;</i></div>
          </div>
          <div><div style="font-family:var(--font-heading);font-size:24px;line-height:1">{_num(steps)}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">goal {step_goal:,}</div></div>
        </div>
        <div style="font-size:10px;color:{step_color}">{step_over}{f" · {active_min} active minutes" if active_min is not None else ""}</div>
      </div>
      <div class="card" style="padding:14px;gap:8px">
        <div class="kicker">Resting HR</div>
        <div style="display:flex;align-items:baseline;gap:6px">
          <div style="font-family:var(--font-heading);font-size:30px;line-height:1">{_num(hr.get("resting_hr") or daily_stats.get("resting_hr"))}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">bpm · 7d {_num(hr.get("seven_day_avg_resting_hr") or daily_stats.get("resting_hr_7day_avg"))}</div>
        </div>
        {rhr_svg}
      </div>
      <div class="card" style="padding:14px;gap:8px">
        <div class="kicker">HRV status</div>
        <div style="display:flex;align-items:baseline;gap:6px">
          <div style="font-family:var(--font-heading);font-size:30px;line-height:1">{_num(hrv_val)}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">ms</div>
        </div>
        <div style="position:relative;height:26px;margin-top:2px">
          <div style="position:absolute;left:0;right:0;top:11px;height:4px;border-radius:999px;background:var(--color-neutral-800)"></div>
          <div style="position:absolute;left:20%;width:55%;top:11px;height:4px;border-radius:999px;background:color-mix(in srgb, #4fae72 55%, transparent)"></div>
          <div style="position:absolute;left:{hrv_marker:.0f}%;top:5px;width:2px;height:16px;border-radius:2px;background:#7fc9b0"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
          <span>{_num(hrv_lo)}</span><span style="color:#7fc9b0">{_label(hrv.get("status"))}</span><span>{_num(hrv_hi)}</span>
        </div>
      </div>
    </div>"""

    total_sleep_h, total_sleep_m = _fmt_hm_clock(sleep.get("total_sleep_hrs"))
    need_h, need_m = _fmt_hm_clock(sleep.get("sleep_need_hrs"))
    need_txt = f"of {need_h}h{need_m:02d} need" if need_h is not None else ""
    stages = [
        ("Deep", sleep.get("deep_sleep_hrs"), sleep.get("deep_pct"), "#2f4a9e"),
        ("Light", sleep.get("light_sleep_hrs"), sleep.get("light_pct"), "#6f9ce8"),
        ("REM", sleep.get("rem_sleep_hrs"), sleep.get("rem_pct"), "#a07fe0"),
        ("Awake", sleep.get("awake_hrs"), None, "#d9a441"),
    ]
    stage_bars, stage_legend = "", ""
    for label, hrs, pct, color in stages:
        if hrs is None:
            continue
        pct = pct if pct is not None else 0
        h, m = _fmt_hm_clock(hrs)
        stage_bars += (f'<div style="width:{max(pct,3)}%;background:{color};position:relative">'
                       f'<div style="position:absolute;inset:auto 0 4px 0;text-align:center;font-size:9px;color:#e9e9ed">{round(pct)}%</div></div>')
        stage_legend += (f'<span><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{color};margin-right:4px"></span>'
                         f'{label} {h}h{m:02d}</span>')
    sleep_stats = [("Avg HR", _num(sleep.get("avg_hr"))), ("HRV", _num(sleep.get("avg_hrv"), " ms")),
                   ("Respiration", _num(sleep.get("avg_respiration"))), ("Awakenings", _num(sleep.get("awake_count")))]
    sleep_stats_html = "".join(
        f'<div><div style="font-size:10px;color:var(--color-neutral-500)">{k}</div>'
        f'<div style="font-family:var(--font-heading);font-size:17px">{v}</div></div>'
        for k, v in sleep_stats
    )
    sleep_card = f"""
    <div>
      <div class="section-title">Last night</div>
      <div class="card" style="padding:16px;gap:14px">
        <div style="display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap">
          <div style="display:flex;align-items:baseline;gap:8px">
            <div style="font-family:var(--font-heading);font-size:34px;line-height:1">{total_sleep_h if total_sleep_h is not None else "&mdash;"}<span style="font-size:17px;color:var(--color-neutral-500)">h</span>{f"{total_sleep_m:02d}" if total_sleep_m is not None else ""}</div>
            <div style="font-size:11px;color:var(--color-neutral-500)">{need_txt}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <div style="font-size:11px;color:var(--color-neutral-400)">Score</div>
            <div style="font-family:var(--font-heading);font-size:20px;color:#6f9ce8">{_num(sleep.get("sleep_score"))}</div>
          </div>
        </div>
        <div style="display:flex;height:34px;gap:2px">{stage_bars or '<div class="muted" style="font-size:12px">No sleep data.</div>'}</div>
        <div style="display:flex;flex-wrap:wrap;gap:12px;font-size:10px;color:var(--color-neutral-500)">{stage_legend}</div>
        <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;
            border-top:1px solid var(--color-divider);padding-top:12px">{sleep_stats_html}</div>
      </div>
    </div>"""

    week_activities = week.get("activities") or []
    load_by_wd = [0.0] * 7
    for a in week_activities:
        try:
            wd = date.fromisoformat(str(a.get("date"))[:10]).weekday()
            load_by_wd[wd] += a.get("training_load") or 0
        except ValueError:
            continue
    max_load = max(load_by_wd) or 1
    today_wd = date.fromisoformat(today).weekday() if today else -1
    wd_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    week_bars = "".join(
        f'<div class="js-bar" data-date="{html.escape(wd)}" data-value="{f"{round(v):,} load" if v else "No activity"}" '
        f'style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:6px;height:100%">'
        f'<div style="width:100%;border-radius:5px 5px 2px 2px;height:{max(round(v/max_load*100),3) if v else 3}px;'
        f'background:{"var(--color-accent)" if i==today_wd else ("var(--color-accent-700)" if v else "var(--color-neutral-800)")};min-height:3px"></div>'
        f'<div style="font-size:10px;color:{"var(--color-accent-200)" if i==today_wd else "var(--color-neutral-600)"}">{wd}</div></div>'
        for i, (wd, v) in enumerate(zip(wd_labels, load_by_wd))
    )
    week_card = f"""
    <div>
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
        <div class="section-title" style="margin-bottom:0">This week's load</div>
        <div style="font-size:11px;color:var(--color-neutral-500)">{_num(week.get("total_training_load"))} · {_num(week.get("total_activities"))} activities</div>
      </div>
      <div class="card" style="padding:14px">
        <div style="display:flex;align-items:flex-end;gap:6px;height:92px">{week_bars or '<div class="muted" style="font-size:12px">No activity this week.</div>'}</div>
      </div>
    </div>""" if week else ""

    today_acts = [a for a in activities if str(a.get("date") or "")[:10] == today][:3]
    act_rows = "".join(_activity_row_compact(a) for a in today_acts)
    today_acts_card = f"""
    <div>
      <div class="section-title">Today &middot; {len(today_acts)} {'activity' if len(today_acts)==1 else 'activities'}</div>
      <div style="display:flex;flex-direction:column;gap:8px">{act_rows or '<div class="muted" style="font-size:13px">No activities logged yet today.</div>'}</div>
    </div>""" if today_acts or activities is not None else ""

    stress_card = _stress_breakdown_card(health)

    return (
        '<section class="panel tabpanel tp-today" style="flex-direction:column;gap:22px">'
        f"{hero}{quick_cards}{stress_card}{sleep_card}{week_card}{today_acts_card}"
        "</section>"
    )


def _activity_row_compact(a: dict) -> str:
    icon, tint = _sport_style(a.get("type"))
    big, sub = _activity_big_stat(a)
    return f"""
    <div class="card" style="padding:12px;flex-direction:row;align-items:center;gap:12px">
      <div style="width:34px;height:34px;flex:0 0 auto;border-radius:9px;display:grid;place-items:center;
          background:color-mix(in srgb, {tint} 18%, transparent);color:{tint};font-size:18px"><i class="ph">{icon}</i></div>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_e(a.get("name"))}</div>
        <div style="font-size:11px;color:var(--color-neutral-500)">{_e(_short_date(a.get("date")))}</div>
      </div>
      <div style="text-align:right">
        <div style="font-family:var(--font-heading);font-size:15px">{big}</div>
        <div style="font-size:10px;color:var(--color-neutral-500)">{sub}</div>
      </div>
    </div>"""


# ── PANEL: TRENDS ────────────────────────────────────────────────────────────

_CHART_SPECS = [
    ("hrv", "HRV", "ms", False, False, "#7fc9b0", "url(#gHrv)"),
    ("rhr", "Resting HR", "bpm", True, False, "#cf5a4e", "url(#gRhr)"),
    ("sleep_score", "Sleep score", "/100", False, False, "#6f9ce8", "url(#gSleep)"),
    ("training_load", "Acute load", "", False, True, "var(--color-accent)", "url(#gArea)"),
    ("stress", "Stress", "avg", True, False, "#d9a441", "url(#gStress)"),
    ("steps", "Steps", "", False, True, "#4fae72", "url(#gSteps)"),
]


def _chart_card_html(c: dict) -> str:
    return f"""
    <div class="card" style="padding:13px;gap:7px">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
        <div class="kicker">{html.escape(c["label"])}</div>
        <div style="font-size:11px;color:{c["deltaColor"]}">{c["delta"]}</div>
      </div>
      <div style="display:flex;align-items:baseline;gap:5px">
        <div style="font-family:var(--font-heading);font-size:26px;line-height:1">{c["value"]}</div>
        <div style="font-size:11px;color:var(--color-neutral-500)">{html.escape(c["unit"])}</div>
      </div>
      <svg class="js-linechart" id="{c["id"]}" data-points="{c["points_json"]}"
          viewBox="0 0 300 78" preserveAspectRatio="none" style="width:100%;height:66px;display:block">
        <path d="{c["area"]}" fill="{c["fill"]}"></path>
        <line x1="0" x2="300" y1="{c["avgY"]}" y2="{c["avgY"]}" stroke="var(--color-neutral-700)" stroke-width="1" stroke-dasharray="3 5" vector-effect="non-scaling-stroke"></line>
        <path d="{c["line"]}" fill="none" stroke="{c["stroke"]}" stroke-width="1.7" vector-effect="non-scaling-stroke" stroke-linejoin="round"></path>
        <line class="chart-crosshair" x1="0" x2="0" y1="0" y2="78" stroke="var(--color-neutral-400)" stroke-width="1" vector-effect="non-scaling-stroke" style="opacity:0;pointer-events:none"></line>
        <circle class="chart-dot" cx="0" cy="0" r="3" fill="{c["stroke"]}" stroke="var(--color-bg)" stroke-width="1.5" style="opacity:0;pointer-events:none"></circle>
        <rect class="chart-hit" x="0" y="0" width="300" height="78" style="fill:transparent;pointer-events:all;cursor:crosshair"></rect>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
        <span>{c["lo"]}</span><span>avg {c["avg"]}</span><span>{c["hi"]}</span>
      </div>
    </div>"""


def _panel_trends(data: dict) -> str:
    trends = data.get("trends")
    if not trends:
        err = data.get("trends_err") or "no data"
        return f'<section class="panel tabpanel tp-trends"><div class="err">Trends unavailable — {_e(err)}</div></section>'

    metrics = trends.get("metrics") or {}
    available_days = trends.get("days") or 30
    ranges = [r for r in (7, 14, 30, 42, 90) if r <= available_days] or [available_days]
    default_range = max(ranges)

    range_pills = "".join(
        f'<label for="range-{r}">{"3 months" if r == 90 else f"{r}d"}</label>' for r in ranges
    )
    range_inputs = "".join(
        f'<input class="hide" type="radio" name="range" id="range-{r}"{" checked" if r == default_range else ""}>'
        for r in ranges
    )
    range_sets = ""
    for r in ranges:
        cards = "".join(
            _chart_card_html(chart) for chart in (
                _chart(metrics.get(key), label, unit, lower_better, r, big=big, stroke=stroke, fill=fill,
                       chart_id=f"lc-{key}-{r}")
                for key, label, unit, lower_better, big, stroke, fill in _CHART_SPECS
            ) if chart is not None
        )
        range_sets += (
            f'<div class="range-set rs-{r}" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">'
            f"{cards}</div>"
        )

    ts = data.get("training_status") or {}
    acwr = ts.get("acwr")
    acwr_pct = _acwr_gauge_pct(acwr)
    acwr_color = "#4fae72" if acwr_pct is not None and 26 <= acwr_pct <= 66 else ("#d9a441" if acwr_pct is not None else "var(--color-neutral-500)")
    acwr_card = ""
    if acwr is not None:
        acwr_card = f"""
        <div class="card" style="padding:16px;gap:12px">
          <div style="display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap">
            <div><div class="kicker">Acute : chronic load</div>
              <div style="display:flex;align-items:baseline;gap:8px;margin-top:3px">
                <div style="font-family:var(--font-heading);font-size:30px;line-height:1;color:{acwr_color}">{acwr:.2f}</div>
                <div style="font-size:12px;color:{acwr_color}">{_label(ts.get("acwr_status"))}</div>
              </div>
            </div>
          </div>
          <div style="position:relative;height:30px">
            <div style="position:absolute;inset:12px 0 auto 0;height:7px;border-radius:999px;display:flex;overflow:hidden">
              <div style="width:26%;background:#5b8fd8"></div><div style="width:14%;background:#7fc9b0"></div>
              <div style="width:26%;background:#4fae72"></div><div style="width:14%;background:#d9a441"></div>
              <div style="width:20%;background:#cf5a4e"></div>
            </div>
            <div style="position:absolute;left:{acwr_pct:.1f}%;top:4px;width:3px;height:23px;border-radius:2px;
                background:var(--color-neutral-100);box-shadow:0 0 0 2px var(--color-surface)"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
            <span>Detraining</span><span>0.8</span><span>Optimal</span><span>1.3</span><span>High risk</span>
          </div>
        </div>"""

    step_series = metrics.get("steps") or {}
    step_daily = (step_series.get("daily") or [])[-14:]
    step_goal = _step_goal(data)
    step_vals = [p.get("value") or 0 for p in step_daily]
    smax = max(step_vals) if step_vals else 1
    step_bars = "".join(
        f'<div class="js-bar" data-date="{_e(_short_date(p.get("date")))}" data-value="{_e(p.get("value"))} steps" '
        f'style="flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end">'
        f'<div style="width:100%;border-radius:3px;'
        f'height:{max(4, (p.get("value") or 0) / (smax or 1) * 100):.1f}%;'
        f'background:{"#4fae72" if (p.get("value") or 0) >= step_goal else "var(--color-neutral-700)"}"></div></div>'
        for p in step_daily
    )
    steps_card = ""
    if step_daily:
        steps_card = f"""
        <div class="card" style="padding:14px;gap:12px">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <div class="kicker">Daily steps</div><div style="font-size:11px;color:var(--color-neutral-500)">goal {step_goal:,}</div>
          </div>
          <div style="display:flex;align-items:flex-end;gap:3px;height:110px">{step_bars}</div>
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
            <span>{_e(_short_date(step_daily[0].get("date")))}</span><span>{_e(_short_date(step_daily[-1].get("date")))}</span>
          </div>
        </div>"""

    return f"""
    <section class="panel tabpanel tp-trends" style="flex-direction:column;gap:16px">
      {range_inputs}
      <div class="rangebar" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div style="font-family:var(--font-heading);font-size:20px">Trends</div>
        <div class="pillbar">{range_pills}</div>
      </div>
      {acwr_card}
      <div class="range-body">{range_sets}</div>
      {steps_card}
    </section>"""


# ── PANEL: ACTIVITY ──────────────────────────────────────────────────────────

_ACTIVITY_FILTERS = ("all", "triathlon", "bike", "run", "strength", "other")
_BIKE_TYPES = {"cycling", "road_biking", "virtual_ride", "indoor_cycling"}
_RUN_TYPES = {"running"}
_SWIM_TYPES = {"lap_swimming", "open_water_swimming", "swimming"}
_TRIATHLON_TYPES = {"multi_sport", "triathlon", "duathlon"}
_STRENGTH_TYPES = {"strength", "strength_training"}


def _activity_filter_key(activity: dict) -> str:
  activity_type = str(activity.get("type") or "").lower()
  if activity_type in _STRENGTH_TYPES:
    return "strength"
  if activity_type in _TRIATHLON_TYPES:
    return "triathlon"
  if activity_type in _BIKE_TYPES:
    return "bike"
  if activity_type in _RUN_TYPES:
    return "run"
  if activity_type in _SWIM_TYPES:
    return "other"
  return "other"


def _activity_filter_matches(activity: dict, filter_key: str) -> bool:
  activity_type = str(activity.get("type") or "").lower()
  if filter_key == "all":
    return True
  if filter_key == "triathlon":
    return activity_type in (_TRIATHLON_TYPES | _BIKE_TYPES | _RUN_TYPES | _SWIM_TYPES)
  return _activity_filter_key(activity) == filter_key


def _activity_summary_card(activities: list[dict], split: list[tuple], summary: dict | None = None) -> str:
  summary = summary or {}
  total_distance = summary.get("total_distance_km", sum(a.get("distance_km") or 0 for a in activities))
  total_duration = summary.get("total_duration_min", sum(a.get("duration_min") or 0 for a in activities))
  total_load = summary.get("total_training_load", sum(a.get("training_load") or 0 for a in activities))
  count = summary.get("total_activities", len(activities))
  avg_load = total_load / count if count else None
  total_dur = total_duration or 0
  palette = ["#4fae72", "#d9a441", "#e2734a", "#4aa7d8", "#a07fe0", "#9397ab"]
  split_items = []
  for i, (label, duration) in enumerate(sorted(split, key=lambda item: -item[1])):
    pct = duration / total_dur * 100 if total_dur else 0
    color = palette[i % len(palette)]
    split_items.append((label, pct, color))
  split_bars = "".join(
    f'<div title="{html.escape(label)}" style="width:{pct:.1f}%;background:{color}"></div>'
    for label, pct, color in split_items
  )
  split_legend = "".join(
    f'<span style="display:flex;align-items:center;gap:5px"><span style="width:7px;height:7px;border-radius:2px;background:{color}"></span>{html.escape(label)}</span>'
    for label, pct, color in split_items
  )
  return f"""
  <div class="card" style="padding:16px;gap:14px">
    <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
    <div><div class="kicker">Distance</div><div style="font-family:var(--font-heading);font-size:24px">{_trim(total_distance)}<span style="font-size:12px;color:var(--color-neutral-500)"> km</span></div></div>
    <div><div class="kicker">Time</div><div style="font-family:var(--font-heading);font-size:24px">{_fmt_dur(total_duration)}</div></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px">
    <div><div class="kicker">Load</div><div style="font-family:var(--font-heading);font-size:24px">{_num(round(total_load))}</div></div>
    <div><div class="kicker">Sessions</div><div style="font-family:var(--font-heading);font-size:24px">{_num(count)}</div></div>
    <div><div class="kicker">Avg load/session</div><div style="font-family:var(--font-heading);font-size:20px">{_num(round(avg_load, 1) if avg_load is not None else None)}</div></div>
    </div>
    <div style="display:flex;height:10px;gap:2px;border-radius:999px;overflow:hidden">{split_bars}</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px 14px;font-size:10px;color:var(--color-neutral-500)">{split_legend}</div>
  </div>"""


def _panel_activity(data: dict) -> str:
    week = data.get("week")
    activities = data.get("activities")
    if not week and not activities:
        err = data.get("week_err") or data.get("activities_err") or "no data"
        return f'<section class="panel tabpanel tp-activity"><div class="err">Activity data unavailable — {_e(err)}</div></section>'

    week = week or {}

    week_start = str(week.get("week_start") or "")[:10]
    week_end = str(week.get("week_end") or "")[:10]
    week_activities = [
      a for a in (activities or [])
      if week_start <= str(a.get("date") or "")[:10] <= week_end
    ]
    filter_labels = "".join(
      f'<label for="activity-filter-{key}">{key.title()}</label>'
      for key in _ACTIVITY_FILTERS
    )
    all_source = week.get("activities") or []
    summary_source = all_source or week_activities
    sections = []
    for key in _ACTIVITY_FILTERS:
      selected_summary = summary_source if key == "all" else [a for a in summary_source if _activity_filter_matches(a, key)]
      selected_list = [a for a in week_activities if _activity_filter_matches(a, key)]
      split = []
      if key == "all":
        split = [
          (f"{_sport_label(activity_type)} {_fmt_dur(values.get('duration_min'))}", values.get("duration_min") or 0)
          for activity_type, values in (week.get("by_type") or {}).items()
        ]
      else:
        for activity in selected_summary:
          label = _sport_label(activity.get("type"))
          existing = next((index for index, item in enumerate(split) if item[0] == label), None)
          duration = activity.get("duration_min") or 0
          if existing is None:
            split.append((label, duration))
          else:
            split[existing] = (label, split[existing][1] + duration)
      max_load = max((a.get("training_load") or 0) for a in selected_list) or 1 if selected_list else 1
      cards = "".join(_activity_row_expandable(a, max_load) for a in selected_list)
      empty = '<div class="muted" style="font-size:13px">No activities for this filter this week.</div>'
      view_more = '<button type="button" class="btn btn-secondary" style="align-self:center" disabled>View More</button>' if selected_list else ""
      sections.append(
        f'<div class="activity-filter-section activity-filter-{key}" style="flex-direction:column;gap:8px">'
        f'{_activity_summary_card(selected_summary, split, week if key == "all" else None)}{cards or empty}{view_more}</div>'
      )

    return f"""
    <section class="panel tabpanel tp-activity" style="flex-direction:column;gap:16px">
      <div style="font-family:var(--font-heading);font-size:20px">Activity</div>
      <div class="activity-filterbar pillbar">{filter_labels}</div>
      {"".join(sections)}
    </section>"""


def _activity_row_expandable(a: dict, max_load: float) -> str:
    icon, tint = _sport_style(a.get("type"))
    big, sub = _activity_big_stat(a)
    load = a.get("training_load") or 0
    load_w = max(2, round(load / max_load * 100))
    detail = _activity_detail(a)
    detail_html = "".join(
        f'<div><div style="font-size:10px;color:var(--color-neutral-500)">{html.escape(k)}</div>'
        f'<div style="font-family:var(--font-heading);font-size:15px">{v}</div></div>'
        for k, v in detail
    )
    return f"""
    <details class="actcard">
      <summary>
        <div style="display:flex;align-items:center;gap:12px">
          <div style="width:34px;height:34px;flex:0 0 auto;border-radius:9px;display:grid;place-items:center;
              background:color-mix(in srgb, {tint} 18%, transparent);color:{tint};font-size:18px"><i class="ph">{icon}</i></div>
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_e(a.get("name"))}</div>
            <div style="font-size:11px;color:var(--color-neutral-500)">{_e(_short_date(a.get("date")))}</div>
          </div>
          <div style="text-align:right;flex:0 0 auto">
            <div style="font-family:var(--font-heading);font-size:15px">{big}</div>
            <div style="font-size:10px;color:var(--color-neutral-500)">{sub}</div>
          </div>
        </div>
        <div style="margin-top:10px;height:3px;border-radius:999px;background:var(--color-neutral-800);overflow:hidden">
          <div style="height:100%;width:{load_w}%;background:{tint};border-radius:999px"></div>
        </div>
      </summary>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(72px,1fr));gap:10px;margin-top:12px;
          border-top:1px solid var(--color-divider);padding-top:10px">{detail_html or '<div class="muted" style="font-size:12px">No additional detail.</div>'}</div>
    </details>"""


# ── PANEL: FITNESS ───────────────────────────────────────────────────────────

def _panel_fitness(data: dict) -> str:
    ts = data.get("training_status") or {}
    athlete = data.get("athlete") or {}
    records = data.get("personal_records")

    vo2 = ts.get("vo2max") or {}
    run_v2, bike_v2 = vo2.get("running"), vo2.get("cycling")
    run_pos = max(0, min(100, ((run_v2 or 30) - 30) / 40 * 100))
    bike_pos = max(0, min(100, ((bike_v2 or 30) - 30) / 40 * 100))
    vo2_card = f"""
    <div class="card" style="padding:16px;gap:12px">
      <div class="kicker">VO&#8322; max</div>
      <div style="display:flex;gap:22px;align-items:flex-end">
        <div><div style="font-family:var(--font-heading);font-size:32px;line-height:1">{_num(run_v2)}</div>
        <div style="font-size:11px;color:var(--color-neutral-500)"><i class="ph">{_RUN}</i> run</div></div>
        <div><div style="font-family:var(--font-heading);font-size:32px;line-height:1">{_num(bike_v2)}</div>
        <div style="font-size:11px;color:var(--color-neutral-500)"><i class="ph">{_BIKE}</i> bike</div></div>
      </div>
      <div style="position:relative;height:22px">
        <div style="position:absolute;left:0;right:0;top:9px;height:5px;border-radius:999px;
            background:linear-gradient(90deg,var(--color-neutral-800),var(--color-accent-700),var(--color-accent))"></div>
        <div style="position:absolute;left:{run_pos:.0f}%;top:3px;width:2px;height:17px;background:var(--color-neutral-200);border-radius:2px"></div>
        <div style="position:absolute;left:{bike_pos:.0f}%;top:3px;width:2px;height:17px;background:var(--color-accent-300);border-radius:2px"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)"><span>30</span><span>70</span></div>
    </div>"""

    thresholds = [
        ("LTHR", _num(athlete.get("lactate_threshold_hr"))),
        ("LT pace", f"{athlete['lactate_threshold_pace']:.2f} /km" if athlete.get("lactate_threshold_pace") else "&mdash;"),
        ("FTP", _num(athlete.get("ftp"), " W")),
        ("Weight", _num(athlete.get("weight_kg"), " kg")),
    ]
    thresholds_html = "".join(
        f'<div><div style="font-family:var(--font-heading);font-size:21px">{v}</div>'
        f'<div style="font-size:10px;color:var(--color-neutral-500)">{k}</div></div>'
        for k, v in thresholds
    )
    thresholds_card = f"""
    <div class="card" style="padding:16px;gap:10px">
      <div class="kicker">Thresholds</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(84px,1fr));gap:12px">{thresholds_html}</div>
    </div>"""

    lthr = athlete.get("lactate_threshold_hr")
    zones_card = ""
    if lthr:
        bounds = [0, 0.80, 0.90, 0.95, 1.00, 1.10]
        widths = [30, 48, 66, 84, 100]
        zone_rows = ""
        for i in range(5):
            lo_bpm = round(lthr * bounds[i]) if i > 0 else None
            hi_bpm = round(lthr * bounds[i + 1]) if i < 4 else None
            rng = f"< {round(lthr * bounds[1])}" if i == 0 else (f"{lo_bpm}+" if i == 4 else f"{lo_bpm}–{hi_bpm}")
            color = ["#9397ab", "#5b8fd8", "#4fae72", "#d9a441", "#cf5a4e"][i]
            zone_rows += (
                f'<div style="display:grid;grid-template-columns:56px 1fr 62px;align-items:center;gap:10px">'
                f'<div style="font-size:11px;color:var(--color-neutral-400)">Z{i+1}</div>'
                f'<div style="height:8px;border-radius:999px;background:var(--color-neutral-800);overflow:hidden">'
                f'<div style="height:100%;width:{widths[i]}%;background:{color};border-radius:999px"></div></div>'
                f'<div style="font-size:11px;text-align:right;color:var(--color-neutral-500)">{rng}</div></div>'
            )
        zones_card = f"""
        <div>
          <div class="section-title">Heart-rate zones</div>
          <div class="card" style="padding:14px;gap:8px">{zone_rows}</div>
        </div>"""

    pr_html = ""
    if records:
        sport_meta = {"running": ("Running", _RUN), "cycling": ("Cycling", _BIKE), "swimming": ("Swimming", _SWIM)}
        groups_html = ""
        for cat, (label, icon) in sport_meta.items():
            items = records.get(cat) or []
            if not items:
                continue
            item_cards = "".join(
                f'<div class="card" style="padding:12px;gap:4px">'
                f'<div style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--color-neutral-500)">{_e(p.get("label"))}</div>'
                f'<div style="font-family:var(--font-heading);font-size:21px;line-height:1.1">{_e(p.get("value_formatted"))}</div>'
                f'<div style="font-size:10px;color:var(--color-neutral-600)">{_month_year(p.get("date"))}</div></div>'
                for p in items
            )
            groups_html += f"""
            <div class="pr-group pr-{cat}">
              <div style="display:flex;align-items:center;gap:7px;margin-bottom:7px;color:var(--color-accent-300)">
                <i class="ph" style="font-size:15px">{icon}</i>
                <span style="font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--color-neutral-400)">{label}</span>
                <span style="flex:1;height:1px;background:linear-gradient(90deg,var(--color-divider),transparent)"></span>
              </div>
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">{item_cards}</div>
            </div>"""
        pr_html = f"""
        <div>
          <input class="hide" type="radio" name="prf" id="prf-all" checked><input class="hide" type="radio" name="prf" id="prf-run">
          <input class="hide" type="radio" name="prf" id="prf-bike"><input class="hide" type="radio" name="prf" id="prf-swim">
          <div class="prbar" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px">
            <div class="section-title" style="margin-bottom:0">Personal records</div>
            <div class="pillbar">
              <label for="prf-all"><i class="ph" style="font-size:13px">&#xe67e;</i>All</label>
              <label for="prf-run"><i class="ph" style="font-size:13px">{_RUN}</i>Run</label>
              <label for="prf-bike"><i class="ph" style="font-size:13px">{_BIKE}</i>Bike</label>
              <label for="prf-swim"><i class="ph" style="font-size:13px">{_SWIM}</i>Swim</label>
            </div>
          </div>
          <div class="pr-body" style="display:flex;flex-direction:column;gap:14px">{groups_html}</div>
        </div>"""

    return f"""
    <section class="panel tabpanel tp-you" style="flex-direction:column;gap:16px">
      <div style="font-family:var(--font-heading);font-size:20px">Fitness</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px">{vo2_card}{thresholds_card}</div>
      {zones_card}
      {pr_html}
    </section>"""


# ── PANEL: GEAR ──────────────────────────────────────────────────────────────
# Bike component maintenance tracking (issue 53) — Garmin's own gear distance
# joined with the local gear-tracker database (tools/gear_tracker.py). The
# API routes it posts to (/api/gear/maintenance, /api/gear/components) are
# served by that module's Starlette sub-app, mounted alongside /dashboard.

_GEAR_ACTIONS = ("lubed", "replaced", "serviced", "adjusted", "other")


def _status_color(status: str) -> str:
    from tools.gear_tracker import _STATUS_COLOR
    return _STATUS_COLOR.get(status, _STATUS_COLOR["unknown"])


def _dot_style(color: str) -> str:
    return f"display:inline-block;width:8px;height:8px;border-radius:50%;background:{color};flex:0 0 auto"


def _tint(color: str, pct: int = 14) -> str:
    """A status color mixed into the surface color, for a subtle status-tinted background."""
    return f"color-mix(in srgb, {color} {pct}%, var(--color-surface))"


def _border_tint(color: str, pct: int = 40) -> str:
    return f"color-mix(in srgb, {color} {pct}%, transparent)"


def _gear_bike_overview_card(g: dict) -> str:
    """A clickable bike card (Bikes overview) that jumps to its entry in the
    Bike Component Tracker section below via a same-page anchor link — no JS
    needed."""
    color = _status_color(g["status_indicator"])
    n = len(g["components"])
    badge = ""
    if g["status_indicator"] == "red":
        badge = (
            f'<span class="gt-badge" style="color:{color};background:{_tint(color)};'
            f'border-color:{_border_tint(color)}"><span style="{_dot_style(color)}"></span>Service due</span>'
        )
    stats = [("Distance", _fmt_km(g.get("distance_km")))]
    if g.get("duration_min"):
        stats.append(("Time", _fmt_dur(g.get("duration_min"))))
    if g.get("total_activities") is not None:
        stats.append(("Rides", str(g["total_activities"])))
    stats_html = "".join(
        f'<div style="font-size:12px;white-space:nowrap"><span style="color:var(--color-text)">{_e(v)}</span> '
        f'<span style="color:var(--color-neutral-500)">{html.escape(k)}</span></div>'
        for k, v in stats
    )
    return f"""
    <a class="card gt-bike-card" href="#bike-{_e(g.get('uuid') or '')}" style="padding:14px;gap:10px">
      <div style="font-size:15px;font-weight:600;font-family:var(--font-heading)">{_e(g.get("name"))}</div>
      <div style="display:flex;gap:14px;flex-wrap:wrap">{stats_html}</div>
      <div style="display:flex;align-items:center;justify-content:space-between;padding-top:10px;border-top:1px solid var(--color-divider)">
        <span style="font-size:12px;color:var(--color-neutral-500)">{n} component{"" if n == 1 else "s"} tracked</span>
        {badge}
      </div>
    </a>"""


def _gear_shoe_card(g: dict) -> str:
    color = _status_color(g["status_indicator"])
    max_km = g.get("max_distance_km") or 750
    pct = min(((g.get("distance_km") or 0) / max_km) * 100, 100) if max_km else 0
    distance_line = _fmt_km(g.get("distance_km"))
    if g.get("max_distance_km"):
        distance_line += f" of {_fmt_km(g['max_distance_km'])}"
    return f"""
    <div class="card" style="padding:14px;gap:10px">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:19px;line-height:1">\U0001F3C3</span>
        <div style="min-width:0">
          <div style="font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_e(g.get("name"))}</div>
          <div style="font-size:11px;color:var(--color-neutral-500)">{_e(g.get("model") or "Shoes")}</div>
        </div>
      </div>
      <div style="font-size:12px;color:var(--color-neutral-400)">{distance_line}</div>
      <div class="gt-wearbar"><div style="width:{pct:.0f}%;background:{color}"></div></div>
    </div>"""


def _gear_component_row(c: dict) -> str:
    """One row of the Bike Component Tracker table — a CSS-grid `<a>` (not a
    real `<table>` row: browsers foster-parent stray children out of a real
    `<table>`/`<tbody>`, so an anchor can't stand in for a `<tr>` there) that
    opens the component's detail/log-maintenance modal via a same-page
    `#component-<id>` anchor, shown with `:target` (see the .gear-modal CSS)
    — no JS needed."""
    color = _status_color(c["status"])
    interval = c.get("maintenance_interval_km")
    since = c.get("distance_since_km") or 0
    pct = min((since / interval) * 100, 100) if interval else 8
    interval_txt = _fmt_km(interval) if interval else "N/A"
    status_label = "N/A" if c["status"] == "unknown" else c["status"].title()
    return f"""
    <a class="gt-comp-row gt-comp-grid" href="#component-{_e(c['id'])}"
       style="background:{_tint(color)};border-left:3px solid {color}">
      <div>{_e(c["name"])}</div>
      <div style="color:var(--color-neutral-400)">{_e(c["last_serviced"])}</div>
      <div>{_fmt_km(c.get("distance_since_km"))}</div>
      <div style="color:var(--color-neutral-400)">{interval_txt}</div>
      <div style="display:flex;align-items:center;gap:8px">
        <div style="width:52px;height:6px;border-radius:99px;background:var(--color-neutral-800);overflow:hidden;flex:none">
          <div style="height:100%;border-radius:99px;background:{color};width:{pct:.0f}%"></div>
        </div>
        <span style="font-size:11px;color:{color}">{_e(status_label)}</span>
      </div>
    </a>"""


def _gear_component_modal(c: dict, bike_name: str | None, history: list[dict], token: str | None) -> str:
    """The CSS-only (`:target`) modal a component row opens: status, wear
    bar, key dates, recent maintenance, a log-maintenance form, and an
    "Edit component" disclosure for renaming/adjusting the interval."""
    color = _status_color(c["status"])
    interval = c.get("maintenance_interval_km")
    since = c.get("distance_since_km") or 0
    pct = min((since / interval) * 100, 100) if interval else 8
    interval_txt = _fmt_km(interval) if interval else "N/A"
    status_label = "N/A" if c["status"] == "unknown" else c["status"].title()
    action_options = "".join(f'<option value="{a}">{a.title()}</option>' for a in _GEAR_ACTIONS)
    today_iso = date.today().isoformat()
    interval_value = "" if interval is None else f"{interval:g}"

    linked_note = (
        '<div style="font-size:11px;color:var(--color-neutral-600)">Linked to its own '
        'Garmin-tracked gear item &mdash; distance comes from Garmin, not this bike.</div>'
        if c.get("linked_gear_uuid") else ""
    )

    history_lines = "".join(
        '<div style="font-size:12px;color:var(--color-neutral-400);padding:2px 0">'
        + _e(f'{h["date"]} · {h["action"].title()}' + (f' — {h["notes"]}' if h.get("notes") else ""))
        + "</div>"
        for h in history
    )
    history_block = f"""
        <div>
          <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--color-neutral-600);margin-bottom:4px">Recent maintenance</div>
          {history_lines}
        </div>""" if history_lines else ""

    unlink_form = ""
    if c.get("linked_gear_uuid"):
        unlink_form = f"""
          <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/components', token))}" style="margin-top:6px">
            <input type="hidden" name="component_id" value="{_e(c['id'])}">
            <input type="hidden" name="bike_uuid" value="{_e(c['bike_uuid'])}">
            <input type="hidden" name="name" value="{_e(c['name'])}">
            <input type="hidden" name="linked_gear_uuid" value="">
            <div style="font-size:11px;color:var(--color-neutral-600);width:100%">Untrack the Garmin gear link — stays on this bike, tracked manually from here on.</div>
            <button type="submit">Unlink</button>
          </form>"""

    return f"""
    <div id="component-{_e(c['id'])}" class="gear-modal">
      <a href="#gm-modal-close" class="gear-modal-backdrop" aria-label="Close"></a>
      <div class="gear-modal-dialog">
        <div>
          <div style="font-size:16px;font-weight:600;font-family:var(--font-heading)">{_e(c["name"])}</div>
          <div style="font-size:12px;color:var(--color-neutral-500);margin-top:2px">{_e(bike_name)}</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span style="{_dot_style(color)}"></span><span style="font-size:12px;color:{color}">{_e(status_label)}</span>
        </div>
        <div style="height:6px;border-radius:99px;background:var(--color-neutral-800);overflow:hidden">
          <div style="height:100%;border-radius:99px;background:{color};width:{pct:.0f}%"></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px;color:var(--color-neutral-300)">
          <div><div style="color:var(--color-neutral-500);font-size:11px;margin-bottom:2px">Last Serviced</div>{_e(c["last_serviced"])}</div>
          <div><div style="color:var(--color-neutral-500);font-size:11px;margin-bottom:2px">Distance Since</div>{_fmt_km(c.get("distance_since_km"))}</div>
          <div><div style="color:var(--color-neutral-500);font-size:11px;margin-bottom:2px">Interval</div>{interval_txt}</div>
          <div><div style="color:var(--color-neutral-500);font-size:11px;margin-bottom:2px">Installed</div>{_e(c["install_date"])}</div>
        </div>
        {linked_note}
        {history_block}
        <div style="border-top:1px solid var(--color-divider)"></div>
        <div style="font-size:13px;font-weight:600;font-family:var(--font-heading)">Log Maintenance</div>
        <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/maintenance', token))}">
          <input type="hidden" name="component_id" value="{_e(c['id'])}">
          <label>Action<select name="action">{action_options}</select></label>
          <label>Date<input type="date" name="date" value="{today_iso}"></label>
          <label>Notes<input type="text" name="notes" placeholder="optional"></label>
          <button type="submit">Log</button>
        </form>
        <details class="gear-actions">
          <summary>Edit component</summary>
          <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/components', token))}">
            <input type="hidden" name="component_id" value="{_e(c['id'])}">
            <input type="hidden" name="bike_uuid" value="{_e(c['bike_uuid'])}">
            <label>Name<input type="text" name="name" value="{_e(c['name'])}" required></label>
            <label>Install date<input type="date" name="install_date" value="{_e(c['install_date'])}"></label>
            <label>Service interval (km)<input type="number" step="1" min="0" name="maintenance_interval_km"
                value="{interval_value}" placeholder="optional"></label>
            <button type="submit">Save</button>
          </form>
          {unlink_form}
        </details>
        <a href="#gm-modal-close" style="align-self:flex-end;font-size:12px;color:var(--color-neutral-500);text-decoration:none">Close</a>
      </div>
    </div>"""


def _gear_link_component_form(g: dict, linkable_gear: list[dict], token: str | None) -> str:
    """Replaces the old free-text "Add component" form (issue 63): pick one
    of the athlete's own Garmin-tracked gear items (a chain, cassette, tires,
    ... registered as its own gear — see the gear tool) to link, so status is
    judged against that item's real tracked distance instead of a
    hand-typed/guessed one. "Custom" still creates a plain, bike-distance-based
    component for parts Garmin doesn't track individually.

    There's no Name field: a linked component is always named after the
    Garmin gear item it's linked to (carried in the option's value below, so
    the POST handler doesn't need to look it up live — see post_component),
    and a Custom one is named after the selected Type. Type also seeds the
    Service interval field's default when it's left blank.

    There's no Install date field either, for the same reason: a linked
    component's install date comes from that Garmin gear item's own
    ``dateBegin`` — also carried in the option's value — falling back to
    today when Garmin doesn't have one (see post_component). A Custom
    component still just defaults to today, same as before.
    """
    from tools.gear_tracker import COMPONENT_TYPES

    opts = ['<option value="">Custom (not tracked in Garmin)</option>']
    for lg in linkable_gear:
        gear_name = lg.get("name") or "Unnamed gear"
        label = gear_name
        if lg.get("distance_km") is not None:
            label += f" — {_fmt_km(lg['distance_km'])}"
        value = f"{lg['uuid']}:{gear_name}:{lg.get('date_begin') or ''}"
        opts.append(f'<option value="{_e(value)}">{_e(label)}</option>')
    options_html = "".join(opts)
    type_options = "".join(f'<option value="{t}">{t}</option>' for t in COMPONENT_TYPES)
    return f"""
    <details class="gear-actions" style="margin-top:4px">
      <summary>+ Link component</summary>
      <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/components', token))}">
        <input type="hidden" name="bike_uuid" value="{_e(g.get('uuid') or '')}">
        <input type="hidden" name="bike_name" value="{_e(g.get('name') or '')}">
        <label>Garmin gear<select name="linked_gear_uuid">{options_html}</select></label>
        <label>Type<select name="component_type">{type_options}</select></label>
        <label>Service interval (km)<input type="number" step="1" min="0" name="maintenance_interval_km" placeholder="optional"></label>
        <button type="submit">Link</button>
      </form>
    </details>"""


def _gear_bike_block(g: dict, linkable_gear: list[dict], token: str | None, is_first: bool) -> str:
    """One bike's entry in the Bike Component Tracker — an expandable
    `<details>` (open by default for the first bike, or any bike needing
    attention) holding its component rows and the Link-component form."""
    from tools.gear_tracker import list_maintenance_log

    components = g["components"]
    is_open = is_first or g["status_indicator"] == "red"

    if components:
        rows_html = "".join(_gear_component_row(c) for c in components)
        modals_html = "".join(
            _gear_component_modal(
                c, g.get("name"), list_maintenance_log(component_id=c["id"], limit=3), token,
            )
            for c in components
        )
        list_html = f"""
        <div style="overflow-x:auto">
          <div style="min-width:620px">
            <div class="gt-comp-grid" style="padding:4px 10px 8px;font-size:10px;text-transform:uppercase;
                letter-spacing:.05em;color:var(--color-neutral-500)">
              <div>Component</div><div>Last Serviced</div><div>Distance Since</div><div>Interval</div><div>Status</div>
            </div>
            <div style="display:flex;flex-direction:column;gap:4px">{rows_html}</div>
          </div>
        </div>
        {modals_html}"""
    else:
        list_html = '<div class="muted" style="font-size:12px">No components tracked yet — link one below.</div>'

    return f"""
    <details class="gt-bike" id="bike-{_e(g.get('uuid') or '')}"{" open" if is_open else ""}>
      <summary>
        <span style="display:flex;align-items:center;gap:10px">
          <span style="{_dot_style(_status_color(g["status_indicator"]))}"></span>
          <span style="font-size:15px;font-weight:600;font-family:var(--font-heading)">{_e(g.get("name"))}</span>
        </span>
      </summary>
      <div style="padding:0 14px 14px;display:flex;flex-direction:column;gap:6px">
        {list_html}
        {_gear_link_component_form(g, linkable_gear, token)}
      </div>
    </details>"""


def _gear_history_section(log_entries: list[dict], bike_names: dict) -> str:
    """Maintenance History — a table with a CSS-only (radio-driven, matching
    the tab/range-toggle pattern above) "Show all"/"Show less" beyond the
    first 5 rows, so long histories don't dominate the tab by default."""
    if not log_entries:
        return """
    <div>
      <div class="section-title">Maintenance History</div>
      <div class="muted" style="font-size:13px">No maintenance logged yet.</div>
    </div>"""

    visible_cap = 5
    rows = []
    for i, entry in enumerate(log_entries):
        bike_name = bike_names.get(entry.get("bike_uuid")) or entry.get("bike_name")
        cls = ' class="gt-hist-extra"' if i >= visible_cap else ""
        rows.append(f"""
        <tr{cls}>
          <td style="color:var(--color-neutral-400);white-space:nowrap">{_e(entry["date"])}</td>
          <td>{_e(bike_name)}</td>
          <td>{_e(entry.get("component_name"))}</td>
          <td>{_e((entry.get("action") or "").title())}</td>
          <td style="color:var(--color-neutral-500)">{_e(entry.get("notes") or "—")}</td>
        </tr>""")
    rows_html = "".join(rows)

    toggle_inputs, toggle_labels = "", ""
    if len(log_entries) > visible_cap:
        toggle_inputs = (
            '<input class="hide" type="radio" name="gt-hist" id="gt-hist-lim" checked>'
            '<input class="hide" type="radio" name="gt-hist" id="gt-hist-all">'
        )
        toggle_labels = (
            '<label class="gt-hist-toggle" for="gt-hist-all">Show all</label>'
            '<label class="gt-hist-toggle" for="gt-hist-lim">Show less</label>'
        )

    return f"""
    {toggle_inputs}
    <div class="gt-history">
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
        <div class="section-title" style="margin-bottom:0">Maintenance History</div>
        {toggle_labels}
      </div>
      <div style="overflow-x:auto">
        <table class="gear-table">
          <thead><tr><th>Date</th><th>Bike</th><th>Component</th><th>Action</th><th>Notes</th></tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>"""


def _panel_gear(data: dict, token: str | None = None, error: str | None = None) -> str:
    gear_status = data.get("gear_status")
    if not gear_status:
        err = data.get("gear_status_err") or "no data"
        return f'<section class="panel tabpanel tp-gear"><div class="err">Gear tracker unavailable — {_e(err)}</div></section>'

    gear = gear_status.get("gear") or []
    active_gear = [g for g in gear if (g.get("status") or "active").lower() == "active"]
    bikes = [g for g in active_gear if g["is_bike"]]
    shoes = [g for g in active_gear if g["is_shoe"]]
    linkable_gear = gear_status.get("linkable_gear") or []

    error_html = (
        f'<div style="padding:10px 12px;border-radius:var(--radius-md);font-size:13px;color:#f0b3ab;'
        f'background:color-mix(in srgb, #cf5a4e 16%, var(--color-surface))">{_e(error)}</div>'
        if error else ""
    )

    bike_cards_html = "".join(_gear_bike_overview_card(g) for g in bikes) or \
        '<div class="muted" style="font-size:13px">No bikes registered yet.</div>'
    shoe_cards_html = "".join(_gear_shoe_card(g) for g in shoes) or \
        '<div class="muted" style="font-size:13px">No shoes registered yet.</div>'

    bikes_html = "".join(
        _gear_bike_block(g, linkable_gear, token, is_first=(i == 0)) for i, g in enumerate(bikes)
    ) or '<div class="muted" style="font-size:13px">No bikes registered yet.</div>'

    # The maintenance log comes from gear_status itself (build_gear_status
    # already fetched it) rather than a second gear_tracker call/connection —
    # see issue #58.
    bike_names = {g["uuid"]: g.get("name") for g in gear if g.get("uuid")}
    log_entries = gear_status.get("maintenance_log") or []
    history_html = _gear_history_section(log_entries, bike_names)

    return f"""
    <section class="panel tabpanel tp-gear" style="flex-direction:column;gap:20px">
      <div style="font-family:var(--font-heading);font-size:20px">Gear</div>
      {error_html}
      <span id="gm-modal-close"></span>
      <div>
        <div class="section-title">Bikes</div>
        <div class="gt-grid">{bike_cards_html}</div>
      </div>
      <div>
        <div class="section-title">Shoes</div>
        <div class="gt-grid">{shoe_cards_html}</div>
      </div>
      <div>
        <div class="section-title">Bike Component Tracker</div>
        {bikes_html}
      </div>
      {history_html}
    </section>"""


# ── ASSEMBLY ─────────────────────────────────────────────────────────────────

_SVG_DEFS = """
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <linearGradient id="gArea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--color-accent)" stop-opacity="0.34"></stop><stop offset="1" stop-color="var(--color-accent)" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gRhr" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#cf5a4e" stop-opacity="0.34"></stop><stop offset="1" stop-color="#cf5a4e" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gHrv" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7fc9b0" stop-opacity="0.34"></stop><stop offset="1" stop-color="#7fc9b0" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gSleep" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6f9ce8" stop-opacity="0.34"></stop><stop offset="1" stop-color="#6f9ce8" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gStress" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d9a441" stop-opacity="0.34"></stop><stop offset="1" stop-color="#d9a441" stop-opacity="0"></stop></linearGradient>
  <linearGradient id="gSteps" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4fae72" stop-opacity="0.34"></stop><stop offset="1" stop-color="#4fae72" stop-opacity="0"></stop></linearGradient>
</defs></svg>"""


def _pwa_asset_url(path: str, token: str | None) -> str:
  return f"{path}?{urlencode({'token': token})}" if token else path


def _gear_api_url(path: str, token: str | None) -> str:
    """A /api/gear/* route URL, carrying the bearer token when one was supplied."""
    return f"{path}?{urlencode({'token': token})}" if token else path


# ── CHART INTERACTIVITY (vanilla JS, no external libraries) ────────────────
# Reads data off the markup emitted above: `data-points` (JSON [{x,y,d,v}, ...]
# in viewBox coordinates) on line/area charts, and `data-date`/`data-value`
# text attributes on tappable bar segments. Drives a single shared tooltip +
# per-chart crosshair overlay; never touches the underlying data.

_CHART_JS = """
(function () {
  var tip = document.getElementById('chart-tooltip');
  if (!tip) return;
  var tipDate = tip.querySelector('.tt-date');
  var tipVal = tip.querySelector('.tt-val');
  var activeBar = null;
  var activeLine = null;

  function pointFromEvent(e) {
    if (e.touches && e.touches.length) return e.touches[0];
    if (e.changedTouches && e.changedTouches.length) return e.changedTouches[0];
    return e;
  }

  function showTip(clientX, clientY, dateText, valueText, above) {
    tipDate.textContent = dateText;
    tipVal.textContent = valueText;
    tip.style.display = 'block';
    var pad = 14;
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = clientX - tw / 2;
    var y = above ? clientY - th - pad : clientY + pad;
    if (above && y < 4) y = clientY + pad;
    if (!above && y + th > window.innerHeight) y = clientY - th - pad;
    if (x + tw > window.innerWidth) x = window.innerWidth - tw - 4;
    if (x < 4) x = 4;
    tip.style.left = x + 'px';
    tip.style.top = Math.max(4, y) + 'px';
  }

  function hideAll() {
    tip.style.display = 'none';
    if (activeLine) {
      activeLine.crosshair.style.opacity = 0;
      activeLine.dot.style.opacity = 0;
      activeLine = null;
    }
    activeBar = null;
  }

  document.querySelectorAll('svg.js-linechart').forEach(function (svg) {
    var pts;
    try { pts = JSON.parse(svg.getAttribute('data-points') || '[]'); } catch (err) { return; }
    if (!pts.length) return;
    var hit = svg.querySelector('.chart-hit');
    var crosshair = svg.querySelector('.chart-crosshair');
    var dot = svg.querySelector('.chart-dot');
    if (!hit || !crosshair || !dot) return;
    var vb = svg.viewBox.baseVal;

    function nearest(clientX) {
      var rect = svg.getBoundingClientRect();
      var vx = rect.width ? (clientX - rect.left) / rect.width * vb.width : pts[0].x;
      var best = pts[0], bestDist = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i].x - vx);
        if (d < bestDist) { bestDist = d; best = pts[i]; }
      }
      return best;
    }

    function update(e) {
      var p = pointFromEvent(e);
      var pt = nearest(p.clientX);
      crosshair.setAttribute('x1', pt.x);
      crosshair.setAttribute('x2', pt.x);
      crosshair.style.opacity = 1;
      dot.setAttribute('cx', pt.x);
      dot.setAttribute('cy', pt.y);
      dot.style.opacity = 1;
      activeBar = null;
      activeLine = { crosshair: crosshair, dot: dot };
      showTip(p.clientX, p.clientY, pt.d, pt.v, true);
    }

    hit.addEventListener('mousemove', update);
    hit.addEventListener('mousedown', update);
    hit.addEventListener('mouseleave', hideAll);
    hit.addEventListener('touchstart', function (e) { e.preventDefault(); update(e); }, { passive: false });
    hit.addEventListener('touchmove', function (e) { e.preventDefault(); update(e); }, { passive: false });
  });

  document.querySelectorAll('.js-bar').forEach(function (bar) {
    function activate(e) {
      if (activeBar === bar) { hideAll(); return; }
      hideAll();
      activeBar = bar;
      var p = pointFromEvent(e);
      showTip(p.clientX, p.clientY, bar.getAttribute('data-date') || '', bar.getAttribute('data-value') || '');
    }
    bar.addEventListener('click', activate);
    bar.addEventListener('touchstart', function (e) { e.preventDefault(); activate(e); }, { passive: false });
  });

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.js-linechart') && !e.target.closest('.js-bar')) hideAll();
  });

  window.addEventListener('scroll', hideAll, { passive: true, capture: true });
})();
"""

# Converts the server-rendered "Last sync" time (offset by the operator's
# configured DASHBOARD_TZ_OFFSET_HOURS) to the viewer's actual local
# timezone, using the UTC instant stashed in the element's data attribute.
_TZ_JS = """
document.querySelectorAll('[data-sync-utc]').forEach(function (el) {
  var d = new Date(el.getAttribute('data-sync-utc'));
  if (!isNaN(d.getTime())) {
    el.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
});
"""

# A component's modal (tools/dashboard.py's .gear-modal, shown via CSS
# :target — see _gear_component_modal) is a single, persistent DOM node
# toggled purely by visibility, not re-rendered each time it opens. Its
# "Edit component" <details> is native HTML state, so once expanded it stays
# expanded across later opens/closes within the same page load — this resets
# it back closed on every navigation between (or out of) a component modal,
# so "Edit component" is always collapsed on a fresh open.
_GEAR_MODAL_JS = """
window.addEventListener('hashchange', function () {
  document.querySelectorAll('.gear-modal .gear-actions[open]').forEach(function (d) { d.open = false; });
});
"""


def _fmt_sync_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            dt = (datetime.fromtimestamp(value / 1000, tz=timezone.utc) + timedelta(hours=_tz_offset_hours()))
            return dt.strftime("%H:%M")
        except (ValueError, OverflowError, OSError):
            return str(value)
    return str(value).replace("T", " ").split(".")[0][-8:-3] if "T" in str(value) else str(value)


def _sync_time_utc_iso(value):
    """The device's last-upload time as a UTC ISO-8601 string, for the
    browser to convert to the viewer's own local timezone client-side.

    Returns None if `value` can't be parsed — the caller then falls back to
    the server-rendered `_fmt_sync_time` text (offset by
    DASHBOARD_TZ_OFFSET_HOURS rather than the viewer's real timezone).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).split(".")[0])
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


_TAB_IDS = {
    "today": "tab-today", "trends": "tab-trends", "activity": "tab-activity",
    "fitness": "tab-you", "gear": "tab-gear",
}
_DEFAULT_TAB = "today"


def render_dashboard_html(data: dict, token: str | None = None,
                          initial_tab: str | None = None, error: str | None = None) -> str:
    """Render the dashboard data dict into a complete HTML document.

    ``token`` is threaded into the injected site navigation and every
    gear-tracker form action, so navigating or submitting never drops the
    ``?token=`` bearer auth.

    ``initial_tab`` (one of "today" (default), "trends", "activity",
    "fitness", "gear") selects which tab starts open — used so a gear-tracker
    form submission can redirect back to the Gear tab specifically rather
    than always landing on Today.

    ``error``, when given, is shown as a banner at the top of the Gear tab —
    a gear-tracker form error (see ``_error_redirect_url`` in
    tools/gear_tracker.py) redirects back here with ``?error=...``.
    """
    weekday_line = data.get("date") or ""
    try:
        weekday_line = date.fromisoformat(data["date"]).strftime("%A %-d %B")
    except (KeyError, ValueError, TypeError):
        pass

    sync = data.get("last_sync") or {}
    sync_time = _fmt_sync_time(sync.get("upload_time"))
    sync_utc_iso = _sync_time_utc_iso(sync.get("upload_time"))
    sync_attr = f' data-sync-utc="{html.escape(sync_utc_iso, quote=True)}"' if sync_utc_iso else ""
    sync_line = (
        f'Last sync <span id="sync-time"{sync_attr}>{_e(sync_time)}</span>'
        if sync_time else "Live from Garmin Connect"
    )

    refresh_meta = (
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if REFRESH_SECONDS > 0 else ""
    )

    active_tab_id = _TAB_IDS.get(initial_tab, _TAB_IDS[_DEFAULT_TAB])
    activity_filter_inputs = "".join(
      f'<input class="hide" type="radio" name="activity-filter" id="activity-filter-{key}"{" checked" if key == "all" else ""}>'
      for key in _ACTIVITY_FILTERS
    )

    panels = (_panel_today(data) + _panel_trends(data) + _panel_activity(data)
             + _panel_fitness(data) + _panel_gear(data, token, error))

    body = f"""
<div style="min-height:100vh;background:
    radial-gradient(120% 60% at 12% -10%, color-mix(in srgb, var(--color-accent) 13%, transparent), transparent 60%),
    var(--color-bg);color:var(--color-text);font-family:var(--font-body);padding-bottom:104px">
  {_SVG_DEFS}
  <div style="position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);
      background:color-mix(in srgb, var(--color-bg) 78%, transparent);border-bottom:1px solid var(--color-divider)">
    <div style="max-width:1120px;margin:0 auto;padding:11px 16px;display:flex;align-items:center;gap:12px">
      <div style="width:26px;height:26px;border-radius:50%;border:1px solid var(--color-accent);
          display:grid;place-items:center;color:var(--color-accent);font-size:15px"><i class="ph">{_PULSE}</i></div>
      <div style="flex:1;min-width:0">
        <div style="font-family:var(--font-heading);font-size:15px;line-height:1.1">{_e(weekday_line)}</div>
        <div style="font-size:11px;color:var(--color-neutral-500)">{sync_line}</div>
      </div>
    </div>
  </div>

  <input class="hide" type="radio" name="tab" id="tab-today"{" checked" if active_tab_id == "tab-today" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-trends"{" checked" if active_tab_id == "tab-trends" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-activity"{" checked" if active_tab_id == "tab-activity" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-you"{" checked" if active_tab_id == "tab-you" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-gear"{" checked" if active_tab_id == "tab-gear" else ""}>
  {activity_filter_inputs}

  <div class="tabpanels" style="max-width:1120px;margin:0 auto;padding:16px">{panels}</div>

  <div class="botnav" style="position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;justify-content:center;padding:0 16px 16px;pointer-events:none">
    <div style="pointer-events:auto;display:flex;gap:2px;padding:6px;border-radius:999px;width:min(420px,100%);
        background:color-mix(in srgb, var(--color-surface) 92%, transparent);backdrop-filter:blur(16px);box-shadow:var(--shadow-md)">
      <label for="tab-today"><i class="ph">&#xe2c2;</i><span>Today</span></label>
      <label for="tab-trends"><i class="ph">&#xe154;</i><span>Trends</span></label>
      <label for="tab-activity"><i class="ph">&#xed60;</i><span>Activity</span></label>
      <label for="tab-you"><i class="ph">&#xe2ac;</i><span>Fitness</span></label>
      <label for="tab-gear"><i class="ph">{_WRENCH}</i><span>Gear</span></label>
    </div>
  </div>

  <div id="chart-tooltip" class="chart-tooltip" role="status" aria-live="polite">
    <div class="tt-date"></div><div class="tt-val"></div>
  </div>
</div>"""

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        f'<link rel="manifest" href="{_e(_pwa_asset_url("/manifest.webmanifest", token))}">'
        f"{refresh_meta}"
        "<title>Garmin Health Dashboard</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        f"{render_nav_html('dashboard', token)}"
        f"{body}"
        f"<script>{_CHART_JS}</script>"
        f"<script>{_TZ_JS}</script>"
        f"<script>{_GEAR_MODAL_JS}</script>"
        f'<script>if ("serviceWorker" in navigator) navigator.serviceWorker.register("{_e(_pwa_asset_url("/sw.js", token))}");</script>'
        "</body></html>"
    )
