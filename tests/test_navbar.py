# tests/test_navbar.py
"""Tests for the shared site navigation bar (tools/navbar.py, issue 43).

Pure rendering — no storage, no Garmin session, no HTTP.
"""
from tools import navbar


def _markup(nav: str) -> str:
    """The nav element on its own — the style block mentions the same classes."""
    return nav[nav.index("</style>"):]


def test_nav_links_all_three_pages():
    nav = navbar.render_nav_html("dashboard", "t0k")

    assert 'href="/dashboard?token=t0k"' in nav
    assert 'href="/training-plan?token=t0k"' in nav
    assert 'href="/weekly-summary?token=t0k"' in nav
    assert "Dashboard" in nav and "Training Plan" in nav and "Weekly Summary" in nav
    # Brand links home.
    assert "Garmin MCP" in nav


def test_nav_highlights_the_active_page_only():
    markup = _markup(navbar.render_nav_html("training-plan", "t0k"))

    assert markup.count("gm-nav__link--active") == 1
    assert markup.count('aria-current="page"') == 1
    active = markup[markup.index("gm-nav__link--active"):]
    assert active.index("Training Plan") < active.index("Weekly Summary")


def test_nav_without_an_active_page_highlights_nothing():
    markup = _markup(navbar.render_nav_html(None, "t0k"))

    assert "gm-nav__link--active" not in markup
    assert "aria-current" not in markup


def test_nav_omits_the_token_when_there_is_none():
    nav = navbar.render_nav_html("dashboard")

    assert "token=" not in nav
    assert 'href="/dashboard"' in nav


def test_nav_token_is_url_encoded():
    nav = navbar.render_nav_html("dashboard", "a b&c")

    assert "token=a+b%26c" in nav
    assert "a b&c" not in nav


def test_nav_styles_are_scoped_to_the_nav_id():
    """Injected into pages this repo doesn't own — it must not restyle them.

    The one deliberate exception is the mobile ``body { padding-bottom }`` rule
    that keeps the fixed bottom tab bar from covering the end of the page.
    """
    nav = navbar.render_nav_html("dashboard", "t0k")
    style = nav[nav.index("<style>") + len("<style>"): nav.index("</style>")]

    selectors = [
        line.split("{")[0].strip()
        for line in style.splitlines()
        if "{" in line and not line.strip().startswith(("@", "/*"))
    ]
    unscoped = [s for s in selectors if not s.startswith("#gm-nav")]
    assert unscoped == ["body"]


def test_inject_nav_goes_inside_the_body_tag():
    page = '<!doctype html><html><head></head><body class="x"><h1>Hi</h1></body></html>'
    out = navbar.inject_nav(page, "weekly-summary", "t0k")

    assert '<body class="x">' in out                  # the host body tag survives
    assert out.index('id="gm-nav"') > out.index('<body class="x">')
    assert out.index('id="gm-nav"') < out.index("<h1>Hi</h1>")
    assert out.endswith("</body></html>")


def test_inject_nav_prepends_to_a_fragment_without_a_body_tag():
    out = navbar.inject_nav("<h1>Fragment</h1>", "dashboard")

    assert out.index('id="gm-nav"') < out.index("<h1>Fragment</h1>")


def test_inject_nav_is_idempotent():
    page = "<html><body><h1>Hi</h1></body></html>"
    once = navbar.inject_nav(page, "dashboard", "t0k")

    assert navbar.inject_nav(once, "dashboard", "t0k") == once
