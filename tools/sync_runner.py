# tools/sync_runner.py
"""Run sync_garmin.py from Settings → Garmin Sync, in the background.

A run is the same command the scheduled Container Apps Job runs, started as
a child process of the web server with the flags picked in Settings. Its
output is streamed into the ``sync_runs`` table every couple of seconds, so
the page can leave and come back (or load on another replica) and still
show the run's progress and log. Only one run goes at a time.

A supervisor thread per run owns the child process: every tick it saves
new output, beats the run's heartbeat, and stops the child if Cancel was
pressed (a flag in the row, so Cancel works from any replica). A run whose
heartbeat stops — the server restarted mid-run — reads as interrupted.
"""
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import date

import db

logger = logging.getLogger(__name__)

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync_garmin.py")
TICK_S = 2
STALE_AFTER_S = 120           # no heartbeat for this long: the run's server is gone
CANCEL_GRACE_S = 10           # SIGTERM, then SIGKILL after this long

# What Settings can run, and the sync_garmin.py flags each mode uses.
MODES = {
    "full": "Everything (like the scheduled sync)",
    "daily": "Daily metrics only",
    "activities": "Activities only",
    "details": "Activity details only",
    "history": "Threshold & race-prediction history",
}
_MODE_FLAG = {"daily": "--daily-only", "activities": "--activities-only",
              "details": "--details-only", "history": "--backfill-history"}
_MODE_FIELDS = {
    "full": ("days", "since", "end_date", "activities_since", "activities_end_date", "detail_limit", "overwrite"),
    "daily": ("days", "since", "end_date"),
    "activities": ("activities_since", "activities_end_date"),
    "details": ("detail_limit", "overwrite"),
    "history": ("history_days",),
}
_INT_FIELDS = {"days": ("--days", 1, 3650), "detail_limit": ("--detail-limit", 1, 10000),
               "history_days": ("--history-days", 1, 365)}
_DATE_FIELDS = {"since": "--since", "end_date": "--end-date",
                "activities_since": "--activities-since", "activities_end_date": "--activities-end-date"}


class SyncRunError(Exception):
    """Bad options, or a run already going — shown to the user as is."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def build_argv(options: dict) -> tuple[dict, list[str]]:
    """The cleaned options and the sync_garmin.py arguments they stand for.
    Fields that don't apply to the mode are dropped; blanks mean the
    script's own default."""
    mode = options.get("mode") or "full"
    if mode not in MODES:
        raise SyncRunError(f"Unknown sync mode {mode!r}.")
    clean, argv = {"mode": mode}, []
    if mode in _MODE_FLAG:
        argv.append(_MODE_FLAG[mode])
    for field in _MODE_FIELDS[mode]:
        value = options.get(field)
        if value is None or value is False or value == "":
            continue
        if field == "overwrite":
            if value is not True:
                raise SyncRunError("Overwrite is on or off.")
            clean[field] = True
            argv.append("--overwrite")
        elif field in _INT_FIELDS:
            flag, lo, hi = _INT_FIELDS[field]
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise SyncRunError(f"{flag} needs a whole number.") from None
            if not lo <= n <= hi:
                raise SyncRunError(f"{flag} must be between {lo} and {hi}.")
            clean[field] = n
            argv += [flag, str(n)]
        else:
            flag = _DATE_FIELDS[field]
            try:
                clean[field] = date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise SyncRunError(f"{flag} needs a date (YYYY-MM-DD).") from None
            argv += [flag, clean[field]]
    for start, end in (("since", "end_date"), ("activities_since", "activities_end_date")):
        if clean.get(start) and clean.get(end) and clean[start] > clean[end]:
            raise SyncRunError(f"{_DATE_FIELDS[start]} must be on or before {_DATE_FIELDS[end]}.")
    if clean.get("activities_end_date") and not clean.get("activities_since"):
        raise SyncRunError("--activities-end-date needs --activities-since.")
    return clean, argv


def command_line(argv: list[str]) -> str:
    return shlex.join(["python", "sync_garmin.py", *argv])


def start(options: dict) -> dict:
    """Start a run in the background; returns its row."""
    if not db.is_configured():
        raise SyncRunError("Syncing needs the database — set DATABASE_URL.", 503)
    clean, argv = build_argv(options)
    run = db.create_sync_run(clean, argv, STALE_AFTER_S)
    if run is None:
        raise SyncRunError("A sync is already running — wait for it to finish, or cancel it.", 409)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", SCRIPT, *argv],
            cwd=os.path.dirname(SCRIPT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except OSError as e:
        db.finish_sync_run(run["id"], "failed", None, f"Could not start the sync: {e}\n")
        raise SyncRunError(f"Could not start the sync: {e}", 500) from e
    logger.info("Sync run %s started: %s", run["id"], command_line(argv))
    threading.Thread(target=_supervise, args=(run["id"], proc), daemon=True,
                     name=f"sync-run-{run['id']}").start()
    return run


def _supervise(run_id: int, proc: subprocess.Popen):
    lines: list[str] = []
    lock = threading.Lock()

    def read():
        for line in proc.stdout:
            with lock:
                lines.append(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def flush() -> bool:
        with lock:
            text = "".join(lines)
            lines.clear()
        try:
            return db.append_sync_run_log(run_id, text)
        except Exception:  # noqa: BLE001 — a DB blip mustn't orphan the child
            logger.exception("Could not save sync run %s output", run_id)
            return False

    cancelled_at = None
    while proc.poll() is None:
        time.sleep(TICK_S)
        if flush() and cancelled_at is None:
            cancelled_at = time.monotonic()
            with lock:
                lines.append("\n[Cancel requested — stopping the sync.]\n")
            proc.terminate()
        if cancelled_at is not None and time.monotonic() - cancelled_at > CANCEL_GRACE_S:
            proc.kill()
    reader.join(timeout=5)
    with lock:
        tail = "".join(lines)
    code = proc.returncode
    status = "cancelled" if cancelled_at is not None else "succeeded" if code == 0 else "failed"
    try:
        db.finish_sync_run(run_id, status, code, tail)
    except Exception:  # noqa: BLE001
        logger.exception("Could not record the end of sync run %s", run_id)
    logger.info("Sync run %s %s (exit %s)", run_id, status, code)


def cancel(run_id: int):
    if not db.request_sync_run_cancel(run_id):
        raise SyncRunError("That sync isn't running.", 409)


# ── what Settings shows ─────────────────────────────────────────────────────

_DATA_TYPE_LABELS = {
    "daily_metrics": "Daily metrics", "activities": "Activities",
    "activity_details": "Activity details", "personal_records": "Personal records",
    "athlete_profile": "Athlete profile", "threshold_snapshots": "Threshold history",
    "active_goals": "Goals", "gear": "Gear",
}


def _iso(value):
    return value.isoformat() if value is not None else None


def run_view(row: dict, with_log: bool = False) -> dict:
    view = {
        "id": row["id"], "options": row["options"], "command": command_line(row["argv"]),
        "status": row["status"], "exitCode": row["exit_code"],
        "cancelRequested": row["cancel_requested"],
        "startedAt": _iso(row["started_at"]), "finishedAt": _iso(row["finished_at"]),
    }
    if with_log:
        view["log"] = row.get("log") or ""
    return view


def overview() -> dict:
    """Settings → Garmin Sync: when each kind of data last synced, the
    recent runs, and the newest run's log."""
    state = db.get_sync_state()
    data = sorted(
        ({"key": key, "label": _DATA_TYPE_LABELS.get(key, key.replace("_", " ").capitalize()),
          "lastSyncTime": _iso(row.get("last_sync_time")),
          "lastSyncedDate": _iso(row.get("last_synced_date")),
          "status": row.get("status"), "error": row.get("error_message")}
         for key, row in state.items()),
        key=lambda r: list(_DATA_TYPE_LABELS).index(r["key"]) if r["key"] in _DATA_TYPE_LABELS else 99,
    )
    times = [r["lastSyncTime"] for r in data if r["lastSyncTime"]]
    runs = db.list_sync_runs(15, STALE_AFTER_S)
    latest = db.get_sync_run(runs[0]["id"]) if runs else None
    return {
        "lastSync": max(times) if times else None,
        "dataTypes": data,
        "runs": [run_view(r) for r in runs],
        "latest": run_view(latest, with_log=True) if latest else None,
        "modes": [{"key": k, "label": v} for k, v in MODES.items()],
    }


def run_detail(run_id: int) -> dict:
    row = db.get_sync_run(run_id)
    if row is None:
        raise SyncRunError("No such sync run.", 404)
    return run_view(row, with_log=True)
