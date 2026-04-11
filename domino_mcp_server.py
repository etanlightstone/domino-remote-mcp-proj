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
# Authentication (automatic):
#   If the connecting MCP client sends credentials (X-Domino-User-Api-Key header or
#   Authorization: Bearer token), those are used for Domino API calls → per-user identity.
#   If no credentials are sent, falls back to the app owner's identity (localhost:8899
#   inside Domino, or DOMINO_API_KEY outside).

from typing import Dict, Any
from fastmcp import FastMCP, Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
import requests
import os
import re
import urllib.parse
import shlex
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

    Inside Domino: always route through localhost:8899. This local proxy handles
    service discovery, TLS, and routing to internal services. DOMINO_API_HOST is
    the raw internal K8s URL (e.g. nucleus-frontend.domino-platform:80) which
    doesn't accept external user tokens.

    When a user credential is present AND DOMINO_PUBLIC_URL is configured, we
    call the public gateway directly so the user's own token is used.

    Outside Domino: use DOMINO_HOST from env/.env file.
    """
    if _is_domino_workspace():
        # If user credentials are present, prefer the public URL (if configured)
        # because localhost:8899 may inject the app owner's token instead.
        if _current_user_api_key.get() is not None:
            public_url = os.environ.get("DOMINO_PUBLIC_URL", "")
            if public_url:
                return public_url.rstrip("/")
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

    Priority order:
      1. Per-request user credential (API key or Bearer token from MCP client)
      2. API_KEY_OVERRIDE env var
      3. App owner's ephemeral token (localhost:8899, inside Domino)
      4. DOMINO_API_KEY env var (outside Domino)

    This means: if a user sends their own credentials, those are always used
    (per-user identity). If not, falls back to the app owner's identity.
    """
    # 1. Per-request user credential (always preferred when present)
    credential = _current_user_api_key.get()
    if credential:
        cred_type, cred_value = credential
        if cred_type == "bearer":
            return {"Authorization": f"Bearer {cred_value}"}
        else:
            return {"X-Domino-Api-Key": cred_value}

    # 2. Explicit override
    api_key_override = os.environ.get("API_KEY_OVERRIDE")
    if api_key_override:
        return {"X-Domino-Api-Key": api_key_override}

    # 3. Inside Domino: ephemeral token from local proxy (app owner identity)
    if _is_domino_workspace():
        resp = requests.get("http://localhost:8899/access-token")
        resp.raise_for_status()
        token = resp.text.strip()
        if token.startswith("Bearer "):
            return {"Authorization": token}
        return {"Authorization": f"Bearer {token}"}

    # 4. Outside Domino: static API key
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

    # Fall back to the host project only if the API lookup found nothing
    # and the requested project matches the current environment.
    domino_project_name = os.getenv("DOMINO_PROJECT_NAME")
    domino_project_id = os.getenv("DOMINO_PROJECT_ID")
    if domino_project_id and domino_project_name == project_name:
        return domino_project_id

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
# Middleware — extract user credentials from incoming requests
# ---------------------------------------------------------------------------
# Always attempts to extract the user's Domino API key or Bearer token from
# the incoming HTTP request. If present, _get_auth_headers() will use it
# (per-user identity). If absent, falls back to app owner's identity.

class UserCredentialMiddleware(Middleware):
    """
    Extracts Domino credentials from incoming MCP requests and makes them
    available to tool handlers via the _current_user_api_key contextvar.

    Supports two header formats:
      - X-Domino-User-Api-Key: <key>       → forwarded as X-Domino-Api-Key
      - Authorization: Bearer <jwt-or-key>  → forwarded as Authorization: Bearer
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        credential = None
        try:
            from fastmcp.server.dependencies import get_http_request
            request = get_http_request()
            api_key = request.headers.get("x-domino-user-api-key")
            if api_key:
                credential = ("api_key", api_key)
            else:
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    credential = ("bearer", auth.removeprefix("Bearer ").strip())
        except Exception:
            pass

        if credential:
            token = _current_user_api_key.set(credential)
            try:
                return await call_next(context)
            finally:
                _current_user_api_key.reset(token)
        else:
            # No user credential — fall through to app_owner identity
            return await call_next(context)


mcp.add_middleware(UserCredentialMiddleware())


# In-memory cache for file version conflict detection.
# Key: (user_name, project_name, file_path) -> {"key": str, "content": str}
_file_version_cache: Dict[tuple, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Tools — Hardware Tiers
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_hardware_tiers(
    user_name: str,
    project_name: str,
    for_model_api: bool = False,
) -> Dict[str, Any]:
    """
    List available hardware tiers for a Domino project.

    Use this to discover hardware tier IDs before running jobs or deploying
    model endpoints. Returns tier names, resources (cores, memory, GPU), and
    whether each tier is the default.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        for_model_api: If True, only return tiers eligible for model API deployment.
    """
    project_id = _get_project_id(user_name, project_name)
    if not project_id:
        return {"error": f"Could not find project '{user_name}/{project_name}'."}

    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/v4/projects/{project_id}/hardwareTiers"
    params: Dict[str, Any] = {}
    if for_model_api:
        params["forModelApi"] = True

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        raw_tiers = response.json()

        simplified = []
        for t in raw_tiers:
            tier: Dict[str, Any] = {
                "id": t.get("hardwareTierId"),
                "name": t.get("hardwareTierName"),
            }
            cores = t.get("cores")
            memory_gb = t.get("memoryInGiB") or t.get("memory")
            if cores is not None:
                tier["cores"] = cores
            if memory_gb is not None:
                tier["memoryGiB"] = memory_gb
            is_default = t.get("isDefault")
            is_model_api = t.get("isModelApiTier")
            if is_default:
                tier["isDefault"] = True
            if is_model_api:
                tier["isModelApiTier"] = True
            gpu_count = t.get("numberOfGpus")
            if gpu_count and gpu_count > 0:
                tier["gpus"] = gpu_count
                gpu_key = t.get("gpuKey")
                if gpu_key:
                    tier["gpuType"] = gpu_key
            simplified.append(tier)

        return {"hardware_tiers": simplified, "count": len(simplified)}
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


# ---------------------------------------------------------------------------
# Tools — Jobs
# ---------------------------------------------------------------------------

@mcp.tool()
async def run_domino_job(
    user_name: str,
    project_name: str,
    run_command: str,
    title: str,
    hardware_tier_id: str | None = None,
) -> Dict[str, Any]:
    """
    Run a command as a job on the Domino platform.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        run_command: The command to run, e.g. 'python train.py --lr 0.01'.
        title: A descriptive title for the job, e.g. 'training run with lr=0.01'.
        hardware_tier_id: Optional hardware tier ID (use list_hardware_tiers to find valid IDs). If omitted, uses the project default.
    """
    encoded_user = _validate_url_parameter(user_name, "user_name")
    encoded_project = _validate_url_parameter(project_name, "project_name")

    api_url = f"{_get_domino_host()}/v1/projects/{encoded_user}/{encoded_project}/runs"
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    # Use shlex.split to respect shell quoting (e.g. python -c "import os; ...")
    # For commands with shell features (pipes, redirects), wrap in bash -c
    if any(c in run_command for c in ['|', '>', '<', '&&', '||', ';']):
        command_list = ["bash", "-c", run_command]
    else:
        command_list = shlex.split(run_command)

    payload: Dict[str, Any] = {
        "command": command_list,
        "isDirect": False,
        "title": title,
        "publishApiEndpoint": False,
    }
    if hardware_tier_id:
        payload["overrideHardwareTierId"] = hardware_tier_id

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
    Returns information about this MCP server's environment and auth mode.

    IMPORTANT: The server hosting project shown here is NOT the user's target
    project. Always ask the user which project they want to work with, or call
    list_projects to let them choose.
    """
    # Detect whether the current request has user credentials
    has_user_cred = _current_user_api_key.get() is not None
    auth_desc = "per-user credential (from request headers)" if has_user_cred else "app owner identity (server-side)"

    info: Dict[str, Any] = {
        "server_type": "remote_http_stateless",
        "auth_identity": auth_desc,
        "instructions": (
            "This is a shared remote MCP server. The hosting project listed below "
            "is where the server itself runs — it is NOT the user's target project. "
            "Ask the user which Domino project they want to operate on, or call "
            "list_projects to show them their available projects."
        ),
    }

    if _is_domino_workspace():
        info["mcp_server_owner"] = os.environ.get("DOMINO_PROJECT_OWNER", "")
        info["mcp_server_project"] = os.environ.get("DOMINO_PROJECT_NAME", "")
    else:
        info["note"] = "Server running outside Domino."

    return info


# ---------------------------------------------------------------------------
# Tools — Model Endpoints (Model APIs)
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_model_endpoints(
    user_name: str,
    project_name: str,
) -> Dict[str, Any]:
    """
    List all model API endpoints in a Domino project.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
    """
    project_id = _get_project_id(user_name, project_name)
    if not project_id:
        return {"error": f"Could not find project '{user_name}/{project_name}'."}

    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/modelManager/getModels"
    params = {"projectId": project_id}

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        models = response.json()

        simplified = []
        for m in models:
            entry: Dict[str, Any] = {
                "id": m.get("id"),
                "name": m.get("name"),
                "description": m.get("description"),
                "status": m.get("activeVersionStatus"),
                "activeVersionNumber": m.get("activeVersionNumber"),
                "activeModelVersionId": m.get("activeModelVersionId"),
                "isAsync": m.get("isAsync"),
            }
            simplified.append(entry)

        return {"model_endpoints": simplified, "count": len(simplified)}
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def publish_model_endpoint(
    user_name: str,
    project_name: str,
    model_name: str,
    inference_file: str,
    inference_function: str,
    description: str = "",
    environment_id: str | None = None,
    hardware_tier_id: str | None = None,
    registered_model_name: str | None = None,
    registered_model_version: str | None = None,
    model_id: str | None = None,
    log_http_requests: bool = False,
) -> Dict[str, Any]:
    """
    Publish (or update) a model API endpoint in Domino.

    Creates a new model API or adds a new version to an existing one.
    The model image will be built asynchronously — use get_model_endpoint_status
    to monitor progress, then start_model_deployment to deploy it.

    For file-based models, provide inference_file and inference_function.
    For registry-based models, provide registered_model_name and
    registered_model_version instead.

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project.
        model_name: A name for the model API endpoint.
        inference_file: Path to the model script in the project, e.g. 'model.py'.
        inference_function: Function name to call for predictions, e.g. 'predict'.
        description: Optional description of the model.
        environment_id: Compute environment ID. If omitted, uses project default.
        hardware_tier_id: Hardware tier for the endpoint (use list_hardware_tiers with for_model_api=True). If omitted, uses default model API tier.
        registered_model_name: MLflow registered model name (alternative to file-based).
        registered_model_version: MLflow registered model version (required with registered_model_name).
        model_id: Existing model API ID to publish a new version to. If omitted, creates a new model API.
        log_http_requests: If True, log request/response payloads for monitoring.
    """
    project_id = _get_project_id(user_name, project_name)
    if not project_id:
        return {"error": f"Could not find project '{user_name}/{project_name}'."}

    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/v4/models/buildModelImage"

    payload: Dict[str, Any] = {
        "modelName": model_name,
        "projectId": project_id,
        "inferenceFunctionFile": inference_file,
        "inferenceFunctionToCall": inference_function,
        "logHttpRequestResponse": log_http_requests,
    }
    if description:
        payload["description"] = description
    if environment_id:
        payload["environmentId"] = environment_id
    if model_id:
        payload["modelId"] = model_id
    if registered_model_name:
        payload["registeredModelName"] = registered_model_name
    if registered_model_version:
        payload["registeredModelVersion"] = registered_model_version

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        out: Dict[str, Any] = {
            "success": True,
            "modelId": result.get("modelId"),
            "modelVersionId": result.get("modelVersionId"),
            "modelVersionNumber": result.get("modelVersionNumber"),
            "buildStatus": result.get("buildStatus"),
            "message": (
                f"Model API '{model_name}' image build started. "
                f"Use get_model_endpoint_status to monitor the build, then "
                f"start_model_deployment to deploy it."
            ),
        }
        if hardware_tier_id:
            out["note"] = (
                f"Hardware tier '{hardware_tier_id}' will be applied when you "
                f"call start_model_deployment."
            )
        return out
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def get_model_endpoint_status(
    model_id: str,
    model_version_id: str,
) -> Dict[str, Any]:
    """
    Get the build and deployment status of a model API endpoint.

    Returns both the image build status and the deployment status.
    Build statuses: 'Building', 'Ready to run', 'Failed', etc.
    Deployment statuses: 'Starting', 'Running', 'Stopped', 'Failed', etc.

    Args:
        model_id: The model API ID (from publish_model_endpoint or list_model_endpoints).
        model_version_id: The model version ID.
    """
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    result: Dict[str, Any] = {"modelId": model_id, "modelVersionId": model_version_id}

    # Get build status
    build_url = f"{_get_domino_host()}/v4/models/{model_id}/{model_version_id}/getBuildStatus"
    try:
        response = requests.get(build_url, headers=headers)
        response.raise_for_status()
        build_data = response.json()
        result["buildStatus"] = build_data.get("status") if isinstance(build_data, dict) else build_data
    except requests.exceptions.RequestException as e:
        result["buildStatus"] = f"Error fetching: {e}"

    # Get deployment status
    deploy_url = f"{_get_domino_host()}/v4/models/{model_id}/{model_version_id}/getModelDeploymentStatus"
    try:
        response = requests.get(deploy_url, headers=headers)
        response.raise_for_status()
        deploy_data = response.json()
        result["deploymentStatus"] = deploy_data.get("status") if isinstance(deploy_data, dict) else deploy_data
    except requests.exceptions.RequestException as e:
        result["deploymentStatus"] = f"Error fetching: {e}"

    return result


@mcp.tool()
async def start_model_deployment(
    model_id: str,
    model_version_id: str,
) -> Dict[str, Any]:
    """
    Start the deployment of a model API version that has been built.

    The model image must be built first (buildStatus = 'Ready to run').
    Use get_model_endpoint_status to check readiness.

    Args:
        model_id: The model API ID.
        model_version_id: The model version ID.
    """
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/v4/models/{model_id}/{model_version_id}/startModelDeployment"

    try:
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        result = response.json()
        status = result.get("status") if isinstance(result, dict) else result
        return {
            "success": True,
            "modelId": model_id,
            "modelVersionId": model_version_id,
            "status": status,
            "message": "Deployment started. Use get_model_endpoint_status to monitor.",
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def stop_model_deployment(
    model_id: str,
    model_version_id: str,
) -> Dict[str, Any]:
    """
    Stop a running model API deployment.

    Args:
        model_id: The model API ID.
        model_version_id: The model version ID.
    """
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/v4/models/{model_id}/{model_version_id}/stopModelDeployment"

    try:
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        result = response.json()
        status = result.get("status") if isinstance(result, dict) else result
        return {
            "success": True,
            "modelId": model_id,
            "modelVersionId": model_version_id,
            "status": status,
            "message": "Deployment stopped.",
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


# ---------------------------------------------------------------------------
# Tools — Model Registry
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_registered_models() -> Dict[str, Any]:
    """
    List models in the Domino Model Registry.

    Returns all registered models visible to the authenticated user,
    including their names, versions, stages, and source experiments.
    """
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    url = f"{_get_domino_host()}/api/registeredmodels/v1"

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        # The response may be a list or an envelope with a key
        models = data if isinstance(data, list) else data.get("registeredModels", data.get("models", [data]))

        simplified = []
        for m in models:
            entry: Dict[str, Any] = {
                "name": m.get("name"),
                "description": m.get("description", ""),
                "latestVersion": m.get("latestVersion") or m.get("latest_versions"),
                "tags": m.get("tags"),
            }
            simplified.append(entry)

        return {"registered_models": simplified, "count": len(simplified)}
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


@mcp.tool()
async def get_registered_model(
    model_name: str,
) -> Dict[str, Any]:
    """
    Get details of a specific registered model, including all versions.

    Args:
        model_name: The registered model name in the Model Registry.
    """
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    encoded_name = urllib.parse.quote(model_name, safe='')

    result: Dict[str, Any] = {}

    # Get model details
    model_url = f"{_get_domino_host()}/api/registeredmodels/v1/{encoded_name}"
    try:
        response = requests.get(model_url, headers=headers)
        response.raise_for_status()
        result["model"] = response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed fetching model: {e}"}

    # Get versions
    versions_url = f"{_get_domino_host()}/api/registeredmodels/v1/{encoded_name}/versions"
    try:
        response = requests.get(versions_url, headers=headers)
        response.raise_for_status()
        result["versions"] = response.json()
    except requests.exceptions.RequestException:
        result["versions"] = "Could not fetch versions"

    # Get deployed model APIs from this registered model
    apis_url = f"{_get_domino_host()}/api/registeredmodels/v1/{encoded_name}/modelapis"
    try:
        response = requests.get(apis_url, headers=headers)
        response.raise_for_status()
        result["deployed_model_apis"] = response.json()
    except requests.exceptions.RequestException:
        result["deployed_model_apis"] = []

    return result


@mcp.tool()
async def register_model_from_experiment(
    user_name: str,
    project_name: str,
    model_name: str,
    experiment_id: str,
    run_id: str,
    description: str = "",
) -> Dict[str, Any]:
    """
    Register a model in the Domino Model Registry from an MLflow experiment run.

    This takes a model that was logged during an experiment run (via
    mlflow.log_model or autologging) and registers it in the Domino Model
    Registry for versioning, governance, and deployment.

    Prerequisite: The experiment run must have already logged a model artifact
    (e.g., via mlflow.sklearn.log_model, mlflow.pytorch.log_model, etc.).

    Args:
        user_name: The username of the project owner.
        project_name: The name of the Domino project where the experiment ran.
        model_name: Name for the registered model (new or existing). If the name
                    already exists, a new version is added.
        experiment_id: The MLflow experiment ID (numeric string, e.g. '1').
        run_id: The MLflow run ID (hex string from the experiment run).
        description: Optional description for this model version.
    """
    project_id = _get_project_id(user_name, project_name)
    if not project_id:
        return {"error": f"Could not find project '{user_name}/{project_name}'."}

    headers = {**_get_auth_headers(), "Content-Type": "application/json"}

    # First, try the v2 API (preferred, supports multiple sources)
    url_v2 = f"{_get_domino_host()}/api/registeredmodels/v2"
    payload_v2: Dict[str, Any] = {
        "name": model_name,
        "experimentId": experiment_id,
        "runId": run_id,
        "projectId": project_id,
    }
    if description:
        payload_v2["description"] = description

    try:
        response = requests.post(url_v2, headers=headers, json=payload_v2)
        response.raise_for_status()
        result = response.json()
        out: Dict[str, Any] = {
            "success": True,
            "message": f"Model '{model_name}' registered in the Model Registry.",
            "details": result,
        }
        # Build a link to the model registry page
        model_reg_name = urllib.parse.quote(model_name, safe='')
        out["registry_url"] = f"{_get_external_host()}/model-registry/{model_reg_name}"
        return out
    except requests.exceptions.RequestException as e:
        # If v2 fails, try v1 as fallback
        pass

    # Fallback: v1 API
    url_v1 = f"{_get_domino_host()}/api/registeredmodels/v1"
    payload_v1: Dict[str, Any] = {
        "name": model_name,
        "experimentId": experiment_id,
        "runId": run_id,
        "projectId": project_id,
    }
    if description:
        payload_v1["description"] = description

    try:
        response = requests.post(url_v1, headers=headers, json=payload_v1)
        response.raise_for_status()
        result = response.json()
        out = {
            "success": True,
            "message": f"Model '{model_name}' registered in the Model Registry (v1 API).",
            "details": result,
        }
        model_reg_name = urllib.parse.quote(model_name, safe='')
        out["registry_url"] = f"{_get_external_host()}/model-registry/{model_reg_name}"
        return out
    except requests.exceptions.RequestException as e:
        return {"error": f"API request failed: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}


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
# Web UI — setup instructions at /
# ---------------------------------------------------------------------------

from starlette.requests import Request
from starlette.responses import HTMLResponse


def _build_landing_html(base_url: str) -> str:
    mcp_url = f"{base_url}/mcp"
    tools = [
        ("list_hardware_tiers", "List available hardware tiers for a project"),
        ("run_domino_job", "Execute a command as a Domino job (with optional hardware tier)"),
        ("check_domino_job_run_status", "Poll a job's status until finished"),
        ("check_domino_job_run_results", "Get stdout from a completed job"),
        ("get_domino_environment_info", "Discover server context and auth mode"),
        ("list_projects", "List accessible Domino projects"),
        ("list_model_endpoints", "List model API endpoints in a project"),
        ("publish_model_endpoint", "Build a model API image for deployment"),
        ("get_model_endpoint_status", "Check model build and deployment status"),
        ("start_model_deployment", "Start a built model API deployment"),
        ("stop_model_deployment", "Stop a running model API deployment"),
        ("list_registered_models", "List models in the Model Registry"),
        ("get_registered_model", "Get details and versions of a registered model"),
        ("register_model_from_experiment", "Register an MLflow experiment model in the Registry"),
        ("list_domino_project_files", "Browse files in a DFS project"),
        ("upload_file_to_domino_project", "Upload file content to a project"),
        ("download_file_from_domino_project", "Download a file from a project"),
        ("smart_sync_file", "Upload with conflict detection"),
    ]
    tools_rows = "\n".join(
        f'<tr><td><code>{name}</code></td><td>{desc}</td></tr>' for name, desc in tools
    )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Domino MCP Server</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: Inter, -apple-system, sans-serif; color: #2E2E38; background: #F7F7F8; }}
  .topbar {{ background: #2E2E38; height: 48px; display: flex; align-items: center; padding: 0 24px; }}
  .topbar svg {{ height: 28px; }}
  .topbar span {{ color: #fff; font-size: 14px; font-weight: 500; margin-left: 12px; opacity: 0.7; }}
  .container {{ max-width: 760px; margin: 0 auto; padding: 32px 24px; }}
  h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ color: #65657B; font-size: 14px; margin-bottom: 28px; }}
  .status {{ display: inline-flex; align-items: center; gap: 6px; background: #E8F5E9; color: #1B6E2D;
             font-size: 13px; font-weight: 500; padding: 4px 12px; border-radius: 12px; margin-bottom: 24px; }}
  .status .dot {{ width: 8px; height: 8px; background: #28A464; border-radius: 50%; }}
  .card {{ background: #fff; border: 1px solid #E0E0E0; border-radius: 8px; padding: 20px; margin-bottom: 16px; }}
  .card h2 {{ font-size: 15px; font-weight: 600; margin-bottom: 12px; }}
  .card h3 {{ font-size: 13px; font-weight: 600; color: #65657B; text-transform: uppercase; letter-spacing: 0.5px;
              margin: 16px 0 8px; }}
  .card h3:first-child {{ margin-top: 0; }}
  .endpoint {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }}
  .endpoint code {{ flex: 1; background: #F7F7F8; border: 1px solid #E0E0E0; border-radius: 4px;
                    padding: 10px 12px; font-size: 14px; font-family: 'SF Mono', Menlo, monospace; word-break: break-all; }}
  .copy-btn {{ background: #543FDE; color: #fff; border: none; border-radius: 4px; padding: 10px 16px;
               font-size: 13px; font-weight: 500; cursor: pointer; white-space: nowrap; font-family: Inter, sans-serif; }}
  .copy-btn:hover {{ background: #3B23D1; }}
  .copy-btn.copied {{ background: #28A464; }}
  pre {{ background: #1E1E2E; color: #CDD6F4; border-radius: 6px; padding: 16px; overflow-x: auto;
         font-size: 13px; line-height: 1.6; font-family: 'SF Mono', Menlo, Consolas, monospace; position: relative; }}
  pre code, .step-body pre code {{ background: none; padding: 0; border-radius: 0; color: inherit; font-size: inherit; }}
  pre .copy-btn {{ position: absolute; top: 8px; right: 8px; padding: 4px 10px; font-size: 11px;
                   background: rgba(255,255,255,0.1); }}
  pre .copy-btn:hover {{ background: rgba(255,255,255,0.2); }}
  .tabs {{ display: flex; gap: 0; margin-bottom: 0; border-bottom: 2px solid #E0E0E0; }}
  .tab {{ padding: 8px 16px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; background: none;
          color: #65657B; border-bottom: 2px solid transparent; margin-bottom: -2px; font-family: Inter, sans-serif; }}
  .tab.active {{ color: #543FDE; border-bottom-color: #543FDE; }}
  .tab-content {{ display: none; padding-top: 16px; }}
  .tab-content.active {{ display: block; }}
  .note {{ background: #F0EDFC; border-left: 3px solid #543FDE; padding: 12px 16px; border-radius: 0 4px 4px 0;
           font-size: 13px; color: #3B23D1; margin-top: 12px; line-height: 1.5; }}
  .warn {{ background: #FFF8E1; border-left: 3px solid #CCB718; padding: 12px 16px; border-radius: 0 4px 4px 0;
           font-size: 13px; color: #7A6E0E; margin-top: 12px; line-height: 1.5; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; font-weight: 600; padding: 8px 12px; border-bottom: 2px solid #E0E0E0; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #F0F0F0; }}
  td code {{ background: #F7F7F8; padding: 2px 6px; border-radius: 3px; font-size: 12px; }}
  .step {{ display: flex; gap: 12px; margin-bottom: 16px; }}
  .step-num {{ flex-shrink: 0; width: 24px; height: 24px; background: #543FDE; color: #fff; border-radius: 50%;
               font-size: 12px; font-weight: 600; display: flex; align-items: center; justify-content: center; }}
  .step-body {{ flex: 1; font-size: 14px; line-height: 1.6; }}
  .step-body code {{ background: #F7F7F8; padding: 2px 6px; border-radius: 3px; font-size: 13px; }}
</style>
</head>
<body>

<div class="topbar">
  <svg viewBox="0 0 100 30" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="15" cy="15" r="12" fill="#543FDE"/>
    <circle cx="15" cy="15" r="5" fill="#fff"/>
    <text x="34" y="21" fill="#fff" font-family="Inter, sans-serif" font-size="16" font-weight="700">domino</text>
  </svg>
  <span>MCP Server</span>
</div>

<div class="container">

  <div class="status"><span class="dot"></span> Running</div>
  <h1>Domino Remote MCP Server</h1>
  <p class="subtitle">Connect your AI coding agent to Domino Data Lab</p>

  <!-- MCP Endpoint -->
  <div class="card">
    <h2>MCP Endpoint</h2>
    <div class="endpoint">
      <code id="mcp-url">{mcp_url}</code>
      <button class="copy-btn" onclick="copyText('mcp-url', this)">Copy</button>
    </div>
    <p style="font-size:12px;color:#8F8FA3;margin-top:4px;" id="url-note"></p>
  </div>

  <!-- Setup Guide -->
  <div class="card">
    <h2>Setup Guide</h2>

    <div class="step">
      <div class="step-num">1</div>
      <div class="step-body">
        Get your <strong>Domino API Key</strong> from your Domino user profile
        (Account Settings &rarr; API Key). Set it as an environment variable on your machine:
        <pre style="margin-top:8px"><code>export DOMINO_API_KEY="your-api-key-here"</code><button class="copy-btn" onclick="copyCode(this)">Copy</button></pre>
      </div>
    </div>

    <div class="step">
      <div class="step-num">2</div>
      <div class="step-body">Configure your coding agent (pick your tool below):</div>
    </div>

    <div class="tabs">
      <button class="tab active" onclick="switchTab(event, 'tab-claude')">Claude Code</button>
      <button class="tab" onclick="switchTab(event, 'tab-cursor')">Cursor</button>
      <button class="tab" onclick="switchTab(event, 'tab-other')">Other</button>
    </div>

    <div id="tab-claude" class="tab-content active">
      <h3>Option A: Static API key</h3>
      <pre><code>claude mcp add --transport http domino \\
  {mcp_url} \\
  --header "X-Domino-User-Api-Key: $DOMINO_API_KEY"</code><button class="copy-btn" onclick="copyCode(this)">Copy</button></pre>

      <h3>Option B: JSON config (recommended)</h3>
      <p style="font-size:13px;margin-bottom:12px;">Add to <code>.claude.json</code> or <code>.mcp.json</code>:</p>
      <pre><code>{{
  "mcpServers": {{
    "domino-mcp": {{
      "type": "http",
      "url": "{mcp_url}",
      "headers": {{
        "X-Domino-User-Api-Key": "${{DOMINO_API_KEY}}"
      }}
    }}
  }}
}}</code><button class="copy-btn" onclick="copyCode(this)">Copy</button></pre>
      <div class="note">
        For OAuth / auto-refreshing tokens, replace <code>headers</code> with
        <code>"headersHelper": "python3 your_auth_script.py"</code> — the script should
        print JSON headers to stdout (e.g. <code>{{"Authorization": "Bearer &lt;token&gt;"}}</code>).
      </div>
      <p style="font-size:13px;margin-top:12px;color:#65657B;">Verify with: <code style="background:#F7F7F8;padding:2px 6px;border-radius:3px;">claude mcp list</code></p>
    </div>

    <div id="tab-cursor" class="tab-content">
      <p style="font-size:13px;margin-bottom:12px;">Add to <code>.cursor/mcp.json</code> in your project root:</p>
      <pre><code>{{
  "mcpServers": {{
    "domino-mcp": {{
      "url": "{mcp_url}",
      "transport": "streamable-http",
      "headers": {{
        "X-Domino-User-Api-Key": "${{DOMINO_API_KEY}}"
      }}
    }}
  }}
}}</code><button class="copy-btn" onclick="copyCode(this)">Copy</button></pre>
      <div class="note">
        Cursor expands <code>${{DOMINO_API_KEY}}</code> from your shell environment automatically.
      </div>
    </div>

    <div id="tab-other" class="tab-content">
      <p style="font-size:13px;margin-bottom:12px;">
        Any MCP client supporting <strong>Streamable HTTP</strong> transport can connect.
      </p>
      <pre><code>MCP Endpoint:  {mcp_url}
Transport:     streamable-http
Auth Header:   X-Domino-User-Api-Key: &lt;your-key&gt;
  — or —
Auth Header:   Authorization: Bearer &lt;your-token&gt;</code><button class="copy-btn" onclick="copyCode(this)">Copy</button></pre>
      <div class="note">
        Both Domino API keys and OAuth/JWT Bearer tokens are accepted.
      </div>
    </div>

    <div class="step" style="margin-top:20px;">
      <div class="step-num">3</div>
      <div class="step-body">
        Start using Domino tools in your agent. Try asking it to
        <em>"list my Domino projects"</em> or <em>"run a job in project X"</em>.
      </div>
    </div>
  </div>

  <!-- Auth -->
  <div class="card">
    <h2>Authentication</h2>
    <p style="font-size:13px;margin-bottom:12px;">
      This server <strong>auto-detects your credentials</strong>.
    </p>
    <div class="note">
      If you send your Domino API key or OAuth Bearer token (via the headers shown above),
      all Domino API calls will run as <strong>your identity</strong> — with your permissions and audit trail.
      If no credentials are sent, the server falls back to the app owner's identity.
    </div>
  </div>

  <!-- Available Tools -->
  <div class="card">
    <h2>Available Tools</h2>
    <table>
      <thead><tr><th>Tool</th><th>Description</th></tr></thead>
      <tbody>
        {tools_rows}
      </tbody>
    </table>
  </div>

</div>

<script>
function copyText(elementId, btn) {{
  const text = document.getElementById(elementId).textContent;
  navigator.clipboard.writeText(text).then(() => {{
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 2000);
  }});
}}

function copyCode(btn) {{
  const pre = btn.closest('pre');
  const code = pre.querySelector('code').textContent;
  navigator.clipboard.writeText(code).then(() => {{
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 2000);
  }});
}}

function switchTab(event, tabId) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById(tabId).classList.add('active');
}}

// Auto-detect the real MCP URL from the browser's address bar.
// The server-side guess may be wrong behind a reverse proxy, but
// window.location always reflects what the user actually visited.
(function() {{
  const base = window.location.origin + window.location.pathname.replace(/\\/+$/, '');
  const mcpUrl = base + '/mcp';
  const serverGuess = document.getElementById('mcp-url').textContent;

  // Update displayed URL
  document.getElementById('mcp-url').textContent = mcpUrl;

  // Update all code snippets that contain the server-side guess
  document.querySelectorAll('pre code').forEach(function(el) {{
    if (el.textContent.includes(serverGuess)) {{
      el.textContent = el.textContent.split(serverGuess).join(mcpUrl);
    }}
  }});

  if (serverGuess !== mcpUrl) {{
    document.getElementById('url-note').textContent = 'URL auto-detected from your browser address bar.';
  }}
}})();
</script>
</body>
</html>"""


@mcp.custom_route("/", methods=["GET"])
async def landing_page(request: Request) -> HTMLResponse:
    # Build the base URL from the incoming request so the displayed MCP endpoint
    # matches whatever URL the user actually used to reach this page.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    # Strip any trailing /mcp or / from the path prefix
    path = request.scope.get("root_path", "").rstrip("/")
    base_url = f"{scheme}://{host}{path}"
    return HTMLResponse(_build_landing_html(base_url))


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> HTMLResponse:
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "auth_mode": MCP_AUTH_MODE})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Starting Domino Remote MCP Server on port {MCP_PORT}")
    print(f"  Auth mode: {MCP_AUTH_MODE}")
    print(f"  Inside Domino: {_is_domino_workspace()}")
    print(f"  MCP endpoint: http://0.0.0.0:{MCP_PORT}/mcp")
    print(f"  Setup page:   http://0.0.0.0:{MCP_PORT}/")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=MCP_PORT, stateless_http=True)
