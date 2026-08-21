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

Layout follows the issue: a minimal horizontal bar on desktop and at the top
of mobile pages by default. Callers can opt into a floating bottom bar for
pages whose host app already uses that navigation pattern.
"""
import html
import re
from urllib.parse import urlencode

# (key, label, path, icon name) for each hosted page, in nav order.
PAGES = (
  ("dashboard", "Dashboard", "/dashboard", "gauge"),
  ("training-plan", "Training Plan", "/training-plan", "calendar"),
  ("weekly-summary", "Weekly Summary", "/weekly-summary", "chart"),
)

BRAND = "Garmin MCP"

NAV_ID = "gm-nav"

_BODY_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_HEAD_TAG_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_VIEWPORT_TAG_RE = re.compile(r"<meta\b(?=[^>]*\bname=[\"']viewport[\"'])[^>]*>", re.IGNORECASE)
_NO_ZOOM_META = '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'

_ICON_PATHS = {
  "gauge": '<path d="M128 40a96 96 0 1 0 96 96 96.1 96.1 0 0 0-96-96Zm0 176a80 80 0 1 1 80-80 80.1 80.1 0 0 1-80 80Zm45.7-125.7a8 8 0 0 1 0 11.4L139.3 136a16 16 0 1 1-11.3-11.3l34.3-34.4a8 8 0 0 1 11.4 0Z"/>',
  "calendar": '<path d="M208 32h-24v-8a8 8 0 0 0-16 0v8H88v-8a8 8 0 0 0-16 0v8H48a16 16 0 0 0-16 16v160a16 16 0 0 0 16 16h160a16 16 0 0 0 16-16V48a16 16 0 0 0-16-16Zm0 176H48V96h160v112Zm0-128H48V48h24v8a8 8 0 0 0 16 0v-8h80v8a8 8 0 0 0 16 0v-8h24Z"/>',
  "chart": '<path d="M40 216a8 8 0 0 1-8-8V48a8 8 0 0 1 16 0v152h168a8 8 0 0 1 0 16Zm40-40a8 8 0 0 1-8-8v-40a8 8 0 0 1 16 0v40a8 8 0 0 1-8 8Zm48 0a8 8 0 0 1-8-8V96a8 8 0 0 1 16 0v72a8 8 0 0 1-8 8Zm48 0a8 8 0 0 1-8-8V64a8 8 0 0 1 16 0v104a8 8 0 0 1-8 8Z"/>',
}

# Every rule is anchored on #gm-nav so the host page is untouched. The bar is
# Styled to match the training-plan app's own tab bar: a neutral dark surface,
# thin border, evenly distributed buttons, and the plan's lavender accent.
_NAV_STYLE = """
#gm-nav, #gm-nav * { box-sizing: border-box; }
#gm-nav {
  display: flex; align-items: stretch; justify-content: center; gap: 0;
  margin: 0; padding: 0; width: 100%; min-height: 52px;
  background: #292b31; border-bottom: 1px solid #3f424d;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  font-size: .8rem; line-height: 1.2;
  /* Out of normal flow on every screen size — see the module docstring for why
     this can't just sit in-flow as the host body's first child. */
  position: fixed; top: 0; left: 0; right: 0; z-index: 2147483647;
}
#gm-nav .gm-nav__brand { display: none; }
#gm-nav .gm-nav__links { display: flex; align-items: stretch; justify-content: center; width: min(520px, 100%); }
#gm-nav .gm-nav__link {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px; min-width: 0; white-space: nowrap; color: #9397ab; text-decoration: none;
  font: inherit; padding: 9px 6px 8px; border: 0; background: none;
}
#gm-nav .gm-nav__link:hover { color: #e9e9ed; background: rgba(233, 233, 237, .07); }
#gm-nav .gm-nav__link--active {
  color: #d2cefd; box-shadow: inset 0 -2px 0 #9184d9;
}
#gm-nav .gm-nav__icon { font-size: 1.2rem; line-height: 1; }
/* Reserve the room the fixed bar would otherwise cover. Only padding-top is
   forced (and only that one side) — padding can't corrupt a host's flex/grid
   layout the way an extra in-flow sibling can, so this stacks safely on top of
   whatever spacing a page already declares rather than replacing it. */
body { padding-top: 52px !important; }
@media (max-width: 640px) {
  #gm-nav {
    padding: env(safe-area-inset-top, 0px) 0 0;
    box-shadow: 0 2px 12px rgba(0, 0, 0, .35);
    transition: transform .2s ease;
  }
  #gm-nav.gm-nav--hidden { transform: translateY(-110%); }
  #gm-nav .gm-nav__links { width: 100%; }
  #gm-nav .gm-nav__link { font-size: .7rem; text-align: center; }
  #gm-nav .gm-nav__link--active {
    box-shadow: inset 0 -2px 0 #9184d9;
  }
  body { padding-top: 4.25rem !important; padding-bottom: 0 !important; }

  #gm-nav.gm-nav--bottom-mobile {
    top: auto; bottom: max(12px, env(safe-area-inset-bottom, 0px));
    left: 50%; right: auto; width: min(420px, calc(100% - 24px));
    min-height: 0; transform: translateX(-50%);
    border: 1px solid #3f424d; border-radius: 999px;
    padding: 0 0 env(safe-area-inset-bottom, 0px);
    box-shadow: 0 4px 18px rgba(0, 0, 0, .4);
    overflow: hidden;
  }
  #gm-nav.gm-nav--bottom-mobile.gm-nav--hidden {
    transform: translateX(-50%) translateY(140%);
  }
  body:has(#gm-nav.gm-nav--bottom-mobile) {
    padding-top: 0 !important; padding-bottom: 5.25rem !important;
  }
}
"""

_NAV_SCRIPT = """
(function () {
  var nav = document.getElementById('gm-nav');
  if (!nav || !window.matchMedia('(max-width: 640px)').matches) return;
  var lastY = window.scrollY;
  window.addEventListener('scroll', function () {
    var y = window.scrollY;
    if (y > lastY + 4 && y > 56) nav.classList.add('gm-nav--hidden');
    else if (y < lastY - 4) nav.classList.remove('gm-nav--hidden');
    lastY = y;
  }, {passive: true});
}());
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _url(path: str, token: str | None) -> str:
    """A route URL carrying the bearer token, when one was supplied.

    Appended with ``&`` rather than ``?`` when ``path`` already carries a
    query string.
    """
    if not token:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{urlencode({'token': token})}"


def _icon(name: str) -> str:
    path = _ICON_PATHS.get(name, _ICON_PATHS["chart"])
    return f'<svg viewBox="0 0 256 256" width="20" height="20" fill="currentColor" aria-hidden="true">{path}</svg>'


def render_nav_html(active: str | None = None, token: str | None = None,
                    mobile_bottom: bool = False) -> str:
    """The site nav bar: a ``<style>`` block plus the ``#gm-nav`` element.

    ``active`` is one of the :data:`PAGES` keys (``dashboard``,
    ``training-plan``, ``weekly-summary``); the matching link is highlighted and
    marked ``aria-current``. Anything else — including None — simply highlights
    nothing. ``token`` is threaded through every link so navigating never drops
    the ``?token=`` bearer auth. ``mobile_bottom`` gives the nav a floating
    bottom-bar layout on narrow screens; desktop placement is unchanged.
    """
    nav_class = "gm-nav"
    if mobile_bottom:
        nav_class += " gm-nav--bottom-mobile"

    links = []
    for key, label, path, icon in PAGES:
        is_active = key == active
        classes = "gm-nav__link" + (" gm-nav__link--active" if is_active else "")
        current = ' aria-current="page"' if is_active else ""
        links.append(
            f'<a class="{classes}" href="{_e(_url(path, token))}"{current}>'
            f'<span class="gm-nav__icon">{_icon(icon)}</span>'
            f'<span class="gm-nav__label">{_e(label)}</span></a>'
        )

    return (
        f"<style>{_NAV_STYLE}</style>"
        f'<nav id="{NAV_ID}" class="{nav_class}" aria-label="Site">'
        f'<a class="gm-nav__brand" href="{_e(_url("/dashboard", token))}">{_e(BRAND)}</a>'
        f'<div class="gm-nav__links">{"".join(links)}</div>'
        f"</nav><script>{_NAV_SCRIPT}</script>"
    )


def inject_nav(page: str, active: str | None = None, token: str | None = None,
               mobile_bottom: bool = False) -> str:
    """Put the nav bar at the top of a page's ``<body>``.

    The page is otherwise untouched. Pages with no ``<body>`` tag (a fragment
    rather than a full document) get the nav prepended, and a page that already
    carries the bar is returned unchanged so a second pass can't double it up.
    ``mobile_bottom`` only changes narrow-screen placement.
    """
    if f'id="{NAV_ID}"' in page:
        return page

    if _VIEWPORT_TAG_RE.search(page):
      page = _VIEWPORT_TAG_RE.sub(_NO_ZOOM_META, page, count=1)
    else:
      head_match = _HEAD_TAG_RE.search(page)
      if head_match:
        page = page[:head_match.end()] + _NO_ZOOM_META + page[head_match.end():]

    nav = render_nav_html(active, token, mobile_bottom)
    match = _BODY_TAG_RE.search(page)
    if match:
        return page[: match.end()] + nav + page[match.end():]
    return nav + page
