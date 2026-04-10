"""
test_mock_driver.py — Unit tests for the mock driver itself.
Validates all four mock modes work as expected before being
relied on in integration tests.
"""
import pytest
from drivers.base import DeviceCredentials
from drivers.mock import MockDriver


def _creds(platform="mock"):
    return DeviceCredentials(
        host="10.0.0.1", username="x", password="y", platform=platform
    )


def test_mock_connect_and_command():
    d = MockDriver(_creds("mock"), timeout=5)
    with d:
        out = d.run_command("show version")
    assert "Cisco" in out


def test_mock_config_commands():
    d = MockDriver(_creds("mock"), timeout=5)
    with d:
        out = d.run_config_commands(["ntp server 10.0.0.1", "logging host 10.0.0.2"])
    assert "ntp server 10.0.0.1" in out
    assert "config applied" in out


def test_mock_get_running_config():
    d = MockDriver(_creds("mock"), timeout=5)
    with d:
        out = d.get_running_config()
    assert len(out) > 0


def test_mock_timeout_raises_on_connect():
    d = MockDriver(_creds("mock_timeout"), timeout=5)
    with pytest.raises(TimeoutError):
        d.connect()


def test_mock_authfail_raises_on_connect():
    d = MockDriver(_creds("mock_authfail"), timeout=5)
    with pytest.raises(ConnectionError):
        d.connect()


def test_mock_cmdfail_raises_on_command():
    d = MockDriver(_creds("mock_cmdfail"), timeout=5)
    d.connect()
    with pytest.raises(RuntimeError):
        d.run_command("show version")
    d.disconnect()


def test_mock_not_connected_raises():
    d = MockDriver(_creds("mock"), timeout=5)
    with pytest.raises(RuntimeError, match="Not connected"):
        d.run_command("show version")


def test_mock_unknown_command_returns_placeholder():
    d = MockDriver(_creds("mock"), timeout=5)
    with d:
        out = d.run_command("show some-unknown-command")
    assert "No canned response" in out


def test_mock_context_manager_disconnects():
    d = MockDriver(_creds("mock"), timeout=5)
    with d:
        assert d._connected is True
    assert d._connected is False
