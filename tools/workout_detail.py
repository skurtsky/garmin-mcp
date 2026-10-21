# tools/workout_detail.py
"""The one workout detail view, shared by the plan viewer and the dashboard.

Both pages used to show a planned workout in their own dialog — the plan
viewer's (tags, completion, skip, edit menu) and Today's read-only copy in
the activity sheet. Now both open this: rendered here from the stored plan
(:func:`render`), served as HTML by ``GET /training-plan/api/workout``, and
shown by the shared overlay script (``WD_JS``/``WD_CSS``, injected into both
pages). From 900px it is a near-full-window panel — the structure graph and
step table beside coach notes and the sport's zones; below that, a near
full-height sheet with its actions pinned to the bottom.

The page decides nothing about the workout: completion, skip, Send to Garmin
and the edit menu all act through the plan's JSON API, so the view looks and
behaves the same wherever it was opened from.
"""
import html as _html
import re
import secrets
from datetime import date, datetime
from urllib.parse import urlencode

import db
from tools import plan_doc, workout_steps

_SPORT_COLORS = {"swim": "#5eb8c9", "bike": "#7fb87a", "run": "#d99a5e", "strength": "#9184d9",
                 "brick": "#c97fa0", "race": "#d9b35a", "rest": "#7c8194", "other": "#7c8194"}
_DONE = "#7fb87a"
_DIFFICULTY = {"easy": "#7fb87a", "moderate": "#d9b35a", "hard": "#d9705e", "very hard": "#c9506a"}
_ZONE_COLORS = {"1": "#6c93b0", "2": "#5eb8ad", "3": "#8fb85e", "4": "#d9b35a", "5": "#d9705e",
                "5a": "#d9705e", "5b": "#d9705e", "5c": "#c9506a"}
_THRESHOLD_NAME = {"bike": "FTP", "run": "Threshold", "swim": "CSS"}


def _e(value) -> str:
    return _html.escape("" if value is None else str(value))


def _dur(minutes) -> str:
    if not minutes:
        return ""
    h, m = divmod(round(minutes), 60)
    return f"{h}h {m}m" if h and m else (f"{h}h" if h else f"{m} min")


def _clock(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def _hm(seconds: float) -> str:
    """0:15, 1:15 — hours and minutes, for the graph's time axis."""
    m = int(round(seconds / 60))
    return f"{m // 60}:{m % 60:02d}"


def _distance(w: dict) -> str:
    if w.get("distanceKm") is not None:
        return f"{plan_doc._tidy(w['distanceKm'], 2)} km"
    if w.get("distanceMeters") is not None:
        return f"{w['distanceMeters']:,} m"
    return ""


def _day_label(day: str, start: str | None = None) -> str:
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return str(day)
    out = f"{d:%a} {d.day} {d:%b}"
    if start:
        try:
            t = datetime.strptime(start, "%H:%M")
            out += f" · {t.strftime('%I:%M %p').lstrip('0').lower()}"
        except ValueError:
            out += f" · {start}"
    return out


def _icon(path: str, size: int = 18, width: float = 2) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{path}</svg>')


_I_CLOSE = '<path d="M6 6l12 12M18 6L6 18"></path>'
_I_PREV = '<path d="M15 6l-6 6 6 6"></path>'
_I_NEXT = '<path d="M9 6l6 6-6 6"></path>'
_I_CHECK = '<path d="M5 12.5l4.5 4.5L19 7.5"></path>'
_I_CHEV = '<path d="M6 9l6 6 6-6"></path>'
_I_MORE = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8">'
           '</circle><circle cx="12" cy="12" r="1.8"></circle><circle cx="19" cy="12" r="1.8"></circle></svg>')
_I_SKIP = '<circle cx="12" cy="12" r="9"></circle><path d="M8 12h8"></path>'
# Send to Garmin: an arrow going into a watch; a tick on the watch once sent
# (the same icon as Today's hero, optically centred).
_WATCH = ('<g transform="translate(-1.6 0)"><rect x="10" y="6" width="12" height="12" rx="3"></rect>'
          '<path d="M12.5 6l.7-3.5h5.6l.7 3.5M12.5 18l.7 3.5h5.6l.7-3.5"></path>'
          '<path class="gs-arrow" d="M2.5 12h5.5M5.5 9.5 8 12l-2.5 2.5"></path>'
          '<path class="gs-tick" d="M13.2 12.2l2 2 3.6-3.8"></path></g>')


# Darker shades of the palette for light mode, so coloured text on its tint
# keeps 4.5:1 on white (the plan viewer's SPORT_COLORS_LIGHT, extended).
_LIGHT = {"#7fb87a": "#3f7a3a", "#5eb8c9": "#24707f", "#d99a5e": "#9a5a1f", "#9184d9": "#5d51b0",
          "#c97fa0": "#94416a", "#d9b35a": "#7d6210", "#7c8194": "#5d6275", "#6c93b0": "#355f7e",
          "#5eb8ad": "#24716a", "#8fb85e": "#4c6c24", "#d9705e": "#a5402e", "#c9506a": "#9c2f48",
          "#d98a5e": "#9f5126"}


def _ink(color: str) -> str:
    """A style fragment colouring text with ``color`` in dark mode and its
    darker shade in light mode (the .wd-ink rule)."""
    return f"--ink:{color};--ink-l:{_LIGHT.get(color, color)}"


def _tint(color: str, pct: int = 16) -> str:
    return f"color-mix(in srgb, {color} {pct}%, transparent)"


# ── zones ─────────────────────────────────────────────────────────────────────

def _zone_bands(plan: dict, sport: str) -> dict | None:
    """The sport's zones on one scale for the zone card: each band's position
    and width (% of the scale), its range text, and where the threshold sits.
    Power is linear in watts; pace is drawn by speed, slow on the left."""
    t = plan_doc.effective_thresholds(plan)
    rows = {r["zone"]: r for r in plan_doc.zone_rows(plan, sport)} if sport in plan_doc.ZONE_SPORTS else {}
    if sport == "bike" and t.get("ftp"):
        ftp = t["ftp"]
        top = ftp * 1.3
        bands = []
        for code, name, lo, hi in plan_doc.BIKE_POWER_PCT_TABLE:
            a, b = ftp * lo / 100, min(top, ftp * hi / 100)
            text = rows[code]["watts"] + " W" if rows.get(code, {}).get("watts") else ""
            bands.append({"code": code, "name": name, "left": a / top * 100, "width": max(.5, (b - a) / top * 100),
                          "text": "< " + text.split("–")[-1] if code == "1" and "–" in text else text})
        return {"bands": bands, "marker": ftp / top * 100, "mark": "FTP", "head": f"FTP {ftp} W"}
    if sport == "run":
        tp = plan_doc.single_pace_seconds(t.get("thresholdPace"))
        if tp is None:
            return None
        slow, fast = 1000 / (tp + 100), 1000 / (tp - 40)

        def pos(pace):
            return max(0.0, min(100.0, (1000 / pace - slow) / (fast - slow) * 100))
        bands = []
        for code, name, lo, hi in plan_doc.RUN_PACE_OFFSET_TABLE:
            a, b = pos(tp + hi), pos(tp + lo)
            hr = rows.get(code, {}).get("hr")
            if hr and hr.startswith("0–"):
                hr = "< " + hr[2:]
            bands.append({"code": code, "name": name, "left": a, "width": max(.5, b - a),
                          "text": f"{rows[code]['pace']} /km" + (f" · {hr} bpm" if hr else "")})
        lthr = t.get("runLthr")
        return {"bands": bands, "marker": pos(tp), "mark": "Threshold",
                "head": f"{plan_doc.fmt_mmss(tp)} /km" + (f" · LTHR {lthr}" if lthr else "")}
    if sport == "swim":
        css = plan_doc.single_pace_seconds(t.get("css"))
        if css is None:
            return None
        slow, fast = 100 / (css + 30), 100 / (css - 10)

        def pos(pace):
            return max(0.0, min(100.0, (100 / pace - slow) / (fast - slow) * 100))
        bands = []
        for code, name, lo, hi in plan_doc.SWIM_PACE_OFFSET_TABLE:
            a, b = pos(css + hi), pos(css + lo)
            bands.append({"code": code, "name": name, "left": a, "width": max(1.2, b - a),
                          "text": f"{rows[code]['pace']} /100m"})
        return {"bands": bands, "marker": pos(css), "mark": "CSS", "head": f"CSS {plan_doc.fmt_mmss(css)} /100m"}
    return None


def _zones_used(sport: str, steps: list | None, primary_zone) -> set[str]:
    """Zone codes this workout trains in: from its step targets, else from
    its primaryZone label ("Zone 2", "Zone 1-2")."""
    codes = [b for b in {"bike": [r[0] for r in plan_doc.BIKE_POWER_PCT_TABLE],
                         "run": [r[0] for r in plan_doc.RUN_PACE_OFFSET_TABLE],
                         "swim": [r[0] for r in plan_doc.SWIM_PACE_OFFSET_TABLE]}.get(sport, [])]
    used = set()

    def span(lo, hi):
        if lo in codes and hi in codes:
            used.update(codes[codes.index(lo):codes.index(hi) + 1])

    def targets():
        for s in steps or []:
            for x in (s.get("steps") if s.get("kind") == "repeat" else [s]):
                if x.get("target"):
                    yield x["target"]
    for target in targets():
        if "zone" in target:
            lo, hi = workout_steps.parse_zone(sport, "zone", target["zone"])
            if lo == "ss":
                used.update({"3", "4"})
            elif lo != "strides":
                span(lo, hi)
        elif "pctFtp" in target and sport == "bike":
            pair = target["pctFtp"] if isinstance(target["pctFtp"], list) else [target["pctFtp"]] * 2
            for code, _, lo, hi in plan_doc.BIKE_POWER_PCT_TABLE:
                if lo <= pair[1] and pair[0] <= hi:
                    used.add(code)
    if not used and primary_zone:
        found = re.findall(r"([1-5][abc]?)", str(primary_zone).lower())
        if found:
            span(found[0], found[-1]) if len(found) > 1 else used.add(found[0])
    return used


def _zones_card(plan: dict, sport: str, steps, primary_zone) -> str:
    z = _zone_bands(plan, sport)
    if not z:
        return ""
    used = _zones_used(sport, steps, primary_zone)
    segs, labels, rows = "", "", ""
    for b in z["bands"]:
        color = _ZONE_COLORS.get(b["code"], "#7c8194")
        on = b["code"] in used
        segs += (f'<span class="wd-zseg" style="left:{b["left"]:.1f}%;width:{b["width"]:.1f}%;background:{color};'
                 f'opacity:{1 if on else .28}"></span>')
        if on:
            labels += f'<span style="left:{b["left"] + b["width"] / 2:.1f}%">Z{_e(b["code"])}</span>'
        rows += (f'<div class="wd-zrow{" on" if on else ""}"><span class="wd-zk wd-ink" style="background:{_tint(color, 20)};'
                 f'{_ink(color)}">{_e(b["code"])}</span><span class="wd-zn">{_e(b["name"])}<small>{_e(b["text"])}</small></span>'
                 f'<span class="wd-zt"><span style="left:{b["left"]:.1f}%;width:{b["width"]:.1f}%;background:{color}"></span></span></div>')
    used_txt = ", ".join(f"Z{c}" for c in [b["code"] for b in z["bands"]] if c in used)
    label = f"{sport.capitalize()} zones" + (f"; this workout uses {used_txt}" if used_txt else "")
    return f"""
      <details class="wd-card wd-zones" data-wd-zones>
        <summary>
          <span class="wd-zhead"><span class="wd-h">Your {_e(sport)} zones</span><span class="wd-muted">{_e(z["head"])}</span>
            <span class="wd-chev">{_icon(_I_CHEV, 16)}</span></span>
          <span class="wd-zscale" role="img" aria-label="{_e(label)}">{segs}
            <span class="wd-zmark" style="left:{z["marker"]:.1f}%"></span></span>
          <span class="wd-zlabels" aria-hidden="true">{labels}<span class="wd-zmarkl" style="left:{z["marker"]:.1f}%">{_e(z["mark"])}</span></span>
        </summary>
        <div class="wd-zrows">{rows}
          {'<div class="wd-zkey"><span></span>Zones this workout uses</div>' if used else ''}</div>
      </details>"""


# ── structure ─────────────────────────────────────────────────────────────────

def _bar_text(b: dict, i: int, n: int) -> tuple[str, str]:
    kick = f"Step {i + 1} of {n} · {b['label']}" + (f" · Rep {b['rep']}" if b.get("rep") else "")
    value = " · ".join(p for p in (b["amount"], b.get("approx"), b["target"], b["extras"]) if p)
    return kick, value


_STRIP_HINT = "Select a step to see its details"


def _structure(resolved: dict, sport: str, plan: dict) -> str:
    bars = resolved["bars"]
    n = len(bars)
    html, gid = "", f"wdr{secrets.token_hex(3)}-"
    for i, b in enumerate(bars):
        kick, value = _bar_text(b, i, n)
        height = workout_steps.graph_height(b["intensity"])
        ramp = workout_steps.ramp_svg(b, f"{gid}{i}")
        html += (f'<button type="button" class="wd-bar{" ramp" if ramp else ""}" data-wg-i="{i}" '
                 f'aria-pressed="false" aria-label="{_e(kick)}: {_e(value)}" '
                 f'data-k="{_e(kick)}" data-v="{_e(value)}" data-n="{_e(b["note"])}" data-c="{b["fill"]}" '
                 f'style="flex-grow:{max(1, round(b["seconds"]))};height:{height}%'
                 f'{"" if ramp else ";background:" + b["color"]}">{ramp}</button>')
    total = resolved["total_s"]
    step = 300 if total <= 1800 else 900 if total <= 7200 else 1800 if total <= 14400 else 3600
    # h:mm ticks; the last one is the total, so drop a tick crowding it.
    ticks = "".join(f'<span style="left:{t / total * 100:.2f}%">{_hm(t)}</span>'
                    for t in range(0, int(total), step) if total - t > step * .45)
    approx = "~" if resolved.get("estimated") else ""
    t = plan_doc.effective_thresholds(plan)
    line = {"bike": f"FTP {t['ftp']} W" if t.get("ftp") else "FTP",
            "run": f"Threshold {t['thresholdPace']}" if t.get("thresholdPace") else "Threshold",
            "swim": f"CSS {t['css']}" if t.get("css") else "CSS"}.get(sport, "Threshold")
    legend = "".join(f'<span><i style="background:{c}"></i>{z}</span>' for _, c, z in workout_steps._BANDS)
    return f"""
        <section class="wd-card wd-struct" aria-label="Structure">
          <div class="wd-cardhead"><h3 class="wd-h">Structure</h3>
            <span class="wd-muted">{approx}{_dur(total / 60)} · {n} steps</span>
            <span class="wd-legend" aria-hidden="true">{legend}</span></div>
          <div class="wd-strip" aria-live="polite" data-hint="{_e(_STRIP_HINT)}">
            <span class="wd-strip-c" style="background:transparent"></span>
            <div class="wd-strip-t"><div class="wd-kick wd-k">{_e(_STRIP_HINT)}</div><div class="wd-v"></div></div>
            <div class="wd-n" hidden></div>
          </div>
          <div class="wd-plot">
            <div class="wd-line" style="bottom:{workout_steps.THRESHOLD_AT}%"><span>{_e(line)}</span></div>
            <div class="wd-bars" role="group" aria-label="Workout steps — select one to see it above">{html}</div>
          </div>
          <div class="wd-ticks">{ticks}<span style="left:100%">{approx}{_hm(total)}</span></div>
        </section>"""


def _step_table(resolved: dict) -> str:
    rows, n = "", 0

    def row(item, indent=False):
        nonlocal n
        n += 1
        amount = item["amount"] + (f' <span class="wd-muted">{_e(item["approx"])}</span>' if item.get("approx") else "")
        target = " · ".join(p for p in (item["target"], item["extras"]) if p) or '<span class="wd-muted">—</span>'
        return (f'<button type="button" class="wd-row{" in" if indent else ""}" data-wg-i="{item["index"]}">'
                f'<span class="wd-rn">{n}</span>'
                f'<span class="wd-rs"><i style="background:{item["fill"]}"></i>{_e(item["label"])}</span>'
                f'<span class="wd-rt">{amount}</span><span class="wd-rg">{target if "<span" in target else _e(target)}</span>'
                f'<span class="wd-rc">{_e(item["note"])}</span></button>')
    for g in resolved["groups"]:
        if "repeat" in g:
            note = f" · {_e(g['note'])}" if g["note"] else ""
            rows += f'<div class="wd-rep">{g["repeat"]} &times; repeat{note}</div>'
            rows += "".join(row(c, indent=True) for c in g["children"])
        else:
            rows += row(g)
    return f"""
        <section class="wd-card wd-steps" aria-label="Steps">
          <div class="wd-thead" aria-hidden="true"><span>#</span><span>Step</span><span>Time</span><span>Target</span><span>Cues</span></div>
          {rows}
        </section>"""


def _strength(steps: list[dict]) -> str:
    def card(s):
        name = str(s.get("exercise") or "").replace("_", " ").title()
        sets = s.get("sets", 1)
        wave_kg, wave_lb = s.get("setWeightsKg"), s.get("setWeightsLb")
        unit = "kg" if wave_kg or s.get("weightKg") is not None else "lb"
        wave = wave_kg or wave_lb
        if wave:
            load = " – ".join(str(plan_doc._tidy(w, 1)) for w in wave) + f" {unit}"
        elif s.get("weightKg") is not None or s.get("weightLb") is not None:
            load = f"{plan_doc._tidy(s.get('weightKg', s.get('weightLb')), 1)} {unit}"
        else:
            load = "Bodyweight"
        each = (str(s["reps"]) if s.get("reps") else _clock(s["durationS"]) if s.get("durationS") else "—")
        chips = "".join(
            f'<span class="wd-set">{_e(each)}<small>{_e(f"{plan_doc._tidy(wave[i], 1)} {unit}" if wave else ("hold" if s.get("durationS") else "reps"))}</small></span>'
            for i in range(sets))
        meta = " · ".join(p for p in (
            f"{sets} × {s['reps']} reps" if s.get("reps") else f"{sets} × {_clock(s['durationS'])}" if s.get("durationS") else f"{sets} sets",
            f"rest {_clock(s['restS'])}" if s.get("restS") else "", s.get("note") or "") if p)
        return (f'<div class="wd-ex"><div class="wd-ex-top"><span class="wd-ex-n">{_e(name)}</span>'
                f'<span class="wd-ex-l">{_e(load)}</span></div><div class="wd-sets">{chips}</div>'
                f'<div class="wd-muted wd-small">{_e(meta)}</div></div>')
    warm = [s for s in steps if s.get("kind") == "exercise" and s.get("warmup")]
    main = [s for s in steps if s.get("kind") == "exercise" and not s.get("warmup")]
    rests = [s for s in steps if s.get("kind") == "rest"]
    out = ""
    if warm:
        out += '<div class="wd-sec">Warm-up</div>' + "".join(card(s) for s in warm)
    out += ('<div class="wd-sec">Main</div>' if warm else "") + "".join(card(s) for s in main)
    if rests:
        total = sum(s.get("durationS") or 0 for s in rests)
        out += f'<div class="wd-muted wd-small">Plus {len(rests)} rest break{"s" if len(rests) > 1 else ""}' + \
               (f" ({_clock(total)})" if total else "") + "</div>"
    n_sets = sum(s.get("sets", 1) for s in warm + main)
    return (f'<section class="wd-card wd-strength" aria-label="Exercises"><div class="wd-cardhead"><h3 class="wd-h">Exercises</h3>'
            f'<span class="wd-muted">{len(warm) + len(main)} exercises · {n_sets} sets</span></div>{out}</section>')


# ── the view ──────────────────────────────────────────────────────────────────

def _activity_link(activity_id, token) -> str:
    q = {"tab": "activity", "activity": str(activity_id), "from": "plan", **({"token": token} if token else {})}
    return "/dashboard?" + urlencode(q)


def _done_card(w: dict, act: dict, token) -> str:
    summary = act.get("summary") or {}
    sport = w.get("sport")
    when = ""
    try:
        when = datetime.fromisoformat(str(summary.get("date") or act.get("activity_date"))[:19])
        when = f"{when:%a} {when.day} {when:%b}, {when.strftime('%I:%M %p').lstrip('0').lower()}"
    except (TypeError, ValueError):
        when = ""
    stats = []
    dur, km = act.get("duration_min"), act.get("distance_km")
    if dur:
        stats.append(("Time", _clock(dur * 60), f"planned {_dur(w.get('durationMinutes'))}" if w.get("durationMinutes") else ""))
    if km:
        planned = _distance(w)
        stats.append(("Distance", f"{plan_doc._tidy(km, 2)} km" if sport != "swim" else f"{round(km * 1000):,} m",
                      f"planned {planned}" if planned else ""))
    if dur and km:
        if sport == "run":
            stats.append(("Avg pace", f"{plan_doc.fmt_mmss(dur * 60 / km)} /km", ""))
        elif sport == "swim":
            stats.append(("Avg pace", f"{plan_doc.fmt_mmss(dur * 60 / (km * 10))} /100m", ""))
    if sport == "bike" and summary.get("normalized_power"):
        stats.append(("Norm. power", f"{round(summary['normalized_power'])} W", f"avg {round(summary['avg_power'])} W" if summary.get("avg_power") else ""))
    elif sport == "bike" and summary.get("avg_speed_kph"):
        stats.append(("Avg speed", f"{summary['avg_speed_kph']} km/h", ""))
    if summary.get("avg_hr"):
        stats.append(("Avg HR", f"{round(summary['avg_hr'])} bpm", ""))
    cells = "".join(f'<div class="wd-stat"><span class="wd-kick">{k}</span><b>{_e(v)}</b>'
                    f'{f"<small>{_e(sub)}</small>" if sub else ""}</div>' for k, v, sub in stats[:4])
    color = _SPORT_COLORS.get(sport, "#7c8194")
    return f"""
        <a class="wd-card wd-done" href="{_e(_activity_link(act['garmin_id'], token))}" data-wd-activity="{int(act['garmin_id'])}">
          <div class="wd-done-top"><span class="wd-bar-c" style="background:{color}"></span>
            <div style="flex:1;min-width:0"><div class="wd-kick wd-ink" style="{_ink(_DONE)}">Done with</div>
              <div class="wd-done-n">{_e(act.get('name') or 'Garmin activity')}{f' · {_e(when)}' if when else ''}</div></div>
            <span class="wd-link">Open activity {_icon(_I_NEXT, 14)}</span></div>
          {f'<div class="wd-stats">{cells}</div>' if cells else ''}
        </a>"""


def _neighbours(plan: dict, workout_id: str) -> tuple[str | None, str | None]:
    order = sorted(((str(d.get("date"))[:10], w.get("startTime") or "99:99", i, w.get("id"))
                    for i, (_, d, w) in enumerate(plan_doc.iter_workouts(plan))
                    if w.get("sport") != "rest" and w.get("id")))
    ids = [o[3] for o in order]
    if workout_id not in ids:
        return None, None
    i = ids.index(workout_id)
    return (ids[i - 1] if i else None), (ids[i + 1] if i + 1 < len(ids) else None)


def _send_button(w: dict, state: dict, steps, bar: bool) -> str:
    if state.get("completed") or w.get("sport") == "rest":
        return ""
    cls = "wd-btn wd-ib wd-gsend" + (" wd-big" if bar else "")
    if state.get("garmin_workout_id"):
        label = "On Garmin — scheduled on its plan day. Press to take it off your watch"
        return (f'<button type="button" class="{cls} sent" data-wd-unsend aria-label="{label}" title="{label}">'
                f'{_icon(_WATCH, 22, 1.7)}</button>')
    blocker = workout_steps.garmin_blocker(w.get("sport"), steps)
    if blocker:
        return (f'<button type="button" class="{cls}" disabled aria-label="Send to Garmin — {_e(blocker)}" '
                f'title="{_e(blocker)}">{_icon(_WATCH, 22, 1.7)}</button>')
    label = f"Send to Garmin — create and schedule it for {_day_label(w['_date'])}"
    return (f'<button type="button" class="{cls}" data-wd-send aria-label="{_e(label)}" title="{_e(label)}">'
            f'{_icon(_WATCH, 22, 1.7)}</button>')


def render(row: dict, workout_id: str, token: str | None = None) -> dict:
    """{"html", "id", "prev", "next"} for one workout of a stored plan row.
    LookupError if the plan has no such workout."""
    plan = row["plan"]
    hit = plan_doc.find_workout(plan, workout_id)
    if hit is None:
        raise LookupError(f"No workout with id {workout_id!r} in this plan.")
    week, day, w = hit
    w = {**w, "_date": str(day.get("date"))[:10]}
    read_only = row.get("status") == "archived"
    state = (db.get_workout_states(row["id"]) or {}).get(workout_id) or {}
    done = bool(state.get("completed"))
    skipped = bool(state.get("skipped")) and not done
    act = db.get_activities_by_ids([state["activity_id"]]).get(state["activity_id"]) if done and state.get("activity_id") else None
    sport = w.get("sport") or "other"
    color = _SPORT_COLORS.get(sport, "#7c8194")
    try:
        steps = workout_steps.clean_steps(sport, w.get("steps")) if w.get("steps") else None
    except plan_doc.PlanError:
        steps = None
    resolved = workout_steps.resolve(plan, sport, steps) if steps else None
    prev_id, next_id = _neighbours(plan, workout_id)

    # Header
    from tools.plan_today import _phase_for_week
    phase = _phase_for_week(plan, week)
    kick = " · ".join(p for p in (_day_label(w["_date"], w.get("startTime")),
                                  f"Week {week.get('weekNumber')}" if week.get("weekNumber") else "", phase or "") if p)
    chips = [f'<span class="wd-chip wd-ink" style="background:{_tint(color)};{_ink(color)};box-shadow:none">{_e(sport.capitalize())}</span>']
    chips += [f'<span class="wd-chip">{_e(x)}</span>' for x in (
        (w.get("type") or "").capitalize(), _dur(w.get("durationMinutes")), _distance(w), w.get("terrain")) if x]
    if w.get("primaryZone"):
        zc = _ZONE_COLORS.get((re.findall(r"[1-5][abc]?", str(w["primaryZone"]).lower()) or ["?"])[0], "#7c8194")
        chips.append(f'<span class="wd-chip wd-ink" style="background:{_tint(zc)};{_ink(zc)};box-shadow:none">{_e(w["primaryZone"])}</span>')
    if w.get("difficulty"):
        dc = _DIFFICULTY.get(str(w["difficulty"]).lower(), "#7c8194")
        chips.append(f'<span class="wd-chip wd-ink" style="background:{_tint(dc)};{_ink(dc)};box-shadow:none">{_e(str(w["difficulty"]).capitalize())}</span>')
    if w.get("trainingEffect"):
        chips.append(f'<span class="wd-chip">{_e(w["trainingEffect"])}</span>')
    badge = (f'<span class="wd-badge wd-ink" style="background:{_tint(_DONE)};{_ink(_DONE)}">{_icon(_I_CHECK, 14, 2.4)}Done</span>' if done
             else '<span class="wd-badge wd-badge-skip">Skipped</span>' if skipped else "")

    # Actions — the same set in the header (desktop) and the pinned bar (phone).
    can_edit = not read_only
    skip_btn = ("" if not can_edit or done or sport == "rest" else
                f'<button type="button" class="wd-btn" data-wd-skip="{"off" if skipped else "on"}">{"Unskip" if skipped else "Skip"}</button>')
    done_btn = ("" if not can_edit else
                f'<button type="button" class="wd-btn{" wd-primary" if not done else ""}" data-wd-complete="{"off" if done else "on"}">'
                f'{"" if done else _icon(_I_CHECK, 16, 2.2)}{"Mark incomplete" if done else "Mark complete"}</button>')
    plan_q = {"workout": workout_id, **({"plan": row["id"]} if row.get("status") != "active" else {}),
              **({"token": token} if token else {})}
    menu_items = (f'<a role="menuitem" class="wd-open-plan" href="{_e("/training-plan?" + urlencode(plan_q))}">Open in plan</a>'
                  + ("" if not can_edit else
                     '<button type="button" role="menuitem" data-wd-act="edit">Edit</button>'
                     '<button type="button" role="menuitem" data-wd-act="duplicate">Duplicate</button>'
                     '<button type="button" role="menuitem" class="danger" data-wd-act="delete">Delete</button>'))

    def more(cls):
        return (f'<div class="wd-more"><button type="button" class="wd-btn wd-ib wd-plain{cls}" data-wd-menu aria-haspopup="menu" '
                f'aria-expanded="false" aria-label="More: open in plan, edit, duplicate, delete">{_I_MORE}</button>'
                f'<div class="wd-menu" role="menu" hidden>{menu_items}</div></div>')

    head_actions = (f'<button type="button" class="wd-btn wd-ib wd-plain" data-wd-go="{_e(prev_id or "")}"{"" if prev_id else " disabled"} '
                    f'aria-label="Previous workout" title="Previous workout">{_icon(_I_PREV)}</button>'
                    f'<button type="button" class="wd-btn wd-ib wd-plain" data-wd-go="{_e(next_id or "")}"{"" if next_id else " disabled"} '
                    f'aria-label="Next workout" title="Next workout">{_icon(_I_NEXT)}</button><span class="wd-sep"></span>'
                    f'{_send_button(w, state, steps, False)}{skip_btn}{done_btn}{more("")}')

    # Skip: the note when skipped, the optional reason form when skipping.
    reason = state.get("skip_reason") or ""
    skip_note = ""
    if skipped:
        how = "Missed — its day went by without it being done." if state.get("skipped_auto") else "Skipped."
        skip_note = (f'<div class="wd-skipnote">{_icon(_I_SKIP, 18)}<div><div>{how}</div>'
                     f'{f"<div class=wd-reason>“{_e(reason)}”</div>" if reason else ""}'
                     + (f'<button type="button" class="wd-linkbtn" data-wd-reason>{"Edit reason" if reason else "Add a reason"}</button>'
                        if can_edit else "") + '</div></div>')
    skip_form = ("" if not can_edit or done or sport == "rest" else f"""
        <form class="wd-card wd-skipform" data-wd-skipform hidden>
          <label for="wd-reason-{_e(workout_id)}">{"Why was it skipped?" if skipped else "Why are you skipping it?"} <span class="wd-muted">Optional</span></label>
          <input id="wd-reason-{_e(workout_id)}" type="text" maxlength="200" autocomplete="off" value="{_e(reason)}"
            placeholder="e.g. sick, travelling, legs too sore">
          <div class="wd-formbtns"><button type="button" class="wd-btn" data-wd-skipcancel>Cancel</button>
            <button type="submit" class="wd-btn wd-primary">{"Save reason" if skipped else "Skip workout"}</button></div>
        </form>""")

    # Main column
    main = (_done_card(w, act, token) if act else "") + skip_note + skip_form
    if resolved and resolved.get("graph"):
        main += _structure(resolved, sport, plan) + _step_table(resolved)
    elif steps and sport == "strength":
        main += _strength(steps)
    text = plan_doc.resolve_tokens(plan, w.get("humanReadable")) if w.get("humanReadable") else ""
    if text and not steps:
        main += (f'<section class="wd-card"><div class="wd-cardhead"><h3 class="wd-h">Workout</h3></div>'
                 f'<div class="wd-text">{_e(text)}</div></section>')
    if not main.strip():
        main = '<section class="wd-card"><p class="wd-muted" style="margin:0">No details for this workout.</p></section>'

    # Side column
    targets = [t.strip() for t in re.split(r"[;\n]", w.get("keyTargets") or "") if t.strip()]
    notes = ""
    if w.get("description") or targets:
        notes = ('<section class="wd-card wd-notes"><h3 class="wd-h">Coach notes</h3>'
                 + (f'<p>{_e(w["description"])}</p>' if w.get("description") else "")
                 + (f'<div class="wd-kick">Key targets</div><ul>{"".join(f"<li>{_e(t)}</li>" for t in targets)}</ul>' if targets else "")
                 + "</section>")
    side = notes + _zones_card(plan, sport, steps, w.get("primaryZone"))
    if text and steps:
        side += (f'<details class="wd-card wd-orig"><summary class="wd-h">Coach&rsquo;s original text</summary>'
                 f'<div class="wd-text">{_e(text)}</div></details>')
    if state.get("garmin_workout_id") and done:
        side += (f'<div class="wd-card wd-watch">{_icon(_WATCH, 20, 1.7)}'
                 f'Was on your watch for {_e(_day_label(w["_date"]))}</div>')

    bar = ("" if read_only and not prev_id and not next_id else
           f'<div class="wd-actbar">{more(" wd-big")}{_send_button(w, state, steps, True)}{skip_btn.replace("wd-btn", "wd-btn wd-big")}'
           f'{done_btn.replace("wd-btn", "wd-btn wd-big wd-grow")}</div>')
    html = f"""
<div class="wd{' done' if done else ''}" data-wd data-id="{_e(workout_id)}" data-date="{_e(w['_date'])}">
  <div class="wd-grab" aria-hidden="true"><span></span></div>
  <header class="wd-head">
    <span class="wd-bar-c" style="background:{color}"></span>
    <div class="wd-titles">
      <div class="wd-kick">{_e(kick)}</div>
      <div class="wd-titlerow"><h2 class="wd-title" id="wd-title">{_e(w.get("name") or "Workout")}</h2>{badge}</div>
      <div class="wd-chips">{"".join(chips)}</div>
    </div>
    <div class="wd-headacts">{head_actions}</div>
    <button type="button" class="wd-btn wd-ib wd-plain wd-close" data-wd-close aria-label="Close">{_icon(_I_CLOSE)}</button>
  </header>
  <div class="wd-body">
    <div class="wd-main">{main}</div>
    <aside class="wd-side">{side}</aside>
  </div>
  <div class="wd-msg" role="status"></div>
  {bar}
</div>"""
    return {"html": html, "id": workout_id, "prev": prev_id, "next": next_id, "readOnly": read_only,
            "title": w.get("name") or "Workout"}


def preview(plan: dict, sport: str | None, steps) -> dict:
    """The plan editor's live preview of steps being typed: the graph (or the
    exercise cards) exactly as the workout view will draw them, with their
    total time. PlanError, naming the step, if they don't validate."""
    cleaned = workout_steps.clean_steps(sport, steps)
    if not cleaned:
        return {"html": "", "totalMinutes": None, "count": 0}
    if sport == "strength":
        return {"html": _strength(cleaned), "totalMinutes": None, "count": len(cleaned)}
    resolved = workout_steps.resolve(plan, sport, cleaned)
    return {"html": _structure(resolved, sport, plan), "totalMinutes": round(resolved["total_s"] / 60),
            "estimated": bool(resolved.get("estimated")), "count": len(resolved["bars"])}


# ── the shared overlay (both pages) ───────────────────────────────────────────
# Uses the pages' own colour tokens (both define the same names, dark and
# light); --color-inset and --color-divider-soft exist only on the dashboard,
# so they fall back to a mix here.
WD_CSS = r"""
.wd-overlay { position:fixed; inset:0; z-index:2147483647; font-family:var(--font-body, 'Inter', -apple-system, 'Segoe UI', sans-serif);
  --wd-panel:color-mix(in srgb, var(--color-surface) 72%, var(--color-bg));
  --wd-inset:var(--color-inset, color-mix(in srgb, var(--color-bg) 55%, var(--color-surface)));
  --wd-line:var(--color-divider-soft, color-mix(in srgb, var(--color-text) 8%, transparent));
  color:var(--color-text); font-size:15px; line-height:1.5; }
.wd-overlay[hidden] { display:none; }
.wd-ink { color:var(--ink); }
html[data-theme=light] .wd-ink { color:var(--ink-l); }
html[data-theme=light] .wd-gsend.sent, html[data-theme=light] .wd-watch svg { color:#3f7a3a; }
html.wd-lock, html.wd-lock body { overflow:hidden; }
.wd-scrim { position:absolute; inset:0; background:var(--color-backdrop, rgba(10,11,16,.7)); }
.wd-panel { position:absolute; display:flex; flex-direction:column; background:var(--wd-panel); overflow:hidden;
  box-shadow:0 0 0 1px var(--color-neutral-800), 0 24px 60px rgba(0,0,0,.45); }
.wd-loading { margin:auto; padding:40px; color:var(--color-neutral-500); }
.wd-err { margin:auto; padding:32px; display:flex; flex-direction:column; gap:12px; align-items:center; text-align:center; }
.wd { display:flex; flex-direction:column; min-height:0; height:100%; }
.wd-kick { font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--color-neutral-500); }
.wd-muted { color:var(--color-neutral-500); }
.wd-small { font-size:12px; }
.wd-h { margin:0; font-size:15px; font-weight:500; }
.wd-card { background:var(--color-surface); border-radius:12px; box-shadow:0 0 0 1px var(--color-neutral-800); padding:16px 18px; }
.wd-cardhead { display:flex; align-items:baseline; gap:12px; margin-bottom:12px; }
.wd-cardhead .wd-h { font-size:16px; }
/* header */
.wd-head { display:flex; align-items:flex-start; gap:18px; background:var(--color-surface); border-bottom:1px solid var(--wd-line); }
.wd-bar-c { width:4px; align-self:stretch; border-radius:2px; flex:0 0 auto; }
.wd-titles { flex:1; min-width:0; display:flex; flex-direction:column; gap:6px; }
.wd-titlerow { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.wd-title { margin:0; font-size:28px; font-weight:500; letter-spacing:-.02em; line-height:1.15; }
.wd-badge { display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:500; padding:4px 10px; border-radius:999px; }
.wd-badge-skip { background:color-mix(in srgb, var(--color-skip, #d9a441) 16%, transparent); color:var(--color-skip, #d9a441); }
.wd-chips { display:flex; flex-wrap:wrap; gap:6px; }
.wd-chip { font-size:12px; padding:3px 10px; border-radius:6px; box-shadow:inset 0 0 0 1px var(--color-neutral-700); color:var(--color-neutral-300); white-space:nowrap; }
.wd-headacts { display:flex; align-items:center; gap:8px; }
.wd-sep { width:1px; height:24px; background:var(--color-neutral-800); margin:0 4px; }
/* buttons */
.wd-btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; height:38px; padding:0 16px; border:0; border-radius:8px;
  font:inherit; font-size:13px; font-weight:500; cursor:pointer; background:transparent; color:var(--color-accent-400);
  box-shadow:inset 0 0 0 1px var(--color-accent-700); text-decoration:none; white-space:nowrap; }
.wd-btn:hover:not([disabled]) { background:color-mix(in srgb, var(--color-accent) 12%, transparent); }
.wd-btn:focus-visible { outline:2px solid var(--color-accent); outline-offset:2px; }
.wd-primary { background:var(--color-accent); color:var(--color-bg); box-shadow:none; }
.wd-primary:hover:not([disabled]) { background:var(--color-accent-400); }
.wd-ib { width:38px; padding:0; }
.wd-plain { box-shadow:none; color:var(--color-neutral-400); }
.wd-plain:hover:not([disabled]) { background:color-mix(in srgb, var(--color-text) 7%, transparent); color:var(--color-text); }
.wd-btn[disabled] { cursor:not-allowed; }
.wd-plain[disabled] { opacity:.35; }
.wd-btn.busy { opacity:.6; pointer-events:none; }
.wd-gsend .gs-tick { display:none; }
.wd-gsend.sent .gs-tick { display:inline; }
.wd-gsend.sent .gs-arrow { display:none; }
.wd-gsend.sending .gs-arrow { animation:wd-push .9s ease-in-out infinite; }
@keyframes wd-push { 0%, 100% { transform:translateX(-2px); opacity:.5; } 50% { transform:translateX(1px); opacity:1; } }
.wd-gsend[disabled]:not(.sent):not(.sending) { opacity:.4; color:var(--color-neutral-400); box-shadow:inset 0 0 0 1px var(--color-neutral-700); }
.wd-gsend.sent { color:#7fb87a; box-shadow:inset 0 0 0 1px color-mix(in srgb, #7fb87a 45%, transparent); }
.wd-linkbtn { border:0; padding:0; background:none; font:inherit; font-size:13px; color:var(--color-accent-400); cursor:pointer; }
/* menu */
.wd-more { position:relative; }
.wd-menu { position:absolute; right:0; top:calc(100% + 6px); z-index:2; min-width:180px; display:flex; flex-direction:column; padding:6px;
  border-radius:10px; background:var(--color-surface); box-shadow:0 0 0 1px var(--color-neutral-700), 0 10px 30px rgba(0,0,0,.4); }
.wd-menu[hidden] { display:none; }
.wd-menu > * { display:block; padding:9px 12px; border:0; border-radius:6px; background:none; color:var(--color-text); font:inherit; font-size:14px;
  text-align:left; text-decoration:none; cursor:pointer; }
.wd-menu > *:hover { background:color-mix(in srgb, var(--color-text) 7%, transparent); }
.wd-menu .danger { color:var(--color-danger, #e0736f); }
.wd-actbar .wd-menu { top:auto; bottom:calc(100% + 6px); left:0; right:auto; }
/* body */
.wd-body { display:grid; gap:20px; align-content:start; }
.wd-main, .wd-side { display:flex; flex-direction:column; gap:16px; min-width:0; }
.wd-text { font-size:13px; line-height:1.7; white-space:pre-wrap; color:var(--color-text); }
.wd-msg { font-size:13px; color:var(--color-danger, #e0736f); }
.wd-msg:empty { display:none; }
.wd-msg.ok { color:var(--color-neutral-300); }
/* done with */
.wd-done { display:flex; flex-direction:column; gap:12px; color:inherit; text-decoration:none; box-shadow:0 0 0 1px color-mix(in srgb, #7fb87a 40%, transparent); }
.wd-done:hover { box-shadow:0 0 0 1px #7fb87a; }
.wd-done-top { display:flex; align-items:center; gap:12px; }
.wd-done-n { font-size:15px; font-weight:500; }
.wd-link { display:flex; align-items:center; gap:4px; font-size:13px; color:var(--color-accent-400); white-space:nowrap; }
.wd-stats { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:14px; padding-top:12px; border-top:1px solid var(--wd-line); }
.wd-stat { display:flex; flex-direction:column; gap:1px; }
.wd-stat b { font-size:20px; font-weight:500; line-height:1.25; }
.wd-stat small { font-size:12px; color:var(--color-neutral-500); }
/* skip */
.wd-skipnote { display:flex; gap:10px; padding:12px 14px; border-radius:10px; font-size:14px;
  background:color-mix(in srgb, var(--color-skip, #d9a441) 10%, transparent); color:var(--color-text); }
.wd-skipnote svg { color:var(--color-skip, #d9a441); flex:0 0 auto; margin-top:2px; }
.wd-reason { font-style:italic; color:var(--color-neutral-300); }
.wd-skipform { display:flex; flex-direction:column; gap:8px; }
.wd-skipform[hidden] { display:none; }
.wd-skipform label { font-size:14px; font-weight:500; }
.wd-skipform input { height:42px; padding:0 12px; border-radius:8px; border:0; font:inherit; color:var(--color-text);
  background:var(--wd-inset); box-shadow:inset 0 0 0 1px var(--color-neutral-700); }
.wd-skipform input:focus { outline:2px solid var(--color-accent); outline-offset:1px; }
.wd-formbtns { display:flex; justify-content:flex-end; gap:8px; }
/* structure */
.wd-struct { display:flex; flex-direction:column; gap:12px; }
.wd-struct .wd-cardhead { margin:0; }
.wd-legend { margin-left:auto; display:flex; gap:10px; font-size:11px; color:var(--color-neutral-500); }
.wd-legend span { display:flex; align-items:center; gap:4px; }
.wd-legend i { width:8px; height:8px; border-radius:2px; }
.wd-strip { display:flex; align-items:center; gap:14px; min-height:56px; padding:10px 14px; border-radius:10px; background:var(--wd-inset);
  box-shadow:inset 0 0 0 1px var(--color-neutral-800); }
.wd-strip-c { width:8px; align-self:stretch; border-radius:3px; flex:0 0 auto; }
.wd-strip-t { flex:1; min-width:0; }
.wd-v { font-size:16px; font-weight:500; }
.wd-n { font-size:13px; color:var(--color-neutral-300); max-width:260px; text-align:right; }
.wd-plot { position:relative; height:200px; border-bottom:1px solid var(--color-neutral-800); }
.wd-bars { position:absolute; inset:0; display:flex; align-items:flex-end; gap:3px; }
.wd-bar { flex-basis:0; min-width:5px; padding:0; border:0; border-radius:4px 4px 0 0; cursor:pointer; opacity:.55; transition:opacity .12s ease; }
.wd-bar:hover { opacity:.8; }
html[data-theme=light] .wd-bar:not(.on) { opacity:.72; }
.wd-bar.on { opacity:1; box-shadow:0 0 0 2px var(--color-text); }
/* A ramp (build / recover) is one sloped shape; its outline follows the slope. */
.wd-bar.ramp { background:none; border-radius:0; }
.wd-bar.ramp svg { display:block; width:100%; height:100%; overflow:visible; }
.wd-bar.ramp.on { box-shadow:none; filter:drop-shadow(2px 0 0 var(--color-text)) drop-shadow(-2px 0 0 var(--color-text)) drop-shadow(0 -2px 0 var(--color-text)); }
.wd-bar:focus-visible { outline:2px solid var(--color-accent); outline-offset:2px; }
.wd-line { position:absolute; left:0; right:0; border-top:1px dashed var(--color-neutral-600); pointer-events:none; }
.wd-line span { position:absolute; left:0; top:-9px; padding:0 6px 0 0; font-size:11px; line-height:18px; color:var(--color-neutral-400);
  background:var(--color-surface); z-index:1; }
.wd-ticks { position:relative; height:14px; font-size:11px; color:var(--color-neutral-500); }
.wd-ticks span { position:absolute; transform:translateX(-50%); white-space:nowrap; }
.wd-ticks span:first-child { transform:none; }
.wd-ticks span:last-child { transform:translateX(-100%); }
/* step table */
.wd-steps { padding:10px; display:flex; flex-direction:column; }
.wd-thead, .wd-row { display:grid; grid-template-columns:26px minmax(0,1.1fr) 96px minmax(0,1.5fr) minmax(0,1.2fr); gap:12px; align-items:center; }
.wd-thead { padding:4px 12px 10px; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--color-neutral-500);
  border-bottom:1px solid var(--wd-line); margin-bottom:4px; }
.wd-row { width:100%; min-height:42px; padding:8px 12px; border:0; border-radius:8px; background:none; color:inherit; font:inherit; font-size:13px;
  text-align:left; cursor:pointer; }
.wd-row:hover { background:var(--wd-inset); }
.wd-row.on { background:color-mix(in srgb, var(--color-accent) 14%, transparent); }
.wd-rn { color:var(--color-neutral-500); font-variant-numeric:tabular-nums; }
.wd-rs { display:flex; align-items:center; gap:10px; }
.wd-rs i { width:8px; height:8px; border-radius:2px; flex:0 0 auto; }
.wd-row.in .wd-rs { padding-left:14px; }
.wd-rt { font-variant-numeric:tabular-nums; }
.wd-rg { color:var(--color-neutral-300); }
.wd-rc { color:var(--color-neutral-500); }
.wd-rep { padding:10px 12px 4px 50px; font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--color-accent-400); }
/* strength */
.wd-strength { display:flex; flex-direction:column; gap:10px; }
.wd-strength .wd-cardhead { margin-bottom:2px; }
.wd-sec { display:flex; align-items:center; gap:8px; font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--color-neutral-500); }
.wd-sec::after { content:""; flex:1; height:1px; background:var(--color-neutral-800); }
.wd-ex { display:flex; flex-direction:column; gap:8px; padding:12px 14px; border-radius:10px; background:var(--wd-inset); }
.wd-ex-top { display:flex; align-items:baseline; justify-content:space-between; gap:10px; }
.wd-ex-n { font-size:15px; font-weight:500; }
.wd-ex-l { font-size:15px; font-weight:500; color:var(--color-accent-300); white-space:nowrap; }
.wd-sets { display:flex; gap:6px; flex-wrap:wrap; }
.wd-set { flex:1 1 56px; max-width:120px; display:flex; flex-direction:column; align-items:center; padding:6px 0; border-radius:8px;
  background:var(--color-surface); font-size:14px; font-weight:500; }
.wd-set small { font-size:11px; font-weight:400; color:var(--color-neutral-500); }
/* side */
.wd-notes { display:flex; flex-direction:column; gap:8px; }
.wd-notes p { margin:0; font-size:14px; color:var(--color-text); }
.wd-notes ul { margin:0; padding-left:18px; font-size:13px; color:var(--color-text); display:flex; flex-direction:column; gap:3px; }
.wd-orig summary { cursor:pointer; list-style:none; }
.wd-orig .wd-text { margin-top:10px; }
.wd-watch { display:flex; align-items:center; gap:10px; font-size:13px; color:var(--color-neutral-400); padding:12px 16px; }
.wd-watch .gs-arrow { display:none; }
.wd-watch svg { color:#7fb87a; }
/* zones */
.wd-zones { padding:0; }
.wd-zones summary { display:flex; flex-direction:column; gap:10px; padding:14px 16px; min-height:44px; cursor:pointer; list-style:none; }
.wd-zones summary::-webkit-details-marker, .wd-orig summary::-webkit-details-marker { display:none; }
.wd-zhead { display:flex; align-items:center; gap:8px; }
.wd-zhead .wd-h { flex:1; }
.wd-zhead .wd-muted { font-size:12px; }
.wd-chev { display:grid; color:var(--color-neutral-500); transition:transform .15s ease; }
.wd-zones[open] .wd-chev { transform:rotate(180deg); }
.wd-zscale { position:relative; display:block; height:14px; border-radius:4px; background:var(--color-neutral-900); }
.wd-zseg { position:absolute; top:0; bottom:0; border-radius:3px; }
.wd-zmark { position:absolute; top:-3px; width:2px; height:20px; border-radius:1px; background:var(--color-text); transform:translateX(-1px); }
.wd-zlabels { position:relative; display:block; height:14px; font-size:11px; line-height:14px; color:var(--color-neutral-500); }
.wd-zlabels span { position:absolute; transform:translateX(-50%); white-space:nowrap; }
.wd-zlabels .wd-zmarkl { color:var(--color-text); top:14px; }
.wd-zones summary .wd-zlabels { height:28px; }
.wd-zrows { display:flex; flex-direction:column; gap:2px; padding:0 12px 14px; }
.wd-zrow { display:grid; grid-template-columns:30px minmax(0,1fr) 64px; gap:10px; align-items:center; padding:6px; border-radius:8px; opacity:.6; }
.wd-zrow.on { opacity:1; background:var(--wd-inset); box-shadow:inset 3px 0 0 var(--color-accent-400); }
.wd-zk { display:grid; place-items:center; height:24px; border-radius:6px; font-size:12px; font-weight:600; }
.wd-zn { display:flex; flex-direction:column; font-size:14px; line-height:1.3; }
.wd-zn small { font-size:12px; color:var(--color-neutral-400); }
.wd-zt { position:relative; height:6px; border-radius:3px; background:var(--color-neutral-800); }
.wd-zt span { position:absolute; top:0; bottom:0; border-radius:3px; }
.wd-zkey { display:flex; align-items:center; gap:6px; margin-top:6px; padding-left:6px; font-size:12px; color:var(--color-neutral-500); }
.wd-zkey span { width:3px; height:12px; border-radius:2px; background:var(--color-accent-400); }
.wd-grab, .wd-actbar { display:none; }
@media (prefers-reduced-motion: reduce) { .wd-gsend.sending .gs-arrow { animation:none; } }

/* Desktop: a near-full-window panel, header actions, two columns. */
@media (min-width: 900px) {
  .wd-panel { top:28px; bottom:28px; left:50%; transform:translateX(-50%); width:min(1360px, calc(100vw - 64px)); border-radius:16px; }
  .wd-head { padding:22px 24px 18px 28px; }
  .wd-close { margin-left:-4px; }
  .wd-body { flex:1; min-height:0; overflow:auto; grid-template-columns:minmax(0,1fr) 340px; padding:24px 28px 28px; }
  .wd-msg { padding:0 28px 14px; }
}
@media (min-width: 900px) and (max-width: 1180px) {
  .wd-body { grid-template-columns:minmax(0,1fr) 300px; }
  .wd-headacts .wd-sep, .wd-headacts [data-wd-go] { display:none; }
  .wd-rc { display:none; }
  .wd-thead, .wd-row { grid-template-columns:26px minmax(0,1.1fr) 90px minmax(0,1.6fr); }
  .wd-thead span:last-child { display:none; }
}

/* Phone: a near full-height sheet; the whole sheet scrolls, actions pinned. */
@media (max-width: 899px) {
  .wd-panel { left:0; right:0; bottom:0; top:max(24px, env(safe-area-inset-top, 0px)); border-radius:20px 20px 0 0; }
  .wd { overflow:auto; overscroll-behavior:contain; }
  .wd-grab { display:grid; place-items:center; padding:8px 0 0; }
  .wd-grab span { width:40px; height:5px; border-radius:3px; background:var(--color-neutral-700); }
  .wd-head { background:none; border:0; padding:6px 16px 0; gap:10px; }
  .wd-head .wd-bar-c, .wd-headacts { display:none; }
  .wd-title { font-size:22px; letter-spacing:-.01em; }
  .wd-chips { flex-wrap:nowrap; overflow-x:auto; margin:0 -16px; padding:0 16px 2px; scrollbar-width:none; }
  .wd-chip { flex:0 0 auto; }
  .wd-body { padding:16px 16px 20px; gap:14px; }
  .wd-main, .wd-side { gap:14px; }
  .wd-card { padding:14px; }
  .wd-msg { padding:0 16px 10px; }
  .wd-actbar { display:flex; gap:8px; position:sticky; bottom:0; margin-top:auto; padding:10px 16px max(14px, env(safe-area-inset-bottom, 0px));
    background:var(--wd-panel); border-top:1px solid var(--wd-line); }
  .wd-big { height:46px; border-radius:10px; font-size:14px; }
  .wd-big.wd-ib { width:46px; }
  .wd-grow { flex:1; }
  .wd-legend, .wd-struct .wd-cardhead .wd-muted { display:none; }
  .wd-plot { height:120px; }
  .wd-bars { gap:2px; }
  .wd-n { max-width:none; text-align:left; }
  .wd-strip { flex-wrap:wrap; }
  .wd-strip .wd-n { flex-basis:100%; padding-left:22px; margin-top:-6px; }
  .wd-steps { padding:6px; }
  .wd-thead { display:none; }
  .wd-row { grid-template-columns:minmax(0,1fr) auto; grid-template-areas:"s t" "g t" "c c"; row-gap:0; min-height:48px; font-size:14px; }
  .wd-rn { display:none; }
  .wd-rs { grid-area:s; }
  .wd-rt { grid-area:t; }
  .wd-rg { grid-area:g; font-size:12px; color:var(--color-neutral-500); padding-left:18px; }
  .wd-rc { grid-area:c; font-size:12px; padding-left:18px; }
  .wd-rc:empty { display:none; }
  .wd-rep { padding-left:12px; }
  .wd-stats { grid-template-columns:repeat(2, minmax(0, 1fr)); }
  .wd-done-top .wd-link { display:none; }
}
"""

WD_JS = r"""
(function () {
  if (window.WD) return;
  var overlay, panel, opts = {}, current = null, changed = false, lastFocus = null, loadSeq = 0;
  var desktop = window.matchMedia('(min-width: 900px)');

  // ?token= and ?plan= ride along on every call, from the URL or the page.
  function params(extra) {
    var src = new URLSearchParams(location.search), out = new URLSearchParams();
    ['token', 'plan'].forEach(function (k) { if (src.get(k)) out.set(k, src.get(k)); });
    var t = document.body && document.body.getAttribute('data-token');
    if (t && !out.get('token')) out.set('token', t);
    Object.keys(extra || {}).forEach(function (k) { out.set(k, extra[k]); });
    return out.toString();
  }
  function url(path, extra) { var q = params(extra); return '/training-plan/api/' + path + (q ? '?' + q : ''); }
  function call(path, body) {
    var init = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    return fetch(url(path, body === undefined ? arguments[2] : null), init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ('Server returned ' + r.status));
        return j;
      });
    });
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  function build() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'wd-overlay';
    overlay.hidden = true;
    overlay.innerHTML = '<div class="wd-scrim" data-wd-close></div><div class="wd-panel" role="dialog" aria-modal="true" aria-labelledby="wd-title"></div>';
    document.body.appendChild(overlay);
    panel = overlay.querySelector('.wd-panel');
    overlay.addEventListener('click', onClick);
    overlay.addEventListener('submit', onSubmit);
    overlay.addEventListener('keydown', onKey);
  }

  function load(id, keepScroll) {
    var seq = ++loadSeq;
    var body = panel.querySelector('.wd-body, .wd');
    var top = keepScroll && body ? body.scrollTop : 0;
    if (!panel.firstChild) panel.innerHTML = '<div class="wd-loading">Loading…</div>';
    return call('workout', undefined, { workout_id: id }).then(function (j) {
      if (seq !== loadSeq) return;
      current = j;
      panel.innerHTML = j.html;
      var zones = panel.querySelector('[data-wd-zones]');
      if (zones) zones.open = desktop.matches;
      if (opts.inPlan) panel.querySelectorAll('.wd-open-plan').forEach(function (a) { a.remove(); });
      var scroller = panel.querySelector(desktop.matches ? '.wd-body' : '.wd');
      if (scroller) scroller.scrollTop = top;
      if (!keepScroll) { var c = panel.querySelector('[data-wd-close]'); if (c) c.focus({ preventScroll: true }); }
    }).catch(function (err) {
      if (seq !== loadSeq) return;
      if (!current && opts.fallback && opts.fallback(id)) { close(); return; }
      panel.innerHTML = '<div class="wd-err"><div>Couldn’t load this workout.</div><div class="wd-muted">' + esc(err.message) + '</div>'
        + '<button type="button" class="wd-btn" data-wd-close>Close</button></div>';
    });
  }

  function open(id, o) {
    build();
    if (o) opts = o;
    if (overlay.hidden) {
      lastFocus = document.activeElement;
      changed = false;
      current = null;
      panel.innerHTML = '';
      overlay.hidden = false;
      document.documentElement.classList.add('wd-lock');
    }
    return load(id, false);
  }

  function close() {
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    panel.innerHTML = '';
    document.documentElement.classList.remove('wd-lock');
    var was = changed;
    changed = false; current = null; loadSeq++;
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus({ preventScroll: true }); } catch (e) { /* gone */ } }
    lastFocus = null;
    if (opts.onClose) opts.onClose(was);
  }

  function say(text, ok) {
    var m = panel.querySelector('.wd-msg');
    if (!m) return;
    m.textContent = text || '';
    m.classList.toggle('ok', !!ok);
  }

  // A change went through: tell the page (it refreshes what's behind), then
  // redraw the view from the server.
  function act(button, promise) {
    if (button) button.classList.add('busy');
    say('');
    return promise.then(function (j) {
      changed = true;
      if (opts.onChange) opts.onChange(j);
      return load(current.id, true);
    }).catch(function (err) {
      if (button) button.classList.remove('busy');
      say(err.message);
    });
  }

  function select(i, focus, toggle) {
    var bar = panel.querySelector('.wd-bar[data-wg-i="' + i + '"]');
    if (!bar) return;
    var wasOn = bar.classList.contains('on');
    panel.querySelectorAll('.wd-bar.on, .wd-row.on').forEach(function (x) {
      x.classList.remove('on');
      if (x.classList.contains('wd-bar')) x.setAttribute('aria-pressed', 'false');
    });
    if (toggle && wasOn) {      // clicking the selected step again clears it
      var s0 = panel.querySelector('.wd-strip');
      s0.querySelector('.wd-k').textContent = s0.getAttribute('data-hint') || '';
      s0.querySelector('.wd-v').textContent = '';
      s0.querySelector('.wd-strip-c').style.background = 'transparent';
      s0.querySelector('.wd-n').hidden = true;
      return;
    }
    bar.classList.add('on');
    bar.setAttribute('aria-pressed', 'true');
    // A repeat's rows stand for every rep: light the row of the same step.
    var k = bar.getAttribute('data-k');
    panel.querySelectorAll('.wd-row').forEach(function (r) {
      var b = panel.querySelector('.wd-bar[data-wg-i="' + r.getAttribute('data-wg-i') + '"]');
      if (r.getAttribute('data-wg-i') === String(i) || (b && sameStep(b, bar))) r.classList.add('on');
    });
    var strip = panel.querySelector('.wd-strip');
    strip.querySelector('.wd-k').textContent = k;
    strip.querySelector('.wd-v').textContent = bar.getAttribute('data-v');
    strip.querySelector('.wd-strip-c').style.background = bar.getAttribute('data-c');
    var note = strip.querySelector('.wd-n');
    note.textContent = bar.getAttribute('data-n');
    note.hidden = !bar.getAttribute('data-n');
    if (focus) bar.focus();
  }
  function sameStep(a, b) {
    var strip = function (x) { return x.getAttribute('data-k').replace(/^Step \d+ of \d+ · /, '').replace(/ · Rep \d+ of \d+$/, ''); };
    return /Rep /.test(a.getAttribute('data-k')) && /Rep /.test(b.getAttribute('data-k'))
      && strip(a) === strip(b) && a.getAttribute('data-v') === b.getAttribute('data-v');
  }

  function menu(show) {
    panel.querySelectorAll('.wd-menu').forEach(function (m) { m.hidden = true; });
    panel.querySelectorAll('[data-wd-menu]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    if (show) {
      show.setAttribute('aria-expanded', 'true');
      var m = show.parentNode.querySelector('.wd-menu');
      m.hidden = false;
      var first = m.querySelector('a, button');
      if (first) first.focus();
    }
  }

  function skipForm(show) {
    var f = panel.querySelector('[data-wd-skipform]');
    if (!f) return;
    f.hidden = !show;
    if (show) {
      f.scrollIntoView({ block: 'nearest' });
      var input = f.querySelector('input');
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }

  function onClick(e) {
    var t = e.target;
    var menuBtn = t.closest('[data-wd-menu]');
    if (menuBtn) { var m = menuBtn.parentNode.querySelector('.wd-menu'); return menu(m.hidden ? menuBtn : null); }
    if (!t.closest('.wd-menu')) menu(null);
    if (t.closest('[data-wd-close]')) return close();
    var go = t.closest('[data-wd-go]');
    if (go && !go.disabled) return load(go.getAttribute('data-wd-go'), false);
    var bar = t.closest('[data-wg-i]');
    if (bar) return select(bar.getAttribute('data-wg-i'), false, true);
    var id = current && current.id;
    var complete = t.closest('[data-wd-complete]');
    if (complete) return act(complete, call('completion', { workout_id: id, completed: complete.getAttribute('data-wd-complete') === 'on' }));
    var skip = t.closest('[data-wd-skip]');
    if (skip) {
      if (skip.getAttribute('data-wd-skip') === 'on') return skipForm(true);
      return act(skip, call('skip', { workout_id: id, skipped: false }));
    }
    if (t.closest('[data-wd-reason]')) return skipForm(true);
    if (t.closest('[data-wd-skipcancel]')) return skipForm(false);
    var unsend = t.closest('[data-wd-unsend]');
    if (unsend && !unsend.disabled) {
      unsend.classList.add('sending'); unsend.disabled = true;
      say('');
      return call('garmin/unsend', { workout_id: id }).then(function (j) {
        changed = true;
        if (opts.onChange) opts.onChange(j);
        return load(id, true).then(function () { say('Taken off your watch.', true); });
      }).catch(function (err) { unsend.classList.remove('sending'); unsend.disabled = false; say(err.message); });
    }
    var send = t.closest('[data-wd-send]');
    if (send && !send.disabled) {
      send.classList.add('sending'); send.disabled = true;
      say('');
      return call('garmin/send', { workout_id: id }).then(function (j) {
        changed = true;
        if (opts.onChange) opts.onChange(j);
        return load(id, true).then(function () {
          say('Created on Garmin and scheduled for ' + (j.scheduled_date || 'its day') +
            ((j.warnings || []).length ? ' — Garmin changed: ' + j.warnings.join('; ') : '.'), true);
        });
      }).catch(function (err) { send.classList.remove('sending'); send.disabled = false; say(err.message); });
    }
    var a = t.closest('[data-wd-act]');
    if (a) {
      var what = a.getAttribute('data-wd-act');
      if (opts.onAction) { close(); return opts.onAction(what, id); }
      var q = params({ workout: id, do: what });
      location.href = '/training-plan?' + q;
      return;
    }
    var activity = t.closest('[data-wd-activity]');
    if (activity && !opts.inPlan && window.openActivityModal) {
      e.preventDefault();
      var aid = +activity.getAttribute('data-wd-activity');
      close();
      return window.openActivityModal(aid);
    }
  }

  function onSubmit(e) {
    var f = e.target.closest('[data-wd-skipform]');
    if (!f) return;
    e.preventDefault();
    act(f.querySelector('[type=submit]'), call('skip', { workout_id: current.id, skipped: true, reason: f.querySelector('input').value.trim() }));
  }

  function onKey(e) {
    e.stopPropagation();      // the page underneath doesn't act on keys meant for the view
    if (e.key === 'Escape') {
      e.stopPropagation();
      var openMenu = panel.querySelector('.wd-menu:not([hidden])');
      if (openMenu) { menu(null); return; }
      var f = panel.querySelector('[data-wd-skipform]:not([hidden])');
      if (f && f.contains(e.target)) { skipForm(false); return; }
      return close();
    }
    if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && e.target.classList && e.target.classList.contains('wd-bar')) {
      e.preventDefault();
      select(+e.target.getAttribute('data-wg-i') + (e.key === 'ArrowLeft' ? -1 : 1), true);
      return;
    }
    if (e.key === 'Tab') {     // keep focus inside the view
      var f2 = [].filter.call(panel.querySelectorAll('button:not([disabled]), a[href], input, summary, [tabindex="0"]'),
        function (x) { return x.offsetParent !== null; });
      if (!f2.length) return;
      var first = f2[0], last = f2[f2.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  // Pages handle their own Escape and arrow keys; while open, the view owns them.
  document.addEventListener('keydown', function (e) {
    if (overlay && !overlay.hidden && !overlay.contains(e.target) && e.key === 'Escape') { e.stopPropagation(); close(); }
  }, true);

  window.WD = { open: open, close: close, isOpen: function () { return !!overlay && !overlay.hidden; } };
})();
"""
