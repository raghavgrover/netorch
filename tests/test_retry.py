"""
test_retry.py — Retry logic, failure modes, and parallel batch tests.
"""
import pytest
from tests.conftest import wait_for_completion


def _submit(client, headers, job_id, devices, commands=None, mode="audit"):
    r = client.post("/jobs", json={
        "job_id":   job_id,
        "mode":     mode,
        "devices":  devices,
        "commands": commands or ["show version"],
        "options":  {"timeout_per_device": 5, "max_workers": 5},
    }, headers=headers)
    assert r.status_code == 202
    return wait_for_completion(client, job_id, headers)


def test_timeout_device_fails_after_retries(client, auth_headers):
    final = _submit(client, auth_headers, "r-timeout-001",
                    [{"host": "10.1.0.1", "group": "mock_timeouts"}])
    assert final["summary"]["failed"] == 1
    assert final["summary"]["success"] == 0

    r = client.get("/jobs/r-timeout-001/detail", headers=auth_headers)
    device = r.json()["devices"][0]
    assert device["status"] == "failed"
    assert device["error"] is not None


def test_authfail_device_fails_immediately(client, auth_headers):
    final = _submit(client, auth_headers, "r-authfail-001",
                    [{"host": "10.2.0.1", "group": "mock_authfail"}])
    assert final["summary"]["failed"] == 1

    r = client.get("/jobs/r-authfail-001/detail", headers=auth_headers)
    device = r.json()["devices"][0]
    assert device["status"] == "failed"
    assert any(w in device["error"].lower()
               for w in ("auth", "authentication", "mock"))


def test_cmdfail_device_marked_success_with_per_cmd_errors(client, auth_headers):
    """
    mock_cmdfail connects OK but every run_command call raises RuntimeError.
    ssh_worker._attempt() catches each exception individually (one try/except
    per command in the audit loop) and stores it in CommandResult.error.
    _attempt() returns normally → device-level status = success.

    The same pattern applies to run_config_commands failures (e.g. a Linux
    driver command returning exit code != 0): ssh_worker catches the exception
    at the remediation block level and records it in CommandResult.error for
    each "[remediate] <cmd>" entry.  Device status remains success because the
    connection itself worked; per-command errors are visible in the results.
    """
    final = _submit(client, auth_headers, "r-cmdfail-001",
                    [{"host": "10.3.0.1", "group": "mock_cmdfail"}],
                    commands=["show version", "show running-config | include ntp"])
    # Connection succeeded → device status is success; errors are per-command
    r = client.get("/jobs/r-cmdfail-001/detail", headers=auth_headers)
    device = r.json()["devices"][0]
    assert device["status"] == "success"
    for cmd_result in device["commands"]:
        assert cmd_result["error"] is not None


def test_parallel_batch_all_succeed(client, auth_headers):
    final = _submit(client, auth_headers, "r-batch-001", [
        {"host": "10.0.0.1", "group": "mock_switches"},
        {"host": "10.0.0.2", "group": "mock_switches"},
        {"host": "10.0.0.3", "group": "mock_switches"},
    ], commands=["show version", "show running-config | include ntp",
                 "show ip interface brief"])

    assert final["status"] == "completed"
    assert final["summary"]["total"] == 3
    assert final["summary"]["success"] == 3
    assert final["summary"]["failed"] == 0

    r = client.get("/jobs/r-batch-001/detail", headers=auth_headers)
    for device in r.json()["devices"]:
        assert device["status"] == "success"
        assert len(device["commands"]) == 3
        for cmd in device["commands"]:
            assert cmd["error"] is None
            assert len(cmd["output"]) > 0


def test_unknown_host_fails_with_inventory_error(client, auth_headers):
    final = _submit(client, auth_headers, "r-noinv-001",
                    [{"host": "192.168.255.255"}])
    assert final["summary"]["failed"] == 1

    r = client.get("/jobs/r-noinv-001/detail", headers=auth_headers)
    device = r.json()["devices"][0]
    assert device["status"] == "failed"
    assert any(w in device["error"].lower()
               for w in ("inventory", "not found", "no inventory"))


def test_mixed_success_and_failure_partial(client, auth_headers):
    final = _submit(client, auth_headers, "r-mixed-001", [
        {"host": "10.0.0.1", "group": "mock_switches"},   # success
        {"host": "10.0.0.2", "group": "mock_switches"},   # success
        {"host": "10.1.0.1", "group": "mock_timeouts"},   # fails
    ])
    assert final["status"] == "partial_failure"
    assert final["summary"]["success"] == 2
    assert final["summary"]["failed"] == 1
