"""
conftest.py — Shared pytest fixtures for netorch integration tests.

How config injection works
--------------------------
We set the NETORCH_CONFIG environment variable to a temp file BEFORE
any netorch module is imported. core/config.py reads that env var as
its first resolution step, so every singleton (server, executor,
inventory, logging_cfg, ratelimit) is built from the test config —
no monkey-patching required.

Import order matters: os.environ must be set before the first
'import core.*' or 'from main import app' anywhere in the test process.
pytest collects and executes conftest.py before any test file, so
setting the env var here is safe as long as no test file imports
netorch modules at module level outside of fixtures/functions.
"""
import os
import time
import tempfile
import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Write temp config files and set env var — MUST happen before any
#    netorch import below (and before pytest imports test files).
# ---------------------------------------------------------------------------

_tmp_dir  = tempfile.mkdtemp(prefix="netorch_test_")
_log_dir  = os.path.join(_tmp_dir, "logs", "jobs")
_inv_dir  = os.path.join(_tmp_dir, "inventory")
_cfg_file = os.path.join(_tmp_dir, "netorch.toml")

os.makedirs(_log_dir, exist_ok=True)
os.makedirs(_inv_dir, exist_ok=True)

# mock_network.ini — network mock devices
Path(os.path.join(_inv_dir, "mock_network.ini")).write_text("""\
# Test inventory — network mock devices

[all:vars]
port=22

[mock_switches]
10.0.0.1  platform=mock          username=testuser  password=testpass
10.0.0.2  platform=mock          username=testuser  password=testpass
10.0.0.3  platform=mock          username=testuser  password=testpass

[mock_timeouts]
10.1.0.1  platform=mock_timeout  username=testuser  password=testpass

[mock_authfail]
10.2.0.1  platform=mock_authfail username=testuser  password=testpass

[mock_cmdfail]
10.3.0.1  platform=mock_cmdfail  username=testuser  password=testpass

[mock_xferfail]
10.4.0.1  platform=mock_xferfail username=testuser  password=testpass
""")

# mock_linux.ini — Linux mock devices
Path(os.path.join(_inv_dir, "mock_linux.ini")).write_text("""\
# Test inventory — Linux mock devices

[all:vars]
port=22

[mock_linux]
10.5.0.1  platform=mock  username=testuser  password=testpass
""")

Path(_cfg_file).write_text(f"""\
[server]
host = "127.0.0.1"
port = 64321
auth_token = "test-token-abc123"

[executor]
max_workers = 10
default_timeout = 5
max_queue_depth = 5
retry_attempts = 2
retry_delay = 0

[inventory]
path = "{_inv_dir}"

[logging]
log_dir = "{_log_dir}"

[ratelimit]
requests_per_minute = 1000
job_submissions_per_minute = 100

[database]
db_path = "{_tmp_dir}/netorch_test.db"

[vault]
type = "none"
""")

# This must be set before ANY netorch module is imported
os.environ["NETORCH_CONFIG"] = _cfg_file


# ---------------------------------------------------------------------------
# 2. Now it's safe to import netorch modules — they will read the test config
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402
from main import app                        # noqa: E402
from secrets.inventory import inventory_client  # noqa: E402
from secrets.provider import reload_provider  # noqa: E402

# Force inventory to re-read from the test path
# (in case a previous test session cached a different path)
inventory_client.reload()
reload_provider()

# ---------------------------------------------------------------------------
# 3. Shared constants
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-token-abc123"
AUTH       = {"Authorization": f"Bearer {TEST_TOKEN}"}


# ---------------------------------------------------------------------------
# 4. Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def client():
    """Single TestClient shared across the entire test session."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def auth_headers():
    return dict(AUTH)


@pytest.fixture
def audit_payload():
    return {
        "job_id":   "test-audit-001",
        "mode":     "audit",
        "devices":  [
            {"host": "10.0.0.1", "group": "mock_switches"},
            {"host": "10.0.0.2", "group": "mock_switches"},
        ],
        "commands": ["show version", "show running-config | include ntp"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }


@pytest.fixture
def remediate_payload():
    return {
        "job_id":                "test-rem-001",
        "mode":                  "remediate",
        "devices":               [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands":              ["show running-config | include ntp"],
        "remediation_commands":  ["ntp server 10.0.0.1", "ntp server 10.0.0.2"],
        "options":               {
            "timeout_per_device":          5,
            "max_workers":                 5,
            "backup_config_before_change": True,
        },
    }


# ---------------------------------------------------------------------------
# 5. Helper available to test files via import
# ---------------------------------------------------------------------------

def wait_for_completion(client, job_id: str, headers: dict, timeout: int = 15) -> dict:
    """
    Poll GET /jobs/{job_id} until the job reaches a terminal state.
    Returns the final status response dict.
    Fails the test if the job doesn't complete within `timeout` seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200, f"Unexpected status {r.status_code}: {r.text}"
        data = r.json()
        if data["status"] not in ("queued", "running"):
            return data
        time.sleep(0.2)
    pytest.fail(
        f"Job '{job_id}' did not reach a terminal state within {timeout}s. "
        f"Last status: {r.json().get('status')}"
    )
