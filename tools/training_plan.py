# tools/training_plan.py
"""The Claude Coach training-plan viewer, backed by PostgreSQL.

The coach skill produces a plan JSON; it's uploaded here, stored as one
document per plan id (tools/plan_service.py), and served inside the viewer
template (tools/assets/plan-viewer.html) at request time — so every device
sees the same plan, completion ticks, edits and zones. One plan is active;
older plans are archived and viewable read-only.

Routes (all behind the same ``?token=`` bearer auth as ``/mcp`` and
``/dashboard``, enforced by the wrapper in server.py). ``?plan=<id>`` selects
a plan; without it the active plan is used.

    GET  /training-plan                  — the viewer, or a "No plan active" page
    GET  /training-plan/plans            — every plan: view / activate / archive / delete
    POST /training-plan/plans/{id}/{action}  — activate | archive | delete
    GET  /training-plan/upload           — the upload form
    POST /training-plan/upload           — validate, confirm a same-id replace, store
    GET  /training-plan/export.json      — the plan JSON as currently edited
    GET  /training-plan/pdf              — printable wall-chart PDF

JSON API used by the viewer:

    GET  /training-plan/api/plan         — {plan, id, status, version, readOnly, completed}
    POST /training-plan/api/operations   — {operations: [...]} → same shape
    POST /training-plan/api/completion   — {workout_id, completed} → same shape
    GET  /training-plan/api/revisions    — [{version, source, summary, created_at}]
    POST /training-plan/api/revisions/{version}/restore → same shape as /api/plan
"""
import html
import json
import logging
import os
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tools import plan_doc, plan_service
from tools.navbar import inject_no_zoom_meta
from tools.pdf_route import render_plan_pdf
from tools.plan_doc import PlanError
from tools.plan_service import PlanStorageUnavailable

logger = logging.getLogger(__name__)

# Form field names on the upload form.
FILE_FIELD = "plan_json"
TEXT_FIELD = "plan_text"  # the JSON carried through the replace confirmation
CONFIRM_FIELD = "confirm_replace"

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "assets", "plan-viewer.html")

# The plan changes on every edit; never let a browser or proxy hold a stale copy.
_NO_STORE = {"Cache-Control": "no-store"}


# ── RENDERING ─────────────────────────────────────────────────────────────────

# The viewer's own "Nocturne" tokens, so these pages match the plan app.
_STYLE = """
:root {
  --bg:#161826; --surface:#232532; --fg:#e9e9ed; --muted:#9397ab; --line:#3f424d;
  --accent:#9184d9; --accent-soft:#423a6a; --err:#e0736f; --ok:#7fb87a; --warn:#d9b35a;
}
* { box-sizing: border-box; }
body {
  margin:0; background:var(--bg); color:var(--fg); line-height:1.55; font-size:15px;
  font-family:"Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width:720px; margin:0 auto; padding:2rem 1.1rem 4rem; }
h1 { font-size:1.45rem; font-weight:500; margin:0 0 .35rem; letter-spacing:-0.015em; }
h2 { font-size:1.05rem; font-weight:500; margin:1.5rem 0 .5rem; }
.meta { color:var(--muted); font-size:.85rem; margin-bottom:1.25rem; }
.card { background:var(--surface); box-shadow:0 0 0 1px var(--line); border-radius:10px; padding:1.1rem; margin-bottom:.9rem; }
.row { display:flex; gap:.75rem; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; }
.title { font-weight:500; font-size:1rem; }
.sub { color:var(--muted); font-size:.82rem; }
.tag { display:inline-block; font-size:.7rem; letter-spacing:.04em; text-transform:uppercase; padding:2px 8px; border-radius:6px;
  border:1px solid var(--accent); color:var(--accent); }
.tag.archived { border-color:var(--line); color:var(--muted); }
label { display:block; font-size:.82rem; color:var(--muted); margin:0 0 .35rem; }
input[type=file] { width:100%; font:inherit; color:var(--fg); }
.actions { display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.9rem; }
button, .btn {
  display:inline-block; padding:.45rem .95rem; font:inherit; font-size:.9rem; border-radius:8px; cursor:pointer;
  border:1px solid var(--line); background:transparent; color:var(--fg); text-decoration:none;
}
.btn-primary, button.btn-primary { border-color:var(--accent); color:var(--accent); }
.btn-danger { border-color:var(--err); color:var(--err); }
form.inline { display:inline; margin:0; }
.error { color:var(--err); font-size:.9rem; margin:0 0 .9rem; white-space:pre-wrap; }
.warn { color:var(--warn); font-size:.88rem; }
.ok { color:var(--ok); }
ul { margin:.35rem 0; padding-left:1.2rem; }
li { margin:.15rem 0; }
.links { margin-top:1.25rem; font-size:.9rem; display:flex; gap:1rem; flex-wrap:wrap; }
.links a { color:var(--accent); }
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _url(path: str, token: str | None, **params) -> str:
    """A route URL carrying the bearer token (when supplied) and extra params."""
    query = {k: v for k, v in params.items() if v is not None}
    if token:
        query["token"] = token
    return f"{path}?{urlencode(query)}" if query else path


def _page(title: str, body: str) -> str:
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


def _json_for_script(value) -> str:
    # Any "<" inside a string ("</script>", "<!--") could end or confuse the
    # <script> data island; \u003c is the same character to JSON.parse.
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def render_viewer_html(row: dict) -> str:
    """The viewer template with the plan and its server state filled in."""
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        page = f.read()
    page = page.replace("__PLAN_TITLE__", _e(plan_doc.plan_title(row["plan"])))
    page = page.replace("__PLAN_SERVER_JSON__", _json_for_script(plan_service.view_payload(row)))
    page = page.replace("__PLAN_JSON__", _json_for_script(row["plan"]))
    return inject_no_zoom_meta(page)


def render_message_html(title: str, message: str, token: str | None) -> str:
    body = (
        f"<h1>{_e(title)}</h1>"
        f'<div class="card"><p>{message}</p></div>'
        f'<div class="links"><a href="{_e(_url("/training-plan/plans", token))}">All plans</a>'
        f'<a href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a></div>'
    )
    return _page(title, body)


def render_no_plan_html(token: str | None = None) -> str:
    """Placeholder page shown at /training-plan when no plan is active."""
    body = (
        "<h1>No plan active</h1>"
        '<div class="meta">Upload a Claude Coach plan JSON, or activate an archived plan.</div>'
        '<div class="card"><div class="actions" style="margin-top:0">'
        f'<a class="btn btn-primary" href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a>'
        f'<a class="btn" href="{_e(_url("/training-plan/plans", token))}">All plans</a>'
        "</div></div>"
    )
    return _page("No plan active", body)


def render_upload_form_html(token: str | None = None, error: str | None = None) -> str:
    """The one-file JSON upload form."""
    try:
        active = plan_service.get_plan()
    except PlanStorageUnavailable:
        active = None
    if active:
        meta = active["plan"].get("meta") or {}
        current = (
            f'<div class="meta">Active plan: {_e(meta.get("event") or active["id"])} '
            f'(<code>{_e(active["id"])}</code>). Uploading a plan with a different id '
            "archives it; the same id replaces its content after a confirmation.</div>"
        )
    else:
        current = '<div class="meta">No plan is active right now.</div>'

    error_html = f'<p class="error">{_e(error)}</p>' if error else ""
    body = (
        "<h1>Upload training plan</h1>"
        f"{current}"
        '<div class="card">'
        f"{error_html}"
        f'<form method="post" action="{_e(_url("/training-plan/upload", token))}" enctype="multipart/form-data">'
        f'<label for="{FILE_FIELD}">Plan JSON from Claude Coach</label>'
        f'<input id="{FILE_FIELD}" name="{FILE_FIELD}" type="file" accept=".json,application/json" required>'
        '<div class="actions"><button type="submit" class="btn-primary">Upload</button></div>'
        "</form></div>"
        f'<div class="links"><a href="{_e(_url("/training-plan", token))}">View plan</a>'
        f'<a href="{_e(_url("/training-plan/plans", token))}">All plans</a></div>'
    )
    return _page("Upload training plan", body)


def render_confirm_replace_html(plan: dict, preview: dict, warnings: list[str], token: str | None) -> str:
    """Shown when an upload's id matches a stored plan: what replacing changes."""
    meta = plan.get("meta") or {}
    items = [
        f"Stored copy: version {preview['version']} ({_e(preview['status'])}), last changed "
        f"{_e(preview['updated_at'][:16].replace('T', ' '))} UTC"
        + (f" by {_e(preview['last_source'])} — “{_e(preview['last_change'])}”" if preview.get("last_change") else "")
        + ".",
        f"<b>{preview['completed_kept']}</b> completed workout(s) keep their tick.",
    ]
    if preview["edits_since_upload"]:
        items.append(
            f'<span class="warn">{preview["edits_since_upload"]} web/MCP edit(s) since the last upload '
            "will be overwritten by the file's content.</span>"
        )
    if preview["completed_orphaned"]:
        names = ", ".join(_e(o["name"] or o["id"]) for o in preview["completed_orphaned"])
        items.append(
            f'<span class="warn">{len(preview["completed_orphaned"])} completed workout(s) aren\'t in the new '
            f"file and will no longer show: {names}.</span>"
        )
    if preview["zone_overrides_reset"]:
        items.append("Zone overrides and validation flags reset to the file's zones.")
    if preview["status"] != "active":
        items.append("This archived plan becomes the active plan.")
    items.append("The current content is kept as a revision, so this can be undone from Settings → History.")
    warn_html = (
        '<h2>Warnings</h2><ul class="warn">' + "".join(f"<li>{_e(w)}</li>" for w in warnings) + "</ul>"
        if warnings else ""
    )
    body = (
        "<h1>Replace existing plan?</h1>"
        f'<div class="meta">{_e(meta.get("event") or preview["id"])} — <code>{_e(preview["id"])}</code> is already stored.</div>'
        '<div class="card"><ul>' + "".join(f"<li>{i}</li>" for i in items) + "</ul>"
        f"{warn_html}"
        f'<form method="post" action="{_e(_url("/training-plan/upload", token))}" enctype="multipart/form-data">'
        f'<textarea name="{TEXT_FIELD}" hidden>{_e(json.dumps(plan, ensure_ascii=False))}</textarea>'
        f'<input type="hidden" name="{CONFIRM_FIELD}" value="1">'
        '<div class="actions"><button type="submit" class="btn-primary">Replace plan</button>'
        f'<a class="btn" href="{_e(_url("/training-plan/upload", token))}">Cancel</a></div>'
        "</form></div>"
    )
    return _page("Replace existing plan?", body)


def render_upload_done_html(row: dict, warnings: list[str], token: str | None) -> str:
    """Only shown when an upload succeeded with warnings (otherwise: redirect)."""
    body = (
        "<h1>Plan uploaded</h1>"
        f'<div class="meta"><code>{_e(row["id"])}</code> is now the active plan (version {row["version"]}).</div>'
        f'<div class="card"><div class="title">{len(warnings)} warning(s)</div><ul class="warn">'
        + "".join(f"<li>{_e(w)}</li>" for w in warnings)
        + "</ul></div>"
        f'<div class="links"><a href="{_e(_url("/training-plan", token))}">View plan</a></div>'
    )
    return _page("Plan uploaded", body)


def render_plans_html(plans: list[dict], token: str | None, error: str | None = None) -> str:
    """Every stored plan with its actions."""
    cards = []
    for p in plans:
        active = p["status"] == "active"
        dates = " – ".join(d for d in (p.get("start_date"), p.get("end_date")) if d)
        view_href = _url("/training-plan", token, plan=None if active else p["id"])
        actions = [
            f'<a class="btn btn-primary" href="{_e(view_href)}">{"View" if active else "View (read-only)"}</a>',
            f'<a class="btn" href="{_e(_url("/training-plan/export.json", token, plan=p["id"]))}">Download JSON</a>',
        ]

        def post(action, label, cls="", confirm=None):
            onsubmit = f' onsubmit="return confirm({_e(json.dumps(confirm))})"' if confirm else ""
            action_url = _url(f"/training-plan/plans/{p['id']}/{action}", token)
            return (
                f'<form class="inline" method="post" action="{_e(action_url)}"{onsubmit}>'
                f'<button type="submit" class="{cls}">{label}</button></form>'
            )

        if active:
            actions.append(post("archive", "Archive", confirm="Archive this plan? No plan will be active."))
        else:
            actions.append(post("activate", "Make active"))
            actions.append(post("delete", "Delete", "btn-danger",
                                confirm="Permanently delete this plan, its history and its completion ticks?"))
        cards.append(
            '<div class="card"><div class="row"><div>'
            f'<div class="title">{_e(p.get("event") or p["id"])}</div>'
            f'<div class="sub">{_e(p.get("athlete") or "")}{" · " if p.get("athlete") and dates else ""}{_e(dates)}</div>'
            f'<div class="sub"><code>{_e(p["id"])}</code> · version {p["version"]} · updated {_e(str(p["updated_at"])[:10])}</div>'
            f'</div><span class="tag{"" if active else " archived"}">{_e(p["status"])}</span></div>'
            f'<div class="actions">{"".join(actions)}</div></div>'
        )
    error_html = f'<p class="error">{_e(error)}</p>' if error else ""
    body = (
        "<h1>Training plans</h1>"
        '<div class="meta">One plan is active at a time. Archived plans stay viewable, read-only.</div>'
        f"{error_html}"
        + ("".join(cards) or '<div class="card">No plans stored yet.</div>')
        + f'<div class="links"><a href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a>'
        f'<a href="{_e(_url("/training-plan", token))}">Active plan</a></div>'
    )
    return _page("Training plans", body)


# ── ROUTES ────────────────────────────────────────────────────────────────────

def _token(request):
    return request.query_params.get("token")


def _plan_param(request):
    return request.query_params.get("plan") or None


def _unavailable(request, exc):
    return HTMLResponse(render_message_html("Plans unavailable", _e(exc), _token(request)),
                        status_code=503, headers=_NO_STORE)


async def serve_plan(request):
    """GET /training-plan — the viewer for the active (or ?plan=) plan."""
    token = _token(request)
    try:
        row = plan_service.get_plan(_plan_param(request))
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    if row is None:
        if _plan_param(request):
            return HTMLResponse(render_message_html("Plan not found", "No plan with that id.", token),
                                status_code=404, headers=_NO_STORE)
        return HTMLResponse(render_no_plan_html(token), headers=_NO_STORE)
    return HTMLResponse(render_viewer_html(row), headers=_NO_STORE)


async def serve_plans(request):
    """GET /training-plan/plans — every stored plan."""
    try:
        plans = plan_service.list_plans()
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    return HTMLResponse(render_plans_html(plans, _token(request), request.query_params.get("error")),
                        headers=_NO_STORE)


async def handle_plan_action(request):
    """POST /training-plan/plans/{id}/{action} — activate | archive | delete."""
    token = _token(request)
    plan_id, action = request.path_params["plan_id"], request.path_params["action"]
    try:
        if action == "activate":
            plan_service.set_status(plan_id, "active")
        elif action == "archive":
            plan_service.set_status(plan_id, "archived")
        elif action == "delete":
            plan_service.delete_plan(plan_id)
        else:
            return HTMLResponse("Unknown action.", status_code=404)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    except (LookupError, PlanError) as e:
        return RedirectResponse(_url("/training-plan/plans", token, error=str(e).strip("'\"")), status_code=303)
    return RedirectResponse(_url("/training-plan/plans", token), status_code=303)


async def serve_upload_form(request):
    """GET /training-plan/upload."""
    return HTMLResponse(render_upload_form_html(_token(request)), headers=_NO_STORE)


async def handle_upload(request):
    """POST /training-plan/upload — validate; confirm a same-id replace; store."""
    token = _token(request)

    def form_error(message, status=400):
        return HTMLResponse(render_upload_form_html(token, message), status_code=status, headers=_NO_STORE)

    async with request.form(max_part_size=plan_service.MAX_UPLOAD_BYTES) as form:
        upload = form.get(FILE_FIELD)
        text = form.get(TEXT_FIELD)
        confirmed = form.get(CONFIRM_FIELD) == "1"
        if hasattr(upload, "read"):
            raw = await upload.read()
        elif isinstance(text, str) and text:
            raw = text.encode("utf-8")
        else:
            return form_error("A plan JSON file is required.")

    try:
        plan, warnings = plan_service.parse_upload(raw)
        preview = plan_service.upload_preview(plan)
        if preview is not None and not confirmed:
            return HTMLResponse(render_confirm_replace_html(plan, preview, warnings, token), headers=_NO_STORE)
        row = plan_service.save_upload(plan)
    except PlanError as e:
        return form_error(str(e))
    except PlanStorageUnavailable as e:
        return form_error(str(e), 503)

    if warnings:
        return HTMLResponse(render_upload_done_html(row, warnings, token), headers=_NO_STORE)
    return RedirectResponse(_url("/training-plan", token), status_code=303)


async def serve_export(request):
    """GET /training-plan/export.json — download the plan as currently edited."""
    try:
        row = plan_service.require_plan(_plan_param(request))
    except PlanStorageUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except LookupError as e:
        return JSONResponse({"error": str(e).strip("'\"")}, status_code=404)
    return Response(
        json.dumps(row["plan"], ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="{row["id"]}.json"'},
    )


async def serve_plan_pdf(request):
    """GET /training-plan/pdf — the wall chart, rendered from the stored plan
    (zone overrides included, since they live in the plan document)."""
    try:
        row = plan_service.get_plan(_plan_param(request))
    except PlanStorageUnavailable as e:
        return HTMLResponse(str(e), status_code=503, headers=_NO_STORE)
    if row is None:
        return HTMLResponse("No plan active.", status_code=404, headers=_NO_STORE)
    return await render_plan_pdf(row["plan"])


# ── JSON API ──────────────────────────────────────────────────────────────────

def _api_error(exc) -> JSONResponse:
    if isinstance(exc, PlanStorageUnavailable):
        return JSONResponse({"error": str(exc)}, status_code=503)
    if isinstance(exc, LookupError):
        return JSONResponse({"error": str(exc).strip("'\"")}, status_code=404)
    return JSONResponse({"error": str(exc)}, status_code=400)


def _plan_response(row: dict, extra: dict | None = None) -> JSONResponse:
    return JSONResponse({"plan": row["plan"], **plan_service.view_payload(row), **(extra or {})},
                        headers=_NO_STORE)


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise PlanError("Request body must be JSON.") from None
    if not isinstance(body, dict):
        raise PlanError("Request body must be a JSON object.")
    return body


async def api_get_plan(request):
    try:
        return _plan_response(plan_service.require_plan(_plan_param(request)))
    except (PlanStorageUnavailable, LookupError) as e:
        return _api_error(e)


async def api_operations(request):
    try:
        body = await _json_body(request)
        result = plan_service.apply_operations(
            _plan_param(request), body.get("operations"), "web", body.get("reason"),
        )
    except (PlanStorageUnavailable, LookupError, PlanError) as e:
        return _api_error(e)
    return _plan_response(result["row"], {"changes": result["changes"], "touched": result["touched"]})


async def api_completion(request):
    try:
        body = await _json_body(request)
        workout_id = body.get("workout_id")
        if not isinstance(workout_id, str) or not workout_id:
            raise PlanError("workout_id is required.")
        plan_service.set_completion(_plan_param(request), workout_id, bool(body.get("completed", True)))
        row = plan_service.require_plan(_plan_param(request))
    except (PlanStorageUnavailable, LookupError, PlanError) as e:
        return _api_error(e)
    return _plan_response(row)


async def api_revisions(request):
    try:
        return JSONResponse(plan_service.revisions(_plan_param(request)), headers=_NO_STORE)
    except (PlanStorageUnavailable, LookupError) as e:
        return _api_error(e)


async def api_restore(request):
    try:
        version = int(request.path_params["version"])
        row = plan_service.restore_revision(_plan_param(request), version, "web")
    except (PlanStorageUnavailable, LookupError, PlanError, ValueError) as e:
        return _api_error(e)
    return _plan_response(row)


ROUTES = [
    Route("/training-plan", serve_plan, methods=["GET"]),
    Route("/training-plan/plans", serve_plans, methods=["GET"]),
    Route("/training-plan/plans/{plan_id}/{action}", handle_plan_action, methods=["POST"]),
    Route("/training-plan/upload", serve_upload_form, methods=["GET"]),
    Route("/training-plan/upload", handle_upload, methods=["POST"]),
    Route("/training-plan/export.json", serve_export, methods=["GET"]),
    Route("/training-plan/pdf", serve_plan_pdf, methods=["GET"]),
    Route("/training-plan/api/plan", api_get_plan, methods=["GET"]),
    Route("/training-plan/api/operations", api_operations, methods=["POST"]),
    Route("/training-plan/api/completion", api_completion, methods=["POST"]),
    Route("/training-plan/api/revisions", api_revisions, methods=["GET"]),
    Route("/training-plan/api/revisions/{version}/restore", api_restore, methods=["POST"]),
]

PATH_PREFIX = "/training-plan"


def owns_path(path: str) -> bool:
    """Whether a request path belongs to this sub-app (used for dispatch)."""
    return path == PATH_PREFIX or path.startswith(f"{PATH_PREFIX}/")


def create_app() -> Starlette:
    """The training-plan sub-app, mounted by server.py behind token auth."""
    return Starlette(routes=ROUTES)
