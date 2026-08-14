# tools/gear_tracker.py
"""Gear tracker — bike component maintenance tracking (issue 53).

Garmin's gear() endpoint (tools/profile.py) reports cumulative distance per
piece of gear (shoes, bikes) but knows nothing about the individual wear
components on a bike (chain, brake pads, tires, ...) or when they were last
serviced. This module fills that gap with a small local SQLite database:

    components       — one row per tracked bike component (name, which bike
                        it belongs to, when it was installed and at what
                        cumulative bike distance, and its maintenance
                        interval).
    maintenance_log   — one row per logged maintenance action (date, action,
                        notes, and the bike's cumulative distance *at the time
                        of service* — the anchor "distance since" is computed
                        from).

The gear's cumulative distance itself always comes live from Garmin
(profile.get_gear); only maintenance records and component definitions are
stored locally. "Distance since [service]" is therefore always
``current_gear_distance_km - distance_at_last_service_km`` (or
``- install_distance_km`` when a component has never been serviced) —
never a value stored directly.

Storage lives on the same Azure File Share already mounted for the Garmin
OAuth tokens (``~/.garminconnect``), under ``gear-tracker.db``.
``GEAR_TRACKER_DB_PATH`` overrides the location (used by tests).

Routes (all behind the same ``?token=`` bearer auth as ``/mcp`` and
``/dashboard``, enforced by the wrapper in server.py):

    GET  /dashboard/gear        — the gear tracker page (overview, bike
                                   component tables, maintenance log)
    GET  /api/gear/components   — components + live maintenance status (JSON)
    POST /api/gear/maintenance  — log a maintenance action
    POST /api/gear/components   — add/edit a component definition

Two MCP tools (``log_maintenance``, ``get_maintenance_status``) expose the
same operations to the assistant, so a coach prompt can flag overdue
maintenance and log it once confirmed.
"""
import html
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from tools.navbar import render_nav_html

logger = logging.getLogger(__name__)

PAGE_PATH = "/dashboard/gear"
API_PREFIX = "/api/gear"

# The token file share is mounted at ~/.garminconnect (see garmin_client.py);
# the path is repeated here rather than imported so this module stays usable
# without a configured Garmin session.
DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~/.garminconnect"), "gear-tracker.db")

# Suggested maintenance intervals used when a component is created without an
# explicit interval, matched case-insensitively by name. Override the whole
# table (or add entries) via GEAR_TRACKER_DEFAULT_INTERVALS_KM, a JSON object
# e.g. {"chain": 500, "belt": 15000}. A null value means "no fixed interval"
# (tracked, but never flagged as due) — e.g. bar tape.
DEFAULT_INTERVALS_KM = {
    "chain": 400,
    "brake pads": 2000,
    "tires": 5000,
    "chain ring": 8000,
    "cassette": 8000,
    "bar tape": None,
}

# Component wear status is a ratio of distance-since-service to its
# maintenance interval: green below 60%, yellow from 60% up to due, red once
# overdue. Shoes instead use Garmin's own cumulative distance against fixed
# bands (issue #53) since they have no "component interval" of their own.
_COMPONENT_YELLOW_RATIO = 0.60
_SHOE_YELLOW_KM = 500
_SHOE_ORANGE_KM = 650
_SHOE_RED_KM = 750

_STATUS_EMOJI = {"green": "\U0001F7E2", "yellow": "\U0001F7E1",
                  "orange": "\U0001F7E0", "red": "\U0001F534", "unknown": "⚪"}
_STATUS_COLOR = {"green": "#4fae72", "yellow": "#d9a441",
                  "orange": "#e2734a", "red": "#cf5a4e", "unknown": "#9397ab"}
_STATUS_RANK = {"green": 0, "yellow": 1, "orange": 2, "red": 3}

_ACTIONS = ("lubed", "replaced", "serviced", "adjusted", "other")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNSET = object()


# ── VALIDATION HELPERS ───────────────────────────────────────────────────────

def _validate_date(value: str, field: str = "date") -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}.")
    try:
        date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}.") from e
    return value


def _load_default_intervals() -> dict:
    intervals = dict(DEFAULT_INTERVALS_KM)
    overrides = os.environ.get("GEAR_TRACKER_DEFAULT_INTERVALS_KM")
    if overrides:
        try:
            parsed = json.loads(overrides)
            if isinstance(parsed, dict):
                intervals.update(parsed)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Ignoring unparseable GEAR_TRACKER_DEFAULT_INTERVALS_KM")
    return intervals


# ── STORAGE ───────────────────────────────────────────────────────────────────

def db_path() -> str:
    """Path to the SQLite database. Read per call so tests can override."""
    return os.environ.get("GEAR_TRACKER_DB_PATH") or DEFAULT_DB_PATH


def _connect() -> sqlite3.Connection:
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bike_uuid TEXT NOT NULL,
            bike_name TEXT,
            name TEXT NOT NULL,
            install_date TEXT NOT NULL,
            install_distance_km REAL NOT NULL DEFAULT 0,
            maintenance_interval_km REAL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS maintenance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            action TEXT NOT NULL,
            distance_at_service_km REAL NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _row_to_component(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "bike_uuid": row["bike_uuid"],
        "bike_name": row["bike_name"],
        "name": row["name"],
        "install_date": row["install_date"],
        "install_distance_km": row["install_distance_km"],
        "maintenance_interval_km": row["maintenance_interval_km"],
    }


def _row_to_log(row: sqlite3.Row) -> dict:
    entry = {
        "id": row["id"],
        "component_id": row["component_id"],
        "date": row["date"],
        "action": row["action"],
        "distance_at_service_km": row["distance_at_service_km"],
        "notes": row["notes"],
    }
    keys = row.keys()
    if "component_name" in keys:
        entry["component_name"] = row["component_name"]
    if "bike_name" in keys:
        entry["bike_name"] = row["bike_name"]
    if "bike_uuid" in keys:
        entry["bike_uuid"] = row["bike_uuid"]
    return entry


def list_components(bike_uuid: str | None = None) -> list[dict]:
    """Every tracked component, optionally scoped to one bike."""
    conn = _connect()
    try:
        if bike_uuid:
            rows = conn.execute(
                "SELECT * FROM components WHERE bike_uuid = ? ORDER BY name",
                (bike_uuid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM components ORDER BY bike_name, name"
            ).fetchall()
        return [_row_to_component(r) for r in rows]
    finally:
        conn.close()


def get_component(component_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM components WHERE id = ?", (component_id,)
        ).fetchone()
        return _row_to_component(row) if row else None
    finally:
        conn.close()


def find_component(bike_uuid: str, name: str) -> dict | None:
    """Look up a tracked component by bike + name, case-insensitively."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM components WHERE bike_uuid = ? AND lower(name) = lower(?)",
            (bike_uuid, name),
        ).fetchone()
        return _row_to_component(row) if row else None
    finally:
        conn.close()


def upsert_component(
    bike_uuid: str,
    name: str,
    bike_name: str | None = None,
    install_date: str | None = None,
    install_distance_km: float = 0.0,
    maintenance_interval_km=_UNSET,
    component_id: int | None = None,
) -> dict:
    """Create a component, or update an existing one.

    With ``component_id`` given, that exact row is updated. Otherwise a
    matching ``bike_uuid`` + ``name`` (case-insensitive) row is updated if one
    exists, and a new component is created if not — so re-submitting the "add
    component" form for the same name just edits it rather than duplicating.

    ``maintenance_interval_km`` left at its default keeps the existing value
    on an update; on create it falls back to :data:`DEFAULT_INTERVALS_KM`
    (matched case-insensitively by name), or None when there's no default.
    Pass ``None`` explicitly to clear/omit an interval.
    """
    if not bike_uuid or not bike_uuid.strip():
        raise ValueError("bike_uuid is required.")
    if not name or not name.strip():
        raise ValueError("name is required.")
    bike_uuid = bike_uuid.strip()
    name = name.strip()
    install_date = _validate_date(install_date or date.today().isoformat(), "install_date")

    conn = _connect()
    try:
        existing = None
        if component_id is not None:
            existing = conn.execute(
                "SELECT * FROM components WHERE id = ?", (component_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"No component with id {component_id}.")
        else:
            existing = conn.execute(
                "SELECT * FROM components WHERE bike_uuid = ? AND lower(name) = lower(?)",
                (bike_uuid, name),
            ).fetchone()

        if maintenance_interval_km is _UNSET:
            if existing is not None:
                maintenance_interval_km = existing["maintenance_interval_km"]
            else:
                maintenance_interval_km = _load_default_intervals().get(name.lower())

        now = datetime.now(timezone.utc).isoformat()
        if existing is not None:
            conn.execute(
                """UPDATE components SET bike_uuid=?, bike_name=?, name=?, install_date=?,
                   install_distance_km=?, maintenance_interval_km=? WHERE id=?""",
                (bike_uuid, bike_name, name, install_date, install_distance_km,
                 maintenance_interval_km, existing["id"]),
            )
            new_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO components
                   (bike_uuid, bike_name, name, install_date, install_distance_km,
                    maintenance_interval_km, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (bike_uuid, bike_name, name, install_date, install_distance_km,
                 maintenance_interval_km, now),
            )
            new_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM components WHERE id = ?", (new_id,)).fetchone()
        return _row_to_component(row)
    finally:
        conn.close()


def log_maintenance_entry(
    component_id: int,
    action: str,
    distance_at_service_km: float,
    date_: str | None = None,
    notes: str | None = None,
) -> dict:
    """Record one maintenance action against a tracked component."""
    if not action or not action.strip():
        raise ValueError("action is required.")
    date_ = _validate_date(date_ or date.today().isoformat())

    conn = _connect()
    try:
        component = conn.execute(
            "SELECT * FROM components WHERE id = ?", (component_id,)
        ).fetchone()
        if component is None:
            raise ValueError(f"No component with id {component_id}.")

        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO maintenance_log
               (component_id, date, action, distance_at_service_km, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (component_id, date_, action.strip(), distance_at_service_km,
             (notes or "").strip() or None, now),
        )
        conn.commit()
        row = conn.execute(
            """SELECT m.*, c.name AS component_name, c.bike_name AS bike_name, c.bike_uuid AS bike_uuid
               FROM maintenance_log m JOIN components c ON c.id = m.component_id
               WHERE m.id = ?""",
            (cur.lastrowid,),
        ).fetchone()
        return _row_to_log(row)
    finally:
        conn.close()


def list_maintenance_log(component_id: int | None = None, limit: int = 200) -> list[dict]:
    """Maintenance log entries, newest first, optionally scoped to one component."""
    conn = _connect()
    try:
        query = """SELECT m.*, c.name AS component_name, c.bike_name AS bike_name, c.bike_uuid AS bike_uuid
                    FROM maintenance_log m JOIN components c ON c.id = m.component_id"""
        params: tuple = ()
        if component_id is not None:
            query += " WHERE m.component_id = ?"
            params = (component_id,)
        query += " ORDER BY m.date DESC, m.id DESC LIMIT ?"
        rows = conn.execute(query, params + (limit,)).fetchall()
        return [_row_to_log(r) for r in rows]
    finally:
        conn.close()


def _last_service(conn: sqlite3.Connection, component_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM maintenance_log WHERE component_id = ? ORDER BY date DESC, id DESC LIMIT 1",
        (component_id,),
    ).fetchone()


# ── STATUS COMPUTATION ───────────────────────────────────────────────────────

def _component_status(distance_since_km: float | None, interval_km) -> str:
    if interval_km is None or interval_km <= 0 or distance_since_km is None:
        return "unknown"
    ratio = distance_since_km / interval_km
    if ratio >= 1.0:
        return "red"
    if ratio > _COMPONENT_YELLOW_RATIO:
        return "yellow"
    return "green"


def _shoe_status(distance_km: float | None) -> str:
    if distance_km is None:
        return "unknown"
    if distance_km < _SHOE_YELLOW_KM:
        return "green"
    if distance_km < _SHOE_ORANGE_KM:
        return "yellow"
    if distance_km < _SHOE_RED_KM:
        return "orange"
    return "red"


def _worst_status(statuses: list[str]) -> str:
    ranked = [s for s in statuses if s in _STATUS_RANK]
    if not ranked:
        return "unknown"
    return max(ranked, key=lambda s: _STATUS_RANK[s])


def _component_with_status(conn: sqlite3.Connection, component: dict,
                            gear_distance_km: float | None) -> dict:
    last = _last_service(conn, component["id"])
    if last is not None:
        last_serviced = last["date"]
        base_distance = last["distance_at_service_km"]
    else:
        last_serviced = None
        base_distance = component["install_distance_km"]

    distance_since = None
    if gear_distance_km is not None:
        distance_since = round(max(gear_distance_km - base_distance, 0), 1)

    interval = component["maintenance_interval_km"]
    status = _component_status(distance_since, interval)

    return {
        **component,
        "last_serviced": last_serviced or component["install_date"],
        "ever_serviced": last is not None,
        "distance_since_km": distance_since,
        "status": status,
        "status_emoji": _STATUS_EMOJI[status],
    }


def build_gear_status(gear_name: str | None = None) -> dict:
    """The full gear-tracker view: every piece of gear from Garmin, each
    bike's tracked components with live maintenance status computed against
    that bike's current cumulative distance.
    """
    from tools.profile import get_gear

    all_gear = get_gear()
    if gear_name:
        all_gear = [g for g in all_gear
                    if (g.get("name") or "").strip().lower() == gear_name.strip().lower()]

    conn = _connect()
    try:
        items = []
        for g in all_gear:
            gear_type = (g.get("activity_type") or "").lower()
            is_bike = "bike" in gear_type or "cycl" in gear_type
            is_shoe = "shoe" in gear_type

            components = []
            uuid = g.get("uuid")
            if uuid:
                rows = conn.execute(
                    "SELECT * FROM components WHERE bike_uuid = ? ORDER BY name", (uuid,)
                ).fetchall()
                components = [
                    _component_with_status(conn, _row_to_component(r), g.get("distance_km"))
                    for r in rows
                ]

            if is_shoe:
                item_status = _shoe_status(g.get("distance_km"))
            elif components:
                item_status = _worst_status([c["status"] for c in components])
            else:
                item_status = "unknown"

            items.append({
                **g,
                "status_indicator": item_status,
                "status_emoji": _STATUS_EMOJI[item_status],
                "status_color": _STATUS_COLOR[item_status],
                "is_bike": is_bike,
                "is_shoe": is_shoe,
                "components": components,
            })

        return {"gear": items}
    finally:
        conn.close()


# ── MCP TOOL FUNCTIONS ───────────────────────────────────────────────────────

def log_maintenance(gear_name: str, component: str, action: str,
                     notes: str | None = None) -> dict:
    """
    Log a maintenance action against a tracked bike component.

    Resolves the bike by its Garmin gear name and looks up the component by
    name (case-insensitive); if that component hasn't been tracked on this
    bike yet, it's created automatically — using the component's default
    maintenance interval when its name matches a known default (chain, brake
    pads, tires, chain ring, cassette, ...), otherwise untracked (no interval,
    never flagged as due). The bike's *current* cumulative distance (from
    Garmin) is recorded as the service point automatically.

    Args:
        gear_name: Exact gear name as registered in Garmin Connect (see the
            gear tool).
        component: Component name, e.g. "Chain", "Brake pads", "Tires".
        action:    What was done — "lubed", "replaced", "serviced",
                   "adjusted", or a short free-form description.
        notes:     Optional free-form notes.
    """
    from tools.profile import get_gear

    matches = [g for g in get_gear()
               if (g.get("name") or "").strip().lower() == gear_name.strip().lower()]
    if not matches:
        raise ValueError(f"No gear found named {gear_name!r}.")
    gear = matches[0]
    uuid = gear.get("uuid")
    if not uuid:
        raise ValueError(f"Gear {gear_name!r} has no uuid to track components against.")

    current_distance = gear.get("distance_km") or 0.0

    comp = find_component(uuid, component)
    if comp is None:
        comp = upsert_component(
            bike_uuid=uuid, name=component, bike_name=gear.get("name"),
            install_distance_km=current_distance,
        )

    entry = log_maintenance_entry(
        component_id=comp["id"], action=action,
        distance_at_service_km=current_distance, notes=notes,
    )
    return {
        "gear_name": gear.get("name"),
        "component": comp["name"],
        "action": entry["action"],
        "date": entry["date"],
        "distance_at_service_km": entry["distance_at_service_km"],
        "notes": entry["notes"],
    }


def get_maintenance_status(gear_name: str | None = None) -> dict:
    """
    Get bike component maintenance status — last serviced date, distance
    since service, maintenance interval, and a status ("green"/"yellow"/
    "red"/"unknown") for every tracked component.

    Args:
        gear_name: Optional exact gear name to scope to one bike (see the
            gear tool). Omit to get every registered piece of gear.
    """
    return build_gear_status(gear_name=gear_name)


# ── RENDERING ─────────────────────────────────────────────────────────────────

_STYLE = """
:root {
  color-scheme: dark;
  --bg:#161826; --surface:#232532; --surface-2:#2b2e3d; --fg:#e9e9ed; --muted:#9397ab;
  --divider:color-mix(in srgb, #e9e9ed 16%, transparent);
  --accent:#9184d9; --accent-200:#e7e5fe;
  --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font-family:var(--font);
       font-size:15px; line-height:1.5; }
main { max-width:920px; margin:0 auto; padding:20px 16px 64px; }
h1 { font-size:22px; font-weight:600; margin:0 0 4px; }
h2 { font-size:15px; font-weight:600; margin:28px 0 10px; letter-spacing:.02em; }
.muted { color:var(--muted); font-size:.85em; }
.error { color:#cf8a80; background:color-mix(in srgb, #cf5a4e 14%, var(--surface));
         border-radius:8px; padding:.7rem 1rem; margin:0 0 1rem; font-size:.9em; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; }
.card { background:var(--surface); border-radius:10px; padding:14px 16px;
        box-shadow:0 0 0 1px var(--divider); }
.card-head { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.dot { width:10px; height:10px; border-radius:50%; flex:0 0 auto; }
.gear-name { font-weight:600; font-size:14px; overflow:hidden; text-overflow:ellipsis;
             white-space:nowrap; }
.gear-type { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em; }
.gear-stats { display:flex; gap:18px; margin-top:4px; }
.gear-stats div div:first-child { font-size:18px; font-weight:600; }
.gear-stats div div:last-child { font-size:10px; color:var(--muted); }
.empty { color:var(--muted); font-size:.9em; padding:.5rem 0; }
table { width:100%; border-collapse:collapse; background:var(--surface); border-radius:10px;
        overflow:hidden; box-shadow:0 0 0 1px var(--divider); }
th, td { text-align:left; padding:9px 12px; font-size:13px; border-bottom:1px solid var(--divider); }
th { color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase; letter-spacing:.05em; }
tr:last-child td { border-bottom:none; }
.bike-block { margin-bottom:22px; }
.bike-title { font-size:14px; font-weight:600; margin-bottom:6px; }
details.row-actions { padding:0; }
details.row-actions summary { cursor:pointer; color:var(--accent); font-size:12px;
                               list-style:none; display:inline-block; margin-right:14px; }
details.row-actions summary::-webkit-details-marker { display:none; }
.inline-form { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:8px;
               padding:10px; background:var(--surface-2); border-radius:8px; }
.inline-form label { font-size:11px; color:var(--muted); display:flex; flex-direction:column; gap:3px; }
input, select, textarea, button {
  font:inherit; color:var(--fg); background:var(--bg); border:1px solid var(--divider);
  border-radius:6px; padding:.4rem .55rem;
}
textarea { min-width:180px; resize:vertical; }
button { cursor:pointer; border-color:var(--accent); color:var(--accent-200); }
button:hover { background:color-mix(in srgb, var(--accent) 20%, var(--bg)); }
.log-list { max-height:340px; overflow-y:auto; display:flex; flex-direction:column; gap:8px;
            padding-right:4px; }
.log-entry { background:var(--surface); border-radius:8px; padding:10px 12px; font-size:13px;
             box-shadow:0 0 0 1px var(--divider); }
.log-entry .log-meta { color:var(--muted); font-size:11px; margin-top:2px; }
.log-entry .log-notes { margin-top:4px; color:var(--fg); opacity:.85; }
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _url(path: str, token: str | None) -> str:
    return f"{path}?{urlencode({'token': token})}" if token else path


def _error_redirect_url(token: str | None, error: str) -> str:
    """The gear page URL with an ``error`` message queued for display,
    carrying the bearer token alongside it when one was supplied."""
    params = {"error": error}
    if token:
        params["token"] = token
    return f"{PAGE_PATH}?{urlencode(params)}"


def _fmt_km(v) -> str:
    if v is None:
        return "&mdash;"
    return f"{v:,.1f} km".replace(".0 km", " km")


def _fmt_dur(minutes) -> str:
    if not minutes:
        return "&mdash;"
    total = round(minutes)
    h, m = divmod(total, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _status_dot(status: str) -> str:
    color = _STATUS_COLOR.get(status, _STATUS_COLOR["unknown"])
    return f'<span class="dot" style="background:{color}"></span>'


def _gear_card_html(g: dict) -> str:
    stats = [("Distance", _fmt_km(g.get("distance_km")))]
    if g.get("duration_min"):
        stats.append(("Time", _fmt_dur(g.get("duration_min"))))
    stats_html = "".join(
        f"<div><div>{v}</div><div>{_e(k)}</div></div>" for k, v in stats
    )
    return f"""
    <div class="card">
      <div class="card-head">{_status_dot(g["status_indicator"])}
        <div style="min-width:0"><div class="gear-name">{_e(g.get("name"))}</div>
        <div class="gear-type">{_e(g.get("activity_type") or "Gear")}</div></div>
      </div>
      <div class="gear-stats">{stats_html}</div>
    </div>"""


def _component_row_html(c: dict, token: str | None) -> str:
    interval = c.get("maintenance_interval_km")
    interval_txt = _fmt_km(interval) if interval else "N/A"
    action_options = "".join(f'<option value="{a}">{a.title()}</option>' for a in _ACTIONS)
    interval_value = "" if interval is None else f"{interval:g}"
    return f"""
    <tr>
      <td>{_e(c["name"])}</td>
      <td>{_e(c["last_serviced"])}</td>
      <td>{_fmt_km(c.get("distance_since_km"))}</td>
      <td>{interval_txt}</td>
      <td>{c["status_emoji"]}</td>
    </tr>
    <tr>
      <td colspan="5" style="padding-top:0">
        <details class="row-actions">
          <summary>Log maintenance</summary>
          <form class="inline-form" method="post" action="{_e(_url(API_PREFIX + '/maintenance', token))}">
            <input type="hidden" name="component_id" value="{c['id']}">
            <label>Action<select name="action">{action_options}</select></label>
            <label>Date<input type="date" name="date" value="{date.today().isoformat()}"></label>
            <label>Notes<input type="text" name="notes" placeholder="optional"></label>
            <button type="submit">Log</button>
          </form>
        </details>
        <details class="row-actions">
          <summary>Edit</summary>
          <form class="inline-form" method="post" action="{_e(_url(API_PREFIX + '/components', token))}">
            <input type="hidden" name="component_id" value="{c['id']}">
            <input type="hidden" name="bike_uuid" value="{_e(c['bike_uuid'])}">
            <label>Name<input type="text" name="name" value="{_e(c['name'])}" required></label>
            <label>Install date<input type="date" name="install_date" value="{_e(c['install_date'])}"></label>
            <label>Interval (km)<input type="number" step="1" min="0" name="maintenance_interval_km"
                value="{interval_value}" placeholder="N/A"></label>
            <button type="submit">Save</button>
          </form>
        </details>
      </td>
    </tr>"""


def _bike_block_html(g: dict, token: str | None) -> str:
    rows = "".join(_component_row_html(c, token) for c in g["components"])
    table = f"""
    <table>
      <thead><tr><th>Component</th><th>Last serviced</th><th>Distance since</th>
        <th>Interval</th><th>Status</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>""" if g["components"] else '<div class="empty">No components tracked yet.</div>'

    add_form = f"""
    <details class="row-actions" style="margin-top:8px">
      <summary>+ Add component</summary>
      <form class="inline-form" method="post" action="{_e(_url(API_PREFIX + '/components', token))}">
        <input type="hidden" name="bike_uuid" value="{_e(g.get('uuid') or '')}">
        <input type="hidden" name="bike_name" value="{_e(g.get('name') or '')}">
        <label>Name<input type="text" name="name" placeholder="e.g. Chain" required></label>
        <label>Install date<input type="date" name="install_date" value="{date.today().isoformat()}"></label>
        <label>Interval (km)<input type="number" step="1" min="0" name="maintenance_interval_km"
            placeholder="optional"></label>
        <button type="submit">Add</button>
      </form>
    </details>"""

    return f"""
    <div class="bike-block">
      <div class="bike-title">{_status_dot(g["status_indicator"])} {_e(g.get("name"))}
        <span class="muted">&middot; {_fmt_km(g.get("distance_km"))}</span></div>
      {table}
      {add_form}
    </div>"""


def _log_entry_html(entry: dict, bike_names: dict) -> str:
    bike_name = bike_names.get(entry.get("bike_uuid")) or entry.get("bike_name")
    notes = f'<div class="log-notes">{_e(entry.get("notes"))}</div>' if entry.get("notes") else ""
    return f"""
    <div class="log-entry">
      <div><strong>{_e(entry.get("action", "").title())}</strong> &mdash;
        {_e(bike_name)} / {_e(entry.get("component_name"))}</div>
      <div class="log-meta">{_e(entry["date"])} &middot; at {_fmt_km(entry["distance_at_service_km"])}</div>
      {notes}
    </div>"""


def _log_form_html(components: list[dict], bike_names: dict, token: str | None) -> str:
    if not components:
        return ""
    options = "".join(
        f'<option value="{c["id"]}">'
        f'{_e(bike_names.get(c.get("bike_uuid")) or c.get("bike_name"))} &mdash; {_e(c["name"])}</option>'
        for c in components
    )
    action_options = "".join(f'<option value="{a}">{a.title()}</option>' for a in _ACTIONS)
    return f"""
    <form class="inline-form" method="post" action="{_e(_url(API_PREFIX + '/maintenance', token))}"
        style="margin-bottom:12px">
      <label>Component<select name="component_id">{options}</select></label>
      <label>Action<select name="action">{action_options}</select></label>
      <label>Date<input type="date" name="date" value="{date.today().isoformat()}"></label>
      <label>Notes<input type="text" name="notes" placeholder="optional"></label>
      <button type="submit">+ Log maintenance</button>
    </form>"""


def render_gear_html(data: dict, token: str | None = None, error: str | None = None) -> str:
    gear = data.get("gear") or []
    active_gear = [g for g in gear if (g.get("status") or "active").lower() == "active"]
    bikes = [g for g in active_gear if g["is_bike"]]

    cards_html = "".join(_gear_card_html(g) for g in active_gear) or \
        '<div class="empty">No registered gear yet — add shoes or a bike in Garmin Connect.</div>'

    bikes_html = "".join(_bike_block_html(g, token) for g in bikes) or \
        '<div class="empty">No bikes registered yet.</div>'

    bike_names = {g["uuid"]: g.get("name") for g in gear if g.get("uuid")}
    all_components = [c for g in bikes for c in g["components"]]
    log_entries = list_maintenance_log(limit=100)
    log_html = "".join(_log_entry_html(e, bike_names) for e in log_entries) or \
        '<div class="empty">No maintenance logged yet.</div>'

    error_html = f'<div class="error">{_e(error)}</div>' if error else ""

    body = f"""
    <h1>Gear tracker</h1>
    <div class="muted">Component wear and maintenance history for your registered gear.</div>
    {error_html}
    <h2>Overview</h2>
    <div class="cards">{cards_html}</div>
    <h2>Bike components</h2>
    {bikes_html}
    <h2>Maintenance log</h2>
    {_log_form_html(all_components, bike_names, token)}
    <div class="log-list">{log_html}</div>
    """

    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Gear Tracker</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        f'{render_nav_html("gear", token)}'
        f"<main>{body}</main></body></html>"
    )


# ── ROUTES ────────────────────────────────────────────────────────────────────

_NO_STORE = {"Cache-Control": "no-store"}


def _wants_json(request) -> bool:
    """Form posts (the page's own UI) redirect back to the page; anything else
    (an API caller) gets JSON back."""
    ctype = request.headers.get("content-type", "")
    return "application/json" in ctype


async def serve_gear_page(request):
    """GET /dashboard/gear — the gear tracker page."""
    token = request.query_params.get("token")
    error = request.query_params.get("error")
    try:
        data = build_gear_status()
    except Exception as e:  # pragma: no cover — defensive, mirrors dashboard.py
        logger.exception("Gear tracker data fetch failed")
        return HTMLResponse(
            render_gear_html({"gear": []}, token, error=f"{type(e).__name__}: {e}"),
            headers=_NO_STORE,
        )
    return HTMLResponse(render_gear_html(data, token, error=error), headers=_NO_STORE)


async def get_components(request):
    """GET /api/gear/components — components + live maintenance status (JSON)."""
    gear_name = request.query_params.get("gear_name")
    return JSONResponse(build_gear_status(gear_name=gear_name), headers=_NO_STORE)


async def _request_fields(request) -> dict:
    if "application/json" in request.headers.get("content-type", ""):
        body = await request.json()
        return body if isinstance(body, dict) else {}
    form = await request.form()
    return dict(form)


async def post_maintenance(request):
    """POST /api/gear/maintenance — log a maintenance action.

    Accepts either a JSON body or an HTML form post; a form post (the page's
    own UI) redirects back to the gear page, a JSON request gets JSON back.
    """
    token = request.query_params.get("token")
    is_json = _wants_json(request)
    fields = await _request_fields(request)

    try:
        component_id = int(fields.get("component_id"))
    except (TypeError, ValueError):
        err = "component_id is required."
        return (JSONResponse({"error": err}, status_code=400) if is_json
                else RedirectResponse(_error_redirect_url(token, err), status_code=303))

    try:
        component = get_component(component_id)
        if component is None:
            raise ValueError(f"No component with id {component_id}.")

        from tools.profile import get_gear
        bike = next((g for g in get_gear() if g.get("uuid") == component["bike_uuid"]), None)
        if bike is None:
            raise ValueError("That component's bike is no longer registered in Garmin.")

        entry = log_maintenance_entry(
            component_id=component_id,
            action=fields.get("action") or "",
            distance_at_service_km=bike.get("distance_km") or 0.0,
            date_=fields.get("date") or None,
            notes=fields.get("notes") or None,
        )
    except ValueError as e:
        return (JSONResponse({"error": str(e)}, status_code=400) if is_json
                else RedirectResponse(_error_redirect_url(token, str(e)), status_code=303))

    if is_json:
        return JSONResponse(entry)
    return RedirectResponse(_url(PAGE_PATH, token), status_code=303)


async def post_component(request):
    """POST /api/gear/components — add or edit a component definition.

    Accepts either a JSON body or an HTML form post; a form post (the page's
    own UI) redirects back to the gear page, a JSON request gets JSON back.
    """
    token = request.query_params.get("token")
    is_json = _wants_json(request)
    fields = await _request_fields(request)

    component_id = fields.get("component_id")
    try:
        component_id = int(component_id) if component_id not in (None, "") else None
    except (TypeError, ValueError):
        component_id = None

    interval_raw = fields.get("maintenance_interval_km")
    if interval_raw in (None, ""):
        interval_km = _UNSET if component_id is not None else None
    else:
        try:
            interval_km = float(interval_raw)
        except (TypeError, ValueError):
            interval_km = _UNSET

    try:
        component = upsert_component(
            bike_uuid=fields.get("bike_uuid") or "",
            name=fields.get("name") or "",
            bike_name=fields.get("bike_name") or None,
            install_date=fields.get("install_date") or None,
            install_distance_km=float(fields.get("install_distance_km") or 0.0),
            maintenance_interval_km=interval_km,
            component_id=component_id,
        )
    except ValueError as e:
        return (JSONResponse({"error": str(e)}, status_code=400) if is_json
                else RedirectResponse(_error_redirect_url(token, str(e)), status_code=303))

    if is_json:
        return JSONResponse(component)
    return RedirectResponse(_url(PAGE_PATH, token), status_code=303)


ROUTES = [
    Route(PAGE_PATH, serve_gear_page, methods=["GET"]),
    Route(f"{API_PREFIX}/components", get_components, methods=["GET"]),
    Route(f"{API_PREFIX}/components", post_component, methods=["POST"]),
    Route(f"{API_PREFIX}/maintenance", post_maintenance, methods=["POST"]),
]


def owns_path(path: str) -> bool:
    """Whether a request path belongs to this sub-app (used for dispatch)."""
    return path == PAGE_PATH or path.startswith(f"{API_PREFIX}/")


def create_app() -> Starlette:
    """The gear-tracker sub-app, mounted by server.py behind token auth."""
    return Starlette(routes=ROUTES)
