"""
secrets/cyberark.py — CyberArk Central Credential Provider (CCP) REST client.

Uses the CyberArk Application Identity Manager (AIM) Web Service API:
  GET https://{url}/AIMWebService/api/Accounts
      ?AppID={app_id}
      &Safe={safe}
      &Object={object_name}

The object name is built from a configurable pattern. Supported tokens:
  {host}   → device IP or hostname
  {group}  → inventory group name

Lookup order:
  1. Build object name from host  → call CCP
  2. If not found (404), build object name from group → call CCP
  3. If still not found → raise VaultSecretNotFound

Authentication options (configure in netorch.toml):
  - Certificate (mutual TLS): set cert_path + cert_key_path
  - No client cert: omit cert fields (CyberArk verifies by IP/app registration)

CyberArk CCP returns JSON; the relevant fields are:
  UserName → username
  Content  → password

Example netorch.toml:

  [vault]
  type = "cyberark"

  [vault.cyberark]
  url                 = "https://cyberark.company.com"
  app_id              = "NetOrch"
  safe                = "Network-Devices"
  object_name_pattern = "{host}"
  verify_ssl          = true
  # cert_path         = "/opt/netorch/certs/client.pem"
  # cert_key_path     = "/opt/netorch/certs/client.key"

Storing secrets in CyberArk:
  Each device account should be stored in the CyberArk Safe with:
    Object name  = the IP or hostname of the device (matching object_name_pattern)
    UserName     = SSH username (e.g. "netaudit")
    Password     = SSH password (stored as the account password)

  For group fallback: create an account with Object name = group name
  (e.g. "cisco_ios_xr") containing the shared group credentials.
"""
from __future__ import annotations

from typing import Any

import httpx

from secrets.base import VaultAuthError, VaultError, VaultProvider, VaultSecretNotFound
from core.logger import get_logger

log = get_logger("vault.cyberark")


class CyberArkProvider(VaultProvider):
    """
    Fetches credentials from CyberArk via the CCP (AIMWebService) REST API.
    Stateless — no token caching needed; CyberArk handles session auth
    via the registered AppID and optional client certificate.
    """

    _API_PATH = "/AIMWebService/api/Accounts"

    def __init__(
        self,
        url: str,
        app_id: str,
        safe: str,
        object_name_pattern: str = "{host}",
        verify_ssl: bool = True,
        cert_path: str = "",
        cert_key_path: str = "",
    ) -> None:
        self._url         = url.rstrip("/")
        self._app_id      = app_id
        self._safe        = safe
        self._pattern     = object_name_pattern
        self._verify_ssl  = verify_ssl

        # Build httpx SSL cert tuple if configured
        if cert_path and cert_key_path:
            self._cert: tuple[str, str] | None = (cert_path, cert_key_path)
        elif cert_path:
            self._cert = (cert_path,)   # type: ignore[assignment]
        else:
            self._cert = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_credentials(
        self,
        host: str,
        group: str | None,
    ) -> tuple[str, str]:
        """
        Returns (username, password).
        Tries host-based object name first, falls back to group.
        """
        # 1. Try by host
        try:
            return self._fetch_account(self._make_object_name(host))
        except VaultSecretNotFound:
            pass

        # 2. Try by group
        if group:
            try:
                return self._fetch_account(self._make_object_name(group))
            except VaultSecretNotFound:
                pass

        raise VaultSecretNotFound(
            f"CyberArk: no account found in Safe='{self._safe}' "
            f"for host='{host}' or group='{group}'. "
            f"Object name pattern: '{self._pattern}'"
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _make_object_name(self, value: str) -> str:
        """
        Renders the object name pattern.
        {host} and {group} are both filled from 'value' since the caller
        passes whichever identifier it's looking up at the moment.
        """
        return self._pattern.format(host=value, group=value)

    def _fetch_account(self, object_name: str) -> tuple[str, str]:
        """
        Calls CCP for a single account by object name.
        Returns (username, password).
        Raises VaultSecretNotFound if the object doesn't exist (404).
        Raises VaultError on other failures.
        """
        url    = self._url + self._API_PATH
        params = {
            "AppID":  self._app_id,
            "Safe":   self._safe,
            "Object": object_name,
        }

        kwargs: dict[str, Any] = {
            "params":  params,
            "verify":  self._verify_ssl,
            "timeout": 10,
        }
        if self._cert:
            kwargs["cert"] = self._cert

        try:
            resp = httpx.get(url, **kwargs)
        except httpx.RequestError as e:
            raise VaultError(
                f"CyberArk CCP connection error ({url}): {e}"
            ) from e

        if resp.status_code == 404:
            raise VaultSecretNotFound(
                f"CyberArk: object '{object_name}' not found in Safe '{self._safe}'"
            )

        if resp.status_code in (401, 403):
            raise VaultAuthError(
                f"CyberArk: access denied (HTTP {resp.status_code}) for "
                f"AppID='{self._app_id}', Safe='{self._safe}', Object='{object_name}'. "
                "Check AppID registration and Safe permissions."
            )

        if not resp.is_success:
            # CyberArk returns error details in JSON when possible
            detail = _extract_cyberark_error(resp)
            raise VaultError(
                f"CyberArk: unexpected status {resp.status_code} for "
                f"object '{object_name}': {detail}"
            )

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as e:
            raise VaultError(
                f"CyberArk: non-JSON response for object '{object_name}': {e}"
            ) from e

        # CyberArk returns UserName and Content (password)
        username = data.get("UserName", "")
        password = data.get("Content", "")

        if not password:
            raise VaultError(
                f"CyberArk: account '{object_name}' in Safe '{self._safe}' "
                "returned an empty password ('Content' field is missing or blank). "
                "Verify the account has a password set in CyberArk."
            )

        log.debug(
            "cyberark_secret_fetched",
            safe=self._safe,
            object_name=object_name,
            username=username,
        )
        return username, password


def _extract_cyberark_error(resp: httpx.Response) -> str:
    """
    Best-effort extraction of the human-readable error from a CyberArk
    error response. CyberArk often wraps errors as:
      {"ErrorCode": "APPAP004E", "ErrorMsg": "..."}
    """
    try:
        body = resp.json()
        msg  = body.get("ErrorMsg") or body.get("Details") or str(body)
        code = body.get("ErrorCode", "")
        return f"{code}: {msg}".strip(": ") if code else msg
    except ValueError:
        return resp.text[:300]
