# tests/test_build_pdf.py
"""Tests for the printable plan PDF (tools/build_pdf.py).

The formatters carry the consistency rules (one format per quantity), so they
are tested directly; the HTML is checked for structure. A real WeasyPrint
render runs only when the library and its system packages are installed.
"""
import pytest

from tools import build_pdf as b
from tests.plan_fakes import sample_plan


# ── formatters ──

@pytest.mark.parametrize("minutes, expected", [
    (50, "0:50"), (90, "1:30"), (180, "3:00"), (719, "11:59"), (0, "0:00"), (None, ""),
])
def test_hm_is_always_h_mm(minutes, expected):
    assert b.hm(minutes) == expected


@pytest.mark.parametrize("leg, value, expected", [
    ("Swim", "46:00", "0:46:00"),     # swim is minutes:seconds
    ("T1", "5:00", "0:05:00"),
    ("T2", "2:30", "0:02:30"),
    ("Bike", "3:12", "3:12:00"),      # bike/run/total are hours:minutes
    ("Run", "1:45", "1:45:00"),
    ("total", "5:32", "5:32:00"),
    ("Bike", "2:58:30", "2:58:30"),   # already h:mm:ss
    ("T1", "5:00 - long run from the swim exit", "0:05:00"),
])
def test_fmt_split_disambiguates_by_leg(leg, value, expected):
    assert b.fmt_split(leg, value) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Zone 2", "Z2"), ("Zone 2-3", "Z2–3"), ("zone 1 - 2", "Z1–2"), ("Z5b", "Z5b"),
    ("Race", ""), (None, ""),
])
def test_zone_short(raw, expected):
    assert b.zone_short(raw) == expected


def test_distance_label_units_per_sport():
    prefs = {"swim": "meters", "bike": "kilometers", "run": "kilometers"}
    assert b.distance_label({"sport": "swim", "distanceMeters": 1800}, prefs) == "1,800 m"
    assert b.distance_label({"sport": "run", "distanceMeters": 7000}, prefs) == "7 km"
    assert b.distance_label({"sport": "run", "distanceMeters": 5500}, prefs) == "5.5 km"
    assert b.distance_label({"sport": "bike", "distanceKm": 45}, prefs) == "45 km"
    assert b.distance_label({"sport": "bike"}, prefs) == ""
    assert b.distance_label({"sport": "swim", "distanceMeters": 1000}, {"swim": "yards"}) == "1,000 yd"
    assert b.distance_label({"sport": "run", "distanceKm": 16.09}, {"run": "miles"}) == "10 mi"


def test_tidy_normalizes_free_text():
    assert b.tidy("2 x 400m at Z2, 20s rest") == "2 × 400 m at Z2, 20s rest"
    assert b.tidy("Squat 3 x 5 at 170-185 lb") == "Squat 3 × 5 at 170–185 lb"
    assert b.tidy("45 min Z1-Z2.") == "45 min Z1–Z2."
    assert b.tidy("80-90K hilly route") == "80–90 km hilly route"
    assert b.tidy("Plan written 2026-09-14") == "Plan written 2026-09-14"
    assert b.tidy("Technique + 100s") == "Technique + 100s"


def test_split_side_note():
    focus, label, side = b.split_side_note(
        "Lightest week. Three easy runs. Bouldering: no flags - Friday is fine.")
    assert focus == "Lightest week. Three easy runs."
    assert label == "Bouldering"
    assert side == "no flags - Friday is fine."
    assert b.split_side_note("Just a focus.") == ("Just a focus.", "", "")


def test_zone_status_reads_the_source_note():
    assert b.zone_status("bike", {}, "TESTED: 20-min test") == "tested"
    assert b.zone_status("run", {}, "UNVALIDATED plan value") == "estimated"
    assert b.zone_status("run", {"run": {"validated": True}}, None) == "validated"
    assert b.zone_status("swim", {}, None, "VALIDATED") == "validated"


# ── structure ──

def test_week_page_has_seven_days_sorted_and_summed():
    plan = sample_plan()
    plan["weeks"][0]["days"][1]["workouts"][0]["startTime"] = "17:30"   # run
    plan["weeks"][0]["days"][1]["workouts"][1]["startTime"] = "06:00"   # strength
    html = b.week_page(plan, plan["weeks"][0], 2)

    assert html.count('class="dh"') == 7                  # missing days filled in
    assert html.count("Rest day") == 4
    assert html.index("Strength A") < html.index("Tempo run")   # by start time
    assert ">3:50<" in html                                # 45+50+45+90 minutes
    assert "99" not in html                                # ignores the stale summary
    assert "0:45 · 2,000 m" in html


def test_overview_has_phase_band_chart_and_zones():
    plan = sample_plan()
    html = b.cover_page(plan)
    assert 'class="vol"' in html and 'colspan="2"' in html
    assert html.count('class="wl"') == 2
    for title in ("Bike", "Run", "Swim"):
        assert f">{title}<" in html


def test_no_solid_black_fills():
    """Printable reference: tints and outlines only, never an ink-black block."""
    plan = sample_plan()
    html = b.build_html(plan)
    assert f"background:{b.INK}" not in html.replace(" ", "")
    assert f"background: {b.INK}" not in b.CSS


def test_race_week_and_strategy_page():
    plan = sample_plan()
    plan["raceStrategy"] = {
        "event": {"name": "Test Tri", "date": "2026-09-27",
                  "distances": {"swim": 1900, "bike": 90, "run": 21.1}},
        "pacing": {
            "swim": {"target": "2:10/100m", "projectedSplit": "41:30"},
            "bike": {"targetPower": "NP 185-190 W", "projectedSplit": "2:58",
                     "bySection": [{"section": "km 0-15 rolling", "power": "cap 180 W",
                                    "speed": "31-33 km/h", "note": "Hold back."}]},
            "run": {"targetPace": "5:00/km", "projectedSplit": "1:45",
                    "bySection": [{"section": "km 0-5 trail", "pace": "5:05/km", "note": "Easy."}]},
            "transitions": {"t1": "5:00 - long run", "t2": "2:30"},
            "projectedTotal": {"central": "5:32"},
            "goals": {"a": "sub-5:30"},
        },
        "keyRisks": ["Run durability."],
        "assessmentSummary": {"strengths": "Bike."},
    }
    plan["weeks"][1]["days"].append({"date": "2026-09-27", "dayOfWeek": "Sunday", "workouts": [
        {"id": "race", "sport": "race", "name": "Test Tri", "durationMinutes": 332,
         "startTime": "07:05"}]})

    week = b.week_page(plan, plan["weeks"][1], 2)
    assert 'class="card race"' in week and "Result" in week

    race = b.race_page(plan)
    assert "Run · pacing by section" in race          # run sections are shown too
    assert "0:41:30" in race and "2:58:00" in race and "T1 0:05:00" in race
    assert "km 0–15" in race and ">31–33<" in race


def test_real_render_one_page_per_week():
    """The fixed-height week pages must never spill onto a second sheet."""
    try:
        from weasyprint import HTML
    except (ImportError, OSError):
        pytest.skip("WeasyPrint (or its pango/harfbuzz libraries) unavailable")
    plan = sample_plan()
    pages = HTML(string=b.build_html(plan)).render().pages
    assert len(pages) == 1 + len(plan["weeks"])     # overview + one per week


# ── no-race plans, narrow phases, busy and empty weeks ──

def _long_plan():
    """24 weeks with one-week phases whose names cannot fit their cell."""
    plan = sample_plan()
    base = plan["weeks"][0]
    weeks = []
    for n in range(1, 25):
        wk = dict(base, weekNumber=n, days=[] if n == 9 else base["days"])
        weeks.append(wk)
    plan["weeks"] = weeks
    plan["phases"] = [
        {"name": "Recovery & Transition", "startWeek": 1, "endWeek": 1},
        {"name": "Base Build", "startWeek": 2, "endWeek": 23},
        {"name": "Re-entry", "startWeek": 24, "endWeek": 24},
    ]
    return plan


def test_narrow_phases_are_numbered_and_keyed():
    plan = _long_plan()
    html = b.cover_page(plan)
    assert "<b>Base Build</b>" in html
    assert "<b>Recovery &amp; Transition</b>" not in html and "<b>1</b>" in html
    assert "Phases: 1 Recovery &amp; Transition (Wk 1) · 3 Re-entry (Wk 24)." in html


def test_cover_without_race_shows_plan_dates_and_dashes_empty_weeks():
    plan = _long_plan()
    html = b.cover_page(plan)
    assert "Monday, September 14, 2026 – Sunday, September 27, 2026" in html
    assert '<div class="bt">—</div>' in html          # week 9 has no sessions


def test_week_rows_line_up_across_days():
    """One table row per session slot: a day with three sessions sets the
    row count for every day, padded with empty slots."""
    plan = sample_plan()
    tue = plan["weeks"][0]["days"][1]["workouts"]
    tue.append({"id": "x", "sport": "bike", "name": "PM commute", "durationMinutes": 55})
    html = b.week_page(plan, plan["weeks"][0], 2)
    grid = html[html.index('<table class="grid">'):]
    assert grid.count("<tr") == 1 + 3 + 1             # day heads, 3 slots, notes
    assert grid.count('class="card"') == 5
    assert grid.count('<td class="slot"></td>') == 2 + 2   # Mon and Thu pad two rows each


def test_empty_week_is_one_block():
    plan = _long_plan()
    html = b.week_page(plan, plan["weeks"][8], 24)
    assert html.count("No sessions this week") == 1
    assert "Rest day" not in html


def test_tidy_spaces_minutes():
    assert b.tidy("2 x 20min easy, ~99min.") == "2 × 20 min easy, ~99 min."


def test_long_race_card_grows_its_rows():
    """The race card is laid out out of flow, so its rows must be sized for it."""
    plan = sample_plan()
    plan["raceStrategy"] = {"event": {"name": "Test Tri", "date": "2026-09-27"}, "pacing": {}}
    plan["weeks"][1]["days"].append({"date": "2026-09-27", "dayOfWeek": "Sunday", "workouts": [
        {"id": "race", "sport": "race", "name": "A Very Long Race Name " * 6,
         "keyTargets": "Hold back early, then race the second half. " * 6}]})
    html = b.week_page(plan, plan["weeks"][1], 2)
    heights = [int(h) for h in __import__("re").findall(r'<tr style="height:(\d+)px">', html)]
    card = html[html.index('class="card race"'):]
    assert sum(heights) + 6 * (len(heights) - 1) >= b.race_card_height(card)
    assert sum(heights) > b.SLOT_AREA
