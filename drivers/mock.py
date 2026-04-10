"""
drivers/mock.py — Mock driver for testing without real network devices.

Simulates connect/disconnect/command execution. Controlled via the
NETORCH_MOCK_* environment variables or direct instantiation in tests.

Behaviour modes (set via platform field in inventory or DeviceCredentials):
  mock           — always succeeds, returns canned output
  mock_timeout   — raises TimeoutError on connect (tests retry logic)
  mock_authfail  — raises ConnectionError on connect (tests auth failure path)
  mock_cmdfail   — connects OK but run_command raises (tests command failure)
  mock_xferfail  — connects OK, run_command succeeds, transfer_file raises

Usage in tests:
    from drivers.mock import MockDriver
    from drivers.base import DeviceCredentials

    creds = DeviceCredentials(host="10.0.0.1", username="x",
                              password="y", platform="mock")
    driver = MockDriver(creds, timeout=5)
    with driver:
        out = driver.run_command("show version")
"""
from __future__ import annotations
import time
from drivers.base import BaseDriver, DeviceCredentials


_SHOW_VERSION = """
Cisco IOS XE Software, Version 17.09.01a
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2023 by Cisco Systems, Inc.

ROM: IOS-XE ROMMON

mock-device uptime is 42 days, 3 hours, 17 minutes
""".strip()

_SHOW_RUN_NTP = "ntp server 10.0.0.1\nntp server 10.0.0.2"

_CANNED_RESPONSES: dict[str, str] = {
    "show version":                        _SHOW_VERSION,
    "show running-config | include ntp":   _SHOW_RUN_NTP,
    "show running-config | include log":   "logging host 10.0.0.50",
    "show running-config | include banner":"banner motd ^Authorized access only^",
    "show running-config":                 "hostname mock-device\nntp server 10.0.0.1",
    "show ip interface brief":             "GigabitEthernet0/0  10.0.0.1  YES NVRAM  up  up",
}


class MockDriver(BaseDriver):

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._connected = False
        self._mode      = creds.platform  # "mock", "mock_timeout", etc.

    def connect(self) -> None:
        if self._mode == "mock_timeout":
            raise TimeoutError(f"[mock] SSH timeout connecting to {self.creds.host}")
        if self._mode == "mock_authfail":
            raise ConnectionError(
                f"[mock] Authentication failed for {self.creds.host}"
            )
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def run_command(self, command: str) -> str:
        if not self._connected:
            raise RuntimeError("[mock] Not connected.")
        if self._mode == "mock_cmdfail":
            raise RuntimeError(f"[mock] Command failed: {command}")
        return _CANNED_RESPONSES.get(
            command.strip(),
            f"[mock] No canned response for: {command}",
        )

    def run_config_commands(self, commands: list[str]) -> str:
        if not self._connected:
            raise RuntimeError("[mock] Not connected.")
        lines = []
        for cmd in commands:
            lines.append(f"# {cmd}")
            lines.append("[mock] config applied")
        return "\n".join(lines)

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        if not self._connected:
            raise RuntimeError("[mock] Not connected.")
        if self._mode == "mock_xferfail":
            raise RuntimeError(
                f"[mock] File transfer failed: {local_path} -> {remote_path}"
            )
        # Normal mock: no real I/O — just succeed silently.

    def get_running_config(self) -> str:
        return _CANNED_RESPONSES["show running-config"]
