"""
linux.py — Driver for Linux OS targets (Ubuntu, RHEL, Debian, etc.).

Uses Paramiko directly (no Netmiko needed — no vendor prompt quirks).
Supports running arbitrary shell commands, reading files, and applying
remediation via shell commands (systemctl, sed, apt, etc.).
"""
from __future__ import annotations
import os
import time
import paramiko

from drivers.base import BaseDriver, DeviceCredentials
from core.logger import get_logger

log = get_logger("driver.linux")


class LinuxDriver(BaseDriver):

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._client.connect(
                hostname=self.creds.host,
                port=self.creds.port,
                username=self.creds.username,
                password=self.creds.password,
                timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as e:
            raise ConnectionError(f"Authentication failed for {self.creds.host}: {e}") from e
        except Exception as e:
            raise ConnectionError(f"SSH connection failed to {self.creds.host}: {e}") from e

    def disconnect(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def run_command(self, command: str) -> str:
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        stdin, stdout, stderr = self._client.exec_command(
            command, timeout=self.timeout
        )
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        # Return both stdout and stderr so the log is complete
        return (out + ("\n[stderr]\n" + err if err.strip() else "")).strip()

    def run_config_commands(self, commands: list[str]) -> str:
        """
        For Linux, 'remediation commands' are just shell commands run in sequence.
        If any command returns a non-zero exit code a RuntimeError is raised
        immediately. ssh_worker.py catches it and records it in the
        CommandResult error field; the continue-on-failure policy (other
        devices keep running) still applies at the job level.
        """
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        results = []
        for cmd in commands:
            stdin, stdout, stderr = self._client.exec_command(
                cmd, timeout=self.timeout
            )
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                raise RuntimeError(
                    f"Command failed with exit code {exit_code}: {cmd}\n"
                    f"stderr: {err.strip()}"
                )
            block = f"# {cmd}\n{out}"
            if err.strip():
                block += f"\n[stderr] {err.strip()}"
            results.append(block)
        return "\n".join(results)

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        size_bytes = os.path.getsize(local_path)
        started = time.monotonic()
        sftp = self._client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()
        elapsed = round(time.monotonic() - started, 2)
        log.info("file_transferred",
                 host=self.creds.host,
                 local_path=local_path,
                 remote_path=remote_path,
                 size_bytes=size_bytes,
                 duration_seconds=elapsed)

    def get_running_config(self) -> str:
        """
        For Linux, 'running config' is a snapshot of key system state:
        hostname, OS release, and active network interfaces.
        """
        snapshot_cmd = (
            "echo '=== hostname ===' && hostname && "
            "echo '=== os-release ===' && cat /etc/os-release && "
            "echo '=== ip addr ===' && ip addr show"
        )
        return self.run_command(snapshot_cmd)
