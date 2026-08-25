#!/usr/bin/env python3
"""Build the printable at-a-glance PDF from a training plan JSON.

    python3 tools/build_pdf.py plan.json -o plan.pdf

Renders through WeasyPrint, the only engine available in the container, so
what you get here is what production produces. On Windows that means running
under WSL — WeasyPrint needs pango/harfbuzz.

Letter landscape, 0.4in margins:

  page 1        cover — phases, zones, projected splits, legend
  pages 2..n+1  one page per week, seven day columns, notes/actuals ruled
                block at the foot of each column
  last page     race strategy & plan notes (only when the plan has a race)

This PDF is a wall chart, not the plan. Full session structures stay in the
JSON and the HTML viewer; each card here carries only sport, duration,
distance, zone, title and key targets — enough to know what today is without
reading a paragraph of it from across the room.

A day column that overflows is clipped rather than spilling onto the next
page. Fix it by shortening that workout's `keyTargets` in the JSON — the
alternative, shrinking the type, makes the whole chart unreadable from the
wall it is pinned to.
"""

import argparse
import html
import json
import pathlib
import re
import sys
import tempfile

SPORT_COLORS = {
    "swim": "#2e8fa3", "bike": "#4d8a46", "run": "#c0742f",
    "strength": "#6c5fc0", "brick": "#a8577d", "race": "#b08a20", "rest": "#8a8f9c",
}


def e(v):
    return html.escape("" if v is None else str(v))


def key_targets(w):
    """Prefer an explicit field; fall back to the KEY TARGETS block many
    plans embed in humanReadable."""
    if w.get("keyTargets"):
        return str(w["keyTargets"]).strip()
    m = re.search(r"KEY TARGETS?:\s*(.+?)(?:\n\s*\n|$)", w.get("humanReadable", "") or "",
                  re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def fmt_hours(h):
    if h is None:
        return ""
    return f"{h:g}h"


def fmt_duration(minutes):
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}" if h and m else (f"{h}h" if h else f"{m}min")


def distance_label(w, prefs):
    if w.get("distanceKm") is not None:
        unit = "mi" if prefs.get("bike") == "miles" else "km"
        return f"{w['distanceKm']:g}{unit}"
    if w.get("distanceMeters") is not None:
        unit = "yd" if prefs.get("swim") == "yards" else "m"
        return f"{w['distanceMeters']:g}{unit}"
    return ""


def date_range(a, b):
    from datetime import date

    def parse(s):
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)

    da, db = parse(a), parse(b)
    if da.month == db.month:
        return f"{da.strftime('%b')} {da.day}–{db.day}, {db.year}"
    return f"{da.strftime('%b')} {da.day} – {db.strftime('%b')} {db.day}, {db.year}"


def full_date(s):
    from datetime import date
    y, m, d = (int(x) for x in s.split("-"))
    dt = date(y, m, d)
    return f"{dt.strftime('%A')} {dt.strftime('%B')} {dt.day}, {dt.year}"


def short_date(s):
    from datetime import date
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d).strftime("%b %-d") if sys.platform != "win32" else date(y, m, d).strftime("%b %d")


# ────────────────────────────── cover ──────────────────────────────

def cover_page(plan):
    meta = plan["meta"]
    race = (plan.get("raceStrategy") or {}).get("event") if plan.get("raceStrategy") else None
    pacing = ((plan.get("raceStrategy") or {}).get("pacing") or {})
    goals = pacing.get("goals") or {}

    dist = ""
    if race and race.get("distances"):
        d = race["distances"]
        parts = []
        if d.get("swim") is not None:
            parts.append(f"{d['swim']:g}m swim")
        if d.get("bike") is not None:
            parts.append(f"{d['bike']:g}km bike")
        if d.get("run") is not None:
            parts.append(f"{d['run']:g}km run")
        dist = " · ".join(parts)

    badges = ""
    if goals.get("a"):
        badges += f'<span class="goal goal-a">A goal · {e(goals["a"])}</span>'
    if goals.get("b"):
        badges += f'<span class="goal goal-b">B goal · {e(goals["b"])}</span>'

    line1 = " · ".join(x for x in [full_date(race["date"]) if race else "", dist] if x)
    head = f"""
    <div class="cover-head">
      <div>
        <div class="kicker">{e(meta.get('generatedBy', 'Claude Coach'))} · {e(meta.get('athlete', ''))}</div>
        <h1>{e(meta.get('event'))}</h1>
        {f'<div class="cover-sub">{e(line1)}</div>' if line1 else ''}
        <div class="cover-sub">{e(meta.get('totalWeeks'))} weeks · {e(meta.get('planStartDate'))} → {e(meta.get('planEndDate'))}</div>
      </div>
      <div class="goals">{badges}</div>
    </div>"""

    rows = "".join(
        f"<tr><td class='phase-cell'><span class='swatch' style='background:{phase_color(p['name'])}'></span>{e(p['name'])}</td>"
        f"<td>{e(p['startWeek'])}–{e(p['endWeek'])}</td>"
        f"<td>{e(p.get('startDate', ''))} → {e(p.get('endDate', ''))}</td>"
        f"<td>{e(p.get('weeklyHoursRange', {}).get('low'))}–{e(p.get('weeklyHoursRange', {}).get('high'))}h</td>"
        f"<td class='focus'>{e(first_sentence(p.get('focus', '')))}</td></tr>"
        for p in plan.get("phases", [])
    )
    phases = f"""
    <div class="block">
      <h2>Phases</h2>
      <table class="tbl">
        <thead><tr><th>Phase</th><th>Weeks</th><th>Dates</th><th>Hours</th><th>Focus</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""

    zones = f'<div class="block"><h2>Zones</h2><div class="zone-cols">{zone_tables(plan)}</div></div>'

    splits = splits_block(plan)
    legend = "".join(
        f'<span class="legend-item"><span class="swatch" style="background:{c}"></span>{s}</span>'
        for s, c in SPORT_COLORS.items() if s != "rest"
    )

    return f"""<section class="page cover {cover_density(plan)}">
      {head}{phases}{zones}
      <div class="cover-foot">{splits}<div class="legend">{legend}</div></div>
    </section>"""


def cover_density(plan):
    """The cover carries every phase and every zone row, so its height is
    data-driven. Rather than let a nine-phase plan spill, tighten the
    leading before anything is lost."""
    z = plan.get("zones") or {}
    ov = plan.get("overrides") or {}
    zone_rows = 0
    bike = z.get("bike") or {}
    if bike.get("power") or ov.get("ftp"):
        zone_rows = max(zone_rows, len(BIKE_POWER_PCT_TABLE))
    run = z.get("run") or {}
    if run.get("hr") or run.get("pace") or ov.get("runLthr") or ov.get("thresholdPace"):
        zone_rows = max(zone_rows, len(HR_PCT_TABLE))
    swim = z.get("swim") or {}
    if swim.get("zones") or ov.get("cssSeconds"):
        zone_rows = max(zone_rows, len(SWIM_PACE_OFFSET_TABLE))
    load = len(plan.get("phases", [])) + zone_rows
    if load > 15:
        return "tighter"
    if load > 11:
        return "tight"
    return ""


def first_sentence(text):
    text = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


def phase_color(name):
    palette = {
        "Prep": "#4a7a96", "Base": "#3f9a8c", "Base 1": "#3f8fa8", "Base 2": "#3f9a8c",
        "Base 3": "#4d8a46", "Build": "#8a8320", "Build 1": "#8a8320", "Build 2": "#b07d2a",
        "Peak": "#b0552f", "Taper": "#6c5fc0", "Race": "#b03050",
    }
    return palette.get(name, "#6a6f7c")


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
    """A threshold/CSS pace from the plan is sometimes a tested range
    ('4:06-4:20/km') rather than one number. Average it to a single
    working pace, same as the HTML viewer does."""
    secs = [int(m.group(1)) * 60 + int(m.group(2)) for m in re.finditer(r"(\d+):(\d+)", str(s or ""))]
    return _fmt_mmss(sum(secs) / len(secs)) if secs else ""


def _parse_mmss(s):
    m = re.search(r"(\d+):(\d+)", str(s or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _range_str(lo, hi, fmt=str, suffix=""):
    vals = sorted([lo, hi])
    a, b = fmt(vals[0]), fmt(vals[1])
    return (a if a == b else f"{a}-{b}") + suffix


def zone_status(sport, zones_validated, css_status=None):
    v = (zones_validated or {}).get(sport)
    if v and v.get("validated"):
        return "Validated"
    if sport == "swim" and css_status:
        return css_status
    return "Estimated"


def zone_tables(plan):
    """Zones are computed live from FTP/LTHR/threshold-pace (using the
    standard formula tables above) instead of reading the zones arrays
    baked into the plan JSON at generation time. Those arrays are
    sometimes partial (a plan that calls run 'pace-anchored' can leave
    hr.zones empty) and never reflect overrides made later in the HTML
    viewer — this keeps the PDF in sync with whatever the browser is
    currently showing. `overrides` (present when this is called from the
    server's PDF endpoint, which sends the browser's live state) take
    priority; the plan's original static values are the fallback for a
    bare `python3 build_pdf.py plan.json` run with no overrides."""
    z = plan.get("zones") or {}
    ov = plan.get("overrides") or {}
    zv = plan.get("zonesValidated") or {}
    out = []

    bike = z.get("bike") or {}
    ftp = ov.get("ftp", (bike.get("power") or {}).get("ftp"))
    bike_lthr = ov.get("bikeLthr", (bike.get("hr") or {}).get("lthr"))
    if bike.get("power") or ftp:
        rows = ""
        for zz, hz in zip(BIKE_POWER_PCT_TABLE, HR_PCT_TABLE):
            watts = _range_str(_pct(ftp, zz["low"]), _pct(ftp, zz["high"]), str, "W") if ftp else "—"
            hr = _range_str(_pct(bike_lthr, hz["low"]), _pct(bike_lthr, hz["high"])) if bike_lthr else "—"
            rows += (f"<tr><td>{e(zz['zone'])} {e(zz['name'])}</td>"
                     f"<td>{e(watts)}</td><td>{e(hr)}</td></tr>")
        header = f"FTP {ftp}W" if ftp else ""
        header += (" · " if header else "") + (f"LTHR {bike_lthr}" if bike_lthr else "")
        header += (" · " if header else "") + zone_status("bike", zv)
        out.append(zone_col("Bike power", header, ["Zone", "Watts", "HR"], rows))

    run = z.get("run") or {}
    run_lthr = ov.get("runLthr", (run.get("hr") or {}).get("lthr"))
    threshold_pace = ov.get("thresholdPace") or _resolve_single_pace((run.get("pace") or {}).get("thresholdPace", ""))
    if run.get("hr") or run.get("pace") or run_lthr or threshold_pace:
        threshold_sec = _parse_mmss(threshold_pace)
        rows = ""
        for hz, pz in zip(HR_PCT_TABLE, RUN_PACE_OFFSET_TABLE):
            hr = _range_str(_pct(run_lthr, hz["low"]), _pct(run_lthr, hz["high"])) if run_lthr else "—"
            pace = (_range_str(threshold_sec + pz["low"], threshold_sec + pz["high"], _fmt_mmss, "/km")
                    if threshold_sec is not None else "—")
            rows += (f"<tr><td>{e(hz['zone'])} {e(hz['name'])}</td>"
                     f"<td>{e(hr)}</td><td>{e(pace)}</td></tr>")
        head = []
        if run_lthr:
            head.append(f"LTHR {run_lthr}")
        if threshold_pace:
            head.append(f"T-pace {threshold_pace}/km")
        head.append(zone_status("run", zv))
        out.append(zone_col("Run", " · ".join(head), ["Zone", "HR", "Pace"], rows))

    swim = z.get("swim") or {}
    css_seconds = ov.get("cssSeconds", swim.get("cssSeconds"))
    css_label = ov.get("cssLabel", swim.get("css", ""))
    if swim.get("zones") or css_seconds:
        rows = ""
        for zz in SWIM_PACE_OFFSET_TABLE:
            pace = (_range_str(css_seconds + zz["low"], css_seconds + zz["high"], _fmt_mmss, "/100m")
                    if css_seconds is not None else "—")
            rows += f"<tr><td>{e(zz['zone'])} {e(zz['name'])}</td><td>{e(pace)}</td></tr>"
        head = f"CSS {css_label}" if css_label else "CSS"
        head += " · " + zone_status("swim", zv, swim.get("cssStatus"))
        out.append(zone_col("Swim", head, ["Zone", "Pace /100m"], rows))

    return "".join(out)


def zone_col(title, header_value, heads, rows):
    return (f"<div class='zone-col'><div class='zone-title'>{e(title)}"
            f"<span class='zone-value'>{e(header_value)}</span></div>"
            f"<table class='tbl tight'><thead><tr><th>" + "</th><th>".join(map(e, heads))
            + f"</th></tr></thead><tbody>{rows}</tbody></table></div>")


def splits_block(plan):
    """Projected splits table only — no narrative verdict, no C goal."""
    rs = plan.get("raceStrategy") or {}
    if not rs.get("event"):
        return ""
    pacing = rs.get("pacing") or {}
    rows = pacing.get("projectedSplits")

    if rows:
        body = ""
        for r in rows:
            cls = " class='total'" if str(r.get("leg", "")).lower() == "total" else ""
            body += (f"<tr{cls}><td>{e(r.get('leg'))}</td><td>{e(r.get('conservative', '—'))}</td>"
                     f"<td>{e(r.get('central', '—'))}</td><td>{e(r.get('strong', '—'))}</td></tr>")
        return (f"<div class='block splits'><h2>Projected splits</h2>"
                f"<table class='tbl tight'><thead><tr><th>Leg</th><th>Conservative</th><th>Central</th>"
                f"<th>Strong</th></tr></thead><tbody>{body}</tbody></table></div>")

    # No three-way matrix in the plan: show the single central estimate per leg
    # and put the spread on the total row, rather than implying a conservative
    # column the data does not contain.
    total = pacing.get("projectedTotal") or {}
    trans = pacing.get("transitions") or {}
    legs = [("Swim", (pacing.get("swim") or {}).get("projectedSplit")),
            ("T1", (trans.get("t1") or "").split(" ")[0] or None),
            ("Bike", (pacing.get("bike") or {}).get("projectedSplit")),
            ("T2", (trans.get("t2") or "").split(" ")[0] or None),
            ("Run", (pacing.get("run") or {}).get("projectedSplit"))]
    body = "".join(f"<tr><td>{e(n)}</td><td>{e(v)}</td></tr>" for n, v in legs if v)
    spread = " / ".join(e(total[k]) for k in ("conservative", "central", "strong") if total.get(k))
    if spread:
        body += f"<tr class='total'><td>Total</td><td>{spread}</td></tr>"
    caption = "<div class='sub'>Total shown conservative / central / strong</div>" if spread else ""
    return (f"<div class='block splits'><h2>Projected splits</h2>{caption}"
            f"<table class='tbl tight'><thead><tr><th>Leg</th><th>Projected</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div>")


# ──────────────────────────── week pages ────────────────────────────

# Ruled writing lines are real elements rather than a repeating background
# gradient: a gradient is rasterised into the PDF as a tiled image, which some
# viewers render as a colour block. More lines are emitted than any column can
# show; .ruled clips the remainder.
RULES = "<i></i>" * 14


def density(week):
    """Pick padding and notes height from how full the fullest day is.

    Type size never changes — a wall chart that needs squinting at has
    failed. Lighter weeks get roomier cards and taller notes blocks instead."""
    worst = 0
    for day in week.get("days", []):
        chars = sum(len(w.get("name", "")) + len(key_targets(w)) + 40
                    for w in day.get("workouts", []) if w.get("sport") != "rest")
        worst = max(worst, chars)
    if worst > 300:
        return "dense"
    if worst > 170:
        return "normal"
    return "roomy"


def week_page(plan, week):
    prefs = plan.get("preferences", {})
    phase = next((p for p in plan.get("phases", [])
                  if p["startWeek"] <= week["weekNumber"] <= p["endWeek"]), None)
    phase_name = week.get("phase") or (phase or {}).get("name", "")

    summary = week.get("summary", {}).get("bySport", {})
    bar = "".join(
        f"<span class='sport-chip'><span class='swatch' style='background:{SPORT_COLORS.get(s, "#8a8f9c")}'></span>"
        f"<b>{s.upper()}</b> {v.get('sessions', 0)}× · {fmt_hours(v.get('hours'))}"
        f"{' · ' + format(v['km'], 'g') + 'km' if v.get('km') else ''}</span>"
        for s, v in summary.items()
    )
    total = week.get("summary", {}).get("totalHours", week.get("targetHours"))

    cols = ""
    for day in week.get("days", []):
        workouts = [w for w in day.get("workouts", []) if w.get("sport") != "rest"]
        cards = ""
        for w in workouts:
            color = SPORT_COLORS.get(w.get("sport"), SPORT_COLORS["rest"])
            meta = " · ".join(x for x in [fmt_duration(w.get("durationMinutes")),
                                          distance_label(w, prefs), w.get("primaryZone")] if x)
            kt = key_targets(w)
            cards += (
                f"<div class='wcard' style='border-left-color:{color}'>"
                f"<div class='wtag' style='color:{color}'>{e(w.get('sport', '').upper())}"
                f"<span class='wmeta'>{e(meta)}</span></div>"
                f"<div class='wtitle'>{e(w.get('name'))}</div>"
                + (f"<div class='wkt'><b>KEY TARGETS</b> {e(kt)}</div>" if kt else "")
                + "</div>"
            )
        if not cards:
            cards = "<div class='rest'>Rest</div>"
        cols += (
            f"<div class='day'>"
            f"<div class='dayhead'><b>{e(day.get('dayOfWeek', '')[:3])}</b>"
            f"<span>{e(short_date(day['date']))}</span></div>"
            f"<div class='cards'>{cards}</div>"
            f"""
            <div class='notes'>
                <span>NOTES / ACTUALS</span>
                <div class='ruled'>{RULES}</div>
            </div>
            """
            f"</div>"
        )

    recovery = "<span class='rec-tag'>Recovery week</span>" if week.get("isRecoveryWeek") else ""
    return f"""<section class="page week {density(week)}">
      <header class="wkhead">
        <div class="wkleft">
          <span class="phase-badge" style="background:{phase_color(phase_name)}">{e(phase_name)}</span>
          <h2>Week {e(week['weekNumber'])}<span class="wkdates">{e(date_range(week['startDate'], week['endDate']))}</span></h2>
        </div>
        <div class="wkright"><span class="wkhours">{e(fmt_hours(total))}</span>{recovery}</div>
      </header>
      <p class="wkintro">{e(week.get('focus', ''))}</p>
      <div class="sportbar">{bar}</div>
      <div class="grid">{cols}</div>
    </section>"""


# ─────────────────────── race strategy page ───────────────────────

def humanize(key):
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", str(key)).strip()
    return words[0].upper() + words[1:]


def race_page(plan):
    rs = plan.get("raceStrategy") or {}
    if not rs.get("event"):
        return ""
    pacing = rs.get("pacing") or {}
    nutrition = rs.get("nutrition") or {}
    summary = rs.get("assessmentSummary") or {}

    bike = pacing.get("bike") or {}
    sections = "".join(
        f"<tr><td>{e(s.get('section'))}</td><td>{e(s.get('power'))}</td>"
        f"<td>{e(s.get('speed'))}</td><td class='note'>{e(s.get('note'))}</td></tr>"
        for s in bike.get("bySection", [])
    )
    bike_block = ""
    if sections:
        bike_block = (f"<div class='block'><h2>Bike — pacing by section</h2>"
                      f"<div class='sub'>{e(bike.get('targetPower', ''))}"
                      f"{' · ' + e(bike.get('targetHR')) if bike.get('targetHR') else ''}"
                      f"{' · split ' + e(bike.get('projectedSplit')) if bike.get('projectedSplit') else ''}</div>"
                      f"<table class='tbl tight'><thead><tr><th>Section</th><th>Power</th><th>Speed</th>"
                      f"<th>Note</th></tr></thead><tbody>{sections}</tbody></table></div>")

    def leg(name, obj):
        if not obj:
            return ""
        bits = [obj.get("target") or obj.get("targetPace"), obj.get("targetHR"),
                ("split " + obj["projectedSplit"]) if obj.get("projectedSplit") else None]
        return (f"<div class='mini'><h3>{name}</h3><div class='sub'>"
                + e(" · ".join(b for b in bits if b)) + "</div>"
                + f"<p>{e(obj.get('notes', ''))}</p></div>")

    during = nutrition.get("during") or {}
    nut = (f"<div class='block'><h2>Race-day nutrition</h2>"
           f"<div class='sub'>{e(during.get('carbsPerHour', ''))}g carb/h · {e(during.get('fluidPerHour', ''))}/h"
           f"{' · ' + e(', '.join(during.get('products', []))) if during.get('products') else ''}</div>"
           f"<p><b>Pre-race.</b> {e(nutrition.get('preRace', ''))}</p>"
           f"<p>{e(nutrition.get('notes', ''))}</p></div>")

    risks = "".join(f"<li>{e(r)}</li>" for r in rs.get("keyRisks", []))
    risks_block = f"<div class='block'><h2>Key risks</h2><ul>{risks}</ul></div>" if risks else ""

    assess = ""
    if summary:
        assess = ("<div class='block'><h2>Assessment summary</h2>"
                  + "".join(f"<p><b>{e(humanize(k))}.</b> {e(v)}</p>" for k, v in summary.items())
                  + "</div>")

    return f"""<section class="notes-page">
      <header class="wkhead"><div class="wkleft"><h2>Race strategy &amp; plan notes</h2></div>
        <div class="wkright"><span class="wkhours">{e(rs['event'].get('name', ''))}</span></div></header>
      <div class="flow">
        {bike_block}
        <div class='block legs'>{leg('Swim', pacing.get('swim'))}{leg('Run', pacing.get('run'))}</div>
        {nut}{assess}{risks_block}
      </div>
    </section>"""


# ────────────────────────────── css ──────────────────────────────

CSS = """
@page {
    size: Letter landscape;
    margin: 0.4in;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: "Inter", "Helvetica Neue", Helvetica, Arial, sans-serif;
  color: #14161f; font-size: 10pt; line-height: 1.3;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.page { height: 7.66in; page-break-after: always; break-after: page; display: flex; flex-direction: column; overflow: hidden; }
.page:last-child { page-break-after: auto; break-after: auto; }
h1 { font-size: 25pt; margin: 2pt 0 4pt; letter-spacing: -0.02em; font-weight: 600; }
h2 { font-size: 12pt; margin: 0 0 4pt; font-weight: 600; letter-spacing: 0.01em; }
h3 { font-size: 10.5pt; margin: 0 0 2pt; font-weight: 600; }
p { margin: 0 0 4pt; }
.swatch { display: inline-block; width: 8pt; height: 8pt; border-radius: 2pt; margin-right: 4pt; vertical-align: -1pt; }

/* ── cover ── */
.cover-head { display: flex; justify-content: space-between; align-items: flex-start;
  border-bottom: 1.5pt solid #14161f; padding-bottom: 8pt; margin-bottom: 10pt; }
.kicker { font-size: 9.5pt; letter-spacing: 0.12em; text-transform: uppercase; color: #6a6f7c; }
.cover-sub { font-size: 10.5pt; color: #3c414f; }
.goals { display: flex; flex-direction: column; gap: 5pt; align-items: flex-end; }
.goal { font-size: 12pt; font-weight: 600; padding: 4pt 10pt; border-radius: 4pt; white-space: nowrap; }
.goal-a { background: #14161f; color: #fff; }
.goal-b { background: #e7e9ef; color: #14161f; }
.block { margin-bottom: 8pt; }
.cover .tbl td { padding: 1.6pt 4pt; }
.cover .tbl th { padding: 1.6pt 4pt; }
.cover h2 { margin-bottom: 2pt; }
.cover { line-height: 1.25; }
.cover.tight h1 { font-size: 22pt; }
.cover.tight .block { margin-bottom: 5pt; }
.cover.tight .cover-head { padding-bottom: 5pt; margin-bottom: 6pt; }
.cover.tight .tbl td, .cover.tight .tbl th { padding: 1.2pt 4pt; }
.cover.tighter h1 { font-size: 20pt; }
.cover.tighter .block { margin-bottom: 4pt; }
.cover.tighter .cover-head { padding-bottom: 4pt; margin-bottom: 5pt; }
.cover.tighter .tbl td, .cover.tighter .tbl th { padding: 0.9pt 4pt; line-height: 1.2; }
.cover.tighter .cover-sub { font-size: 10pt; }
.tbl { width: 100%; border-collapse: collapse; font-size: 10pt; }
.tbl th { text-align: left; font-size: 8.5pt; letter-spacing: 0.08em; text-transform: uppercase;
  color: #6a6f7c; border-bottom: 0.75pt solid #c9ccd6; padding: 2pt 4pt; font-weight: 600; }
.tbl td { padding: 2.5pt 4pt; border-bottom: 0.5pt solid #e7e9ef; vertical-align: top; }
.tbl.tight td, .tbl.tight th { padding: 1.8pt 3pt; }
.tbl .total td { font-weight: 700; border-top: 0.75pt solid #c9ccd6; }
.phase-cell { font-weight: 600; white-space: nowrap; }
.focus { color: #3c414f; }
.zone-cols { display: flex; gap: 14pt; }
.zone-col { flex: 1; }
.zone-title { font-size: 9pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
  display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 2pt; }
.zone-value { font-weight: 500; text-transform: none; letter-spacing: 0; color: #3c414f; font-size: 9.5pt; }
.cover-foot { margin-top: auto; display: flex; gap: 18pt; align-items: flex-end; justify-content: space-between; }
.splits { flex: 1; margin-bottom: 0; max-width: 5.4in; }
.splits .sub { font-size: 9pt; color: #6a6f7c; margin-bottom: 2pt; }
.legend { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8pt; font-size: 9.5pt; color: #3c414f; }
.legend-item { text-transform: capitalize; white-space: nowrap; }

/* ── week pages ── */
.wkhead { display: flex; justify-content: space-between; align-items: flex-start;
  border-bottom: 1pt solid #14161f; padding-bottom: 4pt; }
.wkleft { display: flex; align-items: center; gap: 8pt; }
.phase-badge { color: #fff; font-size: 9pt; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; padding: 3pt 8pt; border-radius: 3pt; }
.wkhead h2 { font-size: 18pt; margin: 0; display: flex; align-items: baseline; gap: 10pt; }
.wkdates { font-size: 11pt; font-weight: 400; color: #6a6f7c; }
.wkright { display: flex; align-items: center; gap: 8pt; }
.wkhours { font-size: 15pt; font-weight: 600; }
.rec-tag { font-size: 9pt; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em;
  background: #6c5fc0; color: #fff; padding: 3pt 8pt; border-radius: 3pt; }
.wkintro { font-size: 10.5pt; color: #3c414f; margin: 5pt 0 5pt; max-width: 9in; }
.sportbar { display: flex; flex-wrap: wrap; gap: 6pt; padding-bottom: 6pt; border-bottom: 0.5pt solid #e7e9ef; }
.sport-chip { font-size: 10pt; background: #f2f3f7; border-radius: 3pt; padding: 3pt 7pt; white-space: nowrap; }
.sport-chip b { letter-spacing: 0.06em; }

/* One row that fills the page, so every day column runs floor to ceiling and
   the notes block absorbs whatever space the cards leave. */
.grid { flex: 1; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr));
  grid-template-rows: minmax(0, 1fr); gap: 5pt; margin-top: 6pt; min-height: 0; }
.day { display: flex; flex-direction: column; min-width: 0; }
.dayhead { display: flex; justify-content: space-between; align-items: baseline;
  border-bottom: 0.75pt solid #14161f; padding-bottom: 2pt; margin-bottom: 4pt; }
.dayhead b { font-size: 11pt; }
.dayhead span { font-size: 9.5pt; color: #6a6f7c; }
.cards { display: flex; flex-direction: column; gap: 4pt; }
.wcard { border-left: 2.5pt solid #8a8f9c; background: #f7f8fa; border-radius: 0 3pt 3pt 0;
  padding: 5pt 5pt 5pt 5pt; overflow-wrap: anywhere; }
.wtag { font-size: 8.5pt; font-weight: 700; letter-spacing: 0.07em; }
.wmeta { display: block; color: #4a4f5c; font-weight: 500; letter-spacing: 0; font-size: 9.5pt; }
.wtitle { font-size: 11pt; font-weight: 700; line-height: 1.15; margin-top: 2pt; }
.wkt { font-size: 10pt; margin-top: 4pt; border-top: 0.5pt solid #dcdfe8; padding-top: 2pt; }
.wkt b { font-size: 8.5pt; letter-spacing: 0.07em; color: #6a6f7c; display: block; }
.rest { background: #f2f3f7; border-radius: 3pt; color: #8a8f9c; font-size: 10pt;
  text-align: center; padding: 6pt 0; min-height: 54pt; display: flex;
  align-items: center; justify-content: center; }
/* The notes block takes whatever the cards leave — so a light week gets a tall
   writing area and a heavy one gets a short one, with no change to type size.
   It clips rather than grows, so surplus rules never push the page over. */
.notes { margin-top: auto; padding-top: 5pt; display: flex; flex-direction: column;
  flex: 1 1 0; min-height: 0; overflow: hidden; }
.notes span { font-size: 8.5pt; letter-spacing: 0.07em; color: #8a8f9c; font-weight: 600; }
.ruled { margin-top: 3pt; border-top: 0.5pt solid #c9ccd6; overflow: hidden; }
.ruled i { display: block; height: 15pt; border-bottom: 0.5pt solid #c9ccd6; }

/* Density tiers: the type never shrinks — only padding and the notes block move. */
.roomy .wcard { padding: 7pt; }
.roomy .cards { gap: 6pt; }
.roomy .notes { padding-top: 9pt; }
.normal .notes { padding-top: 6pt; }
.dense .wcard { padding: 4pt; }
.dense .cards { gap: 3pt; }
.dense .notes { padding-top: 4pt; }
.ruled { flex: 1 1 0; min-height: 0; }

/* ── race strategy pages ── */
/* Not a fixed-height .page: this section flows over as many pages as the plan
   needs. Two columns via multicol so it paginates column-by-column, with each
   block kept whole. */
.notes-page { page-break-before: always; break-before: page; }
.notes-page .flow { column-count: 2; column-gap: 20pt; margin-top: 6pt; }
.notes-page .block { break-inside: auto; }
.notes-page tr, .notes-page li, .notes-page p, .notes-page .mini { break-inside: avoid; }
.notes-page h2, .notes-page h3, .notes-page .sub { break-after: avoid; }
.notes-page .wkhead { column-span: all; }
.notes-page .sub { font-size: 9.5pt; color: #6a6f7c; margin-bottom: 3pt; }
.notes-page p, .notes-page li { font-size: 10pt; margin-bottom: 2.5pt; }
.notes-page .block { margin-bottom: 7pt; }
.notes-page h2 { margin-bottom: 2pt; }
.notes-page ul { margin: 0; padding-left: 14pt; }
.notes-page .note { color: #3c414f; }
.legs .mini + .mini { margin-top: 6pt; }
"""

WEASYPRINT_CSS = r"""
/* ------------------------------------------------------------------
   WeasyPrint overrides.

   The chart above is laid out for Chrome: flexbox, grid, gap and
   `margin-top:auto`. WeasyPrint implements little of that, so every
   affected box is restated here with tables, floats and inline-blocks.
   Injected as a second <style> at the end of <head>, so it is author
   origin and wins ties without !important on every line.
   ------------------------------------------------------------------ */

/* Fixed page height is what makes `position:absolute; bottom:0` land on the
   page floor and keeps one week to one sheet. */
.page {
    display: block;
    height: 7.66in;
    overflow: hidden;
}

/* ------------------------------------------------------------------
   Cover page
   ------------------------------------------------------------------ */

.cover { position: relative; }

.cover-head { display: block; }

.cover-head > div:first-child {
    display: inline-block;
    width: 66%;
    vertical-align: top;
}

.goals {
    display: inline-block;
    width: 32%;
    vertical-align: top;
    text-align: right;
}

.goal {
    display: inline-block;
    margin-bottom: 5pt;
}

.zone-cols {
    display: table;
    width: 100%;
    table-layout: fixed;
}

.zone-col {
    display: table-cell;
    width: 33%;
    padding-right: 10pt;
    vertical-align: top;
}

.zone-title { display: block; }

.zone-value { float: right; }

.cover-foot {
    display: block;
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    margin-top: 0;
}

.splits {
    display: inline-block;
    width: 56%;
    max-width: none;
    vertical-align: bottom;
}

.legend {
    display: inline-block;
    width: 42%;
    vertical-align: bottom;
    text-align: right;
}

.legend-item {
    display: inline-block;
    margin-left: 7pt;
}

/* ------------------------------------------------------------------
   Week pages
   ------------------------------------------------------------------ */

.wkhead { display: block; }

.wkleft {
    display: inline-block;
    width: 70%;
    vertical-align: bottom;
}

.wkright {
    display: inline-block;
    width: 29%;
    vertical-align: bottom;
    text-align: right;
}

.phase-badge {
    display: inline-block;
    margin-right: 8pt;
    white-space: nowrap;
}

.wkhead h2 {
    display: inline-block;
    white-space: nowrap;
}

.wkdates {
    margin-left: 10pt;
    white-space: nowrap;
}

.wkhours, .rec-tag {
    display: inline-block;
    margin-left: 8pt;
}

.sportbar {
    display: block;
    white-space: nowrap;
    overflow: hidden;
}

.sport-chip {
    display: inline-block;
    margin-right: 6pt;
}

/* Seven equal day columns, floor to ceiling: a fixed-height table stretches
   its cells the way the grid's `1fr` row did. Cell widths stay auto — a
   percentage would be resolved before border-spacing is added, so the row
   ran 8 gaps wider than the table and Sunday fell off the page. */
.grid {
    display: table;
    width: 100%;
    height: 4.85in;
    table-layout: fixed;
    border-spacing: 5pt 0;
}

.day {
    display: table-cell;
    width: auto;
    vertical-align: top;
    overflow: hidden;
}

.dayhead { display: block; }

.dayhead b { display: inline-block; }

.dayhead span { float: right; }

.cards { display: block; }

.wcard {
    margin-bottom: 4pt;
    overflow-wrap: break-word;
    break-inside: avoid;
}

.rest {
    display: block;
    min-height: 0;
    padding: 16pt 0;
}

/* No `margin-top:auto` without flex, so the writing block is sized per
   density tier instead of absorbing whatever the cards leave. */
.notes {
    display: block;
    margin-top: 6pt;
    overflow: hidden;
}

.ruled {
    display: block;
    overflow: hidden;
}

.ruled i { height: 13pt; }

.roomy .ruled { height: 195pt; }
.normal .ruled { height: 143pt; }
.dense .ruled { height: 91pt; }

/* ------------------------------------------------------------------
   Week-page type scale

   Scoped to .week so the cover, which already matches Chrome, is left
   alone. Roughly 8% off every size on the week pages.
   ------------------------------------------------------------------ */

.week .wkhead h2 { font-size: 16pt; }
.week .wkdates { font-size: 10pt; }
.week .wkhours { font-size: 13pt; }
.week .rec-tag { font-size: 8pt; }
.week .phase-badge { font-size: 8pt; }
.week .wkintro { font-size: 9.5pt; }
.week .sport-chip { font-size: 9pt; }
.week .dayhead b { font-size: 10pt; }
.week .dayhead span { font-size: 8.5pt; }
.week .wtag { font-size: 8pt; }
.week .wmeta { font-size: 8.5pt; }
.week .wtitle { font-size: 10pt; }
.week .wkt { font-size: 9pt; }
.week .wkt b { font-size: 8pt; }
.week .rest { font-size: 9pt; }
.week .notes span { font-size: 8pt; }

/* ------------------------------------------------------------------
   Race strategy page
   ------------------------------------------------------------------ */

/* Multicol plus forced breaks is where WeasyPrint is least predictable, and
   this section is prose — one column costs a page and always paginates. */
.notes-page .flow {
    display: block;
    column-count: 1;
}

.notes-page .block {
    break-inside: avoid;
    page-break-inside: avoid;
}

.notes-page h2, .notes-page h3 {
    break-after: avoid;
}

/* ------------------------------------------------------------------
   Tables
   ------------------------------------------------------------------ */

table { break-inside: avoid; }

tr, td, th { break-inside: avoid; }

/* ------------------------------------------------------------------
   Rendering quality
   ------------------------------------------------------------------ */

/* Liberation Sans is metric-compatible with Arial, so column widths and line
   counts land where the Chrome layout expects them. */
body {
    font-family: Inter, "Liberation Sans", Arial, "Helvetica Neue", Helvetica,
                 "Noto Sans", "DejaVu Sans", sans-serif;
}

* { hyphens: none; }
"""


def build_html(plan):
    pages = [cover_page(plan)]
    for week in sorted(plan.get("weeks", []), key=lambda w: w["weekNumber"]):
        pages.append(week_page(plan, week))
    race = race_page(plan)
    if race:
        pages.append(race)
    title = f"{plan['meta'].get('event', 'Training plan')} — printable"
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{e(title)}</title>"
            f"<style>{CSS}</style></head><body>{''.join(pages)}</body></html>")


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
    """Append the WeasyPrint override sheet as the last author stylesheet.

    Not passed via ``write_pdf(stylesheets=...)``: those are user origin and
    would lose the cascade to the document's own <style>."""
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