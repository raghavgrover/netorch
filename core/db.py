"""
core/db.py — SQLite database layer for netorch.

Single file at /opt/netorch/netorch.db.
WAL journal mode: multiple readers, one writer, no blocking on reads.

Schema
------
jobs           — one row per job (status, timestamps, device_count, error)
devices        — one row per device per job (status, duration, error)
commands       — one row per command result per device per job (output text)
workflow_logs  — streaming per-device stdout lines for workflow jobs  ← NEW

All writes go through the module-level `db` singleton which serialises
them through a threading.Lock to be safe across executor threads.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from core.config import logging_cfg

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    device_count INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    incident     TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    job_id           TEXT    NOT NULL,
    host             TEXT    NOT NULL,
    platform         TEXT,
    status           TEXT    NOT NULL DEFAULT 'pending',
    duration_seconds REAL,
    error            TEXT,
    config_backup    TEXT,
    PRIMARY KEY (job_id, host),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS commands (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT    NOT NULL,
    host      TEXT    NOT NULL,
    command   TEXT    NOT NULL,
    output    TEXT,
    error     TEXT,
    timestamp TEXT,
    FOREIGN KEY (job_id, host) REFERENCES devices(job_id, host)
);

CREATE TABLE IF NOT EXISTS workflow_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT    NOT NULL,
    host       TEXT    NOT NULL,
    line       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_step_outputs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT    NOT NULL,
    step_name  TEXT    NOT NULL,
    host       TEXT,
    output     TEXT,
    exit_code  INTEGER,
    created_at TEXT    NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status       ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_started_at   ON jobs(started_at);
CREATE INDEX IF NOT EXISTS idx_devices_job        ON devices(job_id);
CREATE INDEX IF NOT EXISTS idx_commands_device    ON commands(job_id, host);
CREATE INDEX IF NOT EXISTS idx_wflogs_job_host    ON workflow_logs(job_id, host);
CREATE INDEX IF NOT EXISTS idx_wflogs_job         ON workflow_logs(job_id);
CREATE INDEX IF NOT EXISTS idx_wfsteps_job        ON workflow_step_outputs(job_id);
"""


class Database:
    """
    Thread-safe SQLite wrapper.

    Uses a single persistent connection in WAL mode.  All mutations are
    serialised through self._lock so concurrent executor threads don't
    interleave writes to the same row.  Reads (SELECT) are safe without
    the lock in WAL mode, but we acquire it anyway for simplicity.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn = self._connect()
        self._apply_schema()

    # ── Connection ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,   # we serialise with self._lock
            isolation_level=None,      # autocommit — we call BEGIN/COMMIT manually
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _apply_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Runtime migrations: add columns to existing DBs
            for sql in (
                "ALTER TABLE jobs ADD COLUMN incident TEXT",
            ):
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

    # ── Low-level helpers ─────────────────────────────────────────────────────

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a single statement (INSERT / UPDATE / DELETE). Thread-safe."""
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params_seq) -> None:
        """Execute a statement for each item in params_seq. Thread-safe."""
        with self._lock:
            self._conn.executemany(sql, params_seq)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a SELECT and return all rows. Thread-safe."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Run a SELECT and return first row or None. Thread-safe."""
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def transaction(self, statements: list[tuple[str, tuple]]) -> None:
        """
        Execute multiple statements in a single transaction.
        Rolls back automatically on error.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for sql, params in statements:
                    self._conn.execute(sql, params)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ── Job write operations ──────────────────────────────────────────────────

    def create_job(self, job_id: str, mode: str, device_count: int, incident: Optional[str] = None) -> None:
        self.execute(
            "INSERT INTO jobs (job_id, mode, status, device_count, incident) VALUES (?,?,?,?,?)",
            (job_id, mode, "queued", device_count, incident),
        )

    def update_job_device_count(self, job_id: str, count: int) -> None:
        self.execute(
            "UPDATE jobs SET device_count=? WHERE job_id=?",
            (count, job_id),
        )

    def mark_job_running(self, job_id: str, started_at: str) -> None:
        self.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE job_id=?",
            (started_at, job_id),
        )

    def mark_job_complete(self, job_id: str, status: str, completed_at: str) -> None:
        self.execute(
            "UPDATE jobs SET status=?, completed_at=? WHERE job_id=?",
            (status, completed_at, job_id),
        )

    def mark_job_failed(self, job_id: str, completed_at: str, error: str) -> None:
        self.execute(
            "UPDATE jobs SET status='failed', completed_at=?, error=? WHERE job_id=?",
            (completed_at, error, job_id),
        )

    def mark_job_cancelled(self, job_id: str, completed_at: str) -> None:
        self.execute(
            "UPDATE jobs SET status='cancelled', completed_at=? WHERE job_id=?",
            (completed_at, job_id),
        )

    # ── Device write operations ───────────────────────────────────────────────

    def upsert_device(
        self,
        job_id: str,
        host: str,
        platform: Optional[str],
        status: str,
        duration_seconds: Optional[float],
        error: Optional[str],
        config_backup: Optional[str],
    ) -> None:
        """Insert or replace a device result row."""
        self.execute(
            """INSERT INTO devices
               (job_id, host, platform, status, duration_seconds, error, config_backup)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(job_id, host) DO UPDATE SET
                   platform=excluded.platform,
                   status=excluded.status,
                   duration_seconds=excluded.duration_seconds,
                   error=excluded.error,
                   config_backup=excluded.config_backup
            """,
            (job_id, host, platform, status, duration_seconds, error, config_backup),
        )

    def insert_commands(
        self,
        job_id: str,
        host: str,
        commands: list[dict],
    ) -> None:
        """
        Bulk-insert command results for a device.
        Replaces existing rows for this (job_id, host) to keep idempotent
        with multiple update_device calls during a running job.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "DELETE FROM commands WHERE job_id=? AND host=?",
                    (job_id, host),
                )
                self._conn.executemany(
                    "INSERT INTO commands (job_id, host, command, output, error, timestamp) "
                    "VALUES (?,?,?,?,?,?)",
                    [
                        (job_id, host,
                         c.get("command", ""),
                         c.get("output", ""),
                         c.get("error"),
                         c.get("timestamp"))
                        for c in commands
                    ],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ── Workflow log operations  ← NEW ────────────────────────────────────────

    def append_workflow_log(self, job_id: str, host: str, line: str) -> None:
        """
        Append a single stdout line from a workflow subprocess to the log.
        Called once per line as the subprocess streams output — must be fast.
        We intentionally do NOT use a transaction per line here; WAL mode
        makes individual INSERTs safe and fast without explicit transactions.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.execute(
            "INSERT INTO workflow_logs (job_id, host, line, created_at) VALUES (?,?,?,?)",
            (job_id, host, line, now),
        )

    def get_workflow_log(
        self,
        job_id: str,
        host: str,
        since_id: int = 0,
    ) -> list[sqlite3.Row]:
        """
        Return workflow log rows for a single device, optionally after a
        given row id (for incremental/polling log tailing from the UI).
        """
        return self.query(
            "SELECT id, line, created_at FROM workflow_logs "
            "WHERE job_id=? AND host=? AND id>? ORDER BY id ASC",
            (job_id, host, since_id),
        )

    def get_workflow_log_all(
        self,
        job_id: str,
        since_id: int = 0,
    ) -> list[sqlite3.Row]:
        """
        Return workflow log rows for all devices in a job, ordered by
        insertion time. Includes the host field so the caller can group by device.
        Used by the full-job log view.
        """
        return self.query(
            "SELECT id, host, line, created_at FROM workflow_logs "
            "WHERE job_id=? AND id>? ORDER BY id ASC",
            (job_id, since_id),
        )

    def get_workflow_log_hosts(self, job_id: str) -> list[str]:
        """Return the distinct hosts that have written workflow log lines."""
        rows = self.query(
            "SELECT DISTINCT host FROM workflow_logs WHERE job_id=? ORDER BY host",
            (job_id,),
        )
        return [r["host"] for r in rows]

    # ── Workflow step outputs ─────────────────────────────────────────────────

    def insert_step_output(
        self,
        job_id: str,
        step_name: str,
        host: Optional[str],
        output: str,
        exit_code: int,
    ) -> None:
        from datetime import datetime, timezone
        self.execute(
            "INSERT INTO workflow_step_outputs "
            "(job_id, step_name, host, output, exit_code, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (job_id, step_name, host, output, exit_code,
             datetime.now(timezone.utc).isoformat()),
        )

    def get_step_outputs(self, job_id: str) -> list[dict]:
        rows = self.query(
            "SELECT step_name, host, output, exit_code, created_at "
            "FROM workflow_step_outputs WHERE job_id=? ORDER BY id ASC",
            (job_id,),
        )
        return [dict(r) for r in rows]

    def get_step_output(
        self,
        job_id: str,
        step_name: str,
        host: Optional[str] = None,
    ) -> Optional[dict]:
        if host is None:
            row = self.query_one(
                "SELECT step_name, host, output, exit_code, created_at "
                "FROM workflow_step_outputs WHERE job_id=? AND step_name=? "
                "AND host IS NULL",
                (job_id, step_name),
            )
        else:
            row = self.query_one(
                "SELECT step_name, host, output, exit_code, created_at "
                "FROM workflow_step_outputs WHERE job_id=? AND step_name=? "
                "AND host=?",
                (job_id, step_name, host),
            )
        return dict(row) if row else None

    # ── Job read operations ───────────────────────────────────────────────────

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        )

    def list_jobs(
        self,
        status_filter: Optional[str] = None,
        mode_filter:   Optional[str] = None,
        limit:  int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Return jobs newest-first with optional filters."""
        conditions, params = [], []
        if status_filter:
            conditions.append("status=?")
            params.append(status_filter)
        if mode_filter:
            conditions.append("mode=?")
            params.append(mode_filter)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params += [limit, offset]
        return self.query(
            f"SELECT * FROM jobs {where} ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
            tuple(params),
        )

    def count_jobs(
        self,
        status_filter: Optional[str] = None,
        mode_filter:   Optional[str] = None,
    ) -> int:
        """Total job count matching filters (for pagination)."""
        conditions, params = [], []
        if status_filter:
            conditions.append("status=?")
            params.append(status_filter)
        if mode_filter:
            conditions.append("mode=?")
            params.append(mode_filter)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        row = self.query_one(f"SELECT COUNT(*) as n FROM jobs {where}", tuple(params))
        return row["n"] if row else 0

    def job_exists(self, job_id: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM jobs WHERE job_id=?", (job_id,)
        )
        return row is not None

    def job_is_cancellable(self, job_id: str) -> bool:
        row = self.query_one(
            "SELECT status FROM jobs WHERE job_id=?", (job_id,)
        )
        return row is not None and row["status"] in ("queued", "running")

    # ── Device read operations ────────────────────────────────────────────────

    def get_devices(self, job_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM devices WHERE job_id=? ORDER BY rowid",
            (job_id,),
        )

    def get_commands(self, job_id: str, host: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM commands WHERE job_id=? AND host=? ORDER BY id",
            (job_id, host),
        )

    def get_device_with_commands(self, job_id: str, host: str) -> Optional[dict]:
        """Return a device row + its commands as a dict. None if not found."""
        device = self.query_one(
            "SELECT * FROM devices WHERE job_id=? AND host=?", (job_id, host)
        )
        if not device:
            return None
        cmds = self.get_commands(job_id, host)
        return {
            **dict(device),
            "commands": [dict(c) for c in cmds],
        }

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_job_summary_counts(self, job_id: str) -> dict:
        """Return success/failed/total counts for a job."""
        row = self.query_one(
            """SELECT
                 device_count AS total,
                 (SELECT COUNT(*) FROM devices WHERE job_id=? AND status='success') AS success,
                 (SELECT COUNT(*) FROM devices WHERE job_id=? AND status='failed')  AS failed
               FROM jobs WHERE job_id=?
            """,
            (job_id, job_id, job_id),
        )
        if not row:
            return {"total": 0, "success": 0, "failed": 0, "in_progress": 0}
        total   = row["total"] or 0
        success = row["success"] or 0
        failed  = row["failed"] or 0
        return {
            "total":       total,
            "success":     success,
            "failed":      failed,
            "in_progress": max(0, total - success - failed),
        }


# ── Module singleton ──────────────────────────────────────────────────────────

def _db_path() -> Path:
    """
    Database lives at /opt/netorch/netorch.db (the netorch install root).
    Falls back to the directory containing the config file if the install
    root cannot be determined (useful for tests with NETORCH_CONFIG set).
    """
    import os
    cfg_env = os.environ.get("NETORCH_CONFIG", "")
    if cfg_env:
        return Path(cfg_env).parent / "netorch.db"
    return Path("/opt/netorch/netorch.db")


db = Database(_db_path())
