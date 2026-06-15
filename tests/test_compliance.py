"""
test_compliance.py — Integration tests for the compliance / vulnerability scanning module.

Tests use the mock driver — no real SSH or PSIRT API calls.
The PSIRT client is not configured in the test config ([psirt] section is absent),
so get_advisories_by_version() raises PSIRTNotConfiguredError.
We test the full API lifecycle (submit → poll → results) and confirm that
devices with unsupported platforms are handled gracefully.
"""
import time
import pytest
from tests.conftest import wait_for_completion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _submit_scan(client, headers, devices, incident=None):
    payload = {"devices": devices}
    if incident:
        payload["incident"] = incident
    r = client.post("/compliance/scans", headers=headers, json=payload)
    return r


def _wait_scan(client, headers, scan_id, timeout=15):
    """Poll GET /compliance/scans/{scan_id} until terminal."""
    deadline = time.monotonic() + timeout
    terminal = {"completed", "partial_failure", "failed", "cancelled"}
    while time.monotonic() < deadline:
        r = client.get(f"/compliance/scans/{scan_id}", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] in terminal:
            return data
        time.sleep(0.3)
    pytest.fail(f"Scan {scan_id} did not complete within {timeout}s")


# ---------------------------------------------------------------------------
# Test 1 — Submit returns 202 and a scan_id
# ---------------------------------------------------------------------------

def test_submit_scan_returns_202(client, auth_headers):
    r = _submit_scan(client, auth_headers, [{"host": "10.0.0.1"}])
    assert r.status_code == 202, r.text
    body = r.json()
    assert "scan_id" in body
    assert body["status"] == "queued"
    assert body["device_count"] == 1


# ---------------------------------------------------------------------------
# Test 2 — Empty devices returns 400
# ---------------------------------------------------------------------------

def test_submit_scan_no_devices_returns_400(client, auth_headers):
    r = client.post("/compliance/scans", headers=auth_headers,
                    json={"devices": [{"group": "nonexistent_group_xyz"}]})
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Test 3 — Group expansion works (mock_switches = 3 hosts)
# ---------------------------------------------------------------------------

def test_submit_scan_group_expands(client, auth_headers):
    r = _submit_scan(client, auth_headers, [{"group": "mock_switches"}])
    assert r.status_code == 202, r.text
    assert r.json()["device_count"] == 3


# ---------------------------------------------------------------------------
# Test 4 — Scan list endpoint returns the scans we submitted
# ---------------------------------------------------------------------------

def test_list_scans(client, auth_headers):
    # Submit one scan so there's at least something to list
    r1 = _submit_scan(client, auth_headers, [{"host": "10.0.0.1"}], incident="INC-TEST")
    assert r1.status_code == 202
    scan_id = r1.json()["scan_id"]

    r = client.get("/compliance/scans", headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "scans" in data
    assert "total" in data
    ids = [s["scan_id"] for s in data["scans"]]
    assert scan_id in ids


# ---------------------------------------------------------------------------
# Test 5 — Scan completes (with errors because PSIRT not configured)
#           Platform 'mock' is not in PLATFORM_TO_OSTYPE, so the scanner
#           sets status=error for every device → final status = 'failed'.
# ---------------------------------------------------------------------------

def test_scan_completes(client, auth_headers):
    r = _submit_scan(client, auth_headers, [{"host": "10.0.0.1"}])
    scan_id = r.json()["scan_id"]

    final = _wait_scan(client, auth_headers, scan_id)
    # 'mock' platform is unsupported → all devices fail → scan fails
    assert final["status"] in ("completed", "partial_failure", "failed")
    assert "scan_id" in final
    assert "device_count" in final


# ---------------------------------------------------------------------------
# Test 6 — GET /compliance/scans/{id}/results returns device list
# ---------------------------------------------------------------------------

def test_scan_results_endpoint(client, auth_headers):
    r = _submit_scan(client, auth_headers, [{"host": "10.0.0.1"}])
    scan_id = r.json()["scan_id"]
    _wait_scan(client, auth_headers, scan_id)

    r = client.get(f"/compliance/scans/{scan_id}/results", headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "devices" in data
    assert "scan_id" in data
    assert data["scan_id"] == scan_id
    assert len(data["devices"]) >= 1
    dev = data["devices"][0]
    assert "host" in dev
    assert "status" in dev
    assert "findings" in dev


# ---------------------------------------------------------------------------
# Test 7 — 404 for unknown scan
# ---------------------------------------------------------------------------

def test_get_unknown_scan_returns_404(client, auth_headers):
    r = client.get("/compliance/scans/scan-does-not-exist-xyz", headers=auth_headers)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Test 8 — CSV endpoint returns text/csv content
# ---------------------------------------------------------------------------

def test_csv_download(client, auth_headers):
    r = _submit_scan(client, auth_headers, [{"host": "10.0.0.1"}])
    scan_id = r.json()["scan_id"]
    _wait_scan(client, auth_headers, scan_id)

    r = client.get(f"/compliance/scans/{scan_id}/results/csv", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers.get("content-type", "")
    # CSV always has a header row
    lines = r.text.strip().splitlines()
    assert len(lines) >= 1
    assert "Scan ID" in lines[0]


# ---------------------------------------------------------------------------
# Test 9 — /compliance/advisories returns a list structure
# ---------------------------------------------------------------------------

def test_list_advisories_empty_ok(client, auth_headers):
    r = client.get("/compliance/advisories", headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "advisories" in data
    assert "total" in data
    assert isinstance(data["advisories"], list)


# ---------------------------------------------------------------------------
# Test 10 — Auth required (wrong token → 403)
# ---------------------------------------------------------------------------

def test_compliance_requires_auth(client):
    bad = {"Authorization": "Bearer wrong-token"}
    r = client.post("/compliance/scans", json={"devices": [{"host": "10.0.0.1"}]},
                    headers=bad)
    assert r.status_code in (401, 403)


def test_compliance_list_requires_auth(client):
    bad = {"Authorization": "Bearer wrong-token"}
    r = client.get("/compliance/scans", headers=bad)
    assert r.status_code in (401, 403)
