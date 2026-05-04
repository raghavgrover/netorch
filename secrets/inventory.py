"""
secrets/inventory.py — Ansible-style INI credential resolver.

Reads from inventory_cfg.path which may be:
  - A single .ini file (legacy single-file layout)
  - A directory containing multiple .ini files (multi-file layout)

INI FORMAT NOTE:
  Each host line looks like:
    192.168.1.10  platform=cisco_ios  username=netaudit  password=MyPass

  configparser.RawConfigParser splits on the FIRST '=' in a line, so the
  above becomes:
    key = "192.168.1.10  platform"   (everything before the first =)
    val = "cisco_ios  username=netaudit  password=MyPass"

  The fix: split raw_key on whitespace → parts[0] is the actual host IP/name,
  parts[1:] joined with '=' + raw_val reconstructs the full inline var string.

Public interface:
  inventory_client.get_credentials(host, group=None, platform_hint=None)
  inventory_client.get_group_hosts(group)
  inventory_client.list_hosts()           → list[str]
  inventory_client.list_groups()          → list[str]
  inventory_client.list_host_entries()    → list[dict]   (UI pagination)
  inventory_client.reload()
"""
from __future__ import annotations

import configparser
import threading
from pathlib import Path
from typing import Optional

from drivers.base import DeviceCredentials
from core.config import inventory as inventory_cfg


def _inventory_files() -> list[Path]:
    """Collect all .ini files to parse. Supports single-file and directory layouts."""
    p = Path(inventory_cfg.path)
    if p.is_file():
        return [p]
    return sorted(p.glob("*.ini"))


class _ParsedInventory:
    def __init__(self) -> None:
        self.by_host:     dict[str, DeviceCredentials]       = {}
        self.by_group:    dict[str, list[DeviceCredentials]] = {}
        self.host_groups: dict[str, set[str]]                = {}
        self.host_entries: list[dict]                        = []


class InventoryClient:

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._cache: _ParsedInventory | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_credentials(
        self,
        host: str,
        group: Optional[str] = None,
        platform_hint: Optional[str] = None,
    ) -> DeviceCredentials:
        inv = self._load()

        if host in inv.by_host:
            return inv.by_host[host]

        if group and group in inv.by_group and inv.by_group[group]:
            template = inv.by_group[group][0]
            return DeviceCredentials(
                host=host,
                username=template.username,
                password=template.password,
                enable_secret=template.enable_secret,
                platform=template.platform or platform_hint or "cisco_ios",
                port=template.port,
            )

        raise RuntimeError(
            f"No inventory entry found for host='{host}' group='{group}'. "
            f"Add it to {inventory_cfg.path}"
        )

    def get_group_hosts(self, group: str) -> list[DeviceCredentials]:
        inv = self._load()
        members = inv.by_group.get(group)
        if members is None:
            raise RuntimeError(
                f"Unknown inventory group '{group}'. "
                f"Known groups: {sorted(inv.by_group.keys())}"
            )
        return list(members)

    def reload(self) -> None:
        with self._lock:
            self._cache = None

    def list_hosts(self) -> list[str]:
        return [e["host"] for e in self._load().host_entries]

    def list_groups(self) -> list[str]:
        return sorted(self._load().by_group.keys())

    def list_host_entries(self) -> list[dict]:
        """Rich host dicts for the UI paginated /inventory/hosts endpoint."""
        return self._load().host_entries

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> _ParsedInventory:
        with self._lock:
            if self._cache is not None:
                return self._cache
            self._cache = self._parse_all()
            return self._cache

    def _parse_all(self) -> _ParsedInventory:
        inv = _ParsedInventory()
        for filepath in _inventory_files():
            self._parse_file(filepath, inv)

        # Build sorted host_entries
        for host in sorted(inv.by_host.keys()):
            creds = inv.by_host[host]
            inv.host_entries.append({
                "host":        host,
                "platform":    creds.platform or "unknown",
                "groups":      sorted(inv.host_groups.get(host, set())),
                "port":        creds.port,
                "credential_source": "inventory" if creds.password else "vault",
                "last_job":    None,
                "last_status": None,
            })

        return inv

    def _parse_file(self, filepath: Path, inv: _ParsedInventory) -> None:
        cfg = configparser.RawConfigParser(allow_no_value=True)
        try:
            cfg.read(str(filepath))
        except configparser.Error:
            return

        # Global defaults from [all:vars]
        global_vars: dict[str, str] = {}
        if cfg.has_section("all:vars"):
            global_vars = dict(cfg.items("all:vars"))

        for section in cfg.sections():
            if section == "all:vars" or section.endswith(":vars"):
                continue

            group_name = section
            group_members: list[DeviceCredentials] = []

            # Per-group var overrides
            group_vars = dict(global_vars)
            vars_section = f"{section}:vars"
            if cfg.has_section(vars_section):
                group_vars.update(dict(cfg.items(vars_section)))

            for raw_key, raw_val in cfg.items(section):
                # ─────────────────────────────────────────────────────────────
                # KEY PARSING FIX:
                #
                # configparser splits on the FIRST '=' in the line.
                # For:   192.168.1.10  platform=cisco_ios  username=admin
                # It produces:
                #   raw_key = "192.168.1.10  platform"
                #   raw_val = "cisco_ios  username=admin"
                #
                # We split raw_key on whitespace:
                #   parts[0]  = "192.168.1.10"        ← actual host
                #   parts[1:] = ["platform"]           ← start of first var name
                #
                # Then reassemble the full inline var string:
                #   "platform=cisco_ios  username=admin"
                # ─────────────────────────────────────────────────────────────
                parts = raw_key.split()
                host_key = parts[0]

                if len(parts) > 1:
                    # parts[1:] is the key-fragment before the first '='.
                    # raw_val is the value after the first '='.
                    # Together they form: "platform=cisco_ios  username=..."
                    first_var_fragment = "".join(parts[1:])
                    inline = f"{first_var_fragment}={raw_val}" if raw_val is not None else first_var_fragment
                else:
                    # Host-only line with no inline vars (uses group/global defaults only)
                    inline = raw_val or ""

                # Build final var map: global < group < inline
                var_map = dict(group_vars)
                for token in inline.split():
                    if "=" in token:
                        k, _, v = token.partition("=")
                        var_map[k.strip()] = v.strip()

                try:
                    port = int(var_map.get("port", 22))
                except ValueError:
                    port = 22

                creds = DeviceCredentials(
                    host=host_key,
                    username=var_map.get("username", ""),
                    password=var_map.get("password", ""),
                    enable_secret=var_map.get("enable_secret") or None,
                    platform=var_map.get("platform", "cisco_ios"),
                    port=port,
                )

                inv.by_host[host_key] = creds
                group_members.append(creds)

                if host_key not in inv.host_groups:
                    inv.host_groups[host_key] = set()
                inv.host_groups[host_key].add(group_name)

            inv.by_group[group_name] = group_members


# Module-level singleton
inventory_client = InventoryClient()
