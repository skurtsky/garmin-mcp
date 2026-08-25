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
layout child; a ``body { padding-bottom }`` rule reserves the room it would
otherwise have taken instead.

A floating rounded bottom pill, matching the dashboard's own tab bar — Dashboard,
Training Plan and Weekly Summary always shown, with a "More" button opening a
popup for Activity and Gear (both dashboard tabs, so they're plain links into
``/dashboard?tab=...`` rather than page links of their own).
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

# (key, label, path, icon name) for the "More" popup — dashboard tabs that
# don't have a page of their own.
_MORE_PAGES = (
  ("activity", "Activity", "/dashboard?tab=activity", "pulse"),
  ("gear", "Gear", "/dashboard?tab=gear", "wrench"),
)

NAV_ID = "gm-nav"
_MORE_TOGGLE_ID = "gm-nav-more"

_BODY_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_HEAD_TAG_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_VIEWPORT_TAG_RE = re.compile(r"<meta\b(?=[^>]*\bname=[\"']viewport[\"'])[^>]*>", re.IGNORECASE)
_NO_ZOOM_META = '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'

_ICON_PATHS = {
  "gauge": '<path d="M128 40a96 96 0 1 0 96 96 96.1 96.1 0 0 0-96-96Zm0 176a80 80 0 1 1 80-80 80.1 80.1 0 0 1-80 80Zm45.7-125.7a8 8 0 0 1 0 11.4L139.3 136a16 16 0 1 1-11.3-11.3l34.3-34.4a8 8 0 0 1 11.4 0Z"/>',
  "calendar": '<path d="M208 32h-24v-8a8 8 0 0 0-16 0v8H88v-8a8 8 0 0 0-16 0v8H48a16 16 0 0 0-16 16v160a16 16 0 0 0 16 16h160a16 16 0 0 0 16-16V48a16 16 0 0 0-16-16Zm0 176H48V96h160v112Zm0-128H48V48h24v8a8 8 0 0 0 16 0v-8h80v8a8 8 0 0 0 16 0v-8h24Z"/>',
  "chart": '<path d="M40 216a8 8 0 0 1-8-8V48a8 8 0 0 1 16 0v152h168a8 8 0 0 1 0 16Zm40-40a8 8 0 0 1-8-8v-40a8 8 0 0 1 16 0v40a8 8 0 0 1-8 8Zm48 0a8 8 0 0 1-8-8V96a8 8 0 0 1 16 0v72a8 8 0 0 1-8 8Zm48 0a8 8 0 0 1-8-8V64a8 8 0 0 1 16 0v104a8 8 0 0 1-8 8Z"/>',
  "more": '<path d="M40,72H216a8,8,0,0,0,0-16H40a8,8,0,0,0,0,16Zm176,32H40a8,8,0,0,0,0,16H216a8,8,0,0,0,0-16Zm0,48H40a8,8,0,0,0,0,16H216a8,8,0,0,0,0-16Z"/>',
  "pulse": '<path d="M240,128a8,8,0,0,1-8,8H207.31l-24.24,66.65a8,8,0,0,1-15,0L143,116l-18.06,49.62a8,8,0,0,1-15,0L92.69,136H24a8,8,0,0,1,0-16H98a8,8,0,0,1,7.52,5.32L120,157.62l18.06-49.62a8,8,0,0,1,15,0L177,192l17.94-49.32A8,8,0,0,1,202.5,120H232A8,8,0,0,1,240,128Z"/>',
  "wrench": '<path d="M226.76,69.28a8,8,0,0,0-12.84-2.88L182,98.34,157.66,74l31.94-31.92a8,8,0,0,0-2.88-12.84,64.09,64.09,0,0,0-79.9,79.9L36.69,179.31a24,24,0,0,0,33.94,33.94l70.16-70.15A64.09,64.09,0,0,0,226.76,69.28ZM138.63,132.36a8,8,0,0,0-2.32,2.34L59.66,201.35a8,8,0,0,1-11.31-11.31l66.65-76.65a8,8,0,0,0,2.34-2.32,8,8,0,0,0-1.53-9.25,48.09,48.09,0,0,1,49.32-79.72L136.4,50.34a8,8,0,0,0,0,11.31L172,97.25a8,8,0,0,0,11.31,0l29.25-29.28a48.09,48.09,0,0,1-72.9,55.32A8,8,0,0,0,138.63,132.36Z"/>',
}

# Every rule is anchored on #gm-nav / .gm-nav-more-* so the host page is
# untouched. Colours are the "Nocturne" dark theme's own tokens, hardcoded
# since this module is injected into pages that don't define them.
_NAV_STYLE = """
#gm-nav, #gm-nav * { box-sizing: border-box; }
#gm-nav {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 2147483647;
  display: flex; justify-content: center;
  padding: 0 16px calc(16px + env(safe-area-inset-bottom, 0px));
  pointer-events: none;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}
#gm-nav .gm-nav__pill {
  pointer-events: auto; display: flex; gap: 2px; padding: 6px; border-radius: 999px;
  width: min(420px, 100%);
  background: color-mix(in srgb, #232532 92%, transparent);
  backdrop-filter: blur(16px);
  box-shadow: 0 0 0 1px #595d6c, 0 6px 18px rgba(0, 0, 0, .55);
}
#gm-nav .gm-nav__link, #gm-nav .gm-nav__more-btn {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px; min-width: 0; white-space: nowrap; padding: 8px 0; border-radius: 999px;
  color: #9397ab; text-decoration: none; font: inherit; border: 0; background: none; cursor: pointer;
}
#gm-nav .gm-nav__icon svg { width: 19px; height: 19px; display: block; }
#gm-nav .gm-nav__label { font-size: 9px; letter-spacing: .06em; text-transform: uppercase; }
#gm-nav .gm-nav__link:hover, #gm-nav .gm-nav__more-btn:hover {
  color: #e9e9ed; background: rgba(233, 233, 237, .07);
}
#gm-nav .gm-nav__link--active,
#gm-nav-more:checked ~ #gm-nav .gm-nav__more-btn {
  background: color-mix(in srgb, #9184d9 20%, transparent); color: #d2cefd;
}
/* Reserve the room the fixed bar would otherwise cover — a bottom pill now,
   not a top bar, so it's padding-bottom rather than padding-top. */
body { padding-bottom: calc(84px + env(safe-area-inset-bottom, 0px)) !important; }

/* ── "More" popup (Activity / Gear) — floats above the pill, same surface /
   blur / shadow / rounding as the pill itself. ── */
.gm-nav-more-backdrop, .gm-nav-more-sheet { display: none; }
#gm-nav-more:checked ~ .gm-nav-more-backdrop {
  display: block; position: fixed; inset: 0; z-index: 2147483646; background: rgba(10, 11, 16, .6);
}
#gm-nav-more:checked ~ .gm-nav-more-sheet { display: flex; }
.gm-nav-more-sheet {
  position: fixed; left: 16px; right: 16px; bottom: calc(84px + env(safe-area-inset-bottom, 0px));
  z-index: 2147483647; flex-direction: column; max-width: 420px; margin: 0 auto; padding: 6px;
  background: color-mix(in srgb, #232532 92%, transparent); backdrop-filter: blur(16px);
  border-radius: 20px; box-shadow: 0 0 0 1px #595d6c, 0 6px 18px rgba(0, 0, 0, .55);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}
.gm-nav-more-item {
  display: flex; align-items: center; gap: 12px; padding: 12px 10px; border-radius: 12px;
  color: #e9e9ed; text-decoration: none; font: inherit; font-size: 14px;
}
.gm-nav-more-item + .gm-nav-more-item { border-top: 1px solid color-mix(in srgb, #e9e9ed 16%, transparent); }
.gm-nav-more-item:hover { background: rgba(145, 132, 217, .12); }
.gm-nav-more-item svg { width: 19px; height: 19px; flex: 0 0 auto; color: #d2cefd; }
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


def render_nav_html(active: str | None = None, token: str | None = None) -> str:
    """The site nav bar: a ``<style>`` block plus the ``#gm-nav`` pill and its
    "More" popup.

    ``active`` is one of the :data:`PAGES` keys (``dashboard``,
    ``training-plan``, ``weekly-summary``); the matching link is highlighted and
    marked ``aria-current``. Anything else — including None — simply highlights
    nothing. ``token`` is threaded through every link (including the More
    popup's Activity/Gear ones) so navigating never drops the ``?token=``
    bearer auth.
    """
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

    more_items = "".join(
        f'<a class="gm-nav-more-item" href="{_e(_url(path, token))}">{_icon(icon)}<span>{_e(label)}</span></a>'
        for _, label, path, icon in _MORE_PAGES
    )

    return (
        f"<style>{_NAV_STYLE}</style>"
        f'<input type="checkbox" id="{_MORE_TOGGLE_ID}" class="gm-nav-more-toggle">'
        f'<nav id="{NAV_ID}" aria-label="Site"><div class="gm-nav__pill">'
        f'{"".join(links)}'
        f'<label for="{_MORE_TOGGLE_ID}" class="gm-nav__more-btn">'
        f'<span class="gm-nav__icon">{_icon("more")}</span><span class="gm-nav__label">More</span></label>'
        "</div></nav>"
        f'<label for="{_MORE_TOGGLE_ID}" class="gm-nav-more-backdrop" aria-hidden="true"></label>'
        f'<div class="gm-nav-more-sheet" role="dialog" aria-modal="true" aria-label="More">{more_items}</div>'
    )


def inject_no_zoom_meta(page: str) -> str:
    """Replace (or add) a page's viewport meta tag with the no-zoom one.

    Split out of :func:`inject_nav` so a page can opt into the no-pinch-zoom
    behavior without also getting the nav bar.
    """
    if _VIEWPORT_TAG_RE.search(page):
        return _VIEWPORT_TAG_RE.sub(_NO_ZOOM_META, page, count=1)
    head_match = _HEAD_TAG_RE.search(page)
    if head_match:
        return page[:head_match.end()] + _NO_ZOOM_META + page[head_match.end():]
    return page


def inject_nav(page: str, active: str | None = None, token: str | None = None) -> str:
    """Insert the nav bar as the first child of a page's ``<body>``.

    The page is otherwise untouched. Pages with no ``<body>`` tag (a fragment
    rather than a full document) get the nav prepended, and a page that already
    carries the bar is returned unchanged so a second pass can't double it up.
    """
    if f'id="{NAV_ID}"' in page:
        return page

    page = inject_no_zoom_meta(page)

    nav = render_nav_html(active, token)
    match = _BODY_TAG_RE.search(page)
    if match:
        return page[: match.end()] + nav + page[match.end():]
    return nav + page

