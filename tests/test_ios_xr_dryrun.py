"""
test_ios_xr_dryrun.py — Unit tests for IOS-XR dry-run validation logic.

Uses an extended mock that simulates IOS-XR dry-run error responses
without requiring a real device.
"""
import pytest
from unittest.mock import MagicMock, patch
from drivers.ios_xr import IosXrDriver
from drivers.base import DeviceCredentials


def _xr_creds():
    return DeviceCredentials(
        host="10.0.0.1", username="x", password="y", platform="cisco_xr"
    )


def _mock_conn(dry_run_output: str, commit_output: str = "Commit complete."):
    """Build a mock Netmiko connection with controlled dry-run output."""
    conn = MagicMock()
    conn.config_mode = MagicMock()
    conn.exit_config_mode = MagicMock()
    conn.send_command_timing = MagicMock(side_effect=[
        # First call: each config command (we'll send 2)
        "",
        "",
        # dry-run
        dry_run_output,
        # commit
        commit_output,
    ])
    return conn


def test_dryrun_passes_and_commits():
    driver = IosXrDriver(_xr_creds(), timeout=5)
    driver._conn = _mock_conn(dry_run_output="!! Dry run result follows\ninterface Gi0/0/0/0\n no shutdown")

    result = driver.run_config_commands(["interface Gi0/0/0/0", "no shutdown"])
    assert "commit dry-run" in result
    assert "commit" in result


def test_dryrun_error_aborts_and_raises():
    driver = IosXrDriver(_xr_creds(), timeout=5)
    conn = MagicMock()
    conn.config_mode = MagicMock()
    conn.exit_config_mode = MagicMock()
    conn.send_command_timing = MagicMock(side_effect=[
        "",                                      # config cmd 1
        "!! error: invalid input detected",      # dry-run with error
        "",                                      # abort call
    ])
    driver._conn = conn

    with pytest.raises(RuntimeError, match="dry-run validation failed"):
        driver.run_config_commands(["interface bad command"])

    # abort must have been called
    abort_calls = [
        call for call in conn.send_command_timing.call_args_list
        if "abort" in str(call)
    ]
    assert len(abort_calls) >= 1


def test_commit_failure_aborts_and_raises():
    driver = IosXrDriver(_xr_creds(), timeout=5)
    conn = MagicMock()
    conn.config_mode = MagicMock()
    conn.exit_config_mode = MagicMock()
    conn.send_command_timing = MagicMock(side_effect=[
        "",                            # config cmd
        "Dry run passed",              # dry-run passes (no error phrases)
        "% commit failed: conflict",   # commit fails
        "",                            # abort
    ])
    driver._conn = conn

    with pytest.raises(RuntimeError, match="commit failed"):
        driver.run_config_commands(["ntp server 10.0.0.1"])


def test_not_connected_raises():
    driver = IosXrDriver(_xr_creds(), timeout=5)
    with pytest.raises(RuntimeError, match="Not connected"):
        driver.run_config_commands(["ntp server 10.0.0.1"])

    with pytest.raises(RuntimeError, match="Not connected"):
        driver.run_command("show version")
