"""
drivers/junos.py — Juniper Junos driver (SRX, EX, QFX, MX).

Junos uses a candidate config model:
  1. Enter configuration mode
  2. Stage commands into candidate config
  3. Run 'commit check' — validates syntax without applying
  4. If check passes, run 'commit'
  5. On any error, run 'rollback 0' to discard candidate config

This matches the IOS-XR discipline and prevents partial configs.
File transfer targets /var/tmp/ by default (writable on all Junos platforms).
"""
from __future__ import annotations
import os
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

from core.logger import get_logger
from drivers.base import BaseDriver, DeviceCredentials

log = get_logger("driver.junos")

_COMMIT_CHECK_ERROR_PHRASES = (
    "error",
    "failed",
    "syntax error",
    "unknown identifier",
    "mgd:",
)


class JunosDriver(BaseDriver):

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._conn = None

    def connect(self) -> None:
        params = {
            "device_type":    "juniper_junos",
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
            log.info("connected", host=self.creds.host, platform="juniper_junos")
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
        Stage → commit check → commit (or rollback 0 on failure).
        Raises RuntimeError if commit check detects config errors.
        """
        if not self._conn:
            raise RuntimeError("Not connected.")

        output_lines: list[str] = []
        self._conn.config_mode()

        try:
            for cmd in commands:
                out = self._conn.send_command_timing(cmd, read_timeout=self.timeout)
                output_lines.append(f"# {cmd}\n{out}")
                log.debug("junos_staged", host=self.creds.host, cmd=cmd)

            check_out = self._conn.send_command_timing(
                "commit check", read_timeout=self.timeout
            )
            output_lines.append(f"# commit check\n{check_out}")
            log.debug("junos_commit_check", host=self.creds.host, output=check_out[:200])

            if any(p in check_out.lower() for p in _COMMIT_CHECK_ERROR_PHRASES):
                self._conn.send_command_timing("rollback 0", read_timeout=self.timeout)
                raise RuntimeError(
                    f"Junos commit check failed for {self.creds.host}:\n{check_out}"
                )

            commit_out = self._conn.send_command_timing(
                "commit", read_timeout=self.timeout
            )
            output_lines.append(f"# commit\n{commit_out}")
            log.info("junos_committed", host=self.creds.host)

            if "error" in commit_out.lower() or "failed" in commit_out.lower():
                self._conn.send_command_timing("rollback 0", read_timeout=self.timeout)
                raise RuntimeError(
                    f"Junos commit failed for {self.creds.host}:\n{commit_out}"
                )

        except RuntimeError:
            raise
        except Exception as e:
            try:
                self._conn.send_command_timing("rollback 0", read_timeout=self.timeout)
            except Exception:
                pass
            raise RuntimeError(f"Junos config error on {self.creds.host}: {e}") from e
        finally:
            try:
                self._conn.exit_config_mode()
            except Exception:
                pass

        return "\n".join(output_lines)

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        """
        Transfer a file to the device via SCP.
        Defaults to /var/tmp/ if no directory is specified in remote_path.
        /var/tmp/ is writable on all Junos platforms (SRX, EX, QFX, MX).
        """
        if not self._conn:
            raise RuntimeError("Not connected.")
        if not os.path.dirname(remote_path):
            remote_path = f"/var/tmp/{remote_path}"

        # Netmiko's file_transfer helper does not support juniper_junos;
        # use send_command to invoke Junos CLI 'file copy' after SCP upload.
        # We transfer to /var/tmp/ via the underlying Paramiko SCP channel.
        try:
            with self._conn.client.open_sftp() as sftp:
                sftp.put(local_path, remote_path)
            log.info("junos_file_transferred",
                     host=self.creds.host, src=local_path, dst=remote_path)
        except Exception as e:
            raise RuntimeError(
                f"File transfer to {self.creds.host}:{remote_path} failed: {e}"
            ) from e

    def get_running_config(self) -> str:
        return self.run_command("show configuration")
