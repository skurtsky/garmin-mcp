from unittest.mock import patch

from tests.workout_fakes import FakeGarmin
from tools.exercise_db import exercise_info, search_exercises, suggest_exercises
from tools.strength_sets import (
    _planned_step_index,
    activity_exercises_summary,
    compare_strength_session,
    parse_exercise_sets,
    strength_sets,
    summarize_sets,
)
from tools.workout import build_workout_payload


def _es(ex, reps, grams, dur, idx, set_type="ACTIVE", cat="SQUAT"):
    return {"exercises": [{"category": cat, "name": ex, "probability": 100.0}] if ex else [],
            "duration": dur, "repetitionCount": reps, "weight": grams, "setType": set_type,
            "startTime": "2026-10-13T20:29:50.0", "wktStepIndex": idx}


def _rest(idx, dur=60.0):
    return {"exercises": [], "duration": dur, "repetitionCount": None, "weight": None,
            "setType": "REST", "startTime": "2026-10-13T20:30:00.0", "wktStepIndex": idx}


# Planned: rest(0) | warm-up squat x2 (1, rest 2, marker 3) | squat x2 (4, rest 5, marker 6) |
#          side plank x2 (7, rest 8, marker 9)
PLAN = [
    {"type": "rest"},
    {"type": "warmup", "sets": 2, "exercise_name": "BARBELL_BACK_SQUAT", "weight_lb": 50, "reps": 6},
    {"type": "repeat", "sets": 2, "exercise_name": "BARBELL_BACK_SQUAT", "set_weights_lb": [115, 125], "reps": 8},
    {"type": "repeat", "sets": 2, "exercise_name": "SIDE_PLANK", "duration_s": 30},
]
RAW = {"activityId": 1, "exerciseSets": [
    _es("BARBELL_BACK_SQUAT", 6, 22687.0, 40.0, 1), _rest(2),
    _es("BARBELL_BACK_SQUAT", 6, 22687.0, 38.0, 1), _rest(2),
    _es("BARBELL_BACK_SQUAT", 8, 52187.0, 41.0, 4), _rest(5),
    _es("BARBELL_BACK_SQUAT", 6, 61250.0, 36.0, 4), _rest(5),       # 135 lb vs planned 125, 6 vs 8 reps
    _es("SIDE_PLANK", 0, 0.0, 41.3, 7, cat="PLANK"), _rest(8),
]}


class _Client(FakeGarmin):
    def get_activity_exercise_sets(self, activity_id):
        return RAW


def _client():
    c = _Client()
    c.add(build_workout_payload("TEST plan", "strength_training", PLAN))
    return c


def test_planned_step_index_counts_repeat_markers_like_fit():
    c = _client()
    idx = _planned_step_index(c.get_workout_by_id(1001))
    assert [(i, v["step"].get("exerciseName"), v["step"]["stepType"]["stepTypeKey"]) for i, v in sorted(idx.items())] == [
        (0, None, "rest"),
        (1, "BARBELL_BACK_SQUAT", "warmup"), (2, None, "rest"),
        (4, "BARBELL_BACK_SQUAT", "interval"), (5, None, "rest"),
        (7, "SIDE_PLANK", "interval"), (8, None, "rest"),
    ]


def test_parse_sets_units_order_and_types_without_plan():
    sets = parse_exercise_sets(RAW)
    assert [s["order"] for s in sets] == list(range(1, len(sets) + 1))
    first = sets[0]
    assert first["exercise_name"] == "BARBELL_BACK_SQUAT" and first["category"] == "SQUAT"
    assert (first["reps"], first["weight_kg"], first["weight_lb"], first["duration_s"]) == (6, 22.69, 50.0, 40.0)
    assert first["set_type"] == "active"          # no plan -> Garmin's own label
    assert sets[1]["set_type"] == "rest" and sets[1]["weight_kg"] is None


def test_strength_sets_labels_warmups_from_workout():
    c = _client()
    with patch("tools.strength_sets.get_client", return_value=c):
        res = strength_sets(1, workout_id=1001)
    types = [s["set_type"] for s in res["sets"] if s["set_type"] != "rest"]
    assert types == ["warm-up", "warm-up", "active", "active", "active"]
    squat = next(e for e in res["exercises"] if e["exercise_name"] == "BARBELL_BACK_SQUAT")
    assert squat["sets"] == 2 and squat["warmup_sets"] == 2 and squat["top_weight_lb"] == 135.0
    assert squat["total_reps"] == 26


def test_summarize_ignores_rest_and_keeps_bodyweight_none():
    out = summarize_sets(parse_exercise_sets(RAW))
    plank = next(e for e in out if e["exercise_name"] == "SIDE_PLANK")
    assert plank["sets"] == 1 and plank["total_time_s"] == 41.3


def test_compare_flags_differences_and_missed_sets():
    c = _client()
    with patch("tools.strength_sets.get_client", return_value=c):
        res = compare_strength_session(1, 1001)
    by_order = {r["order"]: r for r in res["sets"]}
    ok = by_order[1]
    assert ok["planned"]["weight_lb"] == 50.0 and ok["flags"] == []
    second_working = [r for r in res["sets"] if r["planned"] and r["planned"]["set_number"] == 2
                      and r["set_type"] == "active" and r["planned"]["reps"] == 8][0]
    assert second_working["planned"]["weight_lb"] == 125.0       # wave honoured per set
    assert any("135.0 lb vs planned 125.0 lb" in f for f in second_working["flags"])
    assert any("reps 6 vs planned 8" in f for f in second_working["flags"])
    assert res["missed"] == [{"exercise_name": "SIDE_PLANK", "step_type": "interval",
                              "planned_sets": 2, "completed_sets": 1}]
    plank = [r for r in res["sets"] if r["exercise_name"] == "SIDE_PLANK"][0]
    assert plank["planned"]["duration_s"] == 30.0 and any("time 41.3s vs planned 30s" in f for f in plank["flags"])


def test_activity_exercises_summary_is_empty_on_failure():
    class Boom:
        def get_activity_exercise_sets(self, _):
            raise RuntimeError("nope")
    assert activity_exercises_summary(1, client=Boom()) == []
    assert activity_exercises_summary(1, client=_Client())[0]["exercise_name"] == "BARBELL_BACK_SQUAT"


# ── exercise database ────────────────────────────────────────────────────────

def test_exercise_info_found_flags():
    assert exercise_info("SIDE_PLANK")["found"] is True
    assert exercise_info("WEIGHTED_SIDE_PLANK")["found"] is False
    assert exercise_info("SIDE_PLANK")["url"].endswith("/PLANK/SIDE_PLANK")


def test_search_exercises_by_name_and_filters():
    names = [r["exercise_name"] for r in search_exercises("side plank", category="PLANK")]
    assert names[0] == "SIDE_PLANK"
    assert all(r["found_on_garmin_connect"] for r in search_exercises("plank", found_only=True))
    glutes = search_exercises(muscle="glutes", equipment="barbell")
    assert glutes and all("BARBELL" in r["equipment"] and r["muscles"]["GLUTES"] >= 1 for r in glutes)
    assert len(search_exercises("squat", limit=3)) == 3


def test_search_exercises_typo_falls_back_to_fuzzy():
    assert any(r["exercise_name"] == "SIDE_PLANK" for r in search_exercises("sidd plank"))


def test_suggest_exercises():
    assert suggest_exercises("SIDE_PLANKK")[0] == "SIDE_PLANK"
    assert suggest_exercises("") == []
