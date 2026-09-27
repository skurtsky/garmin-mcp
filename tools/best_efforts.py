# tools/best_efforts.py
"""Best efforts inside one activity — the fastest 5K within a long run, the
best 20 minutes of a ride, the fastest 100 m of a swim set — so the Fitness
page can show "close calls": recent efforts just short of a personal record.

Pure functions over Garmin's per-activity payloads (no I/O). Each effort is
keyed by the same ``record_type`` label the personal-records sync stores
(tools/performance.py's _PR_TYPES), so an effort and the record it chases
join on (sport, record_type) and share units: seconds for times, watts for
power.

Sources:
* runs and outdoor rides — Garmin's per-sample distance (``sumDistance``)
  against its timestamps, from the activity-details endpoint;
* best 20-minute power — the stored power series;
* pool swims — the lengths inside each lap (a lap is one unbroken set).
"""

# (record_type, metres) per sport, matching the personal-records labels.
RUN_DISTANCES = (
    ("Fastest 1K", 1000.0), ("Fastest Mile", 1609.344), ("Fastest 5K", 5000.0),
    ("Fastest 10K", 10000.0), ("Fastest Half Marathon", 21097.5),
    ("Fastest Marathon", 42195.0),
)
RIDE_DISTANCES = (("Fastest 40K", 40000.0),)
SWIM_DISTANCES = (("Fastest 100m Swim", 100.0), ("Fastest 400m Swim", 400.0))
POWER_RECORD = "Best 20-Min Power"

# Garmin types whose distance counts toward run/ride records. Treadmill and
# indoor/virtual rides have no real distance, so only power counts for them.
_OUTDOOR_RUN_TYPES = {"running", "street_running", "trail_running", "track_running"}
_RIDE_TYPES = {"road_biking", "cycling", "gravel_cycling", "mountain_biking",
               "virtual_ride", "indoor_cycling"}
_OUTDOOR_RIDE_TYPES = {"road_biking", "cycling", "gravel_cycling", "mountain_biking"}
_POOL_SWIM_TYPES = {"lap_swimming"}

# A recording gap is a pause when it is this many times the typical sample
# spacing (Garmin downsamples long activities, so a fixed cut-off would read
# every sample of a six-hour ride as a pause) and at least this many seconds.
_PAUSE_FACTOR = 3.0
_PAUSE_MIN_SEC = 8.0


def best_rolling_power(series: list[dict], window_sec: int = 1200) -> int | None:
    """Best average power over any ``window_sec`` stretch of an activity's
    power samples (``{t_offset_sec, value}``, as stored in activity_details).
    Each sample holds until the next one, with gaps over 5s (pauses) not
    counted. None when the ride is shorter than the window."""
    pts = [(p["t_offset_sec"], p["value"] or 0) for p in series or [] if p.get("t_offset_sec") is not None]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < window_sec:
        return None
    # (duration, energy) per sample, then a sliding window over time.
    spans = [(min(pts[i + 1][0] - pts[i][0], 5.0), pts[i][1]) for i in range(len(pts) - 1)]
    best, lo, dur, energy = None, 0, 0.0, 0.0
    for dt, watts in spans:
        dur += dt
        energy += dt * watts
        while dur - spans[lo][0] >= window_sec:
            dur -= spans[lo][0]
            energy -= spans[lo][0] * spans[lo][1]
            lo += 1
        if dur >= window_sec:
            best = max(best or 0, energy / dur)
    return round(best) if best is not None else None


def distance_series(details_raw: dict | None) -> list[tuple[float, float]]:
    """(moving seconds, metres) samples from Garmin's activityDetailMetrics.

    Time is the timer's view: a recording gap (auto-pause) adds nothing, so
    a 5K spanning a stop at a light isn't slowed by the minutes spent paused.
    Distance never goes backwards (GPS jitter is clamped)."""
    descriptors = (details_raw or {}).get("metricDescriptors") or []
    samples = (details_raw or {}).get("activityDetailMetrics") or []
    index = {d.get("key"): d.get("metricsIndex") for d in descriptors}
    ts_idx, dist_idx = index.get("directTimestamp"), index.get("sumDistance")
    if ts_idx is None or dist_idx is None:
        return []
    raw = []
    for sample in samples:
        values = sample.get("metrics") or []
        if max(ts_idx, dist_idx) >= len(values):
            continue
        ts, dist = values[ts_idx], values[dist_idx]
        if ts is None or dist is None:
            continue
        raw.append((ts / 1000.0, float(dist)))
    if len(raw) < 2:
        return []
    gaps = sorted(b[0] - a[0] for a, b in zip(raw, raw[1:]) if b[0] > a[0])
    typical = gaps[len(gaps) // 2] if gaps else 1.0
    pause_after = max(_PAUSE_MIN_SEC, typical * _PAUSE_FACTOR)
    out = [(0.0, raw[0][1])]
    for (t0, _), (t1, d1) in zip(raw, raw[1:]):
        dt = t1 - t0
        moving = out[-1][0] + (dt if 0 < dt <= pause_after else 0.0)
        out.append((moving, max(d1, out[-1][1])))
    return out


def fastest_distance(series: list[tuple[float, float]], metres: float) -> float | None:
    """Seconds for the fastest stretch covering ``metres``, or None when the
    activity is shorter. The start point is interpolated between samples, so
    the result doesn't depend on where the samples happen to fall."""
    if len(series) < 2 or series[-1][1] - series[0][1] < metres:
        return None
    best, i = None, 0
    for j in range(1, len(series)):
        tj, dj = series[j]
        target = dj - metres
        if target < series[0][1]:
            continue
        while i + 1 < j and series[i + 1][1] <= target:
            i += 1
        ti, di = series[i]
        tn, dn = series[i + 1]
        start = ti if dn <= di else ti + (target - di) / (dn - di) * (tn - ti)
        took = tj - start
        if took > 0 and (best is None or took < best):
            best = took
    return round(best, 1) if best is not None else None


def swim_efforts(laps_raw: dict | None) -> dict[str, float]:
    """Fastest 100 m / 400 m inside a pool swim. Each lap is one unbroken set;
    within it the consecutive lengths are windowed. A lap without per-length
    data counts at its average pace when it's at least the distance."""
    best: dict[str, float] = {}

    def offer(record, secs):
        if secs and secs > 0 and (record not in best or secs < best[record]):
            best[record] = round(secs, 1)

    for lap in (laps_raw or {}).get("lapDTOs") or []:
        lengths = [(float(x.get("distance") or 0), float(x.get("duration") or 0))
                   for x in lap.get("lengthDTOs") or []
                   if (x.get("distance") or 0) > 0 and (x.get("duration") or 0) > 0]
        lap_m, lap_s = float(lap.get("distance") or 0), float(lap.get("duration") or 0)
        for record, metres in SWIM_DISTANCES:
            if lengths:
                for start in range(len(lengths)):
                    dist = secs = 0.0
                    for d, s in lengths[start:]:
                        dist += d
                        secs += s
                        if dist >= metres:
                            # Pools that don't divide the distance (33⅓ m)
                            # overshoot; scale back to the exact distance.
                            offer(record, secs * metres / dist)
                            break
            elif lap_m >= metres and lap_s > 0:
                offer(record, lap_s * metres / lap_m)
    return best


def activity_efforts(activity_type: str | None, details_raw: dict | None = None,
                     laps_raw: dict | None = None, power_series: list[dict] | None = None) -> list[dict]:
    """Every record-type effort in one activity:
    ``[{sport, record_type, value}]`` with sport the personal-records
    category (running / cycling / swimming)."""
    kind = (activity_type or "").lower()
    efforts = []

    def add(sport, record, value):
        if value:
            efforts.append({"sport": sport, "record_type": record, "value": value})

    if kind in _OUTDOOR_RUN_TYPES:
        series = distance_series(details_raw)
        for record, metres in RUN_DISTANCES:
            add("running", record, fastest_distance(series, metres))
    elif kind in _RIDE_TYPES:
        add("cycling", POWER_RECORD, best_rolling_power(power_series or []))
        if kind in _OUTDOOR_RIDE_TYPES:
            series = distance_series(details_raw)
            for record, metres in RIDE_DISTANCES:
                add("cycling", record, fastest_distance(series, metres))
    elif kind in _POOL_SWIM_TYPES:
        for record, secs in swim_efforts(laps_raw).items():
            add("swimming", record, secs)
    return efforts
