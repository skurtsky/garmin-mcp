# tools/seasons_page.py
"""The Seasons settings pages: create, edit and archive a season; set its dates;
link and unlink plans; add, edit and delete its races; edit its goals and
objectives. Server-rendered forms in the style of the Plans and Upload pages,
so they work with no plan active.

    GET  /training-plan/seasons                               — every season, and a form for a new one
    POST /training-plan/seasons                               — create → the new season's page
    GET  /training-plan/seasons/{id}/edit                     — one season's settings
    POST /training-plan/seasons/{id}/{action}                 — details | archive | restore |
                                                                link | unlink | goals | objectives | races
    POST /training-plan/seasons/{id}/races/{race_id}/{action} — save | delete

Every POST answers 303 back to the page, with ``notice`` or ``error`` in the
query. The soft Friel / date warnings are shown, never enforced.
"""
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from tools import season_doc, season_service
from tools.plan_service import PlanStorageUnavailable
from tools.season_doc import SeasonError
from tools.training_plan import _NO_STORE, _e, _page, _token, _unavailable, _url

_STYLE = """
<style>
main { max-width:860px; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); gap:.7rem; }
.grid2 .wide { grid-column:1 / -1; }
.field { margin:0; }
.field label { margin:0 0 .25rem; }
input[type=text], input[type=date], input[type=url], select, textarea {
  width:100%; font:inherit; font-size:.9rem; padding:.4rem .55rem; border-radius:8px;
  border:1px solid var(--line); background:var(--bg); color:var(--fg);
}
textarea { min-height:3.6rem; resize:vertical; }
.notice { color:var(--ok); font-size:.9rem; margin:0 0 .9rem; }
.warnings { margin:0 0 .9rem; padding:.7rem 1rem .7rem 1.6rem; border-radius:10px; box-shadow:0 0 0 1px var(--warn); color:var(--warn); font-size:.88rem; }
.rows { display:flex; flex-direction:column; gap:.55rem; margin:.2rem 0 .3rem; }
.row-grid { display:grid; grid-template-columns:minmax(0,3fr) minmax(0,1.3fr) minmax(0,1.6fr); gap:.5rem; }
.row-grid.obj { grid-template-columns:minmax(0,3fr) minmax(0,1.4fr) minmax(0,1.6fr) minmax(0,1.5fr); }
@media (max-width:640px) { .row-grid, .row-grid.obj { grid-template-columns:1fr 1fr; } .row-grid > :first-child { grid-column:1 / -1; } }
details.race { border-top:1px solid var(--line); padding:.6rem 0; }
details.race:first-of-type { border-top:0; }
details.race > summary { cursor:pointer; list-style:none; display:flex; gap:.6rem; align-items:baseline; flex-wrap:wrap; }
details.race > summary::-webkit-details-marker { display:none; }
.prio { display:inline-block; min-width:1.5rem; text-align:center; font-weight:600; border-radius:6px; padding:0 .3rem; }
.prio.A { background:var(--accent); color:var(--bg); }
.prio.B { box-shadow:inset 0 0 0 2px var(--accent); color:var(--accent); }
.prio.C { box-shadow:inset 0 0 0 1px var(--muted); color:var(--muted); }
.card h2 { margin-top:0; }
table { width:100%; border-collapse:collapse; font-size:.86rem; margin:.5rem 0; }
th, td { text-align:left; padding:.45rem .5rem; border-bottom:1px solid var(--line); vertical-align:middle; }
th { color:var(--muted); font-weight:500; font-size:.76rem; }
.table-wrap { overflow-x:auto; }
</style>
"""


def _redirect(token, path: str, **params) -> RedirectResponse:
    return RedirectResponse(_url(path, token, **params), status_code=303)


def _date_span(season: dict) -> str:
    return f"{season['startDate']} – {season['endDate']}"


def _options(items, selected=None, blank: str | None = None) -> str:
    out = [f'<option value="">{_e(blank)}</option>'] if blank is not None else []
    for value, label in items:
        out.append(f'<option value="{_e(value)}"{" selected" if value == selected else ""}>{_e(label)}</option>')
    return "".join(out)


def _messages(notice, error, warnings=()) -> str:
    return (
        (f'<p class="notice">{_e(notice)}</p>' if notice else "")
        + (f'<p class="error">{_e(error)}</p>' if error else "")
        + ('<ul class="warnings"><li>' + "</li><li>".join(_e(w) for w in warnings) + "</li></ul>" if warnings else "")
    )


# ── LIST ──────────────────────────────────────────────────────────────────────

def render_seasons_html(seasons: list[dict], token: str | None, notice=None, error=None) -> str:
    def card(s):
        archived = s["status"] == "archived"
        action = "restore" if archived else "archive"
        view_url = _e(_url(f"/training-plan/seasons/{s['id']}", token))
        edit_url = _e(_url(f"/training-plan/seasons/{s['id']}/edit", token))
        action_url = _e(_url(f"/training-plan/seasons/{s['id']}/{action}", token))
        warn = f' · <span class="warn">{len(s["warnings"])} warning(s)</span>' if s["warnings"] else ""
        return (
            f'<div class="card"><div class="row"><div>'
            f'<div class="title"><a href="{view_url}">{_e(s["name"])}</a></div>'
            f'<div class="sub">{_e(_date_span(s))} · {len(s["planIds"])} plan(s) · {len(s["races"])} race(s){warn}</div>'
            f'</div><span class="tag{" archived" if archived else ""}">{_e(s["status"])}</span></div>'
            f'<div class="actions"><a class="btn" href="{view_url}">View</a><a class="btn" href="{edit_url}">Edit</a>'
            f'<form class="inline" method="post" action="{action_url}">'
            f'<button type="submit">{"Restore" if archived else "Archive"}</button></form></div></div>'
        )

    active = [s for s in seasons if s["status"] == "active"]
    archived = [s for s in seasons if s["status"] == "archived"]
    body = (
        _STYLE + "<h1>Seasons</h1>"
        '<div class="meta">A season groups the plans, races, goals and objectives of one training year. '
        "Friel’s limits (3 A races, 5 B races, 3 goals, 4 objectives) are shown as warnings, never enforced.</div>"
        + _messages(notice, error)
        + ("".join(card(s) for s in active) or '<div class="card">No seasons yet — create the first below.</div>')
        + ("<h2>Archived</h2>" + "".join(card(s) for s in archived) if archived else "")
        + '<div class="card"><h2>New season</h2>'
        f'<form method="post" action="{_e(_url("/training-plan/seasons", token))}"><div class="grid2">'
        '<div class="field wide"><label for="n-name">Name</label>'
        '<input id="n-name" type="text" name="name" maxlength="120" required placeholder="Season 2026–27"></div>'
        '<div class="field"><label for="n-start">Starts</label><input id="n-start" type="date" name="startDate" required></div>'
        '<div class="field"><label for="n-end">Ends</label><input id="n-end" type="date" name="endDate" required></div>'
        '</div><div class="actions"><button type="submit" class="btn-primary">Create season</button></div></form></div>'
        f'<div class="links"><a href="{_e(_url("/training-plan/plans", token))}">All plans</a>'
        f'<a href="{_e(_url("/training-plan", token))}">Active plan</a></div>'
    )
    return _page("Seasons", body, token, active="settings")


# ── ONE SEASON ────────────────────────────────────────────────────────────────

def _race_form(season: dict, race: dict | None, token) -> str:
    """The editor for one race (race=None: the blank 'add a race' form)."""
    r = race or {"name": "", "date": "", "priority": "B", "type": "other", "goal": "", "location": "", "url": "", "notes": ""}
    pre = f"r{r['id']}-" if race else "new-"
    path = (f"/training-plan/seasons/{season['id']}/races/{r['id']}/save" if race
            else f"/training-plan/seasons/{season['id']}/races")
    types = list(season_doc.RACE_TYPES.items())
    delete_url = _e(_url(f"/training-plan/seasons/{season['id']}/races/{r['id']}/delete", token)) if race else ""
    return (
        f'<form method="post" action="{_e(_url(path, token))}"><div class="grid2">'
        f'<div class="field wide"><label for="{pre}name">Name</label>'
        f'<input id="{pre}name" type="text" name="name" maxlength="120" required value="{_e(r["name"])}"></div>'
        f'<div class="field"><label for="{pre}date">Date</label>'
        f'<input id="{pre}date" type="date" name="date" required value="{_e(r["date"])}"></div>'
        f'<div class="field"><label for="{pre}priority">Priority</label><select id="{pre}priority" name="priority">'
        + _options([(p, p) for p in season_doc.PRIORITIES], r["priority"]) + "</select></div>"
        f'<div class="field"><label for="{pre}type">Type</label><select id="{pre}type" name="type">'
        + _options(types, r["type"]) + "</select></div>"
        f'<div class="field"><label for="{pre}goal">Goal (target time or text)</label>'
        f'<input id="{pre}goal" type="text" name="goal" maxlength="300" value="{_e(r["goal"])}" placeholder="5:30:00"></div>'
        f'<div class="field"><label for="{pre}location">Location</label>'
        f'<input id="{pre}location" type="text" name="location" maxlength="120" value="{_e(r["location"])}"></div>'
        f'<div class="field"><label for="{pre}url">URL</label>'
        f'<input id="{pre}url" type="url" name="url" maxlength="500" value="{_e(r["url"])}"></div>'
        f'<div class="field wide"><label for="{pre}notes">Notes</label>'
        f'<textarea id="{pre}notes" name="notes" maxlength="2000">{_e(r["notes"])}</textarea></div>'
        '</div><div class="actions"><button type="submit" class="btn-primary">'
        + ("Save race" if race else "Add race") + "</button></div></form>"
        + (f'<form class="inline" method="post" action="{delete_url}" '
           'onsubmit="return confirm(\'Delete this race? Goals and objectives that point at it keep their text.\')">'
           '<div class="actions"><button type="submit" class="btn-danger">Delete race</button></div></form>' if race else "")
    )


def _goal_rows(season: dict, kind: str) -> str:
    """Rows for the goals or objectives form: the stored ones plus one blank."""
    items = season["goals" if kind == "goal" else "objectives"]
    races = [(r["id"], f"{r['name']} · {r['date']}") for r in season["races"]]
    rows = []
    for i, item in enumerate(items + [{}]):
        status = (f'<div class="field"><label>Status</label><select name="status">'
                  + _options([(k, v) for k, v in season_doc.OBJECTIVE_STATUS_LABELS.items()],
                             item.get("status", "not_started")) + "</select></div>") if kind == "objective" else ""
        rows.append(
            f'<div class="row-grid{" obj" if kind == "objective" else ""}">'
            f'<input type="hidden" name="id" value="{_e(item.get("id", ""))}">'
            f'<div class="field"><label>{"Goal" if kind == "goal" else "Objective"}'
            f'{" (new)" if not item else ""}</label>'
            f'<input type="text" name="text" maxlength="300" value="{_e(item.get("text", ""))}"></div>'
            f'<div class="field"><label>Target date</label><input type="date" name="targetDate" value="{_e(item.get("targetDate", ""))}"></div>'
            f'<div class="field"><label>Race</label><select name="raceId">'
            + _options(races, item.get("raceId"), blank="—") + "</select></div>"
            + status + "</div>"
        )
    return '<div class="rows">' + "".join(rows) + "</div>"


def render_season_edit_html(season: dict, all_plans: list[dict], token: str | None, notice=None, error=None) -> str:
    sid = season["id"]
    post = lambda action: _e(_url(f"/training-plan/seasons/{sid}/{action}", token))   # noqa: E731
    archived = season["status"] == "archived"

    plan_rows = "".join(
        f'<tr><td>{_e(p["event"])}<div class="sub"><code>{_e(p["id"])}</code></div></td><td>{_e(p["status"])}</td>'
        f'<td>{_e(p["start"] or "?")} – {_e(p["end"] or "?")}</td><td>'
        f'<form class="inline" method="post" action="{post("unlink")}"><input type="hidden" name="plan_id" value="{_e(p["id"])}">'
        '<button type="submit">Unlink</button></form></td></tr>'
        for p in season["plans"]
    )
    linkable = [(p["id"], p["event"] + (f" — in {p['season']}" if p.get("season") else ""))
                for p in all_plans if p["id"] not in season["planIds"]]
    link_form = (
        f'<form method="post" action="{post("link")}" class="actions"><select name="plan_id" aria-label="Plan to link" required>'
        + _options(linkable, blank="Link a plan…") + '</select><button type="submit" class="btn-primary">Link</button></form>'
        if linkable and not archived else ""
    )

    races = sorted(season["races"], key=lambda r: r["date"])
    race_items = "".join(
        f'<details class="race"><summary><span class="prio {r["priority"]}">{r["priority"]}</span>'
        f'<span class="title">{_e(r["name"])}</span><span class="sub">{_e(r["date"])} · {_e(season_doc.RACE_TYPES[r["type"]])}'
        f'{" · " + _e(r["goal"]) if r["goal"] else ""}</span></summary>{_race_form(season, r, token)}</details>'
        for r in races
    )

    body = (
        _STYLE
        + f'<h1><a href="{_e(_url("/training-plan/seasons/" + sid, token))}" title="Open the season timeline">{_e(season["name"])}</a></h1>'
        f'<div class="meta">{_e(_date_span(season))} · <span class="tag{" archived" if archived else ""}">{_e(season["status"])}</span></div>'
        + _messages(notice, error, season["warnings"])
        + '<div class="card"><h2>Details</h2>'
        f'<form method="post" action="{post("details")}"><div class="grid2">'
        f'<div class="field wide"><label for="s-name">Name</label><input id="s-name" type="text" name="name" maxlength="120" required value="{_e(season["name"])}"></div>'
        f'<div class="field"><label for="s-start">Starts</label><input id="s-start" type="date" name="startDate" required value="{_e(season["startDate"])}"></div>'
        f'<div class="field"><label for="s-end">Ends</label><input id="s-end" type="date" name="endDate" required value="{_e(season["endDate"])}"></div>'
        '</div><div class="actions"><button type="submit" class="btn-primary">Save details</button></form>'
        f'<form class="inline" method="post" action="{post("restore" if archived else "archive")}">'
        f'<button type="submit">{"Restore season" if archived else "Archive season"}</button></form></div></div>'
        '<div class="card"><h2>Plans</h2>'
        '<div class="sub">A plan belongs to at most one season; linking it here moves it out of another. '
        "Plans outside the season’s dates are allowed, with a warning.</div>"
        + (f'<div class="table-wrap"><table><thead><tr><th>Plan</th><th>Status</th><th>Dates</th><th></th></tr></thead>'
           f"<tbody>{plan_rows}</tbody></table></div>" if plan_rows else '<p class="sub">No plans linked yet.</p>')
        + link_form + "</div>"
        '<div class="card"><h2>Races</h2>'
        '<div class="sub">A race’s week in a plan is found by its date. The goal race of each plan is chosen in '
        "Settings → Training.</div>"
        + (race_items or '<p class="sub">No races yet.</p>')
        + f'<details class="race"{"" if races else " open"}><summary><span class="title">Add a race</span></summary>'
        + _race_form(season, None, token) + "</details></div>"
        '<div class="card"><h2>Goals</h2>'
        '<div class="sub">Outcomes for the year. Friel suggests up to three; leave a text blank to remove it.</div>'
        f'<form method="post" action="{post("goals")}">{_goal_rows(season, "goal")}'
        '<div class="actions"><button type="submit" class="btn-primary">Save goals</button></div></form></div>'
        '<div class="card"><h2>Objectives</h2>'
        '<div class="sub">Measurable steps toward the goals. Friel suggests up to four. You set the status by hand.</div>'
        f'<form method="post" action="{post("objectives")}">{_goal_rows(season, "objective")}'
        '<div class="actions"><button type="submit" class="btn-primary">Save objectives</button></div></form></div>'
        f'<div class="links"><a href="{_e(_url("/training-plan/seasons/" + sid, token))}">View season</a>'
        f'<a href="{_e(_url("/training-plan/seasons", token))}">All seasons</a>'
        f'<a href="{_e(_url("/training-plan/plans", token))}">All plans</a></div>'
    )
    return _page(f"Season · {season['name']}", body, token, active="settings")


# ── ROUTES ────────────────────────────────────────────────────────────────────

def _edit_path(season_id: str) -> str:
    return f"/training-plan/seasons/{season_id}/edit"


async def serve_seasons(request):
    """GET /training-plan/seasons."""
    try:
        seasons = await run_in_threadpool(season_service.list_seasons)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    q = request.query_params
    return HTMLResponse(render_seasons_html(seasons, _token(request), q.get("notice"), q.get("error")),
                        headers=_NO_STORE)


async def serve_season_edit(request):
    """GET /training-plan/seasons/{id}/edit."""
    season_id = request.path_params["season_id"]

    def build():
        from tools import plan_service
        season = season_service.get_season(season_id)
        owners = {pid: s["name"] for s in season_service.list_seasons() for pid in s["planIds"]}
        plans = [{**p, "season": owners.get(p["id"])} for p in
                 ({"id": p["id"], "event": p.get("event") or p["id"], "status": p["status"]}
                  for p in plan_service.list_plans())]
        return season, plans

    try:
        season, plans = await run_in_threadpool(build)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    except LookupError:
        return _redirect(_token(request), "/training-plan/seasons", error="No season with that id.")
    q = request.query_params
    return HTMLResponse(render_season_edit_html(season, plans, _token(request), q.get("notice"), q.get("error")),
                        headers=_NO_STORE)


def _rows(form, *fields) -> list[dict]:
    """A form's repeated rows ([name=…] x N) as dicts, by position."""
    columns = [form.getlist(f) for f in fields]
    return [dict(zip(fields, values)) for values in zip(*columns)]


async def handle_create(request):
    """POST /training-plan/seasons."""
    token = _token(request)
    async with request.form() as form:
        data = {k: form.get(k) for k in ("name", "startDate", "endDate")}
    try:
        season = await run_in_threadpool(season_service.create_season, data)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    except SeasonError as e:
        return _redirect(token, "/training-plan/seasons", error=str(e))
    return _redirect(token, _edit_path(season["id"]), notice="Season created.")


_STATUS_MESSAGES = {"archive": "Season archived.", "restore": "Season restored."}


async def handle_season_action(request):
    """POST /training-plan/seasons/{id}/{action}."""
    token = _token(request)
    season_id, action = request.path_params["season_id"], request.path_params["action"]
    async with request.form() as form:
        fields = {k: form.get(k) for k in ("name", "startDate", "endDate", "plan_id", "text")}
        goals = _rows(form, "id", "text", "targetDate", "raceId") if action == "goals" else None
        objectives = _rows(form, "id", "text", "targetDate", "raceId", "status") if action == "objectives" else None
        race = {k: form.get(k) for k in ("name", "date", "priority", "type", "goal", "location", "url", "notes")}

    def run():
        if action == "details":
            season_service.update_season(season_id, {k: fields[k] for k in ("name", "startDate", "endDate")})
            return "Details saved."
        if action in ("archive", "restore"):
            season_service.update_season(season_id, {"status": "archived" if action == "archive" else "active"})
            return _STATUS_MESSAGES[action]
        if action == "link":
            season_service.link_plan(season_id, fields["plan_id"] or "")
            return "Plan linked."
        if action == "unlink":
            season_service.unlink_plan(fields["plan_id"] or "")
            return "Plan unlinked."
        if action == "goals":
            season_service.set_goals(season_id, goals)
            return "Goals saved."
        if action == "objectives":
            season_service.set_objectives(season_id, objectives)
            return "Objectives saved."
        if action == "races":
            season_service.add_race(season_id, race)
            return "Race added."
        return None

    try:
        notice = await run_in_threadpool(run)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    except LookupError as e:
        return _redirect(token, _edit_path(season_id), error=str(e).strip("'\""))
    except SeasonError as e:
        return _redirect(token, _edit_path(season_id), error=str(e))
    if notice is None:
        return HTMLResponse("Unknown action.", status_code=404)
    return _redirect(token, _edit_path(season_id), notice=notice)


async def handle_race_action(request):
    """POST /training-plan/seasons/{id}/races/{race_id}/{action}."""
    token = _token(request)
    season_id, race_id, action = (request.path_params[k] for k in ("season_id", "race_id", "action"))
    async with request.form() as form:
        race = {k: form.get(k) for k in ("name", "date", "priority", "type", "goal", "location", "url", "notes")}
    if action not in ("save", "delete"):
        return HTMLResponse("Unknown action.", status_code=404)
    try:
        if action == "save":
            await run_in_threadpool(season_service.update_race, season_id, race_id, race)
        else:
            await run_in_threadpool(season_service.delete_race, season_id, race_id)
    except PlanStorageUnavailable as e:
        return _unavailable(request, e)
    except LookupError as e:
        return _redirect(token, _edit_path(season_id), error=str(e).strip("'\""))
    except SeasonError as e:
        return _redirect(token, _edit_path(season_id), error=str(e))
    return _redirect(token, _edit_path(season_id), notice="Race saved." if action == "save" else "Race deleted.")


ROUTES = [
    Route("/training-plan/seasons", serve_seasons, methods=["GET"]),
    Route("/training-plan/seasons", handle_create, methods=["POST"]),
    Route("/training-plan/seasons/{season_id}/edit", serve_season_edit, methods=["GET"]),
    Route("/training-plan/seasons/{season_id}/races/{race_id}/{action}", handle_race_action, methods=["POST"]),
    Route("/training-plan/seasons/{season_id}/{action}", handle_season_action, methods=["POST"]),
]
