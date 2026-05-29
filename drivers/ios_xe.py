"""
ios_xe.py — Driver for Cisco IOS and IOS-XE devices (switches, routers).

Uses Netmiko for vendor-aware SSH handling: correct prompt detection,
enable mode, config mode entry/exit, and pagination disabling.
"""
from __future__ import annotations
import os
import re
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from netmiko import file_transfer as netmiko_file_transfer

from drivers.base import BaseDriver, DeviceCredentials

# IOS / IOS-XE CLI error prefixes (exec and config mode).
# All IOS user-facing errors begin with "% " (percent + space).
# Syslog entries use "%FACILITY-SEVERITY-MNEMONIC:" (no space after %)
# so they are not matched here.
_CLI_ERROR_PHRASES = (
    "% invalid input detected",   # unrecognised command — most common
    "% ambiguous command:",        # partial command matches more than one
    "% incomplete command",        # missing required arguments
    "% authorization failed",      # AAA authorisation denied
    "% error:",                    # generic IOS error prefix
    "% bad passwords",             # incorrect enable secret
    "% no such file or directory", # file-system reference not found
    "% unknown command",           # older IOS variants
)


class IosXeDriver(BaseDriver):

    # Netmiko device_type strings for IOS and IOS-XE
    _PLATFORM_MAP = {
        "cisco_ios": "cisco_ios",
        "cisco_xe":  "cisco_xe",
    }

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._conn = None

    def connect(self) -> None:
        device_type = self._PLATFORM_MAP.get(self.creds.platform, "cisco_ios")
        params = {
            "device_type":   device_type,
            "host":          self.creds.host,
            "username":      self.creds.username,
            "password":      self.creds.password,
            "port":          self.creds.port,
            "timeout":       self.timeout,
            "conn_timeout":  self.timeout,
            "banner_timeout": self.timeout,
        }
        if self.creds.enable_secret:
            params["secret"] = self.creds.enable_secret

        try:
            self._conn = ConnectHandler(**params)
            if self.creds.enable_secret:
                self._conn.enable()
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
                f"IOS command error on {self.creds.host} — '{command}':\n{out}"
            )
        return out

    def run_config_commands(self, commands: list[str]) -> str:
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")
        out = self._conn.send_config_set(commands, read_timeout=self.timeout)
        if any(p in out.lower() for p in _CLI_ERROR_PHRASES):
            raise RuntimeError(
                f"IOS config error on {self.creds.host}:\n{out}"
            )
        return out

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        """
        Transfer a file to the device using Netmiko's SCP/TFTP helper.
        remote_path may include an IOS file-system prefix:
          flash:/firmware.bin  →  file_system="flash:/",  dest="firmware.bin"
          bootflash:fw.bin     →  file_system="bootflash:", dest="fw.bin"
        Without a prefix, flash:/ is assumed.
        """
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")
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
