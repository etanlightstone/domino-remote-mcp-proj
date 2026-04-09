# Domino Remote MCP Server

A remote [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes Domino Data Lab operations as tools for AI coding agents. Based on the [domino_mcp_server](https://github.com/dominodatalab/domino_mcp_server), adapted from local stdio transport to **Streamable HTTP** so it can run as a network service.

## What it does

Coding agents (Claude Code, Cursor, Windsurf, etc.) connect to this server over HTTP and get access to Domino tools:

| Tool | Description |
|------|-------------|
| `run_domino_job` | Execute a command as a Domino job |
| `check_domino_job_run_status` | Poll job status (running/finished/error) |
| `check_domino_job_run_results` | Get stdout from a completed job |
| `get_domino_environment_info` | Discover current Domino context |
| `list_projects` | List accessible Domino projects |
| `list_domino_project_files` | Browse files in a DFS project |
| `upload_file_to_domino_project` | Upload file content to a DFS project |
| `download_file_from_domino_project` | Download a file from a DFS project |
| `smart_sync_file` | Upload with conflict detection |

## Deployment

### Option A: As a Domino App (recommended)

1. **Create a Domino project** (or use an existing one) and add these files to it.

2. **Publish as an App** in the Domino UI:
   - Set the app script to `app.sh`
   - The server binds to port 8888 automatically (Domino's required port)

3. **Note the app URL** — it will look something like:
   ```
   https://your-domino.example.com/modelproducts/<app-id>/
   ```

4. The MCP endpoint is at `{app-url}/mcp`.

**Auth:** By default, the server uses the **app owner's identity** for all Domino API calls (via the `localhost:8899` ephemeral token). This means all operations run under the permissions of the user who published the app.

### Option B: Standalone (outside Domino)

```bash
# Set environment variables
export DOMINO_HOST=https://your-domino.example.com
export DOMINO_API_KEY=your-api-key-here

# Install and run
pip install -r requirements.txt
python domino_mcp_server.py
```

The server starts on port 8888. Override with `MCP_PORT=9000`.

## Connecting Your Coding Agent

### Claude Code

```bash
# Basic — connect to the remote MCP server
claude mcp add --transport http domino https://your-domino.example.com/modelproducts/<app-id>/mcp

# If Domino requires API key auth to reach the app URL:
claude mcp add --transport http domino \
  https://your-domino.example.com/modelproducts/<app-id>/mcp \
  --header "X-Domino-Api-Key: ${DOMINO_API_KEY}"
```

Verify it's connected:
```bash
claude mcp list
```

### Cursor

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "domino": {
      "url": "https://your-domino.example.com/modelproducts/<app-id>/mcp",
      "transport": "streamable-http"
    }
  }
}
```

If Domino requires auth to reach the app:

```json
{
  "mcpServers": {
    "domino": {
      "url": "https://your-domino.example.com/modelproducts/<app-id>/mcp",
      "transport": "streamable-http",
      "headers": {
        "X-Domino-Api-Key": "${DOMINO_API_KEY}"
      }
    }
  }
}
```

### Other agents

Any MCP client that supports Streamable HTTP transport can connect. The endpoint is:
```
POST/GET  {server-url}/mcp
```

## Authentication Modes

Controlled by the `MCP_AUTH_MODE` environment variable on the server.

### `app_owner` (default)

All Domino API calls use the app owner's identity. Simple — no per-user setup.

- Inside Domino: ephemeral token from `localhost:8899`
- Outside Domino: `DOMINO_API_KEY` env var

**Tradeoff:** No per-user audit trail in Domino. All actions appear as the app owner.

### `user_token`

Each connecting user provides their own Domino API key via the `X-Domino-User-Api-Key` HTTP header. The server uses that key for Domino API calls on their behalf.

```bash
# Claude Code with per-user auth
claude mcp add --transport http domino \
  https://your-domino.example.com/modelproducts/<app-id>/mcp \
  --header "X-Domino-User-Api-Key: ${DOMINO_API_KEY}"
```

**Benefit:** Per-user permissions and audit trail.

**Note:** This mode requires that Domino's reverse proxy forwards the `X-Domino-User-Api-Key` header to the app. If Domino strips custom headers, you may need to test this in your environment.

## Architecture

```
Coding Agent (Claude Code / Cursor / etc.)
    │
    │  Streamable HTTP (MCP protocol)
    │  POST/GET  {app-url}/mcp
    │
    ▼
┌─────────────────────────────────┐
│  Domino App  (port 8888)        │
│  FastMCP  transport="http"      │
│                                 │
│  ┌───────────────────────────┐  │
│  │ Tools:                    │  │
│  │  run_domino_job           │  │
│  │  check_job_status/results │  │
│  │  list/upload/download     │  │
│  │  smart_sync_file          │  │
│  │  list_projects            │  │
│  └─────────┬─────────────────┘  │
│            │                    │
│  Auth: localhost:8899 token     │
│    (or user's API key)          │
└────────────┬────────────────────┘
             │
             │  Domino REST APIs
             │  /v1/..., /v4/...
             ▼
      Domino Platform
```

## Changes from the Original MCP Server

This is adapted from [dominodatalab/domino_mcp_server](https://github.com/dominodatalab/domino_mcp_server):

| Change | Why |
|--------|-----|
| Transport: `stdio` → `streamable-http` | Network-accessible instead of local subprocess |
| Removed `open_web_browser` | No browser on a server |
| Removed `sync_local_file_to_domino` | Reads server filesystem, not user's |
| Added `list_projects` | Useful for project discovery by remote agents |
| Added `user_token` auth mode | Optional per-user identity for remote users |
| Uses `fastmcp` package | Better HTTP transport support than `mcp[cli]` |

## Extending

Add new tools by defining async functions with the `@mcp.tool()` decorator:

```python
@mcp.tool()
async def my_new_tool(arg1: str, arg2: int) -> Dict[str, Any]:
    """Description shown to the AI agent."""
    headers = _get_auth_headers()
    response = requests.get(f"{_get_domino_host()}/v4/some/endpoint", headers=headers)
    return response.json()
```

The server auto-discovers tools — just add the function and restart.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DOMINO_API_HOST` | Auto (Domino) | — | Set by Domino platform inside apps/workspaces |
| `DOMINO_HOST` | Outside Domino | — | Domino base URL, e.g. `https://domino.example.com` |
| `DOMINO_API_KEY` | Outside Domino | — | Your Domino API key |
| `MCP_AUTH_MODE` | No | `app_owner` | `app_owner` or `user_token` |
| `MCP_PORT` | No | `8888` | Port to listen on |
| `API_KEY_OVERRIDE` | No | — | Forces a specific API key (debugging) |
