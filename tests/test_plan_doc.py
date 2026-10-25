# tests/test_plan_doc.py
"""Tests for the pure training-plan document operations (tools/plan_doc.py)."""
import copy
from datetime import date

import pytest

from tests.plan_fakes import sample_plan
from tools import plan_doc
from tools.plan_doc import PlanError


def _ids(plan, day):
    return [w["id"] for _, d, w in plan_doc.iter_workouts(plan) if d["date"] == day]


# ── VALIDATION ────────────────────────────────────────────────────────────────

def test_valid_plan_has_no_errors():
    errors, warnings = plan_doc.validate_plan(sample_plan())
    assert errors == []
    assert warnings == []


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p.pop("meta"), "meta"),
    (lambda p: p["meta"].pop("id"), "meta.id"),
    (lambda p: p["meta"].update(id="has spaces/slash"), "meta.id"),
    (lambda p: p.pop("weeks"), "weeks"),
    (lambda p: p["weeks"][1]["days"][0]["workouts"][0].update(id="w1-mon-swim"), "Duplicate"),
    (lambda p: p["weeks"][0]["days"][0]["workouts"][0].pop("id"), "no id"),
    (lambda p: p["weeks"][0].update(startDate="soon"), "startDate"),
])
def test_invalid_plans_are_rejected(mutate, message):
    plan = sample_plan()
    mutate(plan)
    errors, _ = plan_doc.validate_plan(plan)
    assert any(message in e for e in errors), errors


def test_non_object_is_rejected():
    assert plan_doc.validate_plan([1, 2])[0]


def test_warnings_for_budgets_phases_and_dates():
    plan = sample_plan()
    plan["phases"] = []
    plan["meta"].pop("totalWeeks")
    plan["weeks"][0]["days"][0]["workouts"][0]["keyTargets"] = "x" * 241
    plan["weeks"][0]["days"][0]["date"] = "2026-09-30"
    errors, warnings = plan_doc.validate_plan(plan)
    assert errors == []
    text = " ".join(warnings)
    assert "phases" in text and "meta.totalWeeks" in text
    assert "keyTargets is 241 characters" in text
    assert "outside the week" in text


# ── WEEKLY TOTALS ─────────────────────────────────────────────────────────────

def test_week_summary_is_computed_from_workouts():
    plan = plan_doc.normalize_plan(sample_plan())
    summary = plan["weeks"][0]["summary"]
    assert summary["totalHours"] == 3.83
    assert summary["bySport"] == {
        "swim": {"sessions": 1, "hours": 0.75, "km": 2},
        "run": {"sessions": 1, "hours": 0.83, "km": 10},
        "strength": {"sessions": 1, "hours": 0.75},
        "bike": {"sessions": 1, "hours": 1.5, "km": 45},
    }


def test_week_summary_ignores_rest_and_uses_range_midpoint():
    week = {"days": [{"date": "2026-09-14", "workouts": [
        {"id": "a", "sport": "rest", "name": "Rest", "durationMinutes": 0},
        {"id": "b", "sport": "bike", "name": "Rouvy", "durationMinutes": 60,
         "distanceKmRange": {"low": 40, "high": 50}},
    ]}]}
    assert plan_doc.week_summary(week) == {"totalHours": 1, "bySport": {"bike": {"sessions": 1, "hours": 1, "km": 45}}}


# ── OPERATIONS ────────────────────────────────────────────────────────────────

def test_apply_operations_never_mutates_the_input():
    plan = sample_plan()
    before = copy.deepcopy(plan)
    plan_doc.apply_operations(plan, [{"op": "remove_workout", "workout_id": "w1-tue-run"}])
    assert plan == before


def test_add_workout_generates_an_id_and_updates_the_summary():
    new, messages, touched = plan_doc.apply_operations(sample_plan(), [{
        "op": "add_workout", "date": "2026-09-15",
        "workout": {"sport": "run", "name": "Strides", "durationMinutes": 20, "distanceKm": "4"},
    }])
    assert touched == ["w1-tue-run-2"]  # w1-tue-run is taken
    assert _ids(new, "2026-09-15") == ["w1-tue-run", "w1-tue-strength", "w1-tue-run-2"]
    added = plan_doc.find_workout(new, "w1-tue-run-2")[2]
    assert added["distanceKm"] == 4 and added["completed"] is False
    assert new["weeks"][0]["summary"]["bySport"]["run"] == {"sessions": 2, "hours": 1.17, "km": 14}
    assert messages == ["Added 'Strides' on Tue Sep 15"]


def test_add_workout_creates_a_missing_day_entry_in_order():
    new, _, touched = plan_doc.apply_operations(sample_plan(), [{
        "op": "add_workout", "date": "2026-09-16", "workout": {"sport": "swim", "name": "Drills"},
    }])
    days = [d["date"] for d in new["weeks"][0]["days"]]
    assert days == sorted(days) and "2026-09-16" in days
    assert touched == ["w1-wed-swim"]
    assert next(d for d in new["weeks"][0]["days"] if d["date"] == "2026-09-16")["dayOfWeek"] == "Wednesday"


@pytest.mark.parametrize("op, message", [
    ({"op": "add_workout", "date": "2027-01-01", "workout": {"sport": "run", "name": "x"}}, "outside every week"),
    ({"op": "add_workout", "date": "2026-09-15", "workout": {"sport": "yoga", "name": "x"}}, "sport must be"),
    ({"op": "add_workout", "date": "2026-09-15", "workout": {"sport": "run"}}, "needs a name"),
    ({"op": "add_workout", "date": "15/09", "workout": {"sport": "run", "name": "x"}}, "YYYY-MM-DD"),
    ({"op": "update_workout", "workout_id": "w1-tue-run", "fields": {"durationMinutes": -5}}, "non-negative"),
    ({"op": "update_workout", "workout_id": "w1-tue-run", "fields": {"name": ""}}, "needs a name"),
    ({"op": "move_workout", "workout_id": "nope", "date": "2026-09-16"}, "No workout"),
    ({"op": "update_week", "week_number": 9, "fields": {"focus": "x"}}, "No week 9"),
    ({"op": "update_week", "week_number": 1, "fields": {"days": []}}, "can't be edited"),
    ({"op": "update_week", "week_number": 1, "fields": {"targetHours": True}}, "wrong type"),
    ({"op": "set_zones", "ftp": "fast"}, "whole number"),
    ({"op": "set_zones", "thresholdPace": "quick"}, "m:ss"),
    ({"op": "set_zones"}, "at least one"),
    ({"op": "set_unit", "unit": "furlongs"}, "unit must be"),
    ({"op": "update_week", "week_number": 1, "fields": {"startDate": "2026-09-15"}}, "can't be edited"),
    ({"op": "set_field", "path": "meta.id", "value": "other"}, "plan id is its storage key"),
    ({"op": "set_field", "path": "meta", "value": {}}, "meta.id can't be changed"),
    ({"op": "set_field", "path": "weeks[0].focus", "value": "x"}, "use update_week"),
    ({"op": "set_field", "path": "overrides.ftp", "value": 300}, "use set_zones"),
    ({"op": "set_field", "path": "phases.5.focus", "value": "x"}, "no such list item"),
    ({"op": "set_field", "path": "phases.0.name.x", "value": "x"}, "isn't an object or a list"),
    ({"op": "set_field", "path": "meta.eventDate", "value": "next spring"}, "YYYY-MM-DD"),
    ({"op": "set_field", "path": "phases", "value": ["Base"]}, "list of objects"),
    ({"op": "set_field", "path": "raceStrategy.x"}, "needs a value"),
    ({"op": "set_field", "path": "raceStrategy.keyRisks.0", "value": "x"}, "doesn't exist"),
    ({"op": "set_field", "path": " ", "value": 1}, "dotted path"),
    ({"op": "append_to_list", "path": "meta.event", "value": "x"}, "isn't a list"),
    ({"op": "append_to_list", "path": "phases", "value": "Peak"}, "list of objects"),
    ({"op": "explode"}, "unknown op"),
])
def test_bad_operations_raise_with_the_operation_named(op, message):
    with pytest.raises(PlanError) as exc:
        plan_doc.apply_operations(sample_plan(), [{"op": "set_unit", "unit": "metric"}, op])
    assert "Operation 2" in str(exc.value) and message in str(exc.value)


def test_operations_are_all_or_nothing():
    plan = sample_plan()
    with pytest.raises(PlanError):
        plan_doc.apply_operations(plan, [
            {"op": "remove_workout", "workout_id": "w1-tue-run"},
            {"op": "remove_workout", "workout_id": "missing"},
        ])
    assert plan_doc.find_workout(plan, "w1-tue-run")


def test_move_workout_across_weeks():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [
        {"op": "move_workout", "workout_id": "w1-thu-bike", "date": "2026-09-24"},
    ])
    week, day, _ = plan_doc.find_workout(new, "w1-thu-bike")
    assert week["weekNumber"] == 2 and day["date"] == "2026-09-24"
    assert "bike" not in new["weeks"][0]["summary"]["bySport"]
    assert new["weeks"][1]["summary"]["bySport"]["bike"]["sessions"] == 1
    assert messages == ["Moved 'Endurance ride' Thu Sep 17 → Thu Sep 24"]


def test_move_to_the_same_day_is_a_no_op():
    _, messages, touched = plan_doc.apply_operations(sample_plan(), [
        {"op": "move_workout", "workout_id": "w1-thu-bike", "date": "2026-09-17"},
    ])
    assert messages == [] and touched == []


def test_update_workout_edits_removes_and_moves():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [{
        "op": "update_workout", "workout_id": "w1-tue-run",
        "fields": {"name": "Hill reps", "distanceKm": None, "keyTargets": "6x90s", "date": "2026-09-16",
                   "id": "hijack", "completed": True},
    }])
    _, day, w = plan_doc.find_workout(new, "w1-tue-run")
    assert w["name"] == "Hill reps" and "distanceKm" not in w and w["keyTargets"] == "6x90s"
    assert day["date"] == "2026-09-16"
    assert "completed" not in w  # completion is tracked outside the document
    assert messages == ["Edited 'Hill reps' and moved 'Hill reps' Tue Sep 15 → Wed Sep 16"]


def test_remove_and_update_week():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [
        {"op": "remove_workout", "workout_id": "w1-mon-swim"},
        {"op": "update_week", "week_number": 1, "fields": {"focus": "Travel", "isRecoveryWeek": True}},
    ])
    assert plan_doc.find_workout(new, "w1-mon-swim") is None
    assert new["weeks"][0]["focus"] == "Travel" and new["weeks"][0]["isRecoveryWeek"] is True
    assert messages[1] == "Updated week 1 (focus, isRecoveryWeek)"


def test_update_week_takes_any_other_field_and_null_removes():
    new, _, _ = plan_doc.apply_operations(sample_plan(), [
        {"op": "update_week", "week_number": 2, "fields": {"notes": "Taper starts", "focus": None}},
    ])
    assert new["weeks"][1]["notes"] == "Taper starts" and "focus" not in new["weeks"][1]


def test_set_field_edits_any_part_of_the_document():
    new, messages, touched = plan_doc.apply_operations(sample_plan(), [
        {"op": "set_field", "path": "meta.eventDate", "value": "2026-09-27"},
        {"op": "set_field", "path": "preferences.run", "value": "miles"},
        {"op": "set_field", "path": "phases[0].focus", "value": "Aerobic durability"},
        {"op": "set_field", "path": "zones.run.pace.paceSource", "value": "TESTED — 30min TT"},
        {"op": "set_field", "path": "raceStrategy.pacing.bike.targetPower", "value": "NP 205W"},
        {"op": "set_field", "path": "raceStrategy.taper", "value": {"startDate": "2026-09-20", "notes": "x"}},
        {"op": "set_field", "path": "zones.swim.cssSeconds", "value": None},
    ])
    assert new["meta"]["eventDate"] == "2026-09-27" and new["meta"]["id"] == "test-block-2026"
    assert new["preferences"]["run"] == "miles"
    assert new["phases"][0]["focus"] == "Aerobic durability"
    assert new["zones"]["run"]["pace"]["paceSource"] == "TESTED — 30min TT"
    assert new["raceStrategy"] == {"pacing": {"bike": {"targetPower": "NP 205W"}},
                                   "taper": {"startDate": "2026-09-20", "notes": "x"}}
    assert "cssSeconds" not in new["zones"]["swim"]
    assert messages[0] == "Set meta.eventDate" and messages[-1] == "Removed zones.swim.cssSeconds"
    assert touched == []


def test_lists_can_be_appended_inserted_and_trimmed():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [
        {"op": "append_to_list", "path": "raceStrategy.keyRisks", "value": "Heat"},
        {"op": "append_to_list", "path": "raceStrategy.keyRisks", "value": "Run durability", "index": 0},
        {"op": "append_to_list", "path": "phases", "value": {"name": "Peak", "startWeek": 3, "endWeek": 4}},
        {"op": "set_field", "path": "phases.0", "value": None},
        {"op": "set_field", "path": "phases.-1.endWeek", "value": 5},
        {"op": "set_field", "path": "raceStrategy.missing", "value": None},   # already absent: no-op
    ])
    assert new["raceStrategy"]["keyRisks"] == ["Run durability", "Heat"]
    assert new["phases"] == [{"name": "Peak", "startWeek": 3, "endWeek": 5}]
    assert messages == ["Added to raceStrategy.keyRisks", "Added to raceStrategy.keyRisks", "Added to phases",
                        "Removed phases.0", "Set phases.-1.endWeek"]


def test_zone_overrides_validation_and_units():
    new, messages, _ = plan_doc.apply_operations(sample_plan(), [
        {"op": "set_zones", "ftp": "262", "thresholdPace": "4:10", "cssLabel": "2:00"},
        {"op": "set_zone_validation", "sport": "bike", "validated": True, "at": "2026-09-24"},
        {"op": "set_unit", "unit": "imperial"},
    ])
    assert new["overrides"] == {"ftp": 262, "thresholdPace": "4:10", "cssLabel": "2:00/100m", "cssSeconds": 120}
    assert new["zonesValidated"] == {"bike": {"validated": True, "at": "2026-09-24"}}
    assert new["unit"] == "imperial"
    thresholds = plan_doc.effective_thresholds(new)
    assert thresholds["ftp"] == 262 and thresholds["bikeLthr"] == 160 and thresholds["css"] == "2:00/100m"

    reset, _, _ = plan_doc.apply_operations(new, [{"op": "set_zones", "ftp": None, "cssLabel": ""}])
    assert reset["overrides"] == {"thresholdPace": "4:10"}
    assert plan_doc.effective_thresholds(reset)["ftp"] == 250


def test_current_week_number():
    plan = sample_plan()
    assert plan_doc.current_week_number(plan, date(2026, 9, 23)) == 2
    assert plan_doc.current_week_number(plan, date(2026, 9, 1)) == 1
    assert plan_doc.current_week_number(plan, date(2027, 1, 1)) is None


# ── TAGS, PURPOSE, COACH REVIEW, WEEK BRIEF ───────────────────────────────────

def _apply(plan, *ops):
    return plan_doc.apply_operations(plan, list(ops))


def test_tags_are_trimmed_deduped_and_removable():
    plan, _, _ = _apply(sample_plan(), {"op": "update_workout", "workout_id": "w1-mon-swim",
                                          "fields": {"tags": ["  25m ", "AE2", "ae2", "Bob   MacQuarrie", ""]}})
    assert plan_doc.find_workout(plan, "w1-mon-swim")[2]["tags"] == ["25m", "AE2", "Bob MacQuarrie"]
    plan, _, _ = _apply(plan, {"op": "update_workout", "workout_id": "w1-mon-swim", "fields": {"tags": []}})
    assert "tags" not in plan_doc.find_workout(plan, "w1-mon-swim")[2]


@pytest.mark.parametrize("tags, message", [
    ("AE2", "list"),
    ([1], "text"),
    (["x" * 41], "max 40"),
    ([f"t{i}" for i in range(9)], "at most 8"),
])
def test_bad_tags_are_rejected(tags, message):
    with pytest.raises(PlanError, match=message):
        _apply(sample_plan(), {"op": "update_workout", "workout_id": "w1-mon-swim", "fields": {"tags": tags}})


def test_add_workout_takes_purpose_cues_and_tags():
    plan, _, _ = _apply(sample_plan(), {"op": "add_workout", "date": "2026-09-16", "workout": {
        "sport": "swim", "name": "Aerobic", "purpose": "Build base", "cues": "Long reach",
        "tags": ["25m"], "purposeMet": None}})
    w = plan_doc.find_workout(plan, "w1-wed-swim")[2]
    assert (w["purpose"], w["cues"], w["tags"]) == ("Build base", "Long reach", ["25m"])
    assert "purposeMet" not in w


def test_purpose_met_must_be_a_boolean_and_false_is_kept():
    with pytest.raises(PlanError, match="purposeMet"):
        _apply(sample_plan(), {"op": "update_workout", "workout_id": "w1-mon-swim",
                               "fields": {"purposeMet": "yes"}})
    plan, _, _ = _apply(sample_plan(), {"op": "update_workout", "workout_id": "w1-mon-swim",
                                          "fields": {"purpose": "Base", "purposeMet": False}})
    assert plan_doc.find_workout(plan, "w1-mon-swim")[2]["purposeMet"] is False
    plan, _, _ = _apply(plan, {"op": "update_workout", "workout_id": "w1-mon-swim", "fields": {"purposeMet": None}})
    assert "purposeMet" not in plan_doc.find_workout(plan, "w1-mon-swim")[2]


def test_add_coach_review_stamps_time_and_records_purpose_met():
    plan, messages, touched = _apply(
        sample_plan(),
        {"op": "update_workout", "workout_id": "w1-mon-swim", "fields": {"purpose": "Base"}},
        {"op": "add_coach_review", "workout_id": "w1-mon-swim", "text": "  Even splits.  ", "purpose_met": True})
    w = plan_doc.find_workout(plan, "w1-mon-swim")[2]
    assert w["purposeMet"] is True
    (entry,) = w["coachReview"]
    assert entry["text"] == "Even splits." and entry["by"] == "coach" and entry["at"].endswith("+00:00")
    assert "purpose met" in messages[-1] and touched[-1] == "w1-mon-swim"


def test_purpose_met_needs_a_purpose_and_reviews_have_limits():
    with pytest.raises(PlanError, match="no purpose"):
        _apply(sample_plan(), {"op": "add_coach_review", "workout_id": "w1-mon-swim", "text": "ok", "purpose_met": True})
    with pytest.raises(PlanError, match="needs text"):
        _apply(sample_plan(), {"op": "add_coach_review", "workout_id": "w1-mon-swim", "text": "  "})
    with pytest.raises(PlanError, match="max 800"):
        _apply(sample_plan(), {"op": "add_coach_review", "workout_id": "w1-mon-swim", "text": "x" * 801})
    with pytest.raises(PlanError, match="add_coach_review"):
        _apply(sample_plan(), {"op": "update_workout", "workout_id": "w1-mon-swim",
                               "fields": {"coachReview": [{"text": "sneaky"}]}})


def test_remove_coach_review_by_index():
    ops = [{"op": "add_coach_review", "workout_id": "w1-mon-swim", "text": t} for t in ("one", "two")]
    plan, _, _ = _apply(sample_plan(), *ops)
    plan, _, _ = _apply(plan, {"op": "remove_coach_review", "workout_id": "w1-mon-swim", "index": 0})
    assert [r["text"] for r in plan_doc.find_workout(plan, "w1-mon-swim")[2]["coachReview"]] == ["two"]
    plan, _, _ = _apply(plan, {"op": "remove_coach_review", "workout_id": "w1-mon-swim"})
    assert "coachReview" not in plan_doc.find_workout(plan, "w1-mon-swim")[2]
    with pytest.raises(PlanError, match="index"):
        _apply(plan, {"op": "remove_coach_review", "workout_id": "w1-mon-swim", "index": 0})


def test_update_week_theme_and_key_sessions():
    plan, _, _ = _apply(sample_plan(), {"op": "update_week", "week_number": 1, "fields": {
        "theme": "  Raise bike power ", "keySessions": ["w1-thu-bike", "Sat long ride"]}})
    week = plan["weeks"][0]
    assert week["theme"] == "Raise bike power" and week["keySessions"] == ["w1-thu-bike", "Sat long ride"]
    plan, _, _ = _apply(plan, {"op": "update_week", "week_number": 1, "fields": {"keySessions": [], "theme": None}})
    assert "keySessions" not in plan["weeks"][0] and "theme" not in plan["weeks"][0]
    with pytest.raises(PlanError, match="at most 6"):
        _apply(plan, {"op": "update_week", "week_number": 1, "fields": {"keySessions": ["a"] * 7}})
    with pytest.raises(PlanError, match="wrong type"):
        _apply(plan, {"op": "update_week", "week_number": 1, "fields": {"keySessions": "Sat"}})


def test_over_budget_coach_notes_come_back_as_warnings():
    warnings = []
    plan_doc.apply_operations(sample_plan(), [{"op": "update_workout", "workout_id": "w1-mon-swim",
                                               "fields": {"purpose": "x" * 241}}], warnings)
    assert warnings and "budget 240" in warnings[0]["message"]


@pytest.mark.parametrize("mutate, message", [
    (lambda w: w.update(tags="AE2"), "tags"),
    (lambda w: w.update(tags=["x" * 41]), "Tag"),
    (lambda w: w.update(purposeMet="yes"), "purposeMet"),
    (lambda w: w.update(coachReview="nice"), "coachReview"),
])
def test_uploaded_plan_rejects_bad_new_fields(mutate, message):
    plan = sample_plan()
    mutate(plan["weeks"][0]["days"][0]["workouts"][0])
    errors, _ = plan_doc.validate_plan(plan)
    assert any(message in e for e in errors), errors


def test_valid_new_fields_pass_and_reach_the_read_view():
    from tools import plan_service
    plan = sample_plan()
    w = plan["weeks"][0]["days"][0]["workouts"][0]
    w.update(tags=["25m", "AE2"], purpose="Aerobic base.", purposeMet=True, cues="Relax",
             coachReview=[{"text": "Good.", "by": "coach", "at": "2026-01-01T00:00:00+00:00"}])
    plan["weeks"][0].update(theme="Build volume", keySessions=[w["id"]])
    errors, _ = plan_doc.validate_plan(plan)
    assert errors == []
    view = plan_service._workout_view(w, {"date": "2026-01-05"}, {}, True)
    assert view["tags"] == ["25m", "AE2"] and view["purpose"] == "Aerobic base." and view["purposeMet"] is True
    assert view["cues"] == "Relax" and view["coachReview"][0]["text"] == "Good."
