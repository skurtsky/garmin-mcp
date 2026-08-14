# tools/dashboard.py
"""Server-rendered health dashboard — "Nocturne" design.

Gathers a live overview from the Garmin client and renders it as a single,
self-contained HTML page (inline CSS, no build step, no external requests —
the Phosphor icon font is embedded as a subsetted base64 woff2, and the small
amount of interactivity below is a single inline `<script>`, no CDN/library).
Each request pulls fresh data server-side.

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
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

# Auto-refresh the browser page this often (seconds). 0 disables refresh.
REFRESH_SECONDS = int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "300"))

# The Trends tab's range toggle (7d/14d/30d) is backed by one get_trends() call
# fetched at this period; each extra day costs a handful of per-day Garmin
# lookups per metric, so this is the knob for trading trend depth for a faster
# page load. Any of get_trends' periods works; only ranges <= the fetched
# window actually show a toggle button.
TREND_PERIOD = os.environ.get("DASHBOARD_TREND_PERIOD", "1m")

# Fallback daily step goal when the athlete has no active Garmin step goal.
DEFAULT_STEP_GOAL = int(os.environ.get("DASHBOARD_STEP_GOAL", "10000"))

_TREND_METRICS = ["rhr", "hrv", "sleep_score", "stress", "steps", "training_load"]


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


def build_dashboard_data() -> dict:
    """Fetch every dashboard section from the Garmin client.

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

    readiness, readiness_err = _safe(get_daily_readiness, today)
    health, health_err = _safe(get_daily_health, today)
    sleep, sleep_err = _safe(get_sleep, today)
    training, training_err = _safe(get_training_readiness, today)
    training_status, training_status_err = _safe(get_training_status, today)
    activities, activities_err = _safe(get_activities, limit=20)
    week, week_err = _safe(get_weekly_summary)
    trends, trends_err = _safe(get_trends, period=TREND_PERIOD, metrics=_TREND_METRICS)
    personal_records, personal_records_err = _safe(get_personal_records)
    active_goals, active_goals_err = _safe(get_active_goals)
    athlete, athlete_err = _safe(get_athlete_profile)
    last_sync, last_sync_err = _safe(_fetch_last_sync)
    gear_status, gear_status_err = _safe(build_gear_status)

    return {
        "date": today,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "tz_offset_hours": _tz_offset_hours(),
        "readiness": readiness, "readiness_err": readiness_err,
        "health": health, "health_err": health_err,
        "sleep": sleep, "sleep_err": sleep_err,
        "training": training, "training_err": training_err,
        "training_status": training_status, "training_status_err": training_status_err,
        "activities": activities, "activities_err": activities_err,
        "week": week, "week_err": week_err,
        "trends": trends, "trends_err": trends_err,
        "personal_records": personal_records, "personal_records_err": personal_records_err,
        "active_goals": active_goals, "active_goals_err": active_goals_err,
        "athlete": athlete, "athlete_err": athlete_err,
        "last_sync": last_sync, "last_sync_err": last_sync_err,
        "gear_status": gear_status, "gear_status_err": gear_status_err,
    }


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
    "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"
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
.tabpanel, .range-set, .pr-group { display:none; }
#tab-today:checked ~ .tabpanels .tp-today,
#tab-trends:checked ~ .tabpanels .tp-trends,
#tab-activity:checked ~ .tabpanels .tp-activity,
#tab-you:checked ~ .tabpanels .tp-you,
#tab-gear:checked ~ .tabpanels .tp-gear { display:flex; }
#range-7:checked ~ .range-body .rs-7,
#range-14:checked ~ .range-body .rs-14,
#range-30:checked ~ .range-body .rs-30 { display:grid; }
#prf-all:checked ~ .pr-body .pr-group,
#prf-run:checked ~ .pr-body .pr-group.pr-running,
#prf-bike:checked ~ .pr-body .pr-group.pr-cycling,
#prf-swim:checked ~ .pr-body .pr-group.pr-swimming { display:block; }
#range-7:checked ~ .rangebar label[for=range-7],
#range-14:checked ~ .rangebar label[for=range-14],
#range-30:checked ~ .rangebar label[for=range-30],
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

/* ── activity expand ── */
details.actcard { padding:12px; border-radius:var(--radius-md); background:var(--color-surface);
                   box-shadow:var(--shadow-sm); }
details.actcard[open] { box-shadow:0 0 0 1px var(--color-accent-700); }
details.actcard summary { cursor:pointer; list-style:none; }
details.actcard summary::-webkit-details-marker { display:none; }

/* ── footer ── */
.footer-link { display:block; text-align:center; padding:18px 16px 0; font-size:11px;
               color:var(--color-neutral-600); text-decoration:none; }
.footer-link:hover { color:var(--color-neutral-400); }

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

    hrv_val = hrv.get("last_night_avg")
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
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(76px,1fr));gap:10px;
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
        <div style="font-size:11px;color:var(--color-neutral-500)">{_num(week.get("total_training_load"))} · {_num(week.get("total_activities"))} days</div>
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
    ranges = [r for r in (7, 14, 30) if r <= available_days] or [available_days]
    default_range = max(ranges)

    range_pills = "".join(
        f'<label for="range-{r}">{r}d</label>' for r in ranges
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

def _panel_activity(data: dict) -> str:
    week = data.get("week")
    activities = data.get("activities")
    if not week and not activities:
        err = data.get("week_err") or data.get("activities_err") or "no data"
        return f'<section class="panel tabpanel tp-activity"><div class="err">Activity data unavailable — {_e(err)}</div></section>'

    week = week or {}
    by_type = week.get("by_type") or {}
    total_dur = week.get("total_duration_min") or 0
    palette = ["#4fae72", "#d9a441", "#e2734a", "#4aa7d8", "#a07fe0", "#9397ab", "#7fc9b0", "#e0736f"]
    split = []
    for i, (t, v) in enumerate(sorted(by_type.items(), key=lambda kv: -(kv[1].get("duration_min") or 0))):
        pct = (v.get("duration_min") or 0) / total_dur * 100 if total_dur else 0
        split.append((f"{_sport_label(t)} {_fmt_dur(v.get('duration_min'))}", pct, palette[i % len(palette)]))
    split_bars = "".join(f'<div title="{html.escape(t)}" style="width:{p:.1f}%;background:{c}"></div>' for t, p, c in split)
    split_legend = "".join(
        f'<span style="display:flex;align-items:center;gap:5px"><span style="width:7px;height:7px;border-radius:2px;background:{c}"></span>{html.escape(t)}</span>'
        for t, p, c in split
    )

    totals_card = f"""
    <div class="card" style="padding:16px;gap:14px">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:12px">
        <div><div class="kicker">Distance</div><div style="font-family:var(--font-heading);font-size:24px">{_trim(week.get("total_distance_km") or 0)}<span style="font-size:12px;color:var(--color-neutral-500)"> km</span></div></div>
        <div><div class="kicker">Time</div><div style="font-family:var(--font-heading);font-size:24px">{_fmt_dur(week.get("total_duration_min"))}</div></div>
        <div><div class="kicker">Load</div><div style="font-family:var(--font-heading);font-size:24px">{_num(round(week.get("total_training_load")) if week.get("total_training_load") is not None else None)}</div></div>
        <div><div class="kicker">Sessions</div><div style="font-family:var(--font-heading);font-size:24px">{_num(week.get("total_activities"))}</div></div>
      </div>
      <div style="display:flex;height:10px;gap:2px;border-radius:999px;overflow:hidden">{split_bars or ''}</div>
      <div style="display:flex;flex-wrap:wrap;gap:10px 14px;font-size:10px;color:var(--color-neutral-500)">{split_legend}</div>
    </div>"""

    max_load = max((a.get("training_load") or 0) for a in activities) or 1 if activities else 1
    act_cards = "".join(_activity_row_expandable(a, max_load) for a in (activities or []))

    return f"""
    <section class="panel tabpanel tp-activity" style="flex-direction:column;gap:16px">
      <div style="font-family:var(--font-heading);font-size:20px">Activity</div>
      {totals_card}
      <div style="display:flex;flex-direction:column;gap:8px">{act_cards or '<div class="muted" style="font-size:13px">No recent activities.</div>'}</div>
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


def _gear_status_dot(status: str) -> str:
    from tools.gear_tracker import _STATUS_COLOR
    color = _STATUS_COLOR.get(status, _STATUS_COLOR["unknown"])
    return f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{color};flex:0 0 auto"></span>'


def _gear_overview_card(g: dict) -> str:
    stats = [("Distance", _fmt_km(g.get("distance_km")))]
    if g.get("duration_min"):
        stats.append(("Time", _fmt_dur(g.get("duration_min"))))
    stats_html = "".join(
        f'<div><div style="font-family:var(--font-heading);font-size:17px">{v}</div>'
        f'<div style="font-size:10px;color:var(--color-neutral-500)">{html.escape(k)}</div></div>'
        for k, v in stats
    )
    return f"""
    <div class="card" style="padding:14px;gap:10px">
      <div style="display:flex;align-items:center;gap:8px">
        {_gear_status_dot(g["status_indicator"])}
        <div style="min-width:0;flex:1">
          <div style="font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_e(g.get("name"))}</div>
          <div style="font-size:10px;color:var(--color-neutral-500);text-transform:uppercase;letter-spacing:.06em">{_e(g.get("activity_type") or "Gear")}</div>
        </div>
      </div>
      <div style="display:flex;gap:16px">{stats_html}</div>
    </div>"""


def _gear_component_row(c: dict, token: str | None) -> str:
    interval = c.get("maintenance_interval_km")
    interval_txt = _fmt_km(interval) if interval else "N/A"
    action_options = "".join(f'<option value="{a}">{a.title()}</option>' for a in _GEAR_ACTIONS)
    interval_value = "" if interval is None else f"{interval:g}"
    today_iso = date.today().isoformat()
    return f"""
    <tr>
      <td>{_e(c["name"])}</td>
      <td>{_e(c["last_serviced"])}</td>
      <td>{_fmt_km(c.get("distance_since_km"))}</td>
      <td>{interval_txt}</td>
      <td>{c["status_emoji"]}</td>
    </tr>
    <tr>
      <td colspan="5" style="padding-top:0;border-bottom:1px solid var(--color-divider)">
        <details class="gear-actions">
          <summary>Log maintenance</summary>
          <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/maintenance', token))}">
            <input type="hidden" name="component_id" value="{c['id']}">
            <label>Action<select name="action">{action_options}</select></label>
            <label>Date<input type="date" name="date" value="{today_iso}"></label>
            <label>Notes<input type="text" name="notes" placeholder="optional"></label>
            <button type="submit">Log</button>
          </form>
        </details>
        <details class="gear-actions">
          <summary>Edit</summary>
          <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/components', token))}">
            <input type="hidden" name="component_id" value="{c['id']}">
            <input type="hidden" name="bike_uuid" value="{_e(c['bike_uuid'])}">
            <label>Name<input type="text" name="name" value="{_e(c['name'])}" required></label>
            <label>Install date<input type="date" name="install_date" value="{_e(c['install_date'])}"></label>
            <label>Interval (km)<input type="number" step="1" min="0" name="maintenance_interval_km"
                value="{interval_value}" placeholder="N/A"></label>
            <button type="submit">Save</button>
          </form>
        </details>
      </td>
    </tr>"""


def _gear_bike_block(g: dict, token: str | None) -> str:
    rows = "".join(_gear_component_row(c, token) for c in g["components"])
    table = f"""
    <table class="gear-table">
      <thead><tr><th>Component</th><th>Last serviced</th><th>Distance since</th>
        <th>Interval</th><th>Status</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>""" if g["components"] else '<div class="muted" style="font-size:12px">No components tracked yet.</div>'

    today_iso = date.today().isoformat()
    add_form = f"""
    <details class="gear-actions" style="margin-top:8px">
      <summary>+ Add component</summary>
      <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/components', token))}">
        <input type="hidden" name="bike_uuid" value="{_e(g.get('uuid') or '')}">
        <input type="hidden" name="bike_name" value="{_e(g.get('name') or '')}">
        <label>Name<input type="text" name="name" placeholder="e.g. Chain" required></label>
        <label>Install date<input type="date" name="install_date" value="{today_iso}"></label>
        <label>Interval (km)<input type="number" step="1" min="0" name="maintenance_interval_km"
            placeholder="optional"></label>
        <button type="submit">Add</button>
      </form>
    </details>"""

    return f"""
    <div class="card" style="padding:14px;gap:10px">
      <div style="display:flex;align-items:center;gap:8px">
        {_gear_status_dot(g["status_indicator"])}
        <div style="font-size:13px;font-weight:600">{_e(g.get("name"))}</div>
        <div class="muted" style="font-size:11px">&middot; {_fmt_km(g.get("distance_km"))}</div>
      </div>
      <div style="overflow-x:auto">{table}</div>
      {add_form}
    </div>"""


def _gear_log_entry(entry: dict, bike_names: dict) -> str:
    bike_name = bike_names.get(entry.get("bike_uuid")) or entry.get("bike_name")
    notes = (f'<div style="margin-top:3px;font-size:11px;color:var(--color-neutral-400)">'
             f'{_e(entry.get("notes"))}</div>') if entry.get("notes") else ""
    return f"""
    <div class="card" style="padding:10px 12px;gap:2px">
      <div style="font-size:12px"><strong>{_e(entry.get("action", "").title())}</strong> &mdash;
        {_e(bike_name)} / {_e(entry.get("component_name"))}</div>
      <div style="font-size:10px;color:var(--color-neutral-500)">{_e(entry["date"])} &middot;
        at {_fmt_km(entry["distance_at_service_km"])}</div>
      {notes}
    </div>"""


def _gear_log_form(components: list[dict], bike_names: dict, token: str | None) -> str:
    if not components:
        return ""
    options = "".join(
        f'<option value="{c["id"]}">'
        f'{_e(bike_names.get(c.get("bike_uuid")) or c.get("bike_name"))} &mdash; {_e(c["name"])}</option>'
        for c in components
    )
    action_options = "".join(f'<option value="{a}">{a.title()}</option>' for a in _GEAR_ACTIONS)
    today_iso = date.today().isoformat()
    return f"""
    <form class="gear-form" method="post" action="{_e(_gear_api_url('/api/gear/maintenance', token))}">
      <label>Component<select name="component_id">{options}</select></label>
      <label>Action<select name="action">{action_options}</select></label>
      <label>Date<input type="date" name="date" value="{today_iso}"></label>
      <label>Notes<input type="text" name="notes" placeholder="optional"></label>
      <button type="submit">+ Log maintenance</button>
    </form>"""


def _panel_gear(data: dict, token: str | None = None) -> str:
    gear_status = data.get("gear_status")
    if not gear_status:
        err = data.get("gear_status_err") or "no data"
        return f'<section class="panel tabpanel tp-gear"><div class="err">Gear tracker unavailable — {_e(err)}</div></section>'

    gear = gear_status.get("gear") or []
    active_gear = [g for g in gear if (g.get("status") or "active").lower() == "active"]
    bikes = [g for g in active_gear if g["is_bike"]]

    cards_html = "".join(_gear_overview_card(g) for g in active_gear) or \
        '<div class="muted" style="font-size:13px">No registered gear yet — add shoes or a bike in Garmin Connect.</div>'

    bikes_html = "".join(
        f'<div style="margin-bottom:12px">{_gear_bike_block(g, token)}</div>' for g in bikes
    ) or '<div class="muted" style="font-size:13px">No bikes registered yet.</div>'

    bike_names = {g["uuid"]: g.get("name") for g in gear if g.get("uuid")}
    all_components = [c for g in bikes for c in g["components"]]

    from tools.gear_tracker import list_maintenance_log
    log_entries, log_err = _safe(list_maintenance_log, limit=50)
    log_html = "".join(_gear_log_entry(e, bike_names) for e in (log_entries or [])) or \
        f'<div class="muted" style="font-size:13px">{"No maintenance logged yet." if not log_err else f"Log unavailable — {_e(log_err)}"}</div>'

    return f"""
    <section class="panel tabpanel tp-gear" style="flex-direction:column;gap:16px">
      <div style="font-family:var(--font-heading);font-size:20px">Gear</div>
      <div>
        <div class="section-title">Overview</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px">{cards_html}</div>
      </div>
      <div>
        <div class="section-title">Bike components</div>
        {bikes_html}
      </div>
      <div>
        <div class="section-title">Maintenance log</div>
        {_gear_log_form(all_components, bike_names, token)}
        <div style="display:flex;flex-direction:column;gap:8px;margin-top:10px;max-height:340px;overflow-y:auto">{log_html}</div>
      </div>
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


def _weekly_report_url(token: str | None) -> str:
    """/weekly-summary, carrying the bearer token when one was supplied.

    /weekly-summary serves the most recent report by default, so a bare link
    to it is always "the latest weekly report."
    """
    return f"/weekly-summary?{urlencode({'token': token})}" if token else "/weekly-summary"


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


_TAB_IDS = {
    "today": "tab-today", "trends": "tab-trends", "activity": "tab-activity",
    "fitness": "tab-you", "gear": "tab-gear",
}
_DEFAULT_TAB = "today"


def render_dashboard_html(data: dict, token: str | None = None,
                          initial_tab: str | None = None) -> str:
    """Render the dashboard data dict into a complete HTML document.

    ``token`` is threaded into the footer link to the latest weekly report,
    and into every gear-tracker form action, so navigating or submitting
    never drops the ``?token=`` bearer auth.

    ``initial_tab`` (one of "today" (default), "trends", "activity",
    "fitness", "gear") selects which tab starts open — used so a gear-tracker
    form submission can redirect back to the Gear tab specifically rather
    than always landing on Today.
    """
    weekday_line = data.get("date") or ""
    try:
        weekday_line = date.fromisoformat(data["date"]).strftime("%A %-d %B")
    except (KeyError, ValueError, TypeError):
        pass

    sync = data.get("last_sync") or {}
    sync_time = _fmt_sync_time(sync.get("upload_time"))
    sync_line = f"Last sync {sync_time}" if sync_time else "Live from Garmin Connect"

    refresh_meta = (
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if REFRESH_SECONDS > 0 else ""
    )

    active_tab_id = _TAB_IDS.get(initial_tab, _TAB_IDS[_DEFAULT_TAB])

    panels = (_panel_today(data) + _panel_trends(data) + _panel_activity(data)
             + _panel_fitness(data) + _panel_gear(data, token))

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
        <div style="font-size:11px;color:var(--color-neutral-500)">{_e(sync_line)}</div>
      </div>
    </div>
  </div>

  <input class="hide" type="radio" name="tab" id="tab-today"{" checked" if active_tab_id == "tab-today" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-trends"{" checked" if active_tab_id == "tab-trends" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-activity"{" checked" if active_tab_id == "tab-activity" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-you"{" checked" if active_tab_id == "tab-you" else ""}>
  <input class="hide" type="radio" name="tab" id="tab-gear"{" checked" if active_tab_id == "tab-gear" else ""}>

  <div class="tabpanels" style="max-width:1120px;margin:0 auto;padding:16px">{panels}</div>

  <a class="footer-link" href="{_e(_weekly_report_url(token))}">Latest weekly report &rarr;</a>

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
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{refresh_meta}"
        "<title>Garmin Health Dashboard</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        f"{body}"
        f"<script>{_CHART_JS}</script>"
        "</body></html>"
    )
