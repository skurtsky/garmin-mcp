# tools/navbar.py
"""The shared site navigation bar for the hosted pages (issue 43).

Three pages are served out of this container — the dashboard, the training-plan
viewer and the weekly training reports — and each one used to be a dead end:
the only way from one to another was editing the URL by hand.

This module renders one small self-contained nav bar and injects it into every
page at serve time, the same way the weekly-summary week switcher is injected.
Nothing is baked into the uploaded HTML files, so re-uploading a plan or a
report picks the current nav up automatically.

Because it is injected into pages this module doesn't control (a Svelte plan
app, a Cowork-generated report, the dashboard), everything is namespaced under
``#gm-nav`` — a single id wrapper plus ``gm-nav__`` classes — so it cannot
collide with the host page's styles. The bar is also always ``position: fixed``
rather than sitting in the document's normal flow: an in-flow element inserted
as the first child of a host ``<body>`` that itself uses ``display: flex`` or
``grid`` (e.g. a sidebar layout) would be treated as an extra flex/grid item,
squeezing in as an unintended column and breaking that layout — exactly what
happened when the bar clipped the training-plan page's own content. Being
fixed removes it from the flow entirely, so it can never be miscounted as a
layout child; a ``body { padding-top / padding-bottom }`` rule reserves the
room it would otherwise have taken instead.

Layout follows the issue: a minimal horizontal bar on desktop, a bottom tab bar
on mobile (three items, thumb-reachable, matching the app-like dashboard).
"""
import html
import re
from urllib.parse import urlencode

# (key, label, path, icon) for each hosted page, in nav order.
PAGES = (
    ("dashboard", "Dashboard", "/dashboard", "\U0001F4CA"),
    ("training-plan", "Training Plan", "/training-plan", "\U0001F5D3"),
    ("weekly-summary", "Weekly Summary", "/weekly-summary", "\U0001F4C8"),
    ("gear", "Gear", "/dashboard/gear", "\U0001F527"),
)

BRAND = "Garmin MCP"

NAV_ID = "gm-nav"

_BODY_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)

# Every rule is anchored on #gm-nav so the host page is untouched. The bar is
# dark in both colour schemes — it reads as chrome around the page rather than
# part of it, and it avoids inheriting whatever the host page does with themes.
_NAV_STYLE = """
#gm-nav, #gm-nav * { box-sizing: border-box; }
#gm-nav {
  display: flex; align-items: center; gap: .25rem;
  margin: 0; padding: .2rem .75rem; width: 100%; min-height: 44px;
  background: #171a21; border-bottom: 1px solid #232833;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  font-size: .9rem; line-height: 1.2;
  /* Out of normal flow on every screen size — see the module docstring for why
     this can't just sit in-flow as the host body's first child. */
  position: fixed; top: 0; left: 0; right: 0; z-index: 2147483647;
}
#gm-nav .gm-nav__brand {
  color: #e6e8eb; font-weight: 600; text-decoration: none; white-space: nowrap;
  padding: .35rem .75rem .35rem 0; margin-right: .35rem;
}
#gm-nav .gm-nav__brand:hover { color: #5aa9e6; }
#gm-nav .gm-nav__links { display: flex; align-items: center; gap: .25rem; }
#gm-nav .gm-nav__link {
  display: flex; align-items: center; gap: .4rem; white-space: nowrap;
  color: #8b93a1; text-decoration: none; font: inherit;
  padding: .35rem .7rem; border: 1px solid transparent; border-radius: 7px;
}
#gm-nav .gm-nav__link:hover { color: #e6e8eb; background: #0f1115; }
#gm-nav .gm-nav__link--active {
  color: #5aa9e6; background: #0f1115; border-color: #232833;
}
#gm-nav .gm-nav__icon { font-size: 1rem; line-height: 1; }
/* Reserve the room the fixed bar would otherwise cover. Only padding-top is
   forced (and only that one side) — padding can't corrupt a host's flex/grid
   layout the way an extra in-flow sibling can, so this stacks safely on top of
   whatever spacing a page already declares rather than replacing it. */
body { padding-top: 52px !important; }
@media (max-width: 640px) {
  #gm-nav {
    left: 0; right: 0; bottom: 0; top: auto;
    border-bottom: none; border-top: 1px solid #232833;
    padding: .25rem .25rem calc(.25rem + env(safe-area-inset-bottom, 0px));
    box-shadow: 0 -2px 12px rgba(0, 0, 0, .35);
  }
  #gm-nav .gm-nav__brand { display: none; }
  #gm-nav .gm-nav__links { flex: 1; gap: 0; }
  #gm-nav .gm-nav__link {
    flex: 1; flex-direction: column; justify-content: center; gap: .15rem;
    padding: .35rem .25rem; font-size: .7rem; text-align: center;
  }
  #gm-nav .gm-nav__icon { font-size: 1.2rem; }
  /* A pill would fill the whole bar height here — mark the tab with a rule
     along the top edge instead. */
  #gm-nav .gm-nav__link--active {
    background: none; border-color: transparent; border-radius: 0;
    box-shadow: inset 0 2px 0 #5aa9e6;
  }
  /* The bar moves to the bottom on mobile — swap which side of the page it
     reserves space on. */
  body { padding-top: 0 !important; padding-bottom: 4.25rem !important; }
}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _url(path: str, token: str | None) -> str:
    """A route URL carrying the bearer token, when one was supplied."""
    return f"{path}?{urlencode({'token': token})}" if token else path


def render_nav_html(active: str | None = None, token: str | None = None) -> str:
    """The site nav bar: a ``<style>`` block plus the ``#gm-nav`` element.

    ``active`` is one of the :data:`PAGES` keys (``dashboard``,
    ``training-plan``, ``weekly-summary``); the matching link is highlighted and
    marked ``aria-current``. Anything else — including None — simply highlights
    nothing. ``token`` is threaded through every link so navigating never drops
    the ``?token=`` bearer auth.
    """
    links = []
    for key, label, path, icon in PAGES:
        is_active = key == active
        classes = "gm-nav__link" + (" gm-nav__link--active" if is_active else "")
        current = ' aria-current="page"' if is_active else ""
        links.append(
            f'<a class="{classes}" href="{_e(_url(path, token))}"{current}>'
            f'<span class="gm-nav__icon" aria-hidden="true">{icon}</span>'
            f'<span class="gm-nav__label">{_e(label)}</span></a>'
        )

    return (
        f"<style>{_NAV_STYLE}</style>"
        f'<nav id="{NAV_ID}" aria-label="Site">'
        f'<a class="gm-nav__brand" href="{_e(_url("/dashboard", token))}">{_e(BRAND)}</a>'
        f'<div class="gm-nav__links">{"".join(links)}</div>'
        "</nav>"
    )


def inject_nav(page: str, active: str | None = None, token: str | None = None) -> str:
    """Put the nav bar at the top of a page's ``<body>``.

    The page is otherwise untouched. Pages with no ``<body>`` tag (a fragment
    rather than a full document) get the nav prepended, and a page that already
    carries the bar is returned unchanged so a second pass can't double it up.
    """
    if f'id="{NAV_ID}"' in page:
        return page

    nav = render_nav_html(active, token)
    match = _BODY_TAG_RE.search(page)
    if match:
        return page[: match.end()] + nav + page[match.end():]
    return nav + page
