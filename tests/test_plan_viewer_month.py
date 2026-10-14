"""The plan viewer is static HTML, so the month view is guarded by checking
the template: its hooks, its CSS and the click-handler order."""

from pathlib import Path

VIEWER = Path(__file__).resolve().parent.parent / "tools" / "assets" / "plan-viewer.html"


def _html():
    return VIEWER.read_text(encoding="utf-8")


def test_month_view_hooks_present():
    html = _html()
    assert 'data-act="view-month"' in html
    assert 'data-act="view-week"' in html
    assert 'data-act="open-week"' in html
    assert ".month-chip" in html
    assert "pv-plan-view" in html


def test_chip_click_opens_workout_before_week():
    """A chip sits inside a day cell that has data-act, so [data-open] must be
    checked first or a chip would open the week instead of the workout."""
    html = _html()
    assert html.index("closest('[data-open]')") < html.index("closest('[data-act]')")


def test_open_week_works_on_archived_plans():
    html = _html()
    assert html.index("a === 'open-week'") < html.index("if (READ_ONLY) return;\n  if (a === 'validate')")


def test_month_toggle_hidden_below_desktop_width():
    assert "@media (max-width: 899px) { .view-seg { display:none; } }" in _html()


def test_viewer_has_appearance_card_and_light_tokens():
    html = _html()
    assert "data-theme-pick" in html
    assert "html[data-theme=light]" in html
    assert html.index("data-theme-pick'") < html.index("if (READ_ONLY) return;\n  if (a === 'validate')")


def test_settings_are_grouped_into_categories_with_a_phone_drill_down():
    html = _html()
    for cat in ("'general'", "'training'", "'plan'", "'calendar'", "'about'"):
        assert cat in html.split("const SETTINGS_CATS", 1)[1].split("];", 1)[0]
    assert 'data-act="settings-cat"' in html and 'data-act="settings-back"' in html
    assert ".set-root .set-pane" in html          # phone: the list, then a page with a back arrow
    assert html.index("a === 'settings-cat'") < html.index("if (READ_ONLY) return;\n  if (a === 'validate')")


def test_paging_carries_on_into_the_neighboring_plans():
    """Past either end of a plan the week view opens the plan before / after
    it (server-supplied neighbors), and the month view shows their days."""
    html = _html()
    assert "const NEIGHBORS = SERVER.neighbors || {};" in html
    step = html.split("function stepWeek(delta) {", 1)[1].split("\n}", 1)[0]
    assert "NEIGHBORS.next" in step and "NEIGHBORS.prev" in step
    assert 'data-act="open-plan"' in html and "a === 'open-plan'" in html
    assert ".month-day.ghost" in html
    assert "get('week')" in html and "get('month')" in html


def test_future_plans_get_their_own_banner_and_stay_editable():
    html = _html()
    assert "const FUTURE = SERVER.status === 'future';" in html
    assert "const READ_ONLY = !!SERVER.readOnly;" in html   # the server says archived only
    assert "/activate" in html.split("function readOnlyBanner()", 1)[1].split("\n}", 1)[0]


def test_viewer_offers_the_threshold_carry_over_and_links_the_season():
    html = _html()
    assert "carryoverBanner() + '<div class=\"plan-head\">'" in html
    assert "a === 'carry-apply' || a === 'carry-dismiss'" in html
    assert "carryover: data.carryover || null" in html     # applyServer keeps it current
    assert "appLink('/training-plan/season')" in html


def test_a_week_link_opens_the_week_view_and_mode_picks_the_view():
    html = _html()
    assert "planView: initialView()," in html
    body = html.split("function initialView() {", 1)[1].split("\n}", 1)[0]
    assert "q.get('week') ? 'week' : storedPlanView()" in body
    assert "mode === 'week' || mode === 'month'" in body
