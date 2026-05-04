"""
secrets/provider.py — Vault factory and credential resolution logic.

This is the single entry point that ssh_worker.py calls instead of
calling inventory_client directly.

Resolution order for every device:
  1. Look up the device in inventory.ini (always — for platform, port, groups)
  2. If password is present in the INI  → return credentials as-is
  3. If password is blank/missing       → ask the configured vault provider
     for (username, password), merge into the inventory credentials, return

The vault provider is a singleton built once from netorch.toml at import time.
If vault type is "none" (default) and a device has no password, a clear
ConfigError is raised rather than silently connecting with an empty password.

Public API:
  resolve_credentials(host, group, platform_hint) → DeviceCredentials
"""
from __future__ import annotations

import threading
from dataclasses import replace

from drivers.base import DeviceCredentials
from secrets.base import VaultError, VaultProvider
from secrets.inventory import inventory_client
from core.logger import get_logger

log = get_logger("vault.provider")

# ── Provider singleton ────────────────────────────────────────────────────────

_provider_lock      = threading.Lock()
_provider_instance: VaultProvider | None = None
_provider_type:     str                  = "none"
_provider_built:    bool                 = False


def _build_provider() -> tuple[VaultProvider | None, str]:
    """
    Reads netorch.toml [vault] section and constructs the appropriate provider.
    Called once (lazily) and cached.
    """
    # Import config here to avoid circular imports at module level
    try:
        import toml
        from core.config import _find_config  # reuse same config-finding logic
        raw = toml.load(_find_config())
    except Exception as e:
        log.warning("vault_config_load_failed", error=str(e))
        return None, "none"

    vault_cfg = raw.get("vault", {})
    vault_type = vault_cfg.get("type", "none").lower()

    if vault_type == "none":
        log.debug("vault_disabled")
        return None, "none"

    if vault_type == "openbao":
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
        log.info("vault_provider_loaded", type="openbao",
                 url=ob.get("url", "http://127.0.0.1:8200"),
                 auth_method=ob.get("auth_method", "token"))
        return provider, "openbao"

    if vault_type == "cyberark":
        from secrets.cyberark import CyberArkProvider
        ca = vault_cfg.get("cyberark", {})
        provider = CyberArkProvider(
            url                 = ca.get("url", ""),
            app_id              = ca.get("app_id", ""),
            safe                = ca.get("safe", ""),
            object_name_pattern = ca.get("object_name_pattern", "{host}"),
            verify_ssl          = ca.get("verify_ssl", True),
            cert_path           = ca.get("cert_path", ""),
            cert_key_path       = ca.get("cert_key_path", ""),
        )
        log.info("vault_provider_loaded", type="cyberark",
                 url=ca.get("url", ""), safe=ca.get("safe", ""))
        return provider, "cyberark"

    log.warning("vault_unknown_type", vault_type=vault_type)
    return None, "none"


def _get_provider() -> tuple[VaultProvider | None, str]:
    """Returns the cached (provider, type) singleton, building it on first call."""
    global _provider_instance, _provider_type, _provider_built
    if _provider_built:
        return _provider_instance, _provider_type
    with _provider_lock:
        if not _provider_built:
            _provider_instance, _provider_type = _build_provider()
            _provider_built = True
    return _provider_instance, _provider_type


# ── Main entry point ──────────────────────────────────────────────────────────

def resolve_credentials(
    host: str,
    group: str | None = None,
    platform_hint: str | None = None,
) -> DeviceCredentials:
    """
    Full credential resolution for a single device.

    1. Fetches inventory entry (platform, port, groups, maybe username/password)
    2. If password is present in inventory → returns as-is
    3. If password is absent               → fetches from configured vault

    Args:
        host:          IP address or hostname of the target device.
        group:         Inventory group, used as both a credential template and
                       a vault fallback lookup key.
        platform_hint: Optional platform override from the job payload.

    Returns:
        Fully populated DeviceCredentials ready for the SSH driver.

    Raises:
        RuntimeError:  Inventory lookup failed (host and group both unknown).
        VaultError:    Vault is configured but lookup failed.
        ConfigError:   Password is missing and no vault is configured.
    """
    # Step 1: always resolve from inventory (platform, port, maybe creds)
    inv_creds: DeviceCredentials = inventory_client.get_credentials(
        host=host,
        group=group,
        platform_hint=platform_hint,
    )

    # Step 2: if inventory already has a password, use it directly
    if inv_creds.password:
        log.debug("credentials_from_inventory", host=host)
        return inv_creds

    # Step 3: password is blank — try vault
    provider, vault_type = _get_provider()

    if provider is None:
        raise RuntimeError(
            f"Device '{host}' has no password in the inventory file and no vault "
            "is configured. Either add the password to the inventory INI, or set "
            "[vault] type = \"openbao\" | \"cyberark\" in netorch.toml."
        )

    log.info("credentials_from_vault", host=host, group=group, vault=vault_type)

    try:
        vault_username, vault_password = provider.get_credentials(
            host=host,
            group=group,
        )
    except VaultError:
        raise  # let the worker surface the original vault error message

    # Merge: vault provides username + password; inventory provides everything else
    # (platform, port, enable_secret hint if set in INI).
    # Vault username takes precedence over the INI username when vault returns one.
    merged_username = vault_username or inv_creds.username

    return replace(
        inv_creds,
        username=merged_username,
        password=vault_password,
    )


def reload_provider() -> None:
    """
    Force the vault provider to be rebuilt on next call.
    Useful after netorch.toml is edited without a full restart.
    Called by POST /inventory/reload.
    """
    global _provider_built
    with _provider_lock:
        _provider_built = False
    log.info("vault_provider_cache_cleared")
