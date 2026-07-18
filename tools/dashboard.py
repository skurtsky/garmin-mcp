# tools/dashboard.py
"""Server-rendered health dashboard.

Gathers a live overview from the Garmin client and renders it as a single,
self-contained HTML page (inline CSS/JS, no build step). Each request pulls
fresh data server-side.

The data-gathering entrypoint (`build_dashboard_data`) lazily imports the
underlying tool functions so that `render_dashboard_html` — a pure function of
a data dict — can be imported and exercised without a live Garmin session.
"""
import os
import html
from datetime import datetime, timedelta, timezone

# Auto-refresh the browser page this often (seconds). 0 disables refresh.
REFRESH_SECONDS = int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "300"))

# 7-day sparklines (RHR / HRV / sleep score) require a per-day trend fetch, which
# adds a handful of Garmin calls to each page load. On by default; set to "0" for
# a leaner, faster dashboard.
SPARKLINES_ENABLED = os.environ.get("DASHBOARD_SPARKLINES", "1") not in ("0", "false", "no", "")


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
        get_sleep,
        get_daily_readiness,
        get_daily_health,
        get_training_status,
        get_training_readiness,
    )
    from tools.activities import get_activities, get_weekly_summary
    from tools.trends import get_trends

    now = _local_now()
    today = now.date().isoformat()

    readiness, readiness_err = _safe(get_daily_readiness, today)
    health, health_err = _safe(get_daily_health, today)
    sleep, sleep_err = _safe(get_sleep, today)
    training, training_err = _safe(get_training_readiness, today)
    training_status, training_status_err = _safe(get_training_status, today)
    activities, activities_err = _safe(get_activities, limit=5)
    week, week_err = _safe(get_weekly_summary)
    last_sync, last_sync_err = _safe(_fetch_last_sync)

    if SPARKLINES_ENABLED:
        trends, trends_err = _safe(
            get_trends, period="7d", metrics=["rhr", "hrv", "sleep_score"]
        )
    else:
        trends, trends_err = None, None

    return {
        "date": today,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "tz_offset_hours": _tz_offset_hours(),
        "readiness": readiness,
        "readiness_err": readiness_err,
        "health": health,
        "health_err": health_err,
        "sleep": sleep,
        "sleep_err": sleep_err,
        "training": training,
        "training_err": training_err,
        "training_status": training_status,
        "training_status_err": training_status_err,
        "activities": activities,
        "activities_err": activities_err,
        "week": week,
        "week_err": week_err,
        "trends": trends,
        "trends_err": trends_err,
        "last_sync": last_sync,
        "last_sync_err": last_sync_err,
    }


# ── RENDERING ─────────────────────────────────────────────────────────────────

# Known Garmin enum strings that don't read well under a generic transform.
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
    # Sentence-case enum-like values: underscore_separated, a single bare token
    # (e.g. "good" / "READY"), or an ALL-CAPS word. Multi-word phrases that are
    # already human-readable ("Good to train") are left untouched.
    if "_" in text or " " not in text or (letters.isalpha() and letters.isupper()):
        spaced = text.replace("_", " ").strip()
        return spaced[:1].upper() + spaced[1:].lower()
    return text


def _label(value) -> str:
    """Human-readable, HTML-escaped enum label ('' when missing)."""
    human = _humanize(value)
    return html.escape(human) if human else ""


# Emoji per sport type for the Recent Activities list.
_SPORT_ICONS = {
    "running": "\U0001F3C3", "trail_running": "\U0001F3C3",
    "treadmill_running": "\U0001F3C3", "track_running": "\U0001F3C3",
    "indoor_running": "\U0001F3C3",
    "road_biking": "\U0001F6B4", "cycling": "\U0001F6B4",
    "indoor_cycling": "\U0001F6B4", "mountain_biking": "\U0001F6B4",
    "gravel_cycling": "\U0001F6B4", "virtual_ride": "\U0001F6B4",
    "lap_swimming": "\U0001F3CA", "open_water_swimming": "\U0001F3CA",
    "swimming": "\U0001F3CA",
    "walking": "\U0001F6B6", "hiking": "\U0001F97E",
    "strength_training": "\U0001F3CB️", "yoga": "\U0001F9D8",
    "cardio": "\U0001F938", "rowing": "\U0001F6A3", "indoor_rowing": "\U0001F6A3",
    "elliptical": "\U0001F3CB️", "multi_sport": "\U0001F501",
}


def _sport_icon(sport) -> str:
    return _SPORT_ICONS.get(str(sport or "").lower(), "\U0001F3C5")


def _e(value) -> str:
    """HTML-escape a value for safe interpolation, mapping None to an em dash."""
    if value is None:
        return "&mdash;"
    return html.escape(str(value))


def _num(value, suffix: str = "") -> str:
    """Render a numeric value with an optional suffix, or an em dash if missing."""
    if value is None:
        return "&mdash;"
    return f"{_e(value)}{html.escape(suffix)}"


def _metric(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="metric-sub">{sub}</div>' if sub else ""
    return (
        '<div class="metric">'
        f'<div class="metric-value">{value}</div>'
        f'<div class="metric-label">{html.escape(label)}</div>'
        f"{sub_html}"
        "</div>"
    )


def _card(title: str, emoji: str, body: str, error: str | None = None) -> str:
    if error:
        body = f'<div class="error">Unavailable — {_e(error)}</div>'
    return (
        '<section class="card">'
        f'<h2><span class="emoji">{emoji}</span>{html.escape(title)}</h2>'
        f'<div class="card-body">{body}</div>'
        "</section>"
    )


def _acwr_indicator(acwr):
    """(arrow_html, word) for an acute:chronic workload ratio.

    ACWR compares acute (7-day) to chronic (28-day) load, so it reads
    directly as whether training load is building or tapering.
    """
    if acwr is None:
        return "", ""
    if acwr >= 1.1:
        word = "ramping" if acwr >= 1.5 else "building"
        return '<span class="trend-up">▲</span>', word
    if acwr <= 0.8:
        return '<span class="trend-down">▼</span>', "tapering"
    return '<span class="trend-flat">▬</span>', "steady"


def _delta_badge(delta, good_up: bool) -> str:
    """A small ▲/▼ delta badge, coloured by whether the move is favourable."""
    if delta is None:
        return ""
    if delta == 0:
        return '<span class="delta flat">→ 0</span>'
    up = delta > 0
    cls = "good" if up == good_up else "bad"
    arrow = "▲" if up else "▼"
    return f'<span class="delta {cls}">{arrow} {abs(delta):g}</span>'


def _sparkline_svg(points: list, color: str, width: int = 140, height: int = 32) -> str:
    """Inline SVG polyline sparkline from a list of {date, value} points.

    None values are skipped (the line connects the present points); a dot marks
    the latest reading. color is a fixed literal supplied by the caller.
    """
    present = [(i, p.get("value")) for i, p in enumerate(points)
               if p.get("value") is not None]
    if len(present) < 2:
        return '<span class="empty">&mdash;</span>'
    ys = [v for _, v in present]
    vmin, vmax = min(ys), max(ys)
    span = (vmax - vmin) or 1
    n = (len(points) - 1) or 1
    pad = 4

    def px(i):
        return pad + (i / n) * (width - 2 * pad)

    def py(v):
        return pad + (1 - (v - vmin) / span) * (height - 2 * pad)

    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in present)
    lx, lv = present[-1]
    dot = f'<circle cx="{px(lx):.1f}" cy="{py(lv):.1f}" r="2.5" fill="{color}"/>'
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" aria-hidden="true">'
        f'<polyline points="{pts}" fill="none" stroke="{color}" '
        f'stroke-width="1.75" stroke-linejoin="round" stroke-linecap="round"/>'
        f"{dot}</svg>"
    )


def _fmt_sync_time(value):
    """Format a Garmin device last-sync timestamp for display.

    Accepts either an epoch-millis integer or an ISO-ish string; returns a
    trimmed 'YYYY-MM-DD HH:MM' style string, or None when unavailable.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            dt = (datetime.fromtimestamp(value / 1000, tz=timezone.utc)
                  + timedelta(hours=_tz_offset_hours()))
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OverflowError, OSError):
            return str(value)
    return str(value).replace("T", " ").split(".")[0]


def _body_battery_card(data: dict) -> str:
    d = data.get("readiness")
    if not d:
        return _card("Body Battery", "\U0001F50B", "", data.get("readiness_err") or "no data")
    bb = d.get("body_battery", {})
    body = (
        '<div class="metrics">'
        + _metric("Current", _num(bb.get("current_level")))
        + _metric("Charged", _num(bb.get("charged")))
        + _metric("Drained", _num(bb.get("drained")))
        + _metric("High / Low", f'{_num(bb.get("highest"))} / {_num(bb.get("lowest"))}')
        + "</div>"
    )
    if bb.get("feedback"):
        body += f'<div class="feedback">{_label(bb.get("feedback"))}</div>'
    return _card("Body Battery", "\U0001F50B", body)


def _sleep_card(data: dict) -> str:
    d = data.get("sleep")
    if not d:
        return _card("Sleep", "\U0001F634", "", data.get("sleep_err") or "no data")
    score = d.get("sleep_score")
    label = d.get("sleep_score_label")
    score_sub = _label(label)
    stages = (
        '<table class="stages sleep-stages">'
        '<tr><th class="st-deep">Deep</th><th class="st-light">Light</th>'
        '<th class="st-rem">REM</th><th class="st-awake">Awake</th></tr>'
        "<tr>"
        f'<td>{_num(d.get("deep_sleep_hrs"), "h")}</td>'
        f'<td>{_num(d.get("light_sleep_hrs"), "h")}</td>'
        f'<td>{_num(d.get("rem_sleep_hrs"), "h")}</td>'
        f'<td>{_num(d.get("awake_hrs"), "h")}</td>'
        "</tr>"
        "<tr>"
        f'<td>{_num(d.get("deep_pct"), "%")}</td>'
        f'<td>{_num(d.get("light_pct"), "%")}</td>'
        f'<td>{_num(d.get("rem_pct"), "%")}</td>'
        f'<td>{_num(d.get("awake_count"))}</td>'
        "</tr>"
        "</table>"
    )
    body = (
        '<div class="metrics">'
        + _metric("Score", _num(score), score_sub)
        + _metric("Duration", _num(d.get("total_sleep_hrs"), "h"))
        + "</div>"
        + stages
    )
    return _card("Sleep", "\U0001F634", body)


def _heart_rate_card(data: dict) -> str:
    d = data.get("readiness")
    health = data.get("health") or {}
    hr = (health.get("heart_rate") or {})
    stats = (d or {}).get("daily_stats", {}) if d else {}
    if not d and not health:
        return _card("Heart Rate", "❤️", "",
                     data.get("readiness_err") or data.get("health_err") or "no data")
    resting = stats.get("resting_hr") if stats else hr.get("resting_hr")
    seven_day = stats.get("resting_hr_7day_avg") if stats else hr.get("seven_day_avg_resting_hr")
    body = (
        '<div class="metrics">'
        + _metric("Resting", _num(resting, " bpm"))
        + _metric("7-day avg", _num(seven_day, " bpm"))
        + _metric("Min", _num(hr.get("min_hr"), " bpm"))
        + _metric("Max", _num(hr.get("max_hr"), " bpm"))
        + "</div>"
    )
    return _card("Heart Rate", "❤️", body)


def _stress_card(data: dict) -> str:
    health = data.get("health")
    if not health:
        return _card("Stress", "\U0001F9D8", "", data.get("health_err") or "no data")
    s = health.get("stress", {})
    body = (
        '<div class="metrics">'
        + _metric("Average", _num(s.get("avg_stress")))
        + _metric("Max", _num(s.get("max_stress")))
        + "</div>"
        + '<table class="stages">'
        + "<tr><th>Rest</th><th>Low</th><th>Medium</th><th>High</th></tr>"
        + "<tr>"
        + f'<td>{_num(s.get("rest_stress_mins"), "m")}</td>'
        + f'<td>{_num(s.get("low_stress_mins"), "m")}</td>'
        + f'<td>{_num(s.get("medium_stress_mins"), "m")}</td>'
        + f'<td>{_num(s.get("high_stress_mins"), "m")}</td>'
        + "</tr>"
        + "</table>"
    )
    return _card("Stress", "\U0001F9D8", body)


def _readiness_card(data: dict) -> str:
    r = (data.get("training") or {}).get("readiness", {}) or {}
    ts = data.get("training_status") or {}
    hrv = (data.get("readiness") or {}).get("hrv") or {}

    if not r and not ts and not hrv:
        return _card("Training Readiness", "⚡", "",
                     data.get("training_err") or data.get("training_status_err")
                     or "no data")

    metrics = [_metric("Score", _num(r.get("score")), _label(r.get("level")))]

    # HRV — current vs baseline (issue 22).
    hrv_cur, hrv_base = hrv.get("last_night_avg"), hrv.get("weekly_avg")
    if hrv_cur is not None or hrv_base is not None:
        sub = f"baseline {_num(hrv_base, ' ms')}" if hrv_base is not None else ""
        metrics.append(_metric("HRV", _num(hrv_cur, " ms"), sub))

    # VO2max — running / cycling (issue 22).
    vo2 = ts.get("vo2max") or {}
    run, bike = vo2.get("running"), vo2.get("cycling")
    if run is not None or bike is not None:
        metrics.append(_metric("VO₂max", f"{_num(run)} / {_num(bike)}", "run / bike"))

    # Acute load with an ACWR building/tapering indicator (issue 23).
    load, acwr = r.get("acute_load"), ts.get("acwr")
    if load is not None or acwr is not None:
        arrow, word = _acwr_indicator(acwr)
        value = f"{_num(load)} {arrow}".strip()
        sub = f"ACWR {acwr:g} · {word}" if acwr is not None else ""
        metrics.append(_metric("Acute Load", value, sub))

    body = '<div class="metrics">' + "".join(metrics) + "</div>"
    if r.get("feedback_short"):
        body += f'<div class="feedback">{_label(r.get("feedback_short"))}</div>'
    return _card("Training Readiness", "⚡", body)


def _activities_card(data: dict) -> str:
    acts = data.get("activities")
    if acts is None:
        return _card("Recent Activities", "\U0001F3C3", "", data.get("activities_err") or "no data")
    if not acts:
        return _card("Recent Activities", "\U0001F3C3", '<div class="empty">No recent activities.</div>')
    rows = "".join(
        "<tr>"
        f'<td>{_e((a.get("date") or "")[:10])}</td>'
        f'<td>{_e(a.get("name"))}</td>'
        f'<td><span class="sport-icon">{_sport_icon(a.get("type"))}</span>'
        f'{_e(str(a.get("type", "")).replace("_", " "))}</td>'
        f'<td class="num">{_num(a.get("distance_km"), " km")}</td>'
        f'<td class="num">{_num(a.get("duration_min"), " min")}</td>'
        f'<td class="num">{_num(a.get("training_load"))}</td>'
        "</tr>"
        for a in acts
    )
    body = (
        '<table class="list">'
        "<tr><th>Date</th><th>Activity</th><th>Type</th>"
        '<th class="num">Dist</th><th class="num">Time</th><th class="num">Load</th></tr>'
        f"{rows}"
        "</table>"
    )
    return _card("Recent Activities", "\U0001F3C3", body)


def _weekly_card(data: dict) -> str:
    w = data.get("week")
    if not w:
        return _card("This Week's Training", "\U0001F4CA", "", data.get("week_err") or "no data")
    metrics = (
        '<div class="metrics">'
        + _metric("Activities", _num(w.get("total_activities")))
        + _metric("Distance", _num(w.get("total_distance_km"), " km"))
        + _metric("Duration", _num(w.get("total_duration_min"), " min"))
        + _metric("Load", _num(w.get("total_training_load")))
        + "</div>"
    )
    by_type = w.get("by_type") or {}
    if by_type:
        rows = "".join(
            "<tr>"
            f'<td>{_e(t.replace("_", " "))}</td>'
            f'<td class="num">{_num(v.get("count"))}</td>'
            f'<td class="num">{_num(v.get("distance_km"), " km")}</td>'
            f'<td class="num">{_num(v.get("duration_min"), " min")}</td>'
            "</tr>"
            for t, v in by_type.items()
        )
        metrics += (
            '<table class="list">'
            '<tr><th>Type</th><th class="num">Count</th>'
            '<th class="num">Dist</th><th class="num">Time</th></tr>'
            f"{rows}"
            "</table>"
        )
    sub = ""
    if w.get("week_start") and w.get("week_end"):
        sub = f'<div class="feedback">{_e(w.get("week_start"))} &rarr; {_e(w.get("week_end"))}</div>'
    return _card("This Week's Training", "\U0001F4CA", metrics + sub)


# Sparkline rows: (metric key, label, unit, higher_is_better, line colour).
_SPARK_SPECS = [
    ("rhr", "Resting HR", " bpm", False, "#e0736f"),
    ("hrv", "HRV", " ms", True, "#6fae7d"),
    ("sleep_score", "Sleep Score", "", True, "#5aa9e6"),
]


def _trends_card(data: dict) -> str:
    """7-day sparklines for RHR, HRV, and sleep score (issue 23 stretch goal).

    Returns an empty string (no card) when sparklines are disabled and no error
    was captured, so the grid simply omits the section.
    """
    t = data.get("trends")
    if not t:
        err = data.get("trends_err")
        if not err:
            return ""
        return _card("7-Day Trends", "\U0001F4C8", "", err)

    metrics = t.get("metrics") or {}
    rows = []
    for key, label, unit, good_up, color in _SPARK_SPECS:
        s = metrics.get(key) or {}
        spark = _sparkline_svg(s.get("daily") or [], color)
        current = _num(s.get("end"), unit)
        rows.append(
            "<tr>"
            f'<td class="spark-label">{html.escape(label)}</td>'
            f'<td class="spark-cell">{spark}</td>'
            f'<td class="num">{current}</td>'
            f'<td class="num">{_delta_badge(s.get("delta"), good_up)}</td>'
            "</tr>"
        )
    body = f'<table class="list spark-table">{"".join(rows)}</table>'
    return _card("7-Day Trends", "\U0001F4C8", body)


# Light-theme variable overrides, shared by the system-preference default (when
# no manual theme is set) and the explicit [data-theme="light"] toggle.
_LIGHT_VARS = """
  --bg:#f5f6f8; --fg:#1a1d23; --muted:#6b7280; --card-bg:#fff; --card-border:#e4e7ec;
  --h2:#2d3340; --value:#111; --sub:#3f8a51; --feedback:#4b515c; --row-border:#eceef2;
  --stage-row:#4b515c; --btn-bg:#fff; --btn-border:#d7dbe2;
"""

_STYLE = """
:root {
  color-scheme: light dark;
  --bg:#0f1115; --fg:#e6e8eb; --muted:#8b93a1; --card-bg:#171a21; --card-border:#232833;
  --h2:#cfd4dc; --value:#fff; --sub:#6fae7d; --feedback:#a7adb8; --row-border:#232833;
  --stage-row:#a7adb8; --btn-bg:#171a21; --btn-border:#2b313d;
}
@media (prefers-color-scheme: light) { :root:not([data-theme]) {%LIGHT%} }
:root[data-theme="light"] {%LIGHT%}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--fg); line-height: 1.4;
}
header { max-width: 1100px; margin: 0 auto 1.25rem; position: relative; }
header h1 { margin: 0 0 .25rem; font-size: 1.5rem; }
header .meta { color: var(--muted); font-size: .85rem; }
header .meta.sync { margin-top: .15rem; }
#theme-toggle {
  position: absolute; top: 0; right: 0;
  background: var(--btn-bg); color: var(--fg);
  border: 1px solid var(--btn-border); border-radius: 8px;
  width: 2.1rem; height: 2.1rem; font-size: 1rem; line-height: 1;
  cursor: pointer; padding: 0;
}
#theme-toggle:hover { border-color: var(--muted); }
.grid {
  max-width: 1100px; margin: 0 auto;
  display: grid; gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}
.card {
  background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px;
  padding: 1rem 1.15rem;
}
.card.wide { grid-column: 1 / -1; }
.card h2 {
  margin: 0 0 .75rem; font-size: 1rem; font-weight: 600;
  display: flex; align-items: center; gap: .5rem; color: var(--h2);
}
.emoji { font-size: 1.15rem; }
.metrics { display: flex; flex-wrap: wrap; gap: 1rem 1.5rem; }
.metric-value { font-size: 1.5rem; font-weight: 700; color: var(--value); }
.metric-label { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; }
.metric-sub { font-size: .75rem; color: var(--sub); margin-top: .1rem; }
.feedback { margin-top: .75rem; font-size: .85rem; color: var(--feedback); font-style: italic; }
table { width: 100%; border-collapse: collapse; margin-top: .85rem; font-size: .85rem; }
th, td { text-align: left; padding: .35rem .4rem; border-bottom: 1px solid var(--row-border); }
th { color: var(--muted); font-weight: 600; font-size: .72rem; text-transform: uppercase; letter-spacing: .03em; }
td.num, th.num { text-align: right; }
table.stages tr:last-child td { border-bottom: none; color: var(--stage-row); }
.error { color: #e0736f; font-size: .85rem; }
.empty { color: var(--muted); font-size: .85rem; }
footer { max-width: 1100px; margin: 1.25rem auto 0; color: var(--muted); font-size: .75rem; }
.sport-icon { margin-right: .4rem; }
.sleep-stages th { border-bottom: 2px solid currentColor; }
.sleep-stages th.st-deep  { color: #3b5bdb; }
.sleep-stages th.st-light { color: #5aa9e6; }
.sleep-stages th.st-rem   { color: #9775fa; }
.sleep-stages th.st-awake { color: #e8863c; }
.trend-up   { color: #e8863c; }
.trend-down { color: #5aa9e6; }
.trend-flat { color: var(--muted); }
.spark { width: 140px; height: 32px; display: block; }
.spark-table td { vertical-align: middle; border-bottom: none; padding: .3rem .4rem; }
.spark-table .spark-label { color: var(--muted); font-size: .8rem; }
.spark-cell { width: 150px; }
.delta { font-size: .8rem; font-weight: 600; white-space: nowrap; }
.delta.good { color: #6fae7d; }
.delta.bad  { color: #e0736f; }
.delta.flat { color: var(--muted); }
""".replace("%LIGHT%", _LIGHT_VARS)


# Flip between light/dark, persisting the choice. Falls back to the system
# preference for the very first toggle when no explicit theme is set yet.
_THEME_TOGGLE_JS = """
(function(){
  var root=document.documentElement, btn=document.getElementById('theme-toggle');
  if(!btn) return;
  btn.addEventListener('click',function(){
    var cur=root.getAttribute('data-theme');
    if(!cur){cur=window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}
    var next=cur==='dark'?'light':'dark';
    root.setAttribute('data-theme',next);
    try{localStorage.setItem('garmin-theme',next);}catch(e){}
  });
})();
"""


def render_dashboard_html(data: dict) -> str:
    """Render the dashboard data dict into a complete HTML document."""
    cards = "".join([
        _readiness_card(data),
        _body_battery_card(data),
        _sleep_card(data),
        _heart_rate_card(data),
        _stress_card(data),
        _trends_card(data),
        _weekly_card(data),
    ])
    activities = f'<div class="grid" style="margin-top:1rem">{_activities_card(data)}</div>'

    refresh_meta = (
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        if REFRESH_SECONDS > 0 else ""
    )
    tz = data.get("tz_offset_hours", 0)
    tz_label = f"UTC{'+' if tz >= 0 else ''}{tz:g}"

    # Last device sync — the timestamp Garmin last received data, distinct from
    # when this page was generated (issue 24).
    sync = data.get("last_sync") or {}
    sync_time = _fmt_sync_time(sync.get("upload_time"))
    sync_html = ""
    if sync_time:
        device = f' &middot; {_e(sync.get("device_name"))}' if sync.get("device_name") else ""
        sync_html = (
            f'<div class="meta sync">Last Garmin sync: '
            f'{_e(sync_time)}{device}</div>'
        )

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{refresh_meta}"
        "<title>Garmin Health Dashboard</title>"
        # Apply a saved theme before first paint to avoid a flash of the wrong mode.
        "<script>try{var t=localStorage.getItem('garmin-theme');"
        "if(t)document.documentElement.setAttribute('data-theme',t);}catch(e){}</script>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        "<header>"
        '<button id="theme-toggle" type="button" aria-label="Toggle dark mode" '
        'title="Toggle light / dark">\U0001F313</button>'
        "<h1>Garmin Health Dashboard</h1>"
        f'<div class="meta">{_e(data.get("date"))} &middot; '
        f'generated {_e(data.get("generated_at"))} ({html.escape(tz_label)})</div>'
        f"{sync_html}"
        "</header>"
        f'<div class="grid">{cards}</div>'
        f"{activities}"
        "<footer>Data fetched live from Garmin Connect on each page load."
        + (f" Auto-refresh every {REFRESH_SECONDS}s." if REFRESH_SECONDS > 0 else "")
        + "</footer>"
        f"<script>{_THEME_TOGGLE_JS}</script>"
        "</body></html>"
    )
