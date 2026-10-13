# tools/exercise_db.py
"""
Garmin strength-exercise reference lookup.

Strength workout steps carry an ``exerciseName`` (Garmin's UPPER_SNAKE_CASE
identifier, e.g. ``BARBELL_BACK_SQUAT``) and a matching ``category`` (e.g.
``SQUAT``). Garmin silently rejects an upload whose exerciseName it doesn't
recognise, so we validate names up front and fail fast with a clear error.

The reference data lives in ``tools/data/garmin_exercises.json`` — the Garmin
Exercise Database:
https://docs.google.com/spreadsheets/d/1OaqIaBhPk4xBnkqVPYvFpj2HZNk_ISX0zHgWfA0WXkQ/edit?gid=971702464

The JSON maps each Garmin exercise category to its list of exerciseName values,
either at the top level ({"SQUAT": ["BARBELL_BACK_SQUAT", ...], ...}) or nested
under a "categories" key. To add exercises, append names under the appropriate
category; no code change is needed. Some exercises appear under more than one
category — the first one encountered wins as the default, and callers may pass an
explicit category to override it.
"""
import difflib
import json
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "data" / "garmin_exercises.json"
# Richer metadata (display name, FOUND flag, difficulty, muscles, equipment) built
# from the exercise sheet by scripts/build_exercise_db.py.
_META_PATH = Path(__file__).resolve().parent / "data" / "garmin_exercise_meta.json"


@lru_cache(maxsize=1)
def _load_catalog() -> dict[str, str]:
    """Return a {exerciseName: category} lookup built from the JSON reference."""
    raw = json.loads(_DATA_PATH.read_text())
    # Accept either a nested {"categories": {...}} form or a flat top-level
    # {category: [names]} mapping. Non-list values (e.g. "_source" metadata) are
    # ignored so the file can carry documentation keys.
    source = raw.get("categories") if isinstance(raw.get("categories"), dict) else raw
    lookup: dict[str, str] = {}
    for category, names in source.items():
        if not isinstance(names, list):
            continue
        for name in names:
            # First occurrence wins so duplicates resolve deterministically.
            lookup.setdefault(str(name).upper(), category)
    return lookup


def _normalize(name: str) -> str:
    """Normalize user input to Garmin's UPPER_SNAKE_CASE convention."""
    return name.strip().upper().replace(" ", "_").replace("-", "_")


def is_known_exercise(name: str) -> bool:
    """True if `name` (after normalization) is a recognised Garmin exercise."""
    if not name:
        return False
    return _normalize(name) in _load_catalog()


def category_for(name: str) -> str | None:
    """Return the Garmin category for `name`, or None if unknown."""
    if not name:
        return None
    return _load_catalog().get(_normalize(name))


def validate_exercise(name: str) -> tuple[str, str]:
    """
    Validate a strength exercise name against the Garmin exercise reference.

    Returns (canonical_name, category). Raises ValueError with a clear message
    if the name is empty or not recognised, so a typo fails fast instead of
    being silently rejected by Garmin as a generic payload error.
    """
    if not name:
        raise ValueError("strength step requires a non-empty exercise_name")
    canonical = _normalize(name)
    category = _load_catalog().get(canonical)
    if category is None:
        hint = suggest_exercises(canonical)
        raise ValueError(
            f"Unknown exercise_name {name!r} (normalized {canonical!r}). "
            "It is not in the Garmin exercise reference "
            "(tools/data/garmin_exercises.json). Check spelling / use Garmin's "
            "UPPER_SNAKE_CASE naming, or add it to the reference."
            + (f" Did you mean: {', '.join(hint)}? (use search_exercises to look up names)" if hint else "")
        )
    return canonical, category


# ── Search / suggestions (backed by garmin_exercise_meta.json) ────────────────

@lru_cache(maxsize=1)
def _load_meta() -> list[dict]:
    return json.loads(_META_PATH.read_text(encoding="utf-8"))


def exercise_info(name: str, category: str | None = None) -> dict | None:
    """Metadata record for an exercise name (optionally pinned to a category)."""
    canonical = _normalize(name or "")
    category = category or category_for(canonical)
    for rec in _load_meta():
        if rec["garmin_name"] == canonical and (category is None or rec["category"] == category):
            return rec
    return None


def _tokens(text: str) -> list[str]:
    return [t for t in _normalize(text).split("_") if t]


def _public(rec: dict, score: float | None = None) -> dict:
    out = {
        "exercise_name": rec["garmin_name"],
        "category": rec["category"],
        "display_name": rec["name"],
        "found_on_garmin_connect": rec["found"],
        "difficulty": rec["difficulty"],
        "muscles": rec["muscles"],
        "equipment": rec["equipment"],
        "url": rec["url"],
    }
    if score is not None:
        out["score"] = round(score, 2)
    return out


def search_exercises(
    query: str = "",
    category: str | None = None,
    equipment: str | None = None,
    muscle: str | None = None,
    found_only: bool = False,
    limit: int = 25,
) -> list[dict]:
    """
    Search the packaged Garmin exercise database.

    `query` is matched against display name, Garmin name and category (all words
    must match, as substrings; a fuzzy fallback catches typos). `category`,
    `equipment` and `muscle` are exact (case-insensitive) filters; `muscle`
    requires a score >= 1 for that muscle group and sorts by it. `found_only`
    keeps exercises that have a page on Garmin Connect. Results are best-first.
    """
    cat = _normalize(category) if category else None
    equip = _normalize(equipment) if equipment else None
    mus = _normalize(muscle) if muscle else None
    words = _tokens(query)

    pool = [
        r for r in _load_meta()
        if (not cat or r["category"] == cat)
        and (not equip or equip in r["equipment"])
        and (not mus or r["muscles"].get(mus, 0) >= 1)
        and (not found_only or r["found"])
    ]

    scored: list[tuple[float, dict]] = []
    for r in pool:
        haystack = f'{_normalize(r["name"])}_{r["garmin_name"]}_{r["category"]}'
        score = 0.0
        if words:
            if not all(w in haystack for w in words):
                continue
            canonical = _normalize(query)
            if r["garmin_name"] == canonical:
                score += 10
            elif r["garmin_name"].startswith(canonical):
                score += 4
            score += 3 * sum(w in _tokens(r["garmin_name"]) for w in words)
        if mus:
            score += r["muscles"][mus]
        score += 1 if r["found"] else 0
        scored.append((score, r))

    if words and not scored:
        # Typo fallback: fuzzy-match against Garmin names.
        for s, r in _fuzzy([r for r in pool], _normalize(query)):
            scored.append((s, r))

    scored.sort(key=lambda t: (-t[0], t[1]["garmin_name"], t[1]["category"]))
    return [_public(r, s) for s, r in scored[: max(1, limit)]]


def _fuzzy(pool: list[dict], canonical: str, cutoff: float = 0.6) -> list[tuple[float, dict]]:
    out = []
    for r in pool:
        ratio = difflib.SequenceMatcher(None, canonical, r["garmin_name"]).ratio()
        if ratio >= cutoff:
            out.append((ratio * 5 + (1 if r["found"] else 0), r))
    return out


def suggest_exercises(name: str, limit: int = 5) -> list[str]:
    """Garmin exercise names close to `name` (for 'did you mean ...' hints)."""
    canonical = _normalize(name or "")
    if not canonical:
        return []
    ranked: dict[str, float] = {}
    words = _tokens(canonical)
    for r in _load_meta():
        g = r["garmin_name"]
        ratio = difflib.SequenceMatcher(None, canonical, g).ratio()
        contains = (canonical in g or g in canonical) if len(g) > 3 else False
        overlap = len(set(words) & set(_tokens(g))) / max(len(words), 1)
        score = ratio + (0.3 if contains else 0) + 0.3 * overlap + (0.05 if r["found"] else 0)
        if ratio >= 0.6 or contains or overlap >= 0.5:
            ranked[g] = max(ranked.get(g, 0), score)
    return [g for g, _ in sorted(ranked.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def exercise_notes(name: str) -> list[str]:
    """Non-blocking advisories for an exercise Garmin accepts (e.g. FOUND=FALSE)."""
    info = exercise_info(name)
    if info is not None and not info["found"]:
        base = info["garmin_name"]
        alts = [s for s in (suggest_exercises(base.removeprefix("WEIGHTED_"), 6) + suggest_exercises(base, 6))
                if s != base]
        alts = list(dict.fromkeys(a for a in alts if (exercise_info(a) or {}).get("found")))[:3]
        msg = f"{info['garmin_name']} has no page on Garmin Connect (it still uploads)."
        if alts:
            msg += " Did you mean " + " / ".join(alts) + "?"
        return [msg]
    return []
