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

After pushing a new image to GHCR:

```powershell
az login

az containerapp update `
  --name garmin-mcp `
  --resource-group garmin-mcp-rg `
  --image ghcr.io/skurtsky/garmin-mcp:latest
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

Garmin OAuth tokens expire periodically. If tool calls start returning authentication errors, refresh the tokens:

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

## Maintenance Summary

| Task | Trigger | Effort |
|---|---|---|
| Run tests | Before any commit | `pytest tests/ -v` |
| Build & push image | After code changes | 2 commands |
| Deploy to Azure | After pushing image | 1 command |
| Refresh Garmin tokens | Auth errors appear | ~5 minutes |
| Check logs | Something broken | 1 command |