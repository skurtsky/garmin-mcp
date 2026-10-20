# tools/workout_steps.py
"""Structured workout steps in a plan: ``workouts[].steps``.

A plan workout has always carried its structure as prose (``humanReadable``).
``steps`` is the same session as data, so Today can draw it and it can be sent
to Garmin Connect without retyping. It is optional: a workout without it shows
its text as before.

Run, bike and swim share one shape; targets name zones (or % of FTP) rather
than fixed numbers, so like the ``{{run-pace:5a}}`` tokens they follow the
plan's current thresholds::

    [{"kind": "warmup", "durationS": 900, "target": {"zone": "1-2"}},
     {"kind": "repeat", "count": 3, "steps": [
         {"kind": "work", "durationS": 480, "target": {"pctFtp": [88, 92]}},
         {"kind": "recovery", "durationS": 240, "target": {"zone": "1"}}]},
     {"kind": "cooldown", "durationS": 600}]

* ``kind``: warmup, work, recovery, rest, cooldown — or repeat, with ``count``
  and ``steps`` (one level; no repeat inside a repeat).
* At most one of ``durationS`` / ``distanceM``; neither means "until lap".
* ``target`` (optional), exactly one of: ``zone`` (bike power, run pace or
  swim CSS-pace zone: 1–4, 5a–5c, ss for bike sweet spot, strides for run;
  swim 1–5; a range like "1-2"), ``hrZone`` (run/bike, 1–4, 5a–5c),
  ``pctFtp`` (bike, 75 or [88, 92]) or ``rpe`` (1–10).
* ``note``; swim steps may add ``stroke`` and ``equipment``.

Strength has no graph; its steps are exercises::

    [{"kind": "exercise", "exercise": "BARBELL_BACK_SQUAT", "sets": 3, "reps": 8,
      "weightKg": 60, "restS": 90},
     {"kind": "rest", "durationS": 120}]

(``reps`` or ``durationS``; ``weightKg``/``weightLb`` or a per-set
``setWeightsKg``/``setWeightsLb``; ``warmup: true`` marks warm-up sets.)

This module is pure: :func:`clean_steps` validates (raising ``PlanError``),
:func:`resolve` turns steps into what Today shows, and :func:`to_garmin`
into the step list ``tools.workout.create_workout`` takes.
"""
from tools import plan_doc
from tools.plan_doc import PlanError

ENDURANCE = ("run", "bike", "swim")
KINDS = ("warmup", "work", "recovery", "rest", "cooldown")
STROKES = ("free", "back", "breast", "fly", "im", "drill", "kick", "pull", "choice")
EQUIPMENT = ("pullbuoy", "paddles", "fins", "snorkel", "kickboard")
MAX_STEPS = 80          # after expanding repeats
MAX_REPEAT = 50

# Zone codes in order, per sport and target kind.
_POWER_ZONES = [r[0] for r in plan_doc.BIKE_POWER_PCT_TABLE]
_HR_ZONES = [r[0] for r in plan_doc.HR_PCT_TABLE]
_RUN_ZONES = [r[0] for r in plan_doc.RUN_PACE_OFFSET_TABLE]
_SWIM_ZONES = [r[0] for r in plan_doc.SWIM_PACE_OFFSET_TABLE]
_ZONE_SPECIALS = {"bike": "ss", "run": "strides"}

# Garmin can take these sports now; swim (pool lengths, strokes) comes later.
GARMIN_SPORT = {"run": "running", "bike": "cycling", "strength": "strength_training"}

_ENDURANCE_KEYS = {"kind", "durationS", "distanceM", "target", "note", "stroke", "equipment"}
_REPEAT_KEYS = {"kind", "count", "steps", "note"}
_EXERCISE_KEYS = {"kind", "exercise", "sets", "reps", "durationS", "weightKg", "weightLb",
                  "setWeightsKg", "setWeightsLb", "restS", "warmup", "note"}
_STRENGTH_REST_KEYS = {"kind", "durationS", "note"}


# ── VALIDATION ────────────────────────────────────────────────────────────────

def _number(value, where: str, *, integer=False, low=None, high=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{where} must be a number.")
    if integer and int(value) != value:
        raise PlanError(f"{where} must be a whole number.")
    if positive and value <= 0:
        raise PlanError(f"{where} must be more than 0.")
    if low is not None and value < low or high is not None and value > high:
        raise PlanError(f"{where} must be from {low} to {high}.")
    return int(value) if integer else value


def _zone_list(sport: str, kind: str) -> list[str]:
    if kind == "hrZone":
        return _HR_ZONES
    return {"bike": _POWER_ZONES, "run": _RUN_ZONES, "swim": _SWIM_ZONES}[sport]


def parse_zone(sport: str, kind: str, value, where: str = "zone") -> tuple[str, str]:
    """(low, high) zone codes of "3", "1-2" or "ss"; PlanError if not valid
    for the sport."""
    text = str(value).strip().lower().replace("–", "-").replace("z", "")
    special = _ZONE_SPECIALS.get(sport) if kind == "zone" else None
    if special and text == special:
        return special, special
    codes = _zone_list(sport, kind)
    parts = [p.strip() for p in text.split("-")]
    if len(parts) > 2 or any(p not in codes for p in parts):
        allowed = ", ".join(codes + ([special] if special else []))
        raise PlanError(f"{where} {value!r} isn't a {sport} zone ({allowed}, or a range like 1-2).")
    lo, hi = parts[0], parts[-1]
    if codes.index(lo) > codes.index(hi):
        raise PlanError(f"{where} {value!r} runs backwards — write the lower zone first.")
    return lo, hi


def _clean_target(sport: str, target, where: str) -> dict:
    if not isinstance(target, dict):
        raise PlanError(f"{where} must be an object like {{\"zone\": \"2\"}}.")
    keys = set(target) & {"zone", "hrZone", "pctFtp", "rpe"}
    if len(keys) != 1 or set(target) - keys:
        raise PlanError(f"{where} takes exactly one of zone, hrZone, pctFtp or rpe.")
    (key,) = keys
    value = target[key]
    if key in ("zone", "hrZone"):
        if key == "hrZone" and sport == "swim":
            raise PlanError(f"{where}: swim steps take a zone (CSS pace) or rpe, not hrZone.")
        lo, hi = parse_zone(sport, key, value, f"{where}.{key}")
        return {key: lo if lo == hi else f"{lo}-{hi}"}
    if key == "pctFtp":
        if sport != "bike":
            raise PlanError(f"{where}: pctFtp is for bike steps.")
        low, high = plan_doc.FTP_PCT_BOUNDS
        pair = value if isinstance(value, list) else [value]
        if len(pair) not in (1, 2):
            raise PlanError(f"{where}.pctFtp is one percent or [low, high].")
        pair = [_number(v, f"{where}.pctFtp", integer=True, low=low, high=high) for v in pair]
        if pair[0] > pair[-1]:
            raise PlanError(f"{where}.pctFtp runs backwards — write the lower percent first.")
        return {"pctFtp": pair[0] if len(pair) == 1 or pair[0] == pair[1] else pair}
    return {"rpe": _number(value, f"{where}.rpe", low=1, high=10)}


def _check_keys(step: dict, allowed: set, where: str) -> None:
    extra = sorted(set(step) - allowed)
    if extra:
        raise PlanError(f"{where} has unknown field(s) {', '.join(extra)} (allowed: {', '.join(sorted(allowed))}).")


def _clean_endurance_step(sport: str, step, where: str) -> dict:
    if not isinstance(step, dict):
        raise PlanError(f"{where} must be an object.")
    kind = step.get("kind")
    if kind not in KINDS:
        raise PlanError(f"{where}.kind must be one of {', '.join(KINDS)} (or repeat).")
    _check_keys(step, _ENDURANCE_KEYS, where)
    out = {"kind": kind}
    if step.get("durationS") is not None and step.get("distanceM") is not None:
        raise PlanError(f"{where}: give durationS or distanceM, not both.")
    if step.get("durationS") is not None:
        out["durationS"] = _number(step["durationS"], f"{where}.durationS", positive=True, high=86400)
    if step.get("distanceM") is not None:
        out["distanceM"] = _number(step["distanceM"], f"{where}.distanceM", positive=True, high=500000)
    if step.get("target") is not None:
        out["target"] = _clean_target(sport, step["target"], f"{where}.target")
    if step.get("note") not in (None, ""):
        if not isinstance(step["note"], str):
            raise PlanError(f"{where}.note must be text.")
        out["note"] = step["note"]
    if step.get("stroke") is not None or step.get("equipment") is not None:
        if sport != "swim":
            raise PlanError(f"{where}: stroke and equipment are for swim steps.")
        if step.get("stroke") is not None:
            if step["stroke"] not in STROKES:
                raise PlanError(f"{where}.stroke must be one of {', '.join(STROKES)}.")
            out["stroke"] = step["stroke"]
        if step.get("equipment") is not None:
            gear = step["equipment"]
            if not isinstance(gear, list) or any(g not in EQUIPMENT for g in gear):
                raise PlanError(f"{where}.equipment is a list of {', '.join(EQUIPMENT)}.")
            if gear:
                out["equipment"] = list(dict.fromkeys(gear))
    return out


def _clean_strength_step(step, where: str) -> dict:
    from tools.exercise_db import validate_exercise
    if not isinstance(step, dict):
        raise PlanError(f"{where} must be an object.")
    kind = step.get("kind")
    if kind == "rest":
        _check_keys(step, _STRENGTH_REST_KEYS, where)
        out = {"kind": "rest"}
        if step.get("durationS") is not None:
            out["durationS"] = _number(step["durationS"], f"{where}.durationS", positive=True, high=3600)
        if step.get("note"):
            out["note"] = str(step["note"])
        return out
    if kind != "exercise":
        raise PlanError(f"{where}.kind must be exercise or rest for strength.")
    _check_keys(step, _EXERCISE_KEYS, where)
    try:
        name, _ = validate_exercise(str(step.get("exercise") or ""))
    except ValueError as e:
        raise PlanError(f"{where}.exercise: {e}") from None
    out = {"kind": "exercise", "exercise": name,
           "sets": _number(step.get("sets", 1), f"{where}.sets", integer=True, low=1, high=20)}
    if step.get("reps") is not None and step.get("durationS") is not None:
        raise PlanError(f"{where}: give reps or durationS, not both.")
    if step.get("reps") is not None:
        out["reps"] = _number(step["reps"], f"{where}.reps", integer=True, low=1, high=200)
    if step.get("durationS") is not None:
        out["durationS"] = _number(step["durationS"], f"{where}.durationS", positive=True, high=3600)
    if step.get("weightKg") is not None and step.get("weightLb") is not None:
        raise PlanError(f"{where}: give weightKg or weightLb, not both.")
    for key in ("weightKg", "weightLb"):
        if step.get(key) is not None:
            out[key] = _number(step[key], f"{where}.{key}", low=0, high=1000)
    if step.get("setWeightsKg") is not None and step.get("setWeightsLb") is not None:
        raise PlanError(f"{where}: give setWeightsKg or setWeightsLb, not both.")
    for key in ("setWeightsKg", "setWeightsLb"):
        if step.get(key) is not None:
            wave = step[key]
            if not isinstance(wave, list) or not wave:
                raise PlanError(f"{where}.{key} is a list with one weight per set.")
            out[key] = [_number(v, f"{where}.{key}", low=0, high=1000) for v in wave]
            if len(wave) != out["sets"]:
                raise PlanError(f"{where}.{key} has {len(wave)} weights for {out['sets']} sets.")
    if step.get("restS") is not None:
        out["restS"] = _number(step["restS"], f"{where}.restS", low=0, high=3600)
    if step.get("warmup"):
        out["warmup"] = True
    if step.get("note"):
        out["note"] = str(step["note"])
    return out


def clean_steps(sport: str | None, steps) -> list[dict] | None:
    """Validated, normalised steps for a workout of ``sport``, or None to
    remove them. Raises PlanError naming the step at fault."""
    if steps is None or steps == []:
        return None
    if not isinstance(steps, list):
        raise PlanError("steps must be a list.")
    if sport == "strength":
        return [_clean_strength_step(s, f"steps[{i}]") for i, s in enumerate(steps)]
    if sport not in ENDURANCE:
        raise PlanError(f"steps are for run, bike, swim and strength workouts, not {sport or 'this sport'}.")
    out, total = [], 0
    for i, step in enumerate(steps):
        where = f"steps[{i}]"
        if isinstance(step, dict) and step.get("kind") == "repeat":
            _check_keys(step, _REPEAT_KEYS, where)
            count = _number(step.get("count"), f"{where}.count", integer=True, low=1, high=MAX_REPEAT)
            inner = step.get("steps")
            if not isinstance(inner, list) or not inner:
                raise PlanError(f"{where}.steps must be a non-empty list.")
            if any(isinstance(s, dict) and s.get("kind") == "repeat" for s in inner):
                raise PlanError(f"{where}: a repeat can't contain another repeat.")
            rep = {"kind": "repeat", "count": count,
                   "steps": [_clean_endurance_step(sport, s, f"{where}.steps[{j}]") for j, s in enumerate(inner)]}
            if step.get("note"):
                rep["note"] = str(step["note"])
            out.append(rep)
            total += count * len(inner)
        else:
            out.append(_clean_endurance_step(sport, step, where))
            total += 1
    if total > MAX_STEPS:
        raise PlanError(f"steps expand to {total} steps; keep it to {MAX_STEPS}.")
    return out


# ── RESOLUTION (what Today shows) ─────────────────────────────────────────────

# Rough intensity (fraction of threshold) for a zone when the threshold
# itself isn't set, so the graph's shape still reads right.
# Pace zones also use it for bar height: speed ratios bunch every zone
# between ~0.9 and 1.05, which hides the shape of the session.
_GENERIC = {"1": .6, "2": .72, "3": .84, "4": .95, "5a": 1.0, "5b": 1.08, "5c": 1.18,
            "ss": .91, "strides": 1.15}
_GENERIC_SWIM = {"1": .6, "2": .72, "3": .86, "4": 1.0, "5": 1.08}
_KIND_DEFAULT = {"warmup": .6, "work": .8, "recovery": .5, "rest": .3, "cooldown": .55}
# Fallback speeds when a distance step's pace can't be worked out.
_DEFAULT_SEC_PER_KM = {"run": 360, "bike": 120, "swim": 1200}

# Bar colours by intensity band, the Fitness tab's zone palette.
_BANDS = ((.56, "#6c93b0", "Z1"), (.76, "#5eb8ad", "Z2"), (.91, "#8fb85e", "Z3"),
          (1.0, "#d9b35a", "Z4"), (9.9, "#d9705e", "Z5"))
REST_COLOR = "#7c8194"


def _row(table, code):
    return next((r for r in table if r[0] == code), None)


def _mid(lo, hi):
    return (lo + hi) / 2


def _span(lo, hi, fmt, suffix):
    a, b = (fmt(v) for v in sorted((lo, hi)))
    return (a if a == b else f"{a}–{b}") + suffix


def _zone_label(lo: str, hi: str) -> str:
    if lo in ("ss", "strides"):
        return "Sweet spot" if lo == "ss" else "Strides"
    return f"Z{lo}" if lo == hi else f"Z{lo}–{hi}"


def _resolve_target(sport: str, target: dict | None, t: dict) -> dict:
    """{text, intensity, sec_per_km} for a step's target from thresholds ``t``:
    text like "209–219 W · 88–92% FTP", intensity as a fraction of threshold
    (for the bar height) and the pace it implies (for distance steps)."""
    if not target:
        return {"text": "", "intensity": None, "sec_per_km": None}
    ftp = t.get("ftp")
    if "rpe" in target:
        return {"text": f"RPE {plan_doc._tidy(target['rpe'], 1)}", "intensity": target["rpe"] / 9, "sec_per_km": None}
    if "pctFtp" in target:
        pair = target["pctFtp"] if isinstance(target["pctFtp"], list) else [target["pctFtp"]] * 2
        pct = _span(pair[0], pair[1], str, "% FTP")
        watts = _span(plan_doc._pct(ftp, pair[0]), plan_doc._pct(ftp, pair[1]), str, " W") if ftp else ""
        return {"text": f"{watts} · {pct}" if watts else pct, "intensity": _mid(*pair) / 100, "sec_per_km": None}
    kind = "hrZone" if "hrZone" in target else "zone"
    lo, hi = parse_zone(sport, kind, target[kind])
    label = _zone_label(lo, hi)
    if kind == "hrZone":
        lthr = t.get("runLthr" if sport == "run" else "bikeLthr")
        a, b = _row(plan_doc.HR_PCT_TABLE, lo), _row(plan_doc.HR_PCT_TABLE, hi)
        low = a[2] or 70      # zone 1 has no floor
        text = _span(plan_doc._pct(lthr, low), plan_doc._pct(lthr, b[3]), str, " bpm") if lthr else ""
        return {"text": f"{text} · {label} HR" if text else f"{label} HR",
                "intensity": _mid(low, b[3]) / 100, "sec_per_km": None}
    if sport == "bike":
        if lo == "ss":
            a_pct, b_pct = plan_doc.BIKE_SWEET_SPOT_PCT
        else:
            a_pct = _row(plan_doc.BIKE_POWER_PCT_TABLE, lo)[2] or 45
            b_pct = _row(plan_doc.BIKE_POWER_PCT_TABLE, hi)[3]
        watts = _span(plan_doc._pct(ftp, a_pct), plan_doc._pct(ftp, b_pct), str, " W") if ftp else ""
        return {"text": f"{watts} · {label}" if watts else label, "intensity": _mid(a_pct, b_pct) / 100,
                "sec_per_km": None}
    if sport == "run":
        base = plan_doc.single_pace_seconds(t.get("thresholdPace"))
        if lo == "strides":
            off_lo, off_hi = plan_doc.STRIDE_PACE_OFFSET
        else:
            off_lo = _row(plan_doc.RUN_PACE_OFFSET_TABLE, hi)[2]   # the faster zone's fast end
            off_hi = _row(plan_doc.RUN_PACE_OFFSET_TABLE, lo)[3]
        intensity = _mid(_GENERIC[lo], _GENERIC[hi])
        if base is None:
            return {"text": label, "intensity": intensity, "sec_per_km": None}
        pace = base + _mid(off_lo, off_hi)
        return {"text": f"{_span(base + off_lo, base + off_hi, plan_doc.fmt_mmss, '/km')} · {label}",
                "intensity": intensity, "sec_per_km": pace}
    # swim: CSS-relative pace per 100 m
    css = plan_doc.single_pace_seconds(t.get("css"))
    off_lo = _row(plan_doc.SWIM_PACE_OFFSET_TABLE, hi)[2]
    off_hi = _row(plan_doc.SWIM_PACE_OFFSET_TABLE, lo)[3]
    intensity = _mid(_GENERIC_SWIM[lo], _GENERIC_SWIM[hi])
    if css is None:
        return {"text": label, "intensity": intensity, "sec_per_km": None}
    pace = css + _mid(off_lo, off_hi)
    return {"text": f"{_span(css + off_lo, css + off_hi, plan_doc.fmt_mmss, '/100m')} · {label}",
            "intensity": intensity, "sec_per_km": pace * 10}


def _fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def _fmt_dist(meters: float) -> str:
    return f"{plan_doc._tidy(meters / 1000, 2)} km" if meters >= 1000 else f"{plan_doc._tidy(meters, 0)} m"


def _band(intensity: float) -> tuple[str, str]:
    return next((c, z) for top, c, z in _BANDS if intensity < top)


_KIND_LABEL = {"warmup": "Warm-up", "work": "Work", "recovery": "Recovery", "rest": "Rest", "cooldown": "Cool-down"}
_STROKE_LABEL = {"im": "IM", "pullbuoy": "pull buoy"}


def _endurance_bar(sport: str, step: dict, t: dict) -> dict:
    target = _resolve_target(sport, step.get("target"), t)
    intensity = target["intensity"] if target["intensity"] is not None else _KIND_DEFAULT[step["kind"]]
    seconds, estimated = step.get("durationS"), False
    if seconds is None:
        estimated = True
        per_km = target["sec_per_km"] or _DEFAULT_SEC_PER_KM[sport]
        seconds = step["distanceM"] / 1000 * per_km if step.get("distanceM") else 60
    amount = (_fmt_dist(step["distanceM"]) if step.get("distanceM")
              else _fmt_dur(step["durationS"]) if step.get("durationS") else "Until lap")
    # How long a distance step should take at its target (or a typical) pace.
    approx = f"~{_fmt_dur(seconds)}" if estimated and step.get("distanceM") else ""
    extras = []
    if step.get("stroke"):
        extras.append(_STROKE_LABEL.get(step["stroke"], step["stroke"]).capitalize())
    if step.get("equipment"):
        extras.append(" + ".join(_STROKE_LABEL.get(g, g) for g in step["equipment"]))
    rest = step["kind"] == "rest"
    color, zone = (REST_COLOR, "") if rest else _band(intensity)
    return {"kind": step["kind"], "label": _KIND_LABEL[step["kind"]], "amount": amount, "approx": approx,
            "target": target["text"], "extras": " · ".join(extras), "note": step.get("note") or "",
            "seconds": seconds, "estimated": estimated, "intensity": round(min(max(intensity, .15), 1.6), 3),
            "color": color, "band": zone}


def resolve(plan: dict, sport: str, steps: list[dict] | None) -> dict | None:
    """What Today shows for a workout's steps, or None without steps.

    Run/bike/swim: {"graph": True, "bars": [...] (repeats expanded, each with
    seconds, intensity, colour and its text), "groups": [...] (the step list,
    repeats kept together, each pointing at its first bar), "total_s"}.
    Strength: {"graph": False, "groups": [...]} — exercises only.
    """
    if not steps:
        return None
    t = plan_doc.effective_thresholds(plan)
    if sport == "strength":
        return {"graph": False, "groups": [_strength_line(s) for s in steps]}
    if sport not in ENDURANCE:
        return None
    bars, groups = [], []
    try:
        for step in steps:
            if step.get("kind") == "repeat":
                first, children = len(bars), []
                for rep in range(step["count"]):
                    for child in step["steps"]:
                        bar = _endurance_bar(sport, child, t)
                        bar["rep"] = f"{rep + 1} of {step['count']}"
                        bars.append(bar)
                        if rep == 0:
                            children.append({**bar, "index": len(bars) - 1})
                groups.append({"repeat": step["count"], "index": first, "children": children,
                               "note": step.get("note") or ""})
            else:
                bars.append(_endurance_bar(sport, step, t))
                groups.append({**bars[-1], "index": len(bars) - 1})
    except (KeyError, TypeError, ValueError, PlanError):
        return None   # steps stored in a shape this version doesn't know
    total = sum(b["seconds"] for b in bars)
    return {"graph": True, "bars": bars, "groups": groups, "total_s": total,
            "estimated": any(b["estimated"] for b in bars)}


def _strength_line(step: dict) -> dict:
    if step.get("kind") == "rest":
        return {"kind": "rest", "label": "Rest", "amount": _fmt_dur(step["durationS"]) if step.get("durationS") else "",
                "detail": "", "note": step.get("note") or ""}
    name = str(step.get("exercise") or "").replace("_", " ").title()
    sets = step.get("sets", 1)
    each = f"{step['reps']} reps" if step.get("reps") else _fmt_dur(step["durationS"]) if step.get("durationS") else ""
    load = ""
    if step.get("setWeightsKg") or step.get("setWeightsLb"):
        unit = "kg" if step.get("setWeightsKg") else "lb"
        load = " – ".join(str(plan_doc._tidy(w, 1)) for w in step.get(f"setWeights{unit.capitalize()}")) + f" {unit}"
    elif step.get("weightKg") is not None:
        load = f"{plan_doc._tidy(step['weightKg'], 1)} kg"
    elif step.get("weightLb") is not None:
        load = f"{plan_doc._tidy(step['weightLb'], 1)} lb"
    detail = " · ".join(p for p in (f"{sets} × {each}" if each else f"{sets} sets", load,
                                    f"rest {_fmt_dur(step['restS'])}" if step.get("restS") else "") if p)
    return {"kind": "warmup" if step.get("warmup") else "exercise", "label": name,
            "amount": "Warm-up" if step.get("warmup") else "", "detail": detail, "note": step.get("note") or ""}


# ── GARMIN ────────────────────────────────────────────────────────────────────

def garmin_blocker(sport: str | None, steps) -> str | None:
    """Why a workout can't be sent to Garmin from its steps, or None if it can."""
    if sport == "swim":
        return "Swim workouts can't be sent to Garmin yet."
    if sport not in GARMIN_SPORT:
        return f"{(sport or 'This').capitalize()} workouts can't be sent to Garmin."
    if not steps:
        return "Add steps to this workout to send it to Garmin."
    return None


def _garmin_target(sport: str, target: dict | None, t: dict, where: str, required: bool) -> dict:
    """The create_workout target fields for a step, from the plan thresholds."""
    if not target or "rpe" in target:
        if required:
            raise PlanError(f"{where}: Garmin needs a {'power' if sport == 'bike' else 'pace or heart-rate'} "
                            "target on work steps (an RPE target can't be sent).")
        return {}
    if sport == "bike":
        ftp = t.get("ftp")
        if "hrZone" in target:
            if required:
                raise PlanError(f"{where}: Garmin bike steps take a power target, not heart rate.")
            return {}
        if not ftp:
            raise PlanError("Set the plan's FTP before sending bike workouts to Garmin.")
        if "pctFtp" in target:
            pair = target["pctFtp"] if isinstance(target["pctFtp"], list) else [target["pctFtp"]] * 2
        else:
            lo, hi = parse_zone(sport, "zone", target["zone"])
            pair = (plan_doc.BIKE_SWEET_SPOT_PCT if lo == "ss"
                    else (_row(plan_doc.BIKE_POWER_PCT_TABLE, lo)[2] or 45, _row(plan_doc.BIKE_POWER_PCT_TABLE, hi)[3]))
        return {"power_watts_min": plan_doc._pct(ftp, pair[0]), "power_watts_max": plan_doc._pct(ftp, pair[1])}
    # run
    if "hrZone" in target:
        lthr = t.get("runLthr")
        if not lthr:
            raise PlanError("Set the plan's run LTHR before sending heart-rate steps to Garmin.")
        lo, hi = parse_zone(sport, "hrZone", target["hrZone"])
        return {"hr_min": plan_doc._pct(lthr, _row(plan_doc.HR_PCT_TABLE, lo)[2] or 70),
                "hr_max": plan_doc._pct(lthr, _row(plan_doc.HR_PCT_TABLE, hi)[3])}
    base = plan_doc.single_pace_seconds(t.get("thresholdPace"))
    if base is None:
        raise PlanError("Set the plan's threshold pace before sending run pace steps to Garmin.")
    lo, hi = parse_zone(sport, "zone", target["zone"])
    if lo == "strides":
        off_fast, off_slow = plan_doc.STRIDE_PACE_OFFSET
    else:
        off_fast = _row(plan_doc.RUN_PACE_OFFSET_TABLE, hi)[2]
        off_slow = _row(plan_doc.RUN_PACE_OFFSET_TABLE, lo)[3]
    # create_workout: pace_min_per_km is the slow end, pace_max_per_km the fast end.
    return {"pace_min_per_km": plan_doc.fmt_mmss(base + off_slow), "pace_max_per_km": plan_doc.fmt_mmss(base + off_fast)}


def _garmin_end(step: dict) -> dict:
    if step.get("durationS"):
        return {"duration_s": int(round(step["durationS"]))}
    if step.get("distanceM"):
        return {"distance_m": step["distanceM"]}
    return {}


def _garmin_step(sport: str, step: dict, t: dict, where: str) -> dict:
    work = step["kind"] == "work"
    out = {"type": "interval" if work else step["kind"], **_garmin_end(step),
           **_garmin_target(sport, step.get("target"), t, where, required=work)}
    if work and not _garmin_end(step):
        raise PlanError(f"{where}: Garmin needs a duration or distance on work steps.")
    if step.get("note"):
        out["description"] = step["note"]
    return out


def to_garmin(plan: dict, sport: str, steps: list[dict]) -> tuple[str, list[dict]]:
    """(sport_type, steps) for ``tools.workout.create_workout``. A repeat of
    one work step (optionally followed by an untargeted timed recovery/rest)
    becomes a Garmin repeat group; any other repeat is written out in full.
    Raises PlanError when something can't be sent."""
    blocker = garmin_blocker(sport, steps)
    if blocker:
        raise PlanError(blocker)
    if sport == "strength":
        return GARMIN_SPORT[sport], [_garmin_strength(s) for s in steps]
    t = plan_doc.effective_thresholds(plan)
    out = []
    for i, step in enumerate(steps):
        where = f"Step {i + 1}"
        if step["kind"] != "repeat":
            out.append(_garmin_step(sport, step, t, where))
            continue
        inner = step["steps"]
        rest = inner[1] if len(inner) == 2 else None
        simple = (inner[0]["kind"] == "work" and len(inner) <= 2
                  and (rest is None or (rest["kind"] in ("recovery", "rest") and not rest.get("distanceM")
                                        and not rest.get("target"))))
        if simple:
            work = _garmin_step(sport, inner[0], t, f"{where} (repeat)")
            work.update(type="repeat", sets=step["count"])
            if rest and rest.get("durationS"):
                work["rest_duration_s"] = int(round(rest["durationS"]))
            out.append(work)
        else:
            for _ in range(step["count"]):
                out += [_garmin_step(sport, s, t, f"{where} (repeat)") for s in inner]
    return GARMIN_SPORT[sport], out


def _garmin_strength(step: dict) -> dict:
    if step["kind"] == "rest":
        out = {"type": "rest"}
        if step.get("durationS"):
            out["duration_s"] = step["durationS"]
        return out
    out = {"type": "warmup" if step.get("warmup") else "repeat", "sets": step.get("sets", 1),
           "exercise_name": step["exercise"]}
    for src, dst in (("reps", "reps"), ("durationS", "duration_s"), ("weightKg", "weight_kg"),
                     ("weightLb", "weight_lb"), ("setWeightsKg", "set_weights"),
                     ("setWeightsLb", "set_weights_lb"), ("restS", "rest_duration_s"), ("note", "description")):
        if step.get(src) is not None:
            out[dst] = step[src]
    return out
