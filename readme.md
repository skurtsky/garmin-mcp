# Garmin MCP Server

A Model Context Protocol (MCP) server that connects to Garmin Connect and exposes
fitness and health data as tools for Claude and other MCP-compatible clients.

Built on [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
and [FastMCP](https://github.com/jlowin/fastmcp).

## Tools

### Profile & Gear

| Tool | Description |
|---|---|
| `athlete_profile` | Weight, height, VO2max (running & cycling), lactate threshold HR and pace, FTP, and 7-day average resting HR |
| `gear` | Registered gear (shoes, bikes, etc.) with distance, activity count, and status |

### Activities

| Tool | Description |
|---|---|
| `recent_activities` | Recent activity list with summary metrics; supports sport type filter and date range |
| `activity_detail` | Full activity detail with lap splits, structured-workout interval/phase breakdown, HR zones, and weather conditions; multisport activities also break out each leg (swim / T1 / bike / T2 / run) under `sub_activities`; `gear` lists the shoes/bike used with their cumulative distance, per-leg for multisport |
| `weekly_summary` | Aggregated activity totals for a Monday–Sunday week with per-sport breakdown |
| `activity_summary` | Aggregated training stats (distance, duration, calories, elevation) over any date range, optionally filtered by sport with a per-sport breakdown |
| `personal_records` | Personal records for running, cycling, and swimming grouped by sport |
| `swim_records` | Longest unbroken swim sets (continuous distance, not session total) across recent swims, ranked by distance |

### Workouts

| Tool | Description |
|---|---|
| `get_scheduled_workouts` | Upcoming scheduled running workouts from calendar items |
| `get_saved_workouts` | Saved workout library with optional sport filter |
| `schedule_workout` | Schedule an existing workout ID to a date |
| `unschedule_workout` | Remove a scheduled workout from calendar |
| `create_workout` | Create a workout (running, cycling, strength_training, cardio) from step definitions and optionally schedule it |
| `delete_workout` | Delete a saved workout by ID |
| `update_workout_weights` | Update exercise weights, per-set notes, and/or the workout-level description in a strength workout by name — uploads a new version and deletes the old one |

### Health & Recovery

| Tool | Description |
|---|---|
| `sleep` | Sleep stages, score, HRV, and recovery metrics for a given date |
| `daily_readiness` | HRV, body battery levels, and daily stress and activity stats |
| `daily_health` | Resting/max/min heart rate, all-day stress zones, body battery charged/drained, and respiration rate |
| `training_readiness` | Composite readiness score (0–100) with contributing factors (sleep, HRV, ACWR, stress) |

### Training & Performance

| Tool | Description |
|---|---|
| `training_status` | Acute:chronic workload ratio, load balance, training status phrase, and current VO2max |
| `performance_predictions` | Race time predictions for 5K, 10K, half marathon, and marathon |
| `performance_trends` | Weekly or monthly trends for HRV and VO2max over a lookback period |
| `get_trends` | Pre-aggregated daily metrics (RHR, HRV, sleep, body battery, stress, steps, training load) over a window with rolling 7d/28d averages, start→end deltas, and min/max |
| `endurance_score` | Endurance score, classification (beginner → elite), and per-sport contribution breakdown |
| `running_tolerance` | Running load tolerance with weekly load bounds and acute/chronic load for a date range |

### Goals & Motivation

| Tool | Description |
|---|---|
| `active_goals` | Step / distance / activity goals with target, current progress, and progress percentage (`goal_type`: active, future, or past) |
| `earned_badges` | Earned challenge/achievement badges with points, category, and date earned |
| `adhoc_challenges` | Ad-hoc / community challenges with date range, personal ranking, and player count |

### Training Plan

`get_training_plan`, `list_training_plans`, `amend_training_plan`,
`complete_plan_workout`, `link_plan_workout_to_garmin`,
`get_training_plan_revisions` and `restore_training_plan_revision` read and
edit the Claude Coach plan stored in PostgreSQL — see
[Training Plan](#training-plan).

### Reports

| Tool | Description |
|---|---|
| `upload_weekly_summary` | Publish a weekly training report (full HTML) for a Monday-started week, readable at `/weekly-summary` |

### Gear Maintenance

| Tool | Description |
|---|---|
| `log_maintenance` | Log a maintenance action (lubed, replaced, serviced, ...) against a tracked bike component, auto-creating it on first use |
| `get_maintenance_status` | Last serviced date, distance since service, interval, and status for every tracked component, optionally scoped to one bike |

## Setup

**1. Clone and install**

```bash
git clone https://github.com/skurtsky/garmin-mcp.git
cd garmin-mcp
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. Configure environment**

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

`.env` variables:

```
GARMIN_EMAIL=you@email.com
GARMIN_PASSWORD=yourpassword
MCP_BEARER_TOKEN=your-generated-token
REQUESTS_CA_BUNDLE=        # Corporate networks only — path to CA bundle
```

Generate a bearer token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**3. Run the server**

```bash
python server.py
```

The server starts on `http://0.0.0.0:8000` by default. Set the `PORT` environment
variable to override.

## Site Navigation

Every hosted page — `/dashboard`, `/training-plan` and `/weekly-summary` —
shares one navigation, so the dashboard and the training plan work as one app:

    Today · Plan · Trends · Activity · More
    More → Fitness, Gear, Weekly Summary, Plan PDF, Settings

On a phone it's a floating bottom pill; from 900px wide it becomes a side rail
with everything visible. The current page is highlighted and the `?token=` is
carried into every link. Today, Trends, Activity, Fitness and Gear are
dashboard tabs; Plan and Settings are the plan viewer.

The nav is rendered by `tools/navbar.py`. The plan viewer and weekly reports
get it injected server-side at request time (never baked into the uploaded
plan or report files), with its CSS scoped under a `#gm-nav` wrapper so it
can't collide with a page's own styling; the dashboard renders it with its tab
items as labels for its CSS tab radios.

### Plan ↔ Garmin

Each planned workout is linked to the Garmin activity that did it, which is
what lets the dashboard's **Today** screen show today's session next to the
ride or run that completed it, and the plan show "Garmin · 52 min" on a done
workout:

- `sync_garmin.py` ticks off the planned workout a newly synced activity
  completed (same date and sport; the closest planned duration when there are
  several). Only first-time syncs match, so un-ticking a workout is never undone.
- A workout ticked by hand (in the viewer, or before matching existed) gets its
  activity attached on the next sync, or straight away when ticked in the viewer.
- When a matched **FTP test** ride has power, Today and Fitness offer "Update
  FTP from test": 95% of the best 20 minutes, written back to the plan as a
  normal (restorable) revision, so its zones and power targets recalculate.

The **Fitness** page shows each threshold as Garmin sees it next to the value
the plan uses — tagged Tested, Provisional or Unvalidated from the plan's own
source notes and zone validation — and the plan's swim / bike / run zones.

## Dashboard

A server-rendered health dashboard is available at `/dashboard` — the same
container and the same bearer-token auth as the MCP endpoint, just a different
route (no extra deployment needed). Open it in a browser with the token as a
query param:

```
http://localhost:8000/dashboard?token=YOUR_TOKEN
```

It's a single self-contained HTML page (inline CSS, no external requests —
`?tab=` selects which one opens, and switching between them client-side is
pure CSS via `:checked` radio inputs, no JS) that fetches fresh data
server-side on each load — all sections concurrently (a thread pool), since
they're independent Garmin API calls with no batch/range endpoint for most of
them; a cold load is still on the order of a few seconds rather than the
minute-plus a fully serial fetch would take. Five tabs:

- **Today** — training readiness (score, level, contributing factors),
  training status (Peaking/Productive/Maintaining/etc., colour-coded, with a
  daily history bar toggling between 7d and 28d) and load ratio, body
  battery, steps vs. goal, resting HR with a 14-day sparkline, HRV status,
  last night's sleep (stages + stats), this week's load by day, and today's
  activities.
- **Trends** — acute:chronic training-load ratio, and HRV / resting HR /
  sleep score / acute load / stress / steps sparkline cards over a 7d/14d/30d
  toggle, plus a 14-day daily-steps chart.
- **Activity** — this week's totals and a by-sport time split, plus a
  scrollable recent-activities list with per-activity pace/speed/HR/load on
  expand.
- **Fitness** — VO₂max (run/bike), thresholds (LTHR/LT pace/FTP/weight),
  approximate heart-rate zones derived from LTHR, and personal records
  grouped by sport with a filter.
- **Gear** — bike component maintenance tracking; see
  [Gear Tracker](#gear-tracker) below.

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_TZ_OFFSET_HOURS` | `0` | Offset from UTC for the "today" date and displayed local time (e.g. `-4`) |
| `DASHBOARD_REFRESH_SECONDS` | `300` | How old the dashboard can get before coming back to the app refreshes it; set `0` to disable |
| `DASHBOARD_TREND_PERIOD` | `14d` | `get_trends` window backing the Trends tab (`7d`, `14d`, `1m`, …) — the 7d/14d/30d toggle only offers ranges within this window. `get_trends` fetches its per-day metrics concurrently, but there's no batch endpoint for most of them, so wider windows still add latency; `1m` (30d) restores the full toggle at the cost of a slower load |

## Training Plan

Claude Coach training plans live in PostgreSQL (the same `DATABASE_URL` the
dashboard uses — the tables are created on startup), so the plan, completion
ticks, edits and zones are the same on every device. The coach skill produces
a plan **JSON**; upload it once and the server renders it inside the plan
viewer (`tools/assets/plan-viewer.html`) on every request.

```
http://localhost:8000/training-plan/upload?token=YOUR_TOKEN
```

- **One active plan per id.** A plan is keyed by its `meta.id`. Uploading a new
  id makes it active and archives the previous plan; archived plans stay
  viewable read-only from `/training-plan/plans` and can be made active again.
- **Re-uploading the same id** shows a confirmation first: how many completed
  workouts keep their tick, any completed workouts missing from the new file,
  and any web/MCP edits since the last upload that the file would overwrite.
  The unit choice carries over; zone overrides reset to the file's zones.
- **Edits in the viewer** — tick a workout, drag it to another day, edit it,
  delete it, or add one with a day's **+** button — save to the server
  immediately. Zone thresholds and validation flags on Settings are stored with
  the plan, so the PDF and the coach see them too.
- **Weekly totals** (sessions, hours, km per sport) are recomputed from the
  workouts on every upload and edit, never taken from the file.
- **History.** Every change (upload, edit, MCP amendment, restore) is saved as
  a revision; Settings → History lists them and restores any one (as a new
  revision, so a restore can be undone). Completion ticks are stored separately
  and never roll back.

| Route | Method | Description |
|---|---|---|
| `/training-plan` | `GET` | The viewer for the active plan (`?plan=<id>` for an archived one, read-only), or a "No plan active" page |
| `/training-plan/plans` | `GET` | Every stored plan, with view / download / activate / archive / delete |
| `/training-plan/plans/{id}/{activate\|archive\|delete}` | `POST` | Plan lifecycle (only archived plans can be deleted) |
| `/training-plan/upload` | `GET` / `POST` | Upload a plan JSON (validated; same-id uploads confirm first) |
| `/training-plan/export.json` | `GET` | The plan JSON as currently edited |
| `/training-plan/pdf` | `GET` | Printable wall-chart PDF of the stored plan |
| `/training-plan/api/plan` | `GET` | Plan + version, status, completion (used by the viewer) |
| `/training-plan/api/operations` | `POST` | `{"operations": [...]}` — the same edit operations as `amend_training_plan` |
| `/training-plan/api/completion` | `POST` | `{"workout_id", "completed"}` |
| `/training-plan/api/revisions` | `GET` | Revision history |
| `/training-plan/api/revisions/{version}/restore` | `POST` | Restore a revision |

### Training plan MCP tools

| Tool | Description |
|---|---|
| `get_training_plan` | Plan meta, current thresholds, phases, a one-line summary per week, and workout detail for the current + next week (or a given week / date range), with completion and Garmin-link state |
| `list_training_plans` | Every stored plan and its status |
| `amend_training_plan` | Atomic edits with a reason: add / update / move / remove workouts, edit a week, set zones or units |
| `complete_plan_workout` | Mark a planned workout complete by id, or from a Garmin activity (matched by date and sport) |
| `link_plan_workout_to_garmin` | Record the Garmin Connect workout scheduled for a planned workout, so it isn't scheduled twice |
| `get_training_plan_revisions` / `restore_training_plan_revision` | History and undo |

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `TRAINING_PLAN_MAX_BYTES` | `20971520` | Upload size cap (20 MB) |

## Weekly Training Reports

Weekly training reports are generated elsewhere (Claude Cowork) and pushed in
with the `upload_weekly_summary` MCP tool, then read in the browser behind the
same `?token=` auth:

```python
upload_weekly_summary(
    week_start_date="2026-08-03",  # Monday of the training week
    html_content="<!doctype html>…",  # the full styled report
)
# → {"url": "/weekly-summary/20260803", "week": "2026-08-03"}
```

`week_start_date` must be a Monday — any other weekday is rejected (with the
correct Monday named in the error) so a week can never end up with two files.
Uploading the same week again overwrites it, which makes regeneration safe.

| Route | Method | Description |
|---|---|---|
| `/weekly-summary` | `GET` | Serves the most recent report, or a placeholder when none exist |
| `/weekly-summary/{YYYYMMDD}` | `GET` | Serves one week's report (e.g. `/weekly-summary/20260803`) |
| `/weekly-summary/list` | `GET` | JSON array of the available weeks, newest first |

```
http://localhost:8000/weekly-summary?token=YOUR_TOKEN
```

Each report is stored verbatim as `{YYYYMMDD}.html` in a `weekly-summaries/`
folder inside the same mounted Azure File Share used for the Garmin tokens
(`~/.garminconnect`), so reports survive container restarts and redeploys.
Nothing is auto-deleted — a year of reports is a few MB.

A week switcher (previous / next week plus a dropdown of every week) is injected
server-side when a report is served — below the shared site nav bar — so the
uploaded HTML doesn't need to know which other weeks exist. The `/list` route
returns the same week metadata as JSON for building navigation elsewhere.

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `WEEKLY_SUMMARY_DIR` | `~/.garminconnect/weekly-summaries` | Where the reports are stored |
| `WEEKLY_SUMMARY_MAX_BYTES` | `20971520` | Per-report size cap (20 MB) |

## Gear Tracker

Garmin's `gear` tool reports cumulative distance per piece of gear (shoes,
bikes) but knows nothing about individual wear components on a bike — chain,
brake pads, tires, and so on — or when they were last serviced. The gear
tracker fills that gap, as the **Gear** tab on the [dashboard](#dashboard):

```
http://localhost:8000/dashboard?tab=gear&token=YOUR_TOKEN
```

(`/dashboard/gear` — its old standalone URL — redirects here.)

It shows an overview card per registered piece of active gear (distance, time,
a status dot), a component table per bike (last serviced, distance since
service, interval, and a status indicator), and a scrollable maintenance log —
all editable in-page via plain HTML forms (no JavaScript). Distance is always
computed live: `current gear distance − distance at last service` (or install
distance, if never serviced) — only maintenance records and component
definitions are stored locally, never a gear's distance itself.

Shoes use fixed distance bands (green `<500 km`, yellow `500–650`, orange
`650–750`, red `>750`); bike components use a wear ratio against their
maintenance interval (green `<60%`, yellow `60–100%`, red `≥100%`, unknown
when no interval is set), and a bike's own status rolls up to its
worst-tracked component.

| Route | Method | Description |
|---|---|---|
| `/dashboard/gear` | `GET` | Redirects to `/dashboard?tab=gear` |
| `/api/gear/components` | `GET` | Components + live maintenance status as JSON; optional `?gear_name=` filter |
| `/api/gear/components` | `POST` | Add or edit a component definition (JSON body, or the Gear tab's own forms) |
| `/api/gear/maintenance` | `POST` | Log a maintenance action (JSON body, or the Gear tab's own forms) |

`POST` routes accept either a JSON body (returns JSON) or an HTML form post
(redirects back to the Gear tab) — the same endpoints back both the in-page
forms and programmatic use.

Components and maintenance log entries are stored in a JSON file
(`gear-tracker/gear_data.json`) in the same mounted Azure File Share used for
the Garmin tokens (`~/.garminconnect`), so they survive container restarts and
redeploys. Every request reads the file fresh (it's tiny) and every write is
atomic, so there's nothing to lock — a SQLite database used to live here, but
SQLite's own locking doesn't work on this file share (SMB, no POSIX file
locks), and a stuck lock took the whole server down along with it (issue #60).
Creating a component without an explicit interval picks up a default matched
case-insensitively by name (chain 400 km, brake pads 2000 km, tires 5000 km,
chain ring / cassette 8000 km, bar tape untracked) — override the whole table
with `GEAR_TRACKER_DEFAULT_INTERVALS_KM` (a JSON object), or set a component's
own interval via its edit form or the components API.

The `log_maintenance` and `get_maintenance_status` MCP tools (above) expose
the same operations to the assistant, so a coach prompt can flag overdue
maintenance and log it once confirmed — `log_maintenance` auto-creates an
untracked component on first use, picking up its default interval when the
name matches a known one.

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `GEAR_TRACKER_DATA_PATH` | `~/.garminconnect/gear-tracker/gear_data.json` | Where the data file is stored |
| `GEAR_TRACKER_DEFAULT_INTERVALS_KM` | *(built-in table)* | JSON object overriding/extending the default maintenance intervals |

## Database-First Tool Reads

When `DATABASE_URL` is set (the same PostgreSQL the dashboard and the
`sync_garmin.py` job use), these MCP tools read from the database before
calling Garmin Connect:

| Tool | Database source |
|---|---|
| `sleep`, `daily_health`, `daily_readiness`, `training_readiness`, `training_status` | The day's `daily_metrics` row, which stores each tool's output exactly as returned |
| `get_trends` | `daily_metrics` for every synced day in the window; only the rest are fetched live |

A synced day is used when it's **final** (synced after that day ended in
local time, per `DASHBOARD_TZ_OFFSET_HOURS`) or **fresh** (synced within
`MCP_DB_MAX_AGE_SECONDS`). Otherwise the tool calls Garmin as before. If that
live call fails and an older synced copy exists, the older copy is returned,
marked `"stale": true`. Every response includes a `data_source` block
(`source`: `db`, `live` or `db+live`, plus `synced_at`, and for `get_trends`
`db_days` / `live_days`).

`get_trends` over long windows is only fast after the history has been
backfilled, e.g. `python sync_garmin.py --daily-only --since 2025-09-01`.
The sync job itself always calls Garmin directly.

| Variable | Default | Description |
|---|---|---|
| `MCP_DB_FIRST` | `1` | Set `0` to make every tool call Garmin live |
| `MCP_DB_MAX_AGE_SECONDS` | `900` | How recently today's (still-changing) row must have been synced to be used |

## Testing

### Run the test suite

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

### Test with MCP Inspector

The MCP Inspector lets you interactively call tools against the running server.

**Step 1** — Start the server:

```bash
python server.py
```

**Step 2** — In a separate terminal, launch the inspector:

```bash
npx @modelcontextprotocol/inspector
```

**Step 3** — In the browser UI that opens, configure the connection:

- **Transport Type:** `Streamable HTTP`
- **URL:** `http://localhost:8000/mcp?token=YOUR_TOKEN`
- **Connection Type:** `Via Proxy`

**Step 4** — Click **Connect**, navigate to **Tools**, and run any tool.

## Project Structure

```
garmin-mcp/
├── server.py              # FastMCP server — tool definitions and entrypoint
├── garmin_client.py       # Authenticated Garmin client singleton
├── tools/
│   ├── activities.py      # get_activities, get_activity, get_activity_summary, get_weekly_summary, get_swim_records
│   ├── challenges.py      # get_active_goals, get_earned_badges, get_adhoc_challenges
│   ├── dashboard.py       # build_dashboard_data, render_dashboard_html (/dashboard route)
│   ├── db_first.py        # database-first reads for the per-day health tools and get_trends
│   ├── gear_tracker.py    # storage + API routes + MCP tools for bike component maintenance (dashboard.py's Gear tab)
│   ├── health.py          # get_sleep, get_daily_readiness, get_daily_health, get_training_status, get_training_readiness
│   ├── navbar.py          # the one site nav (bottom pill / desktop rail) on every hosted page
│   ├── performance.py     # get_endurance_score, get_running_tolerance, get_personal_records
│   ├── profile.py         # get_athlete_profile, get_gear
│   ├── plan_doc.py        # training-plan document rules: validation, weekly totals, edit operations
│   ├── plan_service.py    # training-plan storage (PostgreSQL) + completion / Garmin links + auto-matching
│   ├── plan_today.py      # the active plan as the dashboard's Today / Fitness screens show it
│   ├── plan_tools.py      # training-plan MCP tools
│   ├── training_plan.py   # /training-plan routes: viewer, upload, plans list, JSON API
│   ├── assets/plan-viewer.html  # the plan viewer app the server fills with a stored plan
│   ├── trends.py          # get_performance_predictions, get_performance_trends, get_trends
│   ├── weekly_summaries.py # storage + routes for the weekly reports (/weekly-summary)
│   └── workout.py         # get_scheduled_workouts, get_saved_workouts, schedule/unschedule, create_workout, delete_workout, update_workout_weights
├── tests/
│   ├── conftest.py        # Shared fixtures
│   ├── test_activities.py
│   ├── test_challenges.py
│   ├── test_client.py
│   ├── test_dashboard.py
│   ├── test_db_first.py
│   ├── test_gear_tracker.py
│   ├── test_health.py
│   ├── test_navbar.py
│   ├── test_performance.py
│   ├── test_profile.py
│   ├── test_plan_doc.py
│   ├── test_plan_tools.py
│   ├── test_training_plan.py
│   ├── test_training_plan_db.py  # real PostgreSQL; set TEST_DATABASE_URL
│   ├── test_trends.py
│   ├── test_weekly_summaries.py
│   └── test_workout.py
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

## Deployment

See the [deployment guide](test-deployment.md) for Azure Container Apps setup
including token persistence and SSL configuration.

## Notes

- Garmin's API is unofficial and reverse-engineered — it may change without notice
- The `python-garminconnect` library handles authentication via the Garmin mobile
  SSO flow and stores OAuth tokens in `~/.garminconnect/garmin_tokens.json`
- Token persistence in containerized environments requires mounting a volume or
  storing token JSON in a secret — see deployment notes
- Garmin 401s recover automatically: the client reloads tokens from disk, then
  falls back to a fresh login, and retries the call once. After a failed fresh
  login (MFA, 429) it waits `GARMIN_RELOGIN_COOLDOWN_SECONDS` (default `900`)
  before logging in again — see "Refreshing Garmin Tokens" in the
  [deployment guide](test-deployment.md) for when manual steps are still needed