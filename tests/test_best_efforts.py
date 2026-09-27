# tests/test_best_efforts.py
"""Best efforts inside an activity (tools/best_efforts.py) and their place in
the activity-detail sync payload. Offline — Garmin payloads are built here."""
import pytest

from tools import activities, best_efforts
from tools.best_efforts import activity_efforts, distance_series, fastest_distance, swim_efforts


def _details(samples):
    """A Garmin activity-details payload from (seconds, metres) samples."""
    return {
        "metricDescriptors": [{"key": "directTimestamp", "metricsIndex": 0},
                              {"key": "sumDistance", "metricsIndex": 1}],
        "activityDetailMetrics": [{"metrics": [1_700_000_000_000 + t * 1000, d]} for t, d in samples],
    }


def _steady(total_m, speed, step=5):
    """Samples every ``step`` seconds at a constant speed (m/s)."""
    out, t = [], 0
    while True:
        d = min(total_m, t * speed)
        out.append((t, d))
        if d >= total_m:
            return out
        t += step


def test_fastest_distance_steady_pace():
    series = distance_series(_details(_steady(12000, 4.0)))
    assert fastest_distance(series, 5000) == pytest.approx(1250, abs=0.5)
    assert fastest_distance(series, 10000) == pytest.approx(2500, abs=0.5)
    assert fastest_distance(series, 21097.5) is None


def test_fastest_distance_finds_the_fast_stretch_inside_a_long_run():
    # 3 km easy (3 m/s), 5 km fast (5 m/s), 3 km easy.
    samples, t, d = [], 0, 0.0
    for metres, speed in ((3000, 3.0), (5000, 5.0), (3000, 3.0)):
        end = d + metres
        while d < end:
            samples.append((t, d))
            t += 2
            d = min(end, d + speed * 2)
    samples.append((t, d))
    series = distance_series(_details(samples))
    assert fastest_distance(series, 5000) == pytest.approx(1000, abs=2)


def test_pause_does_not_count():
    # 2.5 km, a 5-minute stop (no samples), then 2.5 km, all at 4 m/s.
    first = _steady(2500, 4.0)
    last_t = first[-1][0]
    second = [(last_t + 300 + t, 2500 + d) for t, d in _steady(2500, 4.0)[1:]]
    series = distance_series(_details(first + second))
    assert fastest_distance(series, 5000) == pytest.approx(1250, abs=5)


def test_downsampled_long_ride_is_not_read_as_paused():
    # A long ride sampled every 12 s: gaps beyond the fixed 8 s cut-off are
    # still normal spacing, not pauses.
    series = distance_series(_details(_steady(45000, 10.0, step=12)))
    assert fastest_distance(series, 40000) == pytest.approx(4000, abs=1)


def test_distance_series_without_distance_metric():
    details = {"metricDescriptors": [{"key": "directTimestamp", "metricsIndex": 0}],
               "activityDetailMetrics": [{"metrics": [1]}, {"metrics": [2]}]}
    assert distance_series(details) == []
    assert distance_series(None) == []


def test_swim_efforts_from_lengths_break_at_laps():
    laps = {"lapDTOs": [
        # 8 × 25 m at 25 s, then a slow set: best 100 = 4 × 25 s.
        {"distance": 200, "duration": 200,
         "lengthDTOs": [{"distance": 25, "duration": 25}] * 8},
        {"distance": 400, "duration": 480,
         "lengthDTOs": [{"distance": 25, "duration": 30}] * 16},
    ]}
    best = swim_efforts(laps)
    assert best["Fastest 100m Swim"] == 100
    # Only the second lap is 400 m unbroken.
    assert best["Fastest 400m Swim"] == 480


def test_swim_efforts_scales_uneven_pool():
    laps = {"lapDTOs": [{"distance": 133.3, "duration": 120,
                         "lengthDTOs": [{"distance": 33.33, "duration": 30}] * 4}]}
    assert swim_efforts(laps)["Fastest 100m Swim"] == pytest.approx(90, abs=0.1)


def test_swim_efforts_lap_without_lengths_uses_average_pace():
    laps = {"lapDTOs": [{"distance": 500, "duration": 550}]}
    best = swim_efforts(laps)
    assert best == {"Fastest 100m Swim": 110.0, "Fastest 400m Swim": 440.0}


def test_activity_efforts_by_sport():
    run = activity_efforts("running", _details(_steady(5200, 4.0)))
    assert {e["record_type"] for e in run} == {"Fastest 1K", "Fastest Mile", "Fastest 5K"}
    assert all(e["sport"] == "running" for e in run)

    power = [{"t_offset_sec": t, "value": 250} for t in range(0, 1300)]
    indoor = activity_efforts("indoor_cycling", _details(_steady(45000, 10.0)), power_series=power)
    assert indoor == [{"sport": "cycling", "record_type": "Best 20-Min Power", "value": 250}]

    outdoor = activity_efforts("road_biking", _details(_steady(45000, 10.0)), power_series=power)
    assert {e["record_type"] for e in outdoor} == {"Best 20-Min Power", "Fastest 40K"}

    assert activity_efforts("treadmill_running", _details(_steady(6000, 4.0))) == []
    assert activity_efforts("bouldering") == []


def test_detail_row_carries_best_efforts(monkeypatch):
    class Fake:
        def get_activity(self, i):
            return {"activityId": 5, "activityTypeDTO": {"typeKey": "running"}, "summaryDTO": {"duration": 1300}}

        def get_activity_splits(self, i):
            return None

        def get_activity_weather(self, i):
            return None

        def get_activity_details(self, i):
            return _details(_steady(5000, 4.0))

        def get_activity_hr_in_timezones(self, i):
            return []

    monkeypatch.setattr("tools.activities.get_client", lambda: Fake())
    monkeypatch.setattr("tools.activities.get_activity_gear", lambda i: [])
    detail, _ = activities.get_activity_detail_row(5)
    five_k = next(e for e in detail["best_efforts"] if e["record_type"] == "Fastest 5K")
    assert five_k["value"] == pytest.approx(1250, abs=0.5)


def test_summary_from_list_stores_dashboard_fields():
    raw = {
        "activityId": 1, "activityName": "Commute", "activityType": {"typeKey": "road_biking"},
        "startTimeLocal": "2026-09-21 08:00:00", "distance": 18000, "duration": 3600,
        "averageHR": 130, "activityTrainingLoad": 30.4, "averageSpeed": 5.0,
        "avgPower": 151.6, "normPower": 160.2, "trainingEffectLabel": "AEROBIC_BASE",
        "aerobicTrainingEffect": 2.84, "startLatitude": 45.4, "startLongitude": -75.7,
        "endLatitude": 45.42, "endLongitude": -75.69, "eventType": {"typeKey": "uncategorized"},
    }
    out = activities._activity_summary_from_list(raw)
    assert out["avg_speed_kph"] == 18.0
    assert (out["avg_power"], out["normalized_power"]) == (152, 160)
    assert out["training_effect_label"] == "AEROBIC_BASE"
    assert out["aerobic_te"] == 2.8
    assert (out["start_lat"], out["end_lon"]) == (45.4, -75.69)
    assert "pool_length_m" not in out and "anaerobic_te" not in out


def test_summary_from_list_pool_length_in_metres():
    raw = {"activityId": 2, "activityType": {"typeKey": "lap_swimming"}, "poolLength": 2500.0,
           "unitOfPoolLength": {"unitKey": "meter", "factor": 100.0}, "activeLengths": 80}
    out = activities._activity_summary_from_list(raw)
    assert out["pool_length_m"] == 25.0
    assert out["active_lengths"] == 80


def test_best_rolling_power_still_exported_from_plan_today():
    from tools import plan_today
    assert plan_today.best_rolling_power is best_efforts.best_rolling_power


# ── detail fetch resilience (Garmin 5xx during a detail sync) ────────────────

def _flaky_client(details_errors, splits_errors=()):
    details_errors, splits_errors = list(details_errors), list(splits_errors)

    class Flaky:
        def get_activity(self, i):
            return {"activityId": i, "activityTypeDTO": {"typeKey": "running"}, "summaryDTO": {"duration": 1300}}

        def get_activity_splits(self, i):
            if splits_errors:
                raise splits_errors.pop(0)
            return {"lapDTOs": []}

        def get_activity_weather(self, i):
            raise RuntimeError("API Error 504 - weather is optional")

        def get_activity_details(self, i):
            if details_errors:
                raise details_errors.pop(0)
            return _details(_steady(5000, 4.0))

        def get_activity_hr_in_timezones(self, i):
            return []

    return Flaky()


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr("tools.activities.time.sleep", sleeps.append)
    monkeypatch.setattr("tools.activities.get_activity_gear", lambda i: [])
    return sleeps


def test_detail_row_retries_a_garmin_504_once(monkeypatch, no_sleep):
    err = RuntimeError("HTTP error: API Error 504 - Gateway time-out")
    monkeypatch.setattr("tools.activities.get_client", lambda: _flaky_client([err]))
    detail, _ = activities.get_activity_detail_row(5)
    assert no_sleep == [activities._TRANSIENT_RETRY_DELAY_SEC]
    assert any(e["record_type"] == "Fastest 5K" for e in detail["best_efforts"])
    assert detail["weather"] is None   # optional parts still degrade quietly


def test_detail_row_raises_when_garmin_keeps_failing(monkeypatch, no_sleep):
    errs = [RuntimeError("API Error 504"), RuntimeError("API Error 502")]
    monkeypatch.setattr("tools.activities.get_client", lambda: _flaky_client([], splits_errors=errs))
    with pytest.raises(RuntimeError, match="502"):
        activities.get_activity_detail_row(5)


def test_detail_row_client_error_means_no_data(monkeypatch, no_sleep):
    err = RuntimeError("API client error (404): not found")
    monkeypatch.setattr("tools.activities.get_client", lambda: _flaky_client([err]))
    detail, route = activities.get_activity_detail_row(5)
    assert no_sleep == []
    assert detail["best_efforts"] == [] and route is None


def test_detail_sync_keeps_the_stored_row_when_an_activity_fails(monkeypatch):
    import db
    import sync_garmin
    written = []
    monkeypatch.setattr(db, "get_recent_activity_ids", lambda limit: [1, 2, 3])
    monkeypatch.setattr(db, "upsert_activity_detail", lambda gid, detail, route: written.append(gid))
    monkeypatch.setattr(db, "update_sync_state", lambda *a, **k: None)

    def row(activity_id):
        if activity_id == 2:
            raise RuntimeError("API Error 504")
        return {}, None

    monkeypatch.setattr("tools.activities.get_activity_detail_row", row)
    sync_garmin.sync_activity_details(limit=10, overwrite=True)
    assert written == [1, 3]
