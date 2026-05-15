"""
api/routes/workflows.py — Workflow management endpoints.

Workflows are YAML files stored under /opt/netorch/workflows/.
Each file defines a name, description, parameters, vars, and a list of steps.

Endpoints
─────────
GET  /workflows                              List all .yaml workflow files
GET  /workflows/{name}                       Full metadata + raw_content
POST /workflows                              Create a new .yaml file
PUT  /workflows/{name}                       Overwrite a .yaml file
DELETE /workflows/{name}                     Delete a .yaml file
POST /workflows/{name}/run                   Submit a workflow job
GET  /workflows/{name}/steps/{job_id}        Step-by-step output for a job
GET  /workflows/{name}/log/{job_id}          Live log (all devices)
GET  /workflows/{name}/log/{job_id}/{host}   Live log (single device)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from api.schemas import (
    DeviceEntry, WorkflowOptions, WorkflowSubmitRequest,
    WorkflowSubmitResponse, JobStatus, JobMode,
)
from core.workflow_runner import submit_workflow, active_workflow_count
from core.workflow_parser import parse as parse_workflow, WorkflowParseError
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


def _safe_name(name: str) -> None:
    """Reject path traversal and wrong extension."""
    if "/" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid workflow name.")
    if not name.endswith(".yaml"):
        raise HTTPException(status_code=400, detail="Workflow name must end in .yaml")


def _workflow_meta(path: Path) -> dict:
    """Return metadata dict for a workflow file, with graceful fallback on parse error."""
    st = path.stat()
    modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    base = {
        "filename":    path.name,
        "name":        path.name,
        "description": "",
        "modified_at": modified,
        "size_bytes":  st.st_size,
        "parameters":  [],
        "steps":       [],
    }
    try:
        wf = parse_workflow(path)
        base["name"]        = wf.name
        base["description"] = wf.description
        base["parameters"]  = wf.parameters
        base["steps"] = [
            {"name": s.name, "type": s.type}
            for s in wf.steps
        ]
    except WorkflowParseError:
        pass  # return minimal metadata if YAML is malformed
    return base


def _expand_devices(devices: list[DeviceEntry]) -> list[DeviceEntry]:
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


# ── CRUD routes ───────────────────────────────────────────────────────────────

@router.get("", summary="List all workflow scripts in WORKFLOWS_DIR")
def list_workflows():
    d = _workflows_dir()
    workflows = []
    for p in sorted(d.glob("*.yaml")):
        try:
            workflows.append(_workflow_meta(p))
        except Exception:
            pass
    return {"workflows": workflows, "total": len(workflows)}


@router.get("/{name}", summary="Get workflow metadata, steps, and raw content")
def get_workflow(name: str):
    _safe_name(name)
    path = _workflows_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found.")
    meta = _workflow_meta(path)
    raw  = path.read_text(encoding="utf-8", errors="replace")
    meta["raw_content"] = raw
    try:
        wf = parse_workflow(path)
        meta["steps"] = [
            {
                "name":     s.name,
                "type":     s.type,
                "run":      s.run,
                "commands": s.commands,
                "script":   s.script,
                "local_path":  s.local_path,
                "remote_path": s.remote_path,
                "runbook":     s.runbook,
            }
            for s in wf.steps
        ]
        meta["vars"]        = wf.vars
        meta["parse_error"] = None
    except WorkflowParseError as e:
        meta["parse_error"] = str(e)
    return meta


class WorkflowWriteBody(BaseModel):
    content: str


class WorkflowCreateBody(BaseModel):
    filename: str
    content:  str


@router.post("", status_code=201, summary="Create a new workflow file")
def create_workflow(body: WorkflowCreateBody):
    name = body.filename.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Filename is required.")
    if not name.endswith(".yaml"):
        raise HTTPException(status_code=400, detail="Filename must end in .yaml")
    if "/" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    path = _workflows_dir() / name
    if path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' already exists. Use PUT to overwrite.",
        )
    try:
        path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    return {"name": name, "created": True}


@router.put("/{name}", summary="Overwrite a workflow file")
def put_workflow(name: str, body: WorkflowWriteBody):
    _safe_name(name)
    path = _workflows_dir() / name
    try:
        path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    return {"name": name, "saved": True}


@router.delete("/{name}", summary="Delete a workflow file")
def delete_workflow(name: str):
    _safe_name(name)
    path = _workflows_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found.")
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    return {"name": name, "deleted": True}


# ── Execution routes ──────────────────────────────────────────────────────────

@router.post(
    "/{name}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkflowSubmitResponse,
    summary="Submit a workflow job against the specified devices",
)
def run_workflow(name: str, body: WorkflowSubmitRequest):
    _safe_name(name)
    path = _workflows_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found.")

    try:
        parse_workflow(path)   # validate before queuing
    except WorkflowParseError as e:
        raise HTTPException(status_code=422, detail=f"Invalid workflow YAML: {e}")

    if active_workflow_count() >= exec_cfg.max_queue_depth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Job queue full ({exec_cfg.max_queue_depth} max). Retry shortly.",
        )

    job_id = body.job_id or f"wf-{uuid.uuid4().hex[:8]}"
    if store.exists(job_id):
        job_id = f"wf-{uuid.uuid4().hex[:8]}"

    expanded_devices = _expand_devices(body.devices)
    incident = body.incident or None

    submit_workflow(
        script_name=name,
        devices=expanded_devices,
        parameters=body.parameters,
        job_id=job_id,
        options=body.options,
        incident=incident,
    )

    log.info("workflow_job_accepted", workflow=name, job_id=job_id,
             device_count=len(expanded_devices))

    log_subdir = (logging_cfg.log_dir / incident) if incident else logging_cfg.log_dir
    return WorkflowSubmitResponse(
        job_id       = job_id,
        status       = JobStatus.queued,
        device_count = len(expanded_devices),
        script       = name,
        log_path     = str(log_subdir / f"{job_id}.json"),
    )


@router.get(
    "/{name}/steps/{job_id}",
    summary="Return step-by-step output for a workflow job",
)
def get_workflow_steps(name: str, job_id: str):
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    outputs = store.get_step_outputs(job_id)
    job_status = store.get_status(job_id)
    return {
        "job_id":  job_id,
        "status":  job_status.status.value if job_status else "unknown",
        "outputs": outputs,
    }


@router.get(
    "/{name}/log/{job_id}",
    summary="Poll workflow log for all devices",
)
def get_workflow_log_all(
    name: str,
    job_id: str,
    since_id: int = Query(0, ge=0),
):
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
    since_id: int = Query(0, ge=0),
):
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
