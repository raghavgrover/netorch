"""
core/job_store.py — SQLite-backed job registry.

Replaces the previous in-memory dict + JSON-file approach with a proper
SQLite database at the path defined in netorch.toml [database] db_path
(defaults to /opt/netorch/netorch.db).

Benefits over the previous implementation:
  - Jobs survive service restarts natively — no startup scanning needed
  - Concurrent reads are safe (WAL journal mode)
  - list_jobs() sorts by started_at in the database — no in-memory sorting
  - Separate devices table avoids loading large blobs for list views

Schema
------
  jobs    — one row per job (id, mode, status, timestamps, device_count, error)
  devices — one row per DeviceResult per job (normalised from the old JSON blob)

Public API (identical to the original — all callers unchanged)
--------------------------------------------------------------
  create(job_id, mode, device_count)
  update_device_count(job_id, count)
  mark_running(job_id)
  update_device(job_id, result)
  mark_complete(job_id)
  mark_failed(job_id, reason)
  mark_cancelled(job_id)
  get_status(job_id)  → Optional[JobStatusResponse]
  get_detail(job_id)  → Optional[JobDetailResponse]
  list_jobs(status_filter, mode_filter, limit, offset) → list[JobStatusResponse]
  log_path(job_id)    → Optional[Path]
  exists(job_id)      → bool
  is_cancellable(job_id) → bool

Thread safety
-------------
  - WAL mode allows concurrent readers with one writer
  - All writes go through a threading.Lock() to serialise them
  - Reads open a short-lived read-only connection (no lock needed in WAL)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import orjson

from api.schemas import (
    CommandResult, DeviceResult, DeviceStatus,
    JobDetailResponse, JobListResponse, JobMode,
    JobStatus, JobStatusResponse, JobSummary,
)
from core.config import database as database_cfg, logging_cfg


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── DDL ───────────────────────────────────────────────────────────────────────

_CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    mode          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    started_at    TEXT,
    completed_at  TEXT,
    device_count  INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL
);
"""

_CREATE_DEVICES = """
CREATE TABLE IF NOT EXISTS devices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT    NOT NULL REFERENCES jobs(job_id),
    host             TEXT    NOT NULL,
    platform         TEXT,
    status           TEXT    NOT NULL DEFAULT 'pending',
    duration_seconds REAL,
    commands_json    TEXT    NOT NULL DEFAULT '[]',
    config_backup    TEXT,
    error            TEXT,
    UNIQUE(job_id, host)
);
"""

_CREATE_IDX_JOB_ID   = "CREATE INDEX IF NOT EXISTS idx_devices_job_id ON devices(job_id);"
_CREATE_IDX_STATUS   = "CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status);"
_CREATE_IDX_CREATED  = "CREATE INDEX IF NOT EXISTS idx_jobs_created   ON jobs(created_at);"


class JobStore:

    def __init__(self) -> None:
        self._db_path  = database_cfg.db_path
        self._lock     = threading.Lock()   # serialises all writes
        self._init_db()

    # ── Schema init ───────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create tables and indexes. Safe to call on every startup (IF NOT EXISTS)."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_conn() as conn:
            conn.execute(_CREATE_JOBS)
            conn.execute(_CREATE_DEVICES)
            conn.execute(_CREATE_IDX_JOB_ID)
            conn.execute(_CREATE_IDX_STATUS)
            conn.execute(_CREATE_IDX_CREATED)
            # WAL mode: readers never block writers and vice versa
            conn.execute("PRAGMA journal_mode=WAL;")
            # fsync on commit — safe default; change to OFF only for benchmarks
            conn.execute("PRAGMA synchronous=NORMAL;")

    # ── Connection helpers ────────────────────────────────────────────────────

    @contextmanager
    def _write_conn(self):
        """
        Context manager that yields a write connection inside a transaction.
        The threading.Lock() ensures only one writer at a time.
        """
        with self._lock:
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=10,
            )
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _read_conn(self) -> sqlite3.Connection:
        """
        Open a short-lived read-only connection.
        WAL mode allows this to run concurrently with writers.
        """
        conn = sqlite3.connect(
            f"file:{self._db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # ── Write side ────────────────────────────────────────────────────────────

    def create(self, job_id: str, mode: JobMode, device_count: int) -> None:
        with self._write_conn() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (job_id, mode, status, device_count, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, mode.value, JobStatus.queued.value, device_count, _now_iso()),
            )

    def update_device_count(self, job_id: str, count: int) -> None:
        with self._write_conn() as conn:
            conn.execute(
                "UPDATE jobs SET device_count=? WHERE job_id=?",
                (count, job_id),
            )

    def mark_running(self, job_id: str) -> None:
        with self._write_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, started_at=? WHERE job_id=?",
                (JobStatus.running.value, _now_iso(), job_id),
            )

    def update_device(self, job_id: str, result: DeviceResult) -> None:
        """
        Upsert a device result row.
        commands are stored as a JSON array so they survive round-trips
        through DeviceResult(**row) without losing any fields.
        """
        commands_json = json.dumps(
            [c.model_dump() for c in (result.commands or [])],
            default=str,
        )
        with self._write_conn() as conn:
            conn.execute(
                """INSERT INTO devices
                       (job_id, host, platform, status,
                        duration_seconds, commands_json, config_backup, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, host) DO UPDATE SET
                       platform         = excluded.platform,
                       status           = excluded.status,
                       duration_seconds = excluded.duration_seconds,
                       commands_json    = excluded.commands_json,
                       config_backup    = excluded.config_backup,
                       error            = excluded.error""",
                (
                    job_id,
                    result.host,
                    result.platform,
                    result.status.value,
                    result.duration_seconds,
                    commands_json,
                    result.config_backup,
                    result.error,
                ),
            )

    def mark_complete(self, job_id: str) -> None:
        """Determine partial_failure vs completed by counting failed devices."""
        with self._write_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM devices WHERE job_id=? AND status='failed'",
                (job_id,),
            ).fetchone()
            failed = row[0] if row else 0
            new_status = (
                JobStatus.partial_failure.value if failed > 0
                else JobStatus.completed.value
            )
            conn.execute(
                "UPDATE jobs SET status=?, completed_at=? WHERE job_id=?",
                (new_status, _now_iso(), job_id),
            )

    def mark_failed(self, job_id: str, reason: str = "") -> None:
        with self._write_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, completed_at=?, error=? WHERE job_id=?",
                (JobStatus.failed.value, _now_iso(), reason, job_id),
            )

    def mark_cancelled(self, job_id: str) -> None:
        with self._write_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, completed_at=? WHERE job_id=?",
                (JobStatus.cancelled.value, _now_iso(), job_id),
            )

    # ── Read side ─────────────────────────────────────────────────────────────

    def get_status(self, job_id: str) -> Optional[JobStatusResponse]:
        conn = self._read_conn()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if not row:
                return None
            return self._status_from_row(conn, dict(row))
        finally:
            conn.close()

    def get_detail(self, job_id: str) -> Optional[JobDetailResponse]:
        conn = self._read_conn()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if not row:
                return None
            job      = dict(row)
            status_r = self._status_from_row(conn, job)
            devices  = self._devices_for_job(conn, job_id)
            return JobDetailResponse(
                job_id       = job["job_id"],
                mode         = JobMode(job["mode"]),
                status       = status_r.status,
                started_at   = job["started_at"],
                completed_at = job["completed_at"],
                summary      = status_r.summary,
                devices      = devices,
                error        = job.get("error"),
            )
        finally:
            conn.close()

    def list_jobs(
        self,
        status_filter: Optional[str] = None,
        mode_filter:   Optional[str] = None,
        limit:         int           = 100,
        offset:        int           = 0,
    ) -> list[JobStatusResponse]:
        """
        Return jobs newest-first. Filtering and pagination happen in SQL
        so no large Python lists are built regardless of history size.
        """
        where_clauses = []
        params: list = []
        if status_filter:
            where_clauses.append("status = ?")
            params.append(status_filter)
        if mode_filter:
            where_clauses.append("mode = ?")
            params.append(mode_filter)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        # Order by created_at DESC — reflects submission order reliably even
        # when started_at is null (queued jobs that haven't run yet)
        sql = f"""
            SELECT * FROM jobs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        conn = self._read_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [self._status_from_row(conn, dict(r)) for r in rows]
        finally:
            conn.close()

    def log_path(self, job_id: str) -> Optional[Path]:
        """
        Legacy compatibility: the old code wrote per-job JSON files.
        We still support reading them if they exist (for GET /logs/{id}/raw).
        New jobs won't have these files — callers should handle None gracefully.
        """
        p = logging_cfg.log_dir / f"{job_id}.json"
        return p if p.exists() else None

    def exists(self, job_id: str) -> bool:
        conn = self._read_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def is_cancellable(self, job_id: str) -> bool:
        conn = self._read_conn()
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if not row:
                return False
            return row["status"] in (JobStatus.queued.value, JobStatus.running.value)
        finally:
            conn.close()

    # ── Internal helpers ──────────────────────────────────────────────────────

    _TERMINAL_STATUSES = {
        JobStatus.completed.value,
        JobStatus.partial_failure.value,
        JobStatus.failed.value,
        JobStatus.cancelled.value,
    }

    def _status_from_row(
        self,
        conn: sqlite3.Connection,
        job: dict,
    ) -> JobStatusResponse:
        """Build a JobStatusResponse from a jobs row + a device count query."""
        job_id  = job["job_id"]
        total   = job["device_count"]

        counts = conn.execute(
            """SELECT
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed,
                   COUNT(*) AS done
               FROM devices WHERE job_id=?""",
            (job_id,),
        ).fetchone()

        success     = counts["success"] or 0
        failed      = counts["failed"]  or 0
        done        = counts["done"]    or 0
        is_terminal = job["status"] in self._TERMINAL_STATUSES
        in_progress = 0 if is_terminal else max(0, total - done)

        return JobStatusResponse(
            job_id       = job_id,
            status       = JobStatus(job["status"]),
            mode         = JobMode(job["mode"]),
            started_at   = job.get("started_at"),
            completed_at = job.get("completed_at"),
            summary      = JobSummary(
                total       = total,
                success     = success,
                failed      = failed,
                in_progress = in_progress,
            ),
        )

    def _devices_for_job(
        self,
        conn: sqlite3.Connection,
        job_id: str,
    ) -> list[DeviceResult]:
        """Fetch and deserialise all device rows for a job."""
        rows = conn.execute(
            "SELECT * FROM devices WHERE job_id=? ORDER BY id",
            (job_id,),
        ).fetchall()

        results = []
        for row in rows:
            try:
                commands_raw = json.loads(row["commands_json"] or "[]")
                commands = [CommandResult(**c) for c in commands_raw]
            except Exception:
                commands = []

            results.append(DeviceResult(
                host             = row["host"],
                platform         = row["platform"],
                status           = DeviceStatus(row["status"]),
                duration_seconds = row["duration_seconds"] or 0.0,
                commands         = commands,
                config_backup    = row["config_backup"],
                error            = row["error"],
            ))
        return results


# Module-level singleton — identical name to original; all callers unchanged
store = JobStore()
