#!/usr/bin/env python3
"""
One-time migration: import existing job JSON files into SQLite.
The DB is guaranteed fresh (deleted by apply.sh before this runs).
"""
import json, os, sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("NETORCH_CONFIG", "/opt/netorch/netorch.toml")

from core.config import logging_cfg

# Derive DB path the same way db.py does
cfg_env = os.environ.get("NETORCH_CONFIG", "")
db_path = Path(cfg_env).parent / "netorch.db" if cfg_env else Path("/opt/netorch/netorch.db")
log_dir = logging_cfg.log_dir

print(f"Database : {db_path}")
print(f"Log dir  : {log_dir}")
print()

# Create fresh schema directly via sqlite3 (no module import needed)
conn = sqlite3.connect(str(db_path))
conn.executescript("""
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL,
    started_at TEXT, completed_at TEXT,
    device_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    job_id TEXT NOT NULL, host TEXT NOT NULL, platform TEXT,
    status TEXT NOT NULL DEFAULT 'pending', duration_seconds REAL,
    error TEXT, config_backup TEXT, PRIMARY KEY (job_id, host)
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL, host TEXT NOT NULL,
    command TEXT NOT NULL, output TEXT, error TEXT, timestamp TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at);
CREATE INDEX IF NOT EXISTS idx_devices_job  ON devices(job_id);
CREATE INDEX IF NOT EXISTS idx_cmds_device  ON commands(job_id, host);
""")
conn.commit()

files = sorted(log_dir.glob("job-*.json"))
if not files:
    print(f"No job-*.json files found in {log_dir}")
    sys.exit(0)

print(f"Found {len(files)} job files\n")
migrated = skipped = errors = 0

for path in files:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        job_id = data.get("job_id")
        if not job_id:
            print(f"  SKIP {path.name} — no job_id"); errors += 1; continue

        exists = conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if exists:
            skipped += 1; continue

        conn.execute(
            "INSERT INTO jobs (job_id,mode,status,started_at,completed_at,device_count,error) VALUES (?,?,?,?,?,?,?)",
            (job_id, data.get("mode","audit"), data.get("status","completed"),
             data.get("started_at"), data.get("completed_at"),
             data.get("device_count",0), data.get("error"))
        )

        devices = data.get("devices", {})
        for host, dr in devices.items():
            conn.execute(
                "INSERT OR REPLACE INTO devices (job_id,host,platform,status,duration_seconds,error,config_backup) VALUES (?,?,?,?,?,?,?)",
                (job_id, host, dr.get("platform"), dr.get("status","success"),
                 dr.get("duration_seconds"), dr.get("error"), dr.get("config_backup"))
            )
            for cmd in dr.get("commands", []):
                conn.execute(
                    "INSERT INTO commands (job_id,host,command,output,error,timestamp) VALUES (?,?,?,?,?,?)",
                    (job_id, host, cmd.get("command",""), cmd.get("output",""),
                     cmd.get("error"), cmd.get("timestamp"))
                )

        conn.commit()
        migrated += 1
        print(f"  OK  {job_id}  ({len(devices)} devices)")

    except Exception as e:
        print(f"  ERR {path.name}: {e}"); errors += 1

print(f"\nDone: {migrated} migrated, {skipped} already in DB, {errors} errors")

if migrated:
    print("\nLatest 5 jobs:")
    for r in conn.execute("SELECT job_id, status, mode FROM jobs ORDER BY started_at DESC LIMIT 5"):
        print(f"  {r[0]:<20} {r[1]:<20} {r[2]}")

conn.close()
