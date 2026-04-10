# netorch — Network Configuration Orchestrator v1.1.0

Lightweight SSH-based audit and remediation orchestrator for Cisco IOS/IOS-XE,
IOS-XR, and Linux devices. Credentials stored in a local Ansible-style INI
inventory file. REST API on port 64321. Designed to run on RHEL 9.

---

## What Changed in v1.1.0
- **Removed OpenBao** — credentials now stored in `/opt/netorch/inventory.ini`
- **Added** `POST /inventory/reload` — reload credentials without restart
- **Added** `GET /inventory/hosts` and `GET /inventory/groups`
- **RHEL 9** installer (`setup.sh`) — uses `dnf`, handles SELinux + firewalld

---

## File Structure

```
/opt/netorch/
├── main.py                         FastAPI entrypoint
├── netorch.toml                    Config (token, port, inventory path)
├── inventory.ini                   Device credentials (Ansible INI format)
├── inventory.ini.example           Annotated template
├── requirements.txt
├── api/
│   ├── auth.py                     Bearer token middleware
│   ├── schemas.py                  Pydantic models
│   └── routes/
│       ├── jobs.py                 POST /jobs, GET /jobs/{id}
│       ├── logs.py                 GET /logs/{id}, /logs/{id}/raw
│       ├── devices.py              GET /devices/{host}/status
│       └── inventory.py            GET /inventory/hosts|groups, POST /inventory/reload
├── core/
│   ├── config.py
│   ├── executor.py
│   ├── job_store.py
│   └── ssh_worker.py
├── drivers/
│   ├── __init__.py                 Driver factory
│   ├── base.py
│   ├── ios_xe.py                   Cisco IOS / IOS-XE
│   ├── ios_xr.py                   Cisco IOS-XR
│   └── linux.py                    Linux OS
├── secrets/
│   └── inventory.py                INI credential resolver
├── logs/jobs/                      Per-job JSON result files
└── scripts/
    ├── setup.sh                    RHEL 9 installer
    ├── bigfix_trigger.sh           BigFix agent script
    ├── example_payload_audit.json
    └── example_payload_remediate.json
```

---

## Quick Start (RHEL 9)

### 1. Install
```bash
sudo bash scripts/setup.sh
```

### 2. Set auth token
```bash
sudo nano /opt/netorch/netorch.toml
# Set: auth_token = "<output of: openssl rand -hex 32>"
```

### 3. Add device credentials
```bash
sudo nano /opt/netorch/inventory.ini
# Replace CHANGE_ME values with real credentials
```

### 4. Start
```bash
sudo systemctl start netorch
sudo systemctl status netorch
curl http://localhost:64321/health
```

---

## Inventory File Format

```ini
[all:vars]
port=22

[core_switches]
192.168.1.10  platform=cisco_ios  username=netaudit  password=MyPass  enable_secret=MyEnable

[core_routers]
10.0.0.1  platform=cisco_xr  username=rtruser  password=MyPass

[linux_servers]
10.0.0.50  platform=linux  username=sysadmin  password=MyPass
```

**Lookup order:** host IP/hostname → group name → error.

---

## API Reference

All endpoints (except `/health`) require `Authorization: Bearer <token>`.

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Health check |
| POST | /jobs | Submit audit or remediation job |
| GET | /jobs/{id} | Poll job status |
| GET | /jobs/{id}/detail | Full per-device results |
| GET | /logs/{id} | Structured JSON log |
| GET | /logs/{id}/raw | Download raw log (for BigFix) |
| GET | /logs/{id}/device/{host} | Single device result |
| GET | /devices/{host}/status | Last known state for a device |
| GET | /inventory/hosts | List all inventory hosts |
| GET | /inventory/groups | List all inventory groups |
| POST | /inventory/reload | Reload inventory.ini without restart |

Interactive docs: `http://<host>:64321/docs`

---

## Logs
```bash
journalctl -u netorch -f
ls /opt/netorch/logs/jobs/
cat /opt/netorch/logs/jobs/<job_id>.json | python3 -m json.tool
```
