"""
api/routes/jobs.py — Job lifecycle endpoints.

POST   /jobs                Submit a new job
GET    /jobs                List jobs (with filters)
GET    /jobs/{id}           Poll job status
GET    /jobs/{id}/detail    Full per-device results
DELETE /jobs/{id}           Cancel a queued or running job

Device entry forms accepted in POST /jobs:

  {"host": "10.0.0.1"}
      → targets that host; credentials looked up by host then group fallback

  {"host": "10.0.0.1", "group": "core_switches"}
      → targets that host; group used as credential fallback

  {"group": "core_switches"}
      → expanded to ALL hosts in that group before execution
"""
import os
from fastapi import APIRouter, HTTPException, Query, status

from api.schemas import (
    JobSubmitRequest, JobSubmitResponse, DeviceEntry,
    JobStatusResponse, JobDetailResponse,
    JobListResponse, CancelResponse,
    JobStatus,
)
from core.executor import submit_job, cancel_job, active_job_count
from core.job_store import store
from core.config import logging_cfg, executor as exec_cfg
from core.logger import get_logger
from secrets.inventory import inventory_client

log    = get_logger("api.jobs")
router = APIRouter(prefix="/jobs", tags=["jobs"])


def _expand_devices(devices: list[DeviceEntry]) -> list[DeviceEntry]:
    """
    Expand any group-only entries into individual host entries.

    {"group": "core_switches"}
        → [{"host": "10.0.0.1", "group": "core_switches", "platform": "cisco_ios"},
           {"host": "10.0.0.2", "group": "core_switches", "platform": "cisco_ios"}, ...]

    Entries that already have a host are passed through unchanged.
    Raises HTTPException 400 if a group name is not found in inventory.
    """
    expanded: list[DeviceEntry] = []

    for entry in devices:
        if entry.host:
            # Already has a concrete host — pass through as-is
            expanded.append(entry)
        else:
            # Group-only entry — expand to all hosts in the group
            try:
                group_creds = inventory_client.get_group_hosts(entry.group)
            except RuntimeError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
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
            detail="No devices resolved after expanding groups. Check your inventory.",
        )

    return expanded


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobSubmitResponse,
    summary="Submit an audit or remediation job",
)
def create_job(request: JobSubmitRequest) -> JobSubmitResponse:
    if store.exists(request.job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job '{request.job_id}' already exists.",
        )
    if active_job_count() >= exec_cfg.max_queue_depth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Job queue full ({exec_cfg.max_queue_depth} max concurrent jobs). Retry shortly.",
        )

    # Validate that all file transfer sources exist on the relay
    if request.file_transfers:
        for ft in request.file_transfers:
            if not os.path.isfile(ft.local_path):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"file_transfers: local_path not found on relay: {ft.local_path}",
                )

    # Expand group-only entries before submitting to the executor
    request.devices = _expand_devices(request.devices)

    if not request.commands and not request.file_transfers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of 'commands' or 'file_transfers' must be provided.",
        )

    submit_job(request)
    log.info("job_accepted",
             job_id=request.job_id,
             device_count=len(request.devices))

    return JobSubmitResponse(
        job_id       = request.job_id,
        status       = JobStatus.queued,
        device_count = len(request.devices),
        log_path     = str(logging_cfg.log_dir / f"{request.job_id}.json"),
    )


@router.get(
    "",
    response_model=JobListResponse,
    summary="List jobs with optional filters",
)
def list_jobs(
    status_filter: str | None = Query(None, alias="status",
        description="queued|running|completed|partial_failure|failed|cancelled"),
    mode_filter:   str | None = Query(None, alias="mode",
        description="audit|remediate"),
    limit:  int = Query(50, ge=1, le=500),
    offset: int = Query(0,  ge=0),
) -> JobListResponse:
    jobs = store.list_jobs(
        status_filter = status_filter,
        mode_filter   = mode_filter,
        limit         = limit,
        offset        = offset,
    )
    return JobListResponse(total=len(jobs), offset=offset, limit=limit, jobs=jobs)


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll job status and summary",
)
def get_job_status(job_id: str) -> JobStatusResponse:
    result = store.get_status(job_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return result


@router.get(
    "/{job_id}/detail",
    response_model=JobDetailResponse,
    summary="Full per-device results for a job",
)
def get_job_detail(job_id: str) -> JobDetailResponse:
    result = store.get_detail(job_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return result


@router.delete(
    "/{job_id}",
    response_model=CancelResponse,
    summary="Cancel a queued or running job",
)
def cancel_job_endpoint(job_id: str) -> CancelResponse:
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not store.is_cancellable(job_id):
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' is already in a terminal state.",
        )
    cancel_job(job_id)
    log.info("job_cancel_accepted", job_id=job_id)
    return CancelResponse(
        job_id  = job_id,
        message = "Cancellation signal sent. In-flight device sessions will complete before the job stops.",
    )
