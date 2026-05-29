"""
drivers/wlc_aireos.py — Driver for Cisco WLC AireOS wireless controllers.

AireOS uses a flat CLI — there is no traditional "config mode" in the IOS sense.
All configuration commands are top-level and prefixed with 'config':
  show wlan summary          (exec-level show)
  config wlan enable 1       (top-level config command — NOT inside a mode)
  config interface address management 10.0.0.1 255.255.255.0 10.0.0.254

Netmiko's CiscoWlcSSH handles:
  - AireOS special login flow (User:/Password: prompts)
  - Paging disabled via 'config paging disable'
  - 'logout' on cleanup

run_config_commands() sends commands WITHOUT entering config mode because
AireOS config commands are issued at exec level, not inside a mode.

Error format: AireOS returns "Incorrect usage." on bad commands and may
also print usage help.  Some commands return "Error:" prefixed messages.

Inventory key: cisco_wlc
"""
from __future__ import annotations

from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

from drivers.base import BaseDriver, DeviceCredentials


# AireOS error indicators — checked case-insensitively against command output.
_CLI_ERROR_PHRASES = (
    "incorrect usage.",      # primary AireOS error for bad syntax
    "error:",                # generic error prefix
    "invalid parameter",     # invalid value for a parameter
    "must be between",       # out-of-range value
    "not found",             # object (wlan, interface, etc.) does not exist
    "already exists",        # duplicate creation attempt
    "access denied",         # permission error
)


class WlcAireOsDriver(BaseDriver):
    """
    Driver for Cisco WLC AireOS (2500, 3504, 5520, 8540 series, vWLC).

    Uses Netmiko's cisco_wlc device type which implements the special
    AireOS SSH login sequence (User: / Password: prompts) and disables
    output paging.
    """

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        super().__init__(creds, timeout)
        self._conn = None

    def connect(self) -> None:
        params = {
            "device_type":    "cisco_wlc",
            "host":           self.creds.host,
            "username":       self.creds.username,
            "password":       self.creds.password,
            "port":           self.creds.port,
            "timeout":        self.timeout,
            "conn_timeout":   self.timeout,
            "banner_timeout": self.timeout,
        }
        # WLC does not use an enable secret — access levels are configured per user
        try:
            self._conn = ConnectHandler(**params)
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
                f"WLC command error on {self.creds.host} — '{command}':\n{out}"
            )
        return out

    def run_config_commands(self, commands: list[str]) -> str:
        """
        Execute AireOS configuration commands.

        AireOS config commands are top-level (not inside a mode), so
        send_config_set is called with enter_config_mode=False and
        exit_config_mode=False.  Each command is sent at exec level.
        """
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")
        output_parts: list[str] = []
        for cmd in commands:
            out = self._conn.send_command_timing(
                cmd, read_timeout=self.timeout
            )
            output_parts.append(f"# {cmd}\n{out}")
            if any(p in out.lower() for p in _CLI_ERROR_PHRASES):
                raise RuntimeError(
                    f"WLC config error on {self.creds.host} — '{cmd}':\n{out}"
                )
        return "\n".join(output_parts)

    def transfer_file(self, local_path: str, remote_path: str) -> None:
        """
        WLC AireOS does not support SCP file transfer via standard SSH.
        File transfers to AireOS controllers use TFTP or HTTP via the
        management interface.  Use a run_shell_script_locally step with
        'transfer upload datatype ...' commands instead.
        """
        raise NotImplementedError(
            "WLC AireOS does not support SCP file transfer. "
            "Use 'transfer upload' WLC commands via a run_shell_script_locally step."
        )

    def get_running_config(self) -> str:
        return self.run_command("show run-config")
