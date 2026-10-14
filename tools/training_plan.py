# tools/training_plan.py
"""The Claude Coach training-plan viewer, backed by PostgreSQL.

The coach skill produces a plan JSON; it's uploaded here, stored as one
document per plan id (tools/plan_service.py), and served inside the viewer
template (tools/assets/plan-viewer.html) at request time — so every device
sees the same plan, completion ticks, edits and zones. One plan is active;
future plans are planned ahead and stay editable; archived plans are viewable
read-only.

Routes (all behind the same ``?token=`` bearer auth as ``/mcp`` and
``/dashboard``, enforced by the wrapper in server.py). ``?plan=<id>`` selects
a plan; without it the active plan is used.

    GET  /training-plan                  — the viewer, or a "No plan active" page
    GET  /training-plan/season           — every plan on one timeline: weekly hours, phases, races
    GET  /training-plan/plans            — every plan: view / activate / future / archive / delete
    POST /training-plan/plans/{id}/{action}  — activate | future | archive | delete
    GET  /training-plan/upload           — the upload form
    POST /training-plan/upload           — validate, confirm a same-id replace, store
                                           (as the active plan only when asked)
    GET  /training-plan/export.json      — the plan JSON as currently edited
    GET  /training-plan/pdf              — printable wall-chart PDF
    GET  /training-plan/calendar.ics     — the active plan as a calendar feed;
                                           authorised by ?key=<feed token>, not
                                           the bearer token (see server.py)

JSON API used by the viewer:

    GET  /training-plan/api/plan         — {plan, id, status, version, readOnly, completed}
    POST /training-plan/api/operations   — {operations: [...]} → same shape
    POST /training-plan/api/completion   — {workout_id, completed} → same shape
    GET  /training-plan/api/revisions    — [{version, source, summary, created_at}]
    POST /training-plan/api/revisions/{version}/restore → same shape as /api/plan
    POST /training-plan/api/carryover    — {keys: [...]}: copy the outgoing plan's thresholds → same shape
    POST /training-plan/api/carryover/dismiss → same shape (carryover: null)
    GET  /training-plan/api/calendar     — {defaultTime, slots, published, feedPath, publishedAt}
    POST /training-plan/api/calendar/schedule — {defaultTime, slots} → same shape
    POST /training-plan/api/calendar/publish  — new feed token (replaces any old one)
    POST /training-plan/api/calendar/revoke   — no feed token; the link stops working
    GET  /training-plan/api/goal-race    — {goal, distances} for Settings' Goal race
    POST /training-plan/api/goal-race    — {name, date, distance, target?, show_on_today} → same shape
    POST /training-plan/api/goal-race/clear → same shape (goal: null)
    GET  /training-plan/api/timezone     — {timezone, effective, source, now} for Settings' Time zone
    POST /training-plan/api/timezone     — {timezone: "America/Toronto" | null} → same shape
"""
import html
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

import db
from tools import goal_race, local_time, plan_calendar, plan_doc, plan_service
from tools.navbar import ICON_LINKS, THEME_BOOT, inject_icon_links, inject_nav, render_nav_html
from tools.pdf_route import inject_pdf_links, render_plan_pdf
from tools.plan_doc import PlanError
from tools.plan_service import PlanStorageUnavailable

logger = logging.getLogger(__name__)

# Form field names on the upload form.
FILE_FIELD = "plan_json"
TEXT_FIELD = "plan_text"  # the JSON carried through the replace confirmation
CONFIRM_FIELD = "confirm_replace"
ACTIVATE_FIELD = "make_active"  # checkbox: store the upload as the active plan

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
html[data-theme=light] {
  --bg:#f4f4f8; --surface:#ffffff; --fg:#1c1e2a; --muted:#595d6c; --line:#e2e5f0;
  --accent:#6e61c2; --accent-soft:#e7e5fe; --err:#c4524d; --ok:#3f7a3a; --warn:#8a6a14;
}
* { box-sizing: border-box; }
@media (max-width: 899px) {
  html { padding-top: env(safe-area-inset-top, 0px); }
}
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
.tag.future { border-color:var(--ok); color:var(--ok); }
label { display:block; font-size:.82rem; color:var(--muted); margin:0 0 .35rem; }
input[type=file] { width:100%; font:inherit; color:var(--fg); }
label.check { display:flex; gap:.6rem; align-items:flex-start; margin:1rem 0 0; font-size:.9rem; color:var(--fg); cursor:pointer; }
label.check input { margin:.25rem 0 0; accent-color:var(--accent); }
label.check .sub { display:block; }
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


def _page(title: str, body: str, token: str | None = None, active: str = "plan") -> str:
    """A plain page (plan list, upload, messages) with the site nav."""
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        f"{THEME_BOOT}{ICON_LINKS}"
        f"<title>{_e(title)}</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        f"{render_nav_html(active, token)}"
        f"<main>{body}</main></body></html>"
    )


def _json_for_script(value) -> str:
    # Any "<" inside a string ("</script>", "<!--") could end or confuse the
    # <script> data island; \u003c is the same character to JSON.parse.
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def render_viewer_html(row: dict, token: str | None = None, goal_future=None, tz_future=None) -> str:
    """The viewer template with the plan and its server state filled in, and
    the site nav (its Plan and Settings items switch the viewer's views)."""
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        page = f.read()
    page = page.replace("__PLAN_TITLE__", _e(plan_doc.plan_title(row["plan"])))
    # Independent reads: overlap their database round trips rather than adding
    # them up (this is the page-open wait). The caller may already have started
    # the goal-race and time-zone reads while the plan itself was loading.
    with ThreadPoolExecutor(max_workers=3) as pool:
        payload = pool.submit(plan_service.view_payload, row)
        goal = goal_future or pool.submit(_goal_race_payload)
        tz = tz_future or pool.submit(_timezone_payload)
        server = {**payload.result(), "goalRace": goal.result(), "timezone": tz.result()}
    page = page.replace("__PLAN_SERVER_JSON__", _json_for_script(server))
    page = page.replace("__PLAN_JSON__", _json_for_script(row["plan"]))
    athlete = (row["plan"].get("meta") or {}).get("athlete")
    return inject_pdf_links(inject_icon_links(inject_nav(page, "plan", token, title=athlete)))


def render_message_html(title: str, message: str, token: str | None) -> str:
    body = (
        f"<h1>{_e(title)}</h1>"
        f'<div class="card"><p>{message}</p></div>'
        f'<div class="links"><a href="{_e(_url("/training-plan/plans", token))}">All plans</a>'
        f'<a href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a></div>'
    )
    return _page(title, body, token)


def render_no_plan_html(token: str | None = None, future: list[dict] | None = None) -> str:
    """Placeholder page shown at /training-plan when no plan is active —
    with the future plans, so the next one is a click from being active."""
    cards = []
    for p in future or []:
        activate_url = _url(f"/training-plan/plans/{p['id']}/activate", token)
        cards.append(
            '<div class="card"><div class="row"><div>'
            f'<div class="title">{_e(p.get("event") or p["id"])}</div>'
            f'<div class="sub">Starts {_e(p.get("start_date") or "?")}</div></div>'
            '<span class="tag future">future</span></div><div class="actions">'
            f'<a class="btn" href="{_e(_url("/training-plan", token, plan=p["id"]))}">View</a>'
            f'<form class="inline" method="post" action="{_e(activate_url)}">'
            '<button type="submit" class="btn-primary">Make active</button></form></div></div>'
        )
    cards = "".join(cards)
    body = (
        "<h1>No plan active</h1>"
        '<div class="meta">Upload a Claude Coach plan JSON, or make a future or archived plan active.</div>'
        + (f"<h2>Future plans</h2>{cards}" if cards else "")
        + '<div class="card"><div class="actions" style="margin-top:0">'
        f'<a class="btn btn-primary" href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a>'
        f'<a class="btn" href="{_e(_url("/training-plan/plans", token))}">All plans</a>'
        "</div></div>"
    )
    return _page("No plan active", body, token)


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
            f'(<code>{_e(active["id"])}</code>). A plan with a different id is stored as a '
            "future plan — it becomes active by itself on its start date — unless you make it "
            "active now (which archives this one); the same id replaces its content after a "
            "confirmation.</div>"
        )
        activate_hint = (f"Archives {_e(meta.get('event') or active['id'])}. Leave unticked to plan ahead — "
                         "it takes over on its start date, or make it active sooner from All plans.")
    else:
        current = '<div class="meta">No plan is active right now.</div>'
        activate_hint = "No plan is active, so this one can start straight away."
    # Never pre-ticked while a plan is active: replacing it is a deliberate choice.
    checked = "" if active else " checked"

    error_html = f'<p class="error">{_e(error)}</p>' if error else ""
    body = (
        "<h1>Upload training plan</h1>"
        f"{current}"
        '<div class="card">'
        f"{error_html}"
        f'<form method="post" action="{_e(_url("/training-plan/upload", token))}" enctype="multipart/form-data">'
        f'<label for="{FILE_FIELD}">Plan JSON from Claude Coach</label>'
        f'<input id="{FILE_FIELD}" name="{FILE_FIELD}" type="file" accept=".json,application/json" required>'
        f'<label class="check"><input type="checkbox" name="{ACTIVATE_FIELD}" value="1"{checked}>'
        f'<span>Make this the active plan<span class="sub">{activate_hint}</span></span></label>'
        '<div class="actions"><button type="submit" class="btn-primary">Upload</button></div>'
        "</form></div>"
        f'<div class="links"><a href="{_e(_url("/training-plan", token))}">View plan</a>'
        f'<a href="{_e(_url("/training-plan/plans", token))}">All plans</a></div>'
    )
    return _page("Upload training plan", body, token)


def render_confirm_replace_html(plan: dict, preview: dict, warnings: list[str], token: str | None,
                                activate: bool = False) -> str:
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
        if activate:
            items.append(f"This {_e(preview['status'])} plan becomes the active plan (the current one is archived).")
        elif preview["status"] == "archived":
            items.append("This archived plan comes back as a future plan (editable, not active).")
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
        + (f'<input type="hidden" name="{ACTIVATE_FIELD}" value="1">' if activate else "")
        + '<div class="actions"><button type="submit" class="btn-primary">Replace plan</button>'
        f'<a class="btn" href="{_e(_url("/training-plan/upload", token))}">Cancel</a></div>'
        "</form></div>"
    )
    return _page("Replace existing plan?", body, token)


_STATUS_PHRASE = {"active": "now the active plan", "future": "stored as a future plan",
                  "archived": "stored as an archived plan"}


def _plan_href(row: dict, token: str | None) -> str:
    """The viewer for a plan: the bare /training-plan for the active one."""
    return _url("/training-plan", token, plan=None if row["status"] == "active" else row["id"])


def render_upload_done_html(row: dict, warnings: list[str], token: str | None) -> str:
    """Only shown when an upload succeeded with warnings (otherwise: redirect)."""
    body = (
        "<h1>Plan uploaded</h1>"
        f'<div class="meta"><code>{_e(row["id"])}</code> is {_STATUS_PHRASE[row["status"]]} (version {row["version"]}).</div>'
        f'<div class="card"><div class="title">{len(warnings)} warning(s)</div><ul class="warn">'
        + "".join(f"<li>{_e(w)}</li>" for w in warnings)
        + "</ul></div>"
        f'<div class="links"><a href="{_e(_plan_href(row, token))}">View plan</a></div>'
    )
    return _page("Plan uploaded", body, token)


def render_plans_html(plans: list[dict], token: str | None, error: str | None = None,
                      notes: list[dict] | None = None, today: str | None = None) -> str:
    """Every stored plan with its actions, and any overlap or gap between one
    plan and the next (shown on the later plan)."""
    cards = []
    for p in plans:
        status = p["status"]
        dates = " – ".join(d for d in (p.get("start_date"), p.get("end_date")) if d)
        lines = [f'<div class="warn">{_e(plan_service.note_text(n))}</div>'
                 for n in notes or [] if n["second"]["id"] == p["id"]]
        if status == "future" and today and str(p.get("start_date") or "") > today:
            lines.insert(0, f'<div class="sub ok">Becomes the active plan automatically on {_e(p["start_date"])}.</div>')
        actions = [
            f'<a class="btn btn-primary" href="{_e(_plan_href(p, token))}">'
            f'{"View (read-only)" if status == "archived" else "View"}</a>',
            f'<a class="btn" data-pdf href="{_e(_url("/training-plan/pdf", token, plan=p["id"]))}">Download PDF</a>',
            f'<a class="btn" href="{_e(_url("/training-plan/export.json", token, plan=p["id"]))}">Download JSON</a>',
        ]

        def post(action, label, cls="", confirm=None):
            onsubmit = f' onsubmit="return confirm({_e(json.dumps(confirm))})"' if confirm else ""
            action_url = _url(f"/training-plan/plans/{p['id']}/{action}", token)
            return (
                f'<form class="inline" method="post" action="{_e(action_url)}"{onsubmit}>'
                f'<button type="submit" class="{cls}">{label}</button></form>'
            )

        if status == "active":
            actions.append(post("future", "Move to future", confirm="Move this plan to future plans? No plan will be active."))
            actions.append(post("archive", "Archive", confirm="Archive this plan? No plan will be active."))
        else:
            actions.append(post("activate", "Make active"))
            if status == "archived":
                actions.append(post("future", "Move to future"))
            else:
                actions.append(post("archive", "Archive"))
            actions.append(post("delete", "Delete", "btn-danger",
                                confirm="Permanently delete this plan, its history and its completion ticks?"))
        cards.append(
            '<div class="card"><div class="row"><div>'
            f'<div class="title">{_e(p.get("event") or p["id"])}</div>'
            f'<div class="sub">{_e(p.get("athlete") or "")}{" · " if p.get("athlete") and dates else ""}{_e(dates)}</div>'
            f'<div class="sub"><code>{_e(p["id"])}</code> · version {p["version"]} · updated {_e(str(p["updated_at"])[:10])}</div>'
            f'</div><span class="tag{"" if status == "active" else " " + status}">{_e(status)}</span></div>'
            + "".join(lines)
            + f'<div class="actions">{"".join(actions)}</div></div>'
        )
    error_html = f'<p class="error">{_e(error)}</p>' if error else ""
    body = (
        "<h1>Training plans</h1>"
        '<div class="meta">One plan is active at a time. Future plans are planned ahead and stay '
        "editable (by you or the coach) and become active on their start date; archived plans stay "
        "viewable, read-only.</div>"
        f"{error_html}"
        + ("".join(cards) or '<div class="card">No plans stored yet.</div>')
        + f'<div class="links"><a href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a>'
        f'<a href="{_e(_url("/training-plan/season", token))}">Season view</a>'
        f'<a href="{_e(_url("/training-plan", token))}">Active plan</a></div>'
    )
    return inject_pdf_links(_page("Training plans", body, token, active="settings"))


# ── SEASON ────────────────────────────────────────────────────────────────────
# Every plan on one horizontal timeline, a row per plan: weekly columns of
# planned hours (with the completed part filled in), the phase band, race
# markers, month gridlines and a today line. One week is a fixed width, so a
# long season scrolls sideways inside its frame (it opens at today). A table
# under the chart carries the same numbers.

# The viewer's phase colours (tools/assets/plan-viewer.html PHASE_COLORS).
PHASE_COLORS = {
    "Prep": "#6c93b0", "Base 1": "#5ea0b8", "Base 2": "#5eb0a3", "Base 3": "#7fb87a", "Base": "#5eb0a3",
    "Build 1": "#b8b05e", "Build 2": "#d9a05a", "Build": "#b8b05e", "Peak": "#d97a5e", "Taper": "#9184d9",
    "Race": "#d9506a",
}
_PHASE_DEFAULT = "#7c8194"

_SEASON_STYLE = """
main { max-width:1180px; }
.season-head { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; flex-wrap:wrap; }
.view-switch { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; font-size:.85rem; }
.view-switch a, .view-switch span { padding:.4rem .85rem; color:var(--fg); text-decoration:none; }
.view-switch a + a, .view-switch a + span { border-left:1px solid var(--line); }
.view-switch a:hover { background:color-mix(in srgb, var(--fg) 7%, transparent); }
.view-switch span { color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); }
@media (max-width: 899px) { .view-switch .month-only { display:none; } }
.legend { display:flex; gap:1rem; flex-wrap:wrap; font-size:.8rem; color:var(--muted); margin:0 0 .6rem; }
.legend span { display:inline-flex; align-items:center; gap:.4rem; }
.legend i { display:inline-block; width:12px; height:12px; border-radius:3px; }
.season { position:relative; overflow-x:auto; background:var(--surface); box-shadow:0 0 0 1px var(--line);
  border-radius:10px; -webkit-overflow-scrolling:touch; }
.season svg { display:block; }
.season text { font-family:inherit; }
.s-grid { stroke:var(--line); stroke-width:1; }
.s-axis { fill:var(--muted); font-size:11px; }
.s-name { fill:var(--fg); font-size:13px; font-weight:500; }
.s-sub { fill:var(--muted); font-size:11px; }
.s-phase { fill:var(--fg); font-size:10.5px; }
.s-plan { fill:color-mix(in srgb, var(--accent) 34%, var(--surface)); }
.s-done { fill:var(--accent); }
.s-today { stroke:var(--accent); stroke-width:1.5; }
.s-today-l { fill:var(--accent); font-size:11px; font-weight:600; }
.s-race { stroke:var(--warn); stroke-width:1; }
.s-race-m { fill:var(--warn); stroke:var(--surface); stroke-width:2; }
.s-race-l { fill:var(--fg); font-size:11px; }
.s-row.archived { opacity:.55; }
.s-wk { cursor:pointer; }
.s-wk rect.hit { fill:transparent; }
.s-wk:hover rect.hit, .s-wk:focus rect.hit { fill:color-mix(in srgb, var(--fg) 7%, transparent); }
.s-wk:focus { outline:none; }
.tip { position:absolute; pointer-events:none; z-index:2; background:var(--bg); color:var(--fg);
  box-shadow:0 0 0 1px var(--line), 0 6px 18px rgba(0,0,0,.25); border-radius:8px; padding:.45rem .6rem;
  font-size:.78rem; line-height:1.45; white-space:nowrap; display:none; }
.tip b { font-weight:600; }
table { width:100%; border-collapse:collapse; font-size:.84rem; }
th, td { text-align:left; padding:.45rem .5rem; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:500; font-size:.76rem; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.table-wrap { overflow-x:auto; }
table a { color:var(--accent); }
"""

_SEASON_SCRIPT = """
(function () {
  var box = document.querySelector('.season'), tip = document.querySelector('.season .tip');
  if (!box) return;
  var today = box.querySelector('[data-today-x]');
  if (today) box.scrollLeft = Math.max(0, +today.getAttribute('data-today-x') - box.clientWidth / 3);
  function show(el) {
    tip.innerHTML = el.getAttribute('data-tip');
    tip.style.display = 'block';
    var r = el.getBoundingClientRect(), b = box.getBoundingClientRect();
    var x = r.left - b.left + box.scrollLeft + r.width / 2 - tip.offsetWidth / 2;
    x = Math.max(box.scrollLeft + 4, Math.min(x, box.scrollLeft + box.clientWidth - tip.offsetWidth - 4));
    tip.style.left = x + 'px';
    tip.style.top = Math.max(4, r.top - b.top - tip.offsetHeight - 6) + 'px';
  }
  function hide() { tip.style.display = 'none'; }
  box.querySelectorAll('[data-tip]').forEach(function (el) {
    el.addEventListener('mouseenter', function () { show(el); });
    el.addEventListener('focus', function () { show(el); });
    el.addEventListener('mouseleave', hide);
    el.addEventListener('blur', hide);
  });
  box.addEventListener('scroll', hide, { passive: true });
})();
"""


def _hours(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m" if h and m else f"{h}h" if h else f"{m}m"


def _day(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _md(d: date, year: bool = False) -> str:
    return f"{d:%b} {d.day}" + (f", {d.year}" if year else "")


def render_season_html(data: dict, token: str | None) -> str:
    plans = data["plans"]
    if not plans:
        body = ("<h1>Season</h1>"
                '<div class="card">No plans yet — upload one to see it on the season timeline.</div>'
                f'<div class="links"><a href="{_e(_url("/training-plan/upload", token))}">Upload a plan</a></div>')
        return _page("Season", body, token)

    today = _day(data["today"])
    first = min(_day(p["start"]) for p in plans)
    last = max(_day(p["end"]) for p in plans)
    origin = first - timedelta(days=first.weekday())        # the Monday on or before
    span_end = max(last, today) if today >= origin else last
    weeks_n = (span_end - origin).days // 7 + 1
    WEEK, BAR, PAD = 22, 14, 12                             # px per week, column width, side padding
    width = PAD * 2 + weeks_n * WEEK
    AXIS, ROW = 30, 146                                     # axis height, row height
    RACE_Y, BARS_TOP, BARS_H = 44, 52, 56                   # within a row: race labels, then columns
    BAND_Y, BAND_H = BARS_TOP + BARS_H + 8, 8
    height = AXIS + ROW * len(plans) + 6
    peak = max((w["plannedMin"] for p in plans for w in p["weeks"]), default=0) or 60

    def x_of(d: date) -> float:
        return PAD + (d - origin).days / 7 * WEEK

    svg = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="Season timeline of {len(plans)} plan(s); the table below has the same numbers">']
    # month gridlines and labels
    m = date(origin.year, origin.month, 1)
    while m <= span_end:
        if m >= origin:
            x = x_of(m)
            svg.append(f'<line class="s-grid" x1="{x:.1f}" x2="{x:.1f}" y1="{AXIS - 8}" y2="{height}"/>')
        label_x = max(x_of(m), PAD) + 4
        if label_x < width - 30:
            svg.append(f'<text class="s-axis" x="{label_x:.1f}" y="16">{_e(m.strftime("%b" if m.month != 1 else "%b %Y"))}</text>')
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)

    for i, p in enumerate(plans):
        y = AXIS + i * ROW
        status = p["status"]
        px = x_of(_day(p["start"]))
        svg.append(f'<g class="s-row {status}">')
        sub = f'{status} · {_md(_day(p["start"]))} – {_md(_day(p["end"]), True)}'
        svg.append(f'<text class="s-name" x="{px:.1f}" y="{y + 16}">{_e(p["event"])}</text>')
        svg.append(f'<text class="s-sub" x="{px:.1f}" y="{y + 29}">{_e(sub)}</text>')
        base = y + BARS_TOP + BARS_H
        for w in p["weeks"]:
            wx = x_of(_day(w["start"]))
            ph = BARS_H * w["plannedMin"] / peak
            dh = BARS_H * min(w["doneMin"], w["plannedMin"]) / peak
            href = _url("/training-plan", token, plan=None if status == "active" else p["id"], week=w["weekNumber"])
            tip = (f'<b>{_e(p["event"])} · Week {w["weekNumber"]}</b><br>'
                   f'{_md(_day(w["start"]))} – {_md(_day(w["end"]))}'
                   f'{" · " + _e(w["phase"]) if w["phase"] else ""}{" · recovery" if w["recovery"] else ""}<br>'
                   f'{_hours(w["plannedMin"])} planned · {_hours(w["doneMin"])} done')
            cx = wx + (WEEK - BAR) / 2
            svg.append(f'<a class="s-wk" href="{_e(href)}" data-tip="{_e(tip)}" tabindex="0" '
                       f'aria-label="Week {w["weekNumber"]}: {_hours(w["plannedMin"])} planned, {_hours(w["doneMin"])} done">')
            svg.append(f'<rect class="hit" x="{wx:.1f}" y="{y + BARS_TOP - 4}" width="{WEEK}" height="{BARS_H + BAND_H + 16}" rx="4"/>')
            for cls, h in (("s-plan", ph), ("s-done", dh)):
                if h >= 1:
                    r = min(4, h)   # rounded data end, square at the baseline
                    svg.append(f'<path class="{cls}" d="M{cx:.1f},{base:.1f}V{base - h + r:.1f}'
                               f'q0,-{r} {r},-{r}h{BAR - 2 * r}q{r},0 {r},{r}V{base:.1f}Z"/>')
            svg.append("</a>")
        # phase band: a 2px surface gap between segments, the name under it when it fits
        for ph in p["phases"]:
            sx, ex = x_of(_day(ph["start"])), x_of(_day(ph["end"]) + timedelta(days=1))
            color = PHASE_COLORS.get(ph["name"], _PHASE_DEFAULT)
            svg.append(f'<rect x="{sx + 1:.1f}" y="{y + BAND_Y}" width="{max(ex - sx - 2, 1):.1f}" height="{BAND_H}" rx="3" fill="{color}"/>')
            if ph["name"] and len(ph["name"]) * 6.2 + 6 <= ex - sx:
                svg.append(f'<text class="s-phase" x="{sx + 3:.1f}" y="{y + BAND_Y + BAND_H + 13}">{_e(ph["name"])}</text>')
        for race in p["races"]:
            rx = x_of(_day(race["date"])) + WEEK / 14
            svg.append(f'<line class="s-race" x1="{rx:.1f}" x2="{rx:.1f}" y1="{y + RACE_Y}" y2="{y + BAND_Y + BAND_H}"/>')
            svg.append(f'<path class="s-race-m" d="M{rx:.1f},{y + RACE_Y - 5}l5,5l-5,5l-5,-5Z"/>')
            # Flip the label to the marker's left when it would run off the right edge.
            fits = rx + 8 + len(race["name"]) * 6.4 <= width - 4
            anchor = "" if fits else ' text-anchor="end"'
            svg.append(f'<text class="s-race-l" x="{rx + 8 if fits else rx - 8:.1f}" y="{y + RACE_Y + 4}"{anchor}>'
                       f'{_e(race["name"])}</text>')
        svg.append("</g>")

    if origin <= today <= span_end:
        tx = x_of(today) + WEEK / 14
        svg.append(f'<line class="s-today" data-today-x="{tx:.0f}" x1="{tx:.1f}" x2="{tx:.1f}" y1="{AXIS - 8}" y2="{height}"/>')
        svg.append(f'<text class="s-today-l" x="{tx + 4:.1f}" y="{AXIS - 1}">Today</text>')
    svg.append("</svg>")

    rows = []
    for p in plans:
        planned = sum(w["plannedMin"] for w in p["weeks"])
        done = sum(w["doneMin"] for w in p["weeks"])
        href = _url("/training-plan", token, plan=None if p["status"] == "active" else p["id"])
        rows.append(
            f'<tr><td><a href="{_e(href)}">{_e(p["event"])}</a></td><td>{_e(p["status"])}</td>'
            f'<td>{_e(p["start"])} – {_e(p["end"])}</td><td class="num">{len(p["weeks"])}</td>'
            f'<td class="num">{_hours(planned)}</td><td class="num">{_hours(done)}</td>'
            f'<td>{_e(", ".join(ph["name"] for ph in p["phases"] if ph["name"]))}</td>'
            f'<td>{_e("; ".join(r["date"] + " " + r["name"] for r in p["races"]))}</td></tr>'
        )
    body = (
        f"<style>{_SEASON_STYLE}</style>"
        '<div class="season-head"><h1>Season</h1>'
        '<nav class="view-switch" aria-label="Plan view">'
        f'<a href="{_e(_url("/training-plan", token, mode="week"))}">Week</a>'
        f'<a class="month-only" href="{_e(_url("/training-plan", token, mode="month"))}">Month</a>'
        '<span aria-current="page">Season</span></nav></div>'
        '<div class="meta">Every active and future plan, and archived ones from the last year. '
        "Columns are each week's planned hours, filled in as workouts are done; a column opens that week.</div>"
        '<div class="legend">'
        '<span><i style="background:color-mix(in srgb, var(--accent) 34%, var(--surface))"></i>Planned</span>'
        '<span><i style="background:var(--accent)"></i>Done</span>'
        '<span><i style="background:var(--warn); transform:rotate(45deg) scale(.75)"></i>Race</span>'
        '<span><i style="background:var(--accent); width:2px"></i>Today</span>'
        "</div>"
        f'<div class="season">{"".join(svg)}<div class="tip" role="tooltip"></div></div>'
        "<h2>Plans</h2>"
        '<div class="table-wrap"><table><thead><tr><th>Plan</th><th>Status</th><th>Dates</th>'
        '<th class="num">Weeks</th><th class="num">Planned</th><th class="num">Done</th><th>Phases</th><th>Races</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        f'<div class="links"><a href="{_e(_url("/training-plan", token))}">Active plan</a>'
        f'<a href="{_e(_url("/training-plan/plans", token))}">All plans</a></div>'
        f"<script>{_SEASON_SCRIPT}</script>"
        # Week / Month: start loading the plan page when the pointer rests on them.
        '<script type="speculationrules">'
        '{"prerender":[{"where":{"selector_matches":".view-switch a"},"eagerness":"moderate"}]}'
        "</script>"
    )
    return _page("Season", body, token)


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
    plan_id = _plan_param(request)

    def build():
        # The goal race and time zone don't depend on the plan: read them while it loads.
        with ThreadPoolExecutor(max_workers=2) as pool:
            goal, tz = pool.submit(_goal_race_payload), pool.submit(_timezone_payload)
            row = plan_service.get_plan(plan_id)
            return row, (render_viewer_html(row, token, goal, tz) if row is not None else None)

    try:
        # The reads block on the database: keep them off the event loop so one
        # slow page open doesn't hold up every other request.
        row, page = await run_in_threadpool(build)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    if row is None:
        if plan_id:
            return HTMLResponse(render_message_html("Plan not found", "No plan with that id.", token),
                                status_code=404, headers=_NO_STORE)
        future = [p for p in await run_in_threadpool(plan_service.list_plans) if p["status"] == "future"]
        return HTMLResponse(render_no_plan_html(token, future), headers=_NO_STORE)
    return HTMLResponse(page, headers=_NO_STORE)


async def serve_plans(request):
    """GET /training-plan/plans — every stored plan."""
    def build():
        plan_service.advance_plans()
        plans = plan_service.list_plans()
        return plans, plan_service.timeline_notes(plans)

    try:
        plans, notes = await run_in_threadpool(build)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    return HTMLResponse(render_plans_html(plans, _token(request), request.query_params.get("error"),
                                          notes, local_time.local_today().isoformat()),
                        headers=_NO_STORE)


async def serve_season(request):
    """GET /training-plan/season — every plan on one timeline."""
    try:
        data = await run_in_threadpool(plan_service.season)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    return HTMLResponse(render_season_html(data, _token(request)), headers=_NO_STORE)


async def handle_plan_action(request):
    """POST /training-plan/plans/{id}/{action} — activate | future | archive | delete."""
    token = _token(request)
    plan_id, action = request.path_params["plan_id"], request.path_params["action"]
    try:
        if action == "activate":
            plan_service.set_status(plan_id, "active")
        elif action == "future":
            plan_service.set_status(plan_id, "future")
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
        activate = form.get(ACTIVATE_FIELD) == "1"
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
            return HTMLResponse(render_confirm_replace_html(plan, preview, warnings, token, activate),
                                headers=_NO_STORE)
        row = plan_service.save_upload(plan, activate)
        warnings = warnings + plan_service.timeline_warnings(row["id"])
    except PlanError as e:
        return form_error(str(e))
    except PlanStorageUnavailable as e:
        return form_error(str(e), 503)

    if warnings:
        return HTMLResponse(render_upload_done_html(row, warnings, token), headers=_NO_STORE)
    return RedirectResponse(_plan_href(row, token), status_code=303)


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


async def serve_calendar(request):
    """GET /training-plan/calendar.ics — the active plan for calendar apps.
    Its own read-only key, since calendar servers (Google, Outlook) fetch and
    store the URL; the bearer token never goes in it."""
    try:
        if not plan_calendar.key_is_valid(request.query_params.get("key")):
            return Response("Unauthorized", status_code=401)
        body = plan_calendar.feed()
    except PlanStorageUnavailable as e:
        return Response(str(e), status_code=503)
    return Response(body, media_type="text/calendar; charset=utf-8", headers={
        "Cache-Control": "no-cache",
        "Content-Disposition": 'inline; filename="training-plan.ics"',
    })


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
        completed = bool(body.get("completed", True))
        plan_service.set_completion(_plan_param(request), workout_id, completed)
        if completed:
            # Ticked by hand: attach the Garmin activity that did it, if synced.
            plan_service.link_completed_workouts(_plan_param(request), [workout_id])
        row = plan_service.require_plan(_plan_param(request))
    except (PlanStorageUnavailable, LookupError, PlanError) as e:
        return _api_error(e)
    return _plan_response(row)


async def api_carryover(request):
    """POST /training-plan/api/carryover — {keys}: the thresholds to copy over."""
    try:
        body = await _json_body(request)
        keys = body.get("keys")
        if not isinstance(keys, list):
            raise PlanError("keys must be a list of thresholds.")
        row = plan_service.apply_carryover(keys, "web")
    except (PlanStorageUnavailable, LookupError, PlanError) as e:
        return _api_error(e)
    return _plan_response(row)


async def api_carryover_dismiss(request):
    try:
        plan_service.dismiss_carryover()
        row = plan_service.require_plan(None)
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


async def api_calendar(request):
    try:
        return JSONResponse(plan_calendar.get_settings(), headers=_NO_STORE)
    except PlanStorageUnavailable as e:
        return _api_error(e)


async def api_calendar_action(request):
    action = request.path_params["action"]
    try:
        if action == "schedule":
            body = await _json_body(request)
            result = plan_calendar.save_schedule(body.get("defaultTime"), body.get("slots"))
        elif action == "publish":
            result = plan_calendar.publish()
        elif action == "revoke":
            result = plan_calendar.revoke()
        else:
            return JSONResponse({"error": "Unknown action."}, status_code=404)
    except (PlanStorageUnavailable, PlanError) as e:
        return _api_error(e)
    return JSONResponse(result, headers=_NO_STORE)


def _goal_race_payload() -> dict | None:
    """The Settings goal-race editor's data, or None when it can't be read
    (the viewer then shows the section as unavailable, not a broken page)."""
    try:
        return goal_race.settings_payload()
    except Exception:  # noqa: BLE001 — the goal race is optional on this page
        logger.exception("Goal race unavailable")
        return None


async def api_get_goal_race(request):
    payload = _goal_race_payload()
    if payload is None:
        return JSONResponse({"error": "Goal race storage is unavailable."}, status_code=503)
    return JSONResponse(payload, headers=_NO_STORE)


def _goal_race_storage_error() -> JSONResponse | None:
    if not db.is_configured():
        return JSONResponse({"error": "The goal race is stored in PostgreSQL — set DATABASE_URL."}, status_code=503)
    return None


async def api_save_goal_race(request):
    if (err := _goal_race_storage_error()) is not None:
        return err
    try:
        body = await _json_body(request)
        goal_race.save_goal_race(body)
    except (PlanError, goal_race.GoalRaceError) as e:
        return _api_error(e)
    return await api_get_goal_race(request)


async def api_clear_goal_race(request):
    if (err := _goal_race_storage_error()) is not None:
        return err
    goal_race.clear_goal_race()
    return await api_get_goal_race(request)


def _timezone_payload() -> dict | None:
    """The Settings time-zone picker's data, or None when it can't be read."""
    try:
        return local_time.settings_payload()
    except Exception:  # noqa: BLE001 — optional on this page, like the goal race
        logger.exception("Time zone settings unavailable")
        return None


async def api_get_timezone(request):
    payload = _timezone_payload()
    if payload is None:
        return JSONResponse({"error": "Time zone settings are unavailable."}, status_code=503)
    return JSONResponse(payload, headers=_NO_STORE)


async def api_save_timezone(request):
    if not db.is_configured():
        return JSONResponse({"error": "The time zone is stored in PostgreSQL — set DATABASE_URL, "
                                      "or set LOCAL_TIMEZONE on the server."}, status_code=503)
    try:
        body = await _json_body(request)
        if "timezone" not in body:
            raise local_time.TimezoneError("Send {\"timezone\": \"Area/City\"} (or null to reset).")
        local_time.save_zone(body["timezone"])
    except (PlanError, local_time.TimezoneError) as e:
        return _api_error(e)
    return await api_get_timezone(request)


ROUTES = [
    Route("/training-plan", serve_plan, methods=["GET"]),
    Route("/training-plan/plans", serve_plans, methods=["GET"]),
    Route("/training-plan/season", serve_season, methods=["GET"]),
    Route("/training-plan/plans/{plan_id}/{action}", handle_plan_action, methods=["POST"]),
    Route("/training-plan/upload", serve_upload_form, methods=["GET"]),
    Route("/training-plan/upload", handle_upload, methods=["POST"]),
    Route("/training-plan/export.json", serve_export, methods=["GET"]),
    Route("/training-plan/pdf", serve_plan_pdf, methods=["GET"]),
    Route(plan_calendar.FEED_PATH, serve_calendar, methods=["GET"]),
    Route("/training-plan/api/plan", api_get_plan, methods=["GET"]),
    Route("/training-plan/api/operations", api_operations, methods=["POST"]),
    Route("/training-plan/api/completion", api_completion, methods=["POST"]),
    Route("/training-plan/api/carryover", api_carryover, methods=["POST"]),
    Route("/training-plan/api/carryover/dismiss", api_carryover_dismiss, methods=["POST"]),
    Route("/training-plan/api/revisions", api_revisions, methods=["GET"]),
    Route("/training-plan/api/revisions/{version}/restore", api_restore, methods=["POST"]),
    Route("/training-plan/api/calendar", api_calendar, methods=["GET"]),
    Route("/training-plan/api/calendar/{action}", api_calendar_action, methods=["POST"]),
    Route("/training-plan/api/goal-race", api_get_goal_race, methods=["GET"]),
    Route("/training-plan/api/goal-race", api_save_goal_race, methods=["POST"]),
    Route("/training-plan/api/goal-race/clear", api_clear_goal_race, methods=["POST"]),
    Route("/training-plan/api/timezone", api_get_timezone, methods=["GET"]),
    Route("/training-plan/api/timezone", api_save_timezone, methods=["POST"]),
]

PATH_PREFIX = "/training-plan"


def owns_path(path: str) -> bool:
    """Whether a request path belongs to this sub-app (used for dispatch)."""
    return path == PATH_PREFIX or path.startswith(f"{PATH_PREFIX}/")


def create_app() -> Starlette:
    """The training-plan sub-app, mounted by server.py behind token auth."""
    return Starlette(routes=ROUTES)
