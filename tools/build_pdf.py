#!/usr/bin/env python3
"""Build the printable at-a-glance PDF from a training plan JSON.

    python3 tools/build_pdf.py plan.json -o plan.pdf

Renders through WeasyPrint, the only engine available in the container, so
what you get here is what production produces. On Windows that means running
under WSL — WeasyPrint needs pango/harfbuzz.

Letter landscape. Every page carries a running footer (athlete · event and
"page / pages") drawn in the @page margin, so it can never collide with the
content.

  page 1        overview — phase band over a 16-week volume chart (hours by
                sport), zone tables, projected splits
  pages 2..n+1  one page per week: header with hours by sport, focus note,
                seven day columns of session cards, a notes block per day
  last page     race strategy (only when the plan has a race)

This PDF is a printable reference, not the plan. Full session structures stay
in the JSON and the HTML viewer; each card carries only sport, start time,
duration · distance · zone, title and key targets.

Print rules the layout keeps to:
  * One format per quantity: session durations h:mm, splits h:mm:ss, swim
    distances in metres, bike/run in km, zones as Z2 / Z2–3.
  * No large dark fills — tints and outlines only, so it is cheap on ink.
  * Nothing is clipped. Session cards share a minimum height so a week's
    rows line up, and grow when their text needs more; the notes block under
    each day is what gives way.
"""

import argparse
import html
import json
import pathlib
import re
import sys
import tempfile
from datetime import date

# ─────────────────────────────── palette ───────────────────────────────

INK = "#15171c"
INK_2 = "#2a2f38"
INK_3 = "#3d4350"
MUTED = "#5b616e"
RULE = "#c9ced8"
RULE_LIGHT = "#e3e6eb"
PANEL = "#f3f4f6"

# Darkened from the viewer's sport colours so each reads at 4.5:1 on white
# and stays distinct in grayscale.
SPORT_COLORS = {
    "swim": "#0b7285", "bike": "#2f7a3a", "run": "#b4530f",
    "strength": "#6741b5", "brick": "#a3366a", "race": "#8a6508", "rest": "#8a8f9c",
}
SPORT_ORDER = ["swim", "bike", "run", "brick", "strength", "race"]
# Bottom-to-top stacking in the volume chart: the big aerobic blocks sit low.
STACK_ORDER = ["bike", "brick", "run", "swim", "strength", "race"]

# Light-to-dark tints for phases, in plan order. Dark text on every one of
# them, so no phase prints as a solid block of ink.
PHASE_RAMP = ["#eef0f3", "#e1e4e9", "#d2d6de", "#bfc5cf", "#aab1bf", "#959dad"]

FONT_DIR = pathlib.Path(__file__).resolve().parent / "assets" / "fonts"
FONT_FILES = [
    ("Barlow", 400, "Barlow-Regular.ttf"),
    ("Barlow", 500, "Barlow-Medium.ttf"),
    ("Barlow", 600, "Barlow-SemiBold.ttf"),
    ("Barlow", 700, "Barlow-Bold.ttf"),
    ("Barlow Condensed", 500, "BarlowCondensed-Medium.ttf"),
    ("Barlow Condensed", 600, "BarlowCondensed-SemiBold.ttf"),
    ("Barlow Condensed", 700, "BarlowCondensed-Bold.ttf"),
]

# Week-page geometry (CSS px at 96/in). The card area is what a week's
# session slots share; the notes block takes whatever is left below it.
PAGE_H = 715            # 7.45in: a hair under the 7.55in content box
HEAD_H = 60             # week header band, fixed
BLOCK_GAP = 9
FOCUS_LINE = 17         # focus paragraph line height
FOCUS_CHARS = 92        # conservative characters per focus line (main column)
SIDE_CHARS = 22         # … and in the side-note box
SLOT_AREA = 380
SLOT_GAP = 6
CHART_H = 230
CONTENT_W = 979         # 10.2in: Letter landscape less the side margins
NAME_CHAR_W = 6.2       # px per character, phase names (condensed bold 10.5pt)
LABEL_CHAR_W = 5.0      # px per character, phase week/hours labels (8.5pt)
GRID_SLACK = 12


R = ' class="r"'


def strip_kmh(v):
    return re.sub(r"\s*km/h", "", str(v or ""))


def e(v):
    return html.escape("" if v is None else str(v))


# ────────────────────────────── formatting ──────────────────────────────

def parse_date(s):
    y, m, d = (int(x) for x in str(s)[:10].split("-"))
    return date(y, m, d)


def short_date(s):
    d = parse_date(s)
    return f"{d.strftime('%b')} {d.day}"


def day_date(s):
    d = parse_date(s)
    return f"{d.strftime('%a')} {d.strftime('%b')} {d.day}"


def full_date(s):
    d = parse_date(s)
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"


def week_range(a, b):
    da, db = parse_date(a), parse_date(b)
    return f"{day_date(a)} – {day_date(b)}, {db.year}" if da.year == db.year else \
        f"{day_date(a)}, {da.year} – {day_date(b)}, {db.year}"


def hm(minutes):
    """Session and weekly durations: always h:mm (0:50, 1:30, 12:05)."""
    if minutes is None:
        return ""
    m = int(round(minutes))
    return f"{m // 60}:{m % 60:02d}"


def fmt_split(leg, value):
    """Race splits arrive as '46:00' (swim, mm:ss), '3:12' (bike, h:mm) or
    '5:32' (total, h:mm). Two-part values are ambiguous on their own, so the
    leg decides: swim and transitions are minutes, everything else hours.
    Output is always h:mm:ss."""
    s = str(value or "").strip()
    nums = re.findall(r"\d+", s.split(" ")[0])
    if not nums:
        return s
    parts = [int(n) for n in nums[:3]]
    if len(parts) == 3:
        h, m, sec = parts
    elif len(parts) == 2:
        if str(leg).strip().lower() in ("swim", "t1", "t2"):
            h, (m, sec) = 0, parts
        else:
            (h, m), sec = parts, 0
    else:
        h, m, sec = 0, parts[0], 0
    h, m = h + m // 60, m % 60
    return f"{h}:{m:02d}:{sec:02d}"


def zone_short(z):
    """'Zone 2' → 'Z2', 'Zone 2-3' → 'Z2–3'. Anything else ('Race') drops."""
    m = re.match(r"\s*(?:zone|z)\s*(\d[a-c]?)(?:\s*[-–]\s*(?:z(?:one)?\s*)?(\d[a-c]?))?\s*$",
                 str(z or ""), re.IGNORECASE)
    if not m:
        return ""
    return f"Z{m.group(1)}" + (f"–{m.group(2)}" if m.group(2) else "")


def _num(v):
    return f"{v:,.1f}".rstrip("0").rstrip(".") if v % 1 else f"{int(v):,}"


def distance_label(w, prefs):
    """Swim in metres (or yards), bike and run in km (or miles)."""
    sport = w.get("sport")
    meters = w.get("distanceMeters")
    km = w.get("distanceKm")
    if meters is None and km is None:
        return ""
    if sport == "swim":
        m = meters if meters is not None else km * 1000
        return f"{_num(m)} {'yd' if prefs.get('swim') == 'yards' else 'm'}"
    k = km if km is not None else meters / 1000
    if prefs.get(sport) == "miles":
        return f"{_num(round(k / 1.609344, 1))} mi"
    return f"{_num(round(k, 1))} km"


_DATE_OR_RANGE = re.compile(r"\d{4}-\d{2}-\d{2}|(\d)\s*-\s*(Z?\d)")


def tidy(text):
    """Light typographic clean-up of free text from the plan: '2 x 400m' →
    '2 × 400 m', '170-185' → '170–185'. ISO dates are left alone."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = re.sub(r"(\d)\s*[xX]\s*(\d)", r"\1 × \2", t)
    t = re.sub(r"(?<=\d)K\b", " km", t)
    t = re.sub(r"(?<=\d)(m|km|min|W|g|lb|kg|mi|yd)\b", r" \1", t)
    t = _DATE_OR_RANGE.sub(lambda m: m.group(0) if m.group(1) is None
                           else f"{m.group(1)}–{m.group(2)}", t)
    return t


def key_targets(w):
    """Prefer an explicit field; fall back to the KEY TARGETS block many
    plans embed in humanReadable."""
    if w.get("keyTargets"):
        return str(w["keyTargets"]).strip()
    m = re.search(r"KEY TARGETS?:\s*(.+?)(?:\n\s*\n|$)", w.get("humanReadable", "") or "",
                  re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def split_side_note(focus):
    """Week focus text often ends with a bouldering/climbing note; give it its
    own box rather than burying it in the paragraph."""
    m = re.search(r"\s*\b(Bouldering|Climbing)\b[^:.]{0,24}:\s*", focus or "")
    if not m:
        return focus or "", "", ""
    return focus[:m.start()].strip(), m.group(1), focus[m.end():].strip()


def first_sentence(text):
    text = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


# ───────────────────────────── plan helpers ─────────────────────────────

def sessions(day):
    ws = [w for w in day.get("workouts", []) if w.get("sport") != "rest"]
    return sorted(ws, key=lambda w: w.get("startTime") or "99:99")


def seven_days(week):
    """The week's days as a full Monday-to-Sunday run from startDate: plans
    may list only the days that have sessions, but the page always shows
    seven columns, with the empty ones as rest days."""
    listed = {str(d.get("date"))[:10]: d for d in week.get("days", [])}
    try:
        start = parse_date(week["startDate"])
    except (KeyError, ValueError):
        return week.get("days", [])
    out = []
    for i in range(7):
        d = date.fromordinal(start.toordinal() + i)
        iso = d.isoformat()
        out.append(listed.get(iso) or {"date": iso, "dayOfWeek": d.strftime("%A"), "workouts": []})
    return out


def sport_minutes(week):
    out = {}
    for day in week.get("days", []):
        for w in sessions(day):
            s = w.get("sport", "other")
            out[s] = out.get(s, 0) + (w.get("durationMinutes") or 0)
    return out


def phase_styles(plan):
    """Tint per phase name, light → dark through the plan. A final 'Race'
    phase is drawn as an outline instead of a fill."""
    phases = plan.get("phases", [])
    n = len(phases)
    styles = {}
    for i, p in enumerate(phases):
        if str(p.get("name", "")).strip().lower() == "race":
            styles[p["name"]] = f"background:#ffffff;border:1.5px solid {INK}"
        else:
            idx = 0 if n <= 1 else round(i * (len(PHASE_RAMP) - 1) / max(n - 1, 1))
            styles[p["name"]] = f"background:{PHASE_RAMP[min(idx, len(PHASE_RAMP) - 1)]}"
    return styles


def phase_for(plan, week):
    for p in plan.get("phases", []):
        if p["startWeek"] <= week["weekNumber"] <= p["endWeek"]:
            return p
    return None


# ──────────────────────────── overview page ────────────────────────────

def cover_page(plan):
    meta = plan["meta"]
    rs = plan.get("raceStrategy") or {}
    race = rs.get("event")
    pacing = rs.get("pacing") or {}
    goals = pacing.get("goals") or {}
    total = pacing.get("projectedTotal") or {}

    sub = []
    if race:
        sub.append(full_date(race["date"]))
        d = race.get("distances") or {}
        if d.get("swim") is not None:
            sub.append(f"{_num(d['swim'] / 1000)} km swim")
        if d.get("bike") is not None:
            sub.append(f"{_num(d['bike'])} km bike")
        if d.get("run") is not None:
            sub.append(f"{_num(d['run'])} km run")

    if not sub and meta.get("planStartDate") and meta.get("planEndDate"):
        sub.append(f"{full_date(meta['planStartDate'])} – {full_date(meta['planEndDate'])}")

    boxes = ""
    if goals.get("a"):
        boxes += goal_box("A goal", goals["a"], strong=True)
    if goals.get("b"):
        boxes += goal_box("B goal", goals["b"])
    if total.get("central"):
        boxes += goal_box("Projected", fmt_split("total", total["central"])[:-3], outline=True)

    head = f"""
    <header class="cover-head">
      <div>
        <div class="kicker">{e(meta.get('generatedBy', 'Claude Coach'))} · Training plan for {e(meta.get('athlete', ''))}</div>
        <h1>{e(meta.get('event'))}</h1>
        {f'<div class="cover-sub">{e(" · ".join(sub))}</div>' if sub else ''}
      </div>
      <div class="goal-boxes">{boxes}</div>
    </header>"""

    weeks = sorted(plan.get("weeks", []), key=lambda w: w["weekNumber"])
    span = ""
    if weeks:
        span = f"{len(weeks)} weeks · {short_date(weeks[0]['startDate'])} → " \
               f"{short_date(weeks[-1]['endDate'])}, {parse_date(weeks[-1]['endDate']).year}"
    legend = "".join(
        f'<span class="lg"><span class="sw" style="background:{SPORT_COLORS[s]}"></span>{s.capitalize()}</span>'
        for s in SPORT_ORDER if any(sport_minutes(w).get(s) for w in weeks)
    )
    caption = "Bar label = planned hours, summed from the sessions. REC = recovery week."
    key = phase_key(plan, weeks)
    if key:
        caption += " Phases: " + " · ".join(key) + "."
    volume = f"""
    <section class="volume">
      <div class="sec-head"><div class="legend">{legend}</div><h2>{e(span)}</h2></div>
      {volume_table(plan, weeks)}
      <div class="caption">{e(caption)}</div>
    </section>"""

    cols = zone_columns(plan) + splits_column(plan)
    bottom = f'<section class="zones"><div class="zrow">{"".join(cols)}</div></section>' if cols else ""

    return f'<section class="page cover">{head}{volume}{bottom}</section>'


def goal_box(label, value, strong=False, outline=False):
    cls = "goal strong" if strong else ("goal outline" if outline else "goal")
    return f'<div class="{cls}"><span class="gl">{e(label)}</span><span class="gv">{e(value)}</span></div>'


def _fits(name, width):
    """Does a phase name fit a band cell: every word on one line, at most
    three lines? Character widths are Barlow Condensed Bold 10.5pt averages."""
    words = name.split()
    if not words or max(len(w) for w in words) * NAME_CHAR_W > width:
        return False
    return len(name) * NAME_CHAR_W <= width * 3


def phase_key(plan, weeks):
    """'1 Recovery & Transition (Wk 1)' for each phase too narrow to name."""
    if not weeks:
        return []
    col_w = CONTENT_W / len(weeks)
    out = []
    for i, p in enumerate(plan.get("phases", []), 1):
        n = p["endWeek"] - p["startWeek"] + 1
        if n > 0 and not _fits(str(p["name"]), n * col_w - 16):
            wk = f"Wk {p['startWeek']}" if n == 1 else f"Wk {p['startWeek']}–{p['endWeek']}"
            out.append(f"{i} {p['name']} ({wk})")
    return out


def volume_table(plan, weeks):
    """Phase band and weekly volume bars share one table, so a phase cell
    always sits exactly over its weeks."""
    if not weeks:
        return ""
    styles = phase_styles(plan)
    phases = plan.get("phases", [])

    band = ""
    covered = 0
    col_w = CONTENT_W / len(weeks)
    for i, p in enumerate(phases, 1):
        n = p["endWeek"] - p["startWeek"] + 1
        if n <= 0:
            continue
        covered += n
        inner = n * col_w - 4 - 12          # cell spacing and padding
        hr = p.get("weeklyHoursRange") or {}
        weeks_lbl = f"Wk {p['startWeek']}" if n == 1 else f"Wk {p['startWeek']}–{p['endWeek']}"
        hours_lbl = ""
        if hr.get("low") is not None and hr.get("high") is not None and (hr["low"] or hr["high"]):
            hours_lbl = f" · {hr['low']:g}–{hr['high']:g} h"
        if len(weeks_lbl + hours_lbl) * LABEL_CHAR_W > inner:
            hours_lbl = ""
        if len(weeks_lbl) * LABEL_CHAR_W > inner:
            weeks_lbl = ""          # the bars below and the key carry it
        name = str(p["name"])
        if _fits(name, inner):
            label = f'<b>{e(name)}</b>'
        else:
            # Too narrow for its name: a number here, the name in the key below.
            label = f'<b>{i}</b>'
        band += (f'<td colspan="{n}" class="phase" style="{styles.get(p["name"], "")}">'
                 f'{label}' + (f'<span>{e(weeks_lbl + hours_lbl)}</span>' if weeks_lbl else "") + '</td>')
    if covered < len(weeks):
        band += f'<td colspan="{len(weeks) - covered}"></td>'

    mins = [sport_minutes(w) for w in weeks]
    peak = max((sum(m.values()) for m in mins), default=0) or 1
    scale = (CHART_H - 18) / peak

    bars = labels = ""
    for w, m in zip(weeks, mins):
        segs = "".join(
            f'<div style="height:{m[s] * scale:.1f}px;background:{SPORT_COLORS.get(s, "#8a8f9c")}"></div>'
            for s in reversed(STACK_ORDER) if m.get(s)
        )
        other = sum(v for k, v in m.items() if k not in STACK_ORDER)
        if other:
            segs += f'<div style="height:{other * scale:.1f}px;background:#8a8f9c"></div>'
        tot = sum(m.values())
        bars += (f'<td class="bar"><div class="bt">{f"{tot / 60:.1f}" if tot else "—"}</div>'
                 f'<div class="stack">{segs}</div></td>')
        rec = '<span class="rec">REC</span>' if w.get("isRecoveryWeek") else ""
        labels += f'<td class="wl"><b>W{e(w["weekNumber"])}</b>{rec}</td>'

    return (f'<table class="vol"><tr class="band">{band}</tr>'
            f'<tr class="bars" style="height:{CHART_H}px">{bars}</tr><tr>{labels}</tr></table>')


# Zones are computed live from FTP/LTHR/threshold-pace using the standard
# tables below, not read from the arrays baked into the JSON: those are
# sometimes partial and never reflect overrides made later in the viewer.
HR_PCT_TABLE = [
    {"zone": "1",  "name": "Recovery",      "low": 0,   "high": 81},
    {"zone": "2",  "name": "Aerobic",       "low": 81,  "high": 89},
    {"zone": "3",  "name": "Tempo",         "low": 90,  "high": 93},
    {"zone": "4",  "name": "Sub-threshold", "low": 94,  "high": 99},
    {"zone": "5a", "name": "Threshold",     "low": 100, "high": 102},
    {"zone": "5b", "name": "VO2max",        "low": 103, "high": 106},
    {"zone": "5c", "name": "Anaerobic",     "low": 106, "high": 120},
]
BIKE_POWER_PCT_TABLE = [
    {"zone": "1",  "name": "Recovery",      "low": 0,   "high": 55},
    {"zone": "2",  "name": "Aerobic",       "low": 56,  "high": 75},
    {"zone": "3",  "name": "Tempo",         "low": 76,  "high": 90},
    {"zone": "4",  "name": "Sub-threshold", "low": 91,  "high": 99},
    {"zone": "5a", "name": "Threshold",     "low": 100, "high": 105},
    {"zone": "5b", "name": "VO2max",        "low": 106, "high": 120},
    {"zone": "5c", "name": "Anaerobic",     "low": 120, "high": 150},
]
# Run pace offsets in seconds/km from threshold pace (positive = slower).
RUN_PACE_OFFSET_TABLE = [
    {"zone": "1",  "low": 70,  "high": 90},
    {"zone": "2",  "low": 50,  "high": 70},
    {"zone": "3",  "low": 15,  "high": 25},
    {"zone": "4",  "low": 5,   "high": 15},
    {"zone": "5a", "low": -2,  "high": 2},
    {"zone": "5b", "low": -20, "high": -15},
    {"zone": "5c", "low": -35, "high": -25},
]
# Swim pace offsets in seconds/100m from CSS.
SWIM_PACE_OFFSET_TABLE = [
    {"zone": "1", "name": "Recovery",  "low": 15, "high": 20},
    {"zone": "2", "name": "Aerobic",   "low": 8,  "high": 12},
    {"zone": "3", "name": "Tempo",     "low": 3,  "high": 6},
    {"zone": "4", "name": "Threshold", "low": 0,  "high": 0},
    {"zone": "5", "name": "VO2max",    "low": -5, "high": -3},
]


def _pct(base, p):
    return round(base * p / 100) if base is not None else None


def _fmt_mmss(total_sec):
    s = round(total_sec)
    return f"{s // 60}:{s % 60:02d}"


def _resolve_single_pace(s):
    """A threshold/CSS pace is sometimes a tested range ('4:06-4:20/km');
    average it to one working pace, same as the HTML viewer does."""
    secs = [int(m.group(1)) * 60 + int(m.group(2)) for m in re.finditer(r"(\d+):(\d+)", str(s or ""))]
    return _fmt_mmss(sum(secs) / len(secs)) if secs else ""


def _parse_mmss(s):
    m = re.search(r"(\d+):(\d+)", str(s or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _range_str(lo, hi, fmt=str):
    vals = sorted([lo, hi])
    a, b = fmt(vals[0]), fmt(vals[1])
    return a if a == b else f"{a}–{b}"


def zone_status(sport, zones_validated, source=None, css_status=None):
    """'Validated' from the viewer's own flag, else what the plan's source
    note says (a 'TESTED: …' FTP is tested, not estimated)."""
    v = (zones_validated or {}).get(sport)
    if v and v.get("validated"):
        return "validated"
    if sport == "swim" and css_status:
        return str(css_status).lower()
    src = str(source or "").strip().upper()
    if src.startswith("TESTED"):
        return "tested"
    if src.startswith("VALIDATED"):
        return "validated"
    return "estimated"


def zone_columns(plan):
    z = plan.get("zones") or {}
    ov = plan.get("overrides") or {}
    zv = plan.get("zonesValidated") or {}
    out = []

    bike = z.get("bike") or {}
    power = bike.get("power") or {}
    ftp = ov.get("ftp", power.get("ftp"))
    bike_lthr = ov.get("bikeLthr", (bike.get("hr") or {}).get("lthr"))
    if power or ftp:
        rows = []
        for zz, hz in zip(BIKE_POWER_PCT_TABLE, HR_PCT_TABLE):
            watts = _range_str(_pct(ftp, zz["low"]), _pct(ftp, zz["high"])) if ftp else "—"
            hr = _range_str(_pct(bike_lthr, hz["low"]), _pct(bike_lthr, hz["high"])) if bike_lthr else "—"
            rows.append([f"{zz['zone']} {zz['name']}", watts, hr])
        head = [f"FTP {ftp} W" if ftp else "", f"LTHR {bike_lthr}" if bike_lthr else "",
                zone_status("bike", zv, None if "ftp" in ov else power.get("ftpSource"))]
        out.append(zone_col("Bike", "bike", " · ".join(h for h in head if h), ["Zone", "W", "HR"], rows))

    run = z.get("run") or {}
    run_lthr = ov.get("runLthr", (run.get("hr") or {}).get("lthr"))
    threshold_pace = ov.get("thresholdPace") or _resolve_single_pace((run.get("pace") or {}).get("thresholdPace", ""))
    if run.get("hr") or run.get("pace") or run_lthr or threshold_pace:
        threshold_sec = _parse_mmss(threshold_pace)
        rows = []
        for hz, pz in zip(HR_PCT_TABLE, RUN_PACE_OFFSET_TABLE):
            hr = _range_str(_pct(run_lthr, hz["low"]), _pct(run_lthr, hz["high"])) if run_lthr else "—"
            pace = (_range_str(threshold_sec + pz["low"], threshold_sec + pz["high"], _fmt_mmss)
                    if threshold_sec is not None else "—")
            rows.append([f"{hz['zone']} {hz['name']}", hr, pace])
        source = None if ("thresholdPace" in ov or "runLthr" in ov) else \
            ((run.get("pace") or {}).get("paceSource") or (run.get("hr") or {}).get("lthrSource"))
        head = [f"LTHR {run_lthr}" if run_lthr else "",
                f"T {threshold_pace}/km" if threshold_pace else "",
                zone_status("run", zv, source)]
        out.append(zone_col("Run", "run", " · ".join(h for h in head if h), ["Zone", "HR", "/km"], rows))

    swim = z.get("swim") or {}
    css_seconds = ov.get("cssSeconds", swim.get("cssSeconds"))
    css_label = ov.get("cssLabel", swim.get("css", ""))
    if swim.get("zones") or css_seconds:
        rows = []
        for zz in SWIM_PACE_OFFSET_TABLE:
            pace = (_range_str(css_seconds + zz["low"], css_seconds + zz["high"], _fmt_mmss)
                    if css_seconds is not None else "—")
            rows.append([f"{zz['zone']} {zz['name']}", pace])
        css = re.sub(r"/100\s*m", "/100 m", str(css_label)) if css_label else ""
        head = (f"CSS {css}" if css else "CSS") + " · " + zone_status(
            "swim", zv, swim.get("cssSource"), None if "cssSeconds" in ov else swim.get("cssStatus"))
        out.append(zone_col("Swim", "swim", head, ["Zone", "/100 m"], rows))

    return out


def zone_col(title, sport, head, heads, rows, color=None):
    color = color or SPORT_COLORS.get(sport, INK)
    th = "".join(f"<th{R if i else ''}>{e(h)}</th>" for i, h in enumerate(heads))
    body = ""
    for r in rows:
        cls = ' class="total"' if r and str(r[0]).lower() in ("finish", "total") else ""
        body += f"<tr{cls}>" + "".join(
            f"<td{R if i else ''}>{e(c)}</td>" for i, c in enumerate(r)) + "</tr>"
    return (f'<div class="zcol"><div class="zhead" style="border-bottom-color:{color}">'
            f'<span class="zv">{e(head)}</span><span class="zt" style="color:{color}">{e(title)}</span></div>'
            f'<table class="ztbl"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>')


def splits_column(plan):
    rs = plan.get("raceStrategy") or {}
    if not rs.get("event"):
        return []
    pacing = rs.get("pacing") or {}
    rows = pacing.get("projectedSplits")
    total = pacing.get("projectedTotal") or {}
    if rows:
        body = [[r.get("leg"), *(fmt_split(r.get("leg"), r.get(k)) if r.get(k) else "—"
                                 for k in ("conservative", "central", "strong"))]
                for r in rows if str(r.get("leg", "")).lower() != "total"]
        tot_row = next((r for r in rows if str(r.get("leg", "")).lower() == "total"), None) or total
        if tot_row:
            body.append(["Finish", *(fmt_split("total", tot_row.get(k)) if tot_row.get(k) else "—"
                                     for k in ("conservative", "central", "strong"))])
        return [zone_col("Projected splits", "", "h:mm:ss", ["Leg", "Slow", "Central", "Strong"], body, INK)]

    # No three-way matrix in the plan: the single central estimate per leg.
    trans = pacing.get("transitions") or {}
    legs = [("Swim", (pacing.get("swim") or {}).get("projectedSplit")),
            ("T1", (trans.get("t1") or "").split(" ")[0] or None),
            ("Bike", (pacing.get("bike") or {}).get("projectedSplit")),
            ("T2", (trans.get("t2") or "").split(" ")[0] or None),
            ("Run", (pacing.get("run") or {}).get("projectedSplit"))]
    body = [[n, fmt_split(n, v)] for n, v in legs if v]
    if total.get("central"):
        body.append(["Finish", fmt_split("total", total["central"])])
    return [zone_col("Projected splits", "", "h:mm:ss", ["Leg", "Central"], body, INK)] if body else []


# ───────────────────────────── week pages ─────────────────────────────

def checkbox(done=False):
    return f'<span class="cb{" done" if done else ""}"></span>'


def session_card(w, prefs):
    sport = w.get("sport", "")
    color = SPORT_COLORS.get(sport, SPORT_COLORS["rest"])
    meta = [hm(w.get("durationMinutes")), distance_label(w, prefs), zone_short(w.get("primaryZone"))]
    if w.get("type") == "rouvy-tag" and not meta[1]:
        meta[1] = "Rouvy"
    kt = tidy(key_targets(w))
    return (
        f'<td class="card" style="border-top-color:{color}">'
        f'<div class="ch"><span class="tm">{e(w.get("startTime") or "")}{checkbox(w.get("completed"))}</span>'
        f'<span class="sp" style="color:{color}">{e(sport)}</span></div>'
        f'<div class="ct">{e(w.get("name"))}</div>'
        f'<div class="cm">{e(" · ".join(x for x in meta if x))}</div>'
        + (f'<div class="cr"></div><div class="ck">{e(kt)}</div>' if kt else "")
        + "</td>"
    )


def race_card(plan, w, rowspan=1):
    rs = plan.get("raceStrategy") or {}
    ev = rs.get("event") or {}
    pacing = rs.get("pacing") or {}
    goals = pacing.get("goals") or {}
    total = (pacing.get("projectedTotal") or {}).get("central")
    d = ev.get("distances") or {}
    dist = " · ".join(x for x in [
        f"{_num(d['swim'] / 1000)} km" if d.get("swim") is not None else "",
        f"{_num(d['bike'])} km" if d.get("bike") is not None else "",
        f"{_num(d['run'])} km" if d.get("run") is not None else ""] if x)

    stats = ""
    if total:
        stats += f'<div class="rs"><span>Proj.</span><b>{e(fmt_split("total", total)[:-3])}</b></div>'
    if goals.get("a"):
        stats += f'<div class="rs"><span>A goal</span><b>{e(re.sub(r"^sub-", "", str(goals["a"])))}</b></div>'

    lines = []
    swim, bike, run = (pacing.get(k) or {} for k in ("swim", "bike", "run"))
    if swim.get("target"):
        lines.append(("Swim", tidy(str(swim["target"]).replace("/100m", "/100 m"))))
    if bike.get("targetPower"):
        lines.append(("Bike", tidy(bike["targetPower"])))
    run_bits = [run.get("targetPace"), f"HR {run['targetHR']}" if run.get("targetHR") else None]
    if any(run_bits):
        lines.append(("Run", tidy(", ".join(b for b in run_bits if b))))
    during = (rs.get("nutrition") or {}).get("during") or {}
    if during.get("carbsPerHour"):
        fuel = f"{during['carbsPerHour']} g carbs/h"
        if during.get("fluidPerHour"):
            fuel += f", {tidy(during['fluidPerHour'])}/h"
        lines.append(("Fuel", fuel))
    kt = tidy(key_targets(w))
    if not lines and kt:
        lines.append(("Plan", kt))

    body = "".join(f'<div class="rl"><b>{e(k)}</b> {e(v)}</div>' for k, v in lines)
    return (
        f'<td class="card race" rowspan="{rowspan}"><div class="race-in">'
        f'<div class="ch"><span class="tm">{e(w.get("startTime") or "")}{checkbox(w.get("completed"))}</span>'
        f'<span class="sp" style="color:{SPORT_COLORS["race"]}">Race day</span></div>'
        f'<div class="ct big">{e(w.get("name") or ev.get("name"))}</div>'
        + (f'<div class="cm">{e(dist)}</div>' if dist else "")
        # Plain blocks, not a nested table: WeasyPrint mis-sizes the rows a
        # rowspanning cell covers when that cell contains a table.
        + (f'<div class="rstats">{stats}</div>' if stats else "")
        + (f'<div class="cr gold"></div>{body}' if body else "")
        + ('<div class="cm note">Full plan on the last page.</div>' if rs.get("event") else "")
        + "</div></td>"
    )


def race_card_height(card_html):
    """Conservative height in px of a race card's content (it is laid out
    out of flow, so the rows it spans must be made tall enough for it):
    each text block wrapped at ~18 characters a line, plus the fixed parts."""
    blocks = re.findall(r'<div class="(?:ct big|cm|cm note|rl)">(.*?)</div>', card_html, re.S)
    lines = sum(max(1, -(-len(re.sub(r"<[^>]+>", "", t)) // 18)) for t in blocks)
    stats = 52 if 'class="rstats"' in card_html else 0
    return 48 + stats + lines * 18


def notes_block(label="Notes", right="RPE ___"):
    lines = "<i></i>" * 30
    return (f'<div class="notes"><div class="nh"><span class="nr">{e(right)}</span>{e(label)}</div>'
            f'<div class="ruled">{lines}</div></div>')


def week_grid(plan, days, per_day, slots, slot_h, prefs):
    """Seven day columns as one table with a row per session slot, so every
    card in a row shares its height and the notes start level across the
    week. Rows have a minimum height and grow with their tallest card; the
    wrapper around the table clips the spare notes rules at the page floor."""
    head = "".join(f'<th class="dh"><span>{e(short_date(d["date"]))}</span>'
                   f'<b>{e(d.get("dayOfWeek", "")[:3])}</b></th>' for d in days)

    # cells[row][col] → html, or None where a rowspan above covers it
    cells = [[None] * len(days) for _ in range(slots)]
    if not any(per_day):
        cells[0][0] = f'<td class="slot rest" colspan="{len(days)}" rowspan="{slots}">No sessions this week</td>'
    else:
        for c, ws in enumerate(per_day):
            races = [w for w in ws if w.get("sport") == "race"]
            if races:
                cells[0][c] = race_card(plan, races[0], slots)
                need = race_card_height(cells[0][c])
                span = slots * slot_h + (slots - 1) * SLOT_GAP
                if need > span:
                    slot_h += (need - span) / slots
            elif not ws:
                cells[0][c] = f'<td class="slot rest" rowspan="{slots}">Rest day</td>'
            else:
                for r in range(slots):
                    cells[r][c] = session_card(ws[r], prefs) if r < len(ws) else '<td class="slot"></td>'

    rows = "".join(f'<tr style="height:{slot_h:.0f}px">' + "".join(x for x in row if x) + "</tr>"
                   for row in cells)
    notes = "".join(
        f'<td class="ncell">{notes_block("Result", "___:___") if any(w.get("sport") == "race" for w in ws) else notes_block()}</td>'
        for ws in per_day)
    return f'<table class="grid"><tr>{head}</tr>{rows}<tr>{notes}</tr></table>'


def week_page(plan, week, total_weeks):
    prefs = plan.get("preferences", {})
    phase = phase_for(plan, week)
    phase_name = week.get("phase") or (phase or {}).get("name", "")
    styles = phase_styles(plan)
    chip_style = styles.get(phase_name) or styles.get((phase or {}).get("name"), f"background:{PHASE_RAMP[0]}")

    mins = sport_minutes(week)
    stats = ""
    for s in SPORT_ORDER + sorted(k for k in mins if k not in SPORT_ORDER):
        if not mins.get(s):
            continue
        ws = [w for d in week.get("days", []) for w in sessions(d) if w.get("sport") == s]
        meters = sum(w.get("distanceMeters") or (w.get("distanceKm") or 0) * 1000 for w in ws)
        if s == "race":
            sub = "race day"
        elif meters and s in ("swim", "run"):
            sub = distance_label({"sport": s, "distanceMeters": meters}, prefs)
        else:
            sub = f"{len(ws)} session{'s' if len(ws) != 1 else ''}"
        stats += (f'<div class="st"><span class="sl" style="color:{SPORT_COLORS.get(s, MUTED)}">{e(s)}</span>'
                  f'<span class="sv">{hm(mins[s])}</span><span class="ss">{e(sub)}</span></div>')
    stats += f'<div class="st total"><span class="sl">Total</span><span class="sv">{hm(sum(mins.values()))}</span></div>'

    focus, side_label, side = split_side_note(week.get("focus", ""))
    rec = '<span class="rec-tag">Recovery week</span>' if week.get("isRecoveryWeek") else ""

    days = seven_days(week)
    per_day = [sessions(d) for d in days]
    slots = max([0 if any(w.get("sport") == "race" for w in s) else len(s) for s in per_day] + [2])
    slot_h = (SLOT_AREA - SLOT_GAP * (slots - 1)) / slots

    focus_lines = max(-(-len(focus) // FOCUS_CHARS), -(-len(side) // SIDE_CHARS) if side else 0, 1)
    focus_h = focus_lines * FOCUS_LINE
    # GRID_SLACK absorbs rounding; a grid even 1px too tall moves to a new page.
    grid_h = PAGE_H - HEAD_H - focus_h - 2 * BLOCK_GAP - GRID_SLACK

    grid = week_grid(plan, days, per_day, slots, slot_h, prefs)

    side_html = (f'<td class="fl side">{e(side_label)}</td>'
                 f'<td class="sidep"><p>{e(tidy(side))}</p></td>') if side else ""
    return f"""<section class="page week">
      <table class="wkhead" style="height:{HEAD_H}px"><tr>
        <td class="wkleft">
          <span class="chip" style="{chip_style}">{e(phase_name)}</span>
          <h2>Week {e(week['weekNumber'])} <span class="of">of {e(total_weeks)}</span></h2>
          <span class="dates">{e(week_range(week['startDate'], week['endDate']))}</span>
          {rec}
        </td>
        <td class="stats">{stats}</td>
      </tr></table>
      <table class="focus" style="height:{focus_h}px"><tr>
        <td class="fl">Focus</td><td class="fmain"><p>{e(tidy(focus))}</p></td>
        {side_html}
      </tr></table>
      <div class="gridwrap" style="height:{grid_h}px">{grid}</div>
    </section>"""


# ───────────────────────── race strategy page ─────────────────────────

def humanize(key):
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", str(key)).strip()
    return words[0].upper() + words[1:]


def _section_cell(text):
    m = re.match(r"^\s*(km\s*[\d.]+\s*[-–]\s*[\d.+]+)\s*(.*)$", str(text or ""), re.IGNORECASE)
    if not m:
        return f"<b>{e(tidy(text))}</b>"
    rest = m.group(2).strip()
    return f"<b>{e(tidy(m.group(1)))}</b>" + (f"<br>{e(rest[:1].upper() + rest[1:])}" if rest else "")


def _pace_table(title, sport, sub, heads, rows):
    if not rows:
        return ""
    color = SPORT_COLORS[sport]
    th = "".join(f"<th{R if h == 'km/h' else ''}>{e(h)}</th>" for h in heads)
    return (f'<div class="rblock"><div class="rhead" style="border-bottom-color:{color}">'
            f'<span>{e(sub)}</span><h3 style="color:{color}">{e(title)}</h3></div>'
            f'<table class="ptbl"><thead><tr>{th}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def race_page(plan):
    rs = plan.get("raceStrategy") or {}
    ev = rs.get("event")
    if not ev:
        return ""
    pacing = rs.get("pacing") or {}
    nutrition = rs.get("nutrition") or {}
    summary = rs.get("assessmentSummary") or {}
    swim, bike, run = (pacing.get(k) or {} for k in ("swim", "bike", "run"))
    trans = pacing.get("transitions") or {}
    total = (pacing.get("projectedTotal") or {}).get("central")

    stats = ""
    for name, obj in (("swim", swim), ("bike", bike), ("run", run)):
        if obj.get("projectedSplit"):
            stats += (f'<div class="st"><span class="sl" style="color:{SPORT_COLORS[name]}">{name}</span>'
                      f'<span class="sv">{e(fmt_split(name, obj["projectedSplit"]))}</span></div>')
    if total:
        stats += f'<div class="st total"><span class="sl">Central</span><span class="sv">{e(fmt_split("total", total)[:-3])}</span></div>'

    sub = [full_date(ev["date"])] if ev.get("date") else []
    gain = re.search(r"(\d[\d,]*)\s*m gain", str(ev.get("profile") or ""))
    if gain:
        sub.append(f"{gain.group(1)} m bike gain")

    bike_rows = [
        f"<tr><td>{_section_cell(s.get('section'))}</td><td><b>{e(tidy(s.get('power')))}</b></td>"
        f"<td class='r'>{e(tidy(strip_kmh(s.get('speed'))))}</td>"
        f"<td class='n'>{e(tidy(s.get('note')))}</td></tr>"
        for s in bike.get("bySection", [])
    ]
    bike_sub = " · ".join(tidy(x) for x in [bike.get("targetPower"),
                                            f"HR {bike['targetHR']}" if bike.get("targetHR") else None] if x)
    run_rows = [
        f"<tr><td>{_section_cell(s.get('section'))}</td><td><b>{e(tidy(s.get('pace')))}</b></td>"
        f"<td class='n'>{e(tidy(s.get('note')))}</td></tr>"
        for s in run.get("bySection", [])
    ]
    run_sub = " · ".join(tidy(x) for x in [run.get("targetPace"),
                                           f"HR {run['targetHR']}" if run.get("targetHR") else None] if x)

    left = (_pace_table("Bike · pacing by section", "bike", bike_sub, ["Section", "Power", "km/h", "Note"], bike_rows)
            + _pace_table("Run · pacing by section", "run", run_sub, ["Section", "Pace", "Note"], run_rows))

    def side(title, color, sub, body):
        return (f'<div class="rblock"><div class="rhead" style="border-bottom-color:{color}">'
                f'<span>{e(sub)}</span><h3 style="color:{color}">{e(title)}</h3></div>{body}</div>')

    right = ""
    if swim:
        right += side("Swim", SPORT_COLORS["swim"], tidy(str(swim.get("target") or "").replace("/100m", "/100 m")),
                      f"<p>{e(tidy(swim.get('notes')))}</p>" if swim.get("notes") else "")
    if trans:
        t_sub, t_notes = [], []
        for k in ("t1", "t2"):
            if trans.get(k):
                time, _, note = str(trans[k]).partition(" ")
                t_sub.append(f"{k.upper()} {fmt_split(k, time)}")
                note = note.strip(" -–")
                if note:
                    t_notes.append(f"<b>{k.upper()}</b> {e(note[:1].upper() + note[1:])}.")
        right += side("Transitions", INK, " · ".join(t_sub),
                      f"<p>{' '.join(t_notes)}</p>" if t_notes else "")
    if nutrition:
        during = nutrition.get("during") or {}
        n_sub = " · ".join(x for x in [f"{during['carbsPerHour']} g carbs/h" if during.get("carbsPerHour") else "",
                                       f"{tidy(during['fluidPerHour'])}/h" if during.get("fluidPerHour") else ""] if x)
        body = ""
        if nutrition.get("preRace"):
            body += f"<p><b>Pre-race.</b> {e(tidy(nutrition['preRace']))}</p>"
        if during.get("products"):
            body += f"<p><b>During.</b> {e('; '.join(during['products']))}.</p>"
        if nutrition.get("notes"):
            body += f"<p><b>Notes.</b> {e(tidy(nutrition['notes']))}</p>"
        right += side("Nutrition", SPORT_COLORS["race"], n_sub, body)

    risks = "".join(f"<li>{e(tidy(r))}</li>" for r in rs.get("keyRisks", []))
    risks_html = (f'<div class="rblock"><div class="rhead"><h3>Key risks</h3></div><ol>{risks}</ol></div>'
                  if risks else "")
    assess = ""
    if summary:
        assess = ('<div class="rblock"><div class="rhead"><h3>Assessment</h3></div><dl>'
                  + "".join(f"<dt>{e(humanize(k))}</dt><dd>{e(tidy(v))}</dd>" for k, v in summary.items())
                  + "</dl></div>")

    left += risks_html
    right += assess
    # A table row that outgrows the page splits onto the next one (each block
    # kept whole), so a long strategy flows rather than being clipped.
    if left and right:
        body = f'<table class="rgrid"><tr><td class="rleft">{left}</td><td class="rright">{right}</td></tr></table>'
    else:
        body = left or right

    return f"""<section class="race-page">
      <table class="wkhead" style="height:{HEAD_H}px"><tr>
        <td class="wkleft">
          <span class="chip" style="background:#ffffff;border:1.5px solid {INK}">Race</span>
          <h2>Race strategy</h2>
          <span class="dates">{e(" · ".join(sub))}</span>
        </td>
        <td class="stats">{stats}</td>
      </tr></table>
      {body}
    </section>"""


# ──────────────────────────────── css ────────────────────────────────

def font_css():
    out = ""
    for family, weight, fname in FONT_FILES:
        path = FONT_DIR / fname
        if path.exists():
            out += (f'@font-face {{ font-family: "{family}"; font-weight: {weight}; '
                    f'font-style: normal; src: url("{path.as_uri()}"); }}\n')
    return out


def _css_string(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def page_css(plan):
    meta = plan.get("meta") or {}
    left = " · ".join(x for x in [meta.get("athlete"), meta.get("event"), meta.get("generatedBy")] if x)
    return f"""
@page {{
  size: Letter landscape;
  margin: 0.4in 0.4in 0.55in;
  @bottom-left {{ content: {_css_string(left)}; font: 500 9pt Barlow, sans-serif; color: {MUTED};
                  vertical-align: top; padding-top: 6pt; }}
  @bottom-right {{ content: counter(page) " / " counter(pages); font: 500 9pt Barlow, sans-serif;
                   color: {MUTED}; vertical-align: top; padding-top: 6pt; }}
}}
"""


CSS = f"""
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  font-family: Barlow, "Liberation Sans", Arial, sans-serif;
  color: {INK}; font-size: 9.5pt; line-height: 1.3;
  font-variant-numeric: tabular-nums;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}}
h1, h2, h3 {{ font-family: "Barlow Condensed", Barlow, sans-serif; margin: 0; }}
p {{ margin: 0; }}
table {{ border-collapse: collapse; }}
td {{ padding: 0; vertical-align: top; }}
/* Block, table and float layout only: WeasyPrint's nested flexbox is an
   order of magnitude slower and paginates unpredictably. */
.page {{ height: {PAGE_H}px; overflow: hidden; position: relative;
  break-after: page; page-break-after: always; }}
.page:last-child {{ break-after: auto; page-break-after: auto; }}

/* ── overview ── */
.cover-head {{ display: table; width: 100%; border-bottom: 2px solid {INK}; margin-bottom: 14px; }}
.cover-head > div {{ display: table-cell; vertical-align: bottom; padding-bottom: 12px; }}
.cover-head > .goal-boxes {{ text-align: right; white-space: nowrap; }}
.kicker {{ font: 600 9pt "Barlow Condensed", sans-serif; letter-spacing: 0.14em;
  text-transform: uppercase; color: {MUTED}; }}
h1 {{ font-size: 32pt; line-height: 1.05; font-weight: 700; margin: 3px 0 4px; }}
.cover-sub {{ font-size: 10.5pt; font-weight: 500; color: {INK_3}; }}
.goal {{ display: inline-block; text-align: left; padding: 5px 11px; background: #e9ebef;
  border-radius: 4px; min-width: 70px; margin-left: 6px; vertical-align: bottom; }}
.goal.strong {{ background: #ffffff; border: 2px solid {INK}; }}
.goal.outline {{ background: #ffffff; border: 1px solid {RULE}; }}
.goal .gl {{ display: block; font: 600 8.5pt "Barlow Condensed", sans-serif; letter-spacing: 0.1em;
  text-transform: uppercase; color: {MUTED}; }}
.goal .gv {{ display: block; font: 700 17pt/1.1 "Barlow Condensed", sans-serif; }}

.sec-head {{ margin-bottom: 6px; }}
.sec-head h2 {{ font-size: 14pt; font-weight: 700; }}
.legend {{ float: right; font-size: 9pt; font-weight: 500; color: {INK_3}; padding-top: 4px; }}
.lg {{ white-space: nowrap; margin-left: 10px; }}
.sw {{ display: inline-block; width: 8pt; height: 8pt; border-radius: 2px; margin-right: 4px;
  vertical-align: -1pt; }}
.vol {{ width: 100%; table-layout: fixed; border-collapse: separate; border-spacing: 4px 0; }}
.vol .phase {{ padding: 4px 6px; border-radius: 3px; }}
.vol .phase b {{ display: block; font: 700 10.5pt/1.15 "Barlow Condensed", sans-serif; }}
.vol .phase span {{ display: block; font-size: 8.5pt; font-weight: 500; color: {INK_2}; }}
.vol .bars td {{ vertical-align: bottom; border-bottom: 1.5px solid {INK}; padding-top: 10px; }}
.vol .bt {{ font-size: 8.5pt; font-weight: 600; text-align: center; padding-bottom: 2px; }}
.vol .stack {{ margin: 0 16%; }}
.vol .stack div {{ border-top: 1px solid #ffffff; }}
.vol .wl {{ text-align: center; padding-top: 4px; }}
.vol .wl b {{ display: block; font: 600 9.5pt "Barlow Condensed", sans-serif; }}
.vol .rec {{ display: block; font: 700 8.5pt "Barlow Condensed", sans-serif; letter-spacing: 0.06em;
  color: {SPORT_COLORS['strength']}; }}
.caption {{ font-size: 8.5pt; color: {MUTED}; margin-top: 4px; }}

.zones {{ position: absolute; left: 0; right: 0; bottom: 0; }}
.zrow {{ display: table; width: 100%; table-layout: fixed; }}
.zcol {{ display: table-cell; padding-right: 20px; }}
.zcol:last-child {{ padding-right: 0; }}
.zhead {{ border-bottom: 2px solid {INK}; padding-bottom: 3px; margin-bottom: 2px; }}
.zt {{ font: 700 12pt "Barlow Condensed", sans-serif; white-space: nowrap; }}
.zv {{ float: right; font-size: 8.5pt; font-weight: 500; color: {INK_3}; padding-top: 4px; }}
.ztbl {{ width: 100%; font-size: 9pt; font-weight: 500; }}
.ztbl th {{ text-align: left; font-weight: 600; color: {MUTED}; padding: 2px 0; }}
.ztbl td {{ padding: 1.5px 0; white-space: nowrap; }}
.ztbl .r {{ text-align: right; padding-left: 6px; }}
.ztbl .total td {{ font-weight: 700; border-top: 1px solid {INK}; }}

/* ── week pages ── */
.wkhead {{ width: 100%; border-bottom: 2px solid {INK}; margin-bottom: {BLOCK_GAP}px; }}
.wkhead td {{ vertical-align: bottom; padding-bottom: 7px; }}
.wkleft {{ white-space: nowrap; }}
.chip {{ display: inline-block; padding: 3px 7px; border-radius: 3px; vertical-align: 4px; margin-right: 6px;
  font: 700 9.5pt "Barlow Condensed", sans-serif; letter-spacing: 0.08em; text-transform: uppercase; }}
.wkhead h2 {{ display: inline; font-size: 24pt; line-height: 1; font-weight: 700; }}
.wkhead h2 .of {{ font-weight: 500; color: {MUTED}; }}
.dates {{ font-size: 10.5pt; font-weight: 500; color: {INK_3}; margin-left: 8px; }}
.rec-tag {{ display: inline-block; padding: 2px 7px; border: 1.5px solid {SPORT_COLORS['strength']};
  border-radius: 3px; color: {SPORT_COLORS['strength']}; margin-left: 8px; vertical-align: 2px;
  font: 700 9pt "Barlow Condensed", sans-serif; letter-spacing: 0.08em; text-transform: uppercase; }}
.stats {{ text-align: right; white-space: nowrap; }}
.st {{ display: inline-block; text-align: right; margin-left: 13px; vertical-align: bottom; }}
.st span {{ display: block; }}
.st .sl {{ font: 600 8.5pt "Barlow Condensed", sans-serif; letter-spacing: 0.08em; text-transform: uppercase; }}
.st .sv {{ font: 600 13.5pt/1.1 "Barlow Condensed", sans-serif; }}
.st .ss {{ font-size: 8.5pt; font-weight: 500; color: {MUTED}; }}
.st.total {{ padding-left: 13px; border-left: 1px solid {RULE}; }}
.st.total .sl {{ color: {MUTED}; }}
.st.total .sv {{ font-size: 22pt; font-weight: 700; }}

.focus {{ width: 100%; margin-bottom: {BLOCK_GAP}px; }}
.focus {{ table-layout: fixed; text-align: left; }}
.focus .fl {{ width: 44px; white-space: nowrap; font: 700 8.5pt/{FOCUS_LINE}px "Barlow Condensed", sans-serif;
  letter-spacing: 0.1em; text-transform: uppercase; color: {MUTED}; }}
.focus .fl.side {{ width: 82px; padding-left: 12px; border-left: 1px solid #d5d8de; }}
.focus td {{ text-align: left; }}
.focus p {{ text-align: left; font-size: 9.5pt; line-height: {FOCUS_LINE}px; color: {INK_2}; }}
.fmain {{ padding-right: 20px; }}
.sidep {{ width: 1.75in; }}

.gridwrap {{ overflow: hidden; }}
.grid {{ table-layout: fixed; border-collapse: separate; border-spacing: 6px;
  margin: -6px -6px 0; width: calc(100% + 12px); }}
.grid td, .grid th {{ vertical-align: top; }}
.dh {{ text-align: left; font-weight: normal; border-bottom: 2px solid {INK}; padding: 0 0 2px; }}
.dh b {{ font: 700 12pt "Barlow Condensed", sans-serif; }}
.dh span {{ float: right; font-size: 9pt; font-weight: 500; color: {MUTED}; padding-top: 3px; }}
.card {{ background: {PANEL}; border-top: 3px solid {INK}; border-radius: 2px; padding: 5px 7px 7px;
  overflow-wrap: break-word; }}
.sp {{ font: 700 9pt "Barlow Condensed", sans-serif; letter-spacing: 0.1em; text-transform: uppercase; }}
.tm {{ float: right; font-size: 9pt; font-weight: 500; color: {MUTED}; white-space: nowrap; }}
.cb {{ display: inline-block; width: 8pt; height: 8pt; border: 1.2pt solid {MUTED}; border-radius: 2px;
  margin-left: 4px; vertical-align: -1pt; }}
.cb.done {{ background: {MUTED}; }}
.ct {{ font: 700 11pt/1.15 "Barlow Condensed", sans-serif; margin-top: 3px; }}
.ct.big {{ font-size: 13.5pt; }}
.cm {{ font-size: 9pt; font-weight: 600; color: {INK_3}; margin-top: 2px; }}
.cm.note {{ font-weight: 500; color: {MUTED}; margin-top: 6px; }}
.cr {{ height: 1px; background: #d5d8de; margin: 4px 0; }}
.cr.gold {{ background: #e0cf9e; margin: 6px 0; }}
.ck {{ font-size: 9pt; line-height: 12pt; color: {INK_2}; }}
.slot {{ border: 1px dashed #d5d8de; border-radius: 2px; }}
.slot.rest {{ vertical-align: middle; text-align: center;
  font: 600 10pt "Barlow Condensed", sans-serif; letter-spacing: 0.08em; text-transform: uppercase;
  color: {MUTED}; }}
.card.race {{ background: #fbf4e0; border: 1.2pt solid {SPORT_COLORS['race']};
  border-top: 3pt solid {SPORT_COLORS['race']}; padding: 0; position: relative; }}
/* Out of flow on purpose: WeasyPrint grows the last spanned row to a
   rowspanning cell's full content height, which pushed the notes off the
   page. The card fills the rows it spans; week_page makes sure they are
   tall enough for it. */
.race-in {{ position: absolute; top: 7px; left: 8px; right: 8px; }}
.rstats {{ margin-top: 6px; white-space: nowrap; }}
.rs {{ display: inline-block; width: 48%; background: #ffffff; border: 1px solid #e0cf9e; border-radius: 2px;
  padding: 3px 5px; white-space: normal; }}
.rs + .rs {{ margin-left: 4%; }}
.rs span {{ display: block; font: 600 8.5pt "Barlow Condensed", sans-serif; letter-spacing: 0.06em;
  text-transform: uppercase; color: {MUTED}; }}
.rs b {{ font: 700 12.5pt/1.1 "Barlow Condensed", sans-serif; }}
.rl {{ font-size: 9pt; line-height: 12pt; color: {INK_2}; margin-bottom: 4px; }}
.rl b {{ color: {INK}; }}
/* Fourteen rules, more than any column shows: the day wrapper clips the
   spare ones, so long cards shorten the notes instead of the page. */
.ncell {{ padding-top: 2px; }}
.nh {{ font: 700 8.5pt "Barlow Condensed", sans-serif; letter-spacing: 0.1em;
  text-transform: uppercase; color: {MUTED}; }}
.nr {{ float: right; }}
.ruled i {{ display: block; height: 17px; border-bottom: 0.75pt solid {RULE}; }}

/* ── race strategy ── */
.race-page {{ break-before: page; page-break-before: always; }}
.race-page .wkhead {{ margin-bottom: 10px; }}
.rgrid {{ width: 100%; table-layout: fixed; }}
.rright {{ width: 3.3in; padding-left: 26px; }}
.rblock {{ margin-bottom: 9px; break-inside: avoid; page-break-inside: avoid; }}
.rhead {{ border-bottom: 2px solid {INK}; padding-bottom: 3px; margin-bottom: 4px; }}
.rhead h3 {{ font-size: 13pt; font-weight: 700; white-space: nowrap; }}
.rhead span {{ float: right; font-size: 8.5pt; font-weight: 500; color: {INK_3}; padding-top: 5px; }}
.ptbl {{ width: 100%; font-size: 9pt; line-height: 11.5pt; }}
.ptbl th {{ text-align: left; font-weight: 600; color: {MUTED}; padding: 3px 8px 3px 0;
  border-bottom: 1px solid {RULE}; }}
.ptbl td {{ padding: 2px 8px 2px 0; border-bottom: 1px solid {RULE_LIGHT}; }}
.ptbl td:first-child {{ width: 1.3in; }}
.ptbl .r {{ text-align: right; white-space: nowrap; }}
.ptbl .n {{ color: {INK_3}; }}
.ptbl tr {{ break-inside: avoid; }}
.rblock p {{ font-size: 9pt; line-height: 11.5pt; color: {INK_2}; margin-bottom: 4px; }}
.rblock ol {{ margin: 0; padding-left: 16px; font-size: 9pt; line-height: 11.5pt; color: {INK_2}; }}
.rblock li {{ margin-bottom: 2px; }}
.rblock dl {{ margin: 0; font-size: 9pt; line-height: 11.5pt; color: {INK_2}; }}
.rblock dt {{ font: 700 8.5pt "Barlow Condensed", sans-serif; letter-spacing: 0.08em;
  text-transform: uppercase; color: {MUTED}; }}
.rblock dd {{ margin: 0 0 4px; }}

* {{ hyphens: none; }}
"""

# Kept for callers that append engine-specific overrides; the stylesheet
# above is written for WeasyPrint directly, so there is nothing to add.
WEASYPRINT_CSS = ""


def build_html(plan):
    weeks = sorted(plan.get("weeks", []), key=lambda w: w["weekNumber"])
    total_weeks = (plan.get("meta") or {}).get("totalWeeks") or len(weeks)
    pages = [cover_page(plan)]
    pages += [week_page(plan, w, total_weeks) for w in weeks]
    race = race_page(plan)
    if race:
        pages.append(race)
    title = f"{plan['meta'].get('event', 'Training plan')} — printable"
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>{e(title)}</title>"
            f"<style>{font_css()}{page_css(plan)}{CSS}</style></head><body>{''.join(pages)}</body></html>")


# ───────────────────────── pdf conversion ─────────────────────────

def via_weasyprint(html_path, pdf_path):
    try:
        from weasyprint import HTML
        from weasyprint.text.fonts import FontConfiguration
    except (ImportError, OSError):
        # OSError: the package is installed but pango/gobject aren't (Windows
        # without GTK) — cffi raises at import, not ImportError.
        return False

    html_text = weasyprint_html(html_path.read_text(encoding="utf-8"))
    HTML(string=html_text, base_url=str(html_path.parent)).write_pdf(
        str(pdf_path), font_config=FontConfiguration()
    )
    return pdf_path.exists()


def weasyprint_html(html_text):
    """Append any WeasyPrint override sheet as the last author stylesheet."""
    if not WEASYPRINT_CSS:
        return html_text
    style = f"<style>{WEASYPRINT_CSS}</style>"
    if "</head>" in html_text:
        return html_text.replace("</head>", style + "</head>", 1)
    return html_text + style


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("-o", "--output", help="output PDF path (default: alongside the JSON)")
    ap.add_argument("--keep-html", action="store_true", help="also write the intermediate print HTML")
    args = ap.parse_args()

    plan_path = pathlib.Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    pdf_path = pathlib.Path(args.output) if args.output else plan_path.with_suffix(".pdf")

    html_out = (pdf_path.with_suffix(".print.html") if args.keep_html
                else pathlib.Path(tempfile.mkdtemp()) / "plan-print.html")
    html_out.write_text(build_html(plan), encoding="utf-8")

    if not via_weasyprint(html_out, pdf_path):
        keep = pdf_path.with_suffix(".print.html")
        keep.write_text(build_html(plan), encoding="utf-8")
        print(f"WeasyPrint is unavailable (needs pango/harfbuzz — on Windows, run this "
              f"under WSL).\nPrint HTML written to {keep} — open it and print to PDF "
              f"(landscape Letter, background graphics on).", file=sys.stderr)
        sys.exit(2)

    print(f"PDF: {pdf_path}  ({pdf_path.stat().st_size // 1024}KB)")
    if args.keep_html:
        print(f"print HTML: {html_out}")


if __name__ == "__main__":
    main()
