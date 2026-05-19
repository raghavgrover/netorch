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
from fastapi import APIRouter, HTTPException

import configparser
import grp
import ipaddress
import shutil
import stat
import xml.etree.ElementTree as ET
from datetime import datetime

from api.schemas import (
    DiscoveredDevice, DiscoveryResponse,
    AddToInventoryRequest, AddToInventoryResponse,
    TriggerScanRequest, TriggerScanResponse,
)
from core.config import bigfix as bigfix_cfg, inventory as inventory_cfg
from core.logger import get_logger
from secrets.inventory import inventory_client

_GROUP_RE = re.compile(r'^[a-zA-Z0-9_-]+$')

log = get_logger("api.discovery")
router = APIRouter(tags=["discovery"])

# ── BigFix Session Relevance query ────────────────────────────────────────────

_RELEVANCE = (
    "("
    "  values of fields whose (name of it = \"IP Address\") of it,"
    "  values of fields whose (name of it = \"MAC Address\") of it,"
    "  values of fields whose (name of it = \"Hostname\") of it,"
    "  values of fields whose (name of it = \"OS\") of it,"
    "  values of fields whose (name of it = \"Device Type\") of it,"
    "  values of fields whose (name of it = \"Scan Point\") of it,"
    "  values of fields whose (name of it = \"Last Scan Time (Server Time)\") of it"
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

    # 3. Try inline password in netorch.toml [bigfix] section
    if bigfix_cfg.password:
        log.info("bigfix_password_from_toml")
        return bigfix_cfg.password

    return None


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/config", summary="Return non-sensitive BigFix discovery configuration")
def get_discovery_config():
    return {
        "scan_point_id": bigfix_cfg.scan_point_id,
        "server_url":    bigfix_cfg.server_url,
        "configured":    bool(bigfix_cfg.server_url),
    }


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
        scan_point  = _first(row[5]) if len(row) > 5 else ""   # Scan Point (column 5)
        scan_time   = _first(row[6]) if len(row) > 6 else ""

        if not ip:
            continue

        devices.append(DiscoveredDevice(
            ip=ip,
            mac=mac,
            hostname=hostname,
            os=os_str,
            device_type=device_type,
            open_ports=scan_point,   # shows which Scan Point detected this device
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


@router.get("/inventory-groups", summary="Return the group names in a specific inventory file")
def get_inventory_groups(file: str = "") -> dict:
    """Return the [section] names from a given .ini inventory file."""
    if not file or "/" in file or ".." in file:
        return {"groups": []}
    inv_dir = inventory_cfg.path if inventory_cfg.path.is_dir() else inventory_cfg.path.parent
    target = inv_dir / file
    if not target.exists():
        return {"groups": []}
    parser = configparser.RawConfigParser(allow_no_value=True, strict=False)
    parser.optionxform = str
    try:
        parser.read_string(target.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {"groups": []}
    # Exclude meta-sections like "all:vars"
    groups = [s for s in parser.sections() if ":" not in s]
    return {"groups": groups}


@router.post("/add-to-inventory", response_model=AddToInventoryResponse,
             summary="Add discovered devices to a netorch inventory file")
def add_to_inventory(body: AddToInventoryRequest) -> AddToInventoryResponse:
    # ── Validate group name (optional — defaults to "ungrouped") ─────────────
    group_name = body.group_name.strip() if body.group_name else "ungrouped"
    if not _GROUP_RE.match(group_name):
        raise HTTPException(
            status_code=400,
            detail="group_name must contain only letters, digits, underscores, or hyphens.",
        )
    # Patch body so the rest of the function uses the resolved name
    body = body.model_copy(update={"group_name": group_name})

    if not body.devices:
        raise HTTPException(status_code=400, detail="No devices provided.")

    inv_dir = inventory_cfg.path if inventory_cfg.path.is_dir() else inventory_cfg.path.parent

    # ── Resolve target file ───────────────────────────────────────────────────
    if body.target == "existing":
        fname = body.inventory_file.strip()
        if not fname:
            raise HTTPException(status_code=400, detail="inventory_file is required for target=existing.")
        if "/" in fname or ".." in fname:
            raise HTTPException(status_code=400, detail="Invalid inventory_file name.")
        target_path = inv_dir / fname
        if not target_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Inventory file '{fname}' not found.",
            )
        display_file = fname

    elif body.target == "new":
        fname = body.new_filename.strip()
        if not fname:
            raise HTTPException(status_code=400, detail="new_filename is required for target=new.")
        if not fname.endswith(".ini"):
            raise HTTPException(status_code=400, detail="new_filename must end in .ini")
        if "/" in fname or ".." in fname:
            raise HTTPException(status_code=400, detail="Invalid new_filename.")
        target_path = inv_dir / fname
        display_file = fname

    else:
        raise HTTPException(status_code=400, detail="target must be 'existing' or 'new'.")

    # ── Read existing file (or start fresh) ───────────────────────────────────
    existing_content = ""
    if target_path.exists():
        try:
            existing_content = target_path.read_text(encoding="utf-8")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot read inventory file: {e}")

    # ── Build host entry lines ────────────────────────────────────────────────
    def _host_line(dev) -> str:
        line = f"{dev.ip}  platform={dev.platform}  port={dev.port}"
        hostname = (dev.hostname or "").strip()
        if hostname and hostname.lower() not in ("n/a", "na", "-", "none", "unknown"):
            line += f"  ; {hostname}"
        return line + "\n"

    host_lines = [_host_line(d) for d in body.devices]

    # ── Determine write strategy ──────────────────────────────────────────────
    section_header = f"[{body.group_name}]"

    # Check if the section already exists (case-insensitive search)
    section_exists = any(
        line.strip().lower() == section_header.lower()
        for line in existing_content.splitlines()
    )

    if section_exists:
        # Insert new hosts immediately after the [group_name] header line
        # so they always appear at the top of the group, before any existing
        # entries or comments.
        lines_in_file = existing_content.splitlines(keepends=True)
        insert_at = len(lines_in_file)   # fallback: end of file
        for i, line in enumerate(lines_in_file):
            if line.strip().lower() == section_header.lower():
                insert_at = i + 1   # right after the header
                break
        new_lines = (
            lines_in_file[:insert_at]
            + host_lines
            + lines_in_file[insert_at:]
        )
        try:
            target_path.write_text("".join(new_lines), encoding="utf-8")
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    else:
        # New section — append at end of file
        prefix = "\n" if existing_content and not existing_content.endswith("\n") else ""
        new_section = f"{prefix}\n{section_header}\n" + "".join(host_lines)
        try:
            mode = "a" if existing_content else "w"
            with open(target_path, mode, encoding="utf-8") as fh:
                fh.write(new_section)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Write failed: {e}")

    # Set ownership: root:netorch, 640
    try:
        netorch_gid = grp.getgrnam("netorch").gr_gid
        import os as _os
        _os.chown(target_path, 0, netorch_gid)
        target_path.chmod(0o640)
    except Exception as e:
        log.warning("inventory_chown_failed", file=str(target_path), error=str(e))

    # ── Reload inventory ──────────────────────────────────────────────────────
    try:
        inventory_client.reload()
    except Exception as e:
        log.warning("inventory_reload_failed", error=str(e))

    log.info("discovery_added_to_inventory",
             file=display_file, group=body.group_name, count=len(body.devices))

    return AddToInventoryResponse(
        added=len(body.devices),
        file=display_file,
        group=body.group_name,
    )


# ── Subnet validation helper ──────────────────────────────────────────────────

def _validate_subnet(subnet: str) -> bool:
    """Accept CIDR (10.0.0.0/24) or range (10.0.0.1-254)."""
    subnet = subnet.strip()
    # CIDR
    try:
        ipaddress.ip_network(subnet, strict=False)
        return True
    except ValueError:
        pass
    # Range: x.x.x.x-y or x.x.x.x-x.x.x.y
    parts = subnet.split("-")
    if len(parts) == 2:
        try:
            ipaddress.ip_address(parts[0].strip())
            end = parts[1].strip()
            # end may be just the last octet or a full IP
            if "." not in end:
                int(end)   # just a number
            else:
                ipaddress.ip_address(end)
            return True
        except (ValueError, AttributeError):
            pass
    return False


@router.post("/trigger-scan", response_model=TriggerScanResponse,
             summary="Create a BigFix Action to run an nmap scan on the Scan Point")
def trigger_scan(body: TriggerScanRequest) -> TriggerScanResponse:
    subnet = body.subnet.strip()

    # ── Validate subnet ───────────────────────────────────────────────────────
    if not _validate_subnet(subnet):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid subnet or range: '{subnet}'. "
                   "Use CIDR notation (10.0.1.0/24) or range (10.0.1.1-254).",
        )

    # ── Check scan_point_id configured ───────────────────────────────────────
    if not bigfix_cfg.scan_point_id or bigfix_cfg.scan_point_id == "0":
        return TriggerScanResponse(
            error="scan_point_id not configured in [bigfix] section of netorch.toml"
        )

    # ── Resolve password ──────────────────────────────────────────────────────
    if not bigfix_cfg.server_url:
        return TriggerScanResponse(
            error="BigFix not configured. Set [bigfix] server_url in netorch.toml."
        )

    password = _get_bigfix_password()
    if not password:
        return TriggerScanResponse(
            error=(
                "BigFix password not found. Store it in OpenBao at "
                "secret/netorch/bigfix (field: password) or set "
                "BIGFIX_PASSWORD environment variable."
            )
        )

    # ── Build nmap output path ────────────────────────────────────────────────
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    if bigfix_cfg.scan_point_os.lower() == "windows":
        output_path = f"C:\\Temp\\netorch_nmap_{ts}.xml"
    else:
        output_path = f"/tmp/netorch_nmap_{ts}.xml"

    # ── Build Action XML ──────────────────────────────────────────────────────
    action_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BES>
  <SingleAction>
    <Title>netorch: Run Nmap Scan - {subnet}</Title>
    <Relevance>true</Relevance>
    <ActionScript MIMEType="application/x-Fixlet-Windows-Shell">waithidden nmap -sV -O --host-timeout 60s {subnet} -oX &quot;{output_path}&quot;</ActionScript>
    <Target>
      <ComputerID>{bigfix_cfg.scan_point_id}</ComputerID>
    </Target>
    <Settings>
      <HasTimeRange>false</HasTimeRange>
      <HasStartTime>false</HasStartTime>
      <HasEndTime>false</HasEndTime>
      <PreActionShowUI>false</PreActionShowUI>
      <HasMessageTemplate>false</HasMessageTemplate>
    </Settings>
  </SingleAction>
</BES>"""

    # ── POST to BigFix /api/actions ───────────────────────────────────────────
    api_url = f"{bigfix_cfg.server_url.rstrip('/')}/api/actions"
    try:
        with httpx.Client(verify=bigfix_cfg.verify_ssl, timeout=30) as client:
            resp = client.post(
                api_url,
                content=action_xml.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                auth=(bigfix_cfg.username, password),
            )
            resp.raise_for_status()
            resp_text = resp.text
    except httpx.HTTPError as e:
        log.warning("bigfix_trigger_scan_error", error=str(e))
        return TriggerScanResponse(error=f"BigFix API unreachable: {e}")
    except Exception as e:
        return TriggerScanResponse(error=f"Request failed: {e}")

    # ── Parse Action ID from response XML ─────────────────────────────────────
    action_id: int | None = None
    try:
        root = ET.fromstring(resp_text)
        # BigFix returns: <BESAPI><Action ...><ID>12345</ID>...
        id_el = root.find(".//Action/ID") or root.find(".//ID")
        if id_el is not None and id_el.text:
            action_id = int(id_el.text.strip())
    except Exception as e:
        log.warning("bigfix_action_parse_error", error=str(e), body=resp_text[:500])

    log.info("bigfix_scan_triggered",
             subnet=subnet, scan_point_id=bigfix_cfg.scan_point_id,
             action_id=action_id)

    return TriggerScanResponse(
        action_id=action_id,
        message=(
            f"Scan triggered on BigFix Scan Point (Computer ID {bigfix_cfg.scan_point_id}). "
            "Results will appear in the Discovery table within 15–30 minutes."
        ),
    )
