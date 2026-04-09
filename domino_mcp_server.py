# Remote MCP Server for Domino Data Lab
#
# Adapted from https://github.com/dominodatalab/domino_mcp_server
# Changed from stdio (local subprocess) to Streamable HTTP (remote network service)
# so that coding agents (Claude Code, Cursor, etc.) can connect over the network.
#
# Deployment modes:
#   1. As a Domino App  — set DOMINO_API_HOST (auto-set by platform), uses localhost:8899 token
#   2. Standalone        — set DOMINO_HOST and DOMINO_API_KEY env vars
#
# Auth modes (MCP_AUTH_MODE env var):
#   "app_owner"  (default) — All API calls use the app owner's identity (localhost:8899 token
#                             inside Domino, or DOMINO_API_KEY outside). Simple, no per-user setup.
#   "user_token"           — Each connecting user must send their Domino API key in the
#                             X-Domino-User-Api-Key header. The server uses that key for
#                             Domino API calls, giving per-user identity and audit trail.

from typing import Dict, Any
from fastmcp import FastMCP, Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
import requests
import os
import re
import urllib.parse
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MCP_AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "app_owner")
MCP_PORT = int(os.environ.get("MCP_PORT", "8888"))

# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def _is_domino_workspace() -> bool:
    """Detect whether we're running inside a Domino workspace/app."""
    return bool(os.environ.get("DOMINO_API_HOST"))


def _get_domino_host() -> str:
    """
    Return the base URL for Domino API calls.

    In app_owner mode inside Domino: route through localhost:8899 (auto-injects owner token).
    In user_token mode inside Domino: call DOMINO_API_HOST directly so the user's own
        API key is used instead of the app owner's token.
    Outside Domino: use DOMINO_HOST from env/.env file.
    """
    if _is_domino_workspace():
        if MCP_AUTH_MODE == "user_token":
            # Bypass localhost:8899 proxy — it would inject the app owner's token,
            # overriding the per-user API key we want to use.
            api_host = os.environ.get("DOMINO_API_HOST", "")
            if api_host:
                return api_host.rstrip("/")
        return "http://localhost:8899"
    host = os.getenv("DOMINO_HOST")
    if not host:
        raise ValueError("DOMINO_HOST environment variable not set.")
    return host.rstrip("/")


def _get_external_host() -> str:
    """
    Return the external (user-facing) Domino URL for generating shareable links.
    """
    if not _is_domino_workspace():
        return _get_domino_host()
    vpu = os.getenv("VSCODE_PROXY_URI", "")
    if vpu:
        parsed = urllib.parse.urlparse(vpu)
        return f"{parsed.scheme}://{parsed.hostname}"
    return _get_domino_host()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

# Per-request credential storage. In user_token mode, middleware populates this
# with a (type, value) tuple: ("api_key", "abc123") or ("bearer", "eyJ...").
import contextvars
_current_user_api_key: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "_current_user_api_key", default=None
)


def _get_auth_headers() -> dict:
    """
    Return authentication headers for Domino API calls.

    In user_token mode: uses the per-request API key from the MCP client.
    In app_owner mode:  uses localhost:8899 (inside Domino) or DOMINO_API_KEY (outside).
    """
    # user_token mode: prefer the per-request credential
    if MCP_AUTH_MODE == "user_token":
        credential = _current_user_api_key.get()
        if credential:
            cred_type, cred_value = credential
            if cred_type == "bearer":
                # Preserve as Bearer token (works for Keycloak JWTs and OAuth tokens)
                return {"Authorization": f"Bearer {cred_value}"}
            else:
                # Plain API key
                return {"X-Domino-Api-Key": cred_value}

    # Explicit override always wins
    api_key_override = os.environ.get("API_KEY_OVERRIDE")
    if api_key_override:
        return {"X-Domino-Api-Key": api_key_override}

    # Inside Domino: ephemeral token from local proxy
    if _is_domino_workspace():
        resp = requests.get("http://localhost:8899/access-token")
        resp.raise_for_status()
        token = resp.text.strip()
        if token.startswith("Bearer "):
            return {"Authorization": token}
        return {"Authorization": f"Bearer {token}"}

    # Outside Domino: static API key
    api_key = os.getenv("DOMINO_API_KEY")
    if not api_key:
        raise ValueError("DOMINO_API_KEY environment variable not set.")
    return {"X-Domino-Api-Key": api_key}


def _get_workspace_project_info() -> dict | None:
    """
    When running inside a Domino workspace, return the auto-detected project
    owner and project name from the platform-provided env vars.
    """
    if not _is_domino_workspace():
        return None
    owner = os.environ.get("DOMINO_PROJECT_OWNER")
    name = os.environ.get("DOMINO_PROJECT_NAME")
    if owner and name:
        return {"user_name": owner, "project_name": name}
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_url_parameter(param_value: str, param_name: str) -> str:
    """Validates and URL-encodes a parameter for safe use in URLs."""
    if any(char in param_value for char in ['/', '\\', '?', '#', '&', '=', '%']):
        raise ValueError(f"Invalid {param_name}: '{param_value}' contains unsafe URL characters")
    return urllib.parse.quote(param_value, safe='')


def _filter_domino_stdout(stdout_text: str) -> str:
    """
    Filters stdout from a Domino job run to extract relevant user output.
    Strips Domino infrastructure noise (spark config, cleanup markers).
    """
    start_patterns = [
        r"### Completed /mnt(?:/artifacts)?/\.domino/configure-spark-defaults\.sh ###",
        r"### Starting user code ###",
        r"Starting job\.\.\.",
    ]
    end_patterns = [
        r"Evaluating cleanup command on EXIT",
        r"### User code finished ###",
        r"Job completed",
    ]

    start_index = 0
    for pattern in start_patterns:
        match = re.search(pattern, stdout_text)
        if match:
            start_index = match.end()
            break

    end_index = len(stdout_text)
    for pattern in end_patterns:
        match = re.search(pattern, stdout_text[start_index:])
        if match:
            end_index = start_index + match.start()
            break

    filtered_text = stdout_text[start_index:end_index].strip()
    if not filtered_text:
        return stdout_text.strip() if stdout_text.strip() else "(No output captured)"
    return filtered_text


def _extract_and_format_mlflow_url(text: str, user_name: str, project_name: str) -> str | None:
    """
    Finds a local MLflow URL (http://127.0.0.1:8768/...) and reformats it
    to the external Domino Cloud URL format.
    """
    pattern = r"http://127\.0\.0\.1:8768/#/experiments/(\d+)/runs/([a-f0-9]+)"
    match = re.search(pattern, text)
    if match:
        experiment_id = match.group(1)
        run_id = match.group(2)
        return f"{_get_external_host()}/experiments/{user_name}/{project_name}/{experiment_id}/{run_id}"
    return None


def _get_project_id(user_name: str, project_name: str) -> str | None:
    """Gets the project ID for a given user and project name."""
    domino_project_id = os.getenv("DOMINO_PROJECT_ID")
    if domino_project_id:
        return domino_project_id

    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/v4/gateway/projects"

    try:
        for relationship in ("Owned", "All"):
            response = requests.get(url, headers=headers, params={"relationship": relationship})
            response.raise_for_status()
            for project in response.json():
                if project.get("name") == project_name:
                    return project.get("id")
    except requests.exceptions.RequestException:
        pass
    return None


def _get_remote_file_info(user_name: str, project_name: str, file_path: str) -> dict | None:
    """Gets the current remote file info (key, size) without downloading content."""
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/v4/files/browseFiles"
    params = {"ownerUsername": user_name, "projectName": project_name, "filePath": "/"}
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        for f in response.json():
            if f.get("path") == file_path:
                return {"key": f.get("key"), "size": f.get("size"), "lastModified": f.get("lastModified")}
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "domino_remote_server",
    instructions=(
        "Domino Data Lab MCP Server. Provides tools to run jobs, check job status, "
        "manage project files, and interact with the Domino platform. "
        "Use get_domino_environment_info first to discover the current context."
    ),
)


# ---------------------------------------------------------------------------
# Middleware — user_token auth mode
# ---------------------------------------------------------------------------
# When MCP_AUTH_MODE=user_token, extract the user's Domino API key from the
# incoming HTTP request's X-Domino-User-Api-Key header and store it in a
# contextvar so that _get_auth_headers() can pick it up for Domino API calls.

class UserTokenMiddleware(Middleware):
    """
    Extracts the Domino API key from the incoming MCP request and makes it
    available to tool handlers via the _current_user_api_key contextvar.
    Only active when MCP_AUTH_MODE=user_token.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if MCP_AUTH_MODE != "user_token":
            return await call_next(context)

        # Try to read the user's API key from the HTTP request headers.
        # FastMCP exposes the underlying HTTP request via get_http_request().
        credential = None
        try:
            from fastmcp.server.dependencies import get_http_request
            request = get_http_request()
            # Check custom header first (plain API key)
            api_key = request.headers.get("x-domino-user-api-key")
            if api_key:
                credential = ("api_key", api_key)
            else:
                # Fall back to Authorization header (Bearer JWT or API key)
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    credential = ("bearer", auth.removeprefix("Bearer ").strip())
        except Exception:
            pass

        if not credential:
            return {"error": "user_token mode requires X-Domino-User-Api-Key header or Authorization: Bearer token"}

        token = _current_user_api_key.set(credential)
        try:
            return await call_next(context)
        finally:
            _current_user_api_key.reset(token)


mcp.add_middleware(UserTokenMiddleware())


# In-memory cache for file version conflict detection.
# Key: (user_name, project_name, file_path) -> {"key": str, "content": str}
_file_version_cache: Dict[tuple, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Tools — Jobs
# ---------------------------------------------------------------------------

@mcp.tool()
async def run_domino_job(
    user_name: str,
    project_name: str,
    run_command: str,
    title: str,
) -> Dict[str, Any]:
    """
    Run a command as a job on the Domino platform.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        run_command: The command to run, e.g. 'python train.py --lr 0.01'.
        title: A descriptive title for the job, e.g. 'training run with lr=0.01'.
    """
    encoded_user = _validate_url_parameter(user_name, "user_name")
    encoded_project = _validate_url_parameter(project_name, "project_name")

    api_url = f"{_get_domino_host()}/v1/projects/{encoded_user}/{encoded_project}/runs"
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    payload = {
        "command": run_command.split(),
        "isDirect": False,
        "title": title,
        "publishApiEndpoint": False,
    }

    try:
        response = requests.post(api_url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def check_domino_job_run_status(
    user_name: str,
    project_name: str,
    run_id: str,
) -> Dict[str, Any]:
    """
    Check the status of a Domino job run. Jobs can take minutes; poll until finished.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        run_id: The run ID returned by run_domino_job.
    """
    encoded_user = _validate_url_parameter(user_name, "user_name")
    encoded_project = _validate_url_parameter(project_name, "project_name")
    encoded_run = _validate_url_parameter(run_id, "run_id")

    api_url = f"{_get_domino_host()}/v1/projects/{encoded_user}/{encoded_project}/runs/{encoded_run}"
    headers = _get_auth_headers()

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def check_domino_job_run_results(
    user_name: str,
    project_name: str,
    run_id: str,
) -> Dict[str, Any]:
    """
    Returns the stdout results from a completed Domino job run.
    Filters out Domino infrastructure noise and reformats MLflow URLs.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        run_id: The run ID of the completed job.
    """
    encoded_user = _validate_url_parameter(user_name, "user_name")
    encoded_project = _validate_url_parameter(project_name, "project_name")
    encoded_run = _validate_url_parameter(run_id, "run_id")

    api_url = f"{_get_domino_host()}/v1/projects/{encoded_user}/{encoded_project}/run/{encoded_run}/stdout"
    headers = _get_auth_headers()

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        raw_stdout = response.json().get("stdout", "")

        filtered = _filter_domino_stdout(raw_stdout)
        mlflow_url = _extract_and_format_mlflow_url(filtered, user_name, project_name)

        if mlflow_url:
            local_run_pattern = r"http://127\.0\.0\.1:8768/#/experiments/\d+/runs/[a-f0-9]+"
            local_exp_pattern = r"View experiment at: http://127\.0\.0\.1:8768/#/experiments/\d+"
            lines = filtered.splitlines()
            lines = [l for l in lines
                     if not re.search(local_run_pattern, l) and not re.search(local_exp_pattern, l)]
            filtered = "\n".join(lines).strip()

        result: Dict[str, Any] = {"results": filtered}
        if mlflow_url:
            result["mlflow_url"] = mlflow_url
        return result
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


# ---------------------------------------------------------------------------
# Tools — Environment Info
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_domino_environment_info() -> Dict[str, Any]:
    """
    Returns information about the current Domino environment and auth mode.
    Call this at the start of a session to discover context.
    """
    import subprocess

    info: Dict[str, Any] = {
        "inside_domino": _is_domino_workspace(),
        "domino_host": _get_domino_host(),
        "auth_mode": MCP_AUTH_MODE,
        "server_type": "remote_http",
    }

    if _is_domino_workspace():
        info["user_name"] = os.environ.get("DOMINO_PROJECT_OWNER", "")
        info["project_name"] = os.environ.get("DOMINO_PROJECT_NAME", "")
        info["project_id"] = os.environ.get("DOMINO_PROJECT_ID", "")
        info["auth_identity"] = "app_owner (localhost:8899 token)"
        try:
            result = subprocess.run(["git", "status"], capture_output=True, text=True, timeout=5)
            info["is_dfs_project"] = result.returncode != 0
        except Exception:
            info["is_dfs_project"] = True
    else:
        info["auth_identity"] = "api_key (DOMINO_API_KEY env var)"
        info["note"] = "Running outside Domino. Set DOMINO_HOST and DOMINO_API_KEY."

    return info


# ---------------------------------------------------------------------------
# Tools — Project Discovery
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_projects(relationship: str = "All") -> Dict[str, Any]:
    """
    List Domino projects accessible to the authenticated user.

    Args:
        relationship: Filter by relationship — 'Owned', 'SharedWithMe', or 'All' (default).
    """
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/v4/gateway/projects"
    params = {"relationship": relationship}

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        projects = response.json()
        simplified = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "owner": p.get("ownerUsername", p.get("owner", {}).get("username", "")),
                "description": p.get("description", ""),
            }
            for p in projects
        ]
        return {"projects": simplified, "count": len(simplified)}
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


# ---------------------------------------------------------------------------
# Tools — File Management (DFS projects)
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_domino_project_files(
    user_name: str,
    project_name: str,
    path: str = "/",
) -> Dict[str, Any]:
    """
    List files in a Domino project directory (DFS projects).

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        path: Directory path to list (default: '/' for root).
    """
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/v4/files/browseFiles"
    params = {"ownerUsername": user_name, "projectName": project_name, "filePath": path}

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        files = [
            {
                "path": f.get("path"),
                "name": f.get("name"),
                "size": f.get("size"),
                "lastModified": f.get("lastModified"),
                "key": f.get("key"),
            }
            for f in response.json()
        ]
        return {"files": files, "count": len(files)}
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def upload_file_to_domino_project(
    user_name: str,
    project_name: str,
    file_path: str,
    file_content: str,
) -> Dict[str, Any]:
    """
    Upload a file to a Domino project (DFS projects).

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        file_path: Destination path in the project, e.g. 'scripts/train.py'.
        file_content: The file content as a string.
    """
    encoded_user = _validate_url_parameter(user_name, "user_name")
    encoded_project = _validate_url_parameter(project_name, "project_name")

    url = f"{_get_domino_host()}/v1/projects/{encoded_user}/{encoded_project}/{file_path}"
    headers = _get_auth_headers()

    try:
        response = requests.put(url, headers=headers, data=file_content.encode("utf-8"))
        response.raise_for_status()
        result = response.json()
        return {
            "success": True,
            "path": result.get("path"),
            "size": result.get("size"),
            "key": result.get("key"),
            "lastModified": result.get("lastModified"),
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def download_file_from_domino_project(
    user_name: str,
    project_name: str,
    file_path: str,
) -> Dict[str, Any]:
    """
    Download a file from a Domino project (DFS projects).
    Caches the version for conflict detection in smart_sync_file.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        file_path: Path of the file to download, e.g. 'scripts/train.py'.
    """
    headers = {
        **_get_auth_headers(),
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    url = f"{_get_domino_host()}/v4/files/editCode"
    params = {"ownerUsername": user_name, "projectName": project_name, "pathString": file_path}

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        result = response.json()

        content = result.get("content")
        if content is None:
            content = result.get("codeContent", "")

        remote_info = _get_remote_file_info(user_name, project_name, file_path)
        file_key = remote_info.get("key") if remote_info else None

        if file_key:
            cache_key = (user_name, project_name, file_path)
            _file_version_cache[cache_key] = {"key": file_key, "content": content}

        return {
            "success": True,
            "path": file_path,
            "content": content,
            "key": file_key,
            "commitId": result.get("currentCommitId"),
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def smart_sync_file(
    user_name: str,
    project_name: str,
    file_path: str,
    content: str,
    force_overwrite: bool = False,
) -> Dict[str, Any]:
    """
    Upload a file with conflict detection. Recommended for shared DFS projects.

    Checks whether the remote file changed since the last download_file_from_domino_project
    call and returns conflict info instead of blindly overwriting.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        file_path: Destination path in the project.
        content: The new file content to upload.
        force_overwrite: If True, skip conflict check and overwrite.
    """
    cache_key = (user_name, project_name, file_path)
    cached_version = _file_version_cache.get(cache_key)
    remote_info = _get_remote_file_info(user_name, project_name, file_path)

    # New file — just create it
    if remote_info is None:
        result = await upload_file_to_domino_project(user_name, project_name, file_path, content)
        if result.get("success"):
            _file_version_cache[cache_key] = {"key": result.get("key"), "content": content}
            return {
                "success": True,
                "action": "created",
                "message": f"Created new file: {file_path}",
                "key": result.get("key"),
                "size": result.get("size"),
            }
        return result

    # Exists but never downloaded — warn unless force
    if cached_version is None and not force_overwrite:
        remote_result = await download_file_from_domino_project(user_name, project_name, file_path)
        remote_content = remote_result.get("content", "")
        if remote_content == content:
            _file_version_cache[cache_key] = {"key": remote_info["key"], "content": content}
            return {"success": True, "action": "no_change", "message": "Content identical", "key": remote_info["key"]}
        return {
            "conflict": True,
            "message": f"File '{file_path}' already exists with different content. Download first or use force_overwrite=True.",
            "remote_content": remote_content,
            "remote_key": remote_info["key"],
            "your_content": content,
        }

    # We have a cached version — check for remote changes
    if cached_version and not force_overwrite:
        if remote_info["key"] != cached_version["key"]:
            remote_result = await download_file_from_domino_project(user_name, project_name, file_path)
            return {
                "conflict": True,
                "message": "Remote file changed since your last download!",
                "your_base_key": cached_version["key"],
                "remote_key": remote_info["key"],
                "your_content": content,
                "remote_content": remote_result.get("content", ""),
                "original_content": cached_version.get("content", ""),
            }

    # Safe to upload
    result = await upload_file_to_domino_project(user_name, project_name, file_path, content)
    if result.get("success"):
        _file_version_cache[cache_key] = {"key": result.get("key"), "content": content}
        action = "force_overwritten" if force_overwrite else "uploaded"
        return {"success": True, "action": action, "message": f"Successfully {action} {file_path}", "key": result.get("key"), "size": result.get("size")}
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Starting Domino Remote MCP Server on port {MCP_PORT}")
    print(f"  Auth mode: {MCP_AUTH_MODE}")
    print(f"  Inside Domino: {_is_domino_workspace()}")
    print(f"  MCP endpoint: http://0.0.0.0:{MCP_PORT}/mcp")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=MCP_PORT)
