"""
drivers/__init__.py — Factory: instantiate the correct driver based on
platform string from inventory.ini or the job payload.
"""
from drivers.base import BaseDriver, DeviceCredentials
from drivers.ios_xe      import IosXeDriver
from drivers.ios_xr      import IosXrDriver
from drivers.nxos        import NxosDriver
from drivers.junos       import JunosDriver
from drivers.fortinet    import FortinetDriver
from drivers.linux       import LinuxDriver
from drivers.mock        import MockDriver
from drivers.aruba_aoscx import ArubaAosCxDriver
from drivers.wlc_aireos  import WlcAireOsDriver
from drivers.viptela     import ViptelaDriver

_DRIVER_MAP: dict[str, type[BaseDriver]] = {
    # Cisco IOS / IOS-XE
    "cisco_ios":      IosXeDriver,
    "cisco_xe":       IosXeDriver,
    # Cisco IOS-XR
    "cisco_xr":       IosXrDriver,
    # Cisco NX-OS
    "cisco_nxos":     NxosDriver,
    # Cisco SD-WAN (vEdge / Viptela OS)
    "cisco_viptela":  ViptelaDriver,
    # Cisco WLC AireOS
    "cisco_wlc":      WlcAireOsDriver,
    # Juniper Junos
    "juniper_junos":  JunosDriver,
    # Fortinet FortiOS
    "fortinet":       FortinetDriver,
    # Aruba AOS-CX
    "aruba_aoscx":    ArubaAosCxDriver,
    # Linux
    "linux":          LinuxDriver,
    # Mock drivers for tests
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
