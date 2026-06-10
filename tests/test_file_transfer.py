"""
test_file_transfer.py — Integration tests for file transfer support.

All tests use the mock driver — no real SSH or file I/O on remote devices.
The mock driver's transfer_file() succeeds silently for platform="mock"
and raises RuntimeError for platform="mock_xferfail".
"""
import pytest
from pathlib import Path
from tests.conftest import wait_for_completion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _submit(client, headers, payload):
    r = client.post("/jobs", headers=headers, json=payload)
    return r


def _detail(client, headers, job_id):
    return client.get(f"/jobs/{job_id}/detail", headers=headers).json()


def _transfer_results(device_result):
    """Parse transfer blocks from the flat output list.

    The detail endpoint returns device["output"] as a list of strings where
    each command header is prefixed with "# ".  A transfer block looks like:
        ["# [transfer: firmware.bin]", "<output line>", ...]
    Returns list of dicts: {command, output, error}.
    """
    lines = device_result.get("output", [])
    results = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("# [transfer:"):
            cmd = line[2:]          # strip leading "# "
            body, error = [], None
            j = i + 1
            while j < len(lines) and not lines[j].startswith("# "):
                if lines[j].startswith("ERROR:"):
                    error = lines[j][6:].strip()
                else:
                    body.append(lines[j])
                j += 1
            results.append({"command": cmd, "output": "\n".join(body), "error": error})
            i = j
        else:
            i += 1
    return results


# ---------------------------------------------------------------------------
# Test 1 — single file transfer succeeds
# ---------------------------------------------------------------------------

def test_single_file_transfer_succeeds(client, auth_headers, tmp_path):
    local_file = tmp_path / "firmware.bin"
    local_file.write_bytes(b"fake firmware content")

    payload = {
        "mode": "run",
        "devices": [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "file_transfers": [
            {"local_path": str(local_file), "remote_path": "/flash/firmware.bin"},
        ],
    }
    r = _submit(client, auth_headers, payload)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    final = wait_for_completion(client, job_id, auth_headers)
    assert final["status"] in ("completed", "partial_failure")

    detail = _detail(client, auth_headers, job_id)
    device = detail["devices"][0]
    transfers = _transfer_results(device)

    assert len(transfers) == 1
    assert transfers[0]["error"] is None
    assert "firmware.bin" in transfers[0]["output"]


# ---------------------------------------------------------------------------
# Test 2 — multiple file transfers all succeed
# ---------------------------------------------------------------------------

def test_multiple_file_transfers_succeed(client, auth_headers, tmp_path):
    file1 = tmp_path / "config.tar.gz"
    file2 = tmp_path / "patch.bin"
    file1.write_bytes(b"config archive")
    file2.write_bytes(b"patch data")

    payload = {
        "mode": "run",
        "devices": [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "file_transfers": [
            {"local_path": str(file1), "remote_path": "/tmp/config.tar.gz"},
            {"local_path": str(file2), "remote_path": "/tmp/patch.bin"},
        ],
    }
    r = _submit(client, auth_headers, payload)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    final = wait_for_completion(client, job_id, auth_headers)
    assert final["status"] in ("completed", "partial_failure")

    detail = _detail(client, auth_headers, job_id)
    device = detail["devices"][0]
    transfers = _transfer_results(device)

    assert len(transfers) == 2
    filenames = {t["command"] for t in transfers}
    assert "[transfer: config.tar.gz]" in filenames
    assert "[transfer: patch.bin]"     in filenames
    for t in transfers:
        assert t["error"] is None


# ---------------------------------------------------------------------------
# Test 3 — missing local_path returns 400 before job is queued
# ---------------------------------------------------------------------------

def test_missing_local_path_returns_400(client, auth_headers):
    payload = {
        "mode": "run",
        "devices": [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "file_transfers": [
            {"local_path": "/nonexistent/path/firmware.bin",
             "remote_path": "/flash/firmware.bin"},
        ],
    }
    r = _submit(client, auth_headers, payload)
    assert r.status_code == 400
    detail_msg = r.json()["detail"].lower()
    assert "local_path" in detail_msg or "nonexistent" in detail_msg


# ---------------------------------------------------------------------------
# Test 4 — transfer failure is captured per-device; job and other devices
#           continue (consistent with continue-on-failure policy)
# ---------------------------------------------------------------------------

def test_transfer_failure_captured_per_device_job_continues(
    client, auth_headers, tmp_path
):
    local_file = tmp_path / "update.bin"
    local_file.write_bytes(b"data")

    payload = {
        "mode": "run",
        # Two devices: one succeeds, one fails on transfer
        "devices": [
            {"host": "10.0.0.1", "group": "mock_switches"},   # mock — transfer OK
            {"host": "10.4.0.1", "group": "mock_xferfail"},   # mock_xferfail — transfer raises
        ],
        "commands": ["show version"],
        "file_transfers": [
            {"local_path": str(local_file), "remote_path": "/flash/update.bin"},
        ],
        "options": {"timeout_per_device": 5, "max_workers": 5},
    }
    r = _submit(client, auth_headers, payload)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    final = wait_for_completion(client, job_id, auth_headers)
    # Job must not be outright "failed" — devices continue despite transfer error
    assert final["status"] in ("completed", "partial_failure")

    detail  = _detail(client, auth_headers, job_id)
    devices = {d["host"]: d for d in detail["devices"]}

    # Successful device: transfer recorded without error
    ok_transfers = _transfer_results(devices["10.0.0.1"])
    assert len(ok_transfers) == 1
    assert ok_transfers[0]["error"] is None

    # Failing device: transfer error captured, but device still ran commands
    fail_transfers = _transfer_results(devices["10.4.0.1"])
    assert len(fail_transfers) == 1
    assert fail_transfers[0]["error"] is not None
    assert "transfer" in fail_transfers[0]["error"].lower() or "mock" in fail_transfers[0]["error"].lower()

    # Commands still ran on the failing device (continue-on-failure)
    non_transfer_output = [l for l in devices["10.4.0.1"]["output"]
                           if l.startswith("# ") and not l.startswith("# [transfer:")]
    assert any("show version" in l for l in non_transfer_output)


# ---------------------------------------------------------------------------
# Test 5 — post-transfer commands are run and their output is captured
# ---------------------------------------------------------------------------

def test_post_transfer_commands_run(client, auth_headers, tmp_path):
    local_file = tmp_path / "script.sh"
    local_file.write_bytes(b"#!/bin/bash\necho hello")

    payload = {
        "mode": "run",
        "devices": [{"host": "10.0.0.1", "group": "mock_switches"}],
        "commands": ["show version"],
        "file_transfers": [
            {
                "local_path":             str(local_file),
                "remote_path":            "/tmp/script.sh",
                "post_transfer_commands": ["chmod +x /tmp/script.sh", "/tmp/script.sh"],
            }
        ],
    }
    r = _submit(client, auth_headers, payload)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    final = wait_for_completion(client, job_id, auth_headers)
    assert final["status"] in ("completed", "partial_failure")

    detail = _detail(client, auth_headers, job_id)
    device = detail["devices"][0]
    transfers = _transfer_results(device)

    assert len(transfers) == 1
    assert transfers[0]["error"] is None
    # Transfer confirmation line must name the file
    assert "script.sh" in transfers[0]["output"] or "script.sh" in transfers[0]["command"]
