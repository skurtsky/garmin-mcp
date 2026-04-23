# Garmin MCP Server

A Model Context Protocol (MCP) server that connects to Garmin Connect and exposes
fitness and health data as tools for Claude and other MCP-compatible clients.

Built on [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
and [FastMCP](https://github.com/jlowin/fastmcp).

## Tools

| Tool | Description |
|---|---|
| `athlete_profile` | Weight, VO2max, lactate threshold HR and pace, FTP |
| `recent_activities` | Recent activity list with summary metrics |
| `activity_detail` | Full activity detail with lap splits and HR zones |
| `sleep` | Sleep quality and recovery metrics for a given date |
| `daily_readiness` | HRV, body battery, and training status for a given date |

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
│   ├── activities.py      # get_activities, get_activity
│   ├── health.py          # get_sleep, get_daily_readiness
│   └── profile.py         # get_athlete_profile
├── tests/
│   ├── conftest.py        # Shared fixtures
│   ├── test_client.py
│   ├── test_activities.py
│   ├── test_health.py
│   └── test_profile.py
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