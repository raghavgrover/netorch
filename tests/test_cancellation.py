"""
test_cancellation.py — Job cancellation tests.
"""
import time
import pytest
from tests.conftest import wait_for_completion


def test_cancel_nonexistent_job(client, auth_headers):
    r = client.delete("/jobs/no-such-job-xyz", headers=auth_headers)
    assert r.status_code == 404


def test_cancel_completed_job_rejected(client, auth_headers):
    client.post("/jobs", json={
        "job_id":   "cancel-done-001",
        "mode": "run",
        "devices":  [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 2},
    }, headers=auth_headers)
    wait_for_completion(client, "cancel-done-001", auth_headers)

    r = client.delete("/jobs/cancel-done-001", headers=auth_headers)
    assert r.status_code == 409


def test_cancel_response_fields(client, auth_headers):
    client.post("/jobs", json={
        "job_id":   "cancel-resp-001",
        "mode": "run",
        "devices":  [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 2},
    }, headers=auth_headers)

    r = client.delete("/jobs/cancel-resp-001", headers=auth_headers)
    # 200 = cancel accepted, 409 = already completed (race condition) — both valid
    if r.status_code == 200:
        data = r.json()
        assert data["job_id"] == "cancel-resp-001"
        assert len(data["message"]) > 0
    else:
        assert r.status_code == 409


def test_cancelled_job_reaches_terminal_state(client, auth_headers):
    client.post("/jobs", json={
        "job_id":   "cancel-terminal-001",
        "mode": "run",
        "devices":  [
            {"host": "10.0.0.1", "group": "mock_switches"},
            {"host": "10.0.0.2", "group": "mock_switches"},
            {"host": "10.0.0.3", "group": "mock_switches"},
        ],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 1},
    }, headers=auth_headers)

    client.delete("/jobs/cancel-terminal-001", headers=auth_headers)

    final = wait_for_completion(client, "cancel-terminal-001", auth_headers)
    assert final["status"] in ("cancelled", "completed", "partial_failure")
