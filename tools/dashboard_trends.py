# tools/dashboard_trends.py
"""The Trends tab's fitness & fatigue card and its metric cards.

Fitness (42-day average of Garmin load), fatigue (7-day) and form (their
difference) over the selected range, then projected forward — dashed — over
the plan's upcoming sessions. The goal race gets a marker, the race-day form
target and a strip under the chart, but only when it falls inside that
projected window.

Below it, HRV / resting HR / sleep score / stress cards: tap one to draw it,
rescaled, over the chart. Scrubbing the chart (hover or drag) reads any day:
the stats, the overlay value and every metric card follow it.

The chart and cards are server-rendered SVG; ``TRENDS_JS`` only handles the
scrub and the overlay toggle, reading the days from a JSON data island.
"""
import html
import json
from datetime import date

from tools import training_load

FITNESS_COLOR = "#9184d9"
FATIGUE_COLOR = "#e2734a"
RACE_COLOR = "#b5abfc"
FRESH = "#4aa7d8"
AMBER = "#d9a441"
# (trends metric key, label, unit, lower is better, colour)
METRICS = (
    ("hrv", "HRV", "ms", False, "#7fc9b0"),
    ("rhr", "Resting HR", "bpm", True, "#cf5a4e"),
    ("sleep_score", "Sleep score", "/100", False, "#6f9ce8"),
    ("stress", "Stress", "avg", True, "#d9a441"),
)
W, H = 360, 200


def _e(v) -> str:
    return "&mdash;" if v is None else html.escape(str(v))


def _mon_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"


def _dow_mon_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a %b')} {d.day}"


def _signed(v: int) -> str:
    return f"+{v}" if v > 0 else str(v)


def window(series: list[dict], today: str, days: int) -> tuple[list[dict], int]:
    """The last ``days`` actual days through today plus up to ``days`` days
    of projection; returns (window, index of today)."""
    actual = [p for p in series if p["date"] <= today][-days:]
    proj = [p for p in series if p["date"] > today][:days]
    return actual + proj, len(actual) - 1


def _metric_daily(trends: dict, key: str) -> dict[str, float]:
    daily = (((trends or {}).get("metrics") or {}).get(key) or {}).get("daily") or []
    return {str(p.get("date"))[:10]: p.get("value") for p in daily if p.get("value") is not None}


def _chart_svg(win: list[dict], ti: int, race_idx: int | None, overlays: dict[str, tuple[str, list]]) -> str:
    n = len(win)
    max_v = max([max(p["fitness"], p["fatigue"]) for p in win] + [1]) * 1.1

    def x(i):
        return 0 if n == 1 else i / (n - 1) * W

    def y(v):
        return 8 + (1 - v / max_v) * 112

    def fy(f):
        return 140 + ((25 - max(-45, min(25, f))) / 70) * 60

    def line(key, a, b):
        return " ".join(f"{'L' if k else 'M'}{x(a + k):.1f} {y(p[key]):.1f}" for k, p in enumerate(win[a:b + 1]))

    last = n - 1
    ctl, atl = line("fitness", 0, ti), line("fatigue", 0, ti)
    ctl_p, atl_p = (line("fitness", ti, last), line("fatigue", ti, last)) if last > ti else ("", "")
    area = f"{ctl} L{x(ti):.1f} 120 L0 120 Z"
    bw = max(1.0, (W / n) * 0.7)
    bars = {}
    for i, p in enumerate(win):
        color = training_load.form_zone(p["form"])["color"]
        y0, y1 = fy(0), fy(p["form"])
        key = (color, p["projected"])
        bars[key] = bars.get(key, "") + (f"M{x(i) - bw / 2:.1f} {min(y0, y1):.1f}h{bw:.1f}"
                                         f"v{max(1, abs(y1 - y0)):.1f}h{-bw:.1f}Z")
    stroke = 'fill="none" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"'
    kids = [
        f'<rect x="0" y="{fy(-10):.1f}" width="{W}" height="{fy(-30) - fy(-10):.1f}" fill="#4fae72" opacity="0.07"/>',
        f'<line x1="0" x2="{W}" y1="{fy(0):.1f}" y2="{fy(0):.1f}" style="stroke:var(--color-neutral-700)" stroke-width="1" vector-effect="non-scaling-stroke"/>',
        f'<line x1="0" x2="{W}" y1="120" y2="120" style="stroke:var(--color-neutral-800)" stroke-width="1" vector-effect="non-scaling-stroke"/>',
        *(f'<path d="{d}" fill="{c}" opacity="{0.35 if proj else 0.9}"/>' for (c, proj), d in bars.items()),
        f'<path d="{area}" fill="{FITNESS_COLOR}" opacity="0.12"/>',
        f'<path d="{atl}" stroke="{FATIGUE_COLOR}" stroke-width="1.5" {stroke}/>',
        f'<path d="{ctl}" stroke="{FITNESS_COLOR}" stroke-width="2.25" {stroke}/>',
    ]
    if ctl_p:
        kids += [f'<path d="{atl_p}" stroke="{FATIGUE_COLOR}" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.8" {stroke}/>',
                 f'<path d="{ctl_p}" stroke="{FITNESS_COLOR}" stroke-width="2" stroke-dasharray="3 3" opacity="0.8" {stroke}/>']
    kids.append(f'<line x1="{x(ti):.1f}" x2="{x(ti):.1f}" y1="0" y2="{H}" stroke="#75798c" stroke-width="1" '
                f'stroke-dasharray="2 3" vector-effect="non-scaling-stroke"/>')
    if race_idx is not None:
        a = max(ti, race_idx - 3)
        kids.append(f'<rect x="{x(a):.1f}" y="{fy(15):.1f}" width="{x(race_idx) - x(a):.1f}" height="{fy(5) - fy(15):.1f}" '
                    f'fill="{FRESH}" opacity="0.16"/>')
        kids.append(f'<line x1="{x(race_idx) - 0.5:.1f}" x2="{x(race_idx) - 0.5:.1f}" y1="10" y2="{H}" stroke="{RACE_COLOR}" '
                    'stroke-width="1.5" vector-effect="non-scaling-stroke"/>')
    for key, (color, vals) in overlays.items():
        present = [v for v in vals if v is not None]
        if len(present) < 2:
            continue
        lo, hi = min(present), max(present)
        pts, started = [], False
        for i, v in enumerate(vals):
            if v is None:
                continue
            pts.append(f"{'L' if started else 'M'}{x(i):.1f} {16 + (1 - (v - lo) / ((hi - lo) or 1)) * 96:.1f}")
            started = True
        kids.append(f'<path class="ff-overlay" data-ov="{key}" d="{" ".join(pts)}" stroke="{color}" stroke-width="1.25" '
                    f'opacity="0.9" style="display:none" {stroke}/>')
    kids.append(f'<line class="ff-hover" x1="0" x2="0" y1="0" y2="{H}" style="stroke:var(--color-text)" stroke-width="1" opacity="0" '
                'vector-effect="non-scaling-stroke"/>')
    return (f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="ff-svg" '
            f'style="width:100%;height:{H}px;display:block;overflow:visible">{"".join(kids)}</svg>')


def _form_text(form: int) -> tuple[str, str]:
    z = training_load.form_zone(form)
    return z["color"], z["label"]


def _race_strip(goal: dict, rf: dict) -> str:
    ok = rf["on_target"]
    color = FRESH if ok else AMBER
    label = "Fresh · on target" if ok else f"{'Too fresh' if rf['too_fresh'] else rf['zone']['label']} · target +5 to +15"
    days = goal["days_left"]
    return f"""
      <div class="ff-race">
        {_ph("flag-checkered", 16, RACE_COLOR)}
        <div style="flex:1;min-width:0;font-size:12px"><div>{_e(goal["name"])}</div>
          <div style="color:var(--color-neutral-500)">{_dow_mon_day(goal["date"])} · {days} day{"" if days == 1 else "s"} · projected from plan</div></div>
        <div style="text-align:right"><div style="font-size:15px;color:{color}">{_signed(rf["form"])}</div>
          <div style="font-size:10px;color:{color}">{label}</div></div>
      </div>"""


def _ph(name, size=16, color=None) -> str:
    from tools.dashboard import _ph as ph
    return ph(name, size, color)


def _mspark(vals: list, color: str, gid: str) -> str:
    present = [v for v in vals if v is not None]
    if len(present) < 2:
        return '<div style="height:30px"></div>'
    w, h = 150, 30
    lo, hi = min(present), max(present)
    pts, started = [], False
    xs = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        px = i / (len(vals) - 1) * w
        py = h - 2 - (v - lo) / ((hi - lo) or 1) * (h - 6)
        pts.append(f"{'L' if started else 'M'}{px:.1f} {py:.1f}")
        xs.append(px)
        started = True
    line = " ".join(pts)
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="width:100%;height:{h}px;display:block">'
            f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{color}" stop-opacity="0.34"/>'
            f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>'
            f'<path d="{line} L{xs[-1]:.1f} {h} L{xs[0]:.1f} {h} Z" fill="url(#{gid})"/>'
            f'<path d="{line}" fill="none" stroke="{color}" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>')


def _delta(daily: dict[str, float], dates: list[str], lower_better: bool) -> tuple[str, str]:
    """This range's average vs the range before it."""
    vals = [daily.get(d) for d in dates]
    cur = [v for v in vals if v is not None]
    if not cur or not dates:
        return "", ""
    first = date.fromisoformat(dates[0])
    span = len(dates)
    prev = [v for d, v in daily.items() if (first - date.fromisoformat(d)).days in range(1, span + 1)]
    if not prev:
        return "", ""
    diff = round(sum(cur) / len(cur) - sum(prev) / len(prev))
    good = diff <= 0 if lower_better else diff >= 0
    return (f"+{diff}" if diff > 0 else str(diff)), ("#7fc9b0" if good else "#cf8a80")


def render(fit: dict | None, goal: dict | None, trends: dict | None, days: int, uid: str) -> str:
    """The fitness & fatigue card and the four metric cards for one range
    (``uid`` keeps element ids unique across the range sets)."""
    metric_daily = {k: _metric_daily(trends, k) for k, *_ in METRICS}
    series = (fit or {}).get("series") or []
    today = (fit or {}).get("today")
    win, ti = window(series, today, days) if series and today else ([], -1)
    has_chart = len(win) >= 2 and ti >= 0

    cards_html = ""
    actual_dates = [p["date"] for p in win[:ti + 1]] if has_chart else []
    if not actual_dates:
        daily_dates = sorted(set().union(*[set(v) for v in metric_daily.values()]))[-days:]
        actual_dates = daily_dates
    for key, label, unit, lower_better, color in METRICS:
        daily = metric_daily[key]
        vals = [daily.get(d) for d in actual_dates]
        current = next((v for v in reversed(vals) if v is not None), None)
        delta, delta_color = _delta(daily, actual_dates, lower_better)
        tag = ('<div class="ff-tag" style="font-size:10px;color:var(--color-neutral-600)">Overlay on chart</div>'
               if has_chart else "")
        cards_html += f"""
        <button type="button" class="ff-metric" data-ov-key="{key}" data-color="{color}" data-label="{_e(label)}" data-unit="{_e(unit)}"
            {"" if has_chart else "disabled"}>
          <div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px">
            <span class="kicker">{_e(label)}</span><span style="font-size:11px;color:{delta_color}">{delta}</span></div>
          <div style="display:flex;align-items:baseline;gap:4px"><span class="ff-mval" data-today="{_e(round(current) if current is not None else "—")}"
              style="font-size:22px;line-height:1">{_e(round(current) if current is not None else None)}</span>
            <span style="font-size:11px;color:var(--color-neutral-500)">{_e(unit)}</span></div>
          {_mspark(vals, color, f"g-{key}-{uid}")}
          {tag}
        </button>"""
    metrics_block = f"""
    <div class="ff-metrics-wrap">
      {'<div style="font-size:11px;color:var(--color-neutral-600)">Tap a card to overlay it on the chart</div>' if has_chart else ""}
      <div class="ff-metrics">{cards_html}</div>
    </div>"""
    if not has_chart:
        return f'<div class="ff-wrap">{metrics_block}</div>'

    now = win[ti]
    first = win[0]
    d_ctl = round(now["fitness"] - first["fitness"])
    race_idx, race_strip = None, ""
    last_proj = win[-1]["date"] if len(win) - 1 > ti else None
    if goal and goal.get("days_left") is not None and 0 <= goal["days_left"] and (
            goal["date"] == today or (last_proj and today < goal["date"] <= last_proj)):
        race_idx = next((i for i, p in enumerate(win) if p["date"] == goal["date"]), None)
        rf = training_load.race_day_form(win, goal["date"])
        if race_idx is not None and rf:
            race_strip = _race_strip(goal, rf)
        else:
            race_idx = None
    overlays = {key: (color, [metric_daily[key].get(p["date"]) if i <= ti else None for i, p in enumerate(win)])
                for key, _, _, _, color in METRICS}
    form_color, form_label = _form_text(now["form"])
    days_json = [{"d": p["date"], "l": p["load"], "c": round(p["fitness"]), "a": round(p["fatigue"]), "f": p["form"],
                  "p": p["projected"], **{k: metric_daily[k].get(p["date"]) for k, *_ in METRICS}} for p in win]
    zones = "".join(f'<span class="ff-leg"><span style="background:{c}"></span>{label}</span>'
                    for _, _, label, c in training_load.FORM_ZONES)
    race_flag = ""
    race_leg = ""
    if race_idx is not None:
        right = (len(win) - 1 - race_idx) / (len(win) - 1) * 100
        race_flag = (f'<div class="ff-flag" style="right:{right:.1f}%">{_ph("flag-checkered", 12)}Race</div>')
        race_leg = (f'<span class="ff-leg"><span style="width:10px;height:7px;border:1px dashed {FRESH};'
                    f'background:color-mix(in srgb, {FRESH} 18%, transparent)"></span>Race-day target</span>')
    end_label = f"{_mon_day(win[-1]['date'])}{' (planned)' if win[-1]['projected'] else ''}"
    days_blob = json.dumps(days_json).replace("<", "\\u003c")
    zone_meta = json.dumps({z[1]: [z[0] if z[0] != float("-inf") else -999, z[2], z[3]] for z in training_load.FORM_ZONES})
    return f"""
    <div class="ff-wrap" data-ti="{ti}">
      <script type="application/json" class="ff-data">{days_blob}</script>
      <script type="application/json" class="ff-zones">{zone_meta}</script>
      <div class="card ff-card">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
          <div style="display:flex;align-items:center;gap:6px;color:var(--color-neutral-500)">{_ph("chart-line", 14)}<div class="kicker">Fitness &amp; fatigue</div></div>
          <div class="ff-when" style="font-size:11px;color:var(--color-neutral-500)" data-today="Today · load {now['load']}">Today · load {now['load']}</div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px">
          <div><div class="kicker ff-key"><span style="background:{FITNESS_COLOR}"></span>Fitness</div>
            <div class="ff-ctl" style="font-size:24px">{round(now['fitness'])}</div>
            <div class="ff-ctl-sub" style="font-size:11px;color:{'#4fae72' if d_ctl >= 0 else '#cf8a80'}"
                data-today="{'↑' if d_ctl >= 0 else '↓'} {abs(d_ctl)} since {_mon_day(first['date'])}"
                data-today-color="{'#4fae72' if d_ctl >= 0 else '#cf8a80'}">{'↑' if d_ctl >= 0 else '↓'} {abs(d_ctl)} since {_mon_day(first['date'])}</div></div>
          <div><div class="kicker ff-key"><span style="background:{FATIGUE_COLOR}"></span>Fatigue</div>
            <div class="ff-atl" style="font-size:24px">{round(now['fatigue'])}</div>
            <div style="font-size:11px;color:var(--color-neutral-500)">7-day</div></div>
          <div><div class="kicker">Form</div>
            <div class="ff-form" style="font-size:24px;color:{form_color}">{_signed(now['form'])}</div>
            <div class="ff-form-label" style="font-size:11px;color:{form_color}">{form_label}</div></div>
        </div>
        <div style="position:relative">
          {race_flag}
          <div class="ff-ovrow" hidden><span class="ff-ovdot"></span><span class="ff-ovlabel" style="color:var(--color-neutral-500)"></span><span class="ff-ovval" style="font-weight:500"></span></div>
          <div class="ff-chart">{_chart_svg(win, ti, race_idx, overlays)}</div>
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600);margin-top:4px">
            <span>{_mon_day(first['date'])}</span><span>{end_label}</span></div>
        </div>
        <div class="ff-legend">{zones}
          {'<span class="ff-leg"><span style="width:12px;height:0;border-top:1.5px dashed #9397ab;border-radius:0"></span>Planned</span>' if len(win) - 1 > ti else ""}
          {race_leg}
          <span class="ff-leg ff-ovleg" hidden><span class="ff-ovleg-sw" style="width:12px;height:2px"></span><span class="ff-ovleg-label"></span></span>
        </div>
        {race_strip}
      </div>
      {metrics_block}
    </div>"""


# ── desktop compare chart ────────────────────────────────────────────────────
# (key, label, unit, lower is better, colour); fitness / fatigue / form come
# from the load series, the rest from the trends metrics.
COMPARE_LINES = (
    ("fitness", "Fitness", "", False, FITNESS_COLOR),
    ("fatigue", "Fatigue", "", True, FATIGUE_COLOR),
    ("form", "Form", "", False, FRESH),
    ("hrv", "HRV", "ms", False, "#7fc9b0"),
    ("rhr", "Resting HR", "bpm", True, "#cf5a4e"),
    ("sleep_score", "Sleep score", "/100", False, "#6f9ce8"),
    ("training_load", "Acute load", "", True, "#b5abfc"),
    ("stress", "Stress", "avg", True, AMBER),
    ("steps", "Steps", "", False, "#4fae72"),
)
COMPARE_DEFAULT = ("fitness", "fatigue", "hrv")


def _pct_over_range(daily: dict[str, float], dates: list[str]) -> str:
    """"+18% over the range": this range's average against the range before."""
    cur = [daily[d] for d in dates if daily.get(d) is not None]
    if not cur or not dates:
        return ""
    first = date.fromisoformat(dates[0])
    prev = [v for d, v in daily.items() if v is not None and 1 <= (first - date.fromisoformat(d)).days <= len(dates)]
    if not prev or not sum(prev):
        return ""
    pct = round((sum(cur) / len(cur) - sum(prev) / len(prev)) / abs(sum(prev) / len(prev)) * 100)
    return f"{'+' if pct > 0 else ''}{pct}% over the range"


def compare(fit: dict | None, trends: dict | None, days: int) -> str:
    """Desktop Trends: any mix of the nine lines on one chart, each scaled to
    its own range so they can be compared, with a tile per line below that
    reads the range average, or the day under the cursor."""
    series = (fit or {}).get("series") or []
    today = (fit or {}).get("today")
    daily = {key: _metric_daily(trends, key) for key, *_ in COMPARE_LINES[3:]}
    daily.update({"fitness": {}, "fatigue": {}, "form": {}})
    if series and today:
        actual = [p for p in series if p["date"] <= today]
        for key, field in (("fitness", "fitness"), ("fatigue", "fatigue"), ("form", "form")):
            daily[key] = {p["date"]: p[field] for p in actual}
    dates = sorted(set().union(*[set(v) for v in daily.values()]))
    if today:
        dates = [d for d in dates if d <= today]
    dates = dates[-days:]
    if len(dates) < 2:
        return ""
    lines = []
    for key, label, unit, lower_better, color in COMPARE_LINES:
        vals = [daily[key].get(d) for d in dates]
        present = [v for v in vals if v is not None]
        lines.append({
            "key": key, "label": label, "unit": unit, "color": color, "vals": vals,
            "avg": round(sum(present) / len(present)) if present else None,
            "sub": _pct_over_range(daily[key], dates) if present else "No data in this range",
        })
    blob = json.dumps({"dates": dates, "lines": lines, "default": list(COMPARE_DEFAULT)}).replace("<", "\\u003c")
    chips = "".join(
        f'<button type="button" class="tc-chip" data-tc-key="{l["key"]}" style="--c:{l["color"]}"'
        f'{" disabled" if l["avg"] is None else ""}><span></span>{_e(l["label"])}</button>' for l in lines)
    return f"""
    <div class="tr-compare" data-tc>
      <script type="application/json" class="tc-data">{blob}</script>
      <div class="card" style="padding:16px;gap:14px">
        <div class="tc-chips">{chips}</div>
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px">
          <div style="font-size:12px;color:var(--color-neutral-500)">Each line is scaled to its own range so trends can be compared. Hover to read a day.</div>
          <div class="tc-date" style="font-size:12px;color:var(--color-neutral-300)"></div></div>
        <div class="tc-chart"></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--color-neutral-600)">
          <span>{_mon_day(dates[0])}</span><span>{_mon_day(dates[len(dates) // 2])}</span><span>{_mon_day(dates[-1])}</span></div>
      </div>
      <div class="tc-tiles"></div>
    </div>"""


TRENDS_CSS = """
.tr-compare { display:none; flex-direction:column; gap:12px; }
.tc-chips { display:flex; flex-wrap:wrap; gap:8px; }
.tc-chip { display:inline-flex; align-items:center; gap:7px; padding:6px 12px; border-radius:999px; border:1px solid var(--color-divider);
  background:transparent; color:var(--color-neutral-400); font:inherit; font-size:12px; cursor:pointer; }
.tc-chip span { width:8px; height:8px; border-radius:50%; background:var(--c); }
.tc-chip.on { color:var(--color-text); border-color:var(--c); background:color-mix(in srgb, var(--c) 14%, transparent); }
.tc-chip:disabled { opacity:.4; cursor:default; }
.tc-chart { height:340px; cursor:crosshair; }
.tc-chart svg { width:100%; height:100%; display:block; }
.tc-tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; }
.tc-tile { display:flex; flex-direction:column; gap:6px; padding:14px; border-radius:8px; background:var(--color-surface);
  box-shadow:var(--shadow-sm); border-top:3px solid var(--c); }
@media (min-width: 900px) {
  .tr-compare { display:flex; }
  .tr-more-title { display:block !important; }
}
.tr-more-title { display:none; }

.ff-wrap { grid-column:1/-1; display:flex; flex-direction:column; gap:12px; }
.ff-card { padding:16px; gap:12px; box-shadow:0 0 0 1px var(--color-accent-700); }
.ff-key { display:flex; align-items:center; gap:5px; }
.ff-key span { width:10px; height:2px; border-radius:2px; display:inline-block; }
.ff-chart { position:relative; cursor:crosshair; touch-action:none; }
.ff-flag { position:absolute; top:-2px; display:flex; align-items:center; gap:3px; font-size:10px; color:#b5abfc; z-index:1; pointer-events:none; }
.ff-ovrow { display:flex; align-items:center; gap:6px; font-size:12px; margin-bottom:6px; }
.ff-ovrow[hidden] { display:none; }
.ff-ovdot { width:8px; height:8px; border-radius:50%; display:inline-block; }
.ff-legend { display:flex; flex-wrap:wrap; gap:6px 12px; font-size:10px; color:var(--color-neutral-500); }
.ff-leg { display:flex; align-items:center; gap:5px; }
.ff-leg[hidden] { display:none; }
.ff-leg > span:first-child { width:7px; height:7px; border-radius:2px; display:inline-block; }
.ff-race { display:flex; align-items:center; gap:10px; padding:10px 12px; border-radius:8px; background:var(--color-inset); }
.ff-metrics-wrap { display:flex; flex-direction:column; gap:8px; }
.ff-metrics { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:10px; }
.ff-metric { display:flex; flex-direction:column; gap:6px; padding:12px; border-radius:8px; border:0; background:var(--color-surface);
  box-shadow:var(--shadow-sm); color:inherit; font:inherit; text-align:left; cursor:pointer; }
.ff-metric:disabled { cursor:default; }
"""

TRENDS_JS = """
(function () {
  // Desktop compare chart: chips pick the lines, the chart draws each scaled
  // to its own range, the tiles read the range average or the hovered day.
  var KEY = 'dash-trend-lines';
  var sel = null;
  function load(def) {
    if (sel) return sel;
    try { var v = JSON.parse(localStorage.getItem(KEY) || 'null'); if (Array.isArray(v)) sel = v; } catch (e) { /* no storage */ }
    return (sel = sel || def.slice());
  }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(sel)); } catch (e) { /* no storage */ } }
  function fmtDate(iso) {
    var d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + d.toLocaleDateString('en-US', { month: 'short' }) + ' ' + d.getDate();
  }
  var blocks = [];
  function fmtVal(v) { return v == null ? '\u2014' : Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : String(Math.round(v)); }
  document.querySelectorAll('[data-tc]').forEach(function (box) {
    var data = JSON.parse(box.querySelector('.tc-data').textContent);
    var chart = box.querySelector('.tc-chart'), tiles = box.querySelector('.tc-tiles'), dateEl = box.querySelector('.tc-date');
    var hover = null;
    var W = 800, H = 300, n = data.dates.length;
    function x(i) { return i / (n - 1) * W; }
    function draw() {
      var on = data.lines.filter(function (l) { return sel.indexOf(l.key) >= 0 && l.avg !== null; });
      box.querySelectorAll('.tc-chip').forEach(function (c) { c.classList.toggle('on', sel.indexOf(c.getAttribute('data-tc-key')) >= 0); });
      var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">';
      [20, 100, 180, 260].forEach(function (y) {
        svg += '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '" stroke="var(--color-divider-soft)" vector-effect="non-scaling-stroke"></line>';
      });
      on.forEach(function (l) {
        var present = l.vals.filter(function (v) { return v != null; });
        var lo = Math.min.apply(null, present), hi = Math.max.apply(null, present), d = '', started = false;
        l.vals.forEach(function (v, i) {
          if (v == null) { started = false; return; }
          var y = 20 + (1 - (v - lo) / ((hi - lo) || 1)) * 240;
          d += (started ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y.toFixed(1);
          started = true;
        });
        svg += '<path d="' + d + '" fill="none" stroke="' + l.color + '" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"></path>';
      });
      if (hover !== null) svg += '<line x1="' + x(hover) + '" x2="' + x(hover) + '" y1="0" y2="' + H + '" stroke="var(--color-neutral-400)" vector-effect="non-scaling-stroke"></line>';
      chart.innerHTML = svg + '</svg>';
      dateEl.textContent = hover !== null ? fmtDate(data.dates[hover]) : '';
      tiles.innerHTML = on.map(function (l) {
        var v = hover !== null ? l.vals[hover] : l.avg;
        return '<div class="tc-tile" style="--c:' + l.color + '"><div class="kicker">' + l.label + '</div>'
          + '<div style="display:flex;align-items:baseline;gap:5px"><div style="font-size:30px;line-height:1">' + fmtVal(v) + '</div>'
          + '<div style="font-size:11px;color:var(--color-neutral-500)">' + l.unit + '</div></div>'
          + '<div style="font-size:11px;color:var(--color-neutral-500)">' + (hover !== null ? (l.vals[hover] == null ? 'No reading' : 'on this day') : l.sub) + '</div></div>';
      }).join('');
    }
    function at(e) {
      var r = chart.getBoundingClientRect();
      return Math.max(0, Math.min(n - 1, Math.round((e.clientX - r.left) / r.width * (n - 1))));
    }
    chart.addEventListener('mousemove', function (e) { var i = at(e); if (i !== hover) { hover = i; draw(); } });
    chart.addEventListener('mouseleave', function () { hover = null; draw(); });
    box.querySelectorAll('.tc-chip').forEach(function (c) {
      c.addEventListener('click', function () {
        var k = c.getAttribute('data-tc-key'), i = sel.indexOf(k);
        if (i >= 0) sel.splice(i, 1); else sel.push(k);
        save(); blocks.forEach(function (b) { b(); });
      });
    });
    load(data.default);
    blocks.push(draw);
    draw();
  });
})();

(function () {
  function fmtDate(iso) {
    var d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + d.toLocaleDateString('en-US', { month: 'short' }) + ' ' + d.getDate();
  }
  function zoneOf(zones, f) {
    var keys = Object.keys(zones);
    for (var i = 0; i < keys.length; i++) if (f > zones[keys[i]][0]) return zones[keys[i]];
    return zones[keys[keys.length - 1]];
  }
  document.querySelectorAll('.ff-wrap[data-ti]').forEach(function (wrap) {
    var days = JSON.parse(wrap.querySelector('.ff-data').textContent);
    var zones = JSON.parse(wrap.querySelector('.ff-zones').textContent);
    var ti = parseInt(wrap.getAttribute('data-ti'), 10);
    var chart = wrap.querySelector('.ff-chart');
    var hover = wrap.querySelector('.ff-hover');
    var overlay = null;
    var q = function (sel) { return wrap.querySelector(sel); };

    function show(i) {
      var now = i === null || i === ti;
      var day = days[now ? ti : i];
      q('.ff-ctl').textContent = day.c;
      q('.ff-atl').textContent = day.a;
      var z = zoneOf(zones, day.f);
      q('.ff-form').textContent = (day.f > 0 ? '+' : '') + day.f;
      q('.ff-form').style.color = z[2];
      q('.ff-form-label').textContent = z[1];
      q('.ff-form-label').style.color = z[2];
      var sub = q('.ff-ctl-sub');
      sub.textContent = now ? sub.getAttribute('data-today') : '42-day';
      sub.style.color = now ? sub.getAttribute('data-today-color') : '#9397ab';
      var when = q('.ff-when');
      when.textContent = now ? when.getAttribute('data-today') : fmtDate(day.d) + ' · ' + (day.p ? 'planned ' : 'load ') + day.l;
      if (hover) {
        var x = days.length > 1 ? (i === null ? 0 : i) / (days.length - 1) * 360 : 0;
        hover.setAttribute('x1', x); hover.setAttribute('x2', x);
        hover.setAttribute('opacity', i === null ? 0 : 0.6);
      }
      wrap.querySelectorAll('.ff-metric').forEach(function (m) {
        var v = m.querySelector('.ff-mval');
        var key = m.getAttribute('data-ov-key');
        if (now) v.textContent = v.getAttribute('data-today');
        else v.textContent = day.p || day[key] == null ? '—' : Math.round(day[key]);
      });
      if (overlay) {
        var m = wrap.querySelector('.ff-metric[data-ov-key="' + overlay + '"]');
        var val = now ? m.querySelector('.ff-mval').getAttribute('data-today')
                      : (day.p || day[overlay] == null ? '—' : Math.round(day[overlay]));
        q('.ff-ovval').textContent = val + (val === '—' ? '' : ' ' + m.getAttribute('data-unit'));
      }
    }
    function indexFrom(e) {
      var r = chart.getBoundingClientRect();
      var cx = e.touches && e.touches.length ? e.touches[0].clientX : e.clientX;
      return Math.round(Math.max(0, Math.min(1, (cx - r.left) / r.width)) * (days.length - 1));
    }
    function scrub(e) { show(indexFrom(e)); }
    chart.addEventListener('mousemove', scrub);
    chart.addEventListener('touchstart', scrub, { passive: true });
    chart.addEventListener('touchmove', scrub, { passive: true });
    chart.addEventListener('mouseleave', function () { show(null); });
    chart.addEventListener('touchend', function () { show(null); });

    wrap.querySelectorAll('.ff-metric').forEach(function (m) {
      m.addEventListener('click', function () {
        var key = m.getAttribute('data-ov-key');
        overlay = overlay === key ? null : key;
        wrap.querySelectorAll('.ff-overlay').forEach(function (p) {
          p.style.display = p.getAttribute('data-ov') === overlay ? '' : 'none';
        });
        wrap.querySelectorAll('.ff-metric').forEach(function (c) {
          var on = c.getAttribute('data-ov-key') === overlay;
          c.style.boxShadow = on ? '0 0 0 1px ' + c.getAttribute('data-color') : '';
          var tag = c.querySelector('.ff-tag');
          if (tag) {
            tag.textContent = on ? 'On chart · tap to remove' : 'Overlay on chart';
            tag.style.color = on ? c.getAttribute('data-color') : '';
          }
        });
        var row = q('.ff-ovrow'), leg = q('.ff-ovleg');
        row.hidden = leg.hidden = !overlay;
        if (overlay) {
          var color = m.getAttribute('data-color');
          q('.ff-ovdot').style.background = color;
          q('.ff-ovval').style.color = color;
          q('.ff-ovlabel').textContent = m.getAttribute('data-label');
          q('.ff-ovleg-sw').style.background = color;
          q('.ff-ovleg-label').textContent = m.getAttribute('data-label') + ' (scaled)';
        }
        show(null);
      });
    });
  });
})();
"""
