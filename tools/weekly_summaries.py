# tools/weekly_summaries.py
"""Storage, navigation and hosting for weekly training reports (issue 41).

A weekly report is a single self-contained HTML page generated elsewhere (Claude
Cowork) and pushed in through the ``upload_weekly_summary`` MCP tool. One file
per training week, named ``{YYYYMMDD}.html`` after the Monday the week starts
on, kept in a ``weekly-summaries/`` folder inside the Azure File Share already
mounted for the Garmin OAuth tokens (``~/.garminconnect``).
``WEEKLY_SUMMARY_DIR`` overrides the location (used by tests).

Re-uploading the same week overwrites it, so a report can be regenerated. Old
weeks are never pruned — a year of reports is a few MB.

The uploaded HTML is stored verbatim; the previous/next/dropdown navigation is
injected server-side at request time, so the generator doesn't have to know
which other weeks exist.

Routes (all behind the same ``?token=`` bearer auth as ``/mcp`` and
``/dashboard``, enforced by the wrapper in server.py):

    GET /weekly-summary             — the most recent report, or a placeholder
    GET /weekly-summary/list        — JSON array of the available weeks
    GET /weekly-summary/{YYYYMMDD}  — one week's report
"""
import html
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from tools import navbar

logger = logging.getLogger(__name__)

PATH_PREFIX = "/weekly-summary"

# A report is a styled HTML page, typically well under 1 MB. The cap only exists
# so a runaway upload can't fill the file share.
MAX_UPLOAD_BYTES = int(os.environ.get("WEEKLY_SUMMARY_MAX_BYTES", 20 * 1024 * 1024))

# The token file share is mounted at ~/.garminconnect (see garmin_client.py);
# the path is repeated here rather than imported so this module stays usable
# without a configured Garmin session.
DEFAULT_SUMMARY_DIR = os.path.join(
    os.path.expanduser("~/.garminconnect"), "weekly-summaries"
)

_WEEK_ID_RE = re.compile(r"^\d{8}$")
_BODY_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)


# ── WEEK IDENTIFIERS ──────────────────────────────────────────────────────────

def parse_week_start(week_start_date: str) -> date:
    """Validate a ``YYYY-MM-DD`` week start and return it as a date.

    Raises ValueError when the string isn't a date in that format or isn't a
    Monday — reports are keyed by the Monday of the training week, so an
    off-by-a-day upload would silently create a second file for the same week.
    """
    if not isinstance(week_start_date, str):
        raise ValueError("week_start_date must be a string in YYYY-MM-DD format.")

    try:
        parsed = datetime.strptime(week_start_date.strip(), "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(
            f"week_start_date must be YYYY-MM-DD, got {week_start_date!r}."
        ) from e

    if parsed.weekday() != 0:
        monday = parsed - timedelta(days=parsed.weekday())
        raise ValueError(
            f"week_start_date must be a Monday — {parsed.isoformat()} is a "
            f"{parsed.strftime('%A')}. The Monday of that week is "
            f"{monday.isoformat()}."
        )

    return parsed


def week_id(week_start: date) -> str:
    """The ``YYYYMMDD`` file/route identifier for a week."""
    return week_start.strftime("%Y%m%d")


def parse_week_id(value: str) -> date | None:
    """The date behind a ``YYYYMMDD`` identifier, or None when it isn't one.

    Rejects anything that isn't exactly eight digits naming a real Monday,
    which also keeps path separators and ``..`` out of file names.
    """
    if not isinstance(value, str) or not _WEEK_ID_RE.match(value):
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None
    return parsed if parsed.weekday() == 0 else None


def week_label(week_start: date) -> str:
    """Human-readable week name, e.g. ``Week of Aug 3, 2026``."""
    return f"Week of {week_start.strftime('%b')} {week_start.day}, {week_start.year}"


# ── STORAGE ───────────────────────────────────────────────────────────────────

def summary_dir() -> str:
    """Directory holding the reports. Read per call so tests can override."""
    return os.environ.get("WEEKLY_SUMMARY_DIR") or DEFAULT_SUMMARY_DIR


def summary_path(identifier: str) -> str:
    return os.path.join(summary_dir(), f"{identifier}.html")


def save_summary(week_start_date: str, html_content: str) -> dict:
    """Store one week's report, replacing any existing report for that week.

    Written via a temp file + rename so a partial write can never be served.
    Raises ValueError when the week isn't a Monday or the HTML doesn't look
    like a report.
    """
    week_start = parse_week_start(week_start_date)

    if not isinstance(html_content, str):
        raise ValueError("html_content must be a string of HTML.")
    if not html_content.strip():
        raise ValueError("html_content is empty.")

    payload = html_content.encode("utf-8")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"The report exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
        )
    if "<" not in html_content:
        raise ValueError("html_content doesn't look like HTML.")

    identifier = week_id(week_start)
    directory = summary_dir()
    os.makedirs(directory, exist_ok=True)

    path = summary_path(identifier)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(payload)
    os.replace(tmp, path)

    logger.info(
        "Stored weekly summary for %s (%d bytes) in %s",
        week_start.isoformat(), len(payload), directory,
    )
    return {
        "url": f"{PATH_PREFIX}/{identifier}",
        "week": week_start.isoformat(),
    }


def read_summary(identifier: str) -> str | None:
    """One week's stored HTML, or None when that week has no report."""
    if parse_week_id(identifier) is None:
        return None
    try:
        with open(summary_path(identifier), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def list_weeks() -> list[dict]:
    """Every stored week, newest first.

    Each entry carries the week's ``YYYYMMDD`` id, its ``YYYY-MM-DD`` Monday, a
    display label, its route, size and last-modified time. Files that aren't
    named after a Monday are ignored rather than breaking the listing.
    """
    try:
        names = os.listdir(summary_dir())
    except FileNotFoundError:
        return []

    weeks = []
    for name in names:
        if not name.endswith(".html"):
            continue
        identifier = name[: -len(".html")]
        week_start = parse_week_id(identifier)
        if week_start is None:
            continue
        try:
            stat = os.stat(os.path.join(summary_dir(), name))
        except FileNotFoundError:  # pragma: no cover — raced with a delete
            continue
        weeks.append({
            "id": identifier,
            "week": week_start.isoformat(),
            "label": week_label(week_start),
            "url": f"{PATH_PREFIX}/{identifier}",
            "bytes": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                                  .strftime("%Y-%m-%d %H:%M UTC"),
        })

    weeks.sort(key=lambda w: w["id"], reverse=True)
    return weeks


def latest_week() -> dict | None:
    """The most recent stored week, or None when nothing is stored."""
    weeks = list_weeks()
    return weeks[0] if weeks else None


# ── RENDERING ─────────────────────────────────────────────────────────────────

_NAV_STYLE = """
.gm-week-nav {
  box-sizing: border-box; display: flex; flex-wrap: wrap; gap: .6rem;
  align-items: center; justify-content: center;
  padding: .6rem 1rem; margin: 0; width: 100%;
  background: #171a21; color: #e6e8eb; border-bottom: 1px solid #232833;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  font-size: .9rem; line-height: 1.4;
}
.gm-week-nav a, .gm-week-nav select, .gm-week-nav span {
  font: inherit; color: #5aa9e6; text-decoration: none;
  border: 1px solid #232833; border-radius: 7px;
  padding: .3rem .7rem; background: #0f1115;
}
.gm-week-nav a:hover { border-color: #5aa9e6; }
.gm-week-nav select { color: #e6e8eb; max-width: 60vw; }
.gm-week-nav .gm-week-nav__off { color: #8b93a1; border-color: #232833; opacity: .55; }
"""

_STYLE = """
:root {
  color-scheme: light dark;
  --bg:#0f1115; --fg:#e6e8eb; --muted:#8b93a1; --card-bg:#171a21; --card-border:#232833;
  --accent:#5aa9e6;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#f5f6f8; --fg:#1a1d23; --muted:#6b7280; --card-bg:#fff; --card-border:#e4e7ec; }
}
* { box-sizing: border-box; }
body {
  margin:0; padding:0; background:var(--bg); color:var(--fg); line-height:1.5;
  font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}
/* Padding lives on main, not body, so the nav bar spans the full width. */
main { max-width:640px; margin:0 auto; padding:2rem 1.5rem; }
h1 { font-size:1.4rem; margin:0 0 .35rem; }
.meta { color:var(--muted); font-size:.85rem; margin-bottom:1.5rem; }
.card {
  background:var(--card-bg); border:1px solid var(--card-border);
  border-radius:10px; padding:1.25rem;
}
.links { margin-top:1.5rem; font-size:.9rem; }
.links a { color:var(--accent); margin-right:1rem; }
p { margin:.5rem 0; }
code { background:var(--card-bg); border:1px solid var(--card-border);
       border-radius:4px; padding:.1rem .35rem; }
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _url(path: str, token: str | None) -> str:
    """A route URL carrying the bearer token, when one was supplied."""
    return f"{path}?{urlencode({'token': token})}" if token else path


def _page(title: str, body: str, token: str | None = None) -> str:
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        f"<title>{_e(title)}</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        f'{navbar.render_nav_html("weekly-summary", token)}'
        f"<main>{body}</main></body></html>"
    )


def render_nav_html(current_id: str, weeks: list[dict], token: str | None = None) -> str:
    """The navigation header injected above each report.

    ``weeks`` is the newest-first listing from :func:`list_weeks`. "Previous"
    walks back in time, "Next" forward; either becomes an inert label at the
    ends of the range.
    """
    ids = [w["id"] for w in weeks]
    position = ids.index(current_id) if current_id in ids else None

    # weeks is newest-first, so the *newer* week sits at the lower index.
    newer = weeks[position - 1] if position not in (None, 0) else None
    older = (
        weeks[position + 1]
        if position is not None and position + 1 < len(weeks)
        else None
    )

    def arrow(entry, label, css):
        if entry is None:
            return f'<span class="gm-week-nav__off {css}">{_e(label)}</span>'
        return (
            f'<a class="{css}" href="{_e(_url(entry["url"], token))}">'
            f"{_e(label)}</a>"
        )

    options = []
    for entry in weeks:
        selected = " selected" if entry["id"] == current_id else ""
        options.append(
            f'<option value="{_e(_url(entry["url"], token))}"{selected}>'
            f'{_e(entry["label"])}</option>'
        )
    if position is None:  # a week that isn't in the listing (shouldn't happen)
        options.insert(0, '<option selected>This week</option>')

    select = (
        '<select class="gm-week-nav__weeks" aria-label="Choose a week" '
        'onchange="if(this.value)window.location.href=this.value">'
        f'{"".join(options)}</select>'
    )

    return (
        f"<style>{_NAV_STYLE}</style>"
        '<nav class="gm-week-nav">'
        f'{arrow(older, "← Previous week", "gm-week-nav__prev")}'
        f"{select}"
        f'{arrow(newer, "Next week →", "gm-week-nav__next")}'
        "</nav>"
    )


def inject_nav(page: str, current_id: str, weeks: list[dict],
               token: str | None = None) -> str:
    """Put the site nav and the week switcher at the top of a report's ``<body>``.

    The report itself is left untouched otherwise; when it has no ``<body>``
    tag (a fragment rather than a full document) the navs are simply prepended.
    """
    site_nav = (
        "" if f'id="{navbar.NAV_ID}"' in page
        else navbar.render_nav_html("weekly-summary", token)
    )
    nav = site_nav + render_nav_html(current_id, weeks, token)
    match = _BODY_TAG_RE.search(page)
    if match:
        return page[: match.end()] + nav + page[match.end():]
    return nav + page


def render_no_summaries_html(token: str | None = None) -> str:
    """Placeholder shown at /weekly-summary when nothing has been uploaded."""
    body = (
        "<h1>No weekly reports yet</h1>"
        '<div class="meta">Nothing has been uploaded to this server yet.</div>'
        '<div class="card">'
        "<p>Weekly training reports are generated elsewhere and pushed in with "
        "the <code>upload_weekly_summary</code> MCP tool. The first upload "
        "shows up here.</p>"
        "</div>"
    )
    return _page("No weekly reports", body, token)


def render_missing_week_html(identifier: str, weeks: list[dict],
                             token: str | None = None) -> str:
    """404 page for a week with no stored report."""
    week_start = parse_week_id(identifier)
    name = week_label(week_start) if week_start else _e(identifier)
    if weeks:
        listed = "".join(
            f'<p><a href="{_e(_url(w["url"], token))}">{_e(w["label"])}</a></p>'
            for w in weeks
        )
        available = f'<div class="card">{listed}</div>'
    else:
        available = '<div class="card"><p>No reports have been uploaded yet.</p></div>'

    body = (
        "<h1>No report for that week</h1>"
        f'<div class="meta">{_e(name)} has not been uploaded.</div>'
        f"{available}"
        f'<div class="links">'
        f'<a href="{_e(_url(PATH_PREFIX, token))}">Latest report</a>'
        "</div>"
    )
    return _page("No report for that week", body, token)


# ── ROUTES ────────────────────────────────────────────────────────────────────

# Reports are regenerated in place for the current week, so never let an
# intermediary or the browser hold on to a stale copy.
_NO_STORE = {"Cache-Control": "no-store"}


def _serve_week(identifier: str, token: str | None):
    weeks = list_weeks()
    page = read_summary(identifier)
    if page is None:
        return HTMLResponse(
            render_missing_week_html(identifier, weeks, token),
            status_code=404,
            headers=_NO_STORE,
        )
    return HTMLResponse(inject_nav(page, identifier, weeks, token), headers=_NO_STORE)


async def serve_latest(request):
    """GET /weekly-summary — the most recent report, with navigation."""
    token = request.query_params.get("token")
    latest = latest_week()
    if latest is None:
        return HTMLResponse(render_no_summaries_html(token), headers=_NO_STORE)
    return _serve_week(latest["id"], token)


async def serve_week(request):
    """GET /weekly-summary/{week_id} — one week's report, with navigation."""
    token = request.query_params.get("token")
    return _serve_week(request.path_params["week_id"], token)


async def serve_list(request):
    """GET /weekly-summary/list — the available weeks as JSON, newest first."""
    return JSONResponse(list_weeks(), headers=_NO_STORE)


ROUTES = [
    Route(PATH_PREFIX, serve_latest, methods=["GET"]),
    # Ahead of the {week_id} route so "list" is never read as a week.
    Route(f"{PATH_PREFIX}/list", serve_list, methods=["GET"]),
    Route(f"{PATH_PREFIX}/{{week_id}}", serve_week, methods=["GET"]),
]


def owns_path(path: str) -> bool:
    """Whether a request path belongs to this sub-app (used for dispatch)."""
    return path == PATH_PREFIX or path.startswith(f"{PATH_PREFIX}/")


def create_app() -> Starlette:
    """The weekly-summary sub-app, mounted by server.py behind token auth."""
    return Starlette(routes=ROUTES)
