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
