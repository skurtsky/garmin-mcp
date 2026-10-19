# tests/test_plan_ftp_token.py
"""The {{bike-ftp:75-77}} live %FTP token: server renderer (tools/plan_doc.py),
amend_training_plan validation, the calendar feed label and the viewer's JS
twin."""
import json
import re
import shutil
import subprocess

import pytest

from tests.plan_fakes import sample_plan
from tools import plan_calendar, plan_doc
from tools.plan_doc import PlanError


def _plan(ftp=248):
    p = sample_plan()
    if ftp is None:
        p["zones"]["bike"]["power"] = {}
        p.pop("overrides", None)
    else:
        p["overrides"] = {**(p.get("overrides") or {}), "ftp": ftp}
    return p


def render(text, ftp=248):
    return plan_doc.resolve_tokens(_plan(ftp), text)


# ── rendering ────────────────────────────────────────────────────────────────

def test_range_renders_percent_and_watts():
    assert render("{{bike-ftp:75-77}}") == "75–77% FTP (186–191 W)"


def test_single_value():
    assert render("{{bike-ftp:75}}") == "75% FTP (186 W)"


def test_watts_only_suffix():
    assert render("{{bike-ftp:75-77:w}}") == "186–191 W"
    assert render("{{bike-ftp:75:w}}") == "186 W"


def test_percent_only_suffix():
    assert render("{{bike-ftp:75-77:pct}}") == "75–77% FTP"
    assert render("{{bike-ftp:75:pct}}") == "75% FTP"


def test_spaces_and_case_are_tolerated_like_the_other_tokens():
    assert render("{{ BIKE-FTP : 75 - 77 : W }}") == "186–191 W"


def test_equal_range_ends_collapse():
    assert render("{{bike-ftp:75-75}}") == "75% FTP (186 W)"


def test_watts_round_to_the_nearest_watt():
    assert render("{{bike-ftp:77}}") == "77% FTP (191 W)"        # 190.96
    assert render("{{bike-ftp:30-200:w}}") == "74–496 W"         # 74.4 and 496


def test_no_ftp_falls_back_to_the_percent_form_for_every_mode():
    for token, expected in (("{{bike-ftp:75-77}}", "75–77% FTP"), ("{{bike-ftp:75}}", "75% FTP"),
                            ("{{bike-ftp:75-77:w}}", "75–77% FTP"), ("{{bike-ftp:75-77:pct}}", "75–77% FTP")):
        assert render(token, ftp=None) == expected


def test_an_ftp_stored_in_an_odd_shape_falls_back_to_percent():
    p = _plan()
    p["overrides"]["ftp"] = "n/a"
    assert plan_doc.resolve_tokens(p, "{{bike-ftp:75}}") == "75% FTP"


@pytest.mark.parametrize("bad", [
    "{{bike-ftp:29}}", "{{bike-ftp:201}}", "{{bike-ftp:0}}", "{{bike-ftp:20-80}}", "{{bike-ftp:80-250}}",
    "{{bike-ftp:80-75}}",                                    # reversed
    "{{bike-ftp:}}", "{{bike-ftp}}", "{{bike-ftp:75-}}", "{{bike-ftp:-77}}", "{{bike-ftp:75.5}}",
    "{{bike-ftp:75–77}}", "{{bike-ftp:75-77:watts}}", "{{bike-ftp:75-77:w:pct}}", "{{bike-ftp:75-77",
])
def test_invalid_tokens_are_left_as_written(bad):
    assert render(bad) == bad


def test_bounds_are_inclusive():
    assert render("{{bike-ftp:30}}") == "30% FTP (74 W)"
    assert render("{{bike-ftp:200}}") == "200% FTP (496 W)"


def test_changing_ftp_changes_the_same_stored_text():
    stored = "Main: 3 x 15 min @ {{bike-ftp:75-77}}"
    plan = _plan(ftp=248)
    plan["weeks"][0]["days"][0]["workouts"][0]["humanReadable"] = stored
    assert render(stored, 248) == "Main: 3 x 15 min @ 75–77% FTP (186–191 W)"
    after, _, _ = plan_doc.apply_operations(plan, [{"op": "set_zones", "ftp": 255}])
    saved = after["weeks"][0]["days"][0]["workouts"][0]["humanReadable"]
    assert saved == stored                                   # nothing cached or rewritten
    assert plan_doc.resolve_tokens(after, saved) == "Main: 3 x 15 min @ 75–77% FTP (191–196 W)"


def test_two_tokens_in_one_line():
    assert render("Warm up {{bike-ftp:55-60:w}}, then {{bike-ftp:88-94}} for 20 min") == \
        "Warm up 136–149 W, then 88–94% FTP (218–233 W) for 20 min"


def test_tokens_inside_a_bullet_list_and_mixed_with_zone_tokens():
    text = "- WU 10 min {{bike-ftp:55}}\n- 3 x 10 min {{bike-ftp:90-95:w}} / {{bike-watts:2}}\n* CD {{bike-ftp:50:pct}}"
    assert render(text) == ("- WU 10 min 55% FTP (136 W)\n- 3 x 10 min 223–236 W / 139-186W\n"
                            "* CD 50% FTP")


# ── amend_training_plan validation ───────────────────────────────────────────

def _update(text):
    return plan_doc.apply_operations(sample_plan(), [
        {"op": "update_workout", "workout_id": "w1-tue-run", "fields": {"humanReadable": text}}])


@pytest.mark.parametrize("good", ["{{bike-ftp:75-77}}", "{{bike-ftp:75}}", "{{bike-ftp:75-77:w}} and {{bike-ftp:80:pct}}"])
def test_amend_accepts_valid_tokens_and_stores_them_unresolved(good):
    plan, _, _ = _update(good)
    stored = next(w for _, _, w in plan_doc.iter_workouts(plan) if w["id"] == "w1-tue-run")["humanReadable"]
    assert stored == good


@pytest.mark.parametrize("bad, why", [
    ("{{bike-ftp:25}}", "30 to 200"), ("{{bike-ftp:75-250}}", "30 to 200"),
    ("{{bike-ftp:80-75}}", "backwards"),
    ("{{bike-ftp:}}", "expected"), ("{{bike-ftp:75–77}}", "expected"), ("{{bike-ftp:75-77:kw}}", "expected"),
    ("{{bike-ftp}}", "expected"), ("{{bike-ftp:75-77", "expected"),
])
def test_amend_rejects_bad_tokens_by_name(bad, why):
    with pytest.raises(PlanError) as exc:
        _update(f"Main {bad} then easy")
    msg = str(exc.value)
    assert "Operation 1" in msg and "humanReadable" in msg and why in msg and bad.strip() in msg


def test_amend_rejects_a_bad_token_on_a_new_workout():
    with pytest.raises(PlanError, match="30 to 200"):
        plan_doc.apply_operations(sample_plan(), [{"op": "add_workout", "date": "2026-09-16", "workout": {
            "sport": "bike", "name": "Ride", "humanReadable": "{{bike-ftp:300}}"}}])


def test_amend_still_lets_other_unknown_tokens_through():
    # Existing behaviour: only the %FTP token is validated.
    plan, _, _ = _update("{{bike-watts:6}} {{foo:ss}}")
    assert plan is not None


# ── calendar feed + viewer ───────────────────────────────────────────────────

def test_calendar_keeps_the_percent_but_never_the_watts():
    text = plan_doc._FTP_TOKEN_RE.sub(plan_calendar._ftp_token_label, "{{bike-ftp:75-77}} / {{bike-ftp:75:w}} / {{bike-ftp:9}}")
    assert text == "75–77% FTP / 75% FTP / {{bike-ftp:9}}"


def test_viewer_renders_the_ftp_token_like_the_server():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = open("tools/assets/plan-viewer.html", encoding="utf-8").read()

    def grab(name, end):
        return re.search(r"const %s = .*?%s" % (name, re.escape(end)), src, re.S).group(0)
    cases = ["{{bike-ftp:75-77}}", "{{bike-ftp:75}}", "{{bike-ftp:75-77:w}}", "{{bike-ftp:75-77:pct}}",
             "{{ BIKE-FTP : 75 - 77 : W }}", "{{bike-ftp:75-75}}", "{{bike-ftp:29}}", "{{bike-ftp:201}}",
             "{{bike-ftp:80-75}}", "{{bike-ftp:75–77}}", "- {{bike-ftp:55}}\n- {{bike-ftp:90-95:w}}"]
    for ftp in (248, 255, None):
        js = "\n".join([
            grab("BIKE_SWEET_SPOT_PCT", ";"), grab("STRIDE_PACE_OFFSET", ";"), grab("RUN_PACE_OFFSET_TABLE", "];"),
            grab("TOKEN_RE", ";"), grab("FTP_TOKEN_RE", ";"), grab("FTP_PCT_BOUNDS", ";"),
            re.search(r"function parseMMSS.*", src).group(0), re.search(r"function fmtMMSS.*", src).group(0),
            re.search(r"function pct\(.*", src).group(0), re.search(r"function rangeStr.*?\n}", src, re.S).group(0),
            re.search(r"function tableRow.*", src).group(0), re.search(r"function ftpSpan.*", src).group(0),
            re.search(r"function resolveTokens\(text\) \{.*?\n}\n", src, re.S).group(0),
            "const BIKE_POWER_PCT_TABLE=[],HR_PCT_TABLE=[],SWIM_PACE_OFFSET_TABLE=[];",
            "const state={overrides:%s};" % json.dumps({"ftp": ftp} if ftp else {}),
            "console.log(JSON.stringify(%s.map(resolveTokens)));" % json.dumps(cases),
        ])
        out = subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout
        assert json.loads(out) == [render(c, ftp) for c in cases]
