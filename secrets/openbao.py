"""
secrets/openbao.py — OpenBao / HashiCorp Vault KV v2 credential provider.

Supports two authentication methods:
  - token    (default) : static token set in netorch.toml
  - approle           : Role ID + Secret ID; auto-renews on expiry

Secret path convention (KV v2):
  Devices : {mount}/data/{prefix}/devices/{host}
  Groups  : {mount}/data/{prefix}/groups/{group}   ← fallback

Each secret must have at minimum:
  username = "netaudit"
  password = "s3cr3t"

Optional fields:
  enable_secret = "en4bl3"   (used by Cisco IOS enable mode)

Example netorch.toml:

  [vault]
  type = "openbao"

  [vault.openbao]
  url         = "http://127.0.0.1:8200"
  auth_method = "token"
  token       = "hvs.XXXXXXXXXXXXXXXX"
  mount       = "secret"
  prefix      = "netorch"

  # AppRole example:
  # auth_method = "approle"
  # role_id     = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  # secret_id   = "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from secrets.base import VaultAuthError, VaultError, VaultProvider, VaultSecretNotFound
from core.logger import get_logger

log = get_logger("vault.openbao")


class OpenBaoProvider(VaultProvider):
    """
    Fetches credentials from a local OpenBao / HashiCorp Vault KV v2 store.

    Thread-safe: token refresh uses a lock so concurrent workers don't
    trigger duplicate AppRole logins.
    """

    def __init__(
        self,
        url: str,
        auth_method: str = "token",
        token: str = "",
        role_id: str = "",
        secret_id: str = "",
        mount: str = "secret",
        prefix: str = "netorch",
        verify_ssl: bool = True,
    ) -> None:
        self._url         = url.rstrip("/")
        self._auth_method = auth_method
        self._static_token= token
        self._role_id     = role_id
        self._secret_id   = secret_id
        self._mount       = mount
        self._prefix      = prefix
        self._verify_ssl  = verify_ssl

        self._lock         = threading.Lock()
        self._token:  str  = token if auth_method == "token" else ""
        self._token_expiry: float = float("inf") if auth_method == "token" else 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def get_credentials(
        self,
        host: str,
        group: str | None,
    ) -> tuple[str, str]:
        """
        Returns (username, password).
        Tries host path first, falls back to group path.
        """
        # 1. Try by host
        try:
            return self._fetch_secret(f"devices/{host}")
        except VaultSecretNotFound:
            pass

        # 2. Try by group
        if group:
            try:
                return self._fetch_secret(f"groups/{group}")
            except VaultSecretNotFound:
                pass

        raise VaultSecretNotFound(
            f"OpenBao: no secret found for host='{host}' or group='{group}'. "
            f"Expected paths: {self._mount}/data/{self._prefix}/devices/{host} "
            f"or {self._mount}/data/{self._prefix}/groups/{group}"
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_secret(self, relative_path: str) -> tuple[str, str]:
        """
        Fetches KV v2 secret at {mount}/data/{prefix}/{relative_path}.
        Returns (username, password).
        Raises VaultSecretNotFound if the path does not exist (404).
        Raises VaultError on other HTTP errors.
        """
        token  = self._get_token()
        path   = f"{self._mount}/data/{self._prefix}/{relative_path}"
        url    = f"{self._url}/v1/{path}"
        headers = {"X-Vault-Token": token}

        try:
            resp = httpx.get(url, headers=headers, verify=self._verify_ssl, timeout=10)
        except httpx.RequestError as e:
            raise VaultError(f"OpenBao connection error ({url}): {e}") from e

        if resp.status_code == 404:
            raise VaultSecretNotFound(f"OpenBao: secret not found at '{path}'")

        if resp.status_code == 403:
            raise VaultAuthError(
                f"OpenBao: permission denied for path '{path}'. "
                "Check token policy or AppRole capabilities."
            )

        if not resp.is_success:
            raise VaultError(
                f"OpenBao: unexpected status {resp.status_code} for path '{path}': "
                f"{resp.text[:200]}"
            )

        try:
            data: dict[str, Any] = resp.json()["data"]["data"]
        except (KeyError, ValueError) as e:
            raise VaultError(
                f"OpenBao: unexpected response shape for '{path}': {e}"
            ) from e

        username = data.get("username", "")
        password = data.get("password", "")

        if not password:
            raise VaultError(
                f"OpenBao: secret at '{path}' is missing the 'password' field."
            )

        log.debug("openbao_secret_fetched", path=path, username=username)
        return username, password

    def _get_token(self) -> str:
        """
        Returns a valid Vault token, refreshing via AppRole if necessary.
        Thread-safe.
        """
        if self._auth_method == "token":
            if not self._static_token:
                raise VaultAuthError(
                    "OpenBao: auth_method is 'token' but no token is configured "
                    "in [vault.openbao] token = ..."
                )
            return self._static_token

        # AppRole — check expiry under lock
        with self._lock:
            if time.monotonic() < self._token_expiry:
                return self._token

            # Token expired or never set — re-authenticate
            log.info("openbao_approle_login", role_id=self._role_id[:8] + "…")
            self._token, self._token_expiry = self._approle_login()
            return self._token

    def _approle_login(self) -> tuple[str, float]:
        """
        Authenticates with AppRole and returns (token, expiry_monotonic).
        """
        if not self._role_id or not self._secret_id:
            raise VaultAuthError(
                "OpenBao: auth_method is 'approle' but role_id or secret_id "
                "is not configured in [vault.openbao]."
            )

        url = f"{self._url}/v1/auth/approle/login"
        payload = {"role_id": self._role_id, "secret_id": self._secret_id}

        try:
            resp = httpx.post(url, json=payload, verify=self._verify_ssl, timeout=10)
        except httpx.RequestError as e:
            raise VaultError(f"OpenBao AppRole login request failed: {e}") from e

        if resp.status_code == 400:
            raise VaultAuthError(
                f"OpenBao AppRole login rejected: {resp.text[:200]}"
            )

        if not resp.is_success:
            raise VaultError(
                f"OpenBao AppRole login failed ({resp.status_code}): {resp.text[:200]}"
            )

        try:
            auth  = resp.json()["auth"]
            token = auth["client_token"]
            ttl   = int(auth.get("lease_duration", 3600))
        except (KeyError, ValueError) as e:
            raise VaultError(f"OpenBao AppRole login: unexpected response: {e}") from e

        # Expire 60 s before actual TTL to avoid races
        expiry = time.monotonic() + max(ttl - 60, 60)
        log.info("openbao_approle_login_ok", ttl_seconds=ttl)
        return token, expiry
