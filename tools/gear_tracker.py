# tools/gear_tracker.py
"""Gear tracker — bike component maintenance tracking (issue 53).

Garmin's gear() endpoint (tools/profile.py) reports cumulative distance per
piece of gear (shoes, bikes) but knows nothing about the individual wear
components on a bike (chain, brake pads, tires, ...) or when they were last
serviced. This module fills that gap with a small local JSON store:

    components       — one entry per tracked bike component (name, which bike
                        it belongs to, when it was installed and at what
                        cumulative bike distance, and its maintenance
                        interval).
    maintenance_log   — one entry per logged maintenance action (date, action,
                        notes, and the bike's cumulative distance *at the time
                        of service* — the anchor "distance since" is computed
                        from).

The gear's cumulative distance itself always comes live from Garmin
(profile.get_gear); only maintenance records and component definitions are
stored locally. "Distance since [service]" is therefore always
``current_gear_distance_km - distance_at_last_service_km`` (or
``- install_distance_km`` when a component has never been serviced) —
never a value stored directly.

Garmin Connect lets a part (a chain, a cassette, a set of tires, ...) be
registered as its *own* gear item, with its own real cumulative distance —
separate from the bike it's mounted on. Garmin's API gives no field linking
that part back to a bike, so a component here can optionally carry a
``linked_gear_uuid`` pointing at one of those Garmin-tracked items (see
:func:`upsert_component` and the ``linkable_gear`` list
:func:`build_gear_status` returns — every active, not-yet-linked,
non-bike/non-shoe gear item, for a "Link component" picker). A linked
component's distance-since is measured against *that item's* live distance
rather than the bike's — computed in :func:`_component_with_status`. Its
service interval, though, is always just the plain stored
``maintenance_interval_km``: never inherited or overridden from Garmin (which
has no real "service interval" concept of its own, only a replace-at
odometer the athlete may or may not have set) — only the "Link component"
form's Type picker (:data:`COMPONENT_TYPES`) seeds a sensible default for it
at creation time, same as :data:`DEFAULT_INTERVALS_KM` does by name for the
``log_maintenance`` MCP tool's auto-create path. A component with no
``linked_gear_uuid`` behaves exactly as before: a name typed in by hand,
measured against the bike's own cumulative distance.

Storage lives on the same Azure File Share already mounted for the Garmin
OAuth tokens (``~/.garminconnect``), under ``gear-tracker/gear_data.json`` —
the same JSON-file-on-the-share pattern already used for the Garmin tokens
and the training plan (tools/training_plan.py). ``GEAR_TRACKER_DATA_PATH``
overrides the location (used by tests).

This used to be a SQLite database, but SQLite's own locking doesn't work on
this file share (it's SMB, which doesn't support the POSIX file locking
SQLite requires) — writes could leave the database locked and, since the
lock is on the file itself, that stuck lock blocked every other reader too,
taking down /dashboard and /mcp along with the gear tab (issue #60). A JSON
file has no locking of its own to get stuck: every read parses the whole
(tiny — a few components, a few dozen log entries) file fresh, and every
write is atomic (write to a ``.tmp`` file, then ``os.replace()`` over the
real path) so a crash mid-write can't corrupt it. ``_lock`` only serializes
read-modify-write cycles *within this process* (e.g. two maintenance-log
submissions arriving at once); it has nothing to do with the file share.

Viewing lives in the dashboard's Gear tab (tools/dashboard.py, ``/dashboard``
— its ``tp-gear`` panel calls :func:`build_gear_status`, styled to match the
rest of the Nocturne dashboard). This module owns the data layer plus the
JSON/form API those forms post to (all behind the same ``?token=`` bearer
auth as ``/mcp`` and ``/dashboard``, enforced by the wrapper in server.py):

    GET  /dashboard/gear        — redirects to /dashboard?tab=gear (the page
                                   used to be standalone; this forwards
                                   existing links/bookmarks)
    GET  /api/gear/components   — components + live maintenance status (JSON)
    POST /api/gear/maintenance  — log a maintenance action
    POST /api/gear/components   — add/edit a component definition

Two MCP tools (``log_maintenance``, ``get_maintenance_status``) expose the
same operations to the assistant, so a coach prompt can flag overdue
maintenance and log it once confirmed.
"""
import json
import logging
import os
import re
import threading
import uuid as uuid_module
from datetime import date
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

PAGE_PATH = "/dashboard/gear"
API_PREFIX = "/api/gear"

# The token file share is mounted at ~/.garminconnect (see garmin_client.py);
# the path is repeated here rather than imported so this module stays usable
# without a configured Garmin session.
DEFAULT_DATA_PATH = os.path.join(
    os.path.expanduser("~/.garminconnect"), "gear-tracker", "gear_data.json"
)

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

# The dashboard's "Link component" form's fixed Type choices (issue 63
# follow-up) — a coarser, always-available alternative to DEFAULT_INTERVALS_KM
# above, which only matches when a component's *name* happens to equal one of
# its keys exactly. A linked component's name is normally the Garmin gear
# item's own (e.g. "Shimano 12s Chain"), which never matches "chain", so the
# form asks for a Type instead and uses that to seed the service interval
# when it's left blank at creation. Not persisted on the component itself.
COMPONENT_TYPES = ("Chain", "Cassette", "Tire", "Brakes")
_TYPE_INTERVAL_KM = {"chain": 400, "cassette": 8000, "tire": 5000, "brakes": 2000}

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

def data_path() -> str:
    """Path to the JSON data file. Read per call so tests can override."""
    return os.environ.get("GEAR_TRACKER_DATA_PATH") or DEFAULT_DATA_PATH


# Read-modify-write cycles (upsert_component, log_maintenance_entry) aren't
# safe to interleave within this process, so this serializes them. It says
# nothing about the file share itself — the atomic write in _write() is what
# keeps the file on disk from being corrupted (see module docstring).
_lock = threading.RLock()


def _read() -> dict:
    """Read the full JSON store, fresh, every call — it's tiny (well under
    10KB) so there's no reason to cache it and risk serving stale data."""
    path = data_path()
    if not os.path.exists(path):
        return {"components": [], "maintenance_log": []}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("components", [])
    data.setdefault("maintenance_log", [])
    return data


def _write(data: dict) -> None:
    """Atomic write: write to a ``.tmp`` file, then ``os.replace()`` over the
    real path, so a crash mid-write can't leave a corrupt/partial file."""
    path = data_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def _find(items: list[dict], **fields):
    """First item matching all given field values exactly, or None."""
    return next(
        (item for item in items if all(item.get(k) == v for k, v in fields.items())),
        None,
    )


def _enrich_log_entry(entry: dict, component: dict) -> dict:
    """A maintenance-log entry plus the fields the old SQL join used to add
    (component/bike name and bike uuid), for display."""
    return {
        **entry,
        "component_name": component["name"],
        "bike_name": component.get("bike_name"),
        "bike_uuid": component.get("bike_uuid"),
    }


def list_components(bike_uuid: str | None = None) -> list[dict]:
    """Every tracked component, optionally scoped to one bike."""
    with _lock:
        components = _read()["components"]
    if bike_uuid:
        return sorted((c for c in components if c["bike_uuid"] == bike_uuid),
                      key=lambda c: c["name"].lower())
    return sorted(components, key=lambda c: ((c.get("bike_name") or "").lower(), c["name"].lower()))


def get_component(component_id: str) -> dict | None:
    with _lock:
        return _find(_read()["components"], id=component_id)


def find_component(bike_uuid: str, name: str) -> dict | None:
    """Look up a tracked component by bike + name, case-insensitively."""
    with _lock:
        components = _read()["components"]
    return next(
        (c for c in components
         if c["bike_uuid"] == bike_uuid and c["name"].lower() == name.lower()),
        None,
    )


def upsert_component(
    bike_uuid: str,
    name: str | None = None,
    bike_name: str | None = None,
    install_date: str | None = None,
    install_distance_km: float = 0.0,
    maintenance_interval_km=_UNSET,
    component_id: str | None = None,
    linked_gear_uuid=_UNSET,
    component_type: str | None = None,
) -> dict:
    """Create a component, or update an existing one.

    With ``component_id`` given, that exact component is updated. Otherwise a
    matching ``bike_uuid`` + ``name`` (case-insensitive) component is updated
    if one exists, and a new component is created if not — so re-submitting
    the "link component" form for the same name just edits it rather than
    duplicating.

    ``maintenance_interval_km`` left at its default keeps the existing value
    on an update; on create it falls back, in order, to ``component_type``'s
    default (see :data:`COMPONENT_TYPES`), then :data:`DEFAULT_INTERVALS_KM`
    (matched case-insensitively by name), or None when neither applies. Pass
    ``None`` explicitly to clear/omit an interval. It is always the plain
    service interval from here on — never re-derived from Garmin data on a
    linked component (see the module docstring).

    ``linked_gear_uuid``, left at its default, keeps the existing link (or
    None) on an update; pass a Garmin gear uuid to link the component to that
    gear item's own live distance (see the module docstring), or ``None``
    explicitly to unlink it. If ``name`` is omitted, it defaults to that
    linked gear item's own name (fetched live if the caller doesn't already
    know it — the dashboard's Link form passes it directly to avoid that
    round trip) — so linking doesn't require retyping it. Without a link,
    ``component_type`` (e.g. "Chain") fills in as the name instead, when
    ``name`` is also omitted — the dashboard's Link form's fallback for
    parts Garmin doesn't track individually.
    """
    if not bike_uuid or not bike_uuid.strip():
        raise ValueError("bike_uuid is required.")
    bike_uuid = bike_uuid.strip()
    name = (name or "").strip()

    if not name and linked_gear_uuid not in (_UNSET, None, ""):
        from tools.profile import get_gear
        linked = next((g for g in get_gear() if g.get("uuid") == linked_gear_uuid), None)
        name = ((linked or {}).get("name") or "").strip()
    if not name and component_type:
        name = component_type.strip()
    if not name:
        raise ValueError("name is required.")

    install_date = _validate_date(install_date or date.today().isoformat(), "install_date")

    with _lock:
        data = _read()
        if component_id is not None:
            existing = _find(data["components"], id=component_id)
            if existing is None:
                raise ValueError(f"No component with id {component_id}.")
        else:
            existing = next(
                (c for c in data["components"]
                 if c["bike_uuid"] == bike_uuid and c["name"].lower() == name.lower()),
                None,
            )

        if maintenance_interval_km is _UNSET:
            if existing is not None:
                maintenance_interval_km = existing["maintenance_interval_km"]
            elif component_type and component_type.strip().lower() in _TYPE_INTERVAL_KM:
                maintenance_interval_km = _TYPE_INTERVAL_KM[component_type.strip().lower()]
            else:
                maintenance_interval_km = _load_default_intervals().get(name.lower())

        if linked_gear_uuid is _UNSET:
            linked_gear_uuid = existing.get("linked_gear_uuid") if existing is not None else None
        else:
            linked_gear_uuid = linked_gear_uuid or None

        if existing is not None:
            existing.update({
                "bike_uuid": bike_uuid,
                "bike_name": bike_name,
                "name": name,
                "install_date": install_date,
                "install_distance_km": install_distance_km,
                "maintenance_interval_km": maintenance_interval_km,
                "linked_gear_uuid": linked_gear_uuid,
            })
            result = dict(existing)
        else:
            result = {
                "id": str(uuid_module.uuid4()),
                "bike_uuid": bike_uuid,
                "bike_name": bike_name,
                "name": name,
                "install_date": install_date,
                "install_distance_km": install_distance_km,
                "maintenance_interval_km": maintenance_interval_km,
                "linked_gear_uuid": linked_gear_uuid,
            }
            data["components"].append(result)
            result = dict(result)
        _write(data)
        return result


def log_maintenance_entry(
    component_id: str,
    action: str,
    distance_at_service_km: float,
    date_: str | None = None,
    notes: str | None = None,
) -> dict:
    """Record one maintenance action against a tracked component."""
    if not action or not action.strip():
        raise ValueError("action is required.")
    date_ = _validate_date(date_ or date.today().isoformat())

    with _lock:
        data = _read()
        component = _find(data["components"], id=component_id)
        if component is None:
            raise ValueError(f"No component with id {component_id}.")

        entry = {
            "id": str(uuid_module.uuid4()),
            "component_id": component_id,
            "date": date_,
            "action": action.strip(),
            "distance_at_service_km": distance_at_service_km,
            "notes": (notes or "").strip() or None,
        }
        data["maintenance_log"].append(entry)
        _write(data)
        return _enrich_log_entry(entry, component)


def _sorted_log(entries: list[dict]) -> list[tuple[int, dict]]:
    """(index, entry) pairs newest-first: by date descending, then by
    insertion order descending as a tiebreak for same-day entries — the JSON
    equivalent of the old ``ORDER BY date DESC, id DESC``."""
    return sorted(enumerate(entries), key=lambda pair: (pair[1]["date"], pair[0]), reverse=True)


def list_maintenance_log(component_id: str | None = None, limit: int = 200) -> list[dict]:
    """Maintenance log entries, newest first, optionally scoped to one component."""
    with _lock:
        data = _read()
    entries = data["maintenance_log"]
    if component_id is not None:
        entries = [e for e in entries if e["component_id"] == component_id]

    result = []
    for _, entry in _sorted_log(entries):
        component = _find(data["components"], id=entry["component_id"])
        if component is None:
            continue  # component was removed — matches the old INNER JOIN
        result.append(_enrich_log_entry(entry, component))
        if len(result) >= limit:
            break
    return result


def _last_service(data: dict, component_id: str) -> dict | None:
    entries = [e for e in data["maintenance_log"] if e["component_id"] == component_id]
    if not entries:
        return None
    return _sorted_log(entries)[0][1]


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


def _is_bike(g: dict) -> bool:
    gear_type = (g.get("activity_type") or "").lower()
    return "bike" in gear_type or "cycl" in gear_type


def _is_shoe(g: dict) -> bool:
    return "shoe" in (g.get("activity_type") or "").lower()


def _component_with_status(data: dict, component: dict, basis_gear: dict | None) -> dict:
    """A component plus its live status, computed against ``basis_gear`` —
    the Garmin gear item its distance is measured against: the linked gear
    item's own distance when the component is linked to one (see the module
    docstring), otherwise the bike itself. ``None`` when that basis gear
    can't be found (e.g. its Garmin gear item was deleted).
    """
    last = _last_service(data, component["id"])
    if last is not None:
        last_serviced = last["date"]
        base_distance = last["distance_at_service_km"]
    else:
        last_serviced = None
        base_distance = component["install_distance_km"]

    gear_distance_km = basis_gear.get("distance_km") if basis_gear else None
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


def build_gear_status(gear_name: str | None = None, log_limit: int = 50) -> dict:
    """The full gear-tracker view: every piece of gear from Garmin, each
    bike's tracked components with live maintenance status computed against
    that bike's (or a linked component's own) current cumulative distance,
    the maintenance log (newest first, up to ``log_limit`` entries), and
    ``linkable_gear`` — active Garmin gear items (chains, cassettes, tires,
    ...) not yet linked to a component, for a "Link component" picker.

    The log is included here — rather than left to a separate
    :func:`list_maintenance_log` call — so a single call covers everything
    the dashboard's Gear tab needs in one round trip against the store
    (issue #58).
    """
    from tools.profile import get_gear

    all_gear = get_gear()
    gear_by_uuid = {g["uuid"]: g for g in all_gear if g.get("uuid")}

    filtered_gear = all_gear
    if gear_name:
        filtered_gear = [g for g in all_gear
                          if (g.get("name") or "").strip().lower() == gear_name.strip().lower()]

    with _lock:
        data = _read()

    linked_uuids = {c["linked_gear_uuid"] for c in data["components"] if c.get("linked_gear_uuid")}
    linkable_gear = sorted(
        (
            {
                "uuid": g["uuid"],
                "name": g.get("name"),
                "model": g.get("model"),
                "distance_km": g.get("distance_km"),
                "max_distance_km": g.get("max_distance_km"),
            }
            for g in all_gear
            if g.get("uuid") and g["uuid"] not in linked_uuids
            and (g.get("status") or "active").lower() == "active"
            and not _is_bike(g) and not _is_shoe(g)
        ),
        key=lambda g: (g["name"] or "").lower(),
    )

    items = []
    for g in filtered_gear:
        is_bike = _is_bike(g)
        is_shoe = _is_shoe(g)

        components = []
        bike_uuid = g.get("uuid")
        if bike_uuid:
            matching = sorted(
                (c for c in data["components"] if c["bike_uuid"] == bike_uuid),
                key=lambda c: c["name"].lower(),
            )
            components = [
                _component_with_status(
                    data, c,
                    gear_by_uuid.get(c["linked_gear_uuid"]) if c.get("linked_gear_uuid") else g,
                )
                for c in matching
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

    maintenance_log = []
    for _, entry in _sorted_log(data["maintenance_log"]):
        component = _find(data["components"], id=entry["component_id"])
        if component is None:
            continue
        maintenance_log.append(_enrich_log_entry(entry, component))
        if len(maintenance_log) >= log_limit:
            break

    return {"gear": items, "maintenance_log": maintenance_log, "linkable_gear": linkable_gear}


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
    never flagged as due). The service point recorded is that new component's
    bike distance; an *existing* component that's linked to its own
    Garmin-tracked gear item (see the gear tool's "Other"-typed entries, e.g.
    a chain or cassette registered on its own) instead records that linked
    item's own current distance, since that's the basis its status is judged
    against.

    Args:
        gear_name: Exact gear name as registered in Garmin Connect (see the
            gear tool).
        component: Component name, e.g. "Chain", "Brake pads", "Tires".
        action:    What was done — "lubed", "replaced", "serviced",
                   "adjusted", or a short free-form description.
        notes:     Optional free-form notes.
    """
    from tools.profile import get_gear

    all_gear = get_gear()
    matches = [g for g in all_gear
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
        basis_distance = current_distance
    elif comp.get("linked_gear_uuid"):
        linked = next((g for g in all_gear if g.get("uuid") == comp["linked_gear_uuid"]), None)
        if linked is None:
            raise ValueError(f"Component {component!r}'s linked gear is no longer registered in Garmin.")
        basis_distance = linked.get("distance_km") or 0.0
    else:
        basis_distance = current_distance

    entry = log_maintenance_entry(
        component_id=comp["id"], action=action,
        distance_at_service_km=basis_distance, notes=notes,
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
    "red"/"unknown") for every tracked component, plus the recent
    maintenance log and the list of Garmin-tracked gear items (chains,
    cassettes, tires, ...) not yet linked to a bike component.

    Args:
        gear_name: Optional exact gear name to scope to one bike (see the
            gear tool). Omit to get every registered piece of gear.
    """
    return build_gear_status(gear_name=gear_name)


# ── API ROUTES ────────────────────────────────────────────────────────────────
# Viewing lives in the dashboard's Gear tab (tools/dashboard.py, /dashboard);
# this module only serves the JSON/form API those forms post to, plus a
# redirect for the page's old standalone URL.

_NO_STORE = {"Cache-Control": "no-store"}


def _dashboard_gear_url(token: str | None) -> str:
    """/dashboard with the Gear tab open, carrying the bearer token when one
    was supplied."""
    params = {"tab": "gear"}
    if token:
        params["token"] = token
    return f"/dashboard?{urlencode(params)}"


def _error_redirect_url(token: str | None, error: str) -> str:
    """The dashboard's Gear tab URL with an ``error`` message queued for
    display, carrying the bearer token alongside it when one was supplied."""
    params = {"tab": "gear", "error": error}
    if token:
        params["token"] = token
    return f"/dashboard?{urlencode(params)}"


def _wants_json(request) -> bool:
    """Form posts (the dashboard's own Gear-tab UI) redirect back to the
    dashboard; anything else (an API caller) gets JSON back."""
    ctype = request.headers.get("content-type", "")
    return "application/json" in ctype


async def serve_gear_page(request):
    """GET /dashboard/gear — redirects into the dashboard's Gear tab.

    Gear tracking used to be its own page; it's now a tab on /dashboard
    (tools/dashboard.py's tp-gear panel), so this just forwards bookmarks and
    existing links to the right place.
    """
    token = request.query_params.get("token")
    return RedirectResponse(_dashboard_gear_url(token), status_code=302)


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

    Accepts either a JSON body or an HTML form post; a form post (the
    dashboard's own Gear-tab UI) redirects back to the Gear tab, a JSON
    request gets JSON back.
    """
    token = request.query_params.get("token")
    is_json = _wants_json(request)
    fields = await _request_fields(request)

    component_id = fields.get("component_id") or None
    if component_id is None:
        err = "component_id is required."
        return (JSONResponse({"error": err}, status_code=400) if is_json
                else RedirectResponse(_error_redirect_url(token, err), status_code=303))

    try:
        component = get_component(component_id)
        if component is None:
            raise ValueError(f"No component with id {component_id}.")

        from tools.profile import get_gear
        all_gear = get_gear()
        bike = next((g for g in all_gear if g.get("uuid") == component["bike_uuid"]), None)
        if bike is None:
            raise ValueError("That component's bike is no longer registered in Garmin.")

        # A linked component's status is judged against its own linked gear
        # item's distance (see the module docstring), not the bike's — the
        # logged service point has to use the same basis.
        linked_uuid = component.get("linked_gear_uuid")
        basis_gear = bike
        if linked_uuid:
            basis_gear = next((g for g in all_gear if g.get("uuid") == linked_uuid), None)
            if basis_gear is None:
                raise ValueError("That component's linked gear is no longer registered in Garmin.")

        entry = log_maintenance_entry(
            component_id=component_id,
            action=fields.get("action") or "",
            distance_at_service_km=basis_gear.get("distance_km") or 0.0,
            date_=fields.get("date") or None,
            notes=fields.get("notes") or None,
        )
    except ValueError as e:
        return (JSONResponse({"error": str(e)}, status_code=400) if is_json
                else RedirectResponse(_error_redirect_url(token, str(e)), status_code=303))

    if is_json:
        return JSONResponse(entry)
    return RedirectResponse(_dashboard_gear_url(token), status_code=303)


async def post_component(request):
    """POST /api/gear/components — add or edit a component definition.

    Accepts either a JSON body or an HTML form post; a form post (the
    dashboard's own Gear-tab UI) redirects back to the Gear tab, a JSON
    request gets JSON back.
    """
    token = request.query_params.get("token")
    is_json = _wants_json(request)
    fields = await _request_fields(request)

    component_id = fields.get("component_id") or None

    interval_raw = fields.get("maintenance_interval_km")
    if interval_raw in (None, ""):
        # _UNSET either way: on an edit that keeps the existing value; on a
        # create it lets upsert_component fall back to the selected Type's
        # (or, failing that, the name's) default interval instead of forcing
        # "no interval" — see upsert_component's docstring.
        interval_km = _UNSET
    else:
        try:
            interval_km = float(interval_raw)
        except (TypeError, ValueError):
            interval_km = _UNSET

    # The "Link component" form's Garmin-gear <select> encodes each option's
    # value as "<uuid>:<name>" so the chosen item's name travels with the
    # submission — sparing a live Garmin lookup here purely to resolve a name
    # the page already had at render time (that lookup was the dashboard's
    # "Link" button feeling stuck/slow). A bare uuid (no colon) still works,
    # e.g. a JSON API caller — upsert_component falls back to its own live
    # lookup in that case. A key that's absent entirely (the plain "Edit"
    # form doesn't carry one) keeps the existing link; present-but-empty (the
    # "Custom, not tracked in Garmin" choice) explicitly clears it.
    linked_raw = fields.get("linked_gear_uuid")
    linked_gear_name = None
    if linked_raw is None:
        linked_gear_uuid = _UNSET
    elif not linked_raw:
        linked_gear_uuid = None
    else:
        linked_gear_uuid, _, linked_gear_name = linked_raw.partition(":")
        linked_gear_name = linked_gear_name or None

    name = fields.get("name") or linked_gear_name or ""

    try:
        component = upsert_component(
            bike_uuid=fields.get("bike_uuid") or "",
            name=name,
            bike_name=fields.get("bike_name") or None,
            install_date=fields.get("install_date") or None,
            install_distance_km=float(fields.get("install_distance_km") or 0.0),
            maintenance_interval_km=interval_km,
            component_id=component_id,
            linked_gear_uuid=linked_gear_uuid,
            component_type=fields.get("component_type") or None,
        )
    except ValueError as e:
        return (JSONResponse({"error": str(e)}, status_code=400) if is_json
                else RedirectResponse(_error_redirect_url(token, str(e)), status_code=303))

    if is_json:
        return JSONResponse(component)
    return RedirectResponse(_dashboard_gear_url(token), status_code=303)


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
    """The gear-tracker API sub-app, mounted by server.py behind token auth."""
    return Starlette(routes=ROUTES)
