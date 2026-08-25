# tools/training_plan.py
"""Hosting for the Claude Coach training-plan viewer (issue 34).

The plan is a self-contained HTML app with its JSON embedded inline in a
``<script type="application/json" id="plan-data">`` tag, so serving it is a
straight file read — no templating, no database. Completion/edit state lives in
the browser's localStorage, per-device.

Storage is the Azure File Share already mounted for the Garmin OAuth tokens
(``~/.garminconnect``), under a ``training-plan/`` folder holding ``plan.html``
and ``plan.json``. Only one plan is active at a time: an upload replaces the
previous one. ``TRAINING_PLAN_DIR`` overrides the location (used by tests).

Routes (all behind the same ``?token=`` bearer auth as ``/mcp`` and
``/dashboard``, enforced by the wrapper in server.py):

    GET  /training-plan          — the current plan, or a "No plan active" page
    GET  /training-plan/upload   — a minimal one-file upload form
    POST /training-plan/upload   — stores the upload, redirects to the plan
    POST /training-plan/reset    — deletes the stored plan
"""
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from tools.navbar import inject_no_zoom_meta

logger = logging.getLogger(__name__)

PLAN_HTML_NAME = "plan.html"
PLAN_JSON_NAME = "plan.json"

# Form field names on the upload form.
HTML_FIELD = "plan_html"

PLAN_DATA_RE = re.compile(
    r'<script[^>]*id=["\']plan-data["\'][^>]*>(.*?)</script>', re.S | re.I
)

# Refuse absurd uploads rather than filling the file share. A compiled plan app
# with embedded JSON is a few hundred KB.
MAX_UPLOAD_BYTES = int(os.environ.get("TRAINING_PLAN_MAX_BYTES", 20 * 1024 * 1024))

# The token file share is mounted at ~/.garminconnect (see garmin_client.py);
# the path is repeated here rather than imported so this module stays usable
# without a configured Garmin session.
DEFAULT_PLAN_DIR = os.path.join(os.path.expanduser("~/.garminconnect"), "training-plan")


# ── STORAGE ───────────────────────────────────────────────────────────────────

def plan_dir() -> str:
    """Directory holding the active plan. Read per call so tests can override."""
    return os.environ.get("TRAINING_PLAN_DIR") or DEFAULT_PLAN_DIR


def plan_html_path() -> str:
    return os.path.join(plan_dir(), PLAN_HTML_NAME)


def plan_json_path() -> str:
    return os.path.join(plan_dir(), PLAN_JSON_NAME)


def plan_exists() -> bool:
    """True when a plan HTML file is stored — the HTML is what gets served."""
    return os.path.isfile(plan_html_path())


def read_plan_html() -> str | None:
    """The stored plan HTML, or None when no plan is active."""
    try:
        with open(plan_html_path(), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def plan_info() -> dict | None:
    """Size / modification time of the active plan, or None when there is none.

    Also surfaces a few fields from ``plan.json`` when it parses, so the upload
    form can show which plan is currently live.
    """
    try:
        stat = os.stat(plan_html_path())
    except FileNotFoundError:
        return None

    info = {
        "html_bytes": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                              .strftime("%Y-%m-%d %H:%M UTC"),
        "title": None,
        "athlete": None,
    }

    try:
        with open(plan_json_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            meta = data.get("meta") or {}
            if isinstance(meta, dict):
                info["title"] = meta.get("event")
                info["athlete"] = meta.get("athlete")
            if not info["title"]:
                for key in ("title", "name", "plan_name", "planName"):
                    if isinstance(data.get(key), str):
                        info["title"] = data[key]
                        break
    except (OSError, ValueError):
        pass  # JSON missing or unparseable — the HTML is still servable

    return info


def extract_plan_json(html_text: str) -> bytes | None:
    """Return validated JSON embedded in the plan-data script, if present."""
    match = PLAN_DATA_RE.search(html_text)
    if not match:
        return None
    try:
        json.loads(match.group(1))
    except ValueError:
        return None
    return match.group(1).strip().encode("utf-8")


def save_plan(html_bytes: bytes) -> None:
    """Store an uploaded plan, replacing any existing one.

    The JSON source is extracted from the uploaded HTML and stored next to it.
    Both files are written via a temp file + rename so a partial write can
    never be served. Raises ValueError when the upload doesn't look like a
    plan (empty HTML, or missing/invalid embedded JSON).
    """
    if not html_bytes.strip():
        raise ValueError("The HTML file is empty.")
    if len(html_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError(f"The HTML file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    try:
        html_text = html_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("The HTML file is not valid UTF-8 text.") from e

    if "<" not in html_text:
        raise ValueError("The HTML file doesn't look like HTML.")

    json_bytes = extract_plan_json(html_text)
    if json_bytes is None:
        raise ValueError("The HTML file has no valid embedded plan JSON.")

    directory = plan_dir()
    os.makedirs(directory, exist_ok=True)
    _atomic_write(plan_html_path(), html_bytes)
    _atomic_write(plan_json_path(), json_bytes)
    logger.info("Stored training plan (%d bytes HTML) in %s", len(html_bytes), directory)


def _atomic_write(path: str, payload: bytes) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(payload)
    os.replace(tmp, path)


def reset_plan() -> list[str]:
    """Delete the stored plan files. Returns the names actually removed."""
    removed = []
    for path in (plan_html_path(), plan_json_path()):
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
        except FileNotFoundError:
            pass
    if removed:
        logger.info("Reset training plan, removed: %s", ", ".join(removed))
    return removed


# ── RENDERING ─────────────────────────────────────────────────────────────────

_STYLE = """
:root {
  color-scheme: light dark;
  --bg:#0f1115; --fg:#e6e8eb; --muted:#8b93a1; --card-bg:#171a21; --card-border:#232833;
  --accent:#5aa9e6; --err:#e0736f; --ok:#6fae7d;
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
label { display:block; font-size:.85rem; color:var(--muted); margin:1rem 0 .35rem; }
label:first-of-type { margin-top:0; }
input[type=file] { width:100%; font:inherit; color:var(--fg); }
button, .btn {
  display:inline-block; margin-top:1.5rem; padding:.55rem 1.1rem; font:inherit;
  border-radius:7px; border:1px solid var(--card-border); background:var(--card-bg);
  color:var(--fg); cursor:pointer; text-decoration:none;
}
button[type=submit] { border-color:var(--accent); color:var(--accent); }
.error { color:var(--err); font-size:.9rem; margin:0 0 1rem; }
.ok { color:var(--ok); }
.links { margin-top:1.5rem; font-size:.9rem; }
.links a { color:var(--accent); margin-right:1rem; }
form.inline { display:inline; }
form.inline button { margin:0; font-size:.9rem; padding:.3rem .7rem; }
p { margin:.5rem 0; }
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
        f"<main>{body}</main></body></html>"
    )


def render_no_plan_html(token: str | None = None) -> str:
    """Placeholder page shown at /training-plan when nothing is uploaded."""
    body = (
        "<h1>No plan active</h1>"
        '<div class="meta">Nothing has been uploaded to this server yet.</div>'
        '<div class="card">'
        "<p>Upload a compiled training-plan app to "
        "see it here.</p>"
        f'<a class="btn" href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a>'
        "</div>"
    )
    return _page("No plan active", body, token)


def render_upload_form_html(token: str | None = None, error: str | None = None) -> str:
    """The minimal one-file upload form — plain HTML, no framework."""
    info = plan_info()
    if info:
        title = f' &mdash; {_e(info["title"])}' if info.get("title") else ""
        current = (
            '<div class="meta">Active plan: uploaded '
            f'{_e(info["updated_at"])}{title}. Uploading replaces it.</div>'
        )
        reset = (
            f'<form class="inline" method="post" '
            f'action="{_e(_url("/training-plan/reset", token))}" '
            "onsubmit=\"return confirm('Delete the active plan?')\">"
            "<button type=\"submit\">Delete active plan</button></form>"
        )
    else:
        current = '<div class="meta">No plan is active right now.</div>'
        reset = ""

    error_html = f'<p class="error">{_e(error)}</p>' if error else ""

    body = (
        "<h1>Upload training plan</h1>"
        f"{current}"
        '<div class="card">'
        f"{error_html}"
        f'<form method="post" action="{_e(_url("/training-plan/upload", token))}" '
        'enctype="multipart/form-data">'
        f'<label for="{HTML_FIELD}">Plan HTML file</label>'
        f'<input id="{HTML_FIELD}" name="{HTML_FIELD}" type="file" accept=".html,.htm,text/html" required>'
        '<button type="submit">Upload</button>'
        "</form>"
        "</div>"
        f'<div class="links"><a href="{_e(_url("/training-plan", token))}">View plan</a>{reset}</div>'
    )
    return _page("Upload training plan", body, token)


def render_reset_html(removed: list[str], token: str | None = None) -> str:
    """Confirmation page for POST /training-plan/reset."""
    if removed:
        message = f'<p class="ok">Deleted {_e(", ".join(removed))}.</p>'
    else:
        message = "<p>Nothing to delete — no plan was active.</p>"

    body = (
        "<h1>Plan reset</h1>"
        f'<div class="card">{message}</div>'
        f'<div class="links">'
        f'<a href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a>'
        f'<a href="{_e(_url("/training-plan", token))}">View plan</a>'
        "</div>"
    )
    return _page("Plan reset", body, token)


# ── ROUTES ────────────────────────────────────────────────────────────────────

# The plan embeds per-browser localStorage state and is replaced on upload, so
# never let an intermediary or the browser hold on to a stale copy.
_NO_STORE = {"Cache-Control": "no-store"}


async def serve_plan(request):
    """GET /training-plan — the stored plan, or the placeholder page."""
    token = request.query_params.get("token")
    page = read_plan_html()
    if page is None:
        return HTMLResponse(render_no_plan_html(token), headers=_NO_STORE)
    return HTMLResponse(inject_no_zoom_meta(page), headers=_NO_STORE)


async def serve_upload_form(request):
    """GET /training-plan/upload — the one-file form."""
    token = request.query_params.get("token")
    return HTMLResponse(render_upload_form_html(token), headers=_NO_STORE)


async def handle_upload(request):
    """POST /training-plan/upload — store the plan, then redirect to it."""
    token = request.query_params.get("token")

    async with request.form() as form:
        html_upload = form.get(HTML_FIELD)
        if not hasattr(html_upload, "read"):
            return HTMLResponse(
                render_upload_form_html(token, "An HTML file is required."),
                status_code=400,
                headers=_NO_STORE,
            )
        html_bytes = await html_upload.read()

    try:
        save_plan(html_bytes)
    except ValueError as e:
        return HTMLResponse(
            render_upload_form_html(token, str(e)), status_code=400, headers=_NO_STORE
        )
    except OSError as e:  # pragma: no cover — file share unavailable
        logger.exception("Failed to store training plan")
        return HTMLResponse(
            render_upload_form_html(token, f"Could not write to storage: {e}"),
            status_code=500,
            headers=_NO_STORE,
        )

    return RedirectResponse(_url("/training-plan", token), status_code=303)


async def handle_reset(request):
    """POST /training-plan/reset — delete the stored plan."""
    token = request.query_params.get("token")
    try:
        removed = reset_plan()
    except OSError as e:  # pragma: no cover — file share unavailable
        logger.exception("Failed to reset training plan")
        return HTMLResponse(f"Could not delete plan: {_e(e)}", status_code=500, headers=_NO_STORE)
    return HTMLResponse(render_reset_html(removed, token), headers=_NO_STORE)


ROUTES = [
    Route("/training-plan", serve_plan, methods=["GET"]),
    Route("/training-plan/upload", serve_upload_form, methods=["GET"]),
    Route("/training-plan/upload", handle_upload, methods=["POST"]),
    Route("/training-plan/reset", handle_reset, methods=["POST"]),
]

PATH_PREFIX = "/training-plan"


def owns_path(path: str) -> bool:
    """Whether a request path belongs to this sub-app (used for dispatch)."""
    return path == PATH_PREFIX or path.startswith(f"{PATH_PREFIX}/")


def create_app() -> Starlette:
    """The training-plan sub-app, mounted by server.py behind token auth."""
    return Starlette(routes=ROUTES)
