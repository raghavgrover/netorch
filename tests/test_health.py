"""
test_health.py — System and auth endpoint tests.
"""
import pytest


def test_health_no_auth(client):
    """Health endpoint requires no auth."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_stats_no_auth(client):
    """Stats endpoint requires no auth."""
    r = client.get("/stats")
    assert r.status_code == 200
    data = r.json()
    assert "active_jobs" in data
    assert "max_queue_depth" in data


def test_auth_missing(client):
    """Requests without auth header are rejected."""
    r = client.get("/jobs")
    assert r.status_code == 422   # missing required header


def test_auth_wrong_token(client):
    """Wrong token is rejected with 401."""
    r = client.get("/jobs", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_auth_correct_token(client, auth_headers):
    """Correct token is accepted."""
    r = client.get("/jobs", headers=auth_headers)
    assert r.status_code == 200
