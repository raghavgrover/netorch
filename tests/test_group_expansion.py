"""
test_group_expansion.py — Tests for group-only, host-only, and mixed
device entry forms in job submissions.
"""
import pytest
from tests.conftest import wait_for_completion


# ---------------------------------------------------------------------------
# Group-only entries
# ---------------------------------------------------------------------------

def test_group_only_expands_all_hosts(client, auth_headers):
    """
    Submitting {"group": "mock_switches"} should target all 3 hosts
    defined in that group in the test inventory.
    """
    r = client.post("/jobs", json={
        "job_id":   "expand-group-001",
        "mode":     "audit",
        "devices":  [{"group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    assert r.status_code == 202
    # 3 hosts in mock_switches group
    assert r.json()["device_count"] == 3

    final = wait_for_completion(client, "expand-group-001", auth_headers)
    assert final["status"] == "completed"
    assert final["summary"]["success"] == 3
    assert final["summary"]["failed"] == 0


def test_group_only_detail_has_all_hosts(client, auth_headers):
    """Detail response should contain one entry per expanded host."""
    client.post("/jobs", json={
        "job_id":   "expand-group-002",
        "mode":     "audit",
        "devices":  [{"group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    wait_for_completion(client, "expand-group-002", auth_headers)

    r = client.get("/jobs/expand-group-002/detail", headers=auth_headers)
    assert r.status_code == 200
    devices = r.json()["devices"]
    assert len(devices) == 3
    hosts = {d["host"] for d in devices}
    assert "10.0.0.1" in hosts
    assert "10.0.0.2" in hosts
    assert "10.0.0.3" in hosts


def test_multiple_groups_expanded(client, auth_headers):
    """Two group-only entries from different groups are both expanded."""
    r = client.post("/jobs", json={
        "job_id":   "expand-multi-001",
        "mode":     "audit",
        "devices":  [
            {"group": "mock_switches"},   # 3 hosts
            {"group": "mock_cmdfail"},    # 1 host
        ],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 10},
    }, headers=auth_headers)
    assert r.status_code == 202
    assert r.json()["device_count"] == 4

    final = wait_for_completion(client, "expand-multi-001", auth_headers)
    assert final["summary"]["total"] == 4


# ---------------------------------------------------------------------------
# Host-only entries (existing behaviour, must still work)
# ---------------------------------------------------------------------------

def test_host_only_entry(client, auth_headers):
    r = client.post("/jobs", json={
        "job_id":   "expand-host-001",
        "mode":     "audit",
        "devices":  [{"host": "10.0.0.1"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 2},
    }, headers=auth_headers)
    assert r.status_code == 202
    assert r.json()["device_count"] == 1

    final = wait_for_completion(client, "expand-host-001", auth_headers)
    assert final["status"] == "completed"


def test_host_with_group_fallback(client, auth_headers):
    """Host + group — targets just the host, group used for creds."""
    r = client.post("/jobs", json={
        "job_id":   "expand-host-group-001",
        "mode":     "audit",
        "devices":  [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 2},
    }, headers=auth_headers)
    assert r.status_code == 202
    assert r.json()["device_count"] == 1

    final = wait_for_completion(client, "expand-host-group-001", auth_headers)
    assert final["status"] == "completed"


# ---------------------------------------------------------------------------
# Mixed entries
# ---------------------------------------------------------------------------

def test_mixed_host_and_group_entries(client, auth_headers):
    """
    Mix of explicit host and group-only entries in the same job.
    """
    r = client.post("/jobs", json={
        "job_id":   "expand-mixed-001",
        "mode":     "audit",
        "devices":  [
            {"host": "10.0.0.1"},            # 1 host explicit
            {"group": "mock_timeouts"},       # 1 host from group
        ],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=auth_headers)
    assert r.status_code == 202
    assert r.json()["device_count"] == 2

    final = wait_for_completion(client, "expand-mixed-001", auth_headers)
    assert final["summary"]["total"] == 2


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

def test_neither_host_nor_group_rejected(client, auth_headers):
    """A device entry with neither host nor group is a 422."""
    r = client.post("/jobs", json={
        "job_id":   "expand-bad-001",
        "mode":     "audit",
        "devices":  [{"platform": "cisco_ios"}],
        "commands": ["show version"],
    }, headers=auth_headers)
    assert r.status_code == 422


def test_unknown_group_rejected(client, auth_headers):
    """A group name that doesn't exist in inventory returns 400."""
    r = client.post("/jobs", json={
        "job_id":   "expand-badgroup-001",
        "mode":     "audit",
        "devices":  [{"group": "this_group_does_not_exist"}],
        "commands": ["show version"],
    }, headers=auth_headers)
    assert r.status_code == 400
    assert "this_group_does_not_exist" in r.json()["detail"]


def test_empty_devices_list_rejected(client, auth_headers):
    r = client.post("/jobs", json={
        "job_id":   "expand-empty-001",
        "mode":     "audit",
        "devices":  [],
        "commands": ["show version"],
    }, headers=auth_headers)
    assert r.status_code == 422
