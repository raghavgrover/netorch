"""
drivers/fortinet.py — Fortinet FortiOS driver (FortiGate firewalls).

FortiOS applies configuration changes immediately (no candidate config or
commit step). Config commands follow the FortiOS CLI hierarchy:
  config <object>
      edit <entry>
          set <key> <value>
      next
  end

File transfer uses the Paramiko SFTP channel directly; FortiOS accepts
SFTP uploads to /tmp/ which persists across sessions until reboot.
"""
from __future__ import annotations
import os
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

from core.logger import get_logger
from drivers.base import BaseDriver, DeviceCredentials

log = get_logger("driver.fortinet")


class FortinetDriver(BaseDriver):

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._conn = None

    def connect(self) -> None:
        params = {
            "device_type":    "fortinet",
            "host":           self.creds.host,
            "username":       self.creds.username,
            "password":       self.creds.password,
            "port":           self.creds.port,
            "timeout":        self.timeout,
            "conn_timeout":   self.timeout,
            "banner_timeout": self.timeout,
        }
        try:
            self._conn = ConnectHandler(**params)
            log.info("connected", host=self.creds.host, platform="fortinet")
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
            log.info("disconnected", host=self.creds.host)

    def run_command(self, command: str) -> str:
        if not self._conn:
            raise RuntimeError("Not connected.")
        return self._conn.send_command(command, read_timeout=self.timeout)

    def run_config_commands(self, commands: list[str]) -> str:
        """
        Send a sequence of FortiOS CLI commands.
        FortiOS applies changes immediately — there is no candidate config or
        commit/rollback mechanism. Commands are sent one at a time so that
        output is captured per-command and errors are surfaced immediately.
        """
        if not self._conn:
            raise RuntimeError("Not connected.")
        output_lines: list[str] = []
        for cmd in commands:
            out = self._conn.send_command_timing(cmd, read_timeout=self.timeout)
            output_lines.append(f"# {cmd}\n{out}")
            if "command parse error" in out.lower() or "unknown action" in out.lower():
                raise RuntimeError(
                    f"FortiOS command error on {self.creds.host} — '{cmd}':\n{out}"
                )
        return "\n".join(output_lines)

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        """
        Transfer a file to the device via SFTP.
        Defaults to /tmp/ if no directory is specified in remote_path.
        /tmp/ is writable on all FortiGate platforms and persists until reboot.
        Use 'execute restore ...' CLI commands after transfer to apply firmware
        or config files.
        """
        if not self._conn:
            raise RuntimeError("Not connected.")
        if not os.path.dirname(remote_path):
            remote_path = f"/tmp/{remote_path}"
        try:
            with self._conn.client.open_sftp() as sftp:
                sftp.put(local_path, remote_path)
            log.info("fortinet_file_transferred",
                     host=self.creds.host, src=local_path, dst=remote_path)
        except Exception as e:
            raise RuntimeError(
                f"File transfer to {self.creds.host}:{remote_path} failed: {e}"
            ) from e

    def get_running_config(self) -> str:
        return self.run_command("show full-configuration")
