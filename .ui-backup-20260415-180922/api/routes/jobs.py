"""
api/routes/jobs.py

Job endpoints.  Additions over the existing implementation:

  GET /jobs/{id}/detail   Per-device results with full command output
                          (required by the UI live log viewer)

  GET /jobs               Added ?limit and ?offset for UI pagination
                          Added ?status filter (existing but now documented)

The rest of the file is unchanged from the existing implementation —
paste this over api/routes/jobs.py keeping the existing imports intact.
Only the new/changed sections are shown with ADDED / CHANGED comments.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from api.schemas import JobSubmitRequest, JobMode
from core.job_store import job_store          # your existing in-memory store
from core.executor import submit_job          # your existing executor

router = APIRouter(prefix="/jobs", tags=["jobs"])


# ─────────────────────────────────────────────────────────────────────────────
# POST /jobs — submit a new job (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED, summary="Submit a new job")
def submit(req: JobSubmitRequest):
    job_id = submit_job(req)
    return {"job_id": job_id, "status": "accepted"}


# ─────────────────────────────────────────────────────────────────────────────
# GET /jobs — list jobs  [CHANGED: added limit/offset/status pagination]
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", summary="List jobs with optional pagination and status filter")
def list_jobs(
    status_filter: Optional[str] = Query(None, alias="status",
        description="Filter by job status: running, completed, failed, partial_failure"),
    limit:  int = Query(50,  ge=1, le=200, description="Max jobs to return"),
    offset: int = Query(0,   ge=0,         description="Pagination offset"),
):
    all_jobs = job_store.list_jobs()   # returns list[JobSummary], newest first

    if status_filter:
        all_jobs = [j for j in all_jobs if j.status == status_filter]

    total     = len(all_jobs)
    page_jobs = all_jobs[offset: offset + limit]

    return {
        "total":  total,
        "offset": offset,
        "limit":  limit,
        "jobs": [
            {
                "id":       j.job_id,
                "mode":     j.mode,
                "status":   j.status,
                "devices":  j.device_targets,    # list of group/host names
                "progress": j.completed_devices,
                "total":    j.total_devices,
                "started":  j.started_at,
                "duration": j.duration_str,
            }
            for j in page_jobs
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /jobs/{id} — single job status (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{job_id}", summary="Get job status")
def get_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Job '{job_id}' not found")
    return {
        "id":       job.job_id,
        "mode":     job.mode,
        "status":   job.status,
        "devices":  job.device_targets,
        "progress": job.completed_devices,
        "total":    job.total_devices,
        "started":  job.started_at,
        "duration": job.duration_str,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /jobs/{id}/detail  [ADDED — per-device results for the UI log viewer]
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{job_id}/detail", summary="Get per-device results for a job")
def get_job_detail(job_id: str):
    """
    Returns the per-device breakdown of a job including command output.

    Used by the UI job detail view and the SSE stream (which calls this
    endpoint every 2 seconds while the job is running).

    Response shape:
    {
      "id":       "job-abc123",
      "status":   "running",
      "progress": 3,
      "total":    10,
      "devices":  [
        {
          "host":     "10.0.0.1",
          "platform": "cisco_xr",
          "status":   "completed",
          "duration": 4.2,
          "output":   ["show version", "Cisco IOS XR Software…", …],
          "error":    null
        },
        …
      ]
    }
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Job '{job_id}' not found")

    device_results = job_store.get_device_results(job_id)  # list[DeviceResult]

    devices = []
    for dr in device_results:
        # Flatten command output into a single list of strings for the log viewer.
        # Each CommandResult has .command and .output (and optionally .error).
        output_lines: list[str] = []
        for cr in (dr.commands or []):
            output_lines.append(f"# {cr.command}")
            if cr.output:
                output_lines.extend(cr.output.splitlines())
            if cr.error:
                output_lines.append(f"ERROR: {cr.error}")

        devices.append({
            "host":     dr.host,
            "platform": dr.platform,
            "status":   dr.status,
            "duration": dr.duration_seconds,
            "output":   output_lines,
            "error":    dr.error,
        })

    return {
        "id":       job.job_id,
        "mode":     job.mode,
        "status":   job.status,
        "progress": job.completed_devices,
        "total":    job.total_devices,
        "started":  job.started_at,
        "duration": job.duration_str,
        "devices":  devices,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /jobs/{id} — cancel a running job (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{job_id}", summary="Cancel a running job")
def cancel_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Job '{job_id}' not found")
    if job.status not in ("running", "pending"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is already in terminal state '{job.status}'",
        )
    job_store.cancel(job_id)
    return {"status": "cancelled", "job_id": job_id}
