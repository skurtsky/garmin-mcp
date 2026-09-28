# tools/dashboard_fitness.py
"""The Fitness tab's redesigned sections.

* Thresholds — Garmin's value and the plan's side by side (with units), a
  12-month sparkline (dots are tests) and the change underneath; a row opens
  its history: every test with its change and what it did to the zones.
* Race predictions — Garmin's run predictor next to the records, with how
  each moved over three months; or triathlon estimates from CSS (or the swim
  records), FTP and threshold pace with a leg-by-leg breakdown. The goal
  race's row is highlighted with its target.
* Records progress — new records from the last 30 days and close calls:
  recent efforts just short of a record.

Pure rendering over tools/dashboard_data.py's data; ``FITNESS_JS`` opens the
history dialogs and the full records list.
"""
import html
from datetime import date

from tools import plan_doc, race_predictor

GREEN, RED, AMBER = "#4fae72", "#cf8a80", "#d9a441"
_STATUS_COLORS = {"Tested": "#7fb87a", "Provisional": "#d9a441", "Unvalidated": "#cf5a4e"}
_SPORT = {"bike": ("#4fae72", "bicycle"), "swim": ("#4aa7d8", "swimming-pool"), "run": ("#d9a441", "sneaker-move"),
          "other": ("#9397ab", "trophy")}


def _e(v) -> str:
    return "&mdash;" if v is None else html.escape(str(v))


def _ph(name, size=16, color=None) -> str:
    from tools.dashboard import _ph as ph
    return ph(name, size, color)


def _fmt(key: str, v) -> str | None:
    if v is None:
        return None
    if key == "ftp":
        return f"{round(v)} W"
    if key in ("bikeLthr", "runLthr"):
        return f"{round(v)} bpm"
    return f"{plan_doc.fmt_mmss(v)}{'/km' if key == 'thresholdPace' else '/100m'}"


def _fmt_delta(key: str, d: float) -> str:
    arrow = "↑" if d > 0 else "↓"
    if key in ("thresholdPace", "css"):
        return f"{arrow} {round(abs(d))} s"
    return f"{arrow} {round(abs(d))}{' W' if key == 'ftp' else ''}"


def current_values(plan: dict | None, athlete: dict | None) -> dict[str, tuple]:
    """{key: (garmin value, plan value)} as numbers."""
    a = athlete or {}
    t = (plan or {}).get("thresholds_now") or {}
    pace = a.get("lactate_threshold_pace")
    return {
        "ftp": (a.get("ftp"), t.get("ftp")),
        "bikeLthr": (None, t.get("bikeLthr")),
        "runLthr": (a.get("lactate_threshold_hr"), t.get("runLthr")),
        "thresholdPace": (round(float(pace) * 60) if pace else None, plan_doc.single_pace_seconds(t.get("thresholdPace"))),
        "css": (None, plan_doc.single_pace_seconds(t.get("css"))),
    }


def _spark(vals: list, tests: list[int], lower_better: bool, w: int, h: int, big: bool = False) -> str:
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 2:
        return f'<svg width="{w}" height="{h}"></svg>' if not big else ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    pad = 6 if big else 2

    def x(i):
        return pad + i / (len(vals) - 1) * (w - 2 * pad)

    def y(v):
        k = (v - lo) / ((hi - lo) or 1)
        if lower_better:
            k = 1 - k
        return (h - 12 - k * (h - 24)) if big else (h - 3 - k * (h - 6))

    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in pts)
    dots = "".join(
        (f'<circle cx="{x(i):.1f}" cy="{y(vals[i]):.1f}" r="4" fill="#e7e5fe" stroke="#232532" stroke-width="2"/>' if big
         else f'<circle cx="{x(i):.1f}" cy="{y(vals[i]):.1f}" r="2.2" fill="#e7e5fe"/>')
        for i in tests if vals[i] is not None)
    if big:
        return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="width:100%;height:{h}px;display:block">'
                f'<line x1="0" x2="{w}" y1="{h - 4}" y2="{h - 4}" stroke="#3f424d" stroke-width="1" vector-effect="non-scaling-stroke"/>'
                f'<polyline points="{line}" fill="none" stroke="#9184d9" stroke-width="2" vector-effect="non-scaling-stroke" '
                f'stroke-linejoin="round"/>{dots}</svg>')
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="display:block">'
            f'<polyline points="{line}" fill="none" stroke="#9184d9" stroke-width="1.5" stroke-linejoin="round" '
            f'stroke-linecap="round"/>{dots}</svg>')


def _month_label(iso: str, with_year: bool) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%b %Y") if with_year else d.strftime("%b")


def _history_dialog(key: str, label: str, h: dict, garmin: str | None, plan: str | None) -> str:
    months = h["months"]
    ticks = [0, 3, 6, 9, len(months) - 1]
    tick_labels = "".join(f"<span>{_month_label(months[i], i in (0, len(months) - 1))}</span>"
                          for i in ticks if i < len(months))
    changes = h.get("changes") or []
    tests = []
    for c in reversed(changes):
        if c["delta"] is None:
            change, color = "first", "#9397ab"
        elif abs(c["delta"]) < 1e-6:
            change, color = "no change", "#9397ab"
        else:
            better = (c["delta"] < 0) == h["lower_better"]
            change, color = _fmt_delta(key, c["delta"]), (GREEN if better else RED)
        d = date.fromisoformat(c["date"])
        tests.append(f'<div class="thr-test"><span style="color:var(--color-neutral-400)">{d.strftime("%b")} {d.day}, {d.year}</span>'
                     f'<span>{_e(_fmt(key, c["value"]))}</span>'
                     f'<span style="text-align:right;font-size:11px;color:{color}">{change}</span></div>')
    delta = _fmt_delta(key, h["change"]) if h.get("change") else "—"
    delta_color = GREEN if h.get("improving") else (RED if h.get("change") else "var(--color-neutral-500)")
    note = h.get("zone_note") or {
        "ftp": "Bike power zones follow this value.", "bikeLthr": "Bike HR zones follow this value.",
        "runLthr": "Run HR zones follow this value.", "thresholdPace": "Run pace zones follow this value.",
        "css": "Swim pace zones follow CSS."}[key]
    source = {"plan": "Plan history", "garmin": "Garmin history"}.get(h.get("source"), "No history yet")
    return f"""
    <div class="thr-modal" data-thr="{key}" hidden role="dialog" aria-modal="true" aria-label="{_e(label)} history">
      <div class="thr-backdrop" data-thr-close></div>
      <div class="thr-box">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
          <div><div class="kicker">Threshold history</div><div style="font-size:17px;font-weight:500">{_e(label)}</div></div>
          <button type="button" class="act-circle" style="border:0;color:var(--color-neutral-500)" data-thr-close aria-label="Close">{_ph("x", 15)}</button>
        </div>
        <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px">
          <div><div class="kicker">Plan</div><div style="font-size:20px">{_e(plan)}</div></div>
          <div><div class="kicker">Garmin</div><div style="font-size:20px;color:var(--color-neutral-400)">{_e(garmin)}</div></div>
          <div><div class="kicker">12 months</div><div style="font-size:20px;color:{delta_color}">{delta}</div></div>
        </div>
        <div>{_spark(h["monthly"], h["test_months"], h["lower_better"], 388, 110, big=True)
              or '<div style="font-size:12px;color:var(--color-neutral-500)">No history yet.</div>'}
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--color-neutral-600);margin-top:4px">{tick_labels}</div></div>
        <div style="display:flex;flex-direction:column">
          <div class="kicker" style="padding-bottom:6px">Tests <span style="text-transform:none;letter-spacing:0">· {source}</span></div>
          {"".join(tests) or '<div style="font-size:12px;color:var(--color-neutral-500)">No changes recorded yet.</div>'}
        </div>
        <div class="thr-note">{_ph("arrows-clockwise", 14, "#b5abfc")}<span>{_e(note)}</span></div>
      </div>
    </div>"""


def thresholds(plan: dict | None, athlete: dict | None, history: dict | None) -> str:
    history = history or {}
    now = current_values(plan, athlete)
    plan_rows = {t["key"]: t for t in (plan or {}).get("thresholds") or [] if t.get("key")}
    status = {k: t.get("status") for k, t in plan_rows.items()}
    from tools.dashboard_data import THRESHOLDS
    rows, dialogs = [], []
    for i, (key, label, _, unit, lower_better) in enumerate(THRESHOLDS):
        garmin, planned = (_fmt(key, v) for v in now[key])
        if planned is None and (plan_rows.get(key) or {}).get("plan"):
            text = str(plan_rows[key]["plan"])
            planned = text if text.endswith(unit) else f"{text}{'' if unit.startswith('/') else ' '}{unit}"
        h = history.get(key) or {"monthly": [], "test_months": [], "change": None, "improving": None,
                                 "lower_better": lower_better, "months": [], "changes": []}
        if not garmin and not planned and not any(v is not None for v in h["monthly"]):
            continue
        spark = _spark(h["monthly"], h["test_months"], lower_better, 56, 20) if h["monthly"] else '<svg width="56" height="20"></svg>'
        delta = _fmt_delta(key, h["change"]) if h.get("change") else ""
        dcolor = GREEN if h.get("improving") else RED
        st = status.get(key)
        color = _STATUS_COLORS.get(st)
        tag = (f'<span class="f-tag" style="background:color-mix(in srgb, {color} 15%, transparent);color:{color}">{_e(st)}</span>'
               if color else "")
        rows.append(f"""
        <div class="thr-row" data-thr-open="{key}" role="button" tabindex="0">
          <div style="min-width:0;font-size:13px">{_e(label)}</div>
          <div style="display:flex;flex-direction:column;align-items:center;gap:1px">{spark}
            <span style="font-size:10px;color:{dcolor}">{delta}</span></div>
          <div style="font-size:13px;color:var(--color-neutral-500);text-align:right">{_e(garmin)}</div>
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
            <span style="font-size:14px;font-weight:500">{_e(planned)}</span>{tag}</div>
        </div>""")
        if h["months"]:
            dialogs.append(_history_dialog(key, label, h, garmin, planned))
    if not rows:
        return ""
    return f"""
      <div class="thr-grid thr-head">
        <div class="section-title" style="margin:0">Thresholds</div>
        <span class="thr-col" style="text-align:center">12 mo</span>
        <span class="thr-col" style="text-align:right">Garmin</span>
        <span class="thr-col" style="text-align:right">Plan</span>
      </div>
      <div class="card thr-card">{"".join(rows)}</div>
      {"".join(dialogs)}"""


# ── RACE PREDICTIONS ─────────────────────────────────────────────────────────

def _t(seconds) -> str | None:
    return race_predictor.fmt_time(seconds)


def _goal_line(goal: dict, predicted) -> str:
    if not goal.get("target_sec"):
        return ""
    line = f"Target {goal['target']}"
    if predicted:
        gap = predicted - goal["target_sec"]
        line += f" · {_t(abs(gap))} to find" if gap > 0 else (f" · {_t(-gap)} ahead" if gap < 0 else " · on target")
    return f'<div style="font-size:11px;color:{AMBER}">{_e(line)}</div>'


def _goal_head(goal: dict) -> str:
    d = date.fromisoformat(goal["date"])
    return (f'<div class="rp-goal-head">{_ph("flag-checkered", 12)}Goal race · {d.strftime("%b")} {d.day}</div>')


def _run_rows(run: list[dict], goal: dict | None, trend_days: int) -> str:
    rows = []
    for r in run:
        is_goal = bool(goal and goal.get("predictor_distance") == r["key"])
        sub = f"Record {_t(r['record_seconds'])}" if r["record_seconds"] else "No record yet"
        faster = r["faster_than_record_by"]
        faster_text = (_t(faster) if faster >= 60 else f"{round(faster)} s") if faster else ""
        chip = (f'<span class="f-tag" style="background:color-mix(in srgb, {GREEN} 15%, transparent);color:{GREEN}">'
                f'{faster_text} faster</span>' if faster else "")
        trend = ""
        if r["change_seconds"]:
            c = r["change_seconds"]
            trend = (f'<span style="font-size:10px;white-space:nowrap;color:{GREEN if c < 0 else RED}">'
                     f'{"↓" if c < 0 else "↑"} {_t(abs(c))} in {round(trend_days / 30)} mo</span>')
        rows.append(f"""
        <div class="rp-row{" rp-goal" if is_goal else ""}">
          {_goal_head(goal) if is_goal else ""}
          <div class="rp-grid">
            <div style="min-width:0"><div style="font-size:13px">{_e(r["name"])}</div>
              <div style="font-size:11px;color:var(--color-neutral-500)">{sub}</div>
              {_goal_line(goal, r["seconds"]) if is_goal else ""}</div>
            <div>{chip}</div>
            <div style="display:flex;flex-direction:column;align-items:flex-end;gap:1px">
              <span style="font-size:14px;font-weight:500">{_e(_t(r["seconds"]))}</span>{trend}</div>
          </div>
        </div>""")
    return "".join(rows)


def _tri_rows(tri: dict, goal: dict | None) -> str:
    legs_meta = {"swim": ("#4aa7d8", "swimming-pool"), "bike": ("#4fae72", "bicycle"), "run": ("#d9a441", "sneaker-move"),
                 "transitions": ("#9397ab", "arrows-left-right")}
    rows = []
    for r in tri["rows"]:
        is_goal = bool(goal and goal.get("distance") == r["key"])
        legs = "".join(
            f'<div class="rp-leg"><div style="display:flex;align-items:center;gap:4px;font-size:10px;color:{legs_meta[l["leg"]][0]}">'
            f'{_ph(legs_meta[l["leg"]][1], 12)}{_e(l["label"])}</div>'
            f'<div style="font-size:13px">{_e(_t(l["seconds"]))}</div>'
            f'<div style="font-size:10px;color:var(--color-neutral-500);white-space:nowrap">{_e(l["target"]) if l["target"] else "&nbsp;"}</div></div>'
            for l in r["legs"])
        rows.append(f"""
        <div class="rp-row{" rp-goal" if is_goal else ""}">
          {_goal_head(goal) if is_goal else ""}
          <div class="rp-grid">
            <div style="min-width:0"><div style="font-size:13px">{_e(r["name"])}</div>
              <div style="font-size:11px;color:var(--color-neutral-500)">{_e(r["distances"])}</div>
              {_goal_line(goal, r["seconds"]) if is_goal else ""}</div>
            <div></div>
            <div style="display:flex;flex-direction:column;align-items:flex-end"><span style="font-size:14px;font-weight:500">{_e(_t(r["seconds"]))}</span></div>
          </div>
          <div class="rp-legs">{legs}</div>
        </div>""")
    return "".join(rows)


def predictions(preds: dict | None, goal: dict | None) -> str:
    if not preds:
        return ""
    run_available = any(r["seconds"] for r in preds["run"])
    tri = preds["tri"]
    start_tri = bool(goal and goal.get("is_triathlon")) or not run_available
    swim = {"css": "CSS", "record": "swim records"}.get(tri["swim_source"], "CSS")
    tri_source = f"Estimated from {swim}, FTP, and threshold pace"
    if tri["missing"]:
        tri_source += f" · needs {', '.join(tri['missing'])}"
    return f"""
      <input class="hide" type="radio" name="rp" id="rp-run"{"" if start_tri else " checked"}>
      <input class="hide" type="radio" name="rp" id="rp-tri"{" checked" if start_tri else ""}>
      <div class="rp-head">
        <div style="min-width:0"><div class="section-title" style="margin:0">Race predictions</div>
          <div class="rp-src rp-src-run">Garmin race predictor</div>
          <div class="rp-src rp-src-tri">{_e(tri_source)}</div></div>
        <div class="f-seg rp-seg"><label for="rp-run">Run</label><label for="rp-tri">Triathlon</label></div>
      </div>
      <div class="card rp-card rp-run">{_run_rows(preds["run"], goal, preds.get("trend_days") or 90)
                                        if run_available else '<div class="rp-empty">No Garmin race predictions synced yet.</div>'}</div>
      <div class="card rp-card rp-tri">{_tri_rows(tri, goal)}</div>"""


# ── RECORDS PROGRESS ─────────────────────────────────────────────────────────

def _when(iso: str, today: date) -> str:
    d = date.fromisoformat(iso)
    return "Today" if d == today else f"{d.strftime('%b')} {d.day}"


def _rec_tile(sport: str) -> str:
    color, icon = _SPORT.get(sport, _SPORT["other"])
    return (f'<div class="act-tile" style="width:30px;height:30px;border-radius:8px;'
            f'background:color-mix(in srgb, {color} 18%, transparent);color:{color}">{_ph(icon, 16)}</div>')


def _open(aid) -> str:
    if not aid:
        return ""
    aid = int(aid)
    return (f' onclick="openActivityModal({aid})" role="button" tabindex="0"'
            f' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){{event.preventDefault();openActivityModal({aid})}}"')


def records(progress: dict | None, today: date) -> str:
    if not progress or not progress.get("count"):
        return ""
    new_rows = "".join(f"""
        <div class="rec-row{" actcard-click" if r.get("activity_id") else ""}"{_open(r.get("activity_id"))}>
          {_rec_tile(r["sport"])}
          <div style="flex:1;min-width:0"><div style="font-size:13px">{_e(r["name"])}</div>
            <div style="font-size:11px;color:var(--color-neutral-500)">{_when(r["date"], today)}{f" · was {_e(r['previous'])}" if r.get("previous") else ""}</div></div>
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px"><span style="font-size:14px;font-weight:500">{_e(r["value"])}</span>
            {f'<span class="f-tag" style="background:color-mix(in srgb, {GREEN} 15%, transparent);color:{GREEN}">{_e(r["improvement"])}</span>' if r.get("improvement") else ""}</div>
        </div>""" for r in progress["new"]) or '<div class="rec-empty">No new records in the last 30 days.</div>'
    close_rows = "".join(f"""
        <div class="rec-row actcard-click"{_open(r.get("activity_id"))}>
          {_rec_tile(r["sport"])}
          <div style="flex:1;min-width:0"><div style="font-size:13px">{_e(r["name"])}</div>
            <div style="font-size:11px;color:var(--color-neutral-500)">{_when(r["date"], today)} · record {_e(r["record"])}</div></div>
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px"><span style="font-size:14px;color:var(--color-neutral-400)">{_e(r["value"])}</span>
            <span class="f-tag" style="background:color-mix(in srgb, {AMBER} 15%, transparent);color:{AMBER}">{_e(r["off"])}</span></div>
        </div>""" for r in progress["close"]) or '<div class="rec-empty">No efforts within 2% of a record lately.</div>'
    return f"""
      <div style="display:flex;justify-content:space-between;align-items:baseline;padding:8px 2px 0">
        <div class="section-title" style="margin:0">Personal records</div>
        <button type="button" class="rec-all" data-prs-open>All {progress["count"]}{_ph("caret-right", 11)}</button>
      </div>
      <div class="card" style="padding:0;gap:0;box-shadow:var(--shadow-sm)">
        <div class="rec-kicker">New · last 30 days</div>{new_rows}
        <div class="rec-kicker" style="border-top:1px solid rgba(233,233,237,.07)">Close calls</div>{close_rows}
        <div style="height:6px"></div>
      </div>"""


FITNESS_CSS = """
.thr-grid { display:grid; grid-template-columns:minmax(0,1fr) 60px 64px 84px; gap:8px; }
.thr-head { align-items:baseline; padding:8px 14px 0 2px; }
.thr-col { font-size:10px; letter-spacing:.08em; text-transform:uppercase; color:var(--color-neutral-600); }
.thr-card { padding:0; gap:0; box-shadow:var(--shadow-sm); overflow:hidden; }
.thr-row { display:grid; grid-template-columns:minmax(0,1fr) 60px 64px 84px; gap:8px; align-items:center; padding:11px 14px;
  cursor:pointer; border-bottom:1px solid rgba(233,233,237,.07); }
.thr-row:last-child { border-bottom:0; }
.thr-row:hover { background:color-mix(in srgb, var(--color-text) 3%, transparent); }
.thr-modal { position:fixed; inset:0; z-index:2147483646; display:flex; align-items:flex-start; justify-content:center; padding:48px 12px; overflow:auto; }
.thr-modal[hidden] { display:none; }
.thr-backdrop { position:fixed; inset:0; background:rgba(10,11,18,.72); }
.thr-box { position:relative; width:420px; max-width:100%; background:var(--color-surface); border-radius:12px;
  box-shadow:0 0 0 1px #3f424d, 0 24px 60px rgba(0,0,0,.5); padding:16px; display:flex; flex-direction:column; gap:14px; }
.thr-test { display:grid; grid-template-columns:minmax(0,1fr) auto 70px; gap:8px; align-items:center; padding:8px 0;
  border-top:1px solid rgba(233,233,237,.07); font-size:13px; }
.thr-note { display:flex; gap:8px; align-items:flex-start; padding:10px 12px; border-radius:8px; background:#1b1d2a; font-size:12px;
  color:var(--color-neutral-400); }
.thr-note .phi { margin-top:2px; }
.rp-head { display:flex; justify-content:space-between; align-items:center; gap:8px; padding:8px 2px 0; }
.rp-src { font-size:11px; color:var(--color-neutral-600); display:none; }
#rp-run:checked ~ .rp-head .rp-src-run, #rp-tri:checked ~ .rp-head .rp-src-tri { display:block; }
.rp-seg { flex:0 0 auto; }
#rp-run:checked ~ .rp-head label[for=rp-run], #rp-tri:checked ~ .rp-head label[for=rp-tri] {
  color:var(--color-accent-200); background:color-mix(in srgb, var(--color-accent) 20%, transparent); }
.card.rp-card { display:none; padding:0; gap:0; box-shadow:var(--shadow-sm); }
#rp-run:checked ~ .rp-run, #rp-tri:checked ~ .rp-tri { display:flex; }
.rp-row { display:flex; flex-direction:column; gap:10px; padding:11px 14px; border-bottom:1px solid rgba(233,233,237,.07); }
.rp-row:last-child { border-bottom:0; }
.rp-row.rp-goal { background:color-mix(in srgb, var(--color-accent) 10%, transparent); box-shadow:inset 3px 0 0 var(--color-accent); }
.rp-goal-head { display:flex; align-items:center; gap:6px; font-size:10px; letter-spacing:.08em; text-transform:uppercase; color:#b5abfc; margin-bottom:-4px; }
.rp-grid { display:grid; grid-template-columns:minmax(0,1fr) auto 72px; gap:8px; align-items:center; }
.rp-legs { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)) 66px; gap:6px; }
.rp-leg { display:flex; flex-direction:column; gap:1px; padding:7px 8px; border-radius:6px; background:#1b1d2a; }
.rp-empty, .rec-empty { padding:12px 14px; font-size:12px; color:var(--color-neutral-500); }
.rec-kicker { padding:10px 14px 2px; font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--color-neutral-500); }
.rec-row { display:flex; align-items:center; gap:12px; padding:9px 14px; }
.rec-all { display:flex; align-items:center; gap:4px; border:0; background:transparent; padding:0; cursor:pointer;
  font:inherit; font-size:11px; color:#b5abfc; }
"""

FITNESS_JS = """
(function () {
  function close() { document.querySelectorAll('.thr-modal').forEach(function (m) { m.hidden = true; }); }
  function open(key) {
    close();
    var m = document.querySelector('.thr-modal[data-thr="' + key + '"]');
    if (m) m.hidden = false;
  }
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t.closest) return;
    var row = t.closest('[data-thr-open]');
    if (row) { open(row.getAttribute('data-thr-open')); return; }
    if (t.closest('[data-thr-close]')) { close(); return; }
    if (t.closest('[data-prs-open]')) {
      var prs = document.querySelector('.f-prs');
      if (prs) { prs.open = true; prs.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') close();
    var row = e.target.closest && e.target.closest('[data-thr-open]');
    if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); open(row.getAttribute('data-thr-open')); }
  });
})();
"""
