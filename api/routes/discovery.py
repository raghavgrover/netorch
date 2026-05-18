"""
api/routes/discovery.py — BigFix Asset Discovery endpoints.

GET /discovery/devices
    Queries the BigFix Session Relevance API for unmanaged assets,
    enriches each result with an inferred netorch platform, and flags
    devices already present in the local inventory.

Password resolution order:
  1. OpenBao secret at path:  secret/netorch/bigfix  →  field: password
  2. Environment variable:    BIGFIX_PASSWORD
  3. Error returned inline    (HTTP 200 with error field — never 5xx)
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter

from api.schemas import DiscoveredDevice, DiscoveryResponse
from core.config import bigfix as bigfix_cfg
from core.logger import get_logger
from secrets.inventory import inventory_client

log = get_logger("api.discovery")
router = APIRouter(tags=["discovery"])

# ── BigFix Session Relevance query ────────────────────────────────────────────

_RELEVANCE = (
    "("
    "  values of fields whose (name of it = \"IP Address\") of it,"
    "  values of fields whose (name of it = \"MAC Address\") of it,"
    "  values of fields whose (name of it = \"DNS Name\") of it,"
    "  values of fields whose (name of it = \"OS\") of it,"
    "  values of fields whose (name of it = \"Device Type\") of it,"
    "  values of fields whose (name of it = \"Open Ports\") of it,"
    "  values of fields whose (name of it = \"Scan Time\") of it"
    ") of bes unmanagedassets"
)


# ── Platform inference ────────────────────────────────────────────────────────

def _infer_platform(os_str: str, device_type: str) -> str:
    o = os_str.lower()
    t = device_type.lower()
    if "ios xr" in o:
        return "cisco_xr"
    if "ios xe" in o:
        return "cisco_xe"
    if "nx-os" in o or "nxos" in o:
        return "cisco_nxos"
    if "ios" in o:
        return "cisco_ios"
    if "junos" in o or "juniper" in o:
        return "juniper_junos"
    if "fortios" in o or "fortinet" in o:
        return "fortinet"
    if "linux" in o and "server" in t:
        return "linux"
    if "linux" in o:
        return "linux"
    if t == "printer":
        return "unsupported"
    return "unknown"


# ── Password resolution ───────────────────────────────────────────────────────

def _get_bigfix_password() -> str | None:
    """
    Try OpenBao first, then environment variable.
    Returns the password string or None if not found.
    """
    # 1. Try OpenBao
    try:
        import toml
        from core.config import _find_config
        raw = toml.load(_find_config())
        vault_cfg = raw.get("vault", {})
        if vault_cfg.get("type", "none").lower() == "openbao":
            from secrets.openbao import OpenBaoProvider
            ob = vault_cfg.get("openbao", {})
            provider = OpenBaoProvider(
                url         = ob.get("url", "http://127.0.0.1:8200"),
                auth_method = ob.get("auth_method", "token"),
                token       = ob.get("token", ""),
                role_id     = ob.get("role_id", ""),
                secret_id   = ob.get("secret_id", ""),
                mount       = ob.get("mount", "secret"),
                prefix      = ob.get("prefix", "netorch"),
                verify_ssl  = ob.get("verify_ssl", True),
            )
            # OpenBao path: secret/netorch/bigfix → field: password
            try:
                import hvac
                client = hvac.Client(url=ob.get("url", "http://127.0.0.1:8200"),
                                     token=ob.get("token", ""))
                resp = client.secrets.kv.v2.read_secret_version(
                    path=f"{ob.get('prefix','netorch')}/bigfix",
                    mount_point=ob.get("mount", "secret"),
                )
                pw = resp["data"]["data"].get("password")
                if pw:
                    log.info("bigfix_password_from_openbao")
                    return pw
            except Exception as e:
                log.debug("bigfix_openbao_lookup_failed", error=str(e))
    except Exception as e:
        log.debug("bigfix_vault_init_failed", error=str(e))

    # 2. Try environment variable
    pw = os.environ.get("BIGFIX_PASSWORD", "")
    if pw:
        log.info("bigfix_password_from_env")
        return pw

    return None


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/devices", response_model=DiscoveryResponse,
            summary="Fetch unmanaged assets from BigFix Asset Discovery")
def get_discovery_devices() -> DiscoveryResponse:
    # Validate config
    if not bigfix_cfg.server_url:
        return DiscoveryResponse(
            error="BigFix not configured. Set [bigfix] server_url in netorch.toml."
        )

    password = _get_bigfix_password()
    if not password:
        return DiscoveryResponse(
            error=(
                "BigFix password not found. Store it in OpenBao at "
                "secret/netorch/bigfix (field: password) or set the "
                "BIGFIX_PASSWORD environment variable."
            )
        )

    # Derive display server string from URL
    parsed = urlparse(bigfix_cfg.server_url)
    bigfix_server = parsed.netloc or bigfix_cfg.server_url

    # Call BigFix REST API
    api_url = f"{bigfix_cfg.server_url.rstrip('/')}/api/query"
    try:
        with httpx.Client(verify=bigfix_cfg.verify_ssl, timeout=30) as client:
            resp = client.get(
                api_url,
                params={"relevance": _RELEVANCE, "output": "json"},
                auth=(bigfix_cfg.username, password),
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        log.warning("bigfix_api_error", error=str(e))
        return DiscoveryResponse(
            bigfix_server=bigfix_server,
            error=f"BigFix API unreachable: {e}",
        )
    except Exception as e:
        return DiscoveryResponse(
            bigfix_server=bigfix_server,
            error=f"BigFix request failed: {e}",
        )

    # Build inventory IP set for in_inventory check
    try:
        inventory_ips: set[str] = set(inventory_client.list_hosts())
    except Exception:
        inventory_ips = set()

    # Parse result tuples — BigFix returns:
    # {"result": [[ip, mac, dns, os, dtype, ports, scan_time], ...]}
    # or {"result": [{"tuple": [...]}, ...]} depending on API version
    raw_results = data.get("result", [])
    devices: list[DiscoveredDevice] = []

    for item in raw_results:
        # Normalise: item may be a list/tuple or a dict with a "tuple" key
        if isinstance(item, dict):
            row = item.get("tuple", item.get("answer", []))
        elif isinstance(item, list):
            row = item
        else:
            row = [str(item)]

        # Each element may itself be a list (multi-value field) — take first
        def _first(v):
            if isinstance(v, list):
                return str(v[0]) if v else ""
            return str(v) if v is not None else ""

        ip          = _first(row[0]) if len(row) > 0 else ""
        mac         = _first(row[1]) if len(row) > 1 else ""
        hostname    = _first(row[2]) if len(row) > 2 else ""
        os_str      = _first(row[3]) if len(row) > 3 else ""
        device_type = _first(row[4]) if len(row) > 4 else ""
        open_ports  = _first(row[5]) if len(row) > 5 else ""
        scan_time   = _first(row[6]) if len(row) > 6 else ""

        if not ip:
            continue

        devices.append(DiscoveredDevice(
            ip=ip,
            mac=mac,
            hostname=hostname,
            os=os_str,
            device_type=device_type,
            open_ports=open_ports,
            scan_time=scan_time,
            inferred_platform=_infer_platform(os_str, device_type),
            in_inventory=(ip in inventory_ips),
        ))

    log.info("bigfix_discovery_complete",
             server=bigfix_server, device_count=len(devices))

    return DiscoveryResponse(
        devices=devices,
        total=len(devices),
        bigfix_server=bigfix_server,
    )
