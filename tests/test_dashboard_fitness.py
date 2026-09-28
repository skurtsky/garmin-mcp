# tests/test_dashboard_fitness.py
"""The Fitness tab's thresholds history, race predictions and records
progress (tools/dashboard_data.py + tools/dashboard_fitness.py). Offline —
the db reads are monkeypatched."""
from datetime import date

import pytest

import db
from tools import dashboard_data, dashboard_fitness as df, race_predictor

TODAY = date(2026, 9, 25)


def _snap(day, source, metric, value):
    return {"snapshot_date": day, "source": source, "metric": metric, "value": value}


@pytest.fixture
def snapshots(monkeypatch):
    rows = [
        _snap("2025-10-01", "plan", "ftp", 228), _snap("2026-01-10", "plan", "ftp", 236),
        _snap("2026-09-25", "plan", "ftp", 248),
        _snap("2025-10-01", "garmin", "ftp", 255), _snap("2026-09-25", "garmin", "ftp", 275),
        _snap("2025-10-01", "garmin", "threshold_pace_s", 270), _snap("2026-06-01", "garmin", "threshold_pace_s", 255),
    ]
    monkeypatch.setattr(db, "get_threshold_snapshots", lambda a, b: rows)
    return rows


def test_threshold_history_prefers_the_plan_and_marks_tests(snapshots):
    h = dashboard_data.threshold_history(TODAY, None, None)
    ftp = h["ftp"]
    assert ftp["source"] == "plan" and len(ftp["monthly"]) == 12
    assert ftp["monthly"][-1] == 248 and ftp["monthly"][0] == 228
    assert ftp["change"] == 20 and ftp["improving"] is True
    assert [c["value"] for c in ftp["changes"]] == [228, 236, 248]
    assert len(ftp["test_months"]) == 2
    assert ftp["zone_note"] == "Plan Z2 Aerobic is now 139–186 W (was 132–177 W)."
    pace = h["thresholdPace"]
    assert pace["source"] == "garmin" and pace["change"] == -15 and pace["improving"] is True   # faster
    assert h["css"]["source"] is None and h["css"]["change"] is None


def test_zone_notes_per_threshold():
    assert dashboard_data._z2_note("runLthr", 168, 170) == "Plan Z2 Aerobic is now 138–151 bpm (was 136–150 bpm)."
    assert dashboard_data._z2_note("css", 125, 121) == "Plan Z2 Aerobic is now 2:09–2:13/100m (was 2:13–2:17/100m)."
    assert dashboard_data._z2_note("ftp", 248, 248) is None


def test_threshold_rows_units_sparkline_and_dialog(snapshots):
    h = dashboard_data.threshold_history(TODAY, None, None)
    plan = {"thresholds_now": {"ftp": 248, "bikeLthr": 164, "runLthr": 170, "thresholdPace": "4:15/km", "css": "2:01/100m"},
            "thresholds": [{"key": "ftp", "status": "Tested"}, {"key": "runLthr", "status": "Provisional"}]}
    athlete = {"ftp": 275, "lactate_threshold_hr": 168, "lactate_threshold_pace": 4.1667}
    html = df.thresholds(plan, athlete, h)
    for text in (">248 W<", ">275 W<", ">164 bpm<", ">168 bpm<", ">4:15/km<", ">4:10/km<", ">2:01/100m<",
                 "↑ 20 W", "↓ 15 s", ">Tested<", ">Provisional<"):
        assert text in html, text
    assert html.count('class="thr-row"') == 5
    assert 'data-thr="ftp"' in html and "Threshold history" in html and "was 132–177 W" in html
    assert "Jan 10, 2026" in html and "first" in html


def test_threshold_rows_without_a_plan_or_history(monkeypatch):
    html = df.thresholds(None, {"ftp": 265, "lactate_threshold_hr": 170}, {})
    assert ">265 W<" in html and ">170 bpm<" in html
    assert "Bike LTHR" not in html and "Swim CSS" not in html   # nothing to show for them


def test_records_progress_new_and_close_calls(monkeypatch):
    records = {
        "cycling": [{"label": "Best 20-Min Power", "value_raw": 261, "value_formatted": "261 W",
                     "date": "2026-09-25 07:00:00", "activity_id": 9}],
        "running": [{"label": "Fastest 10K", "value_raw": 2686, "value_formatted": "44:46",
                     "date": "2026-06-14 09:00:00", "activity_id": 2},
                    {"label": "Fastest 5K", "value_raw": 1235, "value_formatted": "20:35",
                     "date": "2026-05-02 09:00:00", "activity_id": 1}],
    }
    monkeypatch.setattr(db, "get_personal_record_history", lambda since: [
        {"sport": "cycling", "record_type": "Best 20-Min Power", "record_date": date(2026, 9, 25),
         "previous_value_raw": 254, "previous_formatted": "254 W"}])
    monkeypatch.setattr(db, "get_best_efforts", lambda since: [
        {"garmin_id": 40, "sport": "running", "record_type": "Fastest 10K", "value": 2698, "activity_date": date(2026, 9, 14)},
        {"garmin_id": 41, "sport": "running", "record_type": "Fastest 10K", "value": 2740, "activity_date": date(2026, 9, 20)},
        {"garmin_id": 42, "sport": "running", "record_type": "Fastest 5K", "value": 1300, "activity_date": date(2026, 9, 20)},
        {"garmin_id": 9, "sport": "cycling", "record_type": "Best 20-Min Power", "value": 261, "activity_date": date(2026, 9, 25)},
    ])
    p = dashboard_data.records_progress(TODAY, records)
    assert p["count"] == 3
    (new,) = p["new"]
    assert (new["name"], new["previous"], new["improvement"]) == ("Best 20-Min Power", "254 W", "↑ 7 W")
    (close,) = p["close"]        # 2740 is >2% off; the 5K 5% off; the record itself doesn't count
    assert (close["name"], close["value"], close["off"], close["activity_id"]) == ("Fastest 10K", "44:58", "12 s off", 40)
    html = df.records(p, TODAY)
    assert "New · last 30 days" in html and "Today · was 254 W" in html and "↑ 7 W" in html
    assert "Sep 14 · record 44:46" in html and "12 s off" in html and "openActivityModal(40)" in html
    assert "All 3" in html and "data-prs-open" in html


def test_record_improvement_wording():
    assert dashboard_data._improvement("time", 451, 457) == "6 s faster"
    assert dashboard_data._improvement("time", 5500, 5600) == "1:40 faster"
    assert dashboard_data._improvement("distance", 42000, 40000) == "↑ 2.0 km"


def _preds():
    run = race_predictor.run_predictions({"5K": 1205, "10K": 2530, "half_marathon": 5620, "marathon": 11920},
                                         {"10K": 2595}, {"running": [{"label": "Fastest 10K", "value_raw": 2686}]})
    return {"run": run, "tri": race_predictor.tri_predictions(121, 248, 255), "trend_days": 90}


def test_run_predictions_with_goal_highlight():
    goal = {"predictor_distance": "half_marathon", "distance": "half", "date": "2026-10-11", "target": "1:32:00",
            "target_sec": 5520, "is_triathlon": False}
    html = df.predictions(_preds(), goal)
    assert 'id="rp-run" checked' in html
    assert "Record 44:46" in html and "2:36 faster" in html and "↓ 1:05 in 3 mo" in html
    assert "No record yet" in html
    goal_row = html[html.index("rp-goal"):]
    assert "Goal race · Oct 11" in goal_row and "Target 1:32:00 · 1:40 to find" in goal_row
    assert html.count("rp-row rp-goal") == 1


def test_tri_goal_opens_the_triathlon_list():
    goal = {"predictor_distance": None, "distance": "olympic", "date": "2026-10-11", "target": None,
            "target_sec": None, "is_triathlon": True}
    html = df.predictions(_preds(), goal)
    assert 'id="rp-tri" checked' in html
    tri = html[html.index('class="card rp-card rp-tri"'):]
    assert tri.count('class="rp-leg"') == 16 and "2:04/100m" in tri and "228 W" in tri
    assert "Estimated from CSS, FTP, and threshold pace" in html
    assert "rp-goal" in tri and "Target" not in tri    # no target set: no target line


def test_predictions_note_missing_inputs():
    preds = {"run": race_predictor.run_predictions({}, {}, None),
             "tri": race_predictor.tri_predictions(None, None, 255), "trend_days": 90}
    html = df.predictions(preds, None)
    assert 'id="rp-tri" checked' in html            # no Garmin predictions yet
    assert "needs CSS or a swim record, FTP" in html
    assert "No Garmin race predictions synced yet." in html
