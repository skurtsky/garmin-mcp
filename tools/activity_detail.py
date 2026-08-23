# tools/activity_detail.py
"""Activity Detail view (issue 74) — opened by clicking an activity row on
the Today or Activities tab. Shows quick stats, training effect, weather,
HR/power charts (zone-coloured, with pause gaps), lap splits, gear, and —
when the activity has GPS — a Leaflet route map with an elevation profile.
A multisport parent (triathlon, duathlon, ...) also gets a per-leg
breakdown.

Everything is read from the `activity_details` table (`detail` + `route`
JSONB, backfilled by sync_garmin.py — see tools/activities.py's
get_activity_detail_row) joined against the activity's own `activities` row.
No Garmin API call happens on a page load; an activity that hasn't been
synced yet (or whose detail row hasn't landed) just renders an empty state.

A GET here returns one HTML fragment — no page chrome — meant to be fetched
and dropped into the dashboard's persistent #activity-modal container (see
tools/dashboard.py's _ACTIVITY_MODAL_JS), the same fetch-a-fragment pattern
already used for other content that doesn't need a full navigation.
"""
import html
import json
import logging
import re

from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route

import db
from tools.dashboard import _e, _sport_style

API_PREFIX = "/api/activity"

_NO_STORE = {"Cache-Control": "no-store"}


# ── SPORT-TYPE HELPERS ──────────────────────────────────────────────────────

_CYCLING_TYPES = {
    "road_biking", "cycling", "indoor_cycling", "mountain_biking",
    "gravel_cycling", "virtual_ride", "virtual_riding",
}
_SWIM_TYPES = {"lap_swimming", "open_water_swimming", "swimming"}


def _is_cycling(sport) -> bool:
    return str(sport or "").lower() in _CYCLING_TYPES


def _is_swim(sport) -> bool:
    return str(sport or "").lower() in _SWIM_TYPES


# ── FORMATTING HELPERS ──────────────────────────────────────────────────────

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _fmt_hms(total_sec) -> str:
    if not total_sec:
        return "&mdash;"
    total_sec = round(total_sec)
    h, rem = divmod(total_sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_km(km) -> str:
    if km is None:
        return "&mdash;"
    return f"{km:.2f} km"


def _fmt_elev(m) -> str:
    if m is None:
        return "&mdash;"
    return f"{round(m)} m"


def _fmt_temp(c) -> str:
    if c is None:
        return "&mdash;"
    return f"{round(c)}&deg;C"


def _fmt_wind(kph, direction) -> str:
    if kph is None:
        return "&mdash;"
    return f"{round(kph)} km/h" + (f" {html.escape(direction)}" if direction else "")


# Minimal 16x16 stroke icons for the map's weather overlay — the page's
# embedded Phosphor font is subsetted to the sport/nav glyphs already used
# elsewhere (see tools/dashboard.py's _ICON_CSS), so temp/wind/humidity need
# their own small inline SVGs rather than another icon-font codepoint.
_ICON_TEMP = (
    '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" '
    'stroke-width="1.3" stroke-linecap="round"><path d="M7 2.5v6.9a2.5 2.5 0 1 0 2 0V2.5a1 1 0 0 0-2 0Z"/>'
    '<circle cx="8" cy="11.5" r="1.4" fill="currentColor" stroke="none"/></svg>'
)
_ICON_WIND = (
    '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" '
    'stroke-width="1.3" stroke-linecap="round"><path d="M2 6h7a1.8 1.8 0 1 0-1.6-2.7"/>'
    '<path d="M2 10h9a1.8 1.8 0 1 1-1.6 2.7"/></svg>'
)
_ICON_HUMIDITY = (
    '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" '
    'stroke-width="1.3" stroke-linejoin="round"><path d="M8 2s4 4.6 4 7.5a4 4 0 0 1-8 0C4 6.6 8 2 8 2Z"/></svg>'
)


def _speed_label(kph) -> str:
    if not kph:
        return "&mdash;"
    return f"{kph:.1f} km/h"


def _pace_label(kph) -> str:
    """km/h -> 'M:SS /km' pace string."""
    if not kph:
        return "&mdash;"
    secs_per_km = 3600 / kph
    m, s = divmod(round(secs_per_km), 60)
    return f"{m}:{s:02d} /km"


def _pace100_label(kph) -> str:
    """km/h -> 'M:SS /100m' pace string."""
    if not kph:
        return "&mdash;"
    secs = 360 / kph
    m, s = divmod(round(secs), 60)
    return f"{m}:{s:02d} /100m"


def _speed_or_pace(sport, kph) -> tuple[str, str]:
    """(label, value) for a sport-appropriate speed/pace reading."""
    if _is_cycling(sport):
        return "Speed", _speed_label(kph)
    if _is_swim(sport):
        return "Pace", _pace100_label(kph)
    return "Pace", _pace_label(kph)


def _parse_start_seconds(iso) -> float:
    """Seconds since local midnight for the activity's start clock time."""
    if not iso:
        return 0
    m = re.search(r"T(\d{2}):(\d{2}):(\d{2})", str(iso))
    if not m:
        return 0
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def _fmt_activity_datetime(iso) -> str | None:
    if not iso:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", str(iso))
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    return f"{_MONTHS[int(mo) - 1]} {int(d)}, {y} at {h}:{mi}"


def _clock_hm(start_sec, offset_sec) -> str:
    total = int(round(start_sec + offset_sec)) % 86400
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _clock_hms(start_sec, offset_sec) -> str:
    total = int(round(start_sec + offset_sec)) % 86400
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _anaerobic_label(v) -> str:
    if v is None:
        return "—"
    if v < 1:
        return "No Benefit"
    if v < 2:
        return "Minor Benefit"
    if v < 3:
        return "Maintaining"
    if v < 4:
        return "Improving"
    return "Highly Improving"


# ── ZONE / CHART GEOMETRY ───────────────────────────────────────────────────

_HR_ZONE_COLORS = ["#75798c", "#4aa7d8", "#4fae72", "#d9a441", "#e2734a", "#cf5a4e"]
_POWER_ZONE_COLORS = ["#75798c", "#4aa7d8", "#4fae72", "#d9a441", "#e2734a", "#cf5a4e", "var(--color-accent)"]
_GAP_COLOR = "var(--color-neutral-700)"

_CHART_W, _CHART_H, _CHART_TOP, _CHART_BOTTOM = 600, 150, 8, 132
_MAX_CHART_POINTS = 150  # per contiguous (non-paused) run


def _zone_index_for(v, bounds: list) -> int:
    idx = 0
    for i in range(1, len(bounds)):
        if v >= bounds[i]:
            idx = i
        else:
            break
    return idx


def _power_bounds_from_ftp(ftp) -> list[int] | None:
    """Classic 7-zone Coggan power zones scaled to FTP."""
    if not ftp:
        return None
    fractions = [0, 0.55, 0.75, 0.90, 1.05, 1.20, 1.50]
    return [round(ftp * f) for f in fractions]


def _marker_pct(value, bounds: list) -> float:
    """Position (0-100) along the zone-bounds bar for a value — inside its
    own zone's segment, proportional to how far through that zone it is."""
    idx = _zone_index_for(value, bounds)
    lo = bounds[idx]
    hi = bounds[idx + 1] if idx + 1 < len(bounds) else lo * 1.15 + 10
    frac = min(max((value - lo) / (hi - lo), 0), 1) if hi != lo else 0
    return round((idx + frac) / len(bounds) * 100, 1)


def _is_gap(prev_t, cur_t, pauses: list) -> bool:
    for p in pauses:
        if p.get("start_sec") is None or p.get("end_sec") is None:
            continue
        if p["start_sec"] < cur_t and p["end_sec"] > prev_t:
            return True
    return False


def _split_runs(series: list, pauses: list) -> list[list]:
    """Split a series into contiguous runs, breaking wherever a pause falls
    between two consecutive samples."""
    if not series:
        return []
    runs, current = [], [series[0]]
    for i in range(1, len(series)):
        if _is_gap(series[i - 1]["t_offset_sec"], series[i]["t_offset_sec"], pauses):
            runs.append(current)
            current = []
        current.append(series[i])
    runs.append(current)
    return runs


def _downsample(run: list, max_points: int = _MAX_CHART_POINTS) -> list:
    if len(run) <= max_points:
        return run
    step = len(run) / max_points
    idxs = sorted({round(i * step) for i in range(max_points)} | {0, len(run) - 1})
    return [run[i] for i in idxs if i < len(run)]


_SMOOTH_WINDOW_SEC = 60  # matches the mockup's ~50% smoothing on HR/power


def _smooth_run(run: list, window_sec: float = _SMOOTH_WINDOW_SEC) -> list:
    """Moving-average smoothing over a fixed *time* window (not a fixed
    sample count) so a real per-second Garmin recording gets real noise
    reduction — a raw HR/power trace spikes on almost every sample, and a
    small index-based radius barely dents that at 1Hz. Cosmetic (matches the
    mockup's smoothed line) and also keeps the zone-coloured path from
    fragmenting into hundreds of tiny same-length segments."""
    n = len(run)
    if n < 3:
        return run
    span_sec = run[-1]["t_offset_sec"] - run[0]["t_offset_sec"]
    avg_dt = span_sec / (n - 1) if n > 1 else 1
    radius = max(1, round(window_sec / 2 / max(avg_dt, 0.1)))
    values = [p["value"] for p in run]
    smoothed = []
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        smoothed.append(sum(values[lo:hi]) / (hi - lo))
    return [{**p, "value": v} for p, v in zip(run, smoothed)]


def _build_line_chart(series: list, pauses: list, bounds: list | None,
                       colors: list, unit_suffix: str, total_elapsed_sec: float,
                       start_sec: float, avg_value: float | None = None,
                       one_indexed: bool = False) -> dict | None:
    """SVG geometry + hover points for one HR/power chart, matching the
    dashboard's existing .js-linechart convention (see tools/dashboard.py's
    _CHART_JS) so hover tooltips work for free once wired."""
    if len(series) < 2 or not total_elapsed_sec:
        return None

    runs = [_downsample(_smooth_run(r)) for r in _split_runs(series, pauses) if r]
    points_flat = [p for run in runs for p in run]
    if len(points_flat) < 2:
        return None

    values = [p["value"] for p in points_flat]
    lo_raw, hi_raw = min(values), max(values)
    pad = max((hi_raw - lo_raw) * 0.1, 3)
    lo_y, hi_y = lo_raw - pad, hi_raw + pad
    span = (hi_y - lo_y) or 1

    def x_for(t):
        return t / total_elapsed_sec * _CHART_W

    def y_for(v):
        return _CHART_BOTTOM - (v - lo_y) / span * (_CHART_BOTTOM - _CHART_TOP)

    # One <path> per contiguous same-colour stretch within each run.
    paths = []
    for run in runs:
        cur_color, cur_pts = None, []
        for i in range(1, len(run)):
            prev, cur = run[i - 1], run[i]
            mid_v = (prev["value"] + cur["value"]) / 2
            color = colors[_zone_index_for(mid_v, bounds)] if bounds else colors[-1]
            if color != cur_color:
                if len(cur_pts) > 1:
                    paths.append({"color": cur_color, "pts": cur_pts})
                cur_color = color
                cur_pts = [(x_for(prev["t_offset_sec"]), y_for(prev["value"]))]
            cur_pts.append((x_for(cur["t_offset_sec"]), y_for(cur["value"])))
        if len(cur_pts) > 1:
            paths.append({"color": cur_color, "pts": cur_pts})

    path_html = "".join(
        '<path d="{}" fill="none" stroke="{}" stroke-width="2" '
        'vector-effect="non-scaling-stroke" stroke-linejoin="round" '
        'stroke-linecap="round"></path>'.format(
            " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(p["pts"])),
            p["color"],
        )
        for p in paths
    )

    # Dashed grey connector across each pause gap between runs.
    gap_html = ""
    for i in range(1, len(runs)):
        prev_end = runs[i - 1][-1]
        cur_start = runs[i][0]
        x1, y1 = x_for(prev_end["t_offset_sec"]), y_for(prev_end["value"])
        x2, y2 = x_for(cur_start["t_offset_sec"]), y_for(cur_start["value"])
        gap_html += (
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{_GAP_COLOR}" stroke-width="1.5" stroke-dasharray="4,4" '
            'vector-effect="non-scaling-stroke"></line>'
        )

    # Filled area under each run, for a soft accent backdrop.
    area_parts = []
    for run in runs:
        pts = [(x_for(p["t_offset_sec"]), y_for(p["value"])) for p in run]
        area_parts.append(
            f"M{pts[0][0]:.1f},{_CHART_BOTTOM} "
            + " ".join(f"L{x:.1f},{y:.1f}" for x, y in pts)
            + f" L{pts[-1][0]:.1f},{_CHART_BOTTOM} Z"
        )
    area_path = " ".join(area_parts)

    def zone_fields(v):
        if not bounds:
            return None, None, None
        idx = _zone_index_for(v, bounds)
        label = f"Zone {idx + 1}" if one_indexed else f"Zone {idx}"
        return label, colors[idx], _marker_pct(v, bounds)

    hover_points = []
    for p in points_flat:
        zl, zc, mp = zone_fields(p["value"])
        hover_points.append({
            "x": round(x_for(p["t_offset_sec"]), 1),
            "y": round(y_for(p["value"]), 1),
            "t": _clock_hms(start_sec, p["t_offset_sec"]),
            "v": round(p["value"]),
            "zl": zl, "zc": zc, "mp": mp,
        })

    if avg_value is None:
        avg_value = sum(values) / len(values)
    avg_zone_label, avg_zone_color, avg_marker_pct = zone_fields(avg_value)

    return {
        "path_html": path_html,
        "gap_html": gap_html,
        "area_path": area_path,
        "hi_label": round(hi_y),
        "lo_label": round(lo_y),
        "x_label_start": _clock_hm(start_sec, 0),
        "x_label_mid": _clock_hm(start_sec, total_elapsed_sec / 2),
        "x_label_end": _clock_hm(start_sec, total_elapsed_sec),
        "points_json": html.escape(json.dumps(hover_points), quote=True),
        "avg_value": round(avg_value),
        "avg_zone_label": avg_zone_label,
        "avg_zone_color": avg_zone_color,
        "avg_marker_pct": avg_marker_pct,
        "unit_suffix": unit_suffix,
    }


def _zone_minutes_from_series(series: list, pauses: list, bounds: list) -> list[float]:
    """Minutes spent in each zone, computed from consecutive-sample deltas —
    Garmin has no native power-zone breakdown endpoint, so this reconstructs
    one from the same power series already stored for the chart."""
    minutes = [0.0] * len(bounds)
    for i in range(1, len(series)):
        prev, cur = series[i - 1], series[i]
        if _is_gap(prev["t_offset_sec"], cur["t_offset_sec"], pauses):
            continue
        dt = cur["t_offset_sec"] - prev["t_offset_sec"]
        if dt <= 0:
            continue
        idx = _zone_index_for((prev["value"] + cur["value"]) / 2, bounds)
        minutes[idx] += dt / 60
    return minutes


def _zone_table_html(rows: list[tuple]) -> str:
    """rows: list of (zone_label, minutes, pct, color) tuples — the color is
    passed in explicitly per row rather than inferred from position, since a
    zone's number and its index into the bounds/colour ramp aren't always
    the same (Garmin's real HR zones are numbered 1-5 with no "zone 0"
    row, but the chart's bounds ramp reserves index 0 for "below zone 1").
    Column order is zone / duration / bar / % — a header-less bar column
    between the duration and percentage figures."""
    body = "".join(
        f'<div class="ad-zone-row">'
        f'<div class="muted">{html.escape(label)}</div>'
        f'<div>{_fmt_hms(minutes * 60)}</div>'
        f'<div class="ad-zone-bar"><div style="width:{pct}%;background:{color}"></div></div>'
        f'<div class="muted" style="text-align:right">{pct:.1f}%</div>'
        "</div>"
        for label, minutes, pct, color in rows
    )
    header = (
        '<div class="ad-zone-row ad-zone-row-head muted">'
        '<div>ZONE</div><div>DURATION</div><div></div><div style="text-align:right">%</div></div>'
    )
    return f'<div class="card" style="padding:14px;gap:2px">{header}{body}</div>'


def _chart_card_html(chart_id: str, avg_label: str, chart: dict,
                      bounds: list | None, bound_labels: list[str] | None,
                      colors: list) -> str:
    bounds_bar = ""
    if bounds:
        segs = "".join(
            f'<div style="flex:1;background:{colors[i % len(colors)]}"></div>' for i in range(len(bounds))
        )
        labels = "".join(f"<span>{html.escape(lbl)}</span>" for lbl in (bound_labels or []))
        bounds_bar = f"""
        <div class="ad-bounds-bar">
          {segs}
          <div class="ad-chart-marker" style="left:{chart['avg_marker_pct']}%;background:{chart['avg_zone_color']}"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">{labels}</div>"""

    zone_badge = (
        f'<div class="ad-chart-zone" style="color:{chart["avg_zone_color"]}">{chart["avg_zone_label"]}</div>'
        if chart.get("avg_zone_label") else ""
    )

    return f"""
    <div class="card ad-chart-card" style="padding:14px;gap:10px"
        data-avg-value="{chart['avg_value']}" data-unit="{html.escape(chart['unit_suffix'])}"
        data-avg-caption="{html.escape(avg_label)}"
        data-avg-zone-label="{html.escape(chart['avg_zone_label'] or '')}"
        data-avg-zone-color="{html.escape(chart['avg_zone_color'] or '')}"
        data-avg-marker="{chart['avg_marker_pct'] if chart['avg_marker_pct'] is not None else ''}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div>
          <div><span class="ad-chart-num" style="font-family:var(--font-heading);font-size:30px;line-height:1">{chart['avg_value']}</span><span
              style="font-size:13px;color:var(--color-neutral-500)">{html.escape(chart['unit_suffix'])}</span></div>
          <div class="ad-chart-caption muted" style="font-size:11px;margin-top:2px">{html.escape(avg_label)}</div>
        </div>
        {zone_badge}
      </div>
      <svg class="js-adchart" id="{chart_id}" data-points="{chart['points_json']}"
          viewBox="0 0 {_CHART_W} {_CHART_H}" preserveAspectRatio="none"
          style="width:100%;height:130px;display:block">
        <path d="{chart['area_path']}" fill="url(#gArea)"></path>
        {chart['path_html']}
        {chart['gap_html']}
        <line class="chart-crosshair" x1="0" x2="0" y1="0" y2="{_CHART_H}" stroke="var(--color-neutral-400)"
            stroke-width="1" vector-effect="non-scaling-stroke" style="opacity:0;pointer-events:none"></line>
        <circle class="chart-dot" cx="0" cy="0" r="3.5" fill="var(--color-accent)" stroke="var(--color-bg)"
            stroke-width="1.5" style="opacity:0;pointer-events:none"></circle>
        <rect class="chart-hit" x="0" y="0" width="{_CHART_W}" height="{_CHART_H}"
            style="fill:transparent;pointer-events:all;cursor:crosshair"></rect>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
        <span>{chart['x_label_start']}</span><span>{chart['x_label_mid']}</span><span>{chart['x_label_end']}</span>
      </div>
      {bounds_bar}
    </div>"""


def _hr_power_section_html(title: str, chart_card_html: str, zone_table_html: str) -> str:
    return f"""
    <div style="display:flex;flex-direction:column;gap:8px">
      <div class="section-title" style="margin-bottom:0">{html.escape(title)}</div>
      {chart_card_html}
      {zone_table_html}
    </div>"""


# ── SECTIONS ─────────────────────────────────────────────────────────────────

def _quick_stats_html(row: dict, detail: dict, total_elapsed_sec: float) -> str:
    sport = row.get("activity_type")
    speed_label, speed_value = _speed_or_pace(sport, detail.get("avg_speed_kph"))
    stats = [
        ("Total Duration", _fmt_hms(total_elapsed_sec)),
        ("Active Duration", _fmt_hms(detail.get("duration_active_sec"))),
        ("Distance", _fmt_km(row.get("distance_km"))),
        (f"Avg {speed_label}", speed_value),
        ("Elevation", _fmt_elev(detail.get("elevation_gain_m"))),
        ("Calories", _num(detail.get("calories"))),
        ("Avg HR", f"{_num(row.get('avg_hr'))} bpm" if row.get("avg_hr") is not None else "&mdash;"),
    ]
    if detail.get("avg_power"):
        stats.append(("Avg Power", f"{_num(detail['avg_power'])} W"))

    cells = "".join(
        f'<div><div class="muted" style="font-size:10px">{html.escape(label)}</div>'
        f'<div style="font-family:var(--font-heading);font-size:16px">{value}</div></div>'
        for label, value in stats
    )
    return f'<div class="ad-stat-grid">{cells}</div>'


def _num(v) -> str:
    if v is None:
        return "&mdash;"
    return f"{round(v):,}" if isinstance(v, (int, float)) else html.escape(str(v))


def _training_effect_html(detail: dict, training_load) -> str:
    aerobic = detail.get("training_effect")
    anaerobic = detail.get("anaerobic_te")
    if aerobic is None and anaerobic is None:
        return ""

    primary_benefit = _humanize_label(detail.get("training_effect_label"))

    def bar(value, label, sub_label, gradient):
        pct = min(max((value or 0) / 5 * 100, 0), 100)
        return f"""
        <div>
          <div style="display:flex;justify-content:space-between;font-size:11px">
            <span class="muted">{html.escape(label)}</span><span>{value if value is not None else '&mdash;'} &middot; {html.escape(sub_label)}</span>
          </div>
          <div class="ad-te-track" style="background:{gradient}">
            <div style="position:absolute;top:50%;left:{pct}%;transform:translate(-50%,-50%);width:12px;height:12px;
                border-radius:50%;background:var(--color-accent);border:2px solid var(--color-bg)"></div>
          </div>
        </div>"""

    aerobic_gradient = "linear-gradient(90deg,#75798c,#4aa7d8 35%,#4fae72 55%,#d9a441 75%,#e2734a 90%,#cf5a4e)"
    anaerobic_gradient = "linear-gradient(90deg,#3f424d,#4aa7d8 40%,#d9a441 70%,#cf5a4e)"

    return f"""
    <div class="card" style="padding:14px;gap:12px">
      <div class="section-title" style="margin-bottom:0">Training Effect</div>
      <div class="ad-stat-grid">
        <div><div class="muted" style="font-size:10px">Primary Benefit</div>
          <div style="font-family:var(--font-heading);font-size:16px">{_e(primary_benefit)}</div></div>
        <div><div class="muted" style="font-size:10px">Training Load</div>
          <div style="font-family:var(--font-heading);font-size:16px">{_num(training_load)}</div></div>
      </div>
      {bar(aerobic, 'Aerobic', primary_benefit or '—', aerobic_gradient)}
      {bar(anaerobic, 'Anaerobic', _anaerobic_label(anaerobic), anaerobic_gradient)}
    </div>"""


def _humanize_label(value) -> str | None:
    if not value:
        return None
    text = str(value).replace("_", " ").strip()
    return text[:1].upper() + text[1:].lower() if text else None


def _weather_html(weather: dict | None) -> str:
    if not weather:
        return ""
    chips = [
        ("Temp", _fmt_temp(weather.get("temp_c"))),
        ("Humidity", f"{round(weather['humidity_pct'])}%" if weather.get("humidity_pct") is not None else "&mdash;"),
        ("Wind", _fmt_wind(weather.get("wind_kph"), weather.get("wind_dir"))),
    ]
    if weather.get("conditions"):
        chips.append(("Conditions", html.escape(weather["conditions"])))
    cells = "".join(
        f'<div><div class="muted" style="font-size:10px">{label}</div>'
        f'<div style="font-family:var(--font-heading);font-size:15px">{value}</div></div>'
        for label, value in chips
    )
    return f'<div class="card" style="padding:14px"><div class="ad-stat-grid">{cells}</div></div>'


def _splits_table_html(laps: list, sport) -> str:
    if not laps:
        return ""
    show_power = any(lap.get("avg_power") for lap in laps)
    is_cyc = _is_cycling(sport)
    speed_header = "Speed" if is_cyc else "Pace"
    speed_fn = _speed_label if is_cyc else (_pace100_label if _is_swim(sport) else _pace_label)

    rows = "".join(
        f"<tr><td>{lap.get('lap_num')}</td><td>{_fmt_km(lap.get('cum_km'))}</td>"
        f"<td>{_fmt_hms(lap.get('duration_sec'))}</td>"
        f"<td>{speed_fn(lap.get('speed_kph'))}</td>"
        f"<td>{_num(lap.get('avg_hr'))}</td>"
        + (f"<td>{_num(lap.get('avg_power'))}</td>" if show_power else "")
        + "</tr>"
        for lap in laps
    )
    power_th = "<th>Power</th>" if show_power else ""
    return f"""
    <div class="card" style="padding:14px;gap:8px">
      <div class="section-title" style="margin-bottom:0">Splits</div>
      <div style="overflow-x:auto">
        <table class="ad-splits-table">
          <thead><tr><th>Lap</th><th>Dist</th><th>Time</th><th>{speed_header}</th><th>HR</th>{power_th}</tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>"""


def _gear_list_html(gear: list, token: str | None) -> str:
    if not gear:
        return ""
    rows = []
    for g in gear:
        lifespan = g.get("lifespan_km")
        distance = g.get("distance_km") or 0
        pct = min((distance / lifespan) * 100, 100) if lifespan else None
        color = ("var(--color-neutral-600)" if pct is None
                 else "#4fae72" if pct < 70 else "#d9a441" if pct < 90 else "#cf5a4e")
        life_cell = (
            f'<div style="display:flex;align-items:center;gap:8px">'
            f'<div class="gt-wearbar" style="flex:1"><div style="width:{pct}%;background:{color}"></div></div>'
            f'<span class="muted" style="font-size:11px;flex:0 0 auto">{round(pct)}%</span></div>'
            if pct is not None else '<span class="muted">&mdash;</span>'
        )
        rows.append(
            f'<div class="ad-gear-row">'
            f'<div>{_e(g.get("name"))}</div>'
            f'<div class="muted">{_fmt_km(distance)}</div>'
            f'<div>{life_cell}</div>'
            "</div>"
        )
    gear_tracker_url = "/dashboard?tab=gear" + (f"&token={html.escape(token, quote=True)}" if token else "")
    return f"""
    <div class="card" style="padding:14px;gap:2px">
      <div class="section-title" style="margin-bottom:6px">Gear</div>
      <div class="ad-gear-row ad-gear-row-head muted">
        <div>COMPONENT</div><div>DISTANCE</div><div>LIFE USED</div></div>
      {"".join(rows)}
      <a href="{gear_tracker_url}" class="btn btn-secondary"
          style="margin-top:10px;text-align:center;text-decoration:none;padding:10px;border-radius:8px;
          border:1px solid var(--color-accent-700);color:var(--color-accent-200)">Open in Gear Tracker</a>
    </div>"""


def _weather_overlay_html(weather: dict | None) -> str:
    """The map's top-right weather badge — temp/wind/humidity, each with its
    own icon, stacked over the tiles rather than a separate section."""
    if not weather:
        return ""
    chips = []
    if weather.get("temp_c") is not None:
        chips.append((_ICON_TEMP, _fmt_temp(weather["temp_c"])))
    wind = _fmt_wind(weather.get("wind_kph"), weather.get("wind_dir"))
    if weather.get("wind_kph") is not None:
        chips.append((_ICON_WIND, wind))
    if weather.get("humidity_pct") is not None:
        chips.append((_ICON_HUMIDITY, f"{round(weather['humidity_pct'])}%"))
    if not chips:
        return ""
    rows = "".join(
        f'<div style="display:flex;align-items:center;gap:4px">{icon}<span>{value}</span></div>'
        for icon, value in chips
    )
    return f'<div class="ad-map-weather">{rows}</div>'


def _map_section_html(route: list | None, weather: dict | None) -> str:
    if not route:
        return ""
    stride = max(1, len(route) // 300)
    sampled = route[::stride]
    if sampled[-1] != route[-1]:
        sampled.append(route[-1])
    points_json = html.escape(json.dumps([[p["lat"], p["lon"]] for p in sampled]), quote=True)

    elevations = [p.get("elevation_m") for p in sampled if p.get("elevation_m") is not None]
    elev_svg = ""
    if len(elevations) > 1:
        vmin, vmax = min(elevations), max(elevations)
        span = (vmax - vmin) or 1
        n = len(elevations)
        pts = [(i / (n - 1) * _CHART_W, _CHART_TOP + (1 - (v - vmin) / span) * (60 - _CHART_TOP))
               for i, v in enumerate(elevations)]
        line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
        area = line + f" L{pts[-1][0]:.1f},60 L{pts[0][0]:.1f},60 Z"
        elev_svg = f"""
        <svg viewBox="0 0 {_CHART_W} 60" preserveAspectRatio="none" style="width:100%;height:52px;display:block;margin-top:8px">
          <path d="{area}" fill="url(#gArea)"></path>
          <path d="{line}" fill="none" stroke="var(--color-accent)" stroke-width="1.7"
              vector-effect="non-scaling-stroke" stroke-linejoin="round"></path>
        </svg>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600)">
          <span>{_fmt_elev(vmin)}</span><span>{_fmt_elev(vmax)}</span>
        </div>"""

    return f"""
    <div class="card" style="padding:0;gap:0;overflow:hidden">
      <div style="position:relative">
        <div id="activity-map" data-route="{points_json}"></div>
        {_weather_overlay_html(weather)}
      </div>
      {f'<div style="padding:8px 14px 12px">{elev_svg}</div>' if elev_svg else ""}
    </div>"""


def _sub_activities_html(subs: list | None) -> str:
    if not subs:
        return ""
    rows = []
    for sub in subs:
        icon, tint = _sport_style(sub.get("type"))
        pace = sub.get("pace_per_km") or sub.get("pace_per_100m")
        speed = f"{sub.get('avg_speed_kmh')} km/h" if sub.get("avg_speed_kmh") else None
        detail_value = pace or speed or _fmt_hms(sub.get("duration_s"))
        rows.append(f"""
        <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--color-divider)">
          <div style="width:28px;height:28px;flex:0 0 auto;border-radius:8px;display:grid;place-items:center;
              background:color-mix(in srgb, {tint} 18%, transparent);color:{tint};font-size:15px"><i class="ph">{icon}</i></div>
          <div style="flex:1;min-width:0">
            <div style="font-size:13px">{_e(sub.get('name'))}</div>
            <div class="muted" style="font-size:10px">{_fmt_hms(sub.get('duration_s'))}
              {f" &middot; {_fmt_km((sub.get('distance_m') or 0) / 1000)}" if sub.get('distance_m') else ""}</div>
          </div>
          <div style="text-align:right;font-size:12px" class="muted">{html.escape(str(detail_value))}</div>
        </div>""")
    return f"""
    <div class="card" style="padding:14px;gap:4px">
      <div class="section-title" style="margin-bottom:0">Legs</div>
      <div>{''.join(rows)}</div>
    </div>"""


def _empty_state_html(message: str) -> str:
    return (
        '<div class="activity-modal-empty" style="padding:32px 20px;text-align:center">'
        f'<div class="muted" style="font-size:13px">{html.escape(message)}</div></div>'
    )


# ── TOP-LEVEL RENDER ─────────────────────────────────────────────────────────

def _bound_labels(bounds: list) -> list[str]:
    return [str(b) for b in bounds[:-1]] + [f"{bounds[-1]}+"]


def render_activity_detail_fragment(row: dict, token: str | None = None) -> str:
    """The activity-detail modal body's inner HTML for one already-joined
    activities+activity_details row (see db.get_activity_with_detail)."""
    detail = row.get("detail") or {}
    route = row.get("route")
    weather = detail.get("weather")
    sport = row.get("activity_type")
    icon, tint = _sport_style(sport)

    active_sec = detail.get("duration_active_sec") or (row.get("duration_min") or 0) * 60
    pause_sec = sum(
        (p.get("end_sec") or 0) - (p.get("start_sec") or 0) for p in (detail.get("pauses") or [])
    )
    total_elapsed_sec = active_sec + pause_sec
    start_sec = _parse_start_seconds(row.get("local_start_iso"))

    header = f"""
    <div style="display:flex;align-items:center;gap:10px;margin:2px 0 4px">
      <div style="width:36px;height:36px;flex:0 0 auto;border-radius:10px;display:grid;place-items:center;
          background:color-mix(in srgb, {tint} 18%, transparent);color:{tint};font-size:19px"><i class="ph">{icon}</i></div>
      <div style="flex:1;min-width:0">
        <div style="font-family:var(--font-heading);font-size:17px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            {_e(row.get('name'))}</div>
        <div class="muted" style="font-size:12px">{_e(_fmt_activity_datetime(row.get('local_start_iso')))}</div>
      </div>
    </div>"""

    # The route map carries its own weather overlay (temp/wind/humidity); the
    # standalone weather card is only a fallback for an indoor/no-GPS
    # activity that still has weather (rare, but the data may exist).
    map_section = _map_section_html(route, weather)
    weather_fallback = "" if route else _weather_html(weather)

    quick_stats = _quick_stats_html(row, detail, total_elapsed_sec)
    training_effect = _training_effect_html(detail, row.get("training_load"))
    sub_activities = _sub_activities_html(detail.get("sub_activities"))

    hr_zones = detail.get("hr_zones") or []
    hr_series = detail.get("hr_series") or []
    pauses = detail.get("pauses") or []
    hr_section = ""
    if hr_series and len(hr_series) > 1:
        # Garmin's own zone breakdown is 5 zones numbered 1-5 (no "zone 0"
        # row) — prepend the implicit 0-to-zone-1 floor so the chart/bounds
        # bar's index 0 is a real "below zone 1" bucket, keeping the 6-colour
        # _HR_ZONE_COLORS ramp aligned the same way the power zones already are.
        hr_bounds = [0] + [z["min_hr"] for z in hr_zones] if hr_zones else None
        avg_hr = row.get("avg_hr")
        chart = _build_line_chart(hr_series, pauses, hr_bounds, _HR_ZONE_COLORS, " bpm",
                                   total_elapsed_sec, start_sec, avg_value=avg_hr)
        if chart:
            zone_table_html = ""
            if hr_zones:
                zone_rows = [
                    (f"Zone {z['zone']}", z.get("time_min") or 0, z.get("pct_time") or 0,
                     _HR_ZONE_COLORS[z["zone"] % len(_HR_ZONE_COLORS)])
                    for z in hr_zones
                ]
                zone_table_html = _zone_table_html(zone_rows)
            card = _chart_card_html(
                "activity-hr-chart", "Average HR", chart,
                hr_bounds, _bound_labels(hr_bounds) if hr_bounds else None, _HR_ZONE_COLORS)
            hr_section = _hr_power_section_html("Heart Rate", card, zone_table_html)

    power_series = detail.get("power_series") or []
    power_section = ""
    if power_series and len(power_series) > 1 and detail.get("avg_power"):
        power_bounds = _power_bounds_from_ftp(detail.get("ftp"))
        colors = _POWER_ZONE_COLORS if power_bounds else [_POWER_ZONE_COLORS[-1]]
        chart = _build_line_chart(power_series, pauses, power_bounds, colors, " W",
                                   total_elapsed_sec, start_sec,
                                   avg_value=detail.get("avg_power"), one_indexed=True)
        if chart:
            zone_table_html = ""
            if power_bounds:
                minutes = _zone_minutes_from_series(power_series, pauses, power_bounds)
                total_min = sum(minutes) or 1
                zone_rows = [
                    (f"Zone {i + 1}", m, m / total_min * 100, _POWER_ZONE_COLORS[i % len(_POWER_ZONE_COLORS)])
                    for i, m in enumerate(minutes)
                ]
                zone_table_html = _zone_table_html(zone_rows)
            card = _chart_card_html(
                "activity-power-chart", "Average Power", chart,
                power_bounds, _bound_labels(power_bounds) if power_bounds else None, colors)
            power_section = _hr_power_section_html("Power", card, zone_table_html)

    splits_section = _splits_table_html(detail.get("laps") or [], sport)
    gear_section = _gear_list_html(detail.get("gear") or [], token)

    sections = "".join(filter(None, [
        header, map_section, weather_fallback, quick_stats, training_effect,
        sub_activities, hr_section, power_section, splits_section, gear_section,
    ]))
    return f'<div style="display:flex;flex-direction:column;gap:14px">{sections}</div>'


# ── ROUTE ────────────────────────────────────────────────────────────────────

async def get_activity_fragment(request):
    """GET /api/activity/{garmin_id} — the activity-detail modal body, or an
    empty-state fragment when the activity hasn't been synced yet."""
    try:
        garmin_id = int(request.path_params["garmin_id"])
    except (KeyError, ValueError, TypeError):
        return HTMLResponse(_empty_state_html("Invalid activity."), status_code=400, headers=_NO_STORE)

    if not db.is_configured():
        return HTMLResponse(
            _empty_state_html("Activity detail isn't available — no database configured."),
            headers=_NO_STORE)

    row = db.get_activity_with_detail(garmin_id)
    if row is None:
        return HTMLResponse(
            _empty_state_html("Detail for this activity hasn't synced yet — check back after the next sync."),
            headers=_NO_STORE)

    try:
        fragment = render_activity_detail_fragment(row, request.query_params.get("token"))
    except Exception:
        logging.getLogger(__name__).exception("Activity detail render failed for %s", garmin_id)
        return HTMLResponse(
            _empty_state_html("Something went wrong rendering this activity."),
            status_code=500, headers=_NO_STORE)

    return HTMLResponse(fragment, headers=_NO_STORE)


ROUTES = [
    Route(f"{API_PREFIX}/{{garmin_id:int}}", get_activity_fragment, methods=["GET"]),
]


def owns_path(path: str) -> bool:
    return path.startswith(f"{API_PREFIX}/")


def create_app() -> Starlette:
    return Starlette(routes=ROUTES)
