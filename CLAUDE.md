# netorch — Network Configuration Orchestrator

## Project overview

netorch is a lightweight, Ansible-like SSH-based network configuration
orchestrator. It runs as a FastAPI service on a RHEL 9 Linux machine and
exposes a REST API on port 64321. It is designed to be triggered by IBM
BigFix agents to run audit and remediation jobs against network devices
in parallel over SSH.

The orchestrator is the bridge between BigFix (which manages the Linux
relay machine) and network devices (Cisco switches, routers, firewalls,
Linux servers) that have no BigFix agent of their own.

---

## Runtime environment

- **OS:** RHEL 9
- **Python:** 3.11 (virtualenv at `/root/netorch-v3/venv`)
- **Install path:** `/root/netorch-v3` (development) → `/opt/netorch` (production)
- **Service:** `systemd` unit `netorch.service`
- **API port:** 64321
- **Config file:** `netorch.toml` (read via `NETORCH_CONFIG` env var or auto-discovered)
- **Credentials:** `inventory.ini` — Ansible-style INI file with device credentials
- **Logs:** `logs/jobs/<job_id>.json` — one JSON file per job

### Key commands

```bash
# Start / restart / status
sudo systemctl restart netorch
sudo systemctl status netorch
journalctl -u netorch -f

# Run tests (no real SSH needed — uses mock drivers)
cd /root/netorch-v3
PYTHONPATH=. venv/bin/pytest tests/ -v

# Clear stale bytecode (do this after any file change if service behaves oddly)
find /root/netorch-v3 -name "*.pyc" -delete
find /root/netorch-v3 -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true

# Quick API smoke test
curl -s http://localhost:64321/health
curl -s http://localhost:64321/inventory/groups \
  -H "Authorization: Bearer $(grep auth_token /root/netorch-v3/netorch.toml | cut -d'"' -f2)"
```

---

## Project structure

```
/root/netorch-v3/
├── main.py                    FastAPI app entrypoint (port 64321)
├── netorch.toml               Config: auth token, executor settings, inventory path
├── inventory.ini              Device credentials (Ansible INI format)
├── inventory.ini.example      Annotated template
├── requirements.txt           Python dependencies
├── pytest.ini                 Test config
├── CLAUDE.md                  This file
│
├── api/
│   ├── auth.py                Bearer token middleware
│   ├── middleware.py          Rate limiting (slowapi)
│   ├── schemas.py             All Pydantic models
│   └── routes/
│       ├── jobs.py            POST/GET/DELETE /jobs — job lifecycle
│       ├── logs.py            GET /logs/{id} — result retrieval
│       ├── devices.py         GET /devices/{host}/status
│       └── inventory.py       GET/POST /inventory — list hosts/groups, reload
│
├── core/
│   ├── config.py              Loads netorch.toml (respects NETORCH_CONFIG env var)
│   ├── executor.py            ThreadPoolExecutor fan-out, cancellation, queue depth
│   ├── job_store.py           In-memory job registry + JSON persistence
│   ├── ssh_worker.py          Per-device SSH execution with retry logic
│   └── logger.py              Structured JSON logging
│
├── drivers/
│   ├── __init__.py            Driver factory: get_driver(creds)
│   ├── base.py                Abstract BaseDriver interface
│   ├── ios_xe.py              Cisco IOS / IOS-XE (Netmiko)
│   ├── ios_xr.py              Cisco IOS-XR (Netmiko + commit dry-run)
│   ├── linux.py               Linux OS (Paramiko)
│   └── mock.py                Mock driver for tests (4 failure modes)
│
├── secrets/
│   └── inventory.py           INI credential resolver (get_credentials, get_group_hosts)
│
├── logs/jobs/                 Per-job JSON result files (auto-created)
│
├── scripts/
│   ├── setup.sh               RHEL 9 one-shot installer
│   ├── bigfix_trigger.sh      BigFix agent integration script (needs work — see below)
│   ├── run_tests.sh           Test runner
│   └── example_payload_*.json Example job payloads
│
└── tests/
    ├── conftest.py            Fixtures, temp inventory/config, NETORCH_CONFIG injection
    ├── test_health.py
    ├── test_inventory.py
    ├── test_jobs.py
    ├── test_logs.py
    ├── test_cancellation.py
    ├── test_retry.py
    ├── test_mock_driver.py
    ├── test_ios_xr_dryrun.py
    └── test_group_expansion.py
```

---

## API reference

All endpoints except `/health` and `/stats` require:
```
Authorization: Bearer <token>   (token is in netorch.toml [server] auth_token)
```

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Health check, no auth |
| GET | /stats | Active job count, no auth |
| POST | /jobs | Submit audit or remediation job |
| GET | /jobs | List jobs (filter by status, mode) |
| GET | /jobs/{id} | Poll job status |
| GET | /jobs/{id}/detail | Full per-device results |
| DELETE | /jobs/{id} | Cancel a running job |
| GET | /logs/{id} | Structured JSON log |
| GET | /logs/{id}/raw | Download raw log file |
| GET | /logs/{id}/device/{host} | Single device result |
| GET | /devices/{host}/status | Last known state for a device |
| GET | /inventory/hosts | List all hosts |
| GET | /inventory/groups | List all groups |
| POST | /inventory/reload | Reload inventory.ini without restart |

### Job payload — device entry forms

```json
{"host": "10.0.0.1"}                              // host only
{"host": "10.0.0.1", "group": "core_switches"}   // host + group fallback
{"group": "core_switches"}                         // group only → expands to all hosts
```

### Example audit job

```bash
curl -s -X POST http://localhost:64321/jobs \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "audit",
    "devices": [{"group": "linux_servers"}],
    "commands": ["hostname", "uptime"],
    "options": {"timeout_per_device": 30, "max_workers": 20}
  }'
```

---

## Inventory file format

```ini
[all:vars]
port=22

[core_switches]
192.168.1.10  platform=cisco_ios  username=netaudit  password=MyPass  enable_secret=MyEnable
192.168.1.11  platform=cisco_ios  username=netaudit  password=MyPass  enable_secret=MyEnable

[core_routers]
10.0.0.1  platform=cisco_xr  username=rtruser  password=MyPass

[linux_servers]
10.0.0.50  platform=linux  username=sysadmin  password=MyPass
```

Supported platforms: `cisco_ios`, `cisco_xe`, `cisco_xr`, `linux`

---

## Key design decisions

- **Audit-only by default** — `mode` defaults to `"audit"`. Remediation requires
  explicitly setting `"mode": "remediate"`.
- **Continue on failure** — if a device fails, the rest continue. Partial failures
  produce `partial_failure` status, not `failed`.
- **Group expansion at submission time** — group-only device entries are expanded
  to individual hosts in `api/routes/jobs.py:_expand_devices()` before the executor
  sees them.
- **Credentials from inventory.ini** — OpenBao is planned for a later phase.
  Currently all credentials live in `inventory.ini`. Keep this file chmod 640.
- **Config injection for tests** — set `NETORCH_CONFIG=/path/to/test.toml` before
  importing any netorch module. `tests/conftest.py` does this automatically.
- **No stale bytecode** — after editing any `.py` file, clear `__pycache__` and
  restart the service. The RHEL environment has caused stale `.pyc` issues before.

---

## Known working state (as of Phase 2)

- Parallel SSH execution across all device types
- Group-only device entries expand correctly at job submission
- Retry logic with backoff for transient SSH failures
- Auth failures detected and not retried
- IOS-XR dry-run validation before commit
- Job cancellation via DELETE /jobs/{id}
- Rate limiting (slowapi)
- Structured JSON logging to journald
- Full test suite (53 tests, all passing with mock drivers)

---

## Phase 3 — File transfer (complete)

- `FileTransferEntry` model added to `api/schemas.py`
- `file_transfers: Optional[list[FileTransferEntry]]` added to `JobSubmitRequest`
- `drivers/base.py` has abstract `transfer_file(local_path, remote_path)`
- `drivers/linux.py`: paramiko `SFTPClient.put()` with size/duration logging
- `drivers/ios_xe.py`: Netmiko `file_transfer()` with `flash:/` default
- `drivers/ios_xr.py`: Netmiko `file_transfer()` with `disk0:` default
- `drivers/mock.py`: no-op for `mock`, raises `RuntimeError` for `mock_xferfail`
- `core/ssh_worker.py`: transfer step runs after config backup, before
  commands; errors recorded per-transfer, execution continues
- `api/routes/jobs.py`: validates all `local_path` values exist on relay
  before queuing; returns 400 if any missing
- `scripts/bigfix_trigger.sh`: `--file-transfers` arg added; pipe-delimited
  format: `"local:remote:cmd1,cmd2|local2:remote2:"`
- `commands` field is now optional in `JobSubmitRequest` — jobs with only
  `file_transfers` and no commands are valid
- Validation moved to application layer: 400 returned if both `commands`
  and `file_transfers` are empty (was 422 from Pydantic)
- `bigfix_trigger.sh`: `--commands` is now optional; `--remediation-commands`
  only required for `mode=remediate` when no `--file-transfers` present
- Two BigFix Fixlets:
  1. `netorch_fixlet_commands.bes` — commands-only, action script type: sh
  2. `netorch_fixlet_filetransfer.bes` — file transfer + optional pre/post
     commands, action script type: BigFix Action Script, includes
     relay-side SHA1 verification before transfer
- 73 tests passing, 0 failures

---

## Next — Phase 4: Security hardening

- TLS on the API (HTTPS on port 64321)
- Per-token permissions (read-only vs read-write tokens)
- `inventory.ini` encryption at rest (passwords currently plaintext)
- Audit trail (immutable log of who submitted what job and when)
- Optional: OpenBao re-integration as drop-in credential backend

## Backlog

- Job history persistence across service restarts (currently in-memory)
- Log rotation for `/opt/netorch/logs/jobs/`
- Config diff/compliance engine
- Automated remediation from compliance failures
- Rollback support

### Exit code contract for BigFix

| netorch job status | Shell exit code | BigFix action result |
|---|---|---|
| completed | 0 | Fixed / Completed |
| partial_failure | 0 | Fixed / Completed (BigFix gets the result file separately) |
| failed | 1 | Failed |
| API unreachable | 1 | Failed |
| timeout (10 min) | 1 | Failed |

---

## Important warnings

- Always clear `__pycache__` after file changes, especially `core/` files.
  Stale `.pyc` files have caused hard-to-debug errors where old code runs
  despite correct `.py` files being in place.
- `executor.py` passes `device=` (a `DeviceEntry`) to `run_device_job()`.
  `ssh_worker.py` must accept `device:` not `creds:`. These got out of sync
  before — keep them consistent.
- `job_store._build_status()` must set `in_progress=0` for all terminal
  job statuses, even when no device results were written.
