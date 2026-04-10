"""
test_inventory.py — Inventory endpoint tests, including group host listing.
"""


def test_list_hosts(client, auth_headers):
    r = client.get("/inventory/hosts", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] > 0
    assert "10.0.0.1" in data["hosts"]
    assert "10.1.0.1" in data["hosts"]


def test_list_groups(client, auth_headers):
    r = client.get("/inventory/groups", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "mock_switches" in data["groups"]
    assert "mock_timeouts" in data["groups"]
    assert "mock_authfail" in data["groups"]


def test_get_group_hosts(client, auth_headers):
    r = client.get("/inventory/groups/mock_switches", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["group"] == "mock_switches"
    assert data["count"] == 3
    hosts = [h["host"] for h in data["hosts"]]
    assert "10.0.0.1" in hosts
    assert "10.0.0.2" in hosts
    assert "10.0.0.3" in hosts


def test_get_group_hosts_shows_platform(client, auth_headers):
    r = client.get("/inventory/groups/mock_switches", headers=auth_headers)
    for host_entry in r.json()["hosts"]:
        assert host_entry["platform"] == "mock"
        assert host_entry["port"] == 22


def test_get_nonexistent_group_404(client, auth_headers):
    r = client.get("/inventory/groups/does_not_exist", headers=auth_headers)
    assert r.status_code == 404


def test_reload_inventory(client, auth_headers):
    r = client.post("/inventory/reload", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "reloaded"
