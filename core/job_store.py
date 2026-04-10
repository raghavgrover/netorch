"""
core/job_store.py — Thread-safe in-memory job registry with JSON persistence.

Phase 2 additions:
  - mark_failed()    — terminal failure state (executor error or queue full)
  - mark_cancelled() — job was cancelled via DELETE /jobs/{id}
  - list_jobs()      — filterable job listing for GET /jobs
"""
from __future__ import annotations
import threading
import orjson
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from api.schemas import (
    JobMode, JobStatus, JobSummary,
    JobStatusResponse, JobDetailResponse, DeviceResult,
)
from core.config import logging_cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self):
        self._lock  = threading.Lock()
        self._jobs: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def create(self, job_id: str, mode: JobMode, device_count: int) -> None:
        record = {
            "job_id":       job_id,
            "mode":         mode.value,
            "status":       JobStatus.queued.value,
            "started_at":   None,
            "completed_at": None,
            "device_count": device_count,
            "devices":      {},
            "error":        None,
        }
        with self._lock:
            self._jobs[job_id] = record
        self._persist(job_id)

    def update_device_count(self, job_id: str, count: int) -> None:
        """Update device_count after group expansion reveals the real total."""
        with self._lock:
            self._jobs[job_id]["device_count"] = count
        self._persist(job_id)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            self._jobs[job_id]["status"]     = JobStatus.running.value
            self._jobs[job_id]["started_at"] = _now_iso()
        self._persist(job_id)

    def update_device(self, job_id: str, result: DeviceResult) -> None:
        with self._lock:
            self._jobs[job_id]["devices"][result.host] = result.model_dump()
        self._persist(job_id)

    def mark_complete(self, job_id: str) -> None:
        with self._lock:
            job     = self._jobs[job_id]
            devices = list(job["devices"].values())
            failed  = sum(1 for d in devices if d["status"] == "failed")
            job["completed_at"] = _now_iso()
            job["status"] = (
                JobStatus.partial_failure.value if failed > 0
                else JobStatus.completed.value
            )
        self._persist(job_id)

    def mark_failed(self, job_id: str, reason: str = "") -> None:
        with self._lock:
            self._jobs[job_id]["status"]       = JobStatus.failed.value
            self._jobs[job_id]["completed_at"] = _now_iso()
            self._jobs[job_id]["error"]        = reason
        self._persist(job_id)

    def mark_cancelled(self, job_id: str) -> None:
        with self._lock:
            self._jobs[job_id]["status"]       = JobStatus.cancelled.value
            self._jobs[job_id]["completed_at"] = _now_iso()
        self._persist(job_id)

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def get_status(self, job_id: str) -> Optional[JobStatusResponse]:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return None
        return self._build_status(job)

    def get_detail(self, job_id: str) -> Optional[JobDetailResponse]:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return None
        devices = [DeviceResult(**d) for d in job["devices"].values()]
        status_resp = self._build_status(job)
        return JobDetailResponse(
            job_id       = job["job_id"],
            mode         = JobMode(job["mode"]),
            status       = status_resp.status,
            started_at   = job["started_at"],
            completed_at = job["completed_at"],
            summary      = status_resp.summary,
            devices      = devices,
            error        = job.get("error"),
        )

    def list_jobs(
        self,
        status_filter: Optional[str]  = None,
        mode_filter:   Optional[str]  = None,
        limit:         int            = 100,
        offset:        int            = 0,
    ) -> list[JobStatusResponse]:
        with self._lock:
            jobs = list(self._jobs.values())

        results = []
        for job in reversed(jobs):   # newest first
            if status_filter and job["status"] != status_filter:
                continue
            if mode_filter and job["mode"] != mode_filter:
                continue
            results.append(self._build_status(job))

        return results[offset: offset + limit]

    def log_path(self, job_id: str) -> Optional[Path]:
        p = logging_cfg.log_dir / f"{job_id}.json"
        return p if p.exists() else None

    def exists(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._jobs

    def is_cancellable(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return False
        return job["status"] in (JobStatus.queued.value, JobStatus.running.value)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    _TERMINAL_STATUSES = {
        JobStatus.completed.value,
        JobStatus.partial_failure.value,
        JobStatus.failed.value,
        JobStatus.cancelled.value,
    }

    def _build_status(self, job: dict) -> JobStatusResponse:
        devices     = list(job["devices"].values())
        total       = job["device_count"]
        done        = len(devices)
        success     = sum(1 for d in devices if d["status"] == "success")
        failed      = sum(1 for d in devices if d["status"] == "failed")
        is_terminal = job["status"] in self._TERMINAL_STATUSES

        # In_progress must be 0 for any terminal state — even if device
        # results were never written (e.g. job failed before workers started)
        in_progress = 0 if is_terminal else max(0, total - done)

        return JobStatusResponse(
            job_id       = job["job_id"],
            status       = JobStatus(job["status"]),
            mode         = JobMode(job["mode"]),
            started_at   = job["started_at"],
            completed_at = job["completed_at"],
            summary      = JobSummary(
                total       = total,
                success     = success,
                failed      = failed,
                in_progress = in_progress,
            ),
        )

    def _persist(self, job_id: str) -> None:
        with self._lock:
            snapshot = dict(self._jobs[job_id])
        path = logging_cfg.log_dir / f"{job_id}.json"
        path.write_bytes(orjson.dumps(snapshot, option=orjson.OPT_INDENT_2))


store = JobStore()
