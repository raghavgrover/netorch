"""
drivers/viptela.py — Driver for Cisco vEdge (Viptela OS) routers.

Viptela OS uses a candidate config model:
  1. Enter config mode:  conf terminal  (or 'config terminal')
  2. Stage commands into the candidate config
  3. Commit:            commit
  4. Exit:              end

If commit fails, the candidate config is discarded with 'abort'.
Paging is disabled with 'paginate false'.

Config mode prompt: vpn0(config)#  or  vEdge(config)#
Running config:     show running-config

Note on VPN context: Viptela interfaces live inside VPNs (VPN 0 = transport,
VPN 512 = management, others = service VPNs).  Config commands must include
the VPN context:
  vpn 0
    interface ge0/0
      no shutdown

Error format: Similar to IOS — "% Invalid input detected at '^' marker."
Commit errors appear in the commit output as "Aborted" or "Error:".

Inventory key: cisco_viptela
"""
from __future__ import annotations

from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from core.logger import get_logger

from drivers.base import BaseDriver, DeviceCredentials

log = get_logger("driver.viptela")

# Phrases in exec output that indicate a CLI error
_CLI_ERROR_PHRASES = (
    "% invalid input detected",
    "% ambiguous command:",
    "% incomplete command",
    "% error:",
    "% unknown command",
)

# Phrases in commit output that indicate a commit failure
_COMMIT_ERROR_PHRASES = (
    "aborted",
    "error",
    "failed",
    "% ",
)


class ViptelaDriver(BaseDriver):
    """
    Driver for Cisco vEdge routers running Viptela OS.

    Uses Netmiko's cisco_viptela device type, which inherits from
    CiscoSSHConnection and adds a commit() method.  Paging is disabled
    with 'paginate false' during session_preparation.
    """

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._conn = None

    def connect(self) -> None:
        params = {
            "device_type":    "cisco_viptela",
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
            log.info("connected", host=self.creds.host, platform="cisco_viptela")
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
        out = self._conn.send_command(command, read_timeout=self.timeout)
        if any(p in out.lower() for p in _CLI_ERROR_PHRASES):
            raise RuntimeError(
                f"Viptela command error on {self.creds.host} — '{command}':\n{out}"
            )
        return out

    def run_config_commands(self, commands: list[str]) -> str:
        """
        Stage commands → commit (or abort on failure).

        Viptela uses a candidate config model.  send_config_set() enters
        'conf terminal', stages commands, and exits config mode.  The
        commit() call then applies the candidate config.
        """
        if not self._conn:
            raise RuntimeError("Not connected.")

        output_lines: list[str] = []
        self._conn.config_mode()

        try:
            # Stage all commands into candidate config
            for cmd in commands:
                out = self._conn.send_command_timing(cmd, read_timeout=self.timeout)
                output_lines.append(f"# {cmd}\n{out}")
                log.debug("viptela_staged", host=self.creds.host, cmd=cmd)

            # Commit the candidate config
            commit_out = self._conn.send_command_timing("commit", read_timeout=self.timeout)
            output_lines.append(f"# commit\n{commit_out}")
            log.debug("viptela_commit", host=self.creds.host, output=commit_out[:200])

            if any(p in commit_out.lower() for p in _COMMIT_ERROR_PHRASES):
                self._conn.send_command_timing("abort", read_timeout=self.timeout)
                raise RuntimeError(
                    f"Viptela commit failed for {self.creds.host}:\n{commit_out}"
                )

            log.info("viptela_committed", host=self.creds.host)

        except RuntimeError:
            raise
        except Exception as e:
            try:
                self._conn.send_command_timing("abort", read_timeout=self.timeout)
            except Exception:
                pass
            raise RuntimeError(
                f"Viptela config error on {self.creds.host}: {e}"
            ) from e
        finally:
            try:
                self._conn.exit_config_mode()
            except Exception:
                pass

        return "\n".join(output_lines)

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        """
        File transfer to vEdge via SCP.
        remote_path may include a VPN-qualified path:
          /home/admin/filename  →  standard Linux path on vEdge
        Without a prefix, /home/admin/ is used.
        """
        if not self._conn:
            raise RuntimeError("Not connected.")
        import os
        import paramiko
        transport = self._conn.remote_conn.get_transport()
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            dest = remote_path or f"/home/admin/{os.path.basename(local_path)}"
            sftp.put(local_path, dest)
        except Exception as e:
            raise RuntimeError(
                f"File transfer to {self.creds.host}:{remote_path} failed: {e}"
            ) from e
        finally:
            sftp.close()

    def get_running_config(self) -> str:
        return self.run_command("show running-config")
