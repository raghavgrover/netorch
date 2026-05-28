"""
drivers/__init__.py — Factory: instantiate the correct driver based on
platform string from inventory.ini or the job payload.
"""
from drivers.base import BaseDriver, DeviceCredentials
from drivers.ios_xe  import IosXeDriver
from drivers.ios_xr  import IosXrDriver
from drivers.nxos    import NxosDriver
from drivers.junos   import JunosDriver
from drivers.fortinet import FortinetDriver
from drivers.linux   import LinuxDriver
from drivers.mock    import MockDriver

_DRIVER_MAP: dict[str, type[BaseDriver]] = {
    "cisco_ios":      IosXeDriver,
    "cisco_xe":       IosXeDriver,
    "cisco_xr":       IosXrDriver,
    "cisco_nxos":     NxosDriver,
    "juniper_junos":  JunosDriver,
    "fortinet":       FortinetDriver,
    "linux":          LinuxDriver,
    "mock":           MockDriver,
    "mock_timeout":   MockDriver,
    "mock_authfail":  MockDriver,
    "mock_cmdfail":   MockDriver,
    "mock_xferfail":  MockDriver,
}


def get_driver(creds: DeviceCredentials, timeout: int = 30) -> BaseDriver:
    """Return an instantiated (not yet connected) driver for the given platform."""
    driver_cls = _DRIVER_MAP.get(creds.platform)
    if not driver_cls:
        raise ValueError(
            f"Unsupported platform '{creds.platform}'. "
            f"Supported: {list(_DRIVER_MAP.keys())}"
        )
    return driver_cls(creds, timeout=timeout)
