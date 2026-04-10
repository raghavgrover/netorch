"""
test_jobs.py — Job submission, listing, status, and detail tests.

Tests all three DeviceEntry forms:
  1. {"host": "10.0.0.1"}                    — host only
  2. {"host": "10.0.0.1", "group": "..."}   — host + group (cred fallback)
  3. {"group": "mock_switches"}              — group only (all hosts expanded)
"""
import pytest
from tests.conftest import wait_for_completion


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_submit_requires_host_or_group(client, auth_headers):
    """Device entry with neither host nor group is rejected with 422."""
    r = client.post("/jobs", json={
        "job_id":   "bad-no-host-group",
        "mode":     "audit",
        "devices":  [{"platform": "cisco_ios"}],   # neither host nor group
        "commands": ["show version"],
    }, headers=auth_headers)
    assert r.status_code == 422


def test_submit_without_devices_rejected(client, auth_headers):
    r = client.post("/jobs", json={
        "job_id": "bad-no-devices", "mode": "audit",
        "devices": [], "commands": ["show version"],
    }, headers=auth_headers)
    assert r.status_code == 422


def test_submit_without_commands_rejected(client, auth_headers):
    r = client.post("/jobs", json={
        "job_id": "bad-no-commands", "mode": "audit",
        "devices": [{"host": "10.0.0.1"}], "commands": [],
    }, headers=auth_headers)
    # commands is now optional at the schema level; the application rejects
    # a job with neither commands nor file_transfers with 400.
    assert r.status_code == 400


def test_autogenerate_job_id(client, auth_headers):
    r = client.post("/jobs", json={
        "mode": "audit",
        "devices": [{"host": "10.0.0.1"}],
        "commands": ["show version"],
    }, headers=auth_headers)
    assert r.status_code == 202
    assert r.json()["job_id"].startswith("job-")


def test_duplicate_job_id_rejected(client, auth_headers):
    payload = {
        "job_id": "dup-job-001", "mode": "audit",
        "devices": [{"host": "10.0.0.1"}],
        "commands": ["show version"],
    }
    client.post("/jobs", json=payload, headers=auth_headers)
    r = client.post("/jobs", json=payload, headers=auth_headers)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Form 1: host only
# ---------------------------------------------------------------------------

def test_host_only_device_entry(client, auth_headers):
    """{"host": "10.0.0.1"} — direct inventory lookup, no group."""
    r = client.post("/jobs", json={
        "job_id":   "f1-host-only-001",
        "mode":     "audit",
        "devices":  [{"host": "10.0.0.1"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    assert r.status_code == 202
    assert r.json()["device_count"] == 1

    final = wait_for_completion(client, "f1-host-only-001", auth_headers)
    assert final["status"] == "completed"
    assert final["summary"]["success"] == 1


# ---------------------------------------------------------------------------
# Form 2: host + group (group = credential fallback)
# ---------------------------------------------------------------------------

def test_host_and_group_device_entry(client, auth_headers):
    """{"host": "10.0.0.1", "group": "mock_switches"} — host is SSH target,
    group provides credential fallback."""
    r = client.post("/jobs", json={
        "job_id":   "f2-host-group-001",
        "mode":     "audit",
        "devices":  [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    assert r.status_code == 202

    final = wait_for_completion(client, "f2-host-group-001", auth_headers)
    assert final["status"] == "completed"
    assert final["summary"]["success"] == 1


# ---------------------------------------------------------------------------
# Form 3: group only (all hosts in group expanded)
# ---------------------------------------------------------------------------

def test_group_only_device_entry_expands_all_hosts(client, auth_headers):
    """
    {"group": "mock_switches"} — expands to 10.0.0.1, 10.0.0.2, 10.0.0.3.
    device_count in the response must reflect the expanded count (3), not 1.
    """
    r = client.post("/jobs", json={
        "job_id":   "f3-group-only-001",
        "mode":     "audit",
        "devices":  [{"group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    assert r.status_code == 202

    final = wait_for_completion(client, "f3-group-only-001", auth_headers)
    assert final["status"] == "completed"
    # mock_switches has 3 hosts in test inventory
    assert final["summary"]["total"] == 3
    assert final["summary"]["success"] == 3
    assert final["summary"]["failed"] == 0


def test_group_only_detail_shows_all_hosts(client, auth_headers):
    """Detail endpoint shows individual results for each expanded host."""
    client.post("/jobs", json={
        "job_id":   "f3-group-detail-001",
        "mode":     "audit",
        "devices":  [{"group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    wait_for_completion(client, "f3-group-detail-001", auth_headers)

    r = client.get("/jobs/f3-group-detail-001/detail", headers=auth_headers)
    assert r.status_code == 200
    devices = r.json()["devices"]
    assert len(devices) == 3
    hosts = {d["host"] for d in devices}
    assert "10.0.0.1" in hosts
    assert "10.0.0.2" in hosts
    assert "10.0.0.3" in hosts
    for d in devices:
        assert d["status"] == "success"


def test_nonexistent_group_fails_gracefully(client, auth_headers):
    """Unknown group is rejected at submission time with 400."""
    r = client.post("/jobs", json={
        "job_id":   "f3-bad-group-001",
        "mode":     "audit",
        "devices":  [{"group": "nonexistent_group_xyz"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    assert r.status_code == 400


def test_mixed_forms_in_single_job(client, auth_headers):
    """
    Mix all three device-entry forms in one job with no duplicate hosts:
      - host only     → 10.0.0.1 (1 device)
      - host + group  → 10.0.0.2 with mock_switches credential fallback (1 device)
      - group only    → mock_xferfail = 10.4.0.1, no overlap with explicit hosts (1 device)
    Total: 3 devices
    """
    r = client.post("/jobs", json={
        "job_id":   "f-mixed-001",
        "mode":     "audit",
        "devices":  [
            {"host": "10.0.0.1"},
            {"host": "10.0.0.2", "group": "mock_switches"},
            {"group": "mock_xferfail"},
        ],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    assert r.status_code == 202

    final = wait_for_completion(client, "f-mixed-001", auth_headers)
    assert final["status"] == "completed"
    assert final["summary"]["total"] == 3
    assert final["summary"]["success"] == 3


# ---------------------------------------------------------------------------
# Completion states
# ---------------------------------------------------------------------------

def test_partial_failure_with_group(client, auth_headers):
    """Mixed group (some succeed, some timeout) → partial_failure."""
    r = client.post("/jobs", json={
        "job_id":   "pf-group-001",
        "mode":     "audit",
        "devices":  [
            {"group": "mock_switches"},   # 3 hosts, all succeed
            {"group": "mock_timeouts"},   # 1 host, times out
        ],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    assert r.status_code == 202

    final = wait_for_completion(client, "pf-group-001", auth_headers)
    assert final["status"] == "partial_failure"
    assert final["summary"]["success"] == 3
    assert final["summary"]["failed"] == 1


# ---------------------------------------------------------------------------
# Listing and pagination
# ---------------------------------------------------------------------------

def test_list_jobs_returns_list(client, auth_headers):
    r = client.get("/jobs", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["jobs"], list)
    assert "total" in data


def test_list_jobs_filter_by_mode(client, auth_headers):
    r = client.get("/jobs?mode=audit", headers=auth_headers)
    assert r.status_code == 200
    for job in r.json()["jobs"]:
        assert job["mode"] == "audit"


def test_list_jobs_filter_by_status(client, auth_headers):
    r = client.get("/jobs?status=completed", headers=auth_headers)
    assert r.status_code == 200
    for job in r.json()["jobs"]:
        assert job["status"] == "completed"


def test_list_jobs_pagination_no_overlap(client, auth_headers):
    r1 = client.get("/jobs?limit=2&offset=0", headers=auth_headers)
    r2 = client.get("/jobs?limit=2&offset=2", headers=auth_headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    ids1 = {j["job_id"] for j in r1.json()["jobs"]}
    ids2 = {j["job_id"] for j in r2.json()["jobs"]}
    assert len(ids1 & ids2) == 0


# ---------------------------------------------------------------------------
# 404 cases
# ---------------------------------------------------------------------------

def test_get_nonexistent_job(client, auth_headers):
    assert client.get("/jobs/does-not-exist-xyz", headers=auth_headers).status_code == 404


def test_get_nonexistent_job_detail(client, auth_headers):
    assert client.get("/jobs/does-not-exist-xyz/detail", headers=auth_headers).status_code == 404
