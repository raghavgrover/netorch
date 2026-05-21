"""
core/db.py — PostgreSQL database layer for netorch.

Uses psycopg2 with a connection pool (ThreadedConnectionPool) so multiple
executor threads can safely run concurrent queries.

Key differences from the previous SQLite version:
- Placeholders: ? → %s
- AUTOINCREMENT  → SERIAL / BIGSERIAL
- INSERT OR REPLACE → INSERT ... ON CONFLICT DO UPDATE
- row_factory emulated via RealDictCursor
- WAL/PRAGMA removed (PostgreSQL handles concurrency natively)
- Migrations use DO $$ EXCEPTION WHEN duplicate_column THEN NULL; END $$
"""
from __future__ import annotations

import threading
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from core.config import database as db_cfg

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id       TEXT PRIMARY KEY,
        mode         TEXT NOT NULL,
        status       TEXT NOT NULL,
        started_at   TEXT,
        completed_at TEXT,
        device_count INTEGER NOT NULL DEFAULT 0,
        error        TEXT,
        incident     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_started_at ON jobs(started_at)",
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_devices_job ON devices(job_id)",
    """
    CREATE TABLE IF NOT EXISTS commands (
        id        BIGSERIAL PRIMARY KEY,
        job_id    TEXT NOT NULL,
        host      TEXT NOT NULL,
        command   TEXT NOT NULL,
        output    TEXT,
        error     TEXT,
        timestamp TEXT,
        FOREIGN KEY (job_id, host) REFERENCES devices(job_id, host)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_commands_device ON commands(job_id, host)",
    """
    CREATE TABLE IF NOT EXISTS workflow_logs (
        id         BIGSERIAL PRIMARY KEY,
        job_id     TEXT NOT NULL,
        host       TEXT NOT NULL,
        line       TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wflogs_job_host ON workflow_logs(job_id, host)",
    "CREATE INDEX IF NOT EXISTS idx_wflogs_job       ON workflow_logs(job_id)",
    """
    CREATE TABLE IF NOT EXISTS workflow_step_outputs (
        id         BIGSERIAL PRIMARY KEY,
        job_id     TEXT    NOT NULL,
        step_name  TEXT    NOT NULL,
        host       TEXT,
        output     TEXT,
        exit_code  INTEGER,
        created_at TEXT    NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs(job_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wfsteps_job ON workflow_step_outputs(job_id)",
]

# Runtime migrations — safely add columns that may not exist yet
_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS incident TEXT",
]


class _Row(dict):
    """Dict subclass that also supports attribute-style access (row['key'] or row.key)."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class Database:
    """
    Thread-safe PostgreSQL wrapper backed by a connection pool.

    All public methods acquire a connection from the pool, execute the
    query, and return the connection.  The pool size matches the executor's
    thread count so workers never block waiting for a connection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            dsn=db_cfg.dsn,
        )
        self._apply_schema()

    # ── Connection helpers ────────────────────────────────────────────────────

    def _conn(self):
        return self._pool.getconn()

    def _putconn(self, conn) -> None:
        self._pool.putconn(conn)

    def _apply_schema(self) -> None:
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    for stmt in _SCHEMA_STATEMENTS:
                        cur.execute(stmt)
                    for mig in _MIGRATIONS:
                        cur.execute(mig)
        finally:
            self._putconn(conn)

    # ── Low-level helpers ─────────────────────────────────────────────────────

    def execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a single DML statement (INSERT / UPDATE / DELETE)."""
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
        finally:
            self._putconn(conn)

    def executemany(self, sql: str, params_seq) -> None:
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.executemany(sql, list(params_seq))
        finally:
            self._putconn(conn)

    def query(self, sql: str, params: tuple = ()) -> list[_Row]:
        """Run a SELECT and return all rows as _Row dicts.
        Uses 'with conn:' to ensure the implicit transaction is properly
        committed/rolled back before the connection is returned to the pool.
        """
        conn = self._conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    return [_Row(r) for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def query_one(self, sql: str, params: tuple = ()) -> Optional[_Row]:
        """Run a SELECT and return the first row or None."""
        conn = self._conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone()
                    return _Row(row) if row else None
        finally:
            self._putconn(conn)

    def transaction(self, statements: list[tuple[str, tuple]]) -> None:
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    for sql, params in statements:
                        cur.execute(sql, params)
        finally:
            self._putconn(conn)

    # ── Job write operations ──────────────────────────────────────────────────

    def create_job(self, job_id: str, mode: str, device_count: int, incident: Optional[str] = None) -> None:
        self.execute(
            "INSERT INTO jobs (job_id, mode, status, device_count, incident) VALUES (%s,%s,%s,%s,%s)",
            (job_id, mode, "queued", device_count, incident),
        )

    def update_job_device_count(self, job_id: str, count: int) -> None:
        self.execute("UPDATE jobs SET device_count=%s WHERE job_id=%s", (count, job_id))

    def mark_job_running(self, job_id: str, started_at: str) -> None:
        self.execute("UPDATE jobs SET status='running', started_at=%s WHERE job_id=%s", (started_at, job_id))

    def mark_job_complete(self, job_id: str, status: str, completed_at: str) -> None:
        self.execute("UPDATE jobs SET status=%s, completed_at=%s WHERE job_id=%s", (status, completed_at, job_id))

    def mark_job_failed(self, job_id: str, completed_at: str, error: str) -> None:
        self.execute("UPDATE jobs SET status='failed', completed_at=%s, error=%s WHERE job_id=%s", (completed_at, error, job_id))

    def mark_job_cancelled(self, job_id: str, completed_at: str) -> None:
        self.execute("UPDATE jobs SET status='cancelled', completed_at=%s WHERE job_id=%s", (completed_at, job_id))

    # ── Device write operations ───────────────────────────────────────────────

    def upsert_device(self, job_id, host, platform, status, duration_seconds, error, config_backup) -> None:
        self.execute(
            """INSERT INTO devices (job_id, host, platform, status, duration_seconds, error, config_backup)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (job_id, host) DO UPDATE SET
                   platform=EXCLUDED.platform,
                   status=EXCLUDED.status,
                   duration_seconds=EXCLUDED.duration_seconds,
                   error=EXCLUDED.error,
                   config_backup=EXCLUDED.config_backup
            """,
            (job_id, host, platform, status, duration_seconds, error, config_backup),
        )

    def insert_commands(self, job_id: str, host: str, commands: list[dict]) -> None:
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM commands WHERE job_id=%s AND host=%s", (job_id, host))
                    if commands:
                        cur.executemany(
                            "INSERT INTO commands (job_id, host, command, output, error, timestamp) VALUES (%s,%s,%s,%s,%s,%s)",
                            [(job_id, host, c.get("command",""), c.get("output",""), c.get("error"), c.get("timestamp")) for c in commands],
                        )
        finally:
            self._putconn(conn)

    # ── Workflow log operations ───────────────────────────────────────────────

    def append_workflow_log(self, job_id: str, host: str, line: str) -> None:
        from datetime import datetime, timezone
        self.execute(
            "INSERT INTO workflow_logs (job_id, host, line, created_at) VALUES (%s,%s,%s,%s)",
            (job_id, host, line, datetime.now(timezone.utc).isoformat()),
        )

    def get_workflow_log(self, job_id: str, host: str, since_id: int = 0) -> list[_Row]:
        return self.query(
            "SELECT id, line, created_at FROM workflow_logs WHERE job_id=%s AND host=%s AND id>%s ORDER BY id ASC",
            (job_id, host, since_id),
        )

    def get_workflow_log_all(self, job_id: str, since_id: int = 0) -> list[_Row]:
        return self.query(
            "SELECT id, host, line, created_at FROM workflow_logs WHERE job_id=%s AND id>%s ORDER BY id ASC",
            (job_id, since_id),
        )

    def get_workflow_log_hosts(self, job_id: str) -> list[str]:
        rows = self.query(
            "SELECT DISTINCT host FROM workflow_logs WHERE job_id=%s ORDER BY host", (job_id,)
        )
        return [r["host"] for r in rows]

    # ── Workflow step outputs ─────────────────────────────────────────────────

    def insert_step_output(self, job_id, step_name, host, output, exit_code) -> None:
        from datetime import datetime, timezone
        self.execute(
            "INSERT INTO workflow_step_outputs (job_id, step_name, host, output, exit_code, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (job_id, step_name, host, output, exit_code, datetime.now(timezone.utc).isoformat()),
        )

    def get_step_outputs(self, job_id: str) -> list[dict]:
        rows = self.query(
            "SELECT step_name, host, output, exit_code, created_at FROM workflow_step_outputs WHERE job_id=%s ORDER BY id ASC",
            (job_id,),
        )
        return [dict(r) for r in rows]

    def get_step_output(self, job_id: str, step_name: str, host: Optional[str] = None) -> Optional[dict]:
        if host is None:
            row = self.query_one(
                "SELECT step_name, host, output, exit_code, created_at FROM workflow_step_outputs WHERE job_id=%s AND step_name=%s AND host IS NULL",
                (job_id, step_name),
            )
        else:
            row = self.query_one(
                "SELECT step_name, host, output, exit_code, created_at FROM workflow_step_outputs WHERE job_id=%s AND step_name=%s AND host=%s",
                (job_id, step_name, host),
            )
        return dict(row) if row else None

    # ── Job read operations ───────────────────────────────────────────────────

    def get_job(self, job_id: str) -> Optional[_Row]:
        return self.query_one("SELECT * FROM jobs WHERE job_id=%s", (job_id,))

    def list_jobs(self, status_filter=None, mode_filter=None, limit=100, offset=0) -> list[_Row]:
        conditions, params = [], []
        if status_filter:
            conditions.append("status=%s"); params.append(status_filter)
        if mode_filter:
            conditions.append("mode=%s"); params.append(mode_filter)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params += [limit, offset]
        return self.query(
            f"SELECT * FROM jobs {where} ORDER BY started_at DESC NULLS LAST LIMIT %s OFFSET %s",
            tuple(params),
        )

    def count_jobs(self, status_filter=None, mode_filter=None) -> int:
        conditions, params = [], []
        if status_filter:
            conditions.append("status=%s"); params.append(status_filter)
        if mode_filter:
            conditions.append("mode=%s"); params.append(mode_filter)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        row = self.query_one(f"SELECT COUNT(*) AS n FROM jobs {where}", tuple(params))
        return row["n"] if row else 0

    def job_exists(self, job_id: str) -> bool:
        return self.query_one("SELECT 1 FROM jobs WHERE job_id=%s", (job_id,)) is not None

    def job_is_cancellable(self, job_id: str) -> bool:
        row = self.query_one("SELECT status FROM jobs WHERE job_id=%s", (job_id,))
        return row is not None and row["status"] in ("queued", "running")

    # ── Device read operations ────────────────────────────────────────────────

    def get_devices(self, job_id: str) -> list[_Row]:
        return self.query("SELECT * FROM devices WHERE job_id=%s", (job_id,))

    def get_commands(self, job_id: str, host: str) -> list[_Row]:
        return self.query("SELECT * FROM commands WHERE job_id=%s AND host=%s ORDER BY id", (job_id, host))

    def get_device_with_commands(self, job_id: str, host: str) -> Optional[dict]:
        device = self.query_one("SELECT * FROM devices WHERE job_id=%s AND host=%s", (job_id, host))
        if not device:
            return None
        cmds = self.get_commands(job_id, host)
        return {**dict(device), "commands": [dict(c) for c in cmds]}

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_job_summary_counts(self, job_id: str) -> dict:
        row = self.query_one(
            """SELECT
                 device_count AS total,
                 (SELECT COUNT(*) FROM devices WHERE job_id=%s AND status='success') AS success,
                 (SELECT COUNT(*) FROM devices WHERE job_id=%s AND status='failed')  AS failed
               FROM jobs WHERE job_id=%s
            """,
            (job_id, job_id, job_id),
        )
        if not row:
            return {"total": 0, "success": 0, "failed": 0, "in_progress": 0}
        total   = row["total"] or 0
        success = row["success"] or 0
        failed  = row["failed"] or 0
        return {"total": total, "success": success, "failed": failed,
                "in_progress": max(0, total - success - failed)}


# ── Module singleton ──────────────────────────────────────────────────────────

db = Database()
