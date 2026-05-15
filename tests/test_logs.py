"""
test_logs.py — Log retrieval and device status endpoint tests.
"""
import orjson
import pytest
from tests.conftest import wait_for_completion


def _run_job(client, headers, job_id):
    r = client.post("/jobs", json={
        "job_id":   job_id,
        "mode": "run",
        "devices":  [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 2},
    }, headers=headers)
    assert r.status_code == 202
    wait_for_completion(client, job_id, headers)


def test_get_log_json(client, auth_headers):
    _run_job(client, auth_headers, "log-001")
    r = client.get("/logs/log-001", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == "log-001"
    assert "devices" in data


def test_get_log_raw_is_valid_json(client, auth_headers):
    _run_job(client, auth_headers, "log-002")
    r = client.get("/logs/log-002/raw", headers=auth_headers)
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]
    data = orjson.loads(r.content)
    assert data["job_id"] == "log-002"


def test_get_device_log(client, auth_headers):
    _run_job(client, auth_headers, "log-003")
    r = client.get("/logs/log-003/device/10.0.0.1", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["host"] == "10.0.0.1"
    assert data["status"] == "success"


def test_log_not_found(client, auth_headers):
    r = client.get("/logs/nonexistent-job-xyz", headers=auth_headers)
    assert r.status_code == 404


def test_device_log_wrong_host_not_found(client, auth_headers):
    _run_job(client, auth_headers, "log-004")
    r = client.get("/logs/log-004/device/99.99.99.99", headers=auth_headers)
    assert r.status_code == 404


def test_device_status_endpoint(client, auth_headers):
    _run_job(client, auth_headers, "log-005")
    r = client.get("/devices/10.0.0.1/status", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    assert data["device"]["host"] == "10.0.0.1"


def test_device_status_unknown_host(client, auth_headers):
    r = client.get("/devices/99.99.99.1/status", headers=auth_headers)
    assert r.status_code == 404
