"""
test_inventory.py — Inventory endpoint tests, including group host listing.
"""
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def test_list_hosts(client, auth_headers):
    r = client.get("/inventory/hosts", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] > 0
    host_ips = [h["host"] for h in data["hosts"]]
    assert "10.0.0.1" in host_ips
    assert "10.1.0.1" in host_ips


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


def test_inventory_sources_endpoint(client, auth_headers):
    r = client.get("/inventory/sources", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()

    assert "sources" in data
    assert "total_files" in data

    filenames = [s["file"] for s in data["sources"]]
    assert "mock_network.ini" in filenames
    assert "mock_linux.ini" in filenames

    net = next(s for s in data["sources"] if s["file"] == "mock_network.ini")
    assert net["hosts"] >= 5    # 5 hosts across the 5 network groups
    assert net["groups"] >= 5   # mock_switches, mock_timeouts, mock_authfail, mock_cmdfail, mock_xferfail

    lin = next(s for s in data["sources"] if s["file"] == "mock_linux.ini")
    assert lin["hosts"] >= 1
    assert lin["groups"] >= 1

    assert data["total_files"] > 0


def test_duplicate_host_across_files(tmp_path, caplog):
    inv_dir = tmp_path / "inv_dup"
    inv_dir.mkdir()

    (inv_dir / "aaa_first.ini").write_text("""\
[all:vars]
port=22

[group_a]
192.168.1.1  platform=mock  username=first_user  password=p1
""")

    (inv_dir / "bbb_second.ini").write_text("""\
[all:vars]
port=22

[group_b]
192.168.1.1  platform=mock  username=second_user  password=p2
""")

    from secrets.inventory import InventoryClient
    import core.config

    fresh = InventoryClient()
    with patch.object(core.config.inventory, "path", inv_dir):
        with caplog.at_level(logging.WARNING, logger="secrets.inventory"):
            inv = fresh._load()

    # Second file (bbb_second.ini) wins
    assert inv.by_host["192.168.1.1"].username == "second_user"
    assert any("192.168.1.1" in r.getMessage() for r in caplog.records)


def test_empty_inventory_directory(tmp_path):
    empty_dir = tmp_path / "empty_inv"
    empty_dir.mkdir()

    from secrets.inventory import InventoryClient
    import core.config

    fresh = InventoryClient()
    with patch.object(core.config.inventory, "path", empty_dir):
        with pytest.raises(FileNotFoundError, match="no \\*.ini files"):
            fresh._load()
