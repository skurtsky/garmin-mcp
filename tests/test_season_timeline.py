# tests/test_season_timeline.py
"""The season timeline calculation (tools/season_timeline.py): which weeks
count planned vs target hours, gaps between plans, races, totals, next A race.
Pure data in, pure data out — plus the plan data feed (plan_service.timeline_plans)."""
from datetime import date

import pytest

from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import plan_service, season_timeline
from tools.season_timeline import build

SEASON = {"startDate": "2026-09-14", "endDate": "2026-11-29", "races": []}
TODAY = date(2026, 10, 21)   # a Wednesday


def week(n, start, hours=None, workouts=0, done=0, target=None, phase="Base", recovery=False):
    """A plan week as plan_service.timeline_plans reports it. `hours` is the workouts' planned time."""
    from datetime import timedelta
    start_d = date.fromisoformat(start)
    return {"weekNumber": n, "start": start, "end": (start_d + timedelta(days=6)).isoformat(), "phase": phase,
            "recovery": recovery, "plannedMin": round((hours or 0) * 60), "doneMin": 0, "workouts": workouts,
            "doneWorkouts": done, "targetHours": target}


def plan(plan_id, weeks, event=None, status="active", races=()):
    return {"id": plan_id, "status": status, "event": event or plan_id.title(), "start": weeks[0]["start"],
            "end": weeks[-1]["end"], "weeks": list(weeks), "races": list(races)}


def season(**extra):
    return {**SEASON, **extra}


def race(name, date_, priority="A", type_="70.3", id_=None, goal=""):
    return {"id": id_ or f"r_{name}", "name": name, "date": date_, "priority": priority, "type": type_, "goal": goal}


# ── the targetHours fallback ──────────────────────────────────────────────────

def test_a_week_with_workouts_uses_their_planned_hours():
    t = build(season(), [plan("p", [week(1, "2026-09-14", hours=11.8, workouts=11, done=3, target=10)])], TODAY)

    col = t["weeks"][0]
    assert col["kind"] == "planned" and col["hours"] == pytest.approx(11.8)
    assert col["text"] == "Planned 11.8 h, 3 of 11 workouts done"
    assert col["targetHours"] == 10   # kept, but not what the bar shows


def test_a_week_without_workouts_falls_back_to_its_target_hours():
    t = build(season(), [plan("p", [week(1, "2026-09-14", target=11.0)])], TODAY)

    col = t["weeks"][0]
    assert col["kind"] == "target" and col["hours"] == 11.0 and col["plannedHours"] is None
    assert col["text"] == "Target 11.0 h (no workouts yet)"


def test_a_target_of_zero_is_a_known_zero_not_an_empty_slot():
    col = build(season(), [plan("p", [week(1, "2026-09-14", target=0)])], TODAY)["weeks"][0]

    assert col["kind"] == "target" and col["hours"] == 0.0
    assert col["text"] == "Target 0.0 h (no workouts yet)"


def test_a_week_with_neither_is_an_empty_slot():
    t = build(season(), [plan("p", [week(1, "2026-09-14")])], TODAY)

    col = t["weeks"][0]
    assert col["kind"] == "empty" and col["hours"] is None and col["planId"] == "p"
    assert col["text"] == "No workouts or target hours yet"
    assert t["totals"]["hours"] == 0 and t["totals"]["averagePerWeek"] is None and t["totals"]["emptyWeeks"] == 1


def test_workouts_without_minutes_still_make_a_planned_week():
    col = build(season(), [plan("p", [week(1, "2026-09-14", hours=0, workouts=2, target=8)])], TODAY)["weeks"][0]

    assert col["kind"] == "planned" and col["hours"] == 0 and col["text"].startswith("Planned 0.0 h")


# ── totals use the same rule ──────────────────────────────────────────────────

def test_totals_add_planned_and_target_weeks_alike_and_say_when_targets_are_included():
    weeks = [week(1, "2026-09-14", hours=10, workouts=5), week(2, "2026-09-21", hours=12, workouts=6),
             week(3, "2026-09-28", target=8), week(4, "2026-10-05")]   # last: empty slot
    tot = build(season(), [plan("p", weeks)], TODAY)["totals"]

    assert tot["hours"] == 30 and tot["weeksWithHours"] == 3 and tot["averagePerWeek"] == 10
    assert (tot["plannedWeeks"], tot["targetWeeks"], tot["emptyWeeks"]) == (2, 1, 1)
    assert tot["includesTargets"] is True


def test_totals_do_not_claim_targets_when_every_week_is_planned():
    weeks = [week(1, "2026-09-14", hours=10, workouts=5), week(2, "2026-09-21", hours=12, workouts=6)]
    tot = build(season(startDate="2026-09-14", endDate="2026-09-27"), [plan("p", weeks)], TODAY)["totals"]

    assert tot["includesTargets"] is False and tot["hours"] == 22 and tot["averagePerWeek"] == 11


def test_unplanned_weeks_are_not_averaged_in():
    weeks = [week(1, "2026-09-14", hours=10, workouts=5), week(2, "2026-10-12", hours=10, workouts=5)]
    tot = build(season(endDate="2026-10-18"), [plan("p", weeks)], TODAY)["totals"]

    assert tot["unplannedWeeks"] == 3 and tot["weeksWithHours"] == 2 and tot["averagePerWeek"] == 10


def test_totals_of_a_season_with_no_plans():
    t = build(season(), [], TODAY)

    assert t["totals"]["hours"] == 0 and t["totals"]["averagePerWeek"] is None and t["totals"]["includesTargets"] is False
    assert all(c["kind"] == "unplanned" for c in t["weeks"])


# ── columns, gaps and plans ───────────────────────────────────────────────────

def test_one_column_per_week_from_the_monday_on_or_before_the_season_start():
    t = build(season(startDate="2026-09-16", endDate="2026-10-04"), [], TODAY)   # starts on a Wednesday

    assert [c["start"] for c in t["weeks"]] == ["2026-09-14", "2026-09-21", "2026-09-28"]
    assert t["weeks"][0]["end"] == "2026-09-20" and [c["index"] for c in t["weeks"]] == [1, 2, 3]


def test_a_gap_between_plans_is_an_explicit_unplanned_week():
    a = plan("a", [week(1, "2026-09-14", hours=8, workouts=4), week(2, "2026-09-21", hours=8, workouts=4)])
    b = plan("b", [week(1, "2026-10-05", target=9)])

    t = build(season(endDate="2026-10-11"), [a, b], TODAY)

    assert [c["kind"] for c in t["weeks"]] == ["planned", "planned", "unplanned", "target"]
    gap = t["weeks"][2]
    assert gap["start"] == "2026-09-28" and gap["hours"] is None and gap["planId"] is None
    assert gap["text"] == "Unplanned week"


def test_the_week_after_an_a_race_names_the_race():
    a = plan("a", [week(1, "2026-09-14", hours=8, workouts=4), week(2, "2026-09-21", hours=4, workouts=2)])
    b = plan("b", [week(1, "2026-10-05", target=9)])
    s = season(endDate="2026-10-11", races=[race("Tremblant", "2026-09-27")])

    gap = build(s, [a, b], TODAY)["weeks"][2]

    assert gap["kind"] == "unplanned" and gap["text"] == "Unplanned week (after Tremblant)"


def test_a_b_race_does_not_name_the_gap():
    a = plan("a", [week(1, "2026-09-14", hours=8, workouts=4)])
    s = season(endDate="2026-09-27", races=[race("Tune-up", "2026-09-17", priority="B")])

    assert build(s, [a], TODAY)["weeks"][1]["text"] == "Unplanned week"


def test_season_time_before_and_after_the_plans_is_unplanned_too():
    a = plan("a", [week(1, "2026-09-21", hours=8, workouts=4)])

    t = build(season(startDate="2026-09-14", endDate="2026-10-04"), [a], TODAY)

    assert [c["kind"] for c in t["weeks"]] == ["unplanned", "planned", "unplanned"]


def test_a_plan_outside_the_season_dates_widens_the_timeline_rather_than_vanishing():
    a = plan("a", [week(1, "2026-09-14", hours=8, workouts=4), week(2, "2026-12-07", hours=8, workouts=4)])

    t = build(season(startDate="2026-09-14", endDate="2026-10-04"), [a], TODAY)

    assert t["weeks"][-1]["start"] == "2026-12-07" and t["weeks"][-1]["kind"] == "planned"
    assert t["end"] == "2026-12-13"


def test_two_plans_claiming_a_week_the_earlier_keeps_it_and_it_is_flagged():
    a = plan("a", [week(1, "2026-09-14", hours=8, workouts=4)])
    b = plan("b", [week(1, "2026-09-14", target=20)])

    col = build(season(endDate="2026-09-20"), [a, b], TODAY)["weeks"][0]

    assert col["planId"] == "a" and col["kind"] == "planned" and col["overlap"] is True


def test_week_details_carry_the_plan_week_phase_and_recovery_flag():
    a = plan("a", [week(5, "2026-09-14", hours=6, workouts=3, phase="Build 1", recovery=True)], event="Winter base")

    col = build(season(endDate="2026-09-20"), [a], TODAY)["weeks"][0]

    assert (col["planId"], col["planEvent"], col["weekNumber"], col["phase"], col["recovery"]) == (
        "a", "Winter base", 5, "Build 1", True)


def test_bands_merge_weeks_of_the_same_plan_and_phase_and_show_gaps():
    a = plan("a", [week(1, "2026-09-14", target=8), week(2, "2026-09-21", target=8, phase="Build 1"),
                   week(3, "2026-09-28", target=8, phase="Build 1")])
    b = plan("b", [week(1, "2026-10-12", target=8, phase="Build 1")])

    t = build(season(endDate="2026-10-18"), [a, b], TODAY)

    assert [(x["name"], x["from"], x["to"], x["kind"]) for x in t["bands"]] == [
        ("Base", 1, 1, "phase"), ("Build 1", 2, 3, "phase"), ("Unplanned", 4, 4, "unplanned"), ("Build 1", 5, 5, "phase")]
    assert [(s["planId"], s["from"], s["to"]) for s in t["plans"]] == [("a", 1, 3), ("b", 5, 5)]


# ── today and the next A race ─────────────────────────────────────────────────

def test_today_is_marked_on_its_column():
    t = build(season(), [], TODAY)

    assert t["today"] == {"date": "2026-10-21", "col": 6}
    assert [c["index"] for c in t["weeks"] if c["isToday"]] == [6]


def test_today_outside_the_timeline_has_no_column():
    t = build(season(), [], date(2027, 3, 1))

    assert t["today"]["col"] is None and not any(c["isToday"] for c in t["weeks"])


def test_next_a_race_counts_whole_weeks_and_days_from_today():
    s = season(endDate="2027-09-30", races=[race("Tremblant", "2027-06-20"), race("Montreal", "2027-09-19"),
                                            race("Spartan", "2026-11-01", priority="B")])

    nxt = build(s, [], TODAY)["nextARace"]

    assert nxt["name"] == "Tremblant" and nxt["date"] == "2027-06-20" and nxt["days"] == 242 and nxt["weeks"] == 34


def test_next_a_race_skips_past_races_and_is_none_when_there_are_none_left():
    s = season(races=[race("Done", "2026-10-03"), race("Today", "2026-10-21")])

    assert build(s, [], TODAY)["nextARace"]["name"] == "Today"
    assert build(s, [], TODAY)["nextARace"]["days"] == 0
    assert build(season(races=[race("Done", "2026-10-03")]), [], TODAY)["nextARace"] is None
    assert build(season(), [], TODAY)["nextARace"] is None


# ── races ─────────────────────────────────────────────────────────────────────

def test_season_races_land_on_their_week_and_keep_their_priority():
    s = season(races=[race("Tremblant", "2026-10-14", priority="A"), race("10K", "2026-11-01", priority="C", type_="10k")])

    t = build(s, [], TODAY)

    assert [(r["name"], r["col"], r["priority"], r["fromPlan"]) for r in t["races"]] == [
        ("Tremblant", 5, "A", False), ("10K", 7, "C", False)]


def test_a_race_outside_the_timeline_has_no_column():
    t = build(season(endDate="2027-09-30", races=[race("Next year", "2027-06-20")]), [], TODAY)

    assert t["races"][0]["col"] is not None   # inside the season, so inside the timeline
    far = build(season(startDate="2026-09-14", endDate="2026-09-27", races=[race("Far", "2028-01-01")]), [], TODAY)
    assert far["races"][0]["col"] is None


def test_plan_races_show_as_c_unless_a_season_race_covers_that_date():
    a = plan("a", [week(1, "2026-09-14", hours=8, workouts=4)],
             races=[{"date": "2026-09-19", "name": "Club 5K"}, {"date": "2026-09-20", "name": "Tremblant (plan)"}])
    s = season(endDate="2026-09-20", races=[race("Tremblant", "2026-09-20")])

    t = build(s, [a], TODAY)

    assert [(r["name"], r["priority"], r["fromPlan"]) for r in t["races"]] == [
        ("Club 5K", "C", True), ("Tremblant", "A", False)]


def test_the_same_plan_race_in_two_plans_is_listed_once():
    shared = {"date": "2026-09-19", "name": "Club 5K"}
    a = plan("a", [week(1, "2026-09-14", hours=8, workouts=4)], races=[shared])
    b = plan("b", [week(1, "2026-09-14", target=8)], races=[shared])

    assert len(build(season(endDate="2026-09-20"), [a, b], TODAY)["races"]) == 1


def test_plan_derived_races_never_count_as_the_next_a_race():
    a = plan("a", [week(1, "2026-10-26", hours=8, workouts=4)], races=[{"date": "2026-10-31", "name": "Club 5K"}])

    assert build(season(), [a], TODAY)["nextARace"] is None


# ── the data feed: plan_service.timeline_plans ────────────────────────────────

def test_timeline_plans_report_workouts_done_and_target_hours(fake_db):
    plan_service.create_plan(sample_plan())
    plan_service.activate("test-block-2026")
    plan_service.set_completion("test-block-2026", "w1-tue-run", True)

    (p,) = plan_service.timeline_plans(["test-block-2026"])

    w1, w2 = p["weeks"]
    assert (w1["workouts"], w1["doneWorkouts"], w1["plannedMin"], w1["doneMin"]) == (4, 1, 230, 50)
    assert (w1["targetHours"], w2["targetHours"], w2["workouts"]) == (3.0, 4.0, 1)
    assert (p["id"], p["event"], p["start"], p["end"], p["status"]) == ("test-block-2026", "Test Block", "2026-09-14", "2026-09-27", "active")


def test_the_phases_block_wins_over_a_weeks_stale_own_phase():
    plan = sample_plan()
    plan["phases"] = [{"name": "Base 1", "startWeek": 1, "endWeek": 1}, {"name": "Base 2", "startWeek": 2, "endWeek": 2}]
    plan["weeks"][0]["phase"] = "Base 2"      # left over from before the phases were edited
    plan["weeks"][1].pop("phase", None)

    p = plan_service._timeline_plan({"id": "test-block-2026", "plan": plan, "status": "active"}, set())

    assert [w["phase"] for w in p["weeks"]] == ["Base 1", "Base 2"]


def test_a_skeleton_week_has_no_workouts_and_keeps_its_target(fake_db):
    skeleton = sample_plan()
    skeleton["weeks"][1]["days"] = []
    skeleton["weeks"][1]["status"] = "skeleton"
    plan_service.create_plan(skeleton)

    (p,) = plan_service.timeline_plans(["test-block-2026"])
    t = build(season(startDate="2026-09-14", endDate="2026-09-27"), [p], TODAY)

    assert p["weeks"][1]["workouts"] == 0 and p["weeks"][1]["targetHours"] == 4.0
    assert [c["kind"] for c in t["weeks"]] == ["planned", "target"]
    assert t["weeks"][1]["text"] == "Target 4.0 h (no workouts yet)" and t["totals"]["includesTargets"] is True


def test_timeline_plans_pick_only_the_given_plans_and_order_them_by_start(fake_db):
    plan_service.create_plan(sample_plan(id="b"))
    plan_service.create_plan(sample_plan(id="a"))
    plan_service.create_plan(sample_plan(id="c"))

    assert [p["id"] for p in plan_service.timeline_plans(["b", "a"])] == ["a", "b"]
    assert plan_service.timeline_plans([]) == []


def test_race_workouts_and_the_plan_event_are_reported_once(fake_db):
    raced = sample_plan()
    raced["weeks"][1]["days"][0]["workouts"].append({"id": "w2-race", "sport": "race", "name": "Fall 10K", "durationMinutes": 45})
    raced["raceStrategy"] = {"event": {"name": "Fall 10K", "date": "2026-09-22"}}
    plan_service.create_plan(raced)

    (p,) = plan_service.timeline_plans(["test-block-2026"])

    assert p["races"] == [{"date": "2026-09-22", "name": "Fall 10K"}]
