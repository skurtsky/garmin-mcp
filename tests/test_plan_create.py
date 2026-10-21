# tests/test_plan_create.py
"""create_training_plan, the strict plan validation behind it, and
amend_training_plan's replace_week — against the in-memory db fake."""
import copy
from datetime import date

import pytest
from fastmcp.exceptions import ToolError

from tests.plan_fakes import fake_db, sample_plan  # noqa: F401 — fixture
from tools import plan_service, plan_tools, plan_validation


def day(date_, name, *workouts):
    return {"date": date_, "dayOfWeek": name, "workouts": list(workouts)}


def good_plan(**meta):
    """Three Monday–Sunday weeks from 2027-06-28; week 3 is a skeleton."""
    plan = {
        "version": "1.0",
        "meta": {"id": "next-block-2027", "athlete": "Alex", "event": "Next Block",
                 "planStartDate": "2027-06-28", "planEndDate": "2027-07-18", "totalWeeks": 3,
                 "eventDate": "2027-07-18"},
        "zones": {
            "bike": {"power": {"ftp": 250}, "hr": {"lthr": 160}},
            "run": {"hr": {"lthr": 170}, "pace": {"thresholdPace": "4:17/km"}},
            "swim": {"css": "2:05/100m", "cssSeconds": 125},
        },
        "phases": [{"name": "Base", "startWeek": 1, "endWeek": 2, "focus": "Aerobic",
                    "startDate": "2027-06-28", "endDate": "2027-07-11"},
                   {"name": "Build", "startWeek": 3, "endWeek": 3, "focus": "Threshold"}],
        "weeks": [
            {"weekNumber": 1, "startDate": "2027-06-28", "endDate": "2027-07-04", "phase": "Base",
             "targetHours": 4, "targetRunKm": 20, "days": [
                 day("2027-06-29", "Tuesday",
                     {"id": "w1-tue-run", "sport": "run", "name": "Tempo", "durationMinutes": 60,
                      "distanceKm": 12, "humanReadable": "Run at {{run-pace:3}}"}),
                 day("2027-07-01", "Thursday",
                     {"id": "w1-thu-bike", "sport": "bike", "name": "Sweet spot", "durationMinutes": 120,
                      "distanceKm": 50, "humanReadable": "Ride {{bike-watts:ss}}, {{bike-ftp:75-77}}"}),
                 day("2027-07-03", "Saturday",
                     {"id": "w1-sat-run", "sport": "run", "name": "Long", "durationMinutes": 60,
                      "distanceKm": 8}),
             ]},
            {"weekNumber": 2, "startDate": "2027-07-05", "endDate": "2027-07-11", "phase": "Base",
             "targetHours": 1, "days": [
                 day("2027-07-06", "Tuesday",
                     {"id": "w2-tue-swim", "sport": "swim", "name": "CSS set", "durationMinutes": 60,
                      "humanReadable": "Hard {{swim-pace:4}}"}),
             ]},
            {"weekNumber": 3, "startDate": "2027-07-12", "endDate": "2027-07-18", "status": "skeleton",
             "targetHours": 6, "targetRunKm": 30},
        ],
    }
    plan["meta"].update(meta)
    return plan


def issues(plan):
    errors, warnings = plan_validation.validate_document(plan)
    return errors, warnings


def paths(found):
    return [i["path"] for i in found]


@pytest.fixture
def db(fake_db):
    return fake_db


# ── HAPPY PATH ────────────────────────────────────────────────────────────────

def test_a_good_plan_has_no_errors():
    errors, warnings = issues(good_plan())
    assert errors == []
    assert warnings == []


def test_create_stores_a_future_plan_as_revision_one(db):
    result = plan_tools.create_training_plan(good_plan())

    assert result["plan_id"] == "next-block-2027"
    assert (result["revision"], result["status"], result["outcome"]) == (1, "future", "created")
    assert result["counts"] == {"weeks": 3, "workouts": 4, "skeleton_weeks": 1}
    assert result["warnings"] == [] and result["idempotent_replay"] is False
    row = db.plans["next-block-2027"]
    assert row["status"] == "future" and row["version"] == 1
    assert [(r["version"], r["source"]) for r in db.revisions["next-block-2027"]] == [(1, "upload")]


def test_server_computes_summaries_for_filled_weeks_only(db):
    plan = good_plan()
    plan["weeks"][0]["summary"] = {"totalHours": 99, "bySport": {}}   # hand-written, wrong
    result = plan_tools.create_training_plan(plan)

    stored = db.plans["next-block-2027"]["plan"]["weeks"]
    assert stored[0]["summary"]["totalHours"] == 4
    assert stored[0]["summary"]["bySport"]["bike"] == {"sessions": 1, "hours": 2, "km": 50}
    assert stored[0]["summary"]["bySport"]["run"]["km"] == 20
    assert "summary" not in stored[2]
    assert any(w["path"] == "weeks[0].summary" for w in result["warnings"])


def test_create_never_touches_the_active_plan_or_activates(db):
    plan_service.save_upload(sample_plan())   # the active plan
    before = copy.deepcopy(db.plans["test-block-2026"])

    plan_tools.create_training_plan(good_plan())

    assert db.plans["test-block-2026"] == before
    assert db.plans["test-block-2026"]["status"] == "active"
    assert db.plans["next-block-2027"]["status"] == "future"


@pytest.mark.parametrize("status", ["active", "bogus"])
def test_create_cannot_make_an_active_plan(db, status):
    with pytest.raises(ToolError, match="active|status"):
        plan_tools.create_training_plan(good_plan(), status=status)
    assert db.plans == {}


def test_can_create_an_archived_plan(db):
    assert plan_tools.create_training_plan(good_plan(), status="archived")["status"] == "archived"
    assert db.plans["next-block-2027"]["archived_at"] is not None


# ── ATOMIC, VALIDATE-ONLY, CONFLICTS, IDEMPOTENCY ─────────────────────────────

def test_a_rejected_plan_leaves_nothing_behind(db):
    plan = good_plan()
    plan["weeks"][0]["startDate"] = "2027-06-29"   # a Tuesday
    with pytest.raises(ToolError, match=r"weeks\[0\]\.startDate: .*Tuesday.*Monday"):
        plan_tools.create_training_plan(plan, idempotency_key="k1")
    assert db.plans == {} and db.revisions == {} and db.idempotency == {}


def test_validate_only_writes_nothing(db):
    result = plan_tools.create_training_plan(good_plan(), validate_only=True)
    assert result["valid"] is True and result["would"] == "create" and result["errors"] == []
    assert result["counts"]["weeks"] == 3
    assert db.plans == {}


def test_validate_only_reports_errors_instead_of_raising(db):
    plan = good_plan()
    plan["meta"]["totalWeeks"] = 5
    result = plan_tools.create_training_plan(plan, validate_only=True)
    assert result["valid"] is False and result["would"] is None
    assert paths(result["errors"]) == ["meta.totalWeeks"]


def test_existing_id_is_rejected_by_default_and_never_silently_overwritten(db):
    plan_tools.create_training_plan(good_plan())
    changed = good_plan()
    changed["weeks"][0]["focus"] = "Different"
    with pytest.raises(ToolError, match="already exists.*on_conflict='replace'"):
        plan_tools.create_training_plan(changed)
    assert "focus" not in db.plans["next-block-2027"]["plan"]["weeks"][0]
    assert db.plans["next-block-2027"]["version"] == 1
    check = plan_tools.create_training_plan(changed, validate_only=True)
    assert check["valid"] is False and paths(check["errors"]) == ["meta.id"]


def test_replace_swaps_a_future_plan_as_a_new_revision(db):
    plan_tools.create_training_plan(good_plan())
    changed = good_plan()
    changed["weeks"][0]["focus"] = "Different"

    result = plan_tools.create_training_plan(changed, on_conflict="replace")

    assert (result["outcome"], result["revision"]) == ("replaced", 2)
    assert db.plans["next-block-2027"]["plan"]["weeks"][0]["focus"] == "Different"
    assert [r["version"] for r in db.revisions["next-block-2027"]] == [1, 2]
    assert db.revisions["next-block-2027"][0]["plan"]["weeks"][0].get("focus") is None   # still restorable


def test_replace_can_bring_back_an_archived_plan_as_future(db):
    plan_tools.create_training_plan(good_plan(), status="archived")
    result = plan_tools.create_training_plan(good_plan(), on_conflict="replace")
    assert result["status"] == "future" and db.plans["next-block-2027"]["archived_at"] is None


def test_the_active_plan_is_never_replaced(db):
    plan_service.save_upload(sample_plan())
    with pytest.raises(ToolError, match="active plan"):
        plan_tools.create_training_plan(sample_plan(), on_conflict="replace")
    assert db.plans["test-block-2026"]["version"] == 1


def test_idempotency_key_makes_a_retry_a_no_op(db):
    first = plan_tools.create_training_plan(good_plan(), idempotency_key="abc")
    retry = plan_tools.create_training_plan(good_plan(), idempotency_key="abc")

    assert retry["idempotent_replay"] is True
    assert {k: v for k, v in retry.items() if k != "idempotent_replay"} == \
           {k: v for k, v in first.items() if k != "idempotent_replay"}
    assert db.plans["next-block-2027"]["version"] == 1


def test_idempotency_key_with_a_different_payload_is_an_error(db):
    plan_tools.create_training_plan(good_plan(), idempotency_key="abc")
    other = good_plan()
    other["weeks"][0]["focus"] = "x"
    with pytest.raises(ToolError, match="different request"):
        plan_tools.create_training_plan(other, idempotency_key="abc", on_conflict="replace")


def test_exactly_one_of_plan_and_url(db):
    with pytest.raises(ToolError, match="exactly one"):
        plan_tools.create_training_plan()
    with pytest.raises(ToolError, match="exactly one"):
        plan_tools.create_training_plan(good_plan(), plan_url="https://example.com/p.json")


def test_plan_url_loads_the_plan(db, monkeypatch):
    import json
    monkeypatch.setattr(plan_service, "fetch_plan_url", lambda url: json.dumps(good_plan()).encode())
    assert plan_tools.create_training_plan(plan_url="https://example.com/p.json")["plan_id"] == "next-block-2027"


@pytest.mark.parametrize("url, message", [
    ("http://example.com/p.json", "https"),
    ("https://localhost/p.json", "public host"),
    ("https://169.254.169.254/latest", "public host"),
])
def test_plan_url_refuses_non_https_and_private_hosts(db, url, message):
    with pytest.raises(ToolError, match=message):
        plan_tools.create_training_plan(plan_url=url)


def test_plan_url_host_allowlist(db, monkeypatch):
    monkeypatch.setenv("TRAINING_PLAN_URL_HOSTS", "plans.example.com")
    with pytest.raises(ToolError, match="isn't allowed"):
        plan_tools.create_training_plan(plan_url="https://other.example.com/p.json")


# ── VALIDATION: STRUCTURE & CALENDAR ──────────────────────────────────────────

def _break(mutate):
    plan = good_plan()
    mutate(plan)
    return plan


CALENDAR_CASES = [
    ("version missing", lambda p: p.pop("version"), "version", "Missing schema version"),
    ("version major", lambda p: p.update(version="2.0"), "version", "isn't supported"),
    ("start not monday", lambda p: p["weeks"][1].update(startDate="2027-07-06"),
     "weeks[1].startDate", "Tuesday"),
    ("end not sunday", lambda p: p["weeks"][0].update(endDate="2027-07-03"),
     "weeks[0].endDate", "Saturday"),
    ("end not start+6", lambda p: p["weeks"][0].update(endDate="2027-07-11"),
     "weeks[0].endDate", "isn't the Sunday of the week"),
    ("dayOfWeek wrong", lambda p: p["weeks"][0]["days"][0].update(dayOfWeek="Wednesday"),
     "weeks[0].days[0].dayOfWeek", "is a Tuesday"),
    ("day outside week", lambda p: p["weeks"][0]["days"][0].update(date="2027-07-06", dayOfWeek="Tuesday"),
     "weeks[0].days[0].date", "outside the week"),
    ("gap", lambda p: p["weeks"][1].update(startDate="2027-07-12", endDate="2027-07-18"),
     "weeks[1].startDate", "Gap of 7 days"),
    ("overlap", lambda p: p["weeks"][1].update(startDate="2027-06-28", endDate="2027-07-04"),
     "weeks[1].startDate", "Overlaps week 1"),
    ("weekNumber out of order", lambda p: p["weeks"][1].update(weekNumber=3),
     "weeks[1].weekNumber", "this one is 2"),
    ("totalWeeks", lambda p: p["meta"].update(totalWeeks=4), "meta.totalWeeks", "weeks has 3"),
    ("planStartDate", lambda p: p["meta"].update(planStartDate="2027-06-21"),
     "meta.planStartDate", "week 1 starts 2027-06-28"),
    ("planEndDate", lambda p: p["meta"].update(planEndDate="2027-07-25"),
     "meta.planEndDate", "last week ends 2027-07-18"),
    ("eventDate", lambda p: p["meta"].update(eventDate="2027-08-01"), "meta.eventDate", "outside the plan"),
    ("race date", lambda p: p.update(raceStrategy={"event": {"date": "2027-05-01"}}),
     "raceStrategy.event.date", "outside the plan"),
    ("duplicate ids", lambda p: p["weeks"][1]["days"][0]["workouts"][0].update(id="w1-tue-run"),
     "weeks[1].days[0].workouts[0].id", "Duplicate workout id 'w1-tue-run'"),
    ("empty week unflagged", lambda p: p["weeks"][2].pop("status"), "weeks[2].days", "skeleton"),
    ("bad week status", lambda p: p["weeks"][2].update(status="later"), "weeks[2].status", "isn't a week status"),
    ("negative target", lambda p: p["weeks"][0].update(targetRunKm=-1), "weeks[0].targetRunKm", "non-negative"),
]


@pytest.mark.parametrize("name, mutate, path, message", CALENDAR_CASES, ids=[c[0] for c in CALENDAR_CASES])
def test_validation_errors_carry_a_json_path(name, mutate, path, message):
    errors, _ = issues(_break(mutate))
    hits = [e for e in errors if e["path"] == path and message in e["message"]]
    assert hits, errors


def test_the_seventy_three_example_passes_cleanly(db):
    first = good_plan(id="70-3", planStartDate="2027-04-26", planEndDate="2027-06-20", totalWeeks=8,
                      eventDate="2027-06-20")
    first["weeks"] = []
    for n in range(8):
        start = date.fromordinal(date(2027, 4, 26).toordinal() + 7 * n)
        first["weeks"].append({"weekNumber": n + 1, "startDate": start.isoformat(),
                               "endDate": date.fromordinal(start.toordinal() + 6).isoformat(),
                               "status": "skeleton"})
    first["phases"] = [{"name": "All", "startWeek": 1, "endWeek": 8}]
    plan_tools.create_training_plan(first, status="future")

    result = plan_tools.create_training_plan(good_plan())   # starts Jun 28, a week after the 70.3 ends

    assert result["warnings"] == []


# ── VALIDATION: PHASES ────────────────────────────────────────────────────────

PHASE_CASES = [
    ("gap", lambda p: p["phases"][1].update(startWeek=3, endWeek=3) or p["phases"][0].update(endWeek=1),
     "phases", "Weeks 2 aren't in any phase"),
    ("overlap", lambda p: p["phases"][1].update(startWeek=2),
     "phases[1].startWeek", "Overlaps phases[0]"),
    ("beyond the plan", lambda p: p["phases"][1].update(endWeek=5), "phases[1].endWeek", "plan has 3 weeks"),
    ("backwards", lambda p: p["phases"][0].update(startWeek=2, endWeek=1), "phases[0].endWeek", "before it starts"),
    ("start date", lambda p: p["phases"][0].update(startDate="2027-06-21"),
     "phases[0].startDate", "week 1's startDate is 2027-06-28"),
    ("end date", lambda p: p["phases"][0].update(endDate="2027-07-04"),
     "phases[0].endDate", "week 2's endDate is 2027-07-11"),
    ("week phase", lambda p: p["weeks"][1].update(phase="Build"), "weeks[1].phase", "puts week 2 in 'Base'"),
]


@pytest.mark.parametrize("name, mutate, path, message", PHASE_CASES, ids=[c[0] for c in PHASE_CASES])
def test_phase_errors(name, mutate, path, message):
    errors, _ = issues(_break(mutate))
    assert any(e["path"] == path and message in e["message"] for e in errors), errors


def test_no_phases_is_only_a_warning():
    plan = good_plan()
    del plan["phases"]
    errors, warnings = issues(plan)
    assert errors == [] and paths(warnings) == ["phases"]


# ── VALIDATION: TOKENS, ENUMS ─────────────────────────────────────────────────

def _first_workout(plan):
    return plan["weeks"][0]["days"][0]["workouts"][0]


TOKEN_CASES = [
    ("no ftp", lambda p: p["zones"]["bike"]["power"].pop("ftp"), "bike-watts:ss", "zones.bike.power.ftp"),
    ("no threshold pace", lambda p: p["zones"]["run"]["pace"].pop("thresholdPace"), "run-pace:3", "thresholdPace"),
    ("no css", lambda p: p["zones"]["swim"].pop("css"), "swim-pace:4", "zones.swim.css"),
    ("unknown kind", lambda p: _first_workout(p).update(humanReadable="{{run-power:2}}"), "run-power", "unrecognised"),
    ("bad zone", lambda p: _first_workout(p).update(humanReadable="{{run-pace:ss}}"), "run-pace:ss", "no zone 'ss'"),
    ("swim zone 5a", lambda p: _first_workout(p).update(humanReadable="{{swim-pace:5a}}"), "swim-pace:5a", "no zone"),
    ("bad ftp token", lambda p: _first_workout(p).update(humanReadable="{{bike-ftp:300}}"), "bike-ftp:300", "30 to 200"),
    ("unterminated", lambda p: _first_workout(p).update(humanReadable="go {{run-pace:3"), "{{", "unterminated"),
    ("in a step note", lambda p: _first_workout(p).update(
        steps=[{"kind": "work", "durationS": 60, "note": "{{run-hr:9}}"}]), "run-hr:9", "no zone"),
]


@pytest.mark.parametrize("name, mutate, token, message", TOKEN_CASES, ids=[c[0] for c in TOKEN_CASES])
def test_unresolved_tokens_are_errors(name, mutate, token, message):
    errors, _ = issues(_break(mutate))
    assert any(token in e["message"] and message in e["message"] and "render blank" in e["message"]
               for e in errors), errors


def test_token_error_path_points_at_the_field():
    plan = good_plan()
    plan["zones"]["bike"]["power"].pop("ftp")
    errors, _ = issues(plan)
    assert "weeks[0].days[1].workouts[0].humanReadable" in paths(errors)


def test_tokens_resolve_against_overrides_too():
    plan = good_plan()
    plan["zones"]["bike"]["power"].pop("ftp")
    plan["overrides"] = {"ftp": 260}
    assert issues(plan)[0] == []


def test_sport_and_step_enums():
    plan = good_plan()
    _first_workout(plan)["sport"] = "kayak"
    errors, _ = issues(plan)
    assert any(e["path"] == "weeks[0].days[0].workouts[0].sport" and "swim" in e["message"] for e in errors)

    plan = good_plan()
    _first_workout(plan)["steps"] = [{"kind": "sprint", "durationS": 30}]
    errors, _ = issues(plan)
    assert any(e["path"].endswith("workouts[0].steps") and "steps[0].kind" in e["message"] for e in errors)

    plan = good_plan()
    plan["weeks"][1]["days"][0]["workouts"][0].update(
        sport="strength", steps=[{"kind": "exercise", "exercise": "NOT_AN_EXERCISE", "sets": 3, "reps": 8}])
    errors, _ = issues(plan)
    assert any(e["path"].endswith("workouts[0].steps") and "exercise" in e["message"] for e in errors)


# ── SKELETON, TARGETS, DRIFT ──────────────────────────────────────────────────

def test_skeleton_weeks_are_valid_when_flagged_on_the_week_or_the_plan():
    assert issues(good_plan())[0] == []
    plan = good_plan()
    del plan["weeks"][2]["status"]
    plan["skeleton"] = True
    assert issues(plan)[0] == []


def test_skeleton_plan_flag_without_empty_weeks_is_a_warning():
    plan = good_plan()
    plan["weeks"][2] = {"weekNumber": 3, "startDate": "2027-07-12", "endDate": "2027-07-18", "days": [
        day("2027-07-13", "Tuesday", {"id": "w3-tue-run", "sport": "run", "name": "Easy", "durationMinutes": 360,
                                      "distanceKm": 30})], "targetHours": 6}
    plan["skeleton"] = True
    errors, warnings = issues(plan)
    assert errors == [] and "skeleton" in paths(warnings)


def test_skeleton_status_on_a_filled_week_warns():
    plan = good_plan()
    plan["weeks"][1]["status"] = "skeleton"
    assert "weeks[1].status" in paths(issues(plan)[1])


def test_drift_beyond_ten_percent_warns_for_hours_and_run_km():
    plan = good_plan()
    plan["weeks"][0]["targetHours"] = 5      # 4 h planned: -20%
    plan["weeks"][0]["targetRunKm"] = 30     # 20 km planned
    _, warnings = issues(plan)
    messages = {w["path"]: w["message"] for w in warnings}
    assert "add up to 4 h (-20%)" in messages["weeks[0].targetHours"]
    assert "add up to 20 km" in messages["weeks[0].targetRunKm"]


def test_drift_within_ten_percent_is_quiet():
    plan = good_plan()
    plan["weeks"][0]["targetHours"] = 4.3
    plan["weeks"][0]["targetRunKm"] = 21
    assert issues(plan)[1] == []


def test_run_km_drift_notes_runs_without_a_distance():
    plan = good_plan()
    del plan["weeks"][0]["days"][2]["workouts"][0]["distanceKm"]
    _, warnings = issues(plan)
    assert any("1 run workout with no distance" in w["message"] for w in warnings)


# ── ZONES: PROVISIONAL ────────────────────────────────────────────────────────

def test_provisional_zones_need_a_reanchor_date():
    plan = good_plan()
    plan["zones"]["provisional"] = True
    errors, warnings = issues(plan)
    assert errors == [] and paths(warnings) == ["zones.reanchorBy"]

    plan["zones"]["reanchorBy"] = "2027-06-28"
    assert issues(plan) == ([], [])

    plan["zones"]["reanchorBy"] = "2027-07-05"
    assert any("after the plan starts" in w["message"] for w in issues(plan)[1])

    plan["zones"]["reanchorBy"] = "soon"
    assert "zones.reanchorBy" in paths(issues(plan)[0])


def test_get_training_plan_surfaces_provisional_zones_and_skeleton_weeks(db):
    plan = good_plan()
    plan["zones"].update(provisional=True, reanchorBy="2027-06-28")
    plan_tools.create_training_plan(plan)

    overview = plan_tools.get_training_plan("next-block-2027")

    assert overview["zonesProvisional"]["reanchorBy"] == "2027-06-28"
    week3 = overview["weekSummaries"][2]
    assert week3["status"] == "skeleton" and week3["targetHours"] == 6 and week3["targetRunKm"] == 30
    assert week3["plannedRunKm"] == 0
    assert overview["weekSummaries"][0]["plannedRunKm"] == 20


def test_provisional_zones_report_when_overdue(db):
    plan = good_plan()
    plan["zones"].update(provisional=True, reanchorBy="2027-06-28")
    plan_tools.create_training_plan(plan)
    overview = plan_service.plan_overview("next-block-2027", today=date(2027, 7, 1))
    assert overview["zonesProvisional"] == {"reanchorBy": "2027-06-28", "daysLeft": -3, "overdue": True}


def test_include_weeks_false_is_a_plan_level_read(db):
    plan_tools.create_training_plan(good_plan())
    overview = plan_tools.get_training_plan("next-block-2027", sections=["zones", "phases"], include_weeks=False)
    assert "weeks" not in overview and "weekSummaries" not in overview
    assert set(overview["sections"]) == {"zones", "phases"}


# ── OVERLAP ───────────────────────────────────────────────────────────────────

def test_overlap_with_another_plan_warns_but_does_not_block(db):
    plan_tools.create_training_plan(good_plan(id="existing-block", event="Existing Block"))

    result = plan_tools.create_training_plan(good_plan(id="overlapper"))

    overlap = [w for w in result["warnings"] if "Overlaps" in w["message"]]
    assert len(overlap) == 1
    assert "Existing Block" in overlap[0]["message"] and "21 days" in overlap[0]["message"]
    assert "overlapper" in db.plans


def test_archived_plans_are_ignored_for_overlap(db):
    plan_tools.create_training_plan(good_plan(), status="archived")
    result = plan_tools.create_training_plan(good_plan(id="second"))
    assert result["warnings"] == []


# ── replace_week ──────────────────────────────────────────────────────────────

def filled_week(**overrides):
    week = {"weekNumber": 3, "targetHours": 6, "targetRunKm": 30, "days": [
        day("2027-07-13", "Tuesday", {"sport": "run", "name": "Tempo", "durationMinutes": 180,
                                      "distanceKm": 30, "humanReadable": "{{run-pace:3}}"}),
        day("2027-07-15", "Thursday", {"sport": "bike", "name": "Endurance", "durationMinutes": 180,
                                       "distanceKm": 70}),
    ]}
    week.update(overrides)
    return week


@pytest.fixture
def skeleton(db):
    plan_tools.create_training_plan(good_plan())
    return db


def replace(week, number=3, plan_id="next-block-2027"):
    return plan_tools.amend_training_plan(
        [{"op": "replace_week", "week_number": number, "week": week}], "Fill week 3", plan_id)


def test_replace_week_fills_a_skeleton_week(skeleton):
    result = replace(filled_week())

    week = skeleton.plans["next-block-2027"]["plan"]["weeks"][2]
    assert week["startDate"] == "2027-07-12" and week["endDate"] == "2027-07-18"   # from the slot
    assert week["phase"] == "Build"                                                # from the phases block
    assert "status" not in week or week["status"] != "skeleton"
    assert [w["id"] for d in week["days"] for w in d["workouts"]] == ["w3-tue-run", "w3-thu-bike"]
    assert week["summary"]["totalHours"] == 6 and week["summary"]["bySport"]["run"]["km"] == 30
    assert result["warnings"] == [] and result["version"] == 2
    assert result["workout_ids"] == ["w3-tue-run", "w3-thu-bike"]
    assert [w["weekNumber"] for w in result["weeks"]] == [3]
    assert result["changes"] == ["Replaced week 3 (2 workouts)"]


def test_replace_week_flips_a_skeleton_status_and_clears_the_plan_flag(skeleton):
    skeleton.plans["next-block-2027"]["plan"]["skeleton"] = True
    replace(filled_week(status="skeleton"))
    stored = skeleton.plans["next-block-2027"]["plan"]
    assert stored["weeks"][2]["status"] == "planned" and stored["skeleton"] is False


def test_replace_week_can_go_back_to_a_skeleton(skeleton):
    replace(filled_week())
    replace({"status": "skeleton", "targetHours": 5})
    week = skeleton.plans["next-block-2027"]["plan"]["weeks"][2]
    assert week["status"] == "skeleton" and "summary" not in week and week["days"] == []


def test_replace_week_is_one_revision_and_atomic(skeleton):
    with pytest.raises(ToolError, match=r"weeks\[2\]\.days\[0\]\.date"):
        plan_tools.amend_training_plan([
            {"op": "update_week", "week_number": 1, "fields": {"focus": "Changed"}},
            {"op": "replace_week", "week_number": 3, "week": filled_week(days=[
                day("2027-07-20", "Tuesday", {"sport": "run", "name": "x", "durationMinutes": 30})])},
        ], "Bad", "next-block-2027")
    assert skeleton.plans["next-block-2027"]["version"] == 1
    assert "focus" not in skeleton.plans["next-block-2027"]["plan"]["weeks"][0]


REPLACE_ERRORS = [
    ("dates differ", lambda: filled_week(startDate="2027-07-19", endDate="2027-07-25"), "weeks can't be moved"),
    ("week number", lambda: filled_week(weekNumber=4), "can't be renumbered"),
    ("day outside week", lambda: filled_week(days=[day("2027-07-06", "Tuesday", {
        "sport": "run", "name": "x", "durationMinutes": 30})]), "outside the week"),
    ("dayOfWeek", lambda: filled_week(days=[day("2027-07-13", "Monday", {
        "sport": "run", "name": "x", "durationMinutes": 30})]), "is a Tuesday"),
    ("phase", lambda: filled_week(phase="Base"), "puts week 3 in 'Build'"),
    ("duplicate id elsewhere", lambda: filled_week(days=[day("2027-07-13", "Tuesday", {
        "id": "w1-tue-run", "sport": "run", "name": "x", "durationMinutes": 30})]), "Duplicate workout id"),
    ("token", lambda: filled_week(days=[day("2027-07-13", "Tuesday", {
        "sport": "run", "name": "x", "durationMinutes": 30, "humanReadable": "{{bike-watts:9}}"})]), "no zone"),
    ("sport", lambda: filled_week(days=[day("2027-07-13", "Tuesday", {
        "sport": "kayak", "name": "x", "durationMinutes": 30})]), "isn't a sport"),
    ("empty unflagged", lambda: {"targetHours": 5}, "no workouts"),
]


@pytest.mark.parametrize("name, build, message", REPLACE_ERRORS, ids=[c[0] for c in REPLACE_ERRORS])
def test_replace_week_validates_the_week_against_the_plan(skeleton, name, build, message):
    if name == "empty unflagged":
        skeleton.plans["next-block-2027"]["plan"]["weeks"][2].pop("status")
    with pytest.raises(ToolError, match=message):
        replace(build())
    assert skeleton.plans["next-block-2027"]["version"] == 1


def test_replace_week_warns_on_drift_with_a_plan_path(skeleton):
    result = replace(filled_week(targetHours=10, targetRunKm=50))
    assert {w["path"] for w in result["warnings"]} == {"weeks[2].targetHours", "weeks[2].targetRunKm"}


def test_replace_week_numbers_that_do_not_exist(skeleton):
    with pytest.raises(ToolError, match="No week 9"):
        replace(filled_week(), number=9)


def test_replace_week_warns_about_completed_workouts_that_disappear(skeleton):
    replace(filled_week())
    plan_service.set_completion("next-block-2027", "w3-tue-run", True)
    result = replace(filled_week(days=[day("2027-07-15", "Thursday", {
        "sport": "bike", "name": "Endurance", "durationMinutes": 360, "distanceKm": 70})]))
    assert any("w3-tue-run" in w["message"] for w in result["warnings"])


# ── DELETE ────────────────────────────────────────────────────────────────────

def test_delete_needs_confirmation_and_spares_the_active_plan(db):
    plan_tools.create_training_plan(good_plan())
    with pytest.raises(ToolError, match="permanent"):
        plan_tools.delete_training_plan("next-block-2027", "yes")
    assert "next-block-2027" in db.plans

    assert plan_tools.delete_training_plan("next-block-2027", "next-block-2027") == \
        {"deleted": "next-block-2027", "was": "future"}
    assert db.plans == {}

    plan_service.save_upload(sample_plan())
    with pytest.raises(ToolError, match="Archive the active plan"):
        plan_tools.delete_training_plan("test-block-2026", "test-block-2026")


def test_archive_then_recreate_with_replace(db):
    plan_tools.create_training_plan(good_plan())
    plan_tools.set_training_plan_status("next-block-2027", "archived")
    with pytest.raises(ToolError, match="already exists"):
        plan_tools.create_training_plan(good_plan())
