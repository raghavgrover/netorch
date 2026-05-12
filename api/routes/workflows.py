"""
api/routes/workflows.py — Workflow management endpoints.

Workflows differ from runbooks in one fundamental way:
  - Runbooks are lists of device CLI commands (parsed and sent as SSH commands)
  - Workflows are shell scripts that run on the relay server, with full
    freedom to call local tools, REST APIs, and netorch_exec for device steps

Endpoints
─────────
GET  /workflows                          List all .sh scripts in WORKFLOWS_DIR
GET  /workflows/{name}                   Metadata for a single workflow script
POST /workflows                          Create a new workflow script
PUT  /workflows/{name}                   Overwrite an existing workflow script
POST /workflows/{name}/run               Submit a workflow job
GET  /workflows/{name}/log/{job_id}      Full log for all devices (polling)
GET  /workflows/{name}/log/{job_id}/{host}  Per-device log (polling, since_id)
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from api.schemas import (
    DeviceEntry, WorkflowOptions, WorkflowSubmitRequest,
    WorkflowSubmitResponse, WorkflowInfo, JobStatus,
    JobMode,
)
from core.workflow_executor import submit_workflow, active_workflow_count
from core.job_store import store
from core.config import logging_cfg, executor as exec_cfg
from core.logger import get_logger
from secrets.inventory import inventory_client

WORKFLOWS_DIR = Path("/opt/netorch/workflows")

log    = get_logger("api.workflows")
router = APIRouter(prefix="/workflows", tags=["workflows"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _workflows_dir() -> Path:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    return WORKFLOWS_DIR


def _parse_workflow(path: Path) -> dict:
    """
    Read a workflow script and return metadata.

    Extracts:
      description — first non-shebang comment line
      parameters  — lines matching "# PARAM: KEY — description"
                    These are declared by the script author to tell the UI
                    what parameters the script expects.

    Example header the script author writes:
        #!/bin/bash
        # Add a NAD to ISE and configure TACACS on the IOS device.
        #
        # PARAM: ISE_HOSTNAME — IP or FQDN of the Cisco ISE server
        # PARAM: TACACS_KEY — Shared secret for TACACS authentication
        # PARAM: AAA_GROUP_NAME — AAA server group name to create on device
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read workflow: {exc}")

    lines = text.splitlines()

    description = ""
    parameters: list[dict] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#!"):
            continue
        if not stripped.startswith("#"):
            break  # stop at first non-comment line

        # First plain comment becomes the description
        if not description and stripped.startswith("#"):
            candidate = stripped.lstrip("#").strip()
            if candidate and not candidate.startswith("PARAM:"):
                description = candidate

        # Parse PARAM declarations
        param_match = re.match(r"#\s*PARAM:\s*(\w+)\s*(?:[—\-]+\s*(.*))?", stripped)
        if param_match:
            parameters.append({
                "name":        param_match.group(1),
                "description": (param_match.group(2) or "").strip(),
            })

    st = path.stat()
    modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()

    return {
        "name":        path.name,
        "description": description,
        "modified_at": modified,
        "size_bytes":  st.st_size,
        "parameters":  parameters,
    }


def _expand_devices(devices: list[DeviceEntry]) -> list[DeviceEntry]:
    """Expand group-only DeviceEntry objects into individual host entries."""
    expanded: list[DeviceEntry] = []
    for entry in devices:
        if entry.host:
            expanded.append(entry)
        else:
            try:
                group_creds = inventory_client.get_group_hosts(entry.group)
            except RuntimeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                )
            for creds in group_creds:
                expanded.append(DeviceEntry(
                    host=creds.host,
                    group=entry.group,
                    platform=creds.platform,
                ))
    if not expanded:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No devices resolved after expanding groups.",
        )
    return expanded


def _safe_name(name: str) -> None:
    """Reject path traversal attempts."""
    if "/" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid workflow name.")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", summary="List all workflow scripts in WORKFLOWS_DIR")
def list_workflows():
    d = _workflows_dir()
    workflows = []
    for p in sorted(d.glob("*.sh")):
        try:
            wf = _parse_workflow(p)
            workflows.append(wf)
        except HTTPException:
            pass  # skip unreadable files silently
    return {"workflows": workflows, "total": len(workflows)}


class WorkflowWriteBody(BaseModel):
    content: str

class WorkflowCreateBody(BaseModel):
    filename: str
    content:  str


@router.post("", status_code=201, summary="Create a new workflow script")
def create_workflow(body: WorkflowCreateBody):
    name = body.filename.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Filename is required.")
    if not name.endswith(".sh"):
        raise HTTPException(status_code=400, detail="Filename must end in .sh")
    _safe_name(name)
    path = _workflows_dir() / name
    if path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' already exists. Use PUT /workflows/{name} to overwrite.",
        )
    try:
        path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    return {"name": name, "created": True}


@router.put("/{name}", summary="Overwrite a workflow script with new content")
def put_workflow(name: str, body: WorkflowWriteBody):
    _safe_name(name)
    if not name.endswith(".sh"):
        raise HTTPException(status_code=400, detail="Workflow name must end in .sh")
    path = _workflows_dir() / name
    try:
        path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    return {"name": name, "saved": True}


@router.get("/{name}", summary="Get workflow script metadata and declared parameters")
def get_workflow(name: str):
    _safe_name(name)
    path = _workflows_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found.")
    return _parse_workflow(path)


@router.post(
    "/{name}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkflowSubmitResponse,
    summary="Submit a workflow job against the specified devices",
)
def run_workflow(name: str, body: WorkflowSubmitRequest):
    """
    Submit a workflow script to run against each device in `devices`.

    One bash subprocess is spawned per device in parallel (up to
    options.max_workers). Each subprocess receives:
      - All device context as env vars (TARGET_HOST, DEVICE_PLATFORM, etc.)
      - All user-supplied `parameters` as env vars
      - NETORCH_API_URL and NETORCH_TOKEN for netorch_exec callbacks

    Poll GET /jobs/{job_id} for status. Live per-device output is available
    at GET /workflows/{name}/log/{job_id} while the job is running.
    """
    _safe_name(name)

    # Verify script exists
    path = _workflows_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found.")

    # Check queue capacity
    if active_workflow_count() >= exec_cfg.max_queue_depth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Job queue full ({exec_cfg.max_queue_depth} max). Retry shortly.",
        )

    # Guard duplicate job_id (extremely unlikely but consistent with jobs.py)
    if store.exists(body.job_id):
        body = body.model_copy(update={"job_id": f"wf-{uuid.uuid4().hex[:8]}"})

    # Expand groups to individual hosts
    expanded_devices = _expand_devices(body.devices)

    # Build job_id with script name prefix for visibility in job listings
    stem = name[:-3] if name.endswith(".sh") else name
    job_id = body.job_id or f"wf-{stem}-{uuid.uuid4().hex[:6]}"

    incident = body.incident or None

    submit_workflow(
        script_name=name,
        devices=expanded_devices,
        parameters=body.parameters,
        job_id=job_id,
        options=body.options,
        incident=incident,
    )

    log.info(
        "workflow_job_accepted",
        workflow=name,
        job_id=job_id,
        device_count=len(expanded_devices),
        parameters=list(body.parameters.keys()),
    )

    log_subdir = (logging_cfg.log_dir / incident) if incident else logging_cfg.log_dir
    return WorkflowSubmitResponse(
        job_id       = job_id,
        status       = JobStatus.queued,
        device_count = len(expanded_devices),
        script       = name,
        log_path     = str(log_subdir / f"{job_id}.json"),
    )


@router.get(
    "/{name}/log/{job_id}",
    summary="Poll workflow log for all devices (supports incremental since_id)",
)
def get_workflow_log_all(
    name: str,
    job_id: str,
    since_id: int = Query(0, ge=0, description="Return only lines with id > since_id. Pass 0 for all."),
):
    """
    Returns streaming log output from all device subprocesses for a workflow job.

    Designed for incremental polling:
      1. First call: GET /workflows/{name}/log/{job_id}          → returns all lines so far + last_id
      2. Subsequent: GET /workflows/{name}/log/{job_id}?since_id=<last_id> → returns only new lines

    Response:
    {
      "job_id": "wf-abc123",
      "status": "running",
      "lines": [
        {"id": 1, "host": "10.1.1.1", "line": "[10.1.1.1] Step 1: ...", "created_at": "..."},
        ...
      ],
      "last_id": 42,
      "hosts": ["10.1.1.1", "10.1.1.2"]
    }
    """
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job_status = store.get_status(job_id)
    lines = store.get_workflow_log_all(job_id, since_id=since_id)
    hosts = store.get_workflow_log_hosts(job_id)

    last_id = lines[-1]["id"] if lines else since_id

    return {
        "job_id":  job_id,
        "status":  job_status.status.value if job_status else "unknown",
        "lines":   lines,
        "last_id": last_id,
        "hosts":   hosts,
    }


@router.get(
    "/{name}/log/{job_id}/{host}",
    summary="Poll workflow log for a single device",
)
def get_workflow_log_device(
    name: str,
    job_id: str,
    host: str,
    since_id: int = Query(0, ge=0, description="Return only lines with id > since_id."),
):
    """
    Per-device log polling endpoint. Same incremental since_id mechanism as the
    all-devices endpoint but filtered to a single host.
    """
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job_status = store.get_status(job_id)
    lines = store.get_workflow_log(job_id, host, since_id=since_id)
    last_id = lines[-1]["id"] if lines else since_id

    return {
        "job_id":  job_id,
        "host":    host,
        "status":  job_status.status.value if job_status else "unknown",
        "lines":   lines,
        "last_id": last_id,
    }
