"""
drivers/base.py — Abstract base driver and DeviceCredentials dataclass.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DeviceCredentials:
    host:           str
    username:       str
    password:       str
    enable_secret:  Optional[str] = None
    platform:       str           = "cisco_ios"
    port:           int           = 22
    # Internal: set by executor when a device entry could not be resolved.
    # The SSH worker surfaces this as a failed DeviceResult immediately.
    _expansion_error: Optional[str] = field(default=None, repr=False)


class BaseDriver(ABC):

    def __init__(self, creds: DeviceCredentials, timeout: int = 30):
        self.creds   = creds
        self.timeout = timeout

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def run_command(self, command: str) -> str: ...

    @abstractmethod
    def run_config_commands(self, commands: list[str]) -> str: ...

    @abstractmethod
    def get_running_config(self) -> str: ...

    @abstractmethod
    def transfer_file(self, local_path: str, remote_path: str) -> None: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
