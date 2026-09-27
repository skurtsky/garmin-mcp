# tests/test_commutes_goal_race.py
"""Commute detection (tools/commutes.py) and the goal race setting
(tools/goal_race.py). Offline."""
from datetime import date

import pytest

import db
from tools import commutes, goal_race

HOME = (45.4000, -75.7000)
WORK = (45.4300, -75.6800)   # ~3.6 km away


def _ride(gid, day, start, end, km=18.0):
    return {"garmin_id": gid, "activity_type": "road_biking", "distance_km": km,
            "summary": {"date": f"{day} 08:00:00", "start_lat": start[0], "start_lon": start[1],
                        "end_lat": end[0], "end_lon": end[1]}}


def test_repeated_point_to_point_trips_are_commutes():
    rides = [
        _ride(1, "2026-09-21", HOME, WORK),
        _ride(2, "2026-09-21", WORK, HOME, km=19.0),   # reverse direction counts
        _ride(3, "2026-09-22", HOME, WORK, km=17.0),
    ]
    assert commutes.detect(rides) == {1: True, 2: True, 3: True}


def test_loops_are_never_commutes():
    loop = [_ride(i, f"2026-09-{20 + i}", HOME, (45.4005, -75.7003), km=70) for i in range(1, 5)]
    assert commutes.detect(loop) == {1: False, 2: False, 3: False, 4: False}


def test_needs_enough_trips_similar_distance_and_within_window():
    two = [_ride(1, "2026-09-21", HOME, WORK), _ride(2, "2026-09-22", WORK, HOME)]
    assert commutes.detect(two) == {1: False, 2: False}

    longer = two + [_ride(3, "2026-09-23", HOME, WORK, km=30)]  # >15% longer: not the same trip
    assert commutes.detect(longer)[3] is False

    far_apart = two + [_ride(3, "2026-12-01", HOME, WORK)]
    assert commutes.detect(far_apart)[1] is False


def test_rides_without_coordinates_are_not_commutes():
    ride = {"garmin_id": 9, "distance_km": 20, "summary": {"date": "2026-09-21 08:00:00"}}
    assert commutes.detect([ride]) == {9: False}


def test_overrides_win(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: True)
    rides = [_ride(1, "2026-09-21", HOME, WORK), _ride(2, "2026-09-21", WORK, HOME),
             _ride(3, "2026-09-22", HOME, WORK)]
    monkeypatch.setattr(db, "get_rides_in_range", lambda a, b: rides)
    monkeypatch.setattr(db, "get_activity_overrides",
                        lambda ids: {1: {"is_commute": False}, 7: {"is_commute": True}})
    run = {"garmin_id": 7, "activity_type": "running", "summary": {"date": "2026-09-22 07:00:00"}}
    flags = commutes.commute_flags([rides[0], rides[1], run])
    assert flags == {1: False, 2: True, 7: True}


# ── goal race ────────────────────────────────────────────────────────────────

def test_validate_goal_race():
    goal = goal_race.validate({"name": " Fall Classic ", "date": "2026-10-11", "distance": "half",
                               "target": "1:32:00", "show_on_today": "on"})
    assert goal == {"name": "Fall Classic", "date": "2026-10-11", "distance": "half",
                    "target_sec": 5520, "show_on_today": True}
    assert goal_race.validate({"name": "A", "date": "2026-10-11", "distance": "5k"})["target_sec"] is None


@pytest.mark.parametrize("bad", [
    {"date": "2026-10-11", "distance": "half"},
    {"name": "A", "date": "11/10/2026", "distance": "half"},
    {"name": "A", "date": "2026-10-11", "distance": "ultra"},
    {"name": "A", "date": "2026-10-11", "distance": "half", "target": "fast"},
    {"name": "A", "date": "2026-10-11", "distance": "half", "target_sec": -5},
])
def test_invalid_goal_race(bad):
    with pytest.raises(goal_race.GoalRaceError):
        goal_race.validate(bad)


@pytest.mark.parametrize("today,phase,days,show_form", [
    (date(2026, 8, 1), "build", 71, False),
    (date(2026, 9, 25), "build", 16, True),
    (date(2026, 10, 1), "taper", 10, True),
    (date(2026, 10, 11), "race_day", 0, True),
    (date(2026, 10, 12), "done", -1, False),
])
def test_describe_phases(today, phase, days, show_form):
    goal = goal_race.validate({"name": "Fall Classic", "date": "2026-10-11", "distance": "half", "target": "1:32:00"})
    d = goal_race.describe(goal, today)
    assert (d["phase"], d["days_left"], d["show_form"]) == (phase, days, show_form)
    assert d["taper_start"] == "2026-10-01"
    assert d["target"] == "1:32:00"
    assert d["predictor_distance"] == "half_marathon"


def test_taper_by_distance():
    assert {k: v[2] for k, v in goal_race.DISTANCES.items()} == {
        "5k": 7, "10k": 7, "sprint": 7, "half": 10, "olympic": 10, "70.3": 14, "marathon": 21, "140.6": 21}


def test_get_goal_race_ignores_invalid_stored_value(monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "get_setting", lambda key: {"name": "x"})
    assert goal_race.get_goal_race() is None
