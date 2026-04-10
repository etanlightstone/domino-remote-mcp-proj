# Domino Remote MCP Server

A shared [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes Domino Data Lab operations as tools for AI coding agents. Deploy it once as a Domino App, and any team member can connect their coding agent (Claude Code, Cursor, Windsurf, etc.) to run jobs, manage files, deploy models, and interact with the Model Registry — all through natural language.

> **Already coding inside a Domino workspace?** You don't need this. Domino workspaces come with MCP tools and agent skills built in. This server is for connecting **external** coding agents running on your laptop to a shared Domino instance over the network.

## Available Tools

### Hardware & Infrastructure

| Tool | Description |
|------|-------------|
| `list_hardware_tiers` | List available hardware tiers for a project (cores, memory, GPU) |

### Jobs

| Tool | Description |
|------|-------------|
| `run_domino_job` | Execute a command as a Domino job (with optional hardware tier) |
| `check_domino_job_run_status` | Poll job status (running / finished / error) |
| `check_domino_job_run_results` | Get stdout from a completed job |

### Model API Endpoints

| Tool | Description |
|------|-------------|
| `list_model_endpoints` | List model API endpoints in a project |
| `publish_model_endpoint` | Build a model API image (file-based or from Model Registry) |
| `get_model_endpoint_status` | Check build and deployment status |
| `start_model_deployment` | Start a built model API deployment |
| `stop_model_deployment` | Stop a running model API deployment |

### Model Registry

| Tool | Description |
|------|-------------|
| `list_registered_models` | List models in the Domino Model Registry |
| `get_registered_model` | Get details, versions, and deployments of a registered model |
| `register_model_from_experiment` | Register an MLflow experiment model in the Registry |

### Projects & Files

| Tool | Description |
|------|-------------|
| `get_domino_environment_info` | Discover server context and auth mode |
| `list_projects` | List accessible Domino projects |
| `list_domino_project_files` | Browse files in a DFS project |
| `upload_file_to_domino_project` | Upload file content to a DFS project |
| `download_file_from_domino_project` | Download a file from a DFS project |
| `smart_sync_file` | Upload with conflict detection |

## Deployment: Domino App with Identity Propagation

This server is designed to run as a **Domino App** with [App Identity Propagation](https://docs.dominodatalab.com/en/latest/user_guide/4af320/configure-app-identity-propagation/) enabled. This allows each connecting user's Domino API key to flow through to the platform, so actions run under **their identity** with their permissions and audit trail.

### Setup Steps

1. **Create a Domino project** (or use an existing one) and add these files to it.

2. **Publish as an App** in the Domino UI:
   - Set the app script to `app.sh`
   - The server binds to port 8888 (Domino's required app port)

3. **Enable Identity Propagation** on the app so that each user's credentials are forwarded. See the [Domino docs on App Identity Propagation](https://docs.dominodatalab.com/en/latest/user_guide/4af320/configure-app-identity-propagation/) for how to enable this in your deployment.

4. **Note the app URL** — it will look something like:
   ```
   https://your-domino.example.com/modelproducts/<app-id>/
   ```
   The MCP endpoint is at `{app-url}/mcp`.

Without identity propagation, all Domino API calls use the **app owner's identity** (via the `localhost:8899` ephemeral token). This still works, but there's no per-user audit trail — all actions appear as the app owner.

## Connecting Your Coding Agent

For instructions on configuring Claude Code, Cursor, or other coding agents to connect to this MCP server, see:

**[domino_remote_mcp_agent_skill](https://github.com/etanlightstone/domino_remote_mcp_agent_skill)** — the companion repo with agent setup instructions, skills, and configuration examples.

## Architecture

```
Coding Agent (Claude Code / Cursor / etc.)
    │
    │  Streamable HTTP (MCP protocol)
    │  Header: X-Domino-User-Api-Key (identity propagation)
    │
    ▼
┌──────────────────────────────────────┐
│  Domino App  (port 8888)             │
│  FastMCP  transport="streamable-http"│
│                                      │
│  ┌────────────────────────────────┐  │
│  │ Tools:                         │  │
│  │  Jobs       – run, status,     │  │
│  │               results          │  │
│  │  HW Tiers   – list             │  │
│  │  Models     – publish, deploy, │  │
│  │               start, stop      │  │
│  │  Registry   – list, register,  │  │
│  │               get details      │  │
│  │  Files      – list, upload,    │  │
│  │               download, sync   │  │
│  │  Projects   – list, env info   │  │
│  └──────────────┬─────────────────┘  │
│                 │                    │
│  Auth: user's API key (propagated)   │
│    or app owner's token (fallback)   │
└─────────────────┬────────────────────┘
                  │
                  │  Domino REST APIs
                  │  /v1/..., /v4/..., /api/...
                  ▼
           Domino Platform
```

## Typical Workflows

### Train → Register → Deploy

This is the end-to-end ML lifecycle through the MCP server:

1. **Upload training code** to the project with `upload_file_to_domino_project`
2. **Run training** with `run_domino_job` (optionally targeting a GPU tier via `hardware_tier_id`)
3. **Monitor** with `check_domino_job_run_status`, get output with `check_domino_job_run_results`
4. **Register the model** from the experiment run into the Model Registry with `register_model_from_experiment`
5. **Deploy as an API** with `publish_model_endpoint` (file-based or from the registry), then `start_model_deployment`
6. **Monitor the endpoint** with `get_model_endpoint_status`

### Model Registry ↔ Model API Endpoints

The `publish_model_endpoint` tool supports two modes:

- **File-based**: point it at a script and function in the project (`inference_file` + `inference_function`)
- **Registry-based**: point it at a registered model (`registered_model_name` + `registered_model_version`) — the model artifact is pulled from the registry automatically

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

## Origin

Adapted from [dominodatalab/domino_mcp_server](https://github.com/dominodatalab/domino_mcp_server) (local stdio transport) to a remote Streamable HTTP service suitable for shared team use as a Domino App.
