# tools/dashboard_activity.py
"""The dashboard's Activity tab: one week of training, compared with the week
before, against the active plan.

Top to bottom: the header (calendar button, week tag, week arrows), sport
filter pills (with counts), a summary with changes vs last week, the daily
load strip (coloured by each day's primary training benefit), sessions
coming up, and the week's log grouped by day — commutes folded into one
row, planned sessions that were missed shown in place, activities that did
a planned workout tagged with it. The calendar button opens a month view
(``render_calendar``, served by /dashboard/activity-calendar).

Server-rendered like the rest of the dashboard: one section per sport
filter, switched by the page's ``activity-filter`` radios (``?filter=``);
the day filter, commute groups and calendar are small bits of JS
(``ACTIVITY_JS``). Rendering here is pure — the week's plan sessions,
commute flags and result chips are fetched by ``week_extras``.
"""
import html
import logging
from datetime import date, timedelta
from urllib.parse import urlencode

from tools import training_load

logger = logging.getLogger(__name__)

# sport group → (label, colour, Phosphor icon)
SPORTS = {
    "bike": ("Bike", "#4fae72", "bicycle"),
    "swim": ("Swim", "#4aa7d8", "swimming-pool"),
    "run": ("Run", "#d9a441", "sneaker-move"),
    "multi": ("Multisport", "#9184d9", "flag-checkered"),
    "climb": ("Climb", "#a07fe0", "mountains"),
    "strength": ("Strength", "#a07fe0", "barbell"),
    "other": ("Other", "#e2734a", "volleyball"),
}
# Filter pills, in order. There's no Climb / Strength pill: those show under All.
FILTERS = (("all", "All"), ("triathlon", "Triathlon"), ("bike", "Bike"), ("swim", "Swim"),
           ("run", "Run"), ("other", "Other"))
FILTER_KEYS = tuple(k for k, _ in FILTERS)
_TRI = {"swim", "bike", "run", "multi"}
DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
UP, DOWN = "#4fae72", "#cf8a80"
PLAN_OUTLINE = "#75798c"
NEUTRAL_BAR = "#595d6c"
# Result chips: threshold metric → (label, sport group, unit).
CHIP_METRICS = {"ftp": ("FTP", "bike", "W"), "bike_lthr": ("Bike LTHR", "bike", "bpm"),
                "run_lthr": ("Run LTHR", "run", "bpm"), "threshold_pace_s": ("Threshold", "run", "/km"),
                "css_s": ("CSS", "swim", "/100m")}


# ── CLASSIFICATION ───────────────────────────────────────────────────────────

def sport_group(activity_type) -> str:
    t = str(activity_type or "").lower()
    if t in ("multi_sport", "triathlon", "duathlon"):
        return "multi"
    if "swim" in t:
        return "swim"
    if "bik" in t or "cycl" in t or "ride" in t:
        return "bike"
    if "run" in t:
        return "run"
    if "climb" in t or "boulder" in t:
        return "climb"
    if "strength" in t:
        return "strength"
    return "other"


def plan_sport_group(sport) -> str:
    return {"swim": "swim", "bike": "bike", "run": "run", "brick": "bike",
            "strength": "strength"}.get(sport or "", "other")


def matches(group: str, key: str) -> bool:
    if key == "all":
        return True
    if key == "triathlon":
        return group in _TRI
    return group == key


# ── FORMATTING ───────────────────────────────────────────────────────────────

def _e(v) -> str:
    return "&mdash;" if v is None else html.escape(str(v))


def dur(minutes) -> str:
    total = round(minutes or 0)
    h, m = divmod(total, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def key_metric(a: dict, group: str) -> str:
    """The one metric that matters per sport: power (or speed) on the bike,
    pace per 100 m in the pool, pace and HR on a run, HR otherwise."""
    hr = a.get("avg_hr")
    km, mins = a.get("distance_km") or 0, a.get("duration_min") or 0
    if group == "bike":
        if a.get("avg_power"):
            main = f"{round(a['avg_power'])} W avg"
        elif a.get("avg_speed_kph"):
            main = f"{a['avg_speed_kph']:g} km/h"
        elif km and mins:
            main = f"{km / (mins / 60):.1f} km/h"
        else:
            main = None
        return " · ".join(p for p in (main, f"HR {hr}" if hr else None) if p) or "Ride"
    if group == "swim" and km and mins:
        moving = a.get("moving_duration_min") or mins
        return f"{_mmss(moving * 60 / (km * 10))} /100m"
    if group == "run" and km and mins:
        return " · ".join(p for p in (f"{_mmss(mins * 60 / km)} /km", f"HR {hr}" if hr else None) if p)
    return f"HR {hr} avg" if hr else sport_label(a)


def sport_label(a: dict) -> str:
    return str(a.get("type") or "activity").replace("_", " ").title()


def _pct_change(cur: float, prev: float) -> str:
    if not prev:
        return "new" if cur else "–"
    p = round((cur - prev) / prev * 100)
    return f"{'↑' if p >= 0 else '↓'} {abs(p)}%"


def _ph(name: str, size: int = 16, color: str | None = None) -> str:
    from tools.dashboard import _ph as ph
    return ph(name, size, color)


def _tint(color: str, pct: int = 18) -> str:
    return f"color-mix(in srgb, {color} {pct}%, transparent)"


# ── DATA ─────────────────────────────────────────────────────────────────────

def week_extras(week_start: str, week_end: str, activities: list[dict], today: date) -> dict:
    """From PostgreSQL: the plan's sessions this week, which activities are
    commutes, and result chips (a threshold that changed on an activity's
    day). Empty without a database; each part fails on its own."""
    import db
    out = {"planned": [], "plan_total": None, "commutes": {}, "chips": {}}
    if not db.is_configured():
        return out
    start, end = date.fromisoformat(week_start), date.fromisoformat(week_start) + timedelta(days=6)
    try:
        from tools import planned_sessions
        out["planned"] = planned_sessions.planned_sessions(start, end, today)
        if out["planned"]:
            out["plan_total"] = sum(s["est_load"] or 0 for s in out["planned"])
    except Exception:  # noqa: BLE001
        logger.exception("Planned sessions unavailable for the Activity tab")
    try:
        from tools import commutes
        rows = [{"garmin_id": a.get("id"), "activity_type": a.get("type"), "distance_km": a.get("distance_km"),
                 "summary": a} for a in activities if a.get("id") is not None]
        out["commutes"] = commutes.commute_flags(rows)
    except Exception:  # noqa: BLE001
        logger.exception("Commute detection unavailable for the Activity tab")
    try:
        out["chips"] = result_chips(db.get_threshold_snapshots((start - timedelta(days=30)).isoformat(),
                                                               end.isoformat()), activities, start, end)
    except Exception:  # noqa: BLE001
        logger.exception("Result chips unavailable for the Activity tab")
    return out


def result_chips(snapshots: list[dict], activities: list[dict], start: date, end: date) -> dict[int, str]:
    """{activity id: "FTP 262 W ↑8"} — a threshold that changed on a day
    goes on that day's biggest activity of the matching sport. The plan's
    value wins over Garmin's when both changed."""
    series = {}
    for r in snapshots:
        series.setdefault((r["source"], r["metric"]), []).append((str(r["snapshot_date"])[:10], float(r["value"])))
    chips = {}
    for source in ("garmin", "plan"):   # plan last, so it wins
        for (src, metric), pts in series.items():
            if src != source or metric not in CHIP_METRICS:
                continue
            label, group, unit = CHIP_METRICS[metric]
            for (_, before), (day, value) in zip(pts, pts[1:]):
                if abs(value - before) < 1e-6 or not (start.isoformat() <= day <= end.isoformat()):
                    continue
                same_day = [a for a in activities if str(a.get("date") or "")[:10] == day
                            and sport_group(a.get("type")) == group and a.get("id") is not None]
                if not same_day:
                    continue
                target = max(same_day, key=lambda a: a.get("training_load") or 0)
                delta = value - before
                if unit in ("/km", "/100m"):
                    text = f"{label} {_mmss(value)}{unit} {'↓' if delta < 0 else '↑'}{round(abs(delta))} s"
                else:
                    text = f"{label} {round(value)} {unit} {'↑' if delta > 0 else '↓'}{round(abs(delta))}"
                chips[target["id"]] = text
    return chips


def _items(week: dict, extras: dict, today: date) -> list[dict]:
    """The week's done activities plus planned sessions (planned / missed),
    as one list of display items."""
    matched = {s["activity_id"]: s["name"] for s in extras.get("planned") or []
               if s["status"] == "done" and s.get("activity_id")}
    commutes = extras.get("commutes") or {}
    chips = extras.get("chips") or {}
    items = []
    for a in week.get("activities") or []:
        day = str(a.get("date") or "")[:10]
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        group = sport_group(a.get("type"))
        items.append({
            "kind": "done", "id": a.get("id"), "date": day, "dow": d.weekday(), "title": a.get("name") or sport_label(a),
            "group": group, "km": a.get("distance_km") or 0, "min": a.get("duration_min") or 0,
            "load": round(a.get("training_load") or 0),
            "benefit": training_load.benefit_key(a.get("training_effect_label")),
            "metric": key_metric(a, group), "commute": bool(commutes.get(a.get("id"))),
            "planned": matched.get(a.get("id")), "chip": chips.get(a.get("id")),
            "time": str(a.get("date") or ""),
        })
    for s in extras.get("planned") or []:
        if s["status"] == "done":
            continue
        d = date.fromisoformat(s["date"])
        items.append({
            "kind": s["status"], "id": None, "date": s["date"], "dow": d.weekday(), "title": s["name"] or "Session",
            "group": plan_sport_group(s["sport"]), "km": 0, "min": s.get("duration_min") or 0,
            "load": s.get("est_load") or 0, "benefit": s.get("benefit"),
            "metric": s.get("primary_zone") or ("Test" if s.get("is_test") else "Planned"),
            "commute": False, "planned": None, "chip": None, "time": s["date"],
        })
    return items


def _totals(items: list[dict]) -> dict:
    done = [i for i in items if i["kind"] == "done"]
    return {"km": sum(i["km"] for i in done), "min": sum(i["min"] for i in done),
            "load": sum(i["load"] for i in done), "n": len(done)}


# ── RENDER: PANEL ────────────────────────────────────────────────────────────

def week_url(token: str | None, offset: int) -> str:
    params = {"tab": "activity", "week": str(max(0, offset))}
    if token:
        params["token"] = token
    return f"/dashboard?{urlencode(params)}"


def _range_label(week_start: str, week_end: str, offset: int) -> str:
    from tools.dashboard import _format_week_range
    label = _format_week_range(week_start, week_end)
    if offset == 1:
        label += " · last week"
    elif offset > 1:
        label += f" · {offset} weeks ago"
    return label


def _nav_button(direction: str, token: str | None, offset: int, disabled: bool) -> str:
    icon = "caret-left" if direction == "prev" else "caret-right"
    label = "Previous week" if direction == "prev" else "Next week"
    if disabled:
        return f'<span class="act-circle act-disabled" aria-hidden="true">{_ph(icon, 14)}</span>'
    return (f'<a class="act-circle" href="{_e(week_url(token, offset))}" data-week="{offset}" '
            f'aria-label="{label}">{_ph(icon, 14)}</a>')


def _tag(offset: int, token: str | None) -> str:
    if offset <= 0:
        return '<span class="act-tag">This week</span>'
    return (f'<a class="act-tag act-tag-btn" href="{_e(week_url(token, 0))}" data-week="0">'
            f'{_ph("arrow-counter-clockwise", 11)}This week</a>')


def _change_line(text: str, up: bool | None) -> str:
    if not text:
        return ""
    color = UP if up else DOWN
    return f'<div style="font-size:11px;color:{color}">{text}</div>'


def _stat(label: str, value: str, change: str = "", up: bool | None = None, extra: str = "") -> str:
    return (f'<div><div class="kicker">{label}</div><div style="font-size:24px">{value}</div>'
            f'{_change_line(change, up)}{extra}</div>')


def _summary(items, prev_items, key: str, plan_total) -> str:
    cur = _totals(items)
    prev = _totals(prev_items) if prev_items is not None else None
    if prev:
        km_c, time_c = _pct_change(cur["km"], prev["km"]), f"{'↑' if cur['min'] >= prev['min'] else '↓'} {dur(abs(cur['min'] - prev['min']))}"
        load_c, n_c = _pct_change(cur["load"], prev["load"]), f"{'↑' if cur['n'] >= prev['n'] else '↓'} {abs(cur['n'] - prev['n'])}"
    else:
        km_c = time_c = load_c = n_c = ""
    planned = ""
    if key == "all" and plan_total:
        planned = f'<div style="font-size:10px;color:var(--color-neutral-500)">{cur["load"]} / {plan_total} planned</div>'
    by = {}
    for i in items:
        if i["kind"] == "done":
            by[i["group"]] = by.get(i["group"], 0) + i["min"]
    parts = sorted(by.items(), key=lambda kv: -kv[1])
    bar = "".join(f'<div title="{SPORTS[g][0]} {dur(m)}" style="width:{(m / cur["min"] * 100) if cur["min"] else 0:.1f}%;'
                  f'background:{SPORTS[g][1]}"></div>' for g, m in parts)
    legend = "".join(f'<span class="act-leg"><span style="background:{SPORTS[g][1]}"></span>{SPORTS[g][0]} {dur(m)}</span>'
                     for g, m in parts)
    kicker = '<div class="kicker" style="color:var(--color-neutral-600);margin-bottom:-8px">vs last week</div>' if prev else ""
    up = (lambda a, b: prev is not None and a >= b)
    return f"""
      <div class="card act-summary">
        {kicker}
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 12px">
          {_stat("Distance", f'{cur["km"]:.2f}<span style="font-size:12px;color:var(--color-neutral-500)"> km</span>', km_c, up(cur["km"], (prev or {}).get("km", 0)))}
          {_stat("Time", dur(cur["min"]), time_c, up(cur["min"], (prev or {}).get("min", 0)))}
          {_stat("Load", str(cur["load"]), load_c, up(cur["load"], (prev or {}).get("load", 0)), planned)}
          {_stat("Sessions", str(cur["n"]), n_c, up(cur["n"], (prev or {}).get("n", 0)))}
        </div>
        {f'<div class="act-bar">{bar}</div><div class="act-legend">{legend}</div>' if parts else ""}
      </div>"""


def _benefit_color(key) -> str:
    info = training_load.benefit_info(key)
    return info["color"] if info else NEUTRAL_BAR


def _strip(items, key: str, today_dow: int | None, plan_by_day: dict[int, int] | None) -> str:
    days = []
    for d in range(7):
        its = [i for i in items if i["dow"] == d and i["kind"] != "missed"]
        actual = sum(i["load"] for i in its if i["kind"] == "done")
        planned = sum(i["load"] for i in its if i["kind"] == "planned")
        top = max(its, key=lambda i: i["load"], default=None)
        days.append((actual, planned, top["benefit"] if top else None))
    show_plan = key == "all" and bool(plan_by_day)
    peak = max([max(a + p, (plan_by_day or {}).get(d, 0) if show_plan else 0) for d, (a, p, _) in enumerate(days)] + [1])
    cols, used = [], []
    for d, (actual, planned, benefit) in enumerate(days):
        if benefit and benefit not in used:
            used.append(benefit)
        color = _benefit_color(benefit) if benefit else NEUTRAL_BAR
        value = actual or planned
        plan_only = not actual and planned
        bar_style = (f"height:{value / peak * 100:.0f}%;background:transparent;border:1px dashed {color}" if plan_only
                     else f"height:{value / peak * 100:.0f}%;background:{color}")
        outline = ""
        if show_plan and actual and plan_by_day.get(d):
            outline = f'<div class="act-plan-outline" style="height:{plan_by_day[d] / peak * 100:.0f}%;border-color:{PLAN_OUTLINE}"></div>'
        today_cls = " today" if d == today_dow else ""
        cols.append(
            f'<button type="button" class="act-day{today_cls}" data-act-day="{d}" aria-label="{DOW[d]}">'
            f'<div class="act-day-load">{value or ""}</div>'
            f'<div class="act-day-track">{outline}<div class="act-day-bar" style="{bar_style}"></div></div>'
            f'<div class="act-day-letter">{DOW[d][0]}</div></button>')
    legend = "".join(f'<span class="act-leg"><span style="background:{_benefit_color(b)}"></span>'
                     f'{_e(training_load.benefit_info(b)["label"])}</span>' for b in used)
    return f"""
      <div class="card act-strip">
        <div style="display:flex;justify-content:space-between;align-items:baseline">
          <div class="kicker">Daily load</div><div class="act-strip-hint" style="font-size:11px;color:var(--color-neutral-500)">Tap a day to filter</div>
        </div>
        <div class="act-days">{"".join(cols)}</div>
        <div class="act-legend">{legend}</div>
      </div>"""


def _icon_tile(group: str, dashed: bool = False, muted: bool = False) -> str:
    label, color, icon = SPORTS[group]
    if muted:
        color = "#9397ab"
    if dashed:
        return f'<div class="act-tile" style="border:1px dashed {color};color:{color}">{_ph(icon, 18)}</div>'
    return f'<div class="act-tile" style="background:{_tint(color, 14 if muted else 18)};color:{color}">{_ph(icon, 18)}</div>'


def _day_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{DOW[d.weekday()]} {d.day}"


def _upcoming(items) -> str:
    rows = [i for i in items if i["kind"] == "planned"]
    if not rows:
        return ""
    body = "".join(f"""
        <div class="act-row act-planned" data-day="{i['dow']}">
          {_icon_tile(i['group'], dashed=True)}
          <div style="flex:1;min-width:0"><div class="act-title">{_e(i['title'])}</div>
            <div class="act-meta">{_day_label(i['date'])} · {_e(i['metric'])}</div></div>
          <div class="act-right"><div style="font-size:15px;color:var(--color-neutral-400)">{dur(i['min'])}</div>
            <div class="act-sub">{f"~{i['load']} load" if i['load'] else ""}</div></div>
        </div>""" for i in sorted(rows, key=lambda i: i["date"]))
    return f'<div class="act-upcoming" style="display:flex;flex-direction:column;gap:8px"><div class="section-title" style="margin:0">Coming up</div>{body}</div>'


def _right(i) -> tuple[str, str]:
    if i["km"]:
        return f"{i['km']:.2f} km", f"{dur(i['min'])} · {i['load']} load"
    return dur(i["min"]), f"{i['load']} load"


def _activity_row(i) -> str:
    primary, secondary = _right(i)
    tags = ""
    if i.get("chip"):
        tags += f'<span class="act-chip act-chip-result">{_e(i["chip"])}</span>'
    if i.get("planned"):
        tags += f'<span class="act-chip act-chip-plan">{_ph("check", 10)}{_e(i["planned"])}</span>'
    aid = int(i["id"])
    return f"""
        <div class="act-row actcard-click" onclick="openActivityModal({aid})" role="button" tabindex="0"
            onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();openActivityModal({aid})}}">
          {_icon_tile(i['group'])}
          <div style="flex:1;min-width:0"><div class="act-title">{_e(i['title'])}</div>
            <div class="act-tags"><span class="act-meta">{_e(i['metric'])}</span>{tags}</div></div>
          <div class="act-right"><div style="font-size:15px">{primary}</div><div class="act-sub">{secondary}</div></div>
        </div>"""


def _commute_group(rows) -> str:
    km = sum(r["km"] for r in rows)
    mins = sum(r["min"] for r in rows)
    load = sum(r["load"] for r in rows)
    kids = "".join(f"""
          <div class="act-commute-child actcard-click" onclick="openActivityModal({int(r['id'])})" role="button" tabindex="0">
            <div style="flex:1;min-width:0;font-size:12px;color:var(--color-neutral-300)">{_e(r['metric'])}</div>
            <div style="font-size:12px">{r['km']:.2f} km</div>
            <div class="act-sub" style="width:92px;text-align:right">{dur(r['min'])} · {r['load']} load</div>
          </div>""" for r in sorted(rows, key=lambda r: r["time"]))
    return f"""
        <div class="act-commute">
          <button type="button" class="act-commute-head" data-act-commute aria-expanded="false">
            {_icon_tile('bike', muted=True)}
            <div style="flex:1;min-width:0"><div class="act-title" style="display:flex;align-items:center;gap:6px">Commute &times;{len(rows)}
              <span class="act-caret">{_ph("caret-down", 12, "var(--color-neutral-500)")}</span></div>
              <div class="act-meta">{_e(rows[0]['title'])} · auto-grouped</div></div>
            <div class="act-right"><div style="font-size:15px">{km:.2f} km</div><div class="act-sub">{dur(mins)} · {load} load</div></div>
          </button>
          <div class="act-commute-kids">{kids}</div>
        </div>"""


def _missed_row(i) -> str:
    return f"""
        <div class="act-row act-missed">
          <div class="act-tile" style="border:1px dashed #75798c;color:#75798c">{_ph(SPORTS[i['group']][2], 18)}</div>
          <div style="flex:1;min-width:0"><div class="act-title" style="color:var(--color-neutral-400)">{_e(i['title'])}</div>
            <div style="font-size:11px;color:#cf8a80">Missed · {_e(i['metric'])}</div></div>
          <div style="font-size:15px;color:#75798c">{dur(i['min'])}</div>
        </div>"""


def _log(items, today: date, offset: int) -> str:
    groups = []
    for d in range(6, -1, -1):
        its = [i for i in items if i["dow"] == d and i["kind"] != "planned"]
        if not its:
            continue
        its.sort(key=lambda i: i["time"], reverse=True)
        commutes = [i for i in its if i["kind"] == "done" and i["commute"]]
        rows, placed = [], False
        for i in its:
            if i["kind"] == "missed":
                rows.append(_missed_row(i))
            elif i["commute"] and len(commutes) > 1:
                if not placed:
                    rows.append(_commute_group(commutes))
                    placed = True
            else:
                rows.append(_activity_row(i))
        done = [i for i in its if i["kind"] == "done"]
        summary = f"{dur(sum(i['min'] for i in done))} · {sum(i['load'] for i in done)} load" if done else ""
        is_today = offset == 0 and d == today.weekday()
        today_tag = '<span class="act-today">Today</span>' if is_today else ""
        groups.append(f"""
      <div class="act-group" data-day="{d}">
        <div class="act-group-head"><div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:13px;font-weight:500">{_day_label(its[0]['date'])}</span>{today_tag}</div>
          <span style="font-size:11px;color:var(--color-neutral-500)">{summary}</span></div>
        {"".join(rows)}
      </div>""")
    return "".join(groups)


def _pills(items) -> str:
    done = [i for i in items if i["kind"] == "done"]
    pills = []
    for key, label in FILTERS:
        count = sum(1 for i in done if matches(i["group"], key))
        if key != "all" and not count:
            continue
        pills.append(f'<label for="activity-filter-{key}">{label}<span class="act-count">{count}</span></label>')
    return f'<div class="activity-filterbar pillbar act-pills">{"".join(pills)}</div>'


SKELETON = """
      <div class="act-skel" aria-hidden="true">
        <div class="sk" style="height:190px"></div><div class="sk" style="height:96px"></div>
        <div class="sk" style="height:30px;width:70%;border-radius:999px"></div>
        <div class="sk" style="height:62px"></div><div class="sk" style="height:62px"></div>
      </div>"""


def render_panel(week: dict | None, prev_week: dict | None, extras: dict | None, offset: int,
                 today: date, token: str | None = None, err: str | None = None) -> str:
    """The Activity tab's ``<section class="tp-activity">`` for one week."""
    if not week:
        return (f'<section class="panel tabpanel tp-activity" data-week="{offset}">'
                f'<div class="err">Activity data unavailable — {_e(err or "no data")}</div></section>')
    extras = extras or {}
    week_start = str(week.get("week_start") or "")[:10]
    week_end_full = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat() if week_start else ""
    items = _items(week, extras, today)
    prev_items = _items(prev_week, {}, today) if prev_week is not None else None
    plan_by_day = {}
    for s in extras.get("planned") or []:
        d = date.fromisoformat(s["date"]).weekday()
        plan_by_day[d] = plan_by_day.get(d, 0) + (s.get("est_load") or 0)
    today_dow = today.weekday() if offset == 0 else None

    sections = []
    for key, label in FILTERS:
        its = [i for i in items if matches(i["group"], key)]
        prev = [i for i in prev_items if matches(i["group"], key)] if prev_items is not None else None
        log = _log(its, today, offset)
        upcoming = _upcoming(its)
        empty = "" if log or upcoming else (
            '<div class="card" style="padding:24px;align-items:center;font-size:12px;color:var(--color-neutral-500)">'
            'No activities match this filter.</div>')
        sections.append(f"""
    <div class="activity-filter-section activity-filter-{key}" style="flex-direction:column;gap:16px">
      {_summary(its, prev, key, extras.get("plan_total"))}
      {_strip(its, key, today_dow, plan_by_day)}
      {upcoming}
      <div class="act-log">{log}{empty}</div>
    </div>""")

    return f"""
    <section class="panel tabpanel tp-activity" data-week="{offset}" data-week-start="{_e(week_start)}" style="flex-direction:column;gap:16px">
      <div class="act-head">
        <div>
          <div style="display:flex;align-items:center;gap:8px">
            <button type="button" class="act-circle" data-cal-open aria-label="Open calendar">{_ph("calendar-blank", 15)}</button>
            <div style="font-family:var(--font-heading);font-size:20px;font-weight:500">Activity</div>{_tag(offset, token)}
          </div>
          <div style="font-size:12px;color:var(--color-neutral-500);margin-top:2px">{_e(_range_label(week_start, week_end_full, offset))}</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          {_nav_button("prev", token, offset + 1, False)}{_nav_button("next", token, offset - 1, offset <= 0)}
        </div>
      </div>
      {SKELETON}
      <div class="act-body">
        {_pills(items)}
        {"".join(sections)}
      </div>
      <div class="act-cal" hidden role="dialog" aria-modal="true" aria-label="Activity calendar">
        <div class="act-cal-backdrop" data-cal-close></div>
        <div class="act-cal-box"><div class="act-cal-body"><div class="sk" style="height:320px"></div></div></div>
      </div>
    </section>"""


# ── RENDER: CALENDAR ─────────────────────────────────────────────────────────

def calendar_data(year: int, month: int, today: date) -> dict:
    """A month grid's done activities and plan sessions, from PostgreSQL."""
    import db
    first = date(year, month, 1)
    grid_start = first - timedelta(days=first.weekday())
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    grid_end = last + timedelta(days=6 - last.weekday())
    acts = [r["summary"] for r in db.get_activities_in_range(grid_start.isoformat(),
                                                             (grid_end + timedelta(days=1)).isoformat())
            if r.get("summary")]
    try:
        from tools import planned_sessions
        planned = planned_sessions.planned_sessions(grid_start, grid_end, today)
    except Exception:  # noqa: BLE001
        logger.exception("Planned sessions unavailable for the calendar")
        planned = []
    return {"activities": acts, "planned": planned, "grid_start": grid_start, "grid_end": grid_end}


def render_calendar(year: int, month: int, today: date, cal: dict, filter_key: str = "all",
                    viewed_offset: int = 0) -> str:
    """The calendar modal's content for one month."""
    if filter_key not in FILTER_KEYS:
        filter_key = "all"
    grid_start, grid_end = cal["grid_start"], cal["grid_end"]
    items = _items({"activities": cal["activities"]}, {"planned": cal["planned"]}, today)
    items = [i for i in items if matches(i["group"], filter_key)]
    by_day = {}
    for i in items:
        by_day.setdefault(i["date"], []).append(i)
    this_monday = today - timedelta(days=today.weekday())
    m_km = m_min = m_load = m_n = 0
    used, rows = [], []
    day = grid_start
    while day <= grid_end:
        monday = day
        offset = (this_monday - monday).days // 7
        cells, w_min, w_load = [], 0, 0
        for _ in range(7):
            its = by_day.get(day.isoformat(), [])
            done = [i for i in its if i["kind"] == "done"]
            planned = [i for i in its if i["kind"] == "planned"]
            missed = [i for i in its if i["kind"] == "missed"]
            load, mins = sum(i["load"] for i in done), sum(i["min"] for i in done)
            w_min += mins
            w_load += load
            in_month = day.month == month
            if in_month:
                m_km += sum(i["km"] for i in done)
                m_min += mins
                m_load += load
                m_n += len(done)
            top = max(done or planned, key=lambda i: i["load"], default=None)
            color = _benefit_color(top["benefit"]) if top and top["benefit"] else "transparent"
            if top and top["benefit"] and top["benefit"] not in used:
                used.append(top["benefit"])
            dots = "".join(f'<span class="act-dot" style="background:{SPORTS[g][1]}"></span>'
                           for g in dict.fromkeys(i["group"] for i in done))
            dots += "".join(f'<span class="act-dot" style="box-shadow:inset 0 0 0 1px {SPORTS[i["group"]][1]}"></span>'
                            for i in planned)
            dots += "".join('<span class="act-dot" style="box-shadow:inset 0 0 0 1px #cf8a80"></span>' for _ in missed)
            underline = (f"background:{color}" if done else
                         (f"background:transparent;border-top:1px dashed {color}" if planned and top and top["benefit"] else ""))
            is_today = day == today
            cells.append(
                f'<div class="act-cell{" out" if not in_month else ""}{" today" if is_today else ""}">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:2px">'
                f'<span class="act-cell-num">{day.day}</span><span class="act-cell-load">{load or ""}</span></div>'
                f'<div class="act-dots">{dots}</div><div class="act-cell-bar" style="{underline}"></div></div>')
            day += timedelta(days=1)
        clickable = offset >= 0
        attrs = (f' data-cal-week="{offset}" role="button" tabindex="0" title="Open week of {monday.strftime("%b")} {monday.day}"'
                 if clickable else "")
        rows.append(
            f'<div class="act-cal-row{" sel" if clickable and offset == viewed_offset else ""}{" click" if clickable else ""}"{attrs}>'
            f'{"".join(cells)}<div class="act-cal-week"><span style="font-size:12px">{w_load or ""}</span>'
            f'<span class="act-sub">{dur(w_min) if w_min else ""}</span></div></div>')
    prev_m = date(year - (month == 1), (month - 2) % 12 + 1, 1)
    next_m = date(year + (month == 12), month % 12 + 1, 1)
    at_max = (year, month) >= (today.year, today.month)
    summary = (f"{m_km:.0f} km · {dur(m_min)} · {m_load} load · {m_n} sessions" if m_n else "No activities")
    filter_label = dict(FILTERS)[filter_key] if filter_key != "all" else "All sports"
    legend = "".join(f'<span class="act-leg"><span style="width:10px;height:3px;background:{_benefit_color(b)}"></span>'
                     f'{_e(training_load.benefit_info(b)["label"])}</span>' for b in used)
    title = date(year, month, 1).strftime("%B %Y")
    next_btn = (f'<span class="act-circle act-disabled" aria-hidden="true">{_ph("caret-right", 14)}</span>' if at_max else
                f'<button type="button" class="act-circle" data-cal-month="{next_m.strftime("%Y-%m")}" aria-label="Next month">{_ph("caret-right", 14)}</button>')
    return f"""
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <div style="display:flex;align-items:center;gap:8px">
          <button type="button" class="act-circle" data-cal-month="{prev_m.strftime("%Y-%m")}" aria-label="Previous month">{_ph("caret-left", 14)}</button>
          <div style="font-size:16px;font-weight:500;min-width:128px;text-align:center">{title}</div>
          {next_btn}
        </div>
        <button type="button" class="act-circle" style="border:0;color:var(--color-neutral-500)" data-cal-close aria-label="Close">{_ph("x", 15)}</button>
      </div>
      <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 12px;font-size:11px;color:var(--color-neutral-500)">
        <span>{summary}</span><span>{filter_label}</span></div>
      <div style="display:flex;flex-direction:column;gap:4px">
        <div class="act-cal-row act-cal-headrow"><span>M</span><span>T</span><span>W</span><span>T</span><span>F</span><span>S</span><span>S</span><span>Week</span></div>
        {"".join(rows)}
      </div>
      <div class="act-legend" style="border-top:1px solid var(--color-neutral-800);padding-top:10px">{legend}
        <span class="act-leg"><span class="act-dot" style="box-shadow:inset 0 0 0 1px #9397ab;background:transparent"></span>Planned</span>
        <span class="act-leg"><span class="act-dot" style="box-shadow:inset 0 0 0 1px #cf8a80;background:transparent"></span>Missed</span>
      </div>
      <div style="font-size:11px;color:var(--color-neutral-600)">Tap a week to open it.</div>"""


# ── STYLE / SCRIPT ───────────────────────────────────────────────────────────

ACTIVITY_CSS = """
.act-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
.act-circle { width:28px; height:28px; flex:0 0 auto; border-radius:999px; border:1px solid var(--color-divider);
  background:transparent; color:var(--color-text); display:grid; place-items:center; cursor:pointer; padding:0;
  text-decoration:none; font:inherit; }
.act-circle.act-disabled { color:#595d6c; opacity:.5; cursor:default; }
.act-tag { display:inline-flex; align-items:center; gap:4px; font-size:10px; letter-spacing:.08em; text-transform:uppercase;
  padding:2px 8px; border-radius:999px; color:var(--color-accent-200); background:color-mix(in srgb, var(--color-accent) 20%, transparent);
  text-decoration:none; }
.act-tag-btn { background:transparent; border:1px solid var(--color-accent-700); }
.act-pills { align-self:flex-start; max-width:100%; overflow-x:auto; scrollbar-width:none; }
.act-pills::-webkit-scrollbar { display:none; }
.act-pills label { padding:5px 10px; gap:5px; white-space:nowrap; flex:0 0 auto; }
.act-count { font-size:10px; opacity:.7; }
.act-summary { padding:16px; gap:14px; }
.act-bar { display:flex; height:10px; gap:2px; border-radius:999px; overflow:hidden; }
.act-legend { display:flex; flex-wrap:wrap; gap:6px 12px; font-size:10px; color:var(--color-neutral-500); }
.act-leg { display:flex; align-items:center; gap:5px; }
.act-leg > span:first-child { width:7px; height:7px; border-radius:2px; display:inline-block; }
.act-strip { padding:14px 16px 10px; gap:8px; }
.act-days { display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); gap:6px; }
.act-day { display:flex; flex-direction:column; align-items:center; gap:4px; background:transparent; border:0; padding:4px 0;
  border-radius:6px; cursor:pointer; color:inherit; font:inherit; transition:opacity .15s ease; }
.act-day-load { font-size:10px; color:var(--color-neutral-500); height:14px; }
.act-day-track { position:relative; height:56px; width:100%; display:flex; align-items:flex-end; justify-content:center; }
.act-plan-outline { position:absolute; bottom:0; left:20%; width:60%; border:1px dashed; border-radius:4px 4px 2px 2px; }
.act-day-bar { position:relative; width:60%; min-height:3px; border-radius:4px 4px 2px 2px; }
.act-day-letter { font-size:11px; color:var(--color-neutral-500); }
.act-day.today .act-day-letter { color:var(--color-accent-200); font-weight:600; }
.act-days.filtered .act-day { opacity:.45; }
.act-days.filtered .act-day.sel { opacity:1; }
.act-days.filtered .act-day.sel .act-day-letter { font-weight:600; }
.act-row { display:flex; align-items:center; gap:12px; padding:12px; border-radius:8px; background:var(--color-surface);
  box-shadow:var(--shadow-sm); }
.act-row.act-planned, .act-row.act-missed { background:transparent; box-shadow:none; border:1px dashed var(--color-neutral-800); padding:11px 12px; }
.act-row.act-missed { opacity:.75; }
.act-tile { width:34px; height:34px; flex:0 0 auto; border-radius:9px; display:grid; place-items:center; }
.act-title { font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.act-meta { font-size:11px; color:var(--color-neutral-500); }
.act-tags { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
.act-right { text-align:right; flex:0 0 auto; }
.act-sub { font-size:10px; color:var(--color-neutral-500); }
.act-chip { display:inline-flex; align-items:center; gap:3px; font-size:10px; padding:1px 7px; border-radius:999px; }
.act-chip-plan { color:var(--color-accent-200); background:color-mix(in srgb, var(--color-accent) 18%, transparent); }
.act-chip-result { color:#4fae72; background:color-mix(in srgb, #4fae72 18%, transparent); }
.act-log { display:flex; flex-direction:column; gap:18px; }
.act-group { display:flex; flex-direction:column; gap:8px; }
.act-group-head { display:flex; align-items:baseline; justify-content:space-between; gap:8px; padding:0 2px; }
.act-today { font-size:10px; color:var(--color-accent-200); padding:1px 7px; border-radius:999px;
  background:color-mix(in srgb, var(--color-accent) 20%, transparent); }
.act-commute { border-radius:8px; background:var(--color-surface); box-shadow:var(--shadow-sm); }
.act-commute-head { width:100%; display:flex; align-items:center; gap:12px; padding:10px 12px; background:transparent; border:0;
  color:inherit; font:inherit; text-align:left; cursor:pointer; opacity:.8; }
.act-caret { display:inline-grid; transition:transform .15s ease; }
.act-commute-kids { display:none; flex-direction:column; border-top:1px solid var(--color-neutral-800); }
.act-commute.open .act-commute-kids { display:flex; }
.act-commute.open .act-caret { transform:rotate(180deg); }
.act-commute-child { display:flex; align-items:center; gap:12px; padding:8px 12px 8px 58px; }
.act-body { display:flex; flex-direction:column; gap:16px; }
.act-skel { display:none; flex-direction:column; gap:8px; }
.tp-activity.is-loading .act-skel { display:flex; }
.tp-activity.is-loading .act-body { display:none; }
.act-cal[hidden] { display:none; }
.act-cal { position:fixed; inset:0; z-index:2147483646; display:flex; align-items:flex-start; justify-content:center;
  padding:48px 12px; overflow:auto; }
.act-cal-backdrop { position:fixed; inset:0; background:rgba(10,11,18,.72); }
.act-cal-box { position:relative; width:440px; max-width:100%; background:var(--color-surface); border-radius:12px;
  box-shadow:0 0 0 1px #3f424d, 0 24px 60px rgba(0,0,0,.5); padding:16px; }
.act-cal-body { display:flex; flex-direction:column; gap:14px; }
.act-cal-row { display:grid; grid-template-columns:repeat(7, minmax(0,1fr)) 52px; gap:4px; padding:3px; margin:0 -3px; border-radius:8px; }
.act-cal-row.click { cursor:pointer; }
.act-cal-row.sel { background:color-mix(in srgb, var(--color-accent) 16%, transparent); }
.act-cal-headrow { font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--color-neutral-600); text-align:center; }
.act-cell { position:relative; height:56px; border-radius:6px; background:#1b1d2a; overflow:hidden; display:flex;
  flex-direction:column; justify-content:space-between; padding:4px 5px 7px; }
.act-cell.out { opacity:.35; }
.act-cell.today { box-shadow:0 0 0 1px var(--color-accent); }
.act-cell-num { font-size:10px; color:var(--color-neutral-500); }
.act-cell.today .act-cell-num { color:var(--color-accent-200); }
.act-cell-load { font-size:10px; color:var(--color-neutral-300); }
.act-dots { display:flex; flex-wrap:wrap; gap:3px; }
.act-dot { width:6px; height:6px; border-radius:50%; display:inline-block; }
.act-cell-bar { position:absolute; left:0; right:0; bottom:0; height:3px; }
.act-cal-week { display:flex; flex-direction:column; justify-content:center; align-items:flex-end; padding-right:4px; }
"""

ACTIVITY_JS = """
(function () {
  var DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  function section(el) { return el.closest('.activity-filter-section'); }

  // ── tap a day on the load strip: show only that day (tap again to clear) ──
  function applyDay(sec, day) {
    var strip = sec.querySelector('.act-days');
    strip.classList.toggle('filtered', day !== null);
    sec.querySelectorAll('.act-day').forEach(function (b) {
      b.classList.toggle('sel', day !== null && b.getAttribute('data-act-day') === String(day));
    });
    sec.querySelectorAll('.act-group, .act-planned').forEach(function (g) {
      g.hidden = day !== null && g.getAttribute('data-day') !== String(day);
    });
    var hint = sec.querySelector('.act-strip-hint');
    if (hint) hint.textContent = day === null ? 'Tap a day to filter' : DOW[day] + ' only · tap again to clear';
    if (day === null) sec.removeAttribute('data-day'); else sec.setAttribute('data-day', day);
  }

  // ── calendar modal ──
  function cal() { return document.querySelector('.tp-activity .act-cal'); }
  function filterKey() {
    var r = document.querySelector('input[name=activity-filter]:checked');
    return r ? r.id.replace('activity-filter-', '') : 'all';
  }
  function loadMonth(month) {
    var box = cal();
    if (!box) return;
    var panel = box.closest('.tp-activity');
    var q = new URLSearchParams({ month: month, filter: filterKey(), week: panel.getAttribute('data-week') || '0' });
    var token = document.body.getAttribute('data-token');
    if (token) q.set('token', token);
    box.setAttribute('data-month', month);
    fetch('/dashboard/activity-calendar?' + q.toString(), { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (markup) { if (box.getAttribute('data-month') === month) box.querySelector('.act-cal-body').innerHTML = markup; })
      .catch(function () { box.querySelector('.act-cal-body').innerHTML = '<div class="err">Calendar unavailable.</div>'; });
  }
  function openCal() {
    var box = cal();
    if (!box) return;
    var start = box.closest('.tp-activity').getAttribute('data-week-start') || '';
    box.hidden = false;
    loadMonth(start.slice(0, 7) || new Date().toISOString().slice(0, 7));
  }
  function closeCal() { var box = cal(); if (box) box.hidden = true; }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t.closest || !t.closest('.tp-activity')) return;
    var day = t.closest('[data-act-day]');
    if (day) {
      var sec = section(day), d = parseInt(day.getAttribute('data-act-day'), 10);
      applyDay(sec, sec.getAttribute('data-day') === String(d) ? null : d);
      return;
    }
    var commute = t.closest('[data-act-commute]');
    if (commute) {
      var box = commute.closest('.act-commute');
      box.classList.toggle('open');
      commute.setAttribute('aria-expanded', box.classList.contains('open') ? 'true' : 'false');
      return;
    }
    if (t.closest('[data-cal-open]')) { openCal(); return; }
    if (t.closest('[data-cal-close]')) { closeCal(); return; }
    var month = t.closest('[data-cal-month]');
    if (month) { loadMonth(month.getAttribute('data-cal-month')); return; }
    var week = t.closest('[data-cal-week]');
    if (week) {
      closeCal();
      var offset = parseInt(week.getAttribute('data-cal-week'), 10) || 0;
      if (window.__goToWeek) window.__goToWeek(offset);
      return;
    }
  });
  document.addEventListener('keydown', function (e) {
    var box = cal();
    if (!box || box.hidden) return;
    if (e.key === 'Escape') closeCal();
    var week = e.target.closest && e.target.closest('[data-cal-week]');
    if (week && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); week.click(); }
  });
})();
"""
