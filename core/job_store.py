"""
core/job_store.py — Thread-safe job registry backed by PostgreSQL.

Drop-in replacement for the previous in-memory + JSON implementation.
All public methods have identical signatures so no other file needs changing.

What changed from original
──────────────────────────
Before: self._jobs dict (in-memory) + JSON files written on every mutation
After:  PostgreSQL via core.db.db  + JSON files kept as downloadable exports

New in workflow branch
──────────────────────
append_workflow_log()   — called per stdout line from workflow subprocesses
get_workflow_log()      — return lines for a single device (supports since_id)
get_workflow_log_all()  — return lines for all devices in a job (since_id)
get_workflow_log_hosts()— return distinct hosts that have written log lines
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from api.schemas import (
    JobMode, JobStatus, JobSummary,
    JobStatusResponse, JobDetailResponse, DeviceResult, CommandResult,
)
from core.config import logging_cfg
from core.db import db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def create(self, job_id: str, mode: JobMode, device_count: int, incident: Optional[str] = None) -> None:
        db.create_job(job_id, mode.value, device_count, incident=incident)
        self._export_json(job_id)

    def update_device_count(self, job_id: str, count: int) -> None:
        db.update_job_device_count(job_id, count)

    def mark_running(self, job_id: str) -> None:
        db.mark_job_running(job_id, _now_iso())
        self._export_json(job_id)

    def update_device(self, job_id: str, result: DeviceResult) -> None:
        db.upsert_device(
            job_id=job_id,
            host=result.host,
            platform=result.platform,
            status=result.status.value,
            duration_seconds=result.duration_seconds,
            error=result.error,
            config_backup=result.config_backup,
        )
        db.insert_commands(
            job_id=job_id,
            host=result.host,
            commands=[c.model_dump() for c in result.commands],
        )
        self._export_json(job_id)

    def mark_complete(self, job_id: str) -> None:
        counts = db.get_job_summary_counts(job_id)
        if counts["failed"] == 0 and counts["success"] > 0:
            status = JobStatus.completed.value
        elif counts["success"] == 0 and counts["failed"] == 0:
            # No device rows at all — typical of shell/once-only workflows.
            # Fall back to workflow_step_outputs exit codes to determine status.
            step_rows = db.get_step_outputs(job_id)
            if not step_rows:
                status = JobStatus.failed.value
            elif any(r.get("exit_code", 0) != 0 for r in step_rows):
                # Some steps failed but the workflow continued (on_error: continue)
                status = JobStatus.partial_failure.value
            else:
                status = JobStatus.completed.value
        elif counts["success"] == 0:
            # Has device entries but all failed
            status = JobStatus.failed.value
        else:
            status = JobStatus.partial_failure.value
        db.mark_job_complete(job_id, status, _now_iso())
        self._export_json(job_id)

    def mark_failed(self, job_id: str, reason: str = "") -> None:
        db.mark_job_failed(job_id, _now_iso(), reason)
        self._export_json(job_id)

    def mark_cancelled(self, job_id: str) -> None:
        db.mark_job_cancelled(job_id, _now_iso())
        self._export_json(job_id)

    # ------------------------------------------------------------------
    # Workflow log methods  ← NEW
    # ------------------------------------------------------------------

    def append_workflow_log(self, job_id: str, host: str, line: str) -> None:
        """
        Append a single stdout line from a workflow subprocess.
        Called at high frequency (once per line) — must not block.
        """
        db.append_workflow_log(job_id, host, line)

    def get_workflow_log(
        self,
        job_id: str,
        host: str,
        since_id: int = 0,
    ) -> list[dict]:
        """
        Return workflow log lines for a single device.

        Supports incremental polling: pass the `id` of the last received
        row as `since_id` to get only new lines since that point.
        Returns list of dicts with keys: id, line, created_at.
        """
        rows = db.get_workflow_log(job_id, host, since_id=since_id)
        return [{"id": r["id"], "line": r["line"], "created_at": r["created_at"]} for r in rows]

    def get_workflow_log_all(
        self,
        job_id: str,
        since_id: int = 0,
    ) -> list[dict]:
        """
        Return workflow log lines for all devices in a job.
        Includes `host` field so the caller can group output per device.
        Supports incremental polling via `since_id`.
        """
        rows = db.get_workflow_log_all(job_id, since_id=since_id)
        return [
            {
                "id":         r["id"],
                "host":       r["host"],
                "line":       r["line"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_workflow_log_hosts(self, job_id: str) -> list[str]:
        """Return the distinct device hosts that have written log lines."""
        return db.get_workflow_log_hosts(job_id)

    def get_step_outputs(self, job_id: str) -> list[dict]:
        """Return all step outputs for a workflow job."""
        return db.get_step_outputs(job_id)

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def get_status(self, job_id: str) -> Optional[JobStatusResponse]:
        row = db.get_job(job_id)
        if not row:
            return None
        return self._row_to_status(dict(row))

    def get_detail(self, job_id: str) -> Optional[JobDetailResponse]:
        row = db.get_job(job_id)
        if not row:
            return None
        job = dict(row)

        device_rows = db.get_devices(job_id)
        devices = []
        for dr in device_rows:
            cmd_rows = db.get_commands(job_id, dr["host"])
            commands = [
                CommandResult(
                    command=c["command"],
                    output=c["output"] or "",
                    timestamp=c["timestamp"] or "",
                    error=c["error"],
                )
                for c in cmd_rows
            ]
            devices.append(DeviceResult(
                host=dr["host"],
                platform=dr["platform"],
                status=dr["status"],
                duration_seconds=dr["duration_seconds"] or 0.0,
                commands=commands,
                config_backup=dr["config_backup"],
                error=dr["error"],
            ))

        counts = db.get_job_summary_counts(job_id)
        raw_mode = job["mode"]
        mode_val = self._LEGACY_MODE_MAP.get(raw_mode, raw_mode)
        return JobDetailResponse(
            job_id       = job["job_id"],
            mode         = JobMode(mode_val),
            status       = JobStatus(job["status"]),
            started_at   = job["started_at"],
            completed_at = job["completed_at"],
            summary      = JobSummary(**counts),
            devices      = devices,
            error        = job.get("error"),
        )

    def list_jobs(
        self,
        status_filter: Optional[str] = None,
        mode_filter:   Optional[str] = None,
        limit:  int = 100,
        offset: int = 0,
    ) -> list[JobStatusResponse]:
        rows = db.list_jobs(
            status_filter=status_filter,
            mode_filter=mode_filter,
            limit=limit,
            offset=offset,
        )
        return [self._row_to_status(dict(r)) for r in rows]

    def count_jobs(
        self,
        status_filter: Optional[str] = None,
        mode_filter:   Optional[str] = None,
    ) -> int:
        """Total count for pagination — uses DB COUNT(*) instead of loading all rows."""
        return db.count_jobs(status_filter=status_filter, mode_filter=mode_filter)

    def log_path(self, job_id: str) -> Optional[Path]:
        row = db.query_one("SELECT incident FROM jobs WHERE job_id=%s", (job_id,))
        incident = row["incident"] if row else None
        if incident:
            p = logging_cfg.log_dir / incident / f"{job_id}.json"
            if p.exists():
                return p
        p = logging_cfg.log_dir / f"{job_id}.json"
        return p if p.exists() else None

    def exists(self, job_id: str) -> bool:
        return db.job_exists(job_id)

    def is_cancellable(self, job_id: str) -> bool:
        return db.job_is_cancellable(job_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    _LEGACY_MODE_MAP = {"audit": "run", "remediate": "run"}

    def _row_to_status(self, job: dict) -> JobStatusResponse:
        counts = db.get_job_summary_counts(job["job_id"])
        raw_mode = job["mode"]
        mode_val = self._LEGACY_MODE_MAP.get(raw_mode, raw_mode)
        return JobStatusResponse(
            job_id       = job["job_id"],
            status       = JobStatus(job["status"]),
            mode         = JobMode(mode_val),
            started_at   = job["started_at"],
            completed_at = job["completed_at"],
            summary      = JobSummary(**counts),
        )

    def _export_json(self, job_id: str) -> None:
        """
        Write a JSON snapshot of the job to logs/jobs/<job_id>.json.
        This file is used by GET /logs/{id}/raw and the BigFix script.
        It is an export — SQLite is the source of truth.
        """
        try:
            row = db.get_job(job_id)
            if not row:
                return
            job = dict(row)

            device_rows = db.get_devices(job_id)
            devices_out = {}
            for dr in device_rows:
                cmd_rows = db.get_commands(job_id, dr["host"])
                devices_out[dr["host"]] = {
                    **dict(dr),
                    "commands": [dict(c) for c in cmd_rows],
                }

            incident = job.get("incident")
            if incident:
                log_dir = logging_cfg.log_dir / incident
                log_dir.mkdir(parents=True, exist_ok=True)
            else:
                log_dir = logging_cfg.log_dir
            snapshot = {**job, "devices": devices_out}
            path = log_dir / f"{job_id}.json"
            path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass   # Export failure must never crash a job


store = JobStore()
