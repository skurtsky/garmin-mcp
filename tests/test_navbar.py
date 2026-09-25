# tests/test_navbar.py
"""Tests for the shared site navigation bar (tools/navbar.py, issue 43).

Pure rendering — no storage, no Garmin session, no HTTP.
"""
from tools import navbar


def _markup(nav: str) -> str:
    """The nav element on its own — the style block mentions the same classes."""
    return nav[nav.index("</style>"):]


def test_nav_links_all_hosted_pages():
    nav = navbar.render_nav_html("weekly-summary", "t0k")

    assert 'href="/dashboard?token=t0k"' in nav
    assert 'href="/training-plan?token=t0k"' in nav
    assert 'href="/weekly-summary?token=t0k"' in nav
    assert 'href="/training-plan/plans?token=t0k"' in nav
    assert 'href="/training-plan?view=settings&amp;token=t0k"' in nav


def test_nav_pill_is_today_plan_trends_activity_more():
    markup = _markup(navbar.render_nav_html(None, "t0k"))
    pill = markup[:markup.index('gm-nav__rail')]
    labels = [part.split("<")[0] for part in pill.split('class="gm-nav__label">')[1:]]

    assert labels == ["Today", "Plan", "Trends", "Activity"]
    assert "More</span>" in markup


def test_nav_more_sheet_holds_everything_else():
    nav = navbar.render_nav_html(None, "t0k")
    sheet = nav[nav.index("gm-nav-more-sheet\""):]
    labels = [part.split("<")[0] for part in sheet.split("<span>")[1:]]

    assert labels == ["Fitness", "Gear", "Weekly Summary", "Plans", "Plan PDF", "Settings"]
    assert 'href="/training-plan/pdf?token=t0k" data-nav="plan-pdf" download' in sheet


def test_nav_becomes_a_side_rail_on_desktop():
    nav = navbar.render_nav_html("plan", "t0k", title="Kurt")
    style = nav[:nav.index("</style>")]
    desktop = style[style.index("@media (min-width: 900px)"):]

    assert "width: 220px" in desktop and "padding-left: 220px" in desktop
    assert ".gm-nav__more-btn { display: none; }" in desktop
    assert '<div class="gm-nav__title">Kurt</div>' in nav
    markup = _markup(nav)
    rail = [part.split("<")[0] for part in markup.split('class="gm-nav__label">')[1:]]
    assert rail[:10] == ["Today", "Plan", "Trends", "Activity", "Fitness", "Gear",
                         "Weekly Summary", "Plans", "Settings", "More"]


def test_dashboard_tabs_are_labels_that_follow_their_radio():
    nav = navbar.render_nav_html(None, "t0k", tabs={"today": "tab-today", "fitness": "tab-you"})

    assert '<label class="gm-nav__link" for="tab-today" data-nav="today">' in nav
    assert 'class="gm-nav-more-item" for="tab-you"' in nav
    assert "#tab-today:checked ~ #gm-nav [data-nav=today]" in nav
    # A More-sheet tab lights the phone's More button.
    assert "#tab-you:checked ~ #gm-nav .gm-nav__more-btn" in nav
    assert 'href="/training-plan?token=t0k"' in nav      # Plan stays a link


def test_a_more_page_highlights_the_more_button():
    markup = _markup(navbar.render_nav_html("weekly-summary", "t0k"))
    assert "gm-nav__more-btn gm-nav__more-btn--active" in markup


def test_legacy_page_keys_still_highlight():
    markup = _markup(navbar.render_nav_html("training-plan", "t0k"))
    assert 'gm-nav__link--active" href="/training-plan?token=t0k"' in markup


def test_nav_is_a_floating_bottom_pill():
    nav = navbar.render_nav_html("dashboard", "t0k")

    assert "bottom: 0" in nav
    assert "border-radius: 999px" in nav


def test_nav_links_to_dashboard_tabs():
    nav = navbar.render_nav_html("dashboard", "t0k")
    popup = nav[nav.index('id="gm-nav-more"'):]

    assert 'href="/dashboard?tab=activity&amp;token=t0k"' in popup
    assert 'href="/dashboard?tab=gear&amp;token=t0k"' in popup
    assert 'href="/dashboard?tab=fitness&amp;token=t0k"' in popup


def test_nav_highlights_the_active_page_only():
    markup = _markup(navbar.render_nav_html("training-plan", "t0k"))

    assert markup.count("gm-nav__link--active") == 1
    assert markup.count('aria-current="page"') == 1
    active = markup[markup.index("gm-nav__link--active"):]
    assert active.index("Plan") < active.index("Trends")


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

    The deliberate exceptions are ``body`` padding, which reserves the room
    the fixed pill would otherwise cover, and ``html`` top padding, which keeps
    content out from under the iPhone notch / Dynamic Island on mobile.
    """
    nav = navbar.render_nav_html("dashboard", "t0k")
    style = nav[nav.index("<style>") + len("<style>"): nav.index("</style>")]

    selectors = [
        line.split("{")[0].strip()
        for line in style.splitlines()
        if "{" in line and not line.strip().startswith(("@", "/*"))
    ]
    unscoped = {s for s in selectors if not (s.startswith("#gm-nav") or s.startswith(".gm-nav-more"))}
    assert unscoped == {"body", "html"}


def test_nav_respects_ios_safe_areas():
    """The pill hugs the home indicator (not a full 16px above the whole
    inset), and mobile pages are pushed below the notch."""
    nav = navbar.render_nav_html("dashboard", "t0k")
    assert "max(16px, calc(env(safe-area-inset-bottom, 0px) - 12px))" in nav
    assert "calc(16px + env(safe-area-inset-bottom" not in nav
    assert "@media (max-width: 899px)" in nav
    assert "html { padding-top: env(safe-area-inset-top, 0px); }" in nav


def test_nav_is_removed_from_normal_flow():
    """Must never be an in-flow child of a host <body>.

    An in-flow first child would be treated as an extra item by a host page
    that styles its own <body> as a flex or grid container (e.g. a sidebar
    layout) — squeezing in and corrupting that layout. Fixed positioning keeps
    #gm-nav out of the flow entirely so it can never be miscounted this way.
    """
    nav = navbar.render_nav_html("dashboard", "t0k")
    style = nav[nav.index("<style>") + len("<style>"): nav.index("</style>")]
    base_rule = style[style.index("#gm-nav {"): style.index("#gm-nav .gm-nav__pill")]

    assert "position: fixed" in base_rule


def test_inject_nav_goes_inside_the_body_tag():
    page = '<!doctype html><html><head></head><body class="x"><h1>Hi</h1></body></html>'
    out = navbar.inject_nav(page, "weekly-summary", "t0k")

    assert '<body class="x">' in out                  # the host body tag survives
    assert out.index('id="gm-nav"') > out.index('<body class="x">')
    assert out.index('id="gm-nav"') < out.index("<h1>Hi</h1>")
    assert out.endswith("</body></html>")


def test_inject_nav_replaces_existing_viewport_with_no_zoom():
    page = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body></body></html>'
    out = navbar.inject_nav(page, "training-plan")

    assert 'maximum-scale=1' in out
    assert 'user-scalable=no' in out
    assert out.count('name="viewport"') == 1


def test_inject_no_zoom_meta_applies_the_no_zoom_viewport_without_a_nav_bar():
    page = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body></body></html>'
    out = navbar.inject_no_zoom_meta(page)

    assert 'maximum-scale=1' in out
    assert 'user-scalable=no' in out
    assert 'id="gm-nav"' not in out


def test_inject_nav_prepends_to_a_fragment_without_a_body_tag():
    out = navbar.inject_nav("<h1>Fragment</h1>", "dashboard")

    assert out.index('id="gm-nav"') < out.index("<h1>Fragment</h1>")


def test_inject_nav_is_idempotent():
    page = "<html><body><h1>Hi</h1></body></html>"
    once = navbar.inject_nav(page, "dashboard", "t0k")

    assert navbar.inject_nav(once, "dashboard", "t0k") == once


def test_inject_icon_links_adds_them_to_head():
    page = navbar.inject_icon_links("<html><head><title>x</title></head><body></body></html>")
    assert page.startswith("<html><head>" + navbar.ICON_LINKS)


def test_inject_icon_links_is_idempotent_and_skips_headless_pages():
    page = navbar.inject_icon_links("<html><head></head><body></body></html>")
    assert navbar.inject_icon_links(page) == page
    assert navbar.inject_icon_links("<p>fragment</p>") == "<p>fragment</p>"


def test_icon_links_point_at_files_that_exist():
    import os, re
    root = os.path.join(os.path.dirname(__file__), "..", "static")
    for href in re.findall(r'href="/icons/([^"]+)"', navbar.ICON_LINKS):
        assert os.path.isfile(os.path.join(root, "icons", href)), href
