# tools/recovery.py
"""Recovery page (/recovery) — opened from the Today tab's In Focus Recovery
card. One day's recovery in detail: training readiness and what drives it,
sleep (with a stage timeline), HRV status against its baseline, body battery
and stress hour by hour, and the overnight vitals.

The day's numbers are the synced daily_metrics payloads (PostgreSQL), the
same ones the Today tab reads, with the rest of its Monday-to-Sunday week for the weekly
charts. A day the sync hasn't stored yet (or a deployment without
PostgreSQL) falls back to the live Garmin tools for that one day.

The hour-by-hour body battery / stress and the sleep-stage timeline aren't
synced, so they come from Garmin live (get_sleep_data / get_stress_data),
cached per day in the dashboard's section cache. If that call fails the
page still renders; those charts just say they're unavailable.

?date=YYYY-MM-DD picks the day (default today; up to a year back). The
weekly charts show that day's calendar week, Monday to Sunday. Picking
another day — in the calendar, on an HRV dot or on a resting HR / respiration
bar — is a navigation, so the whole page follows it. Everything else — the section
filter pills, the linked hour of the two hourly
charts, the sleep-stage highlight — is a small inline script over
server-rendered markup (_RECOVERY_JS).
"""
import html
import json
import logging
from datetime import date, datetime, timedelta, timezone

from tools.dashboard import (_STYLE, _e, _fetch_parallel, _get_cached, _humanize, _label, _local_now,
                             _pwa_asset_url, _readiness_color, _safe, _set_cached)
from tools.local_time import to_local
from tools.navbar import ICON_LINKS, THEME_BOOT, render_nav_html

logger = logging.getLogger(__name__)

PATH = "/recovery"
HISTORY_DAYS = 365          # how far back the calendar goes
_TODAY_INTRADAY_TTL = 300   # today's hourly data is still filling in
_PAST_INTRADAY_TTL = 6 * 3600

_SECTIONS = [("readiness", "Readiness"), ("sleep", "Sleep"), ("hrv", "HRV"),
             ("battery", "Body battery"), ("stress", "Stress"), ("vitals", "Vitals")]

_GREEN, _AMBER, _RED, _TEAL = "#4fae72", "#d9a441", "#cf5a4e", "#7fc9b0"

# Garmin's sleepLevels activityLevel → stage.
_STAGE_BY_LEVEL = {0: "deep", 1: "light", 2: "rem", 3: "awake"}
_STAGES = [("deep", "Deep", "#3a58b8"), ("light", "Light", "#6f9ce8"),
           ("rem", "REM", "#a07fe0"), ("awake", "Awake", "#d9a441")]
_STAGE_COLOR = {k: c for k, _, c in _STAGES}
_STAGE_ROW = {"awake": 0, "rem": 1, "light": 2, "deep": 3}   # hypnogram row, top to bottom

_STRESS_ZONES = [("Rest", "rest_stress_mins", _GREEN), ("Low", "low_stress_mins", _TEAL),
                 ("Medium", "medium_stress_mins", _AMBER), ("High", "high_stress_mins", _RED)]

_BB_CHARGE, _BB_DRAIN, _BB_WORKOUT = "#6f9ce8", "#3d5a9e", "#b5abfc"

_READY_COPY = {
    "high": ("Ready to train hard", "Recovered: sleep and HRV are where they should be. A good day for a key session."),
    "moderate": ("Recovering well", "Most signals are back to normal. Train as planned, but keep the intensity in check."),
    "low": ("Still recovering", "Recent load is still showing in your sleep and HRV. Favour easy aerobic work."),
    "poor": ("Prioritise rest", "Your body is under strain. Rest or keep any activity very easy."),
}
_READY_BAND = {"PRIME": "high", "HIGH": "high", "MODERATE": "moderate", "LOW": "low", "POOR": "poor"}

_HRV_STATUS = {
    "BALANCED": (_GREEN, "Your 7-day average sits inside your baseline: your body is handling its current load."),
    "UNBALANCED": (_AMBER, "Your 7-day average is outside your baseline. Easier days and good sleep help bring it back."),
    "LOW": (_RED, "Your 7-day average is well below your baseline. Prioritise recovery."),
    "POOR": (_RED, "Your 7-day average is well below your baseline. Prioritise recovery."),
}

_STAGE_NOTES = {
    "deep": "Deep sleep {dur}: most of it comes in the first cycles of the night. This is when muscle repair peaks.",
    "light": "Light sleep {dur}: the bridge between cycles. About half the night is normal.",
    "rem": "REM {dur}: grows in the later cycles. Supports memory, learning and mood; an early alarm cuts it short.",
    "awake": "Awake {dur}{count}. Under 30 minutes is typical.",
}

_FACTOR_COLORS = {"EXCELLENT": _GREEN, "VERY_GOOD": _GREEN, "GOOD": _GREEN, "OPTIMAL": _GREEN,
                  "MODERATE": _AMBER, "FAIR": _AMBER, "HIGH": _AMBER,
                  "LOW": _RED, "POOR": _RED, "VERY_POOR": _RED, "VERY_HIGH": _RED}


# ── DATA ────────────────────────────────────────────────────────────────────

def _parse_day(value, today: date) -> date:
    """?date= as a date, clamped to the last year; anything else is today."""
    try:
        day = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return today
    return min(today, max(today - timedelta(days=HISTORY_DAYS), day))


def _live_day(iso: str) -> dict:
    """One day's payloads from the live Garmin tools (no stored row)."""
    from tools.health import get_daily_health, get_daily_readiness, get_sleep, get_training_readiness
    results = _fetch_parallel({
        "sleep": (get_sleep, (iso,), {}),
        "readiness": (get_daily_readiness, (iso,), {}),
        "health": (get_daily_health, (iso,), {}),
        "training": (get_training_readiness, (iso,), {}),
    })
    return {key: value or {} for key, (value, _) in results.items()}


def _ms_to_dt(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _gmt(value) -> datetime | None:
    """A Garmin "2026-10-15T03:12:00.0" GMT string as an aware datetime."""
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _column(descriptors, key: str, default: int) -> int:
    for d in descriptors or []:
        if d.get("key") == key and isinstance(d.get("index"), int):
            return d["index"]
    return default


def parse_intraday(iso: str, sleep_raw: dict | None, stress_raw: dict | None) -> dict:
    """The sleep-stage timeline and hourly body battery / stress for one
    local day, from Garmin's get_sleep_data / get_stress_data payloads."""
    sleep_raw, stress_raw = sleep_raw or {}, stress_raw or {}
    dto = sleep_raw.get("dailySleepDTO") or {}
    # The *Local timestamps are local wall-clock time encoded as epoch ms.
    bed = _ms_to_dt(dto.get("sleepStartTimestampLocal"))
    wake = _ms_to_dt(dto.get("sleepEndTimestampLocal"))

    stages: list[dict] = []
    for lv in sleep_raw.get("sleepLevels") or []:
        start, end = _gmt(lv.get("startGMT")), _gmt(lv.get("endGMT"))
        try:
            stage = _STAGE_BY_LEVEL.get(int(round(float(lv.get("activityLevel")))))
        except (TypeError, ValueError):
            stage = None
        if not (start and end and stage) or end <= start:
            continue
        minutes = (end - start).total_seconds() / 60
        if stages and stages[-1]["stage"] == stage:
            stages[-1]["minutes"] += minutes
        else:
            stages.append({"stage": stage, "minutes": minutes})

    day = date.fromisoformat(iso)

    def local_hour(ms):
        dt = _ms_to_dt(ms)
        if dt is None:
            return None
        local = to_local(dt)
        return local.hour if local.date() == day else None

    bb: list = [None] * 24
    bb_idx = _column(stress_raw.get("bodyBatteryValueDescriptorsDTOList"), "bodyBatteryLevel", 2)
    for row in stress_raw.get("bodyBatteryValuesArray") or []:
        if not isinstance(row, (list, tuple)) or len(row) <= bb_idx or row[bb_idx] is None:
            continue
        h = local_hour(row[0])
        if h is not None:
            bb[h] = int(row[bb_idx])          # the last reading in each hour

    sums, counts = [0] * 24, [0] * 24
    st_idx = _column(stress_raw.get("stressValueDescriptorsDTOList"), "stressLevel", 1)
    for row in stress_raw.get("stressValuesArray") or []:
        if not isinstance(row, (list, tuple)) or len(row) <= st_idx:
            continue
        value = row[st_idx]
        if value is None or value < 0:       # -1 / -2: off-wrist or too active to measure
            continue
        h = local_hour(row[0])
        if h is not None:
            sums[h] += value
            counts[h] += 1
    stress = [round(sums[h] / counts[h]) if counts[h] else None for h in range(24)]

    wake_hour = None
    if wake and wake.date() == day:
        wake_hour = wake.hour + wake.minute / 60
    return {
        "bed": bed.strftime("%H:%M") if bed else None,
        "wake": wake.strftime("%H:%M") if wake else None,
        "wake_hour": wake_hour,
        "stages": [{"stage": s["stage"], "minutes": round(s["minutes"], 1)} for s in stages],
        "body_battery": bb,
        "stress": stress,
    }


def _fetch_intraday(iso: str) -> dict:
    from garmin_client import get_client
    client = get_client()
    results = _fetch_parallel({
        "sleep": (client.get_sleep_data, (iso,), {}),
        "stress": (client.get_stress_data, (iso,), {}),
    })
    (sleep_raw, sleep_err), (stress_raw, stress_err) = results["sleep"], results["stress"]
    if sleep_raw is None and stress_raw is None:
        raise RuntimeError(sleep_err or stress_err or "no intraday data")
    return parse_intraday(iso, sleep_raw, stress_raw)


def _intraday(iso: str, is_today: bool) -> dict | None:
    """The day's hourly data, cached; a stale copy beats none when Garmin
    can't be reached."""
    key = f"recovery_intraday@{iso}"
    cached, _, fresh = _get_cached(key, _TODAY_INTRADAY_TTL if is_today else _PAST_INTRADAY_TTL)
    if cached is not None and fresh:
        return cached
    value, err = _safe(_fetch_intraday, iso)
    if value is not None:
        _set_cached(key, value)
        return value
    logger.warning("Recovery: hourly data for %s unavailable: %s", iso, err)
    return cached


def _workout_hours(day: date) -> list[int]:
    """Local hours of the day that an activity covered."""
    import db
    rows = db.get_activities_in_range(day.isoformat(), (day + timedelta(days=1)).isoformat())
    hours: set[int] = set()
    for r in rows:
        start = r.get("activity_date")
        if not isinstance(start, datetime):
            continue
        local = to_local(start)
        if local.date() != day:
            continue
        end = local + timedelta(minutes=float(r.get("duration_min") or 0))
        # The hour the activity ends in counts unless it ends right on the hour.
        last = 23 if end.date() > day else max(local.hour, end.hour - (end.minute == 0 and end > local))
        hours.update(range(local.hour, last + 1))
    return sorted(hours)


def build_recovery_data(day_param: str | None = None) -> dict:
    """Everything the page shows for one day (see the module docstring)."""
    import db
    now = _local_now()
    today = now.date()
    day = _parse_day(day_param, today)
    start = day - timedelta(days=day.weekday())      # the week runs Monday to Sunday
    end = min(today, start + timedelta(days=6))
    days: dict[str, dict] = {}
    scores: dict[str, int] = {}
    workout_hours: list[int] = []
    if db.is_configured():
        rows, err = _safe(db.get_recovery_days, start.isoformat(), end.isoformat())
        for r in rows or []:
            days[str(r["metric_date"])] = {
                "sleep": r.get("sleep_data") or {}, "readiness": r.get("readiness_data") or {},
                "health": r.get("health_data") or {}, "training": r.get("training_data") or {},
            }
        if err:
            logger.warning("Recovery: stored days unavailable: %s", err)
        scores = _safe(db.get_readiness_scores, (today - timedelta(days=HISTORY_DAYS)).isoformat(),
                       today.isoformat())[0] or {}
        workout_hours = _safe(_workout_hours, day)[0] or []
    if day.isoformat() not in days:
        days[day.isoformat()] = _live_day(day.isoformat())
        score = ((days[day.isoformat()].get("training") or {}).get("readiness") or {}).get("score")
        if score is not None:
            scores[day.isoformat()] = score
    return {
        "date": day.isoformat(),
        "today": today.isoformat(),
        "now_hour": now.hour,
        "days": days,
        "scores": scores,
        "intraday": _intraday(day.isoformat(), day == today),
        "workout_hours": workout_hours,
    }


# ── FORMATTING ──────────────────────────────────────────────────────────────

def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(value, digits: int = 0) -> str:
    v = _f(value)
    if v is None:
        return "&mdash;"
    return f"{v:.{digits}f}" if digits else str(round(v))


def _hm(minutes) -> str:
    m = _f(minutes)
    if m is None:
        return "&mdash;"
    m = round(m)
    return f"{m // 60}h{m % 60:02d}"


def _day_short(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _unit(text: str) -> str:
    return f'<span class="rc-unit"> {text}</span>'


def _band(level) -> str | None:
    return _READY_BAND.get(str(level or "").upper())


def _icon(paths: str, size: int = 17) -> str:
    return (f'<svg aria-hidden="true" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{paths}</svg>')


_I_MOON = '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"></path>'
_I_PULSE = '<path d="M22 12h-4l-3 8-6-16-3 8H2"></path>'
_I_BATTERY = '<rect x="2" y="7" width="17" height="10" rx="2"></rect><path d="M22 11v2"></path><path d="M11 9l-2 3h3l-2 3"></path>'
_I_STRESS = '<path d="M3 12h3l2-5 4 10 2-5h7"></path>'
_I_HEART = ('<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 '
            '1-1.1a5.5 5.5 0 0 0 0-7.8z"></path>')
_I_LUNGS = ('<path d="M17.7 7.7A2.5 2.5 0 1 1 19.5 12H2"></path><path d="M9.6 4.6A2 2 0 1 1 11 8H2"></path>'
            '<path d="M12.6 19.4A2 2 0 1 0 14 16H2"></path>')
_I_CAL = '<rect x="3" y="5" width="18" height="16" rx="2"></rect><path d="M3 10h18M8 3v4M16 3v4"></path>'
_I_DOWN = '<path d="M6 9l6 6 6-6"></path>'
_I_LEFT = '<path d="M15 18l-6-6 6-6"></path>'
_I_RIGHT = '<path d="M9 18l6-6-6-6"></path>'


def _card_head(icon: str, color: str, title: str, right: str = "") -> str:
    return (f'<div class="rc-ch"><div class="rc-ic" style="background:color-mix(in srgb, {color} 18%, transparent);'
            f'color:{color}">{_icon(icon)}</div><h2 class="rc-h2">{title}</h2>{right}</div>')


# ── SECTIONS ────────────────────────────────────────────────────────────────

def _resting_hr(d: dict):
    stats = (d.get("readiness") or {}).get("daily_stats") or {}
    for v in (stats.get("resting_hr"), ((d.get("health") or {}).get("heart_rate") or {}).get("resting_hr"),
              (d.get("sleep") or {}).get("resting_hr")):
        if v is not None:
            return v
    return None


def _respiration(d: dict):
    v = (d.get("sleep") or {}).get("avg_respiration")
    return v if v is not None else ((d.get("health") or {}).get("respiration") or {}).get("avg_sleep")


def _readiness_section(day: date, d: dict, is_today: bool) -> str:
    tr = (d.get("training") or {}).get("readiness") or {}
    readiness = d.get("readiness") or {}
    score = _f(tr.get("score"))
    color = _readiness_color(tr.get("level")) if score is not None else "var(--color-neutral-600)"
    band = _band(tr.get("level"))
    head, body = _READY_COPY.get(band, ("No readiness score", "Garmin didn't record a training readiness score for this day."))
    if tr.get("feedback_short"):
        head = _humanize(tr["feedback_short"])
    deg = round(max(0, min(100, score or 0)) * 3.6)
    when = "This morning" if is_today else f"{_day_short(day)}, morning"
    bb = readiness.get("body_battery") or {}
    hrv = (readiness.get("hrv") or {}).get("last_night_avg")
    hero = f"""
  <section class="rc-card" aria-label="Training readiness">
    <div class="rc-sp"><div class="rc-kick">Training readiness</div><div class="rc-small">{when}</div></div>
    <div class="rc-hero">
      <div class="rc-ring" role="img" aria-label="Training readiness {_num(score)} of 100"
           style="background:conic-gradient({color} {deg}deg, var(--color-neutral-800) 0deg)">
        <div class="rc-ring-in"><div class="rc-ring-v">{_num(score)}</div>
          <div class="rc-ring-l" style="color:{color}">{_label(tr.get("level")) if tr.get("level") else "&nbsp;"}</div></div>
      </div>
      <div class="rc-hero-t"><div class="rc-headline">{_e(head)}</div><div class="rc-body">{_e(body)}</div></div>
    </div>
    <div class="rc-tiles">
      <div class="rc-tile"><div class="rc-stat-l">HRV</div><div class="rc-stat-v">{_num(hrv)}{_unit("ms")}</div></div>
      <div class="rc-tile"><div class="rc-stat-l">Resting HR</div><div class="rc-stat-v">{_num(_resting_hr(d))}{_unit("bpm")}</div></div>
      <div class="rc-tile"><div class="rc-stat-l">Battery on wake</div><div class="rc-stat-v">{_num(bb.get("start_level"))}</div></div>
    </div>
  </section>"""

    rec = _f(tr.get("recovery_time"))
    factors = [
        ("Sleep score", _num(tr.get("sleep_score")), "sleep_score_factor", "Last night"),
        ("Sleep history", "", "sleep_history_factor", "Recent nights"),
        ("HRV status", f'{_num(tr.get("hrv_weekly_average"))} ms' if tr.get("hrv_weekly_average") is not None else "",
         "hrv_factor", "7-day average against your baseline"),
        ("Recovery time", (f"{round(rec / 60)} h left" if rec else "Recovered") if rec is not None else "",
         "recovery_time_factor", "Time until fully recovered"),
        ("Acute load", _num(tr.get("acute_load")) if tr.get("acute_load") is not None else "", "acwr_factor",
         "Recent training load"),
        ("Stress history", "", "stress_history_factor", "Recent stress"),
    ]
    rows = []
    for label, value, key, note in factors:
        pct, feedback = _f(tr.get(f"{key}_percent")), tr.get(f"{key}_feedback")
        if pct is None and not feedback:
            continue
        fc = _FACTOR_COLORS.get(str(feedback or "").upper(), "var(--color-accent)")
        rows.append(f"""
        <div class="rc-fac">
          <div class="rc-sp rc-fac-t"><span>{label}</span><span><span class="rc-small">{value}</span>
            <b style="color:{fc}">{_label(feedback) if feedback else ""}</b></span></div>
          <div class="rc-track"><div style="width:{max(3, min(100, pct or 0)):.0f}%;background:{fc}"></div></div>
          <div class="rc-small">{note}</div>
        </div>""")
    factor_body = "".join(rows) or '<div class="rc-small">No readiness breakdown for this day.</div>'
    contributors = f"""
  <section class="rc-card rc-card-tight" aria-label="Readiness contributors">
    <details class="rc-details" open>
      <summary><span class="rc-kick">What's driving it</span><span class="rc-toggle">{_icon(_I_DOWN, 16)}</span></summary>
      <div class="rc-facs">{factor_body}</div>
    </details>
  </section>"""
    return f'<div class="rc-row" data-sec="readiness">{hero}{contributors}</div>'


def _sleep_section(d: dict, intraday: dict | None) -> str:
    s = d.get("sleep") or {}
    if not s:
        return _empty("sleep", _I_MOON, "#6f9ce8", "Sleep", "No sleep was recorded for this night.")
    mins = {k: (_f(s.get(f"{k}_sleep_hrs")) or 0) * 60 for k in ("deep", "light", "rem")}
    mins["awake"] = (_f(s.get("awake_hrs")) or 0) * 60
    total = (_f(s.get("total_sleep_hrs")) or 0) * 60
    in_bed = total + mins["awake"]
    need = (_f(s.get("sleep_need_hrs")) or 0) * 60
    score = _f(s.get("sleep_score"))
    sc_color = _GREEN if (score or 0) >= 80 else _AMBER if (score or 0) >= 60 else _RED
    quality = _humanize(s.get("sleep_score_label")) or ("Good" if (score or 0) >= 80 else "Fair" if (score or 0) >= 60 else "Poor")
    if need:
        diff = total - need
        need_line = f"of {_hm(need)} need · " + ("met" if diff >= 0 else f"{_hm(-diff)} short")
        default_note = ("You met your sleep need." if diff >= 0 else f"You slept {_hm(-diff)} under your need.")
    else:
        need_line, default_note = "", ""
    stages_data = (intraday or {}).get("stages") or []
    bed, wake = (intraday or {}).get("bed"), (intraday or {}).get("wake")
    if stages_data:
        segs = "".join(
            f'<div class="rc-seg" data-seg="{x["stage"]}" style="flex:{x["minutes"]:.1f} 1 0px;'
            f'margin-top:{_STAGE_ROW[x["stage"]] * 16 + 2}px;background:{_STAGE_COLOR[x["stage"]]}"></div>'
            for x in stages_data)
        hyp = f"""
    <div class="rc-hyp-wrap">
      <div class="rc-hyp-l"><span>Awake</span><span>REM</span><span>Light</span><span>Deep</span></div>
      <div style="flex:1;min-width:0">
        <div class="rc-hyp" role="img" aria-label="Sleep stages through the night">
          <i style="top:8px"></i><i style="top:24px"></i><i style="top:40px"></i><i style="top:56px"></i>
          <div class="rc-segs">{segs}</div>
        </div>
        <div class="rc-sp rc-axis"><span>{_e(bed or "")}</span><span>{_e(wake or "")}</span></div>
      </div>
    </div>"""
        default_note = (default_note + " Tap a stage to see where it fell in the night.").strip()
    else:
        hyp = '<div class="rc-small">The stage-by-stage timeline isn\'t available for this night.</div>'
    count = s.get("awake_count")
    chips = []
    for key, label, color in _STAGES:
        note = _STAGE_NOTES[key].format(dur=_hm(mins[key]),
                                        count=f" across {count} wake-ups" if count else "")
        pct = round(mins[key] / in_bed * 100) if in_bed else 0
        chips.append(f'<button type="button" class="rc-stage" data-stage="{key}" aria-pressed="false" '
                     f'data-note="{html.escape(note, quote=True)}"><span class="rc-sw" style="background:{color}"></span>'
                     f'<span class="rc-stage-n">{label}</span><b>{_hm(mins[key])}</b><span class="rc-small rc-pct">{pct}%</span></button>')
    times = f'{_e(bed)} – {_e(wake)}<div class="rc-small">in bed {_hm(in_bed)}</div>' if bed and wake else \
        f'<div class="rc-small">in bed {_hm(in_bed)}</div>'
    right = (f'<div class="rc-hv"><span class="rc-small">Score</span><span class="rc-hv-n" style="color:#6f9ce8">{_num(score)}</span>'
             f'<span class="rc-small" style="color:{sc_color}">{_e(quality)}</span></div>')
    return f"""
  <section class="rc-card" data-sec="sleep" aria-label="Sleep">
    {_card_head(_I_MOON, "#6f9ce8", "Sleep", right)}
    <div class="rc-sp" style="align-items:flex-end">
      <div><div class="rc-big">{_hm(total)}</div><div class="rc-small" style="margin-top:4px">{need_line}</div></div>
      <div class="rc-times">{times}</div>
    </div>
    {hyp}
    <div class="rc-stages">{"".join(chips)}</div>
    <div class="rc-inset rc-note" data-stage-note>{_e(default_note) or "&nbsp;"}</div>
  </section>"""


def _hrv_section(day: date, today: date, week: list[tuple[date, dict]]) -> str:
    hrv = ((dict(week).get(day) or {}).get("readiness") or {}).get("hrv") or {}
    nights = [(d, _f(((x.get("readiness") or {}).get("hrv") or {}).get("last_night_avg"))) for d, x in week]
    if not any(v is not None for _, v in nights) and not hrv:
        return _empty("hrv", _I_PULSE, _TEAL, "HRV status", "No HRV readings for this week.")
    low, high = _f(hrv.get("baseline_low")), _f(hrv.get("baseline_high"))
    weekly = _f(hrv.get("weekly_avg"))
    values = [v for _, v in nights if v is not None] + [x for x in (low, high, weekly) if x is not None]
    lo = min(values) - 8 if values else 30
    hi = max(values) + 8 if values else 70
    span = max(hi - lo, 1)
    h = 132

    def y(v):  # px from the bottom
        return (v - lo) / span * h

    status = str(hrv.get("status") or "").upper()
    st_color, st_note = _HRV_STATUS.get(status, ("var(--color-neutral-500)", "No HRV status for this day."))
    band = ""
    axis = ""
    if low is not None and high is not None:
        top, height = h - y(high), y(high) - y(low)
        band = f'<div class="rc-band" style="top:{top:.0f}px;height:{height:.0f}px"></div>'
        axis = (f'<span style="top:{top - 7:.0f}px">{_num(high)}</span>'
                f'<span style="top:{top + height - 7:.0f}px">{_num(low)}</span>')
    avg_line = f'<div class="rc-avg" style="bottom:{y(weekly):.0f}px"></div>' if weekly is not None else ""
    dots, labels = [], []
    for d, v in nights:
        sel = d == day
        label = "Last night" if d == today else _day_short(d)
        if v is None:
            dots.append('<div class="rc-dot rc-dot-none" aria-hidden="true"></div>')
        else:
            in_range = low is None or high is None or low <= v <= high
            c = _GREEN if in_range else _AMBER
            dots.append(
                f'<button type="button" class="rc-dot{" on" if sel else ""}" data-go="{d.isoformat()}" '
                f'aria-label="{_day_short(d)}, HRV {_num(v)} milliseconds" aria-pressed="{"true" if sel else "false"}">'
                f'<span class="rc-dot-v">{_num(v)}</span><span class="rc-dot-d" style="background:{c}"></span>'
                f'<span class="rc-dot-s" style="height:{max(0, y(v) - 5):.0f}px;background:color-mix(in srgb, {c} 45%, transparent)"></span></button>')
        labels.append(f'<span>{"Today" if d == today else d.strftime("%a")[:1] + " " + str(d.day)}</span>')
    sel_v = dict(nights).get(day)
    sel_label = "Last night" if day == today else _day_short(day)
    pill = (f'<span class="rc-pillstat" style="background:color-mix(in srgb, {st_color} 16%, transparent);color:{st_color}">'
            f'{_label(status)}</span>') if status else ""
    baseline = f"{_num(low)}–{_num(high)}" if low is not None and high is not None else "&mdash;"
    return f"""
  <section class="rc-card" data-sec="hrv" aria-label="HRV status">
    {_card_head(_I_PULSE, _TEAL, "HRV status", pill)}
    <div class="rc-3">
      <div><div class="rc-stat-l">{_e(sel_label)}</div><div class="rc-stat-v rc-lg">{_num(sel_v)}{_unit("ms")}</div></div>
      <div><div class="rc-stat-l">7-day avg</div><div class="rc-stat-v rc-lg">{_num(weekly)}{_unit("ms")}</div></div>
      <div><div class="rc-stat-l">Baseline</div><div class="rc-stat-v" style="padding-top:6px">{baseline}{_unit("ms")}</div></div>
    </div>
    <div class="rc-hrv">
      <div class="rc-hrv-axis" aria-hidden="true">{axis}</div>
      <div style="flex:1;min-width:0">
        <div class="rc-hrv-plot">{band}{avg_line}<div class="rc-dots">{"".join(dots)}</div></div>
        <div class="rc-dots rc-dot-days">{"".join(labels)}</div>
      </div>
    </div>
    <div class="rc-legend">
      <span><i class="rc-lg-band"></i>Your baseline</span><span><i class="rc-lg-avg"></i>7-day avg</span>
      <span><i style="background:{_GREEN};border-radius:50%"></i>In range</span>
      <span><i style="background:{_AMBER};border-radius:50%"></i>Outside</span>
    </div>
    <div class="rc-inset rc-note">{_e(st_note)}</div>
  </section>"""


def _hours(data: dict, d: dict) -> dict:
    """The two hourly series, with each hour's readout text."""
    intraday = data.get("intraday") or {}
    is_today = data["date"] == data["today"]
    now_hour = data.get("now_hour") if is_today else None
    bb = list(intraday.get("body_battery") or [None] * 24)
    stress = list(intraday.get("stress") or [None] * 24)
    if is_today and now_hour is not None:
        for h in range(now_hour + 1, 24):
            bb[h] = stress[h] = None
    wake = intraday.get("wake_hour")
    workouts = set(data.get("workout_hours") or [])
    labels, bb_dir, st_dir, bb_color = [], [], [], []
    prev = None
    for h in range(24):
        labels.append(f"{h:02d}:00" + (" (now)" if h == now_hour else ""))
        v = bb[h]
        future = now_hour is not None and h > now_hour
        if v is None:
            bb_dir.append("not yet" if future else "no reading")
            bb_color.append(None)
        else:
            asleep = wake is not None and h < wake
            if h in workouts:
                bb_dir.append("workout")
                bb_color.append(_BB_WORKOUT)
            elif asleep or (prev is not None and v > prev):
                bb_dir.append("charging in sleep" if asleep else "charging")
                bb_color.append(_BB_CHARGE)
            else:
                bb_dir.append("draining" if prev is None or v < prev else "steady")
                bb_color.append(_BB_DRAIN)
            prev = v
        s = stress[h]
        if s is None:
            st_dir.append("not yet" if future else "no reading")
        else:
            zone = "rest" if s <= 25 else "low" if s <= 50 else "medium" if s <= 75 else "high"
            st_dir.append(("asleep · " if wake is not None and h < wake else "") + zone)
    if is_today and now_hour is not None:
        sel = now_hour
    else:
        sel = max((h for h in range(24) if stress[h] is not None), key=lambda h: stress[h], default=12)
    return {"bb": bb, "stress": stress, "labels": labels, "bbDir": bb_dir, "stressDir": st_dir,
            "bbColor": bb_color, "sel": sel, "has": any(v is not None for v in bb + stress)}


def _hour_bars(values: list, colors: list, sel: int, kind: str) -> str:
    out = []
    for h, v in enumerate(values):
        fill = 0 if v is None else max(3, min(100, v))
        cls = "rc-hr" + (" on" if h == sel else "") + (" empty" if v is None else "")
        out.append(f'<button type="button" class="{cls}" data-hour="{h}" '
                   f'aria-label="{h:02d}:00, {kind} {"no reading" if v is None else v}">'
                   f'<span style="height:{fill}%;background:{colors[h] or "transparent"}"></span></button>')
    return (f'<div class="rc-hours">{"".join(out)}</div>'
            '<div class="rc-sp rc-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>')


def _battery_section(data: dict, d: dict, hrs: dict) -> str:
    bb = (d.get("readiness") or {}).get("body_battery") or {}
    is_today = data["date"] == data["today"]
    if not bb and not hrs["has"]:
        return _empty("battery", _I_BATTERY, "#6f9ce8", "Body battery", "No body battery readings for this day.")
    right = (f'<div class="rc-hv"><span class="rc-small">{"Now" if is_today else "End of day"}</span>'
             f'<span class="rc-hv-n">{_num(bb.get("current_level"))}</span></div>')
    stats = f"""
    <div class="rc-inset rc-4">
      <div><div class="rc-stat-l">Charged</div><div class="rc-stat-m" style="color:{_BB_CHARGE}">+{_num(bb.get("charged"))}</div></div>
      <div><div class="rc-stat-l">Drained</div><div class="rc-stat-m">−{_num(bb.get("drained"))}</div></div>
      <div><div class="rc-stat-l">High</div><div class="rc-stat-m">{_num(bb.get("highest"))}</div></div>
      <div><div class="rc-stat-l">Low</div><div class="rc-stat-m">{_num(bb.get("lowest"))}</div></div>
    </div>"""
    if hrs["has"]:
        sel = hrs["sel"]
        at = hrs["bb"][sel]
        chart = f"""
    <div class="rc-sp rc-readout"><span class="rc-small" data-hour-label>{hrs["labels"][sel]}</span>
      <span><b data-bb-at>{"—" if at is None else at}</b><span class="rc-small"> · <span data-bb-dir>{hrs["bbDir"][sel]}</span></span></span></div>
    <div>{_hour_bars(hrs["bb"], hrs["bbColor"], sel, "body battery")}</div>
    <div class="rc-legend"><span><i style="background:{_BB_CHARGE}"></i>Charging</span>
      <span><i style="background:{_BB_DRAIN}"></i>Draining</span><span><i style="background:{_BB_WORKOUT}"></i>Workout</span></div>"""
    else:
        chart = '<div class="rc-small">Hour-by-hour readings aren\'t available for this day.</div>'
    return f"""
  <section class="rc-card" data-sec="battery" aria-label="Body battery">
    {_card_head(_I_BATTERY, "#6f9ce8", "Body battery", right)}{stats}{chart}
  </section>"""


def _stress_section(d: dict, hrs: dict) -> str:
    stress = (d.get("health") or {}).get("stress") or {}
    avg = _f(stress.get("avg_stress"))
    if avg is None:
        avg = _f(((d.get("readiness") or {}).get("daily_stats") or {}).get("avg_stress"))
    if avg is None and not hrs["has"]:
        return _empty("stress", _I_STRESS, _AMBER, "Stress", "No stress readings for this day.")
    zone = None if avg is None else "Rest" if avg <= 25 else "Low" if avg <= 50 else "Medium" if avg <= 75 else "High"
    zcolor = {"Rest": _GREEN, "Low": _TEAL, "Medium": _AMBER, "High": _RED}.get(zone, "var(--color-neutral-500)")
    right = (f'<div class="rc-hv"><span class="rc-small">Avg</span><span class="rc-hv-n">{_num(avg)}</span>'
             f'<span class="rc-small" style="color:{zcolor}">{zone or ""}</span></div>')
    mins = [(label, _f(stress.get(key)), color) for label, key, color in _STRESS_ZONES]
    total = sum(m or 0 for _, m, _ in mins)
    zones = ""
    if total:
        bar = "".join(f'<div style="width:{(m or 0) / total * 100:.1f}%;background:{c}"></div>' for _, m, c in mins)
        legend = "".join(f'<div><div class="rc-small rc-zl"><i style="background:{c}"></i>{label}</div>'
                         f'<div class="rc-stat-m">{_hm(m)}</div></div>' for label, m, c in mins)
        zones = f'<div class="rc-inset rc-zones"><div class="rc-stack">{bar}</div><div class="rc-4">{legend}</div></div>'
    if hrs["has"]:
        sel = hrs["sel"]
        at = hrs["stress"][sel]
        colors = [None if v is None else _GREEN if v <= 25 else _TEAL if v <= 50 else _AMBER if v <= 75 else _RED
                  for v in hrs["stress"]]
        chart = f"""
    <div class="rc-sp rc-readout"><span class="rc-small" data-hour-label>{hrs["labels"][sel]}</span>
      <span><b data-stress-at>{"—" if at is None else at}</b><span class="rc-small"> · <span data-stress-dir>{hrs["stressDir"][sel]}</span></span></span></div>
    <div class="rc-stressbars">{_hour_bars(hrs["stress"], colors, sel, "stress")}</div>
    <div class="rc-legend"><span><i style="background:{_GREEN}"></i>Rest 0–25</span><span><i style="background:{_TEAL}"></i>Low 26–50</span>
      <span><i style="background:{_AMBER}"></i>Medium 51–75</span><span><i style="background:{_RED}"></i>High 76+</span></div>"""
    else:
        chart = '<div class="rc-small">Hour-by-hour readings aren\'t available for this day.</div>'
    return f"""
  <section class="rc-card" data-sec="stress" aria-label="Stress">
    {_card_head(_I_STRESS, _AMBER, "Stress", right)}{zones}{chart}
  </section>"""


def _mini_bars(values: list, color: str, week: list[tuple[date, dict]], day: date, today: date, what: str) -> str:
    """One bar per weekday, Monday to Sunday; a tap picks that day for the page."""
    known = [v for v in values if v is not None]
    if not known:
        return ""
    lo, hi = min(known), max(known)
    span = (hi - lo) or 1
    bars = []
    for (d, _), v in zip(week, values):
        sel = d == day
        h = 4 if v is None else round((v - lo) / span * 30) + 8
        bg = ("var(--color-neutral-800)" if v is None else color if sel else "var(--color-neutral-700)")
        label = f"{_day_short(d)}, {what} {_num(v, 1) if what == 'respiration' else _num(v)}" if v is not None else f"{_day_short(d)}, no {what}"
        go = f' data-go="{d.isoformat()}"' if d <= today else " disabled"
        bars.append(f'<button type="button" class="rc-mbar{" on" if sel else ""}"{go} aria-label="{label}" '
                    f'aria-pressed="{"true" if sel else "false"}"><span style="height:{h}px;background:{bg}"></span></button>')
    days = "".join(f'<span class="{"on" if d == day else ""}">{d.strftime("%a")[0]}</span>' for d, _ in week)
    return f'<div class="rc-mini">{"".join(bars)}</div><div class="rc-mini-days" aria-hidden="true">{days}</div>'


def _vitals_section(day: date, today: date, d: dict, week: list[tuple[date, dict]]) -> str:
    rhr = [_f(_resting_hr(x)) for _, x in week]
    resp = [_f(_respiration(x)) for _, x in week]
    known = [v for v in rhr if v is not None]
    rhr_avg = (((d.get("readiness") or {}).get("daily_stats") or {}).get("resting_hr_7day_avg")
               or (sum(known) / len(known) if known else None))
    return f"""
  <section class="rc-vitals" data-sec="vitals" aria-label="Overnight vitals">
    <div class="rc-card rc-vital">
      <div class="rc-vl"><span style="color:#e0736f">{_icon(_I_HEART, 14)}</span>Resting HR</div>
      <div class="rc-big rc-v">{_num(_resting_hr(d))}{_unit("bpm")}</div>
      {_mini_bars(rhr, "#e0736f", week, day, today, "resting heart rate")}
      <div class="rc-small">7-day avg {_num(rhr_avg)} bpm</div>
    </div>
    <div class="rc-card rc-vital">
      <div class="rc-vl"><span style="color:{_TEAL}">{_icon(_I_LUNGS, 14)}</span>Respiration</div>
      <div class="rc-big rc-v">{_num(_respiration(d), 1)}{_unit("brpm")}</div>
      {_mini_bars(resp, _TEAL, week, day, today, "respiration")}
      <div class="rc-small">Avg while asleep</div>
    </div>
  </section>"""


def _empty(sec: str, icon: str, color: str, title: str, message: str) -> str:
    return (f'<section class="rc-card" data-sec="{sec}" aria-label="{title}">{_card_head(icon, color, title)}'
            f'<div class="rc-small">{message}</div></section>')


# ── PAGE ────────────────────────────────────────────────────────────────────

def render_recovery_body(data: dict, token: str | None = None) -> str:
    day = date.fromisoformat(data["date"])
    today = date.fromisoformat(data["today"])
    is_today = day == today
    days = data.get("days") or {}
    monday = day - timedelta(days=day.weekday())
    week = [(monday + timedelta(days=i), days.get((monday + timedelta(days=i)).isoformat()) or {})
            for i in range(7)]
    d = days.get(data["date"]) or {}
    hrs = _hours(data, d)

    date_label = "Today" if is_today else _day_short(day) + (f" {day.year}" if day.year != today.year else "")
    pills = '<button type="button" class="rc-pill" data-filter="all" aria-pressed="true">All</button>' + "".join(
        f'<button type="button" class="rc-pill" data-filter="{k}" aria-pressed="false">{label}</button>'
        for k, label in _SECTIONS)
    config = json.dumps({
        "date": data["date"], "today": data["today"],
        "min": (today - timedelta(days=HISTORY_DAYS)).isoformat(),
        "scores": data.get("scores") or {},
        "sections": len(_SECTIONS),
        "bb": hrs["bb"], "stress": hrs["stress"], "labels": hrs["labels"],
        "bbDir": hrs["bbDir"], "stressDir": hrs["stressDir"],
    }).replace("</", "<\\/")

    main = f"""
<main class="rc">
  <a class="rc-back" href="{_e(_pwa_asset_url("/dashboard", token))}">{_icon(_I_LEFT, 20)}Today</a>
  <div class="rc-head">
    <h1 class="rc-h1">Recovery</h1>
    <button type="button" id="rc-date-btn" class="rc-date" aria-haspopup="dialog" aria-expanded="false"
            aria-controls="rc-cal" aria-label="Choose day, showing {_e(date_label)}">{_icon(_I_CAL, 16)}{_e(date_label)}<span class="rc-chev">{_icon(_I_DOWN, 14)}</span></button>
    <div id="rc-cal" class="rc-cal" role="dialog" aria-label="Choose a day" hidden>
      <div class="rc-sp">
        <button type="button" class="rc-icbtn" data-cal-step="-1" aria-label="Previous month">{_icon(_I_LEFT, 18)}</button>
        <div class="rc-cal-title" data-cal-title></div>
        <button type="button" class="rc-icbtn" data-cal-step="1" aria-label="Next month">{_icon(_I_RIGHT, 18)}</button>
      </div>
      <div class="rc-cal-grid rc-cal-wd" aria-hidden="true"><span>M</span><span>T</span><span>W</span><span>T</span><span>F</span><span>S</span><span>S</span></div>
      <div class="rc-cal-grid" data-cal-days></div>
      <div class="rc-sp rc-cal-foot">
        <div class="rc-legend"><span><i style="background:{_GREEN};border-radius:50%"></i>High</span>
          <span><i style="background:{_AMBER};border-radius:50%"></i>Moderate</span><span><i style="background:{_RED};border-radius:50%"></i>Low</span></div>
        <div style="display:flex;gap:4px"><button type="button" class="rc-textbtn" data-cal-today>Today</button>
          <button type="button" class="rc-textbtn rc-textbtn-fill" data-cal-close>Close</button></div>
      </div>
    </div>
  </div>
  <div class="rc-pills" role="group" aria-label="Filter sections">{pills}</div>
  {_readiness_section(day, d, is_today)}
  <div class="rc-row">{_sleep_section(d, data.get("intraday"))}{_hrv_section(day, today, week)}</div>
  <div class="rc-row">{_battery_section(data, d, hrs)}{_stress_section(d, hrs)}</div>
  {_vitals_section(day, today, d, week)}
</main>"""
    nav = render_nav_html("today", token, title="Ridgeline")
    return (f"{nav}{main}<script>window.__RC = {config};</script><script>{_RECOVERY_JS}</script>"
            "</body></html>")


def render_recovery_html(data: dict, token: str | None = None) -> str:
    return _head(token) + render_recovery_body(data, token)


def _head(token: str | None) -> str:
    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        f'<link rel="manifest" href="{_e(_pwa_asset_url("/manifest.webmanifest", token))}">'
        f"{THEME_BOOT}{ICON_LINKS}"
        "<title>Recovery · Ridgeline</title>"
        f"<style>{_STYLE}{RECOVERY_CSS}</style>"
        "</head><body>"
    )


def render_recovery_page(day_param: str | None, token: str | None = None) -> str:
    """The whole page for /recovery; a failure is shown in the page."""
    try:
        return render_recovery_html(build_recovery_data(day_param), token)
    except Exception as e:  # noqa: BLE001 — shown in the page, never raised
        logger.exception("Recovery render failed")
        return (_head(token) + render_nav_html("today", token, title="Ridgeline")
                + f'<main class="rc"><div class="err">Recovery error: {_e(e)}</div></main></body></html>')


RECOVERY_CSS = """
.rc { max-width:560px; margin:0 auto; padding:calc(8px + env(safe-area-inset-top, 0px)) 16px 40px;
  display:flex; flex-direction:column; gap:14px; }
.rc-back { align-self:flex-start; display:inline-flex; align-items:center; gap:2px; min-height:44px; padding-right:12px;
  color:var(--color-accent-400); text-decoration:none; font-size:15px; }
.rc-head { position:relative; display:flex; align-items:center; justify-content:space-between; gap:12px; }
.rc-h1 { font-size:30px; font-weight:600; letter-spacing:-.01em; line-height:1.15; }
.rc-date { min-height:44px; display:inline-flex; align-items:center; gap:7px; padding:0 14px; border-radius:999px;
  border:1px solid color-mix(in srgb, var(--color-accent) 45%, transparent);
  background:color-mix(in srgb, var(--color-accent) 14%, transparent); color:var(--color-accent-200);
  font:inherit; font-size:14px; font-weight:500; cursor:pointer; white-space:nowrap; }
.rc-chev { display:grid; transition:transform .15s ease; }
.rc-date[aria-expanded=true] .rc-chev { transform:rotate(180deg); }
.rc-cal { position:absolute; right:0; top:54px; z-index:30; width:336px; max-width:calc(100vw - 32px); box-sizing:border-box;
  background:var(--color-surface); border-radius:16px; padding:12px; box-shadow:var(--shadow-md);
  display:flex; flex-direction:column; gap:8px; }
.rc-cal[hidden] { display:none; }
.rc-cal-title { font-size:15px; font-weight:600; }
.rc-cal-grid { display:grid; grid-template-columns:repeat(7, minmax(0, 1fr)); gap:2px; }
.rc-cal-wd { text-align:center; font-size:11px; color:var(--color-neutral-500); }
.rc-cal-day { height:44px; border:0; border-radius:10px; background:transparent; color:var(--color-text); padding:0;
  display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; font:inherit; font-size:14px; cursor:pointer; }
.rc-cal-day:hover:not(:disabled) { background:var(--color-neutral-900); }
.rc-cal-day:disabled { color:var(--color-neutral-700); cursor:default; }
.rc-cal-day.today { box-shadow:inset 0 0 0 1.5px var(--color-accent); }
.rc-cal-day.sel, .rc-cal-day.sel:hover { background:var(--color-accent); color:var(--color-bg); }
.rc-cal-day i { width:5px; height:5px; border-radius:99px; }
.rc-cal-foot { border-top:1px solid var(--color-divider-soft); padding-top:8px; }
.rc-icbtn { width:44px; height:44px; border:0; border-radius:10px; background:transparent; color:var(--color-neutral-300);
  display:grid; place-items:center; cursor:pointer; }
.rc-icbtn:disabled { opacity:.35; cursor:default; }
.rc-textbtn { min-height:36px; padding:0 10px; border:0; border-radius:8px; background:transparent; color:var(--color-accent-400);
  font:inherit; font-size:13px; font-weight:500; cursor:pointer; }
.rc-textbtn-fill { background:var(--color-neutral-800); color:var(--color-text); }
.rc-pills { display:flex; gap:6px; flex-wrap:wrap; }
.rc-pill { min-height:34px; padding:0 13px; border-radius:999px; border:1px solid var(--color-divider); background:transparent;
  color:var(--color-neutral-300); font:inherit; font-size:12px; font-weight:500; cursor:pointer; }
.rc-pill[aria-pressed=true] { background:var(--color-accent); border-color:var(--color-accent); color:var(--color-bg); }
.rc-row { display:flex; flex-direction:column; gap:14px; }
[data-sec][hidden] { display:none !important; }
.rc-row:not(:has(> :not([hidden]))) { display:none; }
.rc-card { background:var(--color-surface); border-radius:14px; padding:16px; box-shadow:var(--shadow-sm);
  display:flex; flex-direction:column; gap:14px; min-width:0; }
.rc-card-tight { padding:6px 16px; gap:0; }
.rc-sp { display:flex; justify-content:space-between; align-items:center; gap:8px; }
.rc-kick { font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--color-neutral-500); }
.rc-small { font-size:12px; color:var(--color-neutral-500); }
.rc-unit { font-size:11px; font-weight:400; color:var(--color-neutral-500); }
.rc-big { font-size:32px; font-weight:600; line-height:1; }
.rc-stat-l { font-size:11px; color:var(--color-neutral-500); }
.rc-stat-v { font-size:20px; font-weight:600; line-height:1.15; }
.rc-stat-v.rc-lg { font-size:24px; }
.rc-stat-m { font-size:16px; font-weight:600; }
.rc-inset { background:var(--color-inset); border-radius:10px; padding:10px 12px; }
.rc-note { font-size:12px; color:var(--color-neutral-300); }
.rc-ch { display:flex; align-items:center; gap:10px; }
.rc-ic { width:30px; height:30px; border-radius:9px; display:grid; place-items:center; flex:0 0 auto; }
.rc-h2 { flex:1; font-size:17px; font-weight:600; }
.rc-hv { display:flex; align-items:baseline; gap:6px; }
.rc-hv-n { font-size:22px; font-weight:600; }
.rc-pillstat { font-size:12px; font-weight:500; padding:4px 10px; border-radius:999px; }
.rc-hero { display:flex; gap:16px; align-items:center; }
.rc-ring { width:116px; height:116px; border-radius:50%; flex:0 0 auto; display:grid; place-items:center; }
.rc-ring-in { width:96px; height:96px; border-radius:50%; background:var(--color-surface);
  display:flex; flex-direction:column; align-items:center; justify-content:center; }
.rc-ring-v { font-size:36px; font-weight:600; line-height:1; }
.rc-ring-l { font-size:12px; font-weight:500; margin-top:4px; }
.rc-hero-t { flex:1; min-width:0; display:flex; flex-direction:column; gap:6px; }
.rc-headline { font-size:18px; font-weight:600; line-height:1.25; }
.rc-body { font-size:13px; color:var(--color-neutral-400); line-height:1.45; }
.rc-tiles, .rc-3 { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:8px; }
.rc-4 { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:8px; }
.rc-tile { background:var(--color-inset); border-radius:10px; padding:10px 12px; }
.rc-details summary { min-height:44px; display:flex; align-items:center; justify-content:space-between; cursor:pointer; list-style:none; }
.rc-details summary::-webkit-details-marker { display:none; }
.rc-toggle { display:grid; color:var(--color-accent-400); transition:transform .15s ease; }
.rc-details[open] .rc-toggle { transform:rotate(180deg); }
.rc-facs { display:flex; flex-direction:column; gap:14px; padding:4px 0 12px; }
.rc-fac { display:flex; flex-direction:column; gap:6px; }
.rc-fac-t { font-size:13px; align-items:baseline; }
.rc-fac-t b { font-weight:500; margin-left:6px; }
.rc-track { height:6px; border-radius:99px; background:var(--color-neutral-800); overflow:hidden; }
.rc-track div { height:100%; border-radius:99px; }
.rc-times { text-align:right; font-size:12px; color:var(--color-neutral-400); }
.rc-hyp-wrap { display:flex; gap:8px; }
.rc-hyp-l { display:flex; flex-direction:column; width:40px; flex:0 0 auto; }
.rc-hyp-l span { height:16px; font-size:10px; line-height:16px; color:var(--color-neutral-500); }
.rc-hyp { position:relative; height:64px; }
.rc-hyp i { position:absolute; left:0; right:0; height:1px; background:var(--color-divider-soft); }
.rc-segs { position:absolute; inset:0; display:flex; align-items:flex-start; }
.rc-seg { height:12px; min-width:1px; border-radius:2px; transition:opacity .15s ease; }
.rc-hyp[data-focus=deep] .rc-seg:not([data-seg=deep]), .rc-hyp[data-focus=light] .rc-seg:not([data-seg=light]),
.rc-hyp[data-focus=rem] .rc-seg:not([data-seg=rem]), .rc-hyp[data-focus=awake] .rc-seg:not([data-seg=awake]) { opacity:.18; }
.rc-axis { margin-top:6px; font-size:10px; color:var(--color-neutral-500); }
.rc-stages { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:8px; }
.rc-stage { min-height:48px; border:1px solid transparent; border-radius:10px; background:var(--color-inset); padding:8px 10px;
  display:flex; align-items:center; gap:8px; cursor:pointer; text-align:left; font:inherit; font-size:13px; color:var(--color-text); }
.rc-stage[aria-pressed=true] { border-color:var(--color-accent); background:color-mix(in srgb, var(--color-accent) 16%, transparent); }
.rc-stage b { font-weight:600; }
.rc-stage-n { flex:1; }
.rc-sw { width:10px; height:10px; border-radius:3px; flex:0 0 auto; }
.rc-pct { width:30px; text-align:right; font-size:11px; }
.rc-hrv { display:flex; gap:8px; }
.rc-hrv-axis { position:relative; width:20px; height:132px; flex:0 0 auto; font-size:10px; color:#4fae72; }
.rc-hrv-axis span { position:absolute; right:0; line-height:14px; }
.rc-hrv-plot { position:relative; height:132px; }
.rc-band { position:absolute; left:0; right:0; border-radius:6px; background:rgba(79,174,114,.14); border:1px dashed rgba(79,174,114,.5); }
.rc-avg { position:absolute; left:0; right:0; height:0; border-top:1.5px dashed color-mix(in srgb, var(--color-text) 60%, transparent); }
.rc-dots { position:relative; display:grid; grid-template-columns:repeat(7, minmax(0, 1fr)); gap:6px; align-items:end; height:100%; }
.rc-dot-days { height:auto; margin-top:6px; text-align:center; font-size:10px; color:var(--color-neutral-500); }
.rc-dot { height:132px; border:0; background:transparent; padding:0; cursor:pointer;
  display:flex; flex-direction:column; justify-content:flex-end; align-items:center; font:inherit; color:var(--color-neutral-500); }
.rc-dot-v { font-size:10px; line-height:14px; margin-bottom:3px; }
.rc-dot-d { width:10px; height:10px; border-radius:50%; flex:0 0 auto; transition:transform .15s ease; }
.rc-dot-s { width:2px; display:block; }
.rc-dot.on { color:var(--color-text); }
.rc-dot.on .rc-dot-v { font-weight:600; }
.rc-dot.on .rc-dot-d { transform:scale(1.4); box-shadow:0 0 0 1.5px var(--color-surface), 0 0 0 3px var(--color-text); }
.rc-pinned { display:flex; align-items:center; justify-content:space-between; gap:8px; font-size:12px; color:var(--color-neutral-300);
  background:color-mix(in srgb, var(--color-accent) 12%, transparent); border-radius:10px; padding:4px 4px 4px 12px; }
.rc-pinned[hidden] { display:none; }
.rc-pinned b { font-weight:600; }
.rc-pinned button { min-height:36px; padding:0 12px; border:0; border-radius:8px; background:transparent;
  color:var(--color-accent-400); font:inherit; font-size:12px; font-weight:500; cursor:pointer; }
.rc-legend { display:flex; flex-wrap:wrap; gap:8px 14px; font-size:11px; color:var(--color-neutral-500); }
.rc-legend span { display:flex; align-items:center; gap:6px; }
.rc-legend i { width:10px; height:10px; border-radius:3px; display:inline-block; }
.rc-legend i.rc-lg-band { width:14px; height:9px; background:rgba(79,174,114,.14); border:1px dashed rgba(79,174,114,.6); }
.rc-legend i.rc-lg-avg { width:14px; height:0; border-radius:0; border-top:1.5px dashed color-mix(in srgb, var(--color-text) 70%, transparent); }
.rc-readout { font-size:12px; align-items:baseline; }
.rc-readout b { font-size:14px; font-weight:600; }
.rc-hours { display:flex; gap:2px; height:84px; }
.rc-hr { flex:1 1 0px; min-width:0; border:0; padding:0; border-radius:3px; background:var(--color-inset); cursor:pointer;
  display:flex; flex-direction:column; justify-content:flex-end; overflow:hidden; }
.rc-hr.empty { background:color-mix(in srgb, var(--color-text) 4%, transparent); }
.rc-hr.on { box-shadow:inset 0 0 0 1.5px var(--color-text); }
.rc-hr span { display:block; width:100%; }
.rc-stressbars .rc-hr span { border-radius:3px 3px 0 0; }
.rc-zones { display:flex; flex-direction:column; gap:10px; }
.rc-stack { display:flex; height:8px; gap:2px; border-radius:4px; overflow:hidden; }
.rc-zl { display:flex; align-items:center; gap:5px; font-size:11px; }
.rc-zl i { width:8px; height:8px; border-radius:2px; }
.rc-vitals { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px; }
.rc-vital { padding:14px; gap:10px; }
.rc-vl { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--color-neutral-500); }
.rc-vl span { display:grid; }
.rc-big.rc-v { font-size:26px; }
.rc-mini { display:flex; gap:4px; height:40px; align-items:flex-end; }
.rc-mbar { flex:1; min-width:0; height:100%; padding:0; border:0; background:transparent; cursor:pointer; display:flex; align-items:flex-end; }
.rc-mbar:disabled { cursor:default; }
.rc-mbar span { display:block; width:100%; border-radius:3px; }
.rc-mbar.on span { box-shadow:0 0 0 1.5px var(--color-surface), 0 0 0 3px var(--color-text); }
.rc-mini-days { display:flex; gap:4px; margin-top:6px; font-size:10px; color:var(--color-neutral-500); }
.rc-mini-days span { flex:1; text-align:center; }
.rc-mini-days .on { color:var(--color-text); font-weight:600; }
@media (min-width: 900px) {
  .rc { max-width:1160px; padding:24px 24px 48px; gap:20px; }
  .rc-row { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:20px; align-items:stretch; }
  .rc-row:has(> [hidden]) { grid-template-columns:minmax(0, 1fr); }
  .rc-card { padding:20px; }
  .rc-card-tight { padding:8px 20px; }
  .rc-h1 { font-size:32px; }
  .rc-head { justify-content:flex-start; gap:16px; }
  .rc-cal { left:0; right:auto; }
  .rc-hours { height:120px; gap:3px; }
  .rc-ring { width:140px; height:140px; }
  .rc-ring-in { width:116px; height:116px; }
  .rc-ring-v { font-size:44px; }
  .rc-headline { font-size:22px; }
  .rc-body { font-size:14px; }
  .rc-facs { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:18px 24px; }
  .rc-vitals { gap:20px; }
}
"""


_RECOVERY_JS = r"""
(function () {
  // Picking another day swaps the page's content in place (no reload, so the
  // scroll position stays); the page script below runs again on the new copy.
  var swapSeq = 0;
  function swapDay(url, push) {
    var seq = ++swapSeq, y = window.scrollY;
    function fallback() { window.location.href = url; }
    fetch(url, { credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) throw new Error('status ' + res.status);
      return res.text();
    }).then(function (text) {
      if (seq !== swapSeq) return;                       // a newer pick superseded this one
      var m = /window\.__RC\s*=\s*(\{[^\n]*\});/.exec(text);
      var doc = new DOMParser().parseFromString(text, 'text/html');
      var next = doc.querySelector('main.rc'), cur = document.querySelector('main.rc');
      if (!m || !next || !cur || !next.querySelector('#rc-date-btn')) throw new Error('unexpected page');
      window.__RC = JSON.parse(m[1]);
      cur.replaceWith(document.importNode(next, true));
      if (push) history.pushState({ rc: 1 }, '', url);
      window.__rcInit();
      window.scrollTo(0, y);
    }).catch(fallback);
  }
  window.addEventListener('popstate', function () { if (window.__RC) swapDay(window.location.href, false); });

  function init() {
  var C = window.__RC;
  if (!C) return;
  function all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function one(sel, root) { return (root || document).querySelector(sel); }

  // ── Section filter pills: none picked = All ──
  var KEY = 'rc-filters', filters = [];
  try { filters = JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch (e) { filters = []; }
  if (!Array.isArray(filters)) filters = [];
  function applyFilters() {
    all('[data-filter]').forEach(function (b) {
      var k = b.getAttribute('data-filter');
      b.setAttribute('aria-pressed', (k === 'all' ? filters.length === 0 : filters.indexOf(k) >= 0) ? 'true' : 'false');
    });
    all('[data-sec]').forEach(function (s) {
      s.hidden = filters.length > 0 && filters.indexOf(s.getAttribute('data-sec')) < 0;
    });
    try { sessionStorage.setItem(KEY, JSON.stringify(filters)); } catch (e) {}
  }
  all('[data-filter]').forEach(function (b) {
    b.addEventListener('click', function () {
      var k = b.getAttribute('data-filter');
      if (k === 'all') filters = [];
      else {
        var i = filters.indexOf(k);
        if (i >= 0) filters.splice(i, 1); else filters.push(k);
        if (filters.length >= C.sections) filters = [];
      }
      applyFilters();
    });
  });
  applyFilters();

  // ── Sleep stage highlight ──
  var hyp = one('.rc-hyp'), note = one('[data-stage-note]');
  var defaultNote = note ? note.textContent : '';
  all('[data-stage]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var k = btn.getAttribute('data-stage'), on = btn.getAttribute('aria-pressed') !== 'true';
      all('[data-stage]').forEach(function (b) { b.setAttribute('aria-pressed', on && b === btn ? 'true' : 'false'); });
      if (hyp) hyp.setAttribute('data-focus', on ? k : '');
      if (note) note.textContent = on ? btn.getAttribute('data-note') : defaultNote;
    });
  });

  // ── Body battery + stress: one selected hour across both charts ──
  function setText(sel, text) { all(sel).forEach(function (el) { el.textContent = text; }); }
  function pickHour(h) {
    all('[data-hour]').forEach(function (b) { b.classList.toggle('on', +b.getAttribute('data-hour') === h); });
    setText('[data-hour-label]', C.labels[h]);
    setText('[data-bb-at]', C.bb[h] == null ? '—' : C.bb[h]);
    setText('[data-bb-dir]', C.bbDir[h]);
    setText('[data-stress-at]', C.stress[h] == null ? '—' : C.stress[h]);
    setText('[data-stress-dir]', C.stressDir[h]);
  }
  all('[data-hour]').forEach(function (b) {
    b.addEventListener('click', function () { pickHour(+b.getAttribute('data-hour')); });
  });

  // ── Date picker ──
  var btn = document.getElementById('rc-date-btn'), cal = document.getElementById('rc-cal');
  if (!btn || !cal) return;
  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
                'October', 'November', 'December'];
  var view = { y: +C.date.slice(0, 4), m: +C.date.slice(5, 7) - 1 };
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function iso(y, m, d) { return y + '-' + pad(m + 1) + '-' + pad(d); }
  function dotColor(score) {
    if (score == null) return 'transparent';
    return score >= 75 ? '#4fae72' : score >= 50 ? '#d9a441' : '#cf5a4e';
  }
  function go(day) {
    var u = new URL(window.location.href);
    if (day === C.today) u.searchParams.delete('date'); else u.searchParams.set('date', day);
    if (day === C.date) { open(false); return; }
    swapDay(u.toString(), true);
  }
  // The HRV dots and the resting HR / respiration bars each pick a day for the whole page.
  all('[data-go]').forEach(function (b) {
    b.addEventListener('click', function () { go(b.getAttribute('data-go')); });
  });
  function render() {
    one('[data-cal-title]', cal).textContent = MONTHS[view.m] + ' ' + view.y;
    var grid = one('[data-cal-days]', cal);
    grid.textContent = '';
    var lead = (new Date(view.y, view.m, 1).getDay() + 6) % 7;
    var count = new Date(view.y, view.m + 1, 0).getDate();
    for (var i = 0; i < lead; i++) grid.appendChild(document.createElement('span'));
    for (var d = 1; d <= count; d++) {
      var day = iso(view.y, view.m, d);
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'rc-cal-day' + (day === C.today ? ' today' : '') + (day === C.date ? ' sel' : '');
      var out = day > C.today || day < C.min;
      b.disabled = out;
      var score = C.scores[day];
      b.setAttribute('aria-label', MONTHS[view.m] + ' ' + d + (out ? '' : score != null ? ', readiness ' + score : ', no readiness score'));
      if (day === C.date) b.setAttribute('aria-current', 'date');
      var n = document.createElement('span');
      n.textContent = d;
      var dot = document.createElement('i');
      dot.style.background = day === C.date && score != null ? 'currentColor' : dotColor(score);
      b.appendChild(n); b.appendChild(dot);
      if (!out) b.addEventListener('click', (function (target) { return function () { go(target); }; })(day));
      grid.appendChild(b);
    }
    var first = iso(view.y, view.m, 1), last = iso(view.y, view.m, count);
    one('[data-cal-step="1"]', cal).disabled = last >= C.today;
    one('[data-cal-step="-1"]', cal).disabled = first <= C.min;
  }
  function open(show) {
    cal.hidden = !show;
    btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    if (show) { view = { y: +C.date.slice(0, 4), m: +C.date.slice(5, 7) - 1 }; render(); }
  }
  btn.addEventListener('click', function () { open(cal.hidden); });
  all('[data-cal-step]', cal).forEach(function (b) {
    b.addEventListener('click', function () {
      var t = new Date(view.y, view.m + +b.getAttribute('data-cal-step'), 1);
      view = { y: t.getFullYear(), m: t.getMonth() };
      render();
    });
  });
  one('[data-cal-today]', cal).addEventListener('click', function () { go(C.today); });
  one('[data-cal-close]', cal).addEventListener('click', function () { open(false); btn.focus(); });
  if (window.__rcOutside) document.removeEventListener('click', window.__rcOutside);
  window.__rcOutside = function (e) {
    if (!cal.hidden && !cal.contains(e.target) && !btn.contains(e.target)) open(false);
  };
  document.addEventListener('click', window.__rcOutside);
  cal.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { open(false); btn.focus(); }
  });
  }
  window.__rcInit = init;
  init();
})();
"""
