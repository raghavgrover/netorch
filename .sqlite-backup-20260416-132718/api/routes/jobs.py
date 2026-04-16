"""
api/routes/jobs.py — Job lifecycle endpoints.

Only change from original: GET /jobs uses store.count_jobs() for the
accurate total count (SQLite COUNT(*)) instead of len(jobs) which would
only count the current page.
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
    expanded: list[DeviceEntry] = []
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No devices resolved after expanding groups.")
    return expanded


@router.post("", status_code=status.HTTP_202_ACCEPTED,
             response_model=JobSubmitResponse,
             summary="Submit an audit or remediation job")
def create_job(request: JobSubmitRequest) -> JobSubmitResponse:
    if store.exists(request.job_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Job '{request.job_id}' already exists.")
    if active_job_count() >= exec_cfg.max_queue_depth:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"Job queue full ({exec_cfg.max_queue_depth} max). Retry shortly.")
    if request.file_transfers:
        for ft in request.file_transfers:
            if not os.path.isfile(ft.local_path):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail=f"file_transfers: local_path not found: {ft.local_path}")
    request.devices = _expand_devices(request.devices)
    if not request.commands and not request.file_transfers:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="At least one of 'commands' or 'file_transfers' must be provided.")
    submit_job(request)
    log.info("job_accepted", job_id=request.job_id, device_count=len(request.devices))
    return JobSubmitResponse(
        job_id       = request.job_id,
        status       = JobStatus.queued,
        device_count = len(request.devices),
        log_path     = str(logging_cfg.log_dir / f"{request.job_id}.json"),
    )


@router.get("", response_model=JobListResponse,
            summary="List jobs with optional filters and pagination")
def list_jobs(
    status_filter: str | None = Query(None, alias="status"),
    mode_filter:   str | None = Query(None, alias="mode"),
    limit:  int = Query(50, ge=1, le=500),
    offset: int = Query(0,  ge=0),
) -> JobListResponse:
    # Use DB COUNT(*) for the true total — not just len(current page)
    total = store.count_jobs(status_filter=status_filter, mode_filter=mode_filter)
    jobs  = store.list_jobs(status_filter=status_filter, mode_filter=mode_filter,
                            limit=limit, offset=offset)
    return JobListResponse(total=total, offset=offset, limit=limit, jobs=jobs)


@router.get("/{job_id}", response_model=JobStatusResponse,
            summary="Poll job status and summary")
def get_job_status(job_id: str) -> JobStatusResponse:
    result = store.get_status(job_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return result


@router.get("/{job_id}/detail", summary="Full per-device results for a job")
def get_job_detail(job_id: str):
    detail: JobDetailResponse | None = store.get_detail(job_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    devices_out = []
    for dr in detail.devices:
        output_lines: list[str] = []
        for cr in (dr.commands or []):
            output_lines.append(f"# {cr.command}")
            if cr.output:
                output_lines.extend(cr.output.splitlines())
            if cr.error:
                output_lines.append(f"ERROR: {cr.error}")
        devices_out.append({
            "host":     dr.host,
            "platform": dr.platform,
            "status":   dr.status.value,
            "duration": dr.duration_seconds,
            "output":   output_lines,
            "error":    dr.error,
        })

    return {
        "id":       detail.job_id,
        "mode":     detail.mode.value,
        "status":   detail.status.value,
        "progress": detail.summary.success + detail.summary.failed,
        "total":    detail.summary.total,
        "started":  detail.started_at,
        "duration": detail.completed_at,
        "devices":  devices_out,
    }


@router.delete("/{job_id}", response_model=CancelResponse,
               summary="Cancel a queued or running job")
def cancel_job_endpoint(job_id: str) -> CancelResponse:
    if not store.exists(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not store.is_cancellable(job_id):
        raise HTTPException(status_code=409,
                            detail=f"Job '{job_id}' is already in a terminal state.")
    cancel_job(job_id)
    log.info("job_cancel_accepted", job_id=job_id)
    return CancelResponse(job_id=job_id,
                          message="Cancellation signal sent. In-flight sessions will complete before stopping.")
