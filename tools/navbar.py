# tools/navbar.py
"""The one site navigation for every hosted page (issue 43; unified nav).

The dashboard, the training-plan viewer and the weekly reports used to carry
three different bars that only reached each other through "More". There is
now a single nav, rendered here and used by all of them:

    Today · Plan · Trends · Activity · More
    More → Fitness, Gear, Weekly Summary, Plans, Plan PDF, Settings

On a phone it's the floating rounded bottom pill; from 900px wide it becomes
a side rail with everything visible (nothing hidden under More). Today,
Trends, Activity, Fitness and Gear are dashboard tabs, Plan and Settings are
the plan viewer, Plans is the plan list.

It is injected into pages this module doesn't control (the plan viewer, a
Cowork-generated weekly report), so everything is namespaced under
``#gm-nav`` — a single id wrapper plus ``gm-nav__`` classes — and it is
always ``position: fixed`` rather than in the document's normal flow: an
in-flow element inserted as the first child of a host ``<body>`` that itself
uses ``display: flex`` or ``grid`` would be treated as an extra layout item.
``body`` padding reserves the room it covers instead (bottom on a phone,
left beside the rail).

On the dashboard itself the tabs are CSS radio inputs, so there those items
are ``<label for=…>`` rather than links (see ``tabs`` on
:func:`render_nav_html`), and the highlighting follows whichever radio is
checked.
"""
import html
import re
from urllib.parse import urlencode

# (key, label, path, icon) in nav order. PRIMARY is the phone pill; the rest
# sit under More on a phone and in the rail on a desktop.
PRIMARY = (
    ("today", "Today", "/dashboard", "sun"),
    ("plan", "Plan", "/training-plan", "calendar"),
    ("trends", "Trends", "/dashboard?tab=trends", "trends"),
    ("activity", "Activity", "/dashboard?tab=activity", "pulse"),
)
MORE = (
    ("fitness", "Fitness", "/dashboard?tab=fitness", "heartbeat"),
    ("gear", "Gear", "/dashboard?tab=gear", "wrench"),
    ("weekly-summary", "Weekly Summary", "/weekly-summary", "chart"),
    ("plans", "Plans", "/training-plan/plans", "stack"),
    ("plan-pdf", "Plan PDF", "/training-plan/pdf", "download"),
    ("settings", "Settings", "/training-plan?view=settings", "gear"),
)
# The rail: the dashboard pages, then the reports and plans, then Settings
# pinned to the bottom. Plan PDF is a phone-only shortcut (Settings has it too).
_RAIL_TOP = ("fitness", "gear")
_RAIL_MIDDLE = ("weekly-summary", "plans")
_RAIL_BOTTOM = ("settings",)

# Older page keys, still accepted for ``active``.
_ALIASES = {"dashboard": "today", "training-plan": "plan"}

NAV_ID = "gm-nav"
_MORE_TOGGLE_ID = "gm-nav-more"

_BODY_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_HEAD_TAG_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_VIEWPORT_TAG_RE = re.compile(r"<meta\b(?=[^>]*\bname=[\"']viewport[\"'])[^>]*>", re.IGNORECASE)
# Browser-tab favicon and iOS home-screen icon. The tab icon is transparent;
# favicon.svg switches its strokes to white on dark-mode tab bars, and
# browsers without SVG favicons (Safari) fall back to favicon.ico. Served
# without the bearer token (see server.py) — iOS fetches the touch icon
# without it.
ICON_LINKS = (
  '<link rel="icon" href="/favicon.ico" sizes="32x32">'
  '<link rel="icon" type="image/svg+xml" href="/icons/favicon.svg">'
  '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">'
)
_NO_ZOOM_META = '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'

# Phosphor icons (regular, plus the filled variant shown while active).
_ICON_PATHS = {
  'sun': '<path d="M120,40V16a8,8,0,0,1,16,0V40a8,8,0,0,1-16,0Zm72,88a64,64,0,1,1-64-64A64.07,64.07,0,0,1,192,128Zm-16,0a48,48,0,1,0-48,48A48.05,48.05,0,0,0,176,128ZM58.34,69.66A8,8,0,0,0,69.66,58.34l-16-16A8,8,0,0,0,42.34,53.66Zm0,116.68-16,16a8,8,0,0,0,11.32,11.32l16-16a8,8,0,0,0-11.32-11.32ZM192,72a8,8,0,0,0,5.66-2.34l16-16a8,8,0,0,0-11.32-11.32l-16,16A8,8,0,0,0,192,72Zm5.66,114.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32-11.32ZM48,128a8,8,0,0,0-8-8H16a8,8,0,0,0,0,16H40A8,8,0,0,0,48,128Zm80,80a8,8,0,0,0-8,8v24a8,8,0,0,0,16,0V216A8,8,0,0,0,128,208Zm112-88H216a8,8,0,0,0,0,16h24a8,8,0,0,0,0-16Z"/>',
  'sun-fill': '<path d="M120,40V16a8,8,0,0,1,16,0V40a8,8,0,0,1-16,0Zm8,24a64,64,0,1,0,64,64A64.07,64.07,0,0,0,128,64ZM58.34,69.66A8,8,0,0,0,69.66,58.34l-16-16A8,8,0,0,0,42.34,53.66Zm0,116.68-16,16a8,8,0,0,0,11.32,11.32l16-16a8,8,0,0,0-11.32-11.32ZM192,72a8,8,0,0,0,5.66-2.34l16-16a8,8,0,0,0-11.32-11.32l-16,16A8,8,0,0,0,192,72Zm5.66,114.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32-11.32ZM48,128a8,8,0,0,0-8-8H16a8,8,0,0,0,0,16H40A8,8,0,0,0,48,128Zm80,80a8,8,0,0,0-8,8v24a8,8,0,0,0,16,0V216A8,8,0,0,0,128,208Zm112-88H216a8,8,0,0,0,0,16h24a8,8,0,0,0,0-16Z"/>',
  'calendar': '<path d="M208,32H184V24a8,8,0,0,0-16,0v8H88V24a8,8,0,0,0-16,0v8H48A16,16,0,0,0,32,48V208a16,16,0,0,0,16,16H208a16,16,0,0,0,16-16V48A16,16,0,0,0,208,32ZM72,48v8a8,8,0,0,0,16,0V48h80v8a8,8,0,0,0,16,0V48h24V80H48V48ZM208,208H48V96H208V208Z"/>',
  'calendar-fill': '<path d="M208,32H184V24a8,8,0,0,0-16,0v8H88V24a8,8,0,0,0-16,0v8H48A16,16,0,0,0,32,48V208a16,16,0,0,0,16,16H208a16,16,0,0,0,16-16V48A16,16,0,0,0,208,32Zm0,48H48V48H72v8a8,8,0,0,0,16,0V48h80v8a8,8,0,0,0,16,0V48h24Z"/>',
  'trends': '<path d="M232,208a8,8,0,0,1-8,8H32a8,8,0,0,1-8-8V48a8,8,0,0,1,16,0V156.69l50.34-50.35a8,8,0,0,1,11.32,0L128,132.69,180.69,80H160a8,8,0,0,1,0-16h40a8,8,0,0,1,8,8v40a8,8,0,0,1-16,0V91.31l-58.34,58.35a8,8,0,0,1-11.32,0L96,123.31l-56,56V200H224A8,8,0,0,1,232,208Z"/>',
  'trends-fill': '<path d="M216,40H40A16,16,0,0,0,24,56V200a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V56A16,16,0,0,0,216,40ZM200,192H56a8,8,0,0,1-8-8V72a8,8,0,0,1,16,0v76.69l34.34-34.35a8,8,0,0,1,11.32,0L128,132.69,172.69,88H144a8,8,0,0,1,0-16h48a8,8,0,0,1,8,8v48a8,8,0,0,1-16,0V99.31l-50.34,50.35a8,8,0,0,1-11.32,0L104,131.31l-40,40V176H200a8,8,0,0,1,0,16Z"/>',
  'pulse': '<path d="M240,128a8,8,0,0,1-8,8H204.94l-37.78,75.58A8,8,0,0,1,160,216h-.4a8,8,0,0,1-7.08-5.14L95.35,60.76,63.28,131.31A8,8,0,0,1,56,136H24a8,8,0,0,1,0-16H50.85L88.72,36.69a8,8,0,0,1,14.76.46l57.51,151,31.85-63.71A8,8,0,0,1,200,120h32A8,8,0,0,1,240,128Z"/>',
  'pulse-fill': '<path d="M216,40H40A16,16,0,0,0,24,56V200a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V56A16,16,0,0,0,216,40Zm-8,96H188.64L159,188a8,8,0,0,1-6.95,4h-.46a8,8,0,0,1-6.89-4.84L103,89.92,79,132a8,8,0,0,1-7,4H48a8,8,0,0,1,0-16H67.36L97.05,68a8,8,0,0,1,14.3.82L153,166.08l24-42.05a8,8,0,0,1,6.95-4h24a8,8,0,0,1,0,16Z"/>',
  'more': '<path d="M128,96a32,32,0,1,0,32,32A32,32,0,0,0,128,96Zm0,48a16,16,0,1,1,16-16A16,16,0,0,1,128,144ZM48,96a32,32,0,1,0,32,32A32,32,0,0,0,48,96Zm0,48a16,16,0,1,1,16-16A16,16,0,0,1,48,144ZM208,96a32,32,0,1,0,32,32A32,32,0,0,0,208,96Zm0,48a16,16,0,1,1,16-16A16,16,0,0,1,208,144Z"/>',
  'more-fill': '<path d="M156,128a28,28,0,1,1-28-28A28,28,0,0,1,156,128ZM48,100a28,28,0,1,0,28,28A28,28,0,0,0,48,100Zm160,0a28,28,0,1,0,28,28A28,28,0,0,0,208,100Z"/>',
  'heartbeat': '<path d="M72,144H32a8,8,0,0,1,0-16H67.72l13.62-20.44a8,8,0,0,1,13.32,0l25.34,38,9.34-14A8,8,0,0,1,136,128h24a8,8,0,0,1,0,16H140.28l-13.62,20.44a8,8,0,0,1-13.32,0L88,126.42l-9.34,14A8,8,0,0,1,72,144ZM178,40c-20.65,0-38.73,8.88-50,23.89C116.73,48.88,98.65,40,78,40a62.07,62.07,0,0,0-62,62c0,.75,0,1.5,0,2.25a8,8,0,1,0,16-.5c0-.58,0-1.17,0-1.75A46.06,46.06,0,0,1,78,56c19.45,0,35.78,10.36,42.6,27a8,8,0,0,0,14.8,0c6.82-16.67,23.15-27,42.6-27a46.06,46.06,0,0,1,46,46c0,53.61-77.76,102.15-96,112.8-10.83-6.31-42.63-26-66.68-52.21a8,8,0,1,0-11.8,10.82c31.17,34,72.93,56.68,74.69,57.63a8,8,0,0,0,7.58,0C136.21,228.66,240,172,240,102A62.07,62.07,0,0,0,178,40Z"/>',
  'wrench': '<path d="M226.76,69a8,8,0,0,0-12.84-2.88l-40.3,37.19-17.23-3.7-3.7-17.23,37.19-40.3A8,8,0,0,0,187,29.24,72,72,0,0,0,88,96,72.34,72.34,0,0,0,94,124.94L33.79,177c-.15.12-.29.26-.43.39a32,32,0,0,0,45.26,45.26c.13-.13.27-.28.39-.42L131.06,162A72,72,0,0,0,232,96,71.56,71.56,0,0,0,226.76,69ZM160,152a56.14,56.14,0,0,1-27.07-7,8,8,0,0,0-9.92,1.77L67.11,211.51a16,16,0,0,1-22.62-22.62L109.18,133a8,8,0,0,0,1.77-9.93,56,56,0,0,1,58.36-82.31l-31.2,33.81a8,8,0,0,0-1.94,7.1L141.83,108a8,8,0,0,0,6.14,6.14l26.35,5.66a8,8,0,0,0,7.1-1.94l33.81-31.2A56.06,56.06,0,0,1,160,152Z"/>',
  'chart': '<path d="M224,200h-8V40a8,8,0,0,0-8-8H152a8,8,0,0,0-8,8V80H96a8,8,0,0,0-8,8v40H48a8,8,0,0,0-8,8v64H32a8,8,0,0,0,0,16H224a8,8,0,0,0,0-16ZM160,48h40V200H160ZM104,96h40V200H104ZM56,144H88v56H56Z"/>',
  'stack': '<path d="M230.91,172A8,8,0,0,1,228,182.91l-96,56a8,8,0,0,1-8.06,0l-96-56A8,8,0,0,1,36,169.09l92,53.65,92-53.65A8,8,0,0,1,230.91,172ZM220,121.09l-92,53.65L36,121.09A8,8,0,0,0,28,134.91l96,56a8,8,0,0,0,8.06,0l96-56A8,8,0,1,0,220,121.09ZM24,80a8,8,0,0,1,4-6.91l96-56a8,8,0,0,1,8.06,0l96,56a8,8,0,0,1,0,13.82l-96,56a8,8,0,0,1-8.06,0l-96-56A8,8,0,0,1,24,80Zm23.88,0L128,126.74,208.12,80,128,33.26Z"/>',
  'gear': '<path d="M128,80a48,48,0,1,0,48,48A48.05,48.05,0,0,0,128,80Zm0,80a32,32,0,1,1,32-32A32,32,0,0,1,128,160Zm88-29.84q.06-2.16,0-4.32l14.92-18.64a8,8,0,0,0,1.48-7.06,107.21,107.21,0,0,0-10.88-26.25,8,8,0,0,0-6-3.93l-23.72-2.64q-1.48-1.56-3-3L186,40.54a8,8,0,0,0-3.94-6,107.71,107.71,0,0,0-26.25-10.87,8,8,0,0,0-7.06,1.49L130.16,40Q128,40,125.84,40L107.2,25.11a8,8,0,0,0-7.06-1.48A107.6,107.6,0,0,0,73.89,34.51a8,8,0,0,0-3.93,6L67.32,64.27q-1.56,1.49-3,3L40.54,70a8,8,0,0,0-6,3.94,107.71,107.71,0,0,0-10.87,26.25,8,8,0,0,0,1.49,7.06L40,125.84Q40,128,40,130.16L25.11,148.8a8,8,0,0,0-1.48,7.06,107.21,107.21,0,0,0,10.88,26.25,8,8,0,0,0,6,3.93l23.72,2.64q1.49,1.56,3,3L70,215.46a8,8,0,0,0,3.94,6,107.71,107.71,0,0,0,26.25,10.87,8,8,0,0,0,7.06-1.49L125.84,216q2.16.06,4.32,0l18.64,14.92a8,8,0,0,0,7.06,1.48,107.21,107.21,0,0,0,26.25-10.88,8,8,0,0,0,3.93-6l2.64-23.72q1.56-1.48,3-3L215.46,186a8,8,0,0,0,6-3.94,107.71,107.71,0,0,0,10.87-26.25,8,8,0,0,0-1.49-7.06Zm-16.1-6.5a73.93,73.93,0,0,1,0,8.68,8,8,0,0,0,1.74,5.48l14.19,17.73a91.57,91.57,0,0,1-6.23,15L187,173.11a8,8,0,0,0-5.1,2.64,74.11,74.11,0,0,1-6.14,6.14,8,8,0,0,0-2.64,5.1l-2.51,22.58a91.32,91.32,0,0,1-15,6.23l-17.74-14.19a8,8,0,0,0-5-1.75h-.48a73.93,73.93,0,0,1-8.68,0,8,8,0,0,0-5.48,1.74L100.45,215.8a91.57,91.57,0,0,1-15-6.23L82.89,187a8,8,0,0,0-2.64-5.1,74.11,74.11,0,0,1-6.14-6.14,8,8,0,0,0-5.1-2.64L46.43,170.6a91.32,91.32,0,0,1-6.23-15l14.19-17.74a8,8,0,0,0,1.74-5.48,73.93,73.93,0,0,1,0-8.68,8,8,0,0,0-1.74-5.48L40.2,100.45a91.57,91.57,0,0,1,6.23-15L69,82.89a8,8,0,0,0,5.1-2.64,74.11,74.11,0,0,1,6.14-6.14A8,8,0,0,0,82.89,69L85.4,46.43a91.32,91.32,0,0,1,15-6.23l17.74,14.19a8,8,0,0,0,5.48,1.74,73.93,73.93,0,0,1,8.68,0,8,8,0,0,0,5.48-1.74L155.55,40.2a91.57,91.57,0,0,1,15,6.23L173.11,69a8,8,0,0,0,2.64,5.1,74.11,74.11,0,0,1,6.14,6.14,8,8,0,0,0,5.1,2.64l22.58,2.51a91.32,91.32,0,0,1,6.23,15l-14.19,17.74A8,8,0,0,0,199.87,123.66Z"/>',
  'download': '<path d="M224,144v64a8,8,0,0,1-8,8H40a8,8,0,0,1-8-8V144a8,8,0,0,1,16,0v56H208V144a8,8,0,0,1,16,0Zm-101.66,5.66a8,8,0,0,0,11.32,0l40-40a8,8,0,0,0-11.32-11.32L136,124.69V32a8,8,0,0,0-16,0v92.69L93.66,98.34a8,8,0,0,0-11.32,11.32Z"/>',
}

# Every rule is anchored on #gm-nav / .gm-nav-more-* so the host page is
# untouched. Colours are the "Nocturne" dark theme's own tokens, hardcoded
# since this module is injected into pages that don't define them.
_NAV_STYLE = """
#gm-nav, #gm-nav * { box-sizing: border-box; }
#gm-nav {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 2147483647;
  display: flex; justify-content: center;
  /* Sit just clear of the iPhone home indicator rather than a full 16px above
     the whole safe-area inset, which floated the pill noticeably high. */
  padding: 0 16px max(16px, calc(env(safe-area-inset-bottom, 0px) - 12px));
  pointer-events: none;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}
#gm-nav .gm-nav__pill {
  pointer-events: auto; display: flex; gap: 2px; padding: 6px; border-radius: 999px;
  width: min(420px, 100%);
  background: color-mix(in srgb, #232532 94%, transparent);
  backdrop-filter: blur(16px);
  box-shadow: 0 0 0 1px #595d6c, 0 6px 18px rgba(0, 0, 0, .55);
}
#gm-nav .gm-nav__link, #gm-nav .gm-nav__more-btn {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px; min-width: 0; white-space: nowrap; padding: 8px 0; border-radius: 999px;
  color: #9397ab; text-decoration: none; font: inherit; border: 0; background: none; cursor: pointer;
}
#gm-nav .gm-nav__icon { display: grid; }
#gm-nav .gm-nav__icon svg { width: 19px; height: 19px; display: block; grid-area: 1 / 1; }
#gm-nav .gm-nav__icon .gm-nav__on { visibility: hidden; }
#gm-nav .gm-nav__label { font-size: 9px; letter-spacing: .06em; text-transform: uppercase; }
#gm-nav .gm-nav__title, #gm-nav .gm-nav__rail, #gm-nav .gm-nav__divider, #gm-nav .gm-nav__spacer { display: none; }
#gm-nav .gm-nav__link:hover, #gm-nav .gm-nav__more-btn:hover {
  color: #e9e9ed; background: rgba(233, 233, 237, .07);
}
/* Tap feedback — a quick press so a tap visibly registered. The empty
   ontouchstart on the nav is what turns :active on in iOS Safari. */
#gm-nav .gm-nav__link, #gm-nav .gm-nav__more-btn { -webkit-tap-highlight-color: transparent;
  transition: transform .12s ease, opacity .12s ease; }
#gm-nav .gm-nav__link:active, #gm-nav .gm-nav__more-btn:active { transform: scale(.94); opacity: .75; }
#gm-nav .gm-nav__link--active,
#gm-nav .gm-nav__more-btn--active,
#gm-nav-more:checked ~ #gm-nav .gm-nav__more-btn {
  background: rgba(145, 132, 217, .2); color: #e7e5fe;
}
#gm-nav .gm-nav__link--active .gm-nav__on, #gm-nav .gm-nav__more-btn--active .gm-nav__on { visibility: visible; }
#gm-nav .gm-nav__link--active .gm-nav__off, #gm-nav .gm-nav__more-btn--active .gm-nav__off { visibility: hidden; }
/* Reserve the room the fixed bar would otherwise cover — a bottom pill on a
   phone, so it's padding-bottom rather than padding-top. */
body { padding-bottom: calc(68px + max(16px, calc(env(safe-area-inset-bottom, 0px) - 12px))) !important; }
/* Standalone iOS (black-translucent status bar) draws the page under the
   notch / Dynamic Island. Pad the root, not <body>, so a host page's own body
   padding is left alone. Zero on desktop and notch-less screens anyway. */
@media (max-width: 899px) {
  html { padding-top: env(safe-area-inset-top, 0px); }
}

/* ── "More" popup — floats above the pill, same surface / blur / shadow /
   rounding as the pill itself. ── */
.gm-nav-more-toggle { position: absolute; opacity: 0; width: 0; height: 0; pointer-events: none; }
.gm-nav-more-backdrop, .gm-nav-more-sheet { display: none; }
#gm-nav-more:checked ~ .gm-nav-more-backdrop {
  display: block; position: fixed; inset: 0; z-index: 2147483646; background: rgba(10, 11, 16, .6);
}
#gm-nav-more:checked ~ .gm-nav-more-sheet { display: flex; }
.gm-nav-more-sheet {
  position: fixed; left: 16px; right: 16px; bottom: calc(68px + max(16px, calc(env(safe-area-inset-bottom, 0px) - 12px)));
  z-index: 2147483647; flex-direction: column; max-width: 420px; margin: 0 auto; padding: 6px;
  background: color-mix(in srgb, #232532 94%, transparent); backdrop-filter: blur(16px);
  border-radius: 20px; box-shadow: 0 0 0 1px #595d6c, 0 6px 18px rgba(0, 0, 0, .55);
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}
.gm-nav-more-item {
  display: flex; align-items: center; gap: 12px; padding: 12px 10px; border-radius: 12px;
  color: #e9e9ed; text-decoration: none; font: inherit; font-size: 14px; cursor: pointer;
}
.gm-nav-more-item + .gm-nav-more-item { border-top: 1px solid color-mix(in srgb, #e9e9ed 16%, transparent); }
.gm-nav-more-item:hover { background: rgba(145, 132, 217, .12); }
.gm-nav-more-item svg { width: 19px; height: 19px; flex: 0 0 auto; color: #d2cefd; }

/* ── Desktop: the same nav as a side rail, everything visible. ── */
@media (min-width: 900px) {
  body { padding-bottom: 0 !important; padding-left: 220px !important; }
  #gm-nav { top: 0; right: auto; width: 220px; padding: 0; display: block; }
  #gm-nav .gm-nav__pill {
    width: 100%; height: 100%; flex-direction: column; gap: 2px; padding: 20px 12px; overflow-y: auto;
    border-radius: 0; background: #1b1d2b; backdrop-filter: none;
    box-shadow: none; border-right: 1px solid rgba(233, 233, 237, .1);
  }
  #gm-nav .gm-nav__title { display: block; padding: 0 10px 18px; font-size: 15px; font-weight: 500; color: #e9e9ed;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #gm-nav .gm-nav__rail { display: flex; }
  #gm-nav .gm-nav__divider { display: block; height: 1px; background: #3f424d; margin: 12px 10px; flex: 0 0 auto; }
  #gm-nav .gm-nav__spacer { display: block; flex: 1; }
  #gm-nav .gm-nav__link {
    flex: 0 0 auto; flex-direction: row; justify-content: flex-start; gap: 10px;
    padding: 9px 10px; border-radius: 8px; color: #b2b6ca;
  }
  #gm-nav .gm-nav__link:active { transform: none; }
  #gm-nav .gm-nav__icon svg { width: 18px; height: 18px; }
  #gm-nav .gm-nav__label { font-size: 14px; letter-spacing: 0; text-transform: none; }
  #gm-nav .gm-nav__more-btn { display: none; }
  #gm-nav-more:checked ~ .gm-nav-more-backdrop, #gm-nav-more:checked ~ .gm-nav-more-sheet { display: none; }
}
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


def _svg(name: str, cls: str = "") -> str:
    path = _ICON_PATHS.get(name, _ICON_PATHS["chart"])
    klass = f' class="{cls}"' if cls else ""
    return (f'<svg{klass} viewBox="0 0 256 256" width="20" height="20" fill="currentColor" '
            f'aria-hidden="true">{path}</svg>')


def _icon(name: str) -> str:
    """The icon, plus its filled variant (shown while the item is active)."""
    filled = f"{name}-fill" if f"{name}-fill" in _ICON_PATHS else name
    return f'<span class="gm-nav__icon">{_svg(name, "gm-nav__off")}{_svg(filled, "gm-nav__on")}</span>'


_ALL = {key: (label, path, icon) for key, label, path, icon in PRIMARY + MORE}
_MORE_KEYS = {key for key, *_ in MORE}


def _item(key: str, active: str | None, token: str | None, tabs: dict, extra_class: str = "") -> str:
    """One pill / rail entry: a link, or a label for a dashboard tab radio."""
    label, path, icon = _ALL[key]
    is_active = key == active and key not in tabs
    classes = " ".join(c for c in ("gm-nav__link", extra_class, "gm-nav__link--active" if is_active else "") if c)
    current = ' aria-current="page"' if is_active else ""
    inner = f'{_icon(icon)}<span class="gm-nav__label">{_e(label)}</span>'
    if key in tabs:
        return f'<label class="{classes}" for="{_e(tabs[key])}" data-nav="{key}">{inner}</label>'
    return f'<a class="{classes}" href="{_e(_url(path, token))}" data-nav="{key}"{current}>{inner}</a>'


def _more_item(key: str, token: str | None, tabs: dict) -> str:
    label, path, icon = _ALL[key]
    inner = f"{_svg(icon)}<span>{_e(label)}</span>"
    if key in tabs:
        # Picking a tab also closes the sheet.
        return (f'<label class="gm-nav-more-item" for="{_e(tabs[key])}" data-nav="{key}" '
                f'onclick="document.getElementById(\'{_MORE_TOGGLE_ID}\').checked=false">{inner}</label>')
    download = " download" if key == "plan-pdf" else ""
    return f'<a class="gm-nav-more-item" href="{_e(_url(path, token))}" data-nav="{key}"{download}>{inner}</a>'


def _tab_style(tabs: dict) -> str:
    """Highlighting for dashboard tabs, following the checked radio. The
    radios must come before the nav, as its siblings."""
    rules = []
    for key, radio in tabs.items():
        target = f"#{radio}:checked ~ #{NAV_ID} [data-nav={key}]"
        rules.append(f"{target} {{ background: rgba(145, 132, 217, .2); color: #e7e5fe; }}")
        rules.append(f"{target} .gm-nav__on {{ visibility: visible; }} {target} .gm-nav__off {{ visibility: hidden; }}")
        if key in _MORE_KEYS:
            more = f"#{radio}:checked ~ #{NAV_ID} .gm-nav__more-btn"
            rules.append(f"@media (max-width: 899px) {{ {more} {{ background: rgba(145, 132, 217, .2); color: #e7e5fe; }} "
                         f"{more} .gm-nav__on {{ visibility: visible; }} {more} .gm-nav__off {{ visibility: hidden; }} }}")
    return "\n".join(rules)


def render_nav_html(active: str | None = None, token: str | None = None, *,
                    tabs: dict[str, str] | None = None, title: str | None = None) -> str:
    """The site nav: a ``<style>`` block, the ``#gm-nav`` pill / rail and its
    "More" popup.

    ``active`` is an item key (``today``, ``plan``, ``trends``, ``activity``,
    ``fitness``, ``gear``, ``weekly-summary``, ``plans``, ``settings``; the
    older ``dashboard`` / ``training-plan`` still work). It's highlighted and
    marked ``aria-current``; a More item also highlights the phone's More
    button. Anything else — including None — highlights nothing.

    ``tabs`` maps item keys to radio input ids on the current page (the
    dashboard's tabs): those items become ``<label for=…>`` and highlight
    with their radio instead of ``active``. ``title`` heads the desktop rail.
    ``token`` is threaded through every link so navigating never drops the
    ``?token=`` bearer auth.
    """
    tabs = tabs or {}
    active = _ALIASES.get(active, active)

    items = "".join(_item(key, active, token, tabs) for key, *_ in PRIMARY)
    rail = (
        "".join(_item(key, active, token, tabs, "gm-nav__rail") for key in _RAIL_TOP)
        + '<div class="gm-nav__divider"></div>'
        + "".join(_item(key, active, token, tabs, "gm-nav__rail") for key in _RAIL_MIDDLE)
        + '<div class="gm-nav__spacer"></div>'
        + "".join(_item(key, active, token, tabs, "gm-nav__rail") for key in _RAIL_BOTTOM)
    )
    more_active = " gm-nav__more-btn--active" if active in _MORE_KEYS and active not in tabs else ""
    more_items = "".join(_more_item(key, token, tabs) for key, *_ in MORE)

    return (
        f"<style>{_NAV_STYLE}{_tab_style(tabs)}</style>"
        f'<input type="checkbox" id="{_MORE_TOGGLE_ID}" class="gm-nav-more-toggle" aria-label="More">'
        f'<nav id="{NAV_ID}" aria-label="Site" ontouchstart=""><div class="gm-nav__pill">'
        f'<div class="gm-nav__title">{_e(title or "Garmin")}</div>'
        f"{items}{rail}"
        f'<label for="{_MORE_TOGGLE_ID}" class="gm-nav__more-btn{more_active}" data-nav="more">'
        f'{_icon("more")}<span class="gm-nav__label">More</span></label>'
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


def inject_icon_links(page: str) -> str:
    """Add the site favicon / touch-icon links to a page's ``<head>``.

    For pages this module doesn't render itself (the plan viewer template,
    uploaded weekly reports). A page that already carries them, or has no
    ``<head>``, is returned unchanged.
    """
    if 'rel="apple-touch-icon"' in page:
        return page
    head_match = _HEAD_TAG_RE.search(page)
    if head_match:
        return page[:head_match.end()] + ICON_LINKS + page[head_match.end():]
    return page


def inject_nav(page: str, active: str | None = None, token: str | None = None,
               title: str | None = None) -> str:
    """Insert the nav bar as the first child of a page's ``<body>``.

    The page is otherwise untouched. Pages with no ``<body>`` tag (a fragment
    rather than a full document) get the nav prepended, and a page that already
    carries the bar is returned unchanged so a second pass can't double it up.
    """
    if f'id="{NAV_ID}"' in page:
        return page

    page = inject_no_zoom_meta(page)

    nav = render_nav_html(active, token, title=title)
    match = _BODY_TAG_RE.search(page)
    if match:
        return page[: match.end()] + nav + page[match.end():]
    return nav + page
