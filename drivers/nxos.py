"""
drivers/nxos.py — Cisco NX-OS driver (Nexus data centre switches).

NX-OS uses a similar config model to IOS-XE: config mode entry, apply
commands, exit. No candidate config or commit step. Changes take effect
immediately on exit from config mode.

File transfer targets bootflash:/ by default. Remote path may include
an explicit file-system prefix (bootflash:, volatile:, etc.).
"""
from __future__ import annotations
import os
import re
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from netmiko import file_transfer as netmiko_file_transfer

from drivers.base import BaseDriver, DeviceCredentials

# NX-OS CLI error prefixes (exec and config mode).
# NX-OS uses "% Invalid command at '^' marker." instead of IOS's
# "% Invalid input detected at '^' marker." — keep phrase lists separate.
_CLI_ERROR_PHRASES = (
    "% invalid command",    # unrecognised command
    "% incomplete command", # missing required arguments
    "% ambiguous command",  # partial command matches more than one
    "% invalid input",      # alternate NX-OS error form
    "% permission denied",  # role-based access denied
)


class NxosDriver(BaseDriver):

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._conn = None

    def connect(self) -> None:
        params = {
            "device_type":    "cisco_nxos",
            "host":           self.creds.host,
            "username":       self.creds.username,
            "password":       self.creds.password,
            "port":           self.creds.port,
            "timeout":        self.timeout,
            "conn_timeout":   self.timeout,
            "banner_timeout": self.timeout,
        }
        # NX-OS does not use an enable secret — roles are configured on the device
        try:
            self._conn = ConnectHandler(**params)
        except NetmikoAuthenticationException as e:
            raise ConnectionError(f"Authentication failed for {self.creds.host}: {e}") from e
        except NetmikoTimeoutException as e:
            raise TimeoutError(f"SSH timeout connecting to {self.creds.host}: {e}") from e

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
                f"NX-OS command error on {self.creds.host} — '{command}':\n{out}"
            )
        return out

    def run_config_commands(self, commands: list[str]) -> str:
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")
        out = self._conn.send_config_set(commands, read_timeout=self.timeout)
        if any(p in out.lower() for p in _CLI_ERROR_PHRASES):
            raise RuntimeError(
                f"NX-OS config error on {self.creds.host}:\n{out}"
            )
        return out

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        """
        Transfer a file to the device via SCP.
        remote_path may include an NX-OS file-system prefix:
          bootflash:firmware.bin   →  file_system="bootflash:", dest="firmware.bin"
          volatile:fw.bin          →  file_system="volatile:",  dest="fw.bin"
        Without a prefix, bootflash: is assumed.
        """
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")
        m = re.match(r'^(\w+:[/]?)', remote_path)
        if m:
            file_system = m.group(1)
            dest_file   = remote_path[len(file_system):]
        else:
            file_system = 'bootflash:'
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
