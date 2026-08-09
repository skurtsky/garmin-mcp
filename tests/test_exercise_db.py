# tests/test_exercise_db.py
import pytest

from tools.exercise_db import (
    validate_exercise,
    is_known_exercise,
    category_for,
)


def test_validate_known_exercise_returns_canonical_and_category():
    canonical, category = validate_exercise("BARBELL_BACK_SQUAT")
    assert canonical == "BARBELL_BACK_SQUAT"
    assert category == "SQUAT"


def test_validate_normalizes_case_and_spaces():
    canonical, category = validate_exercise("barbell back squat")
    assert canonical == "BARBELL_BACK_SQUAT"
    assert category == "SQUAT"


def test_validate_unknown_exercise_raises_clear_error():
    with pytest.raises(ValueError) as exc:
        validate_exercise("BARBELL_BACK_SQUATT")
    assert "Unknown exercise_name" in str(exc.value)


def test_validate_empty_raises():
    with pytest.raises(ValueError):
        validate_exercise("")


def test_is_known_exercise():
    assert is_known_exercise("OVERHEAD_BARBELL_PRESS") is True
    assert is_known_exercise("NOT_A_REAL_LIFT") is False
    assert is_known_exercise("") is False


def test_category_for():
    assert category_for("BARBELL_FRONT_SQUAT") == "SQUAT"
    assert category_for("NOPE") is None
