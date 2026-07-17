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
        get_training_readiness,
    )
    from tools.activities import get_activities, get_weekly_summary

    now = _local_now()
    today = now.date().isoformat()

    readiness, readiness_err = _safe(get_daily_readiness, today)
    health, health_err = _safe(get_daily_health, today)
    sleep, sleep_err = _safe(get_sleep, today)
    training, training_err = _safe(get_training_readiness, today)
    activities, activities_err = _safe(get_activities, limit=5)
    week, week_err = _safe(get_weekly_summary)

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
        "activities": activities,
        "activities_err": activities_err,
        "week": week,
        "week_err": week_err,
    }


# ── RENDERING ─────────────────────────────────────────────────────────────────

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
        body += f'<div class="feedback">{_e(bb.get("feedback"))}</div>'
    return _card("Body Battery", "\U0001F50B", body)


def _sleep_card(data: dict) -> str:
    d = data.get("sleep")
    if not d:
        return _card("Sleep", "\U0001F634", "", data.get("sleep_err") or "no data")
    score = d.get("sleep_score")
    label = d.get("sleep_score_label")
    score_sub = html.escape(str(label).replace("_", " ").title()) if label else ""
    stages = (
        '<table class="stages">'
        "<tr><th>Deep</th><th>Light</th><th>REM</th><th>Awake</th></tr>"
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
    d = data.get("training")
    if not d:
        return _card("Training Readiness", "⚡", "", data.get("training_err") or "no data")
    r = d.get("readiness", {})
    score = r.get("score")
    level = r.get("level")
    level_sub = html.escape(str(level).replace("_", " ").title()) if level else ""
    body = (
        '<div class="metrics">'
        + _metric("Score", _num(score), level_sub)
        + "</div>"
    )
    if r.get("feedback_short"):
        body += f'<div class="feedback">{_e(r.get("feedback_short"))}</div>'
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
        f'<td>{_e(str(a.get("type", "")).replace("_", " "))}</td>'
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


_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  background: #0f1115; color: #e6e8eb; line-height: 1.4;
}
header { max-width: 1100px; margin: 0 auto 1.25rem; }
header h1 { margin: 0 0 .25rem; font-size: 1.5rem; }
header .meta { color: #8b93a1; font-size: .85rem; }
.grid {
  max-width: 1100px; margin: 0 auto;
  display: grid; gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}
.card {
  background: #171a21; border: 1px solid #232833; border-radius: 12px;
  padding: 1rem 1.15rem;
}
.card.wide { grid-column: 1 / -1; }
.card h2 {
  margin: 0 0 .75rem; font-size: 1rem; font-weight: 600;
  display: flex; align-items: center; gap: .5rem; color: #cfd4dc;
}
.emoji { font-size: 1.15rem; }
.metrics { display: flex; flex-wrap: wrap; gap: 1rem 1.5rem; }
.metric-value { font-size: 1.5rem; font-weight: 700; color: #fff; }
.metric-label { font-size: .75rem; color: #8b93a1; text-transform: uppercase; letter-spacing: .03em; }
.metric-sub { font-size: .75rem; color: #6fae7d; margin-top: .1rem; }
.feedback { margin-top: .75rem; font-size: .85rem; color: #a7adb8; font-style: italic; }
table { width: 100%; border-collapse: collapse; margin-top: .85rem; font-size: .85rem; }
th, td { text-align: left; padding: .35rem .4rem; border-bottom: 1px solid #232833; }
th { color: #8b93a1; font-weight: 600; font-size: .72rem; text-transform: uppercase; letter-spacing: .03em; }
td.num, th.num { text-align: right; }
table.stages tr:last-child td { border-bottom: none; color: #a7adb8; }
.error { color: #e0736f; font-size: .85rem; }
.empty { color: #8b93a1; font-size: .85rem; }
footer { max-width: 1100px; margin: 1.25rem auto 0; color: #6b7280; font-size: .75rem; }
@media (prefers-color-scheme: light) {
  body { background: #f5f6f8; color: #1a1d23; }
  header .meta, .metric-label, th, footer, .empty { color: #6b7280; }
  .card { background: #fff; border-color: #e4e7ec; }
  .card h2 { color: #2d3340; }
  .metric-value { color: #111; }
  th, td { border-bottom: 1px solid #eceef2; }
  table.stages tr:last-child td { color: #4b515c; }
  .feedback { color: #4b515c; }
}
"""


def render_dashboard_html(data: dict) -> str:
    """Render the dashboard data dict into a complete HTML document."""
    cards = "".join([
        _readiness_card(data),
        _body_battery_card(data),
        _sleep_card(data),
        _heart_rate_card(data),
        _stress_card(data),
        _weekly_card(data),
    ])
    activities = f'<div class="grid" style="margin-top:1rem">{_activities_card(data)}</div>'

    refresh_meta = (
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        if REFRESH_SECONDS > 0 else ""
    )
    tz = data.get("tz_offset_hours", 0)
    tz_label = f"UTC{'+' if tz >= 0 else ''}{tz:g}"

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{refresh_meta}"
        "<title>Garmin Health Dashboard</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        "<header>"
        "<h1>Garmin Health Dashboard</h1>"
        f'<div class="meta">{_e(data.get("date"))} &middot; '
        f'generated {_e(data.get("generated_at"))} ({html.escape(tz_label)})</div>'
        "</header>"
        f'<div class="grid">{cards}</div>'
        f"{activities}"
        "<footer>Data fetched live from Garmin Connect on each page load."
        + (f" Auto-refresh every {REFRESH_SECONDS}s." if REFRESH_SECONDS > 0 else "")
        + "</footer>"
        "</body></html>"
    )
