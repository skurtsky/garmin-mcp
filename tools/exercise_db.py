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
import json
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "data" / "garmin_exercises.json"


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
        raise ValueError(
            f"Unknown exercise_name {name!r} (normalized {canonical!r}). "
            "It is not in the Garmin exercise reference "
            "(tools/data/garmin_exercises.json). Check spelling / use Garmin's "
            "UPPER_SNAKE_CASE naming, or add it to the reference."
        )
    return canonical, category
