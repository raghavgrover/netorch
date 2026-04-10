"""
drivers/ios_xr.py — Cisco IOS-XR driver with commit validation.

IOS-XR uses a candidate config model:
  1. Enter config mode
  2. Stage commands into candidate config
  3. Run 'commit dry-run' — validates syntax without applying
  4. If dry-run passes, run 'commit'
  5. On any error, run 'abort' to discard candidate config

This ensures no partial configs are ever applied.
"""
from __future__ import annotations
import os
import re
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from netmiko import file_transfer as netmiko_file_transfer
from core.logger import get_logger
from drivers.base import BaseDriver, DeviceCredentials

log = get_logger("driver.ios_xr")

# Phrases in dry-run output that indicate a config error
_DRY_RUN_ERROR_PHRASES = (
    "error",
    "invalid",
    "failed",
    "% ",       # IOS-XR error prefix
)


class IosXrDriver(BaseDriver):

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._conn = None

    def connect(self) -> None:
        params = {
            "device_type":    "cisco_xr",
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
            log.info("connected", host=self.creds.host, platform="cisco_xr")
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
            log.info("disconnected", host=self.creds.host)

    def run_command(self, command: str) -> str:
        if not self._conn:
            raise RuntimeError("Not connected.")
        return self._conn.send_command(command, read_timeout=self.timeout)

    def run_config_commands(self, commands: list[str]) -> str:
        """
        Stage → dry-run validate → commit (or abort on failure).
        Raises RuntimeError if dry-run detects config errors.
        """
        if not self._conn:
            raise RuntimeError("Not connected.")

        output_lines: list[str] = []
        self._conn.config_mode()

        try:
            # Stage all commands into candidate config
            for cmd in commands:
                out = self._conn.send_command_timing(
                    cmd, read_timeout=self.timeout
                )
                output_lines.append(f"# {cmd}\n{out}")
                log.debug("xr_staged", host=self.creds.host, cmd=cmd)

            # Dry-run validation
            dry_run_out = self._conn.send_command_timing(
                "commit dry-run", read_timeout=self.timeout
            )
            output_lines.append(f"# commit dry-run\n{dry_run_out}")
            log.debug("xr_dry_run", host=self.creds.host, output=dry_run_out[:200])

            dry_run_lower = dry_run_out.lower()
            if any(phrase in dry_run_lower for phrase in _DRY_RUN_ERROR_PHRASES):
                self._conn.send_command_timing("abort", read_timeout=self.timeout)
                raise RuntimeError(
                    f"IOS-XR dry-run validation failed for {self.creds.host}:\n"
                    f"{dry_run_out}"
                )

            # Commit
            commit_out = self._conn.send_command_timing(
                "commit", read_timeout=self.timeout
            )
            output_lines.append(f"# commit\n{commit_out}")
            log.info("xr_committed", host=self.creds.host)

            if "error" in commit_out.lower() or "failed" in commit_out.lower():
                self._conn.send_command_timing("abort", read_timeout=self.timeout)
                raise RuntimeError(
                    f"IOS-XR commit failed for {self.creds.host}:\n{commit_out}"
                )

        except RuntimeError:
            raise
        except Exception as e:
            try:
                self._conn.send_command_timing("abort", read_timeout=self.timeout)
            except Exception:
                pass
            raise RuntimeError(
                f"IOS-XR config error on {self.creds.host}: {e}"
            ) from e
        finally:
            try:
                self._conn.exit_config_mode()
            except Exception:
                pass

        return "\n".join(output_lines)

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        """
        Transfer a file to the device using Netmiko's SCP helper.
        remote_path may include an IOS-XR file-system prefix:
          disk0:firmware.bin    →  file_system="disk0:",    dest="firmware.bin"
          harddisk:/fw.bin      →  file_system="harddisk:/", dest="fw.bin"
          bootflash:fw.bin      →  file_system="bootflash:", dest="fw.bin"
        Without a prefix, disk0: is assumed.
        """
        if not self._conn:
            raise RuntimeError("Not connected.")
        m = re.match(r'^(\w+:[/]?)', remote_path)
        if m:
            file_system = m.group(1)
            dest_file   = remote_path[len(file_system):]
        else:
            file_system = 'disk0:'
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
