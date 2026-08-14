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

`/training-plan` and `/weekly-summary` share a navigation bar so they aren't
dead ends: a horizontal bar across the top on desktop, a bottom tab bar on
mobile, with the current page highlighted and the `?token=` carried into every
link. Its "Gear" entry links straight into the dashboard's Gear tab
(`/dashboard?tab=gear`).

The bar is rendered by `tools/navbar.py` and injected server-side at request
time (never baked into the uploaded plan or report files, so re-uploading picks
up the current nav automatically). Its CSS is scoped under a `#gm-nav` wrapper
so it can't collide with the Svelte plan app or a report's own styling.

`/dashboard` (below) has its own self-contained design with its own tab bar and
doesn't use this shared nav — it links out to the latest weekly report from
its footer instead.

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
server-side on each load. Five tabs:

- **Today** — training readiness (score, level, contributing factors), body
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
| `DASHBOARD_REFRESH_SECONDS` | `300` | Browser auto-refresh interval; set `0` to disable |
| `DASHBOARD_TREND_PERIOD` | `1m` | `get_trends` window backing the Trends tab (`7d`, `14d`, `1m`, …) — the 7d/14d/30d toggle only offers ranges within this window |
| `DASHBOARD_STEP_GOAL` | `10000` | Fallback daily step goal used when there's no active Garmin step goal |

## Training Plan Viewer

The compiled Claude Coach training plan (a self-contained HTML app with its JSON
embedded in a `<script type="application/json" id="plan-data">` tag) can be
hosted from the same container, behind the same `?token=` auth:

| Route | Method | Description |
|---|---|---|
| `/training-plan` | `GET` | Serves the active plan HTML (with the site nav injected), or a "No plan active" page |
| `/training-plan/upload` | `GET` | Minimal two-file upload form (HTML + JSON) |
| `/training-plan/upload` | `POST` | Stores the upload, replacing any existing plan, then redirects to `/training-plan` |
| `/training-plan/reset` | `POST` | Deletes the stored plan files |

```
http://localhost:8000/training-plan/upload?token=YOUR_TOKEN
```

The two files are stored as `plan.html` and `plan.json` in a `training-plan/`
folder inside the same mounted Azure File Share used for the Garmin tokens
(`~/.garminconnect`), so a plan survives container restarts and redeploys. Only
one plan is active at a time and an upload replaces the previous one. Uploads
are validated before being written (the HTML must be non-empty UTF-8 markup and
the JSON must parse), so a bad upload leaves the live plan untouched.

Completion and edit state is kept in the browser's localStorage by the plan app
itself — per-device, no server-side state and no cross-device sync. The stored
HTML is served as uploaded, with only the shared site nav bar injected into its
`<body>`.

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `TRAINING_PLAN_DIR` | `~/.garminconnect/training-plan` | Where the plan files are stored |
| `TRAINING_PLAN_MAX_BYTES` | `20971520` | Per-file upload size cap (20 MB) |

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

Components and maintenance log entries are stored in a SQLite database
(`gear-tracker.db`) in the same mounted Azure File Share used for the Garmin
tokens (`~/.garminconnect`), so they survive container restarts and redeploys.
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
| `GEAR_TRACKER_DB_PATH` | `~/.garminconnect/gear-tracker.db` | Where the database is stored |
| `GEAR_TRACKER_DEFAULT_INTERVALS_KM` | *(built-in table)* | JSON object overriding/extending the default maintenance intervals |

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
│   ├── gear_tracker.py    # storage + API routes + MCP tools for bike component maintenance (dashboard.py's Gear tab)
│   ├── health.py          # get_sleep, get_daily_readiness, get_daily_health, get_training_status, get_training_readiness
│   ├── navbar.py          # shared site nav bar injected into every hosted page
│   ├── performance.py     # get_endurance_score, get_running_tolerance, get_personal_records
│   ├── profile.py         # get_athlete_profile, get_gear
│   ├── training_plan.py   # storage + routes for the hosted plan viewer (/training-plan)
│   ├── trends.py          # get_performance_predictions, get_performance_trends, get_trends
│   ├── weekly_summaries.py # storage + routes for the weekly reports (/weekly-summary)
│   └── workout.py         # get_scheduled_workouts, get_saved_workouts, schedule/unschedule, create_workout, delete_workout, update_workout_weights
├── tests/
│   ├── conftest.py        # Shared fixtures
│   ├── test_activities.py
│   ├── test_challenges.py
│   ├── test_client.py
│   ├── test_dashboard.py
│   ├── test_gear_tracker.py
│   ├── test_health.py
│   ├── test_navbar.py
│   ├── test_performance.py
│   ├── test_profile.py
│   ├── test_training_plan.py
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