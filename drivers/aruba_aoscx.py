"""
drivers/aruba_aoscx.py — Driver for Aruba AOS-CX switches.

AOS-CX uses an IOS-style CLI:
  - Privileged exec prompt:  switch#
  - Config mode prompt:      switch(config)#
  - Config entry:            configure terminal
  - Config exit:             end / exit

Netmiko has no dedicated aruba_aoscx device type.  The cisco_ios device type
provides compatible prompt detection, enable-mode handling, and config mode
entry/exit for AOS-CX because the prompt and command structure are
intentionally IOS-compatible.

Config model: immediate-apply (no commit step), identical to IOS/IOS-XE.
Error format: "% Invalid input detected at '^' marker." (IOS-compatible).

Inventory key: aruba_aoscx
"""
from __future__ import annotations

import os
import re
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

from drivers.base import BaseDriver, DeviceCredentials


# AOS-CX shares the IOS "% " error prefix convention.
_CLI_ERROR_PHRASES = (
    "% invalid input detected",
    "% ambiguous command:",
    "% incomplete command",
    "% authorization failed",
    "% error:",
    "% unknown command",
    "% no such file or directory",
)


class ArubaAosCxDriver(BaseDriver):
    """
    Driver for Aruba AOS-CX switches (3800, 6000, 6100, 6200, 6300, 8xxx series).

    Uses cisco_ios device_type in Netmiko — AOS-CX CLI is IOS-compatible
    for standard show commands and configuration operations.
    """

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._conn = None

    def connect(self) -> None:
        params = {
            "device_type":    "cisco_ios",   # AOS-CX uses IOS-compatible prompts
            "host":           self.creds.host,
            "username":       self.creds.username,
            "password":       self.creds.password,
            "port":           self.creds.port,
            "timeout":        self.timeout,
            "conn_timeout":   self.timeout,
            "banner_timeout": self.timeout,
        }
        if self.creds.enable_secret:
            params["secret"] = self.creds.enable_secret

        try:
            self._conn = ConnectHandler(**params)
            if self.creds.enable_secret:
                self._conn.enable()
        except NetmikoAuthenticationException as e:
            raise ConnectionError(
                f"Authentication failed for {self.creds.host}: {e}"
            ) from e
        except NetmikoTimeoutException as e:
            raise TimeoutError(
                f"SSH timeout connecting to {self.creds.host}: {e}"
            ) from e

    def disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.disconnect()
            except Exception:
                pass
            self._conn = None

    def run_command(self, command: str) -> str:
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")
        out = self._conn.send_command(command, read_timeout=self.timeout)
        if any(p in out.lower() for p in _CLI_ERROR_PHRASES):
            raise RuntimeError(
                f"AOS-CX command error on {self.creds.host} — '{command}':\n{out}"
            )
        return out

    def run_config_commands(self, commands: list[str]) -> str:
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")
        out = self._conn.send_config_set(commands, read_timeout=self.timeout)
        if any(p in out.lower() for p in _CLI_ERROR_PHRASES):
            raise RuntimeError(
                f"AOS-CX config error on {self.creds.host}:\n{out}"
            )
        return out

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        """
        AOS-CX supports SCP for file transfers.
        remote_path may include a filesystem prefix:
          flash:/filename   →  file_system="flash:/",  dest="filename"
        Without a prefix, flash:/ is assumed.
        """
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")
        from netmiko import file_transfer as netmiko_file_transfer
        m = re.match(r'^(\w+:[/]?)', remote_path)
        if m:
            file_system = m.group(1)
            dest_file   = remote_path[len(file_system):]
        else:
            file_system = 'flash:/'
            dest_file   = os.path.basename(remote_path)
        if not dest_file:
            dest_file = os.path.basename(local_path)
        netmiko_file_transfer(
            ssh_conn=self._conn,
            source_file=local_path,
            dest_file=dest_file,
            file_system=file_system,
            direction='put',
            overwrite_file=True,
        )

    def get_running_config(self) -> str:
        return self.run_command("show running-config")
