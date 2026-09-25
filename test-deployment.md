# Testing & Deployment Guide

## Running the Test Suite

Make sure your virtual environment is active and dependencies are installed:

```bash
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

Run all tests:

```bash
pytest tests/ -v
```

Run a specific test file:

```bash
pytest tests/test_activities.py -v
pytest tests/test_health.py -v
pytest tests/test_profile.py -v
pytest tests/test_client.py -v
```

Run a specific test:

```bash
pytest tests/test_activities.py::test_get_activities_returns_list -v
```

**Note:** Tests make real API calls to Garmin Connect. Avoid running them repeatedly in quick succession to prevent 429 rate limiting.

---

## Running the Server Locally

**Step 1 — Start the server**

```bash
.venv\Scripts\activate
python server.py
```

You should see:
```
INFO: Starting Garmin MCP server on port 8000
INFO: Uvicorn running on http://0.0.0.0:8000
```

**Step 2 — Test with MCP Inspector**

In a separate terminal:

```bash
npx @modelcontextprotocol/inspector
```

In the browser UI that opens:
- **Transport Type:** `Streamable HTTP`
- **URL:** `http://localhost:8000/mcp?token=YOUR_TOKEN`
- **Connection Type:** `Via Proxy`

Click **Connect** → navigate to **Tools** → run any tool to verify.

**Step 3 — Test with Claude Desktop**

Make sure `server.py` is running, then open Claude Desktop and start a new conversation. The Garmin tools will be available automatically via the config in `claude_desktop_config.json`.

If Claude Desktop isn't picking up the tools, check:

```bash
cat "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json"
```

And check the logs at:
```
%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\logs\
```

---

## Building and Pushing a New Docker Image

After making code changes:

```bash
# Build the image
docker build -t ghcr.io/skurtsky/garmin-mcp:latest .

# Login to GitHub Container Registry (first time only)
echo "YOUR_GITHUB_TOKEN" | docker login ghcr.io -u skurtsky --password-stdin

# Push the image
docker push ghcr.io/skurtsky/garmin-mcp:latest
```

**Test the Docker image locally before pushing:**

```bash
docker run --env-file .env -p 8000:8000 ghcr.io/skurtsky/garmin-mcp:latest
```

Then test with the MCP Inspector at `http://localhost:8000/mcp?token=YOUR_TOKEN`.

---

## Deploying Updates to Azure

**Automated:** pushing to `main` triggers `.github/workflows/deploy.yml`, which builds
the image, pushes it to GHCR, and updates the Azure Container App automatically. See
that workflow for setup requirements (Azure OIDC federated credential + GitHub secrets
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`).

**Manual (fallback/reference):** after pushing a new image to GHCR by hand:

```powershell
az containerapp update `
  --name garmin-mcp `
  --resource-group garmin-mcp-rg `
  --image ghcr.io/skurtsky/garmin-mcp:latest `
  --set-env-vars DEPLOY_TIME="$(Get-Date -Format 'yyyyMMddHHmmss')"
```

Azure performs a zero-downtime rolling restart automatically.

Verify the update deployed correctly:

```powershell
az containerapp logs show `
  --name garmin-mcp `
  --resource-group garmin-mcp-rg `
  --follow
```

---

## Refreshing Garmin Tokens

Garmin OAuth tokens expire periodically. **In most cases the server now recovers on its own:** when a Garmin call returns a 401 it reloads `garmin_tokens.json` from the file share (the `garmin-sync` job may have saved fresher tokens), falls back to a fresh login with `GARMIN_EMAIL` / `GARMIN_PASSWORD` if that doesn't work, and retries the call once. No restart needed.

The manual steps below are only needed when the logs show `Garmin fresh login failed: …` — usually because Garmin wants MFA or is rate-limiting logins (429). Tool calls then fail with `Garmin session expired and automatic re-login failed (…)`. After a failed login the server waits `GARMIN_RELOGIN_COOLDOWN_SECONDS` (default 900) before trying another fresh login, so it doesn't hammer Garmin's SSO.

**Step 1 — Re-authenticate locally**

```bash
python -c "
from garminconnect import Garmin
import os
from dotenv import load_dotenv
load_dotenv()
client = Garmin(os.environ['GARMIN_EMAIL'], os.environ['GARMIN_PASSWORD'])
client.login()
client.client.dump(os.path.expanduser('~/.garminconnect'))
print('Tokens refreshed')
"
```

**Step 2 — Upload new tokens to Azure File Share**

```powershell
$STORAGE_KEY = az storage account keys list `
  --account-name garminmcpkurt `
  --resource-group garmin-mcp-rg `
  --query "[0].value" -o tsv

az storage file upload `
  --share-name garminconnect `
  --account-name garminmcpkurt `
  --account-key $STORAGE_KEY `
  --source "$env:USERPROFILE\.garminconnect\garmin_tokens.json" `
  --path garmin_tokens.json `
  --overwrite true
```

**Step 3 — Restart the container**

A restart is no longer strictly required: the next 401 reloads the uploaded tokens from disk, even during the login cooldown. Restarting is still the guaranteed way to pick them up immediately:

```powershell
az containerapp revision restart `
  --name garmin-mcp `
  --resource-group garmin-mcp-rg `
  --revision $(az containerapp show `
    --name garmin-mcp `
    --resource-group garmin-mcp-rg `
    --query "properties.latestRevisionName" -o tsv)
```

---

## Checking Server Health

**Check running status:**

```powershell
az containerapp show `
  --name garmin-mcp `
  --resource-group garmin-mcp-rg `
  --query "properties.runningStatus"
```

**Stream live logs:**

```powershell
az containerapp logs show `
  --name garmin-mcp `
  --resource-group garmin-mcp-rg `
  --follow
```

**Quick connectivity test via MCP Inspector:**

- **URL:** `DEPLOYED_URL/mcp?token=YOUR_TOKEN`
- **Transport Type:** `Streamable HTTP`
- **Connection Type:** `Via Proxy`

---

## Publishing a Training Plan

Training plans are stored in the PostgreSQL database (`DATABASE_URL`), not on
the file share. The tables (`training_plans`, `training_plan_revisions`,
`training_plan_workout_state`) are created automatically when the container
starts — there's no manual migration step. `docs/postgres-schema.sql` has the
same DDL if you ever want to create them by hand.

1. Open `DEPLOYED_URL/training-plan/upload?token=YOUR_TOKEN`
2. Pick the plan `.json` the coach skill produced and submit. A new plan id
   becomes the active plan (the previous one is archived); an id that's already
   stored shows what replacing it would change and asks you to confirm.

Older plans are listed at `DEPLOYED_URL/training-plan/plans?token=YOUR_TOKEN`
(view read-only, download, make active again, or delete).

**Checking the tables from Azure Cloud Shell (PowerShell)** — optional, after
the first deploy. Cloud Shell has `psql` built in. If the server's firewall
doesn't already allow all IPs, add Cloud Shell's address first and remove it
afterwards:

```powershell
$ip = Invoke-RestMethod https://api.ipify.org
az postgres flexible-server firewall-rule create `
  --resource-group garmin-mcp-rg --name garmin-mcp-db `
  --rule-name cloudshell-temp --start-ip-address $ip --end-ip-address $ip

psql "host=garmin-mcp-db.postgres.database.azure.com dbname=garmin user=garminadmin sslmode=require"
#   \dt training_plan*
#   SELECT id, status, version, updated_at FROM training_plans;
#   SELECT version, source, summary, created_at FROM training_plan_revisions ORDER BY created_at DESC LIMIT 10;

az postgres flexible-server firewall-rule delete `
  --resource-group garmin-mcp-rg --name garmin-mcp-db --rule-name cloudshell-temp --yes
```

Plans uploaded before this change (the `training-plan/` folder on the file
share) are no longer read; delete that folder whenever convenient.

---

## Maintenance Summary

| Task | Trigger | Effort |
|---|---|---|
| Run tests | Before any commit | `pytest tests/ -v` |
| Build & push image | After code changes | 2 commands |
| Deploy to Azure | After pushing image | 1 command |
| Refresh Garmin tokens | Auth errors appear | ~5 minutes |
| Publish a training plan | New plan generated | Upload the plan JSON in the browser |
| Check logs | Something broken | 1 command |