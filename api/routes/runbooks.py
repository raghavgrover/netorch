"""
api/routes/runbooks.py — Runbook management endpoints.

GET  /runbooks              List all .sh runbooks in /opt/netorch/runbooks
GET  /runbooks/{name}       Fetch a runbook's content and metadata
POST /runbooks/{name}/run   Submit a job that runs a runbook on given devices
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from api.schemas import (
    DeviceEntry, JobOptions, JobSubmitRequest, JobSubmitResponse,
    JobStatus, JobMode,
)
from core.executor import submit_job, active_job_count
from core.job_store import store
from core.config import logging_cfg, executor as exec_cfg
from core.logger import get_logger
from secrets.inventory import inventory_client

RUNBOOKS_DIR = Path("/opt/netorch/runbooks")

log    = get_logger("api.runbooks")
router = APIRouter(prefix="/runbooks", tags=["runbooks"])


# ─── helpers ─────────────────────────────────────────────────────────────────

def _runbooks_dir() -> Path:
    RUNBOOKS_DIR.mkdir(parents=True, exist_ok=True)
    return RUNBOOKS_DIR


def _parse_runbook(path: Path) -> dict:
    """Read a runbook file and return metadata + extracted commands."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot read runbook: {e}")

    lines = text.splitlines()

    # First non-shebang comment line becomes the description
    description = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#!"):
            continue
        if stripped.startswith("#"):
            description = stripped.lstrip("#").strip()
            break

    # Commands: non-blank, non-comment lines
    commands = [
        line.rstrip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]

    st = path.stat()
    modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()

    return {
        "name":          path.name,
        "description":   description,
        "modified_at":   modified,
        "size_bytes":    st.st_size,
        "command_count": len(commands),
        "commands":      commands,
    }


def _expand_devices(devices: list) -> list:
    """Expand group-only DeviceEntry objects into individual host entries."""
    expanded = []
    for entry in devices:
        if entry.host:
            expanded.append(entry)
        else:
            try:
                group_creds = inventory_client.get_group_hosts(entry.group)
            except RuntimeError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            for creds in group_creds:
                expanded.append(DeviceEntry(host=creds.host, group=entry.group, platform=creds.platform))
    if not expanded:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No devices resolved after expanding groups.",
        )
    return expanded


# ─── routes ──────────────────────────────────────────────────────────────────

@router.get("", summary="List all runbooks in /opt/netorch/runbooks")
def list_runbooks():
    d = _runbooks_dir()
    runbooks = []
    for p in sorted(d.glob("*.sh")):
        try:
            rb = _parse_runbook(p)
            rb.pop("commands", None)   # metadata only in listing
            runbooks.append(rb)
        except HTTPException:
            pass  # skip unreadable files silently
    return {"runbooks": runbooks, "total": len(runbooks)}


class RunbookWriteBody(BaseModel):
    content: str


@router.put("/{name}", summary="Overwrite a runbook file with new content")
def put_runbook(name: str, body: RunbookWriteBody):
    if "/" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid runbook name.")
    if not name.endswith(".sh"):
        raise HTTPException(status_code=400, detail="Runbook name must end in .sh")
    path = _runbooks_dir() / name
    try:
        path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    return {"name": name, "saved": True}


@router.get("/{name}", summary="Get runbook content and extracted commands")
def get_runbook(name: str):
    if "/" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid runbook name.")
    path = _runbooks_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Runbook '{name}' not found.")
    return _parse_runbook(path)


@router.post(
    "/{name}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobSubmitResponse,
    summary="Run a runbook on the specified devices",
)
def run_runbook(name: str, body: dict):
    """
    body: { "devices": [...], "options": {...} }
    Devices follow the same DeviceEntry format as /jobs.
    The runbook's non-comment lines are submitted as commands in audit mode.
    """
    # Validate runbook name
    if "/" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid runbook name.")

    # Locate and parse runbook
    path = _runbooks_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Runbook '{name}' not found.")
    rb = _parse_runbook(path)
    commands = rb["commands"]
    if not commands:
        raise HTTPException(
            status_code=400,
            detail=f"Runbook '{name}' has no executable commands (all lines are blank or comments).",
        )

    # Parse devices
    raw_devices = body.get("devices", [])
    if not raw_devices:
        raise HTTPException(status_code=400, detail="'devices' is required.")
    try:
        device_entries = [
            DeviceEntry(**d) if isinstance(d, dict) else d
            for d in raw_devices
        ]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid device entry: {e}")

    # Parse options
    raw_opts = body.get("options", {})
    try:
        options = JobOptions(**raw_opts) if raw_opts else JobOptions()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid options: {e}")

    # Check queue capacity
    if active_job_count() >= exec_cfg.max_queue_depth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Job queue full ({exec_cfg.max_queue_depth} max). Retry shortly.",
        )

    # Expand groups → individual hosts
    device_entries = _expand_devices(device_entries)

    # Build job_id with runbook name prefix for visibility in job listings
    # Use str.replace for Python 3.8 compatibility (removesuffix requires 3.9+)
    rb_stem = name[:-3] if name.endswith(".sh") else name
    job_id = f"runbook-{rb_stem}-{uuid.uuid4().hex[:6]}"

    # Guard against duplicate job_id (extremely unlikely but match jobs.py behaviour)
    if store.exists(job_id):
        job_id = f"runbook-{rb_stem}-{uuid.uuid4().hex[:8]}"

    # Build and submit the job request
    incident = body.get("incident") or None
    request = JobSubmitRequest(
        job_id=job_id,
        mode=JobMode.audit,
        devices=device_entries,
        commands=commands,
        options=options,
        incident=incident,
    )

    submit_job(request)
    log.info(
        "runbook_job_accepted",
        runbook=name,
        job_id=job_id,
        device_count=len(device_entries),
        command_count=len(commands),
    )

    log_subdir = (logging_cfg.log_dir / incident) if incident else logging_cfg.log_dir
    return JobSubmitResponse(
        job_id       = job_id,
        status       = JobStatus.queued,
        device_count = len(device_entries),
        log_path     = str(log_subdir / f"{job_id}.json"),
    )
