# tools/season_view.py
"""The Season view: one season on one timeline.

    GET /training-plan/seasons/{id}   — the season's timeline, goals, objectives and numbers
    GET /training-plan/season         — 303 to the active plan's season (else the newest active
                                        season; else a page that says there is none yet)

One column per week across every plan of the season: a phase band above, the
planned hours as bars (recovery weeks lighter, weeks with only a target hatched,
weeks with neither an empty slot, weeks no plan covers a dashed unplanned slot),
race markers by priority (A filled, B ring, C small) and a today line. The
numbers come from tools/season_timeline.py; this module only draws them.
"""
import math
from datetime import date, timedelta

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from tools import season_doc, season_service
from tools.plan_service import PlanStorageUnavailable
from tools.plan_today import DEFAULT_PHASE_COLOR, PHASE_COLORS
from tools.season_timeline import fmt_hours
from tools.training_plan import _NO_STORE, _e, _page, _token, _unavailable, _url

_STYLE = """
<style>
main { max-width:1180px; }
.season-head { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; flex-wrap:wrap; }
.view-switch { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; font-size:.85rem; }
.view-switch a, .view-switch span { padding:.4rem .85rem; color:var(--fg); text-decoration:none; }
.view-switch a + a, .view-switch a + span { border-left:1px solid var(--line); }
.view-switch a:hover { background:color-mix(in srgb, var(--fg) 7%, transparent); }
.view-switch span { color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); }
@media (max-width: 899px) { .view-switch .month-only { display:none; } }
.warnings { margin:0 0 .9rem; padding:.7rem 1rem .7rem 1.6rem; border-radius:10px; box-shadow:0 0 0 1px var(--warn); color:var(--warn); font-size:.88rem; }
.cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); gap:.8rem; margin-bottom:.9rem; }
.cards .card { margin:0; display:flex; flex-direction:column; gap:.25rem; min-width:0; }
.kicker { font-size:.72rem; letter-spacing:.05em; text-transform:uppercase; color:var(--accent); }
.big { font-size:1.05rem; font-weight:500; }
.stats { display:flex; flex-wrap:wrap; gap:.6rem 2.2rem; margin:.2rem 0 1rem; }
.stat { display:flex; flex-direction:column; min-width:0; }
.stat b { font-size:1.7rem; font-weight:500; line-height:1.1; font-variant-numeric:tabular-nums; }
.stat span { font-size:.8rem; color:var(--muted); }
.pill { display:inline-block; font-size:.7rem; letter-spacing:.04em; text-transform:uppercase; padding:1px 8px; border-radius:999px; border:1px solid var(--muted); color:var(--muted); width:fit-content; }
.pill.met { border-color:var(--ok); color:var(--ok); }
.pill.on_track { border-color:var(--accent); color:var(--accent); }
.legend { display:flex; gap:.5rem 1.1rem; flex-wrap:wrap; font-size:.8rem; color:var(--muted); margin:0 0 .6rem; }
.legend span { display:inline-flex; align-items:center; gap:.4rem; }
.legend i { display:inline-block; width:12px; height:12px; border-radius:3px; }
.season { position:relative; overflow-x:auto; background:var(--surface); box-shadow:0 0 0 1px var(--line);
  border-radius:10px; -webkit-overflow-scrolling:touch; }
.season svg { display:block; }
.season text { font-family:inherit; }
.s-grid { stroke:var(--line); stroke-width:1; }
.s-axis { fill:var(--muted); font-size:11px; }
.s-plan { fill:var(--muted); font-size:11px; }
.s-phase { font-size:10.5px; fill:#101421; font-weight:500; }
.s-gapband { fill:none; stroke:var(--muted); stroke-width:1; stroke-dasharray:3 3; }
.s-gaptxt { fill:var(--muted); font-size:10.5px; }
.s-seam { stroke:var(--muted); stroke-width:1; stroke-dasharray:3 4; opacity:.7; }
.s-empty, .s-gap { fill:none; stroke:var(--muted); stroke-width:1; stroke-dasharray:3 3; }
.s-gap { fill:color-mix(in srgb, var(--muted) 9%, transparent); }
.s-today { stroke:var(--accent); stroke-width:1.5; }
.s-today-l { fill:var(--accent); font-size:11px; font-weight:600; }
.s-stem { stroke:var(--muted); stroke-width:1; stroke-dasharray:2 3; }
.s-rA { fill:var(--accent); }
.s-rB { fill:var(--surface); stroke:var(--accent); stroke-width:2; }
.s-rC { fill:var(--muted); }
.s-rl { fill:var(--fg); font-size:11px; paint-order:stroke; stroke:var(--surface); stroke-width:3px; stroke-linejoin:round; }
.s-rl.a { font-weight:600; }
.s-wk, .s-race { cursor:pointer; outline:none; }
.s-wk.static { cursor:default; }
.s-wk rect.hit { fill:transparent; }
.s-wk:hover rect.hit, .s-wk:focus rect.hit { fill:color-mix(in srgb, var(--fg) 8%, transparent); }
.s-wk.today rect.hit { fill:color-mix(in srgb, var(--accent) 7%, transparent); }
.tip { position:absolute; pointer-events:none; z-index:2; background:var(--bg); color:var(--fg);
  box-shadow:0 0 0 1px var(--line), 0 6px 18px rgba(0,0,0,.25); border-radius:8px; padding:.45rem .6rem;
  font-size:.78rem; line-height:1.45; white-space:nowrap; display:none; }
.tip b { font-weight:600; }
table { width:100%; border-collapse:collapse; font-size:.84rem; }
th, td { text-align:left; padding:.45rem .5rem; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:500; font-size:.76rem; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.table-wrap { overflow-x:auto; margin-bottom:.5rem; }
table a { color:var(--accent); }
.prio { display:inline-block; min-width:1.4rem; text-align:center; font-weight:600; border-radius:6px; padding:0 .3rem; }
.prio.A { background:var(--accent); color:var(--bg); }
.prio.B { box-shadow:inset 0 0 0 2px var(--accent); color:var(--accent); }
.prio.C { box-shadow:inset 0 0 0 1px var(--muted); color:var(--muted); }
</style>
"""

_SCRIPT = """
(function () {
  var box = document.querySelector('.season'), tip = document.querySelector('.season .tip');
  if (!box) return;
  var today = box.querySelector('[data-today-x]');
  if (today) box.scrollLeft = Math.max(0, +today.getAttribute('data-today-x') - box.clientWidth / 3);
  function show(el) {
    tip.innerHTML = el.getAttribute('data-tip');
    tip.style.display = 'block';
    var r = el.getBoundingClientRect(), b = box.getBoundingClientRect();
    var x = r.left - b.left + box.scrollLeft + r.width / 2 - tip.offsetWidth / 2;
    x = Math.max(box.scrollLeft + 4, Math.min(x, box.scrollLeft + box.clientWidth - tip.offsetWidth - 4));
    tip.style.left = x + 'px';
    tip.style.top = Math.max(4, r.top - b.top - tip.offsetHeight - 6) + 'px';
  }
  function hide() { tip.style.display = 'none'; }
  box.querySelectorAll('[data-tip]').forEach(function (el) {
    el.addEventListener('mouseenter', function () { show(el); });
    el.addEventListener('focus', function () { show(el); });
    el.addEventListener('mouseleave', hide);
    el.addEventListener('blur', hide);
  });
  box.addEventListener('scroll', hide, { passive: true });
})();
"""

# Chart geometry (px). A week is a fixed width, so a long season scrolls sideways inside its frame.
WEEK, BAR, X0, PAD_R = 22, 14, 34, 14
TODAY_Y, PLAN_Y, BAND_Y, BAND_H = 12, 28, 36, 16
BARS_TOP, BARS_BOT = 78, 218
LANE0, LANE_H = BARS_BOT + 40, 18


def _day(value) -> date:
    return date.fromisoformat(str(value)[:10])


def _md(d: date, year: bool = False) -> str:
    return f"{d:%b} {d.day}" + (f", {d.year}" if year else "")


def _span(start: str, end: str) -> str:
    return f"{_md(_day(start))} – {_md(_day(end), True)}"


def _color(phase: str) -> str:
    return PHASE_COLORS.get(phase, DEFAULT_PHASE_COLOR)


# ── CHART ─────────────────────────────────────────────────────────────────────

def _chart(tl: dict, token: str | None) -> str:
    cols = tl["weeks"]
    origin = _day(tl["start"])
    n = len(cols)
    width = X0 + n * WEEK + PAD_R

    def x_col(i: int) -> float:
        return X0 + (i - 1) * WEEK

    def x_of(d: date) -> float:
        return X0 + (d - origin).days / 7 * WEEK + WEEK / 14

    peak = max((c["hours"] or 0 for c in cols), default=0)
    step = 2 if peak <= 8 else 4
    top = max(step, math.ceil(peak / step) * step)

    def y_of(h: float) -> float:
        return BARS_BOT - h / top * (BARS_BOT - BARS_TOP)

    hatches: dict[str, str] = {}

    def hatch(color: str) -> str:
        return hatches.setdefault(color, f"hatch{len(hatches)}")

    # races first: their label lanes decide the chart's height
    marks, lanes = [], []
    for r in tl["races"]:
        if r["col"] is None:
            continue
        x = x_of(_day(r["date"]))
        label = r["name"] + (f" · {r['priority']}" if not r["fromPlan"] else "")
        w = len(label) * 6.2 + 16
        right = x + w <= width - 4
        lo, hi = (x - 6, x + w) if right else (x - w, x + 6)
        lane = next((i for i, end in enumerate(lanes) if lo > end + 6), None)
        if lane is None:
            lane = len(lanes)
            lanes.append(hi)
        else:
            lanes[lane] = hi
        marks.append((r, x, lane, label, right))
    height = LANE0 + max(len(lanes), 1) * LANE_H + 6

    out = []
    # y gridlines
    for g in range(0, top + 1, step):
        y = y_of(g)
        out.append(f'<line class="s-grid" x1="{X0}" x2="{width - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="s-axis" x="{X0 - 6}" y="{y + 3.5:.1f}" text-anchor="end">{g}</text>')
    out.append(f'<text class="s-axis" x="{X0}" y="{BARS_TOP - 10}" dx="2">hours per week</text>')

    # phase band, with the gaps between plans as dashed "Unplanned" segments
    for b in tl["bands"]:
        x, w = x_col(b["from"]) + 1, (b["to"] - b["from"] + 1) * WEEK - 2
        if b["kind"] == "unplanned":
            out.append(f'<rect class="s-gapband" x="{x:.1f}" y="{BAND_Y}" width="{w:.1f}" height="{BAND_H}" rx="3"/>')
            if w >= 62:
                out.append(f'<text class="s-gaptxt" x="{x + w / 2:.1f}" y="{BAND_Y + 11.5}" text-anchor="middle">Unplanned</text>')
            continue
        out.append(f'<rect x="{x:.1f}" y="{BAND_Y}" width="{w:.1f}" height="{BAND_H}" rx="3" fill="{_color(b["name"])}"/>')
        if b["name"] and len(b["name"]) * 6.2 + 8 <= w:
            out.append(f'<text class="s-phase" x="{x + w / 2:.1f}" y="{BAND_Y + 11.5}" text-anchor="middle">{_e(b["name"])}</text>')

    # plan names and the seam where the next plan starts
    for i, p in enumerate(tl["plans"]):
        x = x_col(p["from"])
        if i:
            out.append(f'<line class="s-seam" x1="{x - 1:.1f}" x2="{x - 1:.1f}" y1="{PLAN_Y - 10}" y2="{BARS_BOT}"/>')
        room = (p["to"] - p["from"] + 1) * WEEK - 6
        fit = int(room / 6.2)
        if fit >= 4:
            name = p["event"] if len(p["event"]) <= fit else p["event"][:fit - 1] + "…"
            out.append(f'<text class="s-plan" x="{x + 3:.1f}" y="{PLAN_Y}">{_e(name)}</text>')

    # one column per week
    for c in cols:
        x = x_col(c["index"])
        cx = x + (WEEK - BAR) / 2
        kind = c["kind"]
        color = _color(c["phase"])
        dates = _span(c["start"], c["end"])
        if kind == "unplanned":
            title = f"Season week {c['index']}"
            tip = f"<b>{title}</b><br>{_e(dates)}<br>{_e(c['text'])}"
            label = f"{title}, {dates}: {c['text']}"
        else:
            title = f"{c['planEvent']} · week {c['weekNumber']}"
            tip = (f"<b>{_e(title)}</b> · season week {c['index']}<br>{_e(dates)}"
                   f"{' · ' + _e(c['phase']) if c['phase'] else ''}{' · recovery' if c['recovery'] else ''}"
                   f"<br>{_e(c['text'])}")
            label = f"{title}, {dates}: {c['text']}"
        cls = "s-wk" + (" static" if kind == "unplanned" else "") + (" today" if c["isToday"] else "")
        if kind == "unplanned":
            out.append(f'<g class="{cls}" data-tip="{_e(tip)}" tabindex="0" role="img" aria-label="{_e(label)}">')
        else:
            href = _url("/training-plan", token, plan=None if c["planStatus"] == "active" else c["planId"],
                        week=c["weekNumber"])
            out.append(f'<a class="{cls}" href="{_e(href)}" data-tip="{_e(tip)}" tabindex="0" aria-label="{_e(label)}">')
        out.append(f'<rect class="hit" x="{x:.1f}" y="{BAND_Y}" width="{WEEK}" height="{BARS_BOT - BAND_Y}" rx="4"/>')
        if kind == "unplanned":
            out.append(f'<rect class="s-gap" x="{cx:.1f}" y="{BARS_TOP}" width="{BAR}" height="{BARS_BOT - BARS_TOP}" rx="3"/>')
        elif kind == "empty":
            out.append(f'<rect class="s-empty" x="{cx:.1f}" y="{BARS_BOT - 24}" width="{BAR}" height="24" rx="3"/>')
        else:
            h = c["hours"] / top * (BARS_BOT - BARS_TOP)
            lighter = ' opacity=".5"' if c["recovery"] else ""
            if h < 1.5:   # a zero-hour week is a thin line on the axis, not nothing
                out.append(f'<rect x="{cx:.1f}" y="{BARS_BOT - 2}" width="{BAR}" height="2" fill="{color}"{lighter}/>')
            elif kind == "planned":
                out.append(f'<rect x="{cx:.1f}" y="{BARS_BOT - h:.1f}" width="{BAR}" height="{h:.1f}" rx="2" fill="{color}"{lighter}/>')
            else:   # target only: hatched
                out.append(f'<rect x="{cx + .6:.1f}" y="{BARS_BOT - h + .6:.1f}" width="{BAR - 1.2}" height="{h - 1.2:.1f}" rx="2" '
                           f'fill="url(#{hatch(color)})" stroke="{color}" stroke-width="1.2"{lighter}/>')
        out.append("</a>" if kind != "unplanned" else "</g>")

    # month ticks along the bottom
    m, seen = date(origin.year, origin.month, 1), set()
    end = _day(tl["end"])
    while m <= end:
        if m >= origin:
            x = x_of(m) - WEEK / 14
            out.append(f'<line class="s-grid" x1="{x:.1f}" x2="{x:.1f}" y1="{BARS_BOT}" y2="{BARS_BOT + 6}"/>')
        lx = max(x_of(m) - WEEK / 14, X0) + 3
        if (m.year, m.month) not in seen and lx < width - 30:
            seen.add((m.year, m.month))
            out.append(f'<text class="s-axis" x="{lx:.1f}" y="{BARS_BOT + 19}">{m:%b}{" ’" + str(m.year)[2:] if m.month == 1 or m == date(origin.year, origin.month, 1) else ""}</text>')
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)

    # race markers by priority: A filled, B ring, C small
    for r, x, lane, label, right in marks:
        ly = LANE0 + lane * LANE_H
        kind = r["priority"]
        out.append(f'<line class="s-stem" x1="{x:.1f}" x2="{x:.1f}" y1="{BARS_BOT + 24}" y2="{ly}"/>')
        meta = [season_doc.RACE_TYPES.get(r["type"], "Race") if not r["fromPlan"] else "From the plan",
                f"{r['priority']} race" if not r["fromPlan"] else None, r["location"] or None]
        tip = (f"<b>{_e(r['name'])}</b><br>{_e(_md(_day(r['date']), True))} · {_e(' · '.join(m for m in meta if m))}"
               + (f"<br>Goal: {_e(r['goal'])}" if r["goal"] else ""))
        out.append(f'<g class="s-race" data-tip="{_e(tip)}" tabindex="0" role="img" '
                   f'aria-label="{_e(r["name"] + ", " + r["date"] + ", priority " + kind)}">')
        if kind == "A":
            out.append(f'<circle class="s-rA" cx="{x:.1f}" cy="{ly}" r="6"/>')
        elif kind == "B":
            out.append(f'<circle class="s-rB" cx="{x:.1f}" cy="{ly}" r="5"/>')
        else:
            out.append(f'<circle class="s-rC" cx="{x:.1f}" cy="{ly}" r="3.5"/>')
        anchor = "" if right else ' text-anchor="end"'
        out.append(f'<text class="s-rl{" a" if kind == "A" else ""}" x="{x + 10 if right else x - 10:.1f}" '
                   f'y="{ly + 3.8}"{anchor}>{_e(label)}</text></g>')

    t = tl["today"]
    if t["col"] is not None:
        tx = x_of(_day(t["date"]))
        out.append(f'<line class="s-today" data-today-x="{tx:.0f}" x1="{tx:.1f}" x2="{tx:.1f}" y1="{TODAY_Y + 3}" y2="{BARS_BOT + 24}"/>')
        anchor = "" if tx + 40 < width else ' text-anchor="end"'
        out.append(f'<text class="s-today-l" x="{tx + (4 if not anchor else -4):.1f}" y="{TODAY_Y}"{anchor}>Today</text>')

    defs = "".join(
        f'<pattern id="{pid}" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<rect width="5" height="5" fill="{color}" fill-opacity=".2"/><rect width="2" height="5" fill="{color}"/></pattern>'
        for color, pid in hatches.items())
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="group" '
            f'aria-label="Season timeline of {n} weeks across {len(tl["plans"])} plan(s); the tables below have the same numbers">'
            f"<defs>{defs}</defs>" + "".join(out) + "</svg>")


def _legend() -> str:
    hatch = ("background:repeating-linear-gradient(45deg,var(--fg) 0 2px,transparent 2px 5px);"
             "border:1px solid var(--fg)")
    return (
        '<div class="legend">'
        '<span><i style="background:var(--muted)"></i>Planned hours (colour = phase)</span>'
        '<span><i style="background:var(--muted);opacity:.5"></i>Recovery week</span>'
        f'<span><i style="{hatch}"></i>Target hours, no workouts yet</span>'
        '<span><i style="border:1px dashed var(--muted)"></i>Empty or unplanned week</span>'
        '<span><i style="background:var(--accent);border-radius:50%"></i>A race</span>'
        '<span><i style="box-shadow:inset 0 0 0 2px var(--accent);border-radius:50%"></i>B race</span>'
        '<span><i style="background:var(--muted);border-radius:50%;transform:scale(.6)"></i>C race</span>'
        '<span><i style="background:var(--accent);width:2px"></i>Today</span></div>'
    )


# ── PAGE ──────────────────────────────────────────────────────────────────────

def _stats(tl: dict) -> str:
    tot, nxt = tl["totals"], tl["nextARace"]
    includes = " · includes targets" if tot["includesTargets"] else ""
    avg = fmt_hours(tot["averagePerWeek"]) if tot["averagePerWeek"] is not None else "—"
    if nxt:
        when = (f"{nxt['weeks']} wk" if nxt["days"] >= 7 else f"{nxt['days']} d")
        race = (f'{_e(nxt["name"])} · {_e(_md(_day(nxt["date"]), True))} ({nxt["days"]} days)')
    else:
        when, race = "—", "No A race ahead"
    weeks = f"over {tot['weeksWithHours']} week(s) with hours"
    return (
        '<div class="stats">'
        f'<div class="stat"><b>{tot["hours"]:.0f} h</b><span>planned total{includes}</span></div>'
        f'<div class="stat"><b>{avg}</b><span>average per week, {weeks}{includes}</span></div>'
        f'<div class="stat"><b>{when}</b><span>to the next A race · {race}</span></div>'
        f'<div class="stat"><b>{len(tl["weeks"])}</b><span>weeks in the timeline'
        f'{" · " + str(tot["unplannedWeeks"]) + " unplanned" if tot["unplannedWeeks"] else ""}</span></div>'
        "</div>"
    )


def _link_line(item: dict, races: dict) -> str:
    bits = []
    if item.get("raceId") in races:
        bits.append(_e(races[item["raceId"]]["name"]))
    if item.get("targetDate"):
        bits.append("by " + _e(_md(_day(item["targetDate"]), True)))
    return f'<span class="sub">{" · ".join(bits)}</span>' if bits else ""


def _goals(season: dict) -> str:
    races = {r["id"]: r for r in season["races"]}
    if not season["goals"]:
        return '<div class="card sub">No goals yet — add up to three under Edit season.</div>'
    return '<div class="cards">' + "".join(
        f'<div class="card"><span class="kicker">Goal {i}</span><span class="big">{_e(g["text"])}</span>{_link_line(g, races)}</div>'
        for i, g in enumerate(season["goals"], 1)) + "</div>"


def _objectives(season: dict) -> str:
    races = {r["id"]: r for r in season["races"]}
    if not season["objectives"]:
        return '<div class="card sub">No objectives yet — add up to four under Edit season.</div>'
    return '<div class="cards">' + "".join(
        f'<div class="card"><span class="kicker">Objective {i}</span><span class="big">{_e(o["text"])}</span>'
        f'<span class="pill {o["status"]}">{_e(season_doc.OBJECTIVE_STATUS_LABELS[o["status"]])}</span>{_link_line(o, races)}</div>'
        for i, o in enumerate(season["objectives"], 1)) + "</div>"


def _plans_table(season: dict, data: dict, token: str | None) -> str:
    by_id = {p["id"]: p for p in data["plans"]}
    cols = data["timeline"]["weeks"]
    rows = []
    for p in season["plans"]:
        tp = by_id.get(p["id"])
        mine = [c for c in cols if c["planId"] == p["id"]]
        hours = sum(c["hours"] for c in mine if c["hours"] is not None)
        targets = any(c["kind"] == "target" for c in mine)
        phases = []
        for c in mine:
            if c["phase"] and c["phase"] not in phases:
                phases.append(c["phase"])
        href = _url("/training-plan", token, plan=None if p["status"] == "active" else p["id"])
        rows.append(
            f'<tr><td><a href="{_e(href)}">{_e(p["event"])}</a></td><td>{_e(p["status"])}</td>'
            f'<td>{_e(p["start"] or "?")} – {_e(p["end"] or "?")}</td><td class="num">{len(tp["weeks"]) if tp else 0}</td>'
            f'<td class="num">{hours:.1f} h{" *" if targets else ""}</td><td>{_e(", ".join(phases))}</td></tr>')
    if not rows:
        return '<p class="sub">No plans are linked to this season yet.</p>'
    star = '<p class="sub">* includes weeks with target hours only (no workouts yet).</p>' if any(
        c["kind"] == "target" for c in cols) else ""
    return ('<div class="table-wrap"><table><thead><tr><th>Plan</th><th>Status</th><th>Dates</th><th class="num">Weeks</th>'
            f'<th class="num">Hours</th><th>Phases</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>{star}')


def _races_table(data: dict, token: str | None) -> str:
    tl = data["timeline"]
    cols = {c["index"]: c for c in tl["weeks"]}
    if not tl["races"]:
        return '<p class="sub">No races yet — add them under Edit season.</p>'
    rows = []
    for r in tl["races"]:
        col = cols.get(r["col"]) if r["col"] else None
        if col and col["planId"]:
            href = _url("/training-plan", token, plan=None if col["planStatus"] == "active" else col["planId"],
                        week=col["weekNumber"])
            where = f'<a href="{_e(href)}">{_e(col["planEvent"])} · week {col["weekNumber"]}</a>'
        elif col:
            where = "Unplanned week"
        else:
            where = "Outside the timeline"
        rows.append(
            f'<tr><td><span class="prio {r["priority"]}">{r["priority"]}</span> {_e(r["name"])}'
            f'{" <span class=sub>(from the plan)</span>" if r["fromPlan"] else ""}</td>'
            f'<td>{_e(r["date"])}</td><td>{_e(season_doc.RACE_TYPES.get(r["type"], "Race") if not r["fromPlan"] else "")}</td>'
            f'<td>{_e(r["goal"])}</td><td>{where}</td></tr>')
    return ('<div class="table-wrap"><table><thead><tr><th>Race</th><th>Date</th><th>Type</th><th>Goal</th>'
            f'<th>Week in a plan</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _unassigned(plans: list[dict], token: str | None) -> str:
    if not plans:
        return ""
    items = "".join(
        f'<li><a href="{_e(_url("/training-plan", token, plan=None if p["status"] == "active" else p["id"]))}">{_e(p["event"])}</a>'
        f' <span class="sub">{_e(p["status"])} · {_e(p["start"] or "?")} – {_e(p["end"] or "?")}</span></li>'
        for p in plans)
    return ("<h2>Not in a season</h2>"
            '<div class="sub">Plans in no season don’t appear on a season timeline. Link them from a season’s page.</div>'
            f"<ul>{items}</ul>")


def render_season_view_html(data: dict, unassigned: list[dict], seasons: list[dict], token: str | None) -> str:
    season, tl = data["season"], data["timeline"]
    archived = season["status"] == "archived"
    others = [s for s in seasons if s["id"] != season["id"]]
    other_links = "".join(
        f'<a href="{_e(_url("/training-plan/seasons/" + s["id"], token))}">{_e(s["name"])}</a>' for s in others)
    body = (
        _STYLE
        + '<div class="season-head"><div>'
        f'<h1>{_e(season["name"])}</h1>'
        f'<div class="meta">{_e(_span(season["startDate"], season["endDate"]))} · '
        f'<span class="tag{" archived" if archived else ""}">{_e(season["status"])}</span></div></div>'
        '<nav class="view-switch" aria-label="Plan view">'
        f'<a href="{_e(_url("/training-plan", token, mode="week"))}">Week</a>'
        f'<a class="month-only" href="{_e(_url("/training-plan", token, mode="month"))}">Month</a>'
        '<span aria-current="page">Season</span></nav></div>'
        + ('<ul class="warnings"><li>' + "</li><li>".join(_e(w) for w in season["warnings"]) + "</li></ul>"
           if season["warnings"] else "")
        + "<h2>Goals</h2>" + _goals(season) + _stats(tl)
        + "<h2>Timeline</h2>"
        '<div class="meta">A column is a week of the season across all its plans; it opens that week in the plan. '
        "Hours come from the week’s workouts, or its target hours until it has workouts.</div>"
        + _legend()
        + f'<div class="season">{_chart(tl, token)}<div class="tip" role="tooltip"></div></div>'
        + "<h2>Objectives</h2>" + _objectives(season)
        + "<h2>Plans</h2>" + _plans_table(season, data, token)
        + "<h2>Races</h2>" + _races_table(data, token)
        + _unassigned(unassigned, token)
        + f'<div class="links"><a href="{_e(_url("/training-plan/seasons/" + season["id"] + "/edit", token))}">Edit season</a>'
        f'<a href="{_e(_url("/training-plan/seasons", token))}">All seasons</a>{other_links}'
        f'<a href="{_e(_url("/training-plan", token))}">Active plan</a></div>'
        f"<script>{_SCRIPT}</script>"
        '<script type="speculationrules">'
        '{"prerender":[{"where":{"selector_matches":".view-switch a"},"eagerness":"moderate"}]}'
        "</script>"
    )
    return _page(f"Season · {season['name']}", body, token)


def render_no_season_html(unassigned: list[dict], token: str | None) -> str:
    body = (
        _STYLE + "<h1>Season</h1>"
        '<div class="card">No season yet. A season groups your plans, races, goals and objectives on one timeline — '
        f'<a href="{_e(_url("/training-plan/seasons", token))}">create the first one</a>.</div>'
        + _unassigned(unassigned, token)
        + f'<div class="links"><a href="{_e(_url("/training-plan/plans", token))}">All plans</a></div>'
    )
    return _page("Season", body, token)


# ── ROUTES ────────────────────────────────────────────────────────────────────

async def serve_season_view(request):
    """GET /training-plan/seasons/{id}."""
    season_id = request.path_params["season_id"]

    def build():
        return (season_service.timeline(season_id), season_service.unassigned_plans(), season_service.list_seasons())

    try:
        data, unassigned, seasons = await run_in_threadpool(build)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    except LookupError:
        return RedirectResponse(_url("/training-plan/seasons", _token(request), error="No season with that id."),
                                status_code=303)
    return HTMLResponse(render_season_view_html(data, unassigned, seasons, _token(request)), headers=_NO_STORE)


async def serve_season_default(request):
    """GET /training-plan/season — the active plan's season."""
    token = _token(request)
    try:
        season_id = await run_in_threadpool(season_service.default_season_id)
        if season_id is None:
            unassigned = await run_in_threadpool(season_service.unassigned_plans)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    if season_id:
        return RedirectResponse(_url(f"/training-plan/seasons/{season_id}", token), status_code=303)
    return HTMLResponse(render_no_season_html(unassigned, token), headers=_NO_STORE)


ROUTES = [
    Route("/training-plan/season", serve_season_default, methods=["GET"]),
    Route("/training-plan/seasons/{season_id}", serve_season_view, methods=["GET"]),
]
