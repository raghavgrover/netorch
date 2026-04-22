"""
tests/test_vault.py — Vault credential resolution tests.

Tests cover:
  1. Plain-text password in INI → vault never called
  2. Blank password in INI + OpenBao vault → credentials merged correctly
  3. Blank password in INI + CyberArk vault → credentials merged correctly
  4. Blank password + no vault configured → clear error raised
  5. Vault lookup fails → VaultError propagated
  6. OpenBao: host found directly, no group fallback needed
  7. OpenBao: host not found, group fallback succeeds
  8. OpenBao: AppRole login and token caching
  9. CyberArk: host found directly
 10. CyberArk: host not found, group fallback succeeds
 11. CyberArk: empty Content field raises VaultError
 12. reload_provider() clears singleton cache
"""
from __future__ import annotations

import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from drivers.base import DeviceCredentials
from secrets.base import VaultAuthError, VaultError, VaultSecretNotFound


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_creds(host="10.0.0.1", password="", username="netuser",
                platform="cisco_ios", port=22, enable_secret=None):
    return DeviceCredentials(
        host=host,
        username=username,
        password=password,
        enable_secret=enable_secret,
        platform=platform,
        port=port,
    )


# ── secrets/provider.py tests ─────────────────────────────────────────────────

class TestResolveCredentials:
    """Tests for secrets.provider.resolve_credentials()"""

    def _reset_provider(self):
        """Force provider singleton to rebuild on next call."""
        import secrets.provider as p
        with p._provider_lock:
            p._provider_built = False
            p._provider_instance = None
            p._provider_type = "none"

    def test_plain_text_password_skips_vault(self):
        """If inventory has a password, vault is never touched."""
        creds_with_pw = _make_creds(password="mypassword")

        with patch("secrets.provider.inventory_client") as mock_inv, \
             patch("secrets.provider._get_provider") as mock_prov:
            mock_inv.get_credentials.return_value = creds_with_pw

            from secrets.provider import resolve_credentials
            result = resolve_credentials("10.0.0.1", group="g1")

        mock_prov.assert_not_called()
        assert result.password == "mypassword"

    def test_blank_password_calls_vault(self):
        """If inventory has no password, vault is called."""
        self._reset_provider()
        creds_no_pw = _make_creds(password="", username="netuser")
        mock_vault = MagicMock()
        mock_vault.get_credentials.return_value = ("vaultuser", "vaultpass")

        with patch("secrets.provider.inventory_client") as mock_inv, \
             patch("secrets.provider._get_provider", return_value=(mock_vault, "openbao")):
            mock_inv.get_credentials.return_value = creds_no_pw

            from secrets.provider import resolve_credentials
            result = resolve_credentials("10.0.0.1", group="g1")

        mock_vault.get_credentials.assert_called_once_with(host="10.0.0.1", group="g1")
        assert result.password == "vaultpass"
        assert result.username == "vaultuser"

    def test_vault_username_overrides_inventory_username(self):
        """Vault username takes precedence over INI username when vault is used."""
        self._reset_provider()
        creds_no_pw = _make_creds(password="", username="ini_user")
        mock_vault = MagicMock()
        mock_vault.get_credentials.return_value = ("vault_user", "vault_pw")

        with patch("secrets.provider.inventory_client") as mock_inv, \
             patch("secrets.provider._get_provider", return_value=(mock_vault, "openbao")):
            mock_inv.get_credentials.return_value = creds_no_pw

            from secrets.provider import resolve_credentials
            result = resolve_credentials("10.0.0.1")

        assert result.username == "vault_user"

    def test_vault_username_empty_falls_back_to_inventory_username(self):
        """If vault returns empty username, the INI username is kept."""
        self._reset_provider()
        creds_no_pw = _make_creds(password="", username="ini_user")
        mock_vault = MagicMock()
        mock_vault.get_credentials.return_value = ("", "vault_pw")

        with patch("secrets.provider.inventory_client") as mock_inv, \
             patch("secrets.provider._get_provider", return_value=(mock_vault, "openbao")):
            mock_inv.get_credentials.return_value = creds_no_pw

            from secrets.provider import resolve_credentials
            result = resolve_credentials("10.0.0.1")

        assert result.username == "ini_user"
        assert result.password == "vault_pw"

    def test_non_password_fields_preserved_from_inventory(self):
        """Platform, port, enable_secret from INI are preserved after vault merge."""
        self._reset_provider()
        creds_no_pw = _make_creds(
            password="", platform="cisco_xr", port=8022, enable_secret="en"
        )
        mock_vault = MagicMock()
        mock_vault.get_credentials.return_value = ("u", "p")

        with patch("secrets.provider.inventory_client") as mock_inv, \
             patch("secrets.provider._get_provider", return_value=(mock_vault, "openbao")):
            mock_inv.get_credentials.return_value = creds_no_pw

            from secrets.provider import resolve_credentials
            result = resolve_credentials("10.0.0.1")

        assert result.platform == "cisco_xr"
        assert result.port == 8022
        assert result.enable_secret == "en"

    def test_no_vault_configured_blank_password_raises(self):
        """Blank password + vault type = 'none' → RuntimeError with clear message."""
        self._reset_provider()
        creds_no_pw = _make_creds(password="")

        with patch("secrets.provider.inventory_client") as mock_inv, \
             patch("secrets.provider._get_provider", return_value=(None, "none")):
            mock_inv.get_credentials.return_value = creds_no_pw

            from secrets.provider import resolve_credentials
            with pytest.raises(RuntimeError, match="no vault is configured"):
                resolve_credentials("10.0.0.1")

    def test_vault_error_propagates(self):
        """VaultError from the provider bubbles up unchanged."""
        self._reset_provider()
        creds_no_pw = _make_creds(password="")
        mock_vault = MagicMock()
        mock_vault.get_credentials.side_effect = VaultError("connection refused")

        with patch("secrets.provider.inventory_client") as mock_inv, \
             patch("secrets.provider._get_provider", return_value=(mock_vault, "openbao")):
            mock_inv.get_credentials.return_value = creds_no_pw

            from secrets.provider import resolve_credentials
            with pytest.raises(VaultError, match="connection refused"):
                resolve_credentials("10.0.0.1")

    def test_reload_provider_clears_cache(self):
        """reload_provider() forces the singleton to rebuild next call."""
        import secrets.provider as p

        # Force it built
        p._provider_built = True
        p._provider_type = "openbao"

        from secrets.provider import reload_provider
        reload_provider()

        assert p._provider_built is False


# ── secrets/openbao.py tests ──────────────────────────────────────────────────

class TestOpenBaoProvider:
    """Unit tests for OpenBaoProvider — HTTP calls are mocked."""

    def _make_provider(self, **kwargs):
        from secrets.openbao import OpenBaoProvider
        defaults = dict(
            url="http://vault:8200",
            auth_method="token",
            token="test-token",
            mount="secret",
            prefix="netorch",
        )
        defaults.update(kwargs)
        return OpenBaoProvider(**defaults)

    def _mock_response(self, status_code=200, json_body=None, text=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.is_success = (200 <= status_code < 300)
        resp.json.return_value = json_body or {}
        resp.text = text
        return resp

    def test_get_credentials_by_host(self):
        """Happy path: host secret found directly."""
        provider = self._make_provider()
        secret_resp = self._mock_response(json_body={
            "data": {"data": {"username": "admin", "password": "s3cr3t"}}
        })

        with patch("httpx.get", return_value=secret_resp):
            username, password = provider.get_credentials("10.0.0.1", None)

        assert username == "admin"
        assert password == "s3cr3t"

    def test_get_credentials_host_404_falls_back_to_group(self):
        """Host secret 404 → group secret used as fallback."""
        provider = self._make_provider()
        not_found = self._mock_response(status_code=404)
        group_resp = self._mock_response(json_body={
            "data": {"data": {"username": "grp_user", "password": "grp_pass"}}
        })

        with patch("httpx.get", side_effect=[not_found, group_resp]):
            username, password = provider.get_credentials("10.0.0.1", "core_switches")

        assert username == "grp_user"
        assert password == "grp_pass"

    def test_both_host_and_group_404_raises_not_found(self):
        """If neither host nor group secret exists, VaultSecretNotFound raised."""
        provider = self._make_provider()
        not_found = self._mock_response(status_code=404)

        with patch("httpx.get", return_value=not_found):
            with pytest.raises(VaultSecretNotFound, match="no secret found"):
                provider.get_credentials("10.0.0.1", "some_group")

    def test_403_raises_vault_auth_error(self):
        """HTTP 403 → VaultAuthError."""
        provider = self._make_provider()
        forbidden = self._mock_response(status_code=403)

        with patch("httpx.get", return_value=forbidden):
            with pytest.raises(VaultAuthError, match="permission denied"):
                provider.get_credentials("10.0.0.1", None)

    def test_missing_password_field_raises_vault_error(self):
        """Secret exists but has no 'password' key → VaultError."""
        provider = self._make_provider()
        resp = self._mock_response(json_body={
            "data": {"data": {"username": "admin"}}  # no password
        })

        with patch("httpx.get", return_value=resp):
            with pytest.raises(VaultError, match="missing the 'password' field"):
                provider.get_credentials("10.0.0.1", None)

    def test_static_token_auth_no_network_call(self):
        """Static token is returned immediately without any HTTP call."""
        provider = self._make_provider(auth_method="token", token="mytoken")
        assert provider._get_token() == "mytoken"

    def test_missing_static_token_raises(self):
        """auth_method=token with empty token → VaultAuthError."""
        provider = self._make_provider(auth_method="token", token="")
        with pytest.raises(VaultAuthError, match="no token is configured"):
            provider._get_token()

    def test_approle_login_success(self):
        """AppRole login returns token and sets expiry."""
        import time
        provider = self._make_provider(
            auth_method="approle",
            role_id="role-123",
            secret_id="secret-456",
            token="",
        )
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.is_success = True
        login_resp.json.return_value = {
            "auth": {"client_token": "new-token", "lease_duration": 3600}
        }

        before = time.monotonic()
        with patch("httpx.post", return_value=login_resp):
            token, expiry = provider._approle_login()

        assert token == "new-token"
        assert expiry > before + 3000  # roughly 3600 - 60

    def test_approle_login_rejected_raises_auth_error(self):
        """AppRole login HTTP 400 → VaultAuthError."""
        provider = self._make_provider(
            auth_method="approle", role_id="r", secret_id="s", token=""
        )
        bad_resp = MagicMock()
        bad_resp.status_code = 400
        bad_resp.is_success = False
        bad_resp.text = "invalid role"

        with patch("httpx.post", return_value=bad_resp):
            with pytest.raises(VaultAuthError, match="rejected"):
                provider._approle_login()

    def test_approle_token_cached_avoids_repeated_login(self):
        """Once a valid AppRole token is cached, subsequent calls don't re-login."""
        import time
        provider = self._make_provider(
            auth_method="approle", role_id="r", secret_id="s", token=""
        )
        # Pre-populate cache with a non-expired token
        provider._token = "cached-token"
        provider._token_expiry = time.monotonic() + 3600

        with patch("httpx.post") as mock_post:
            token = provider._get_token()
            mock_post.assert_not_called()

        assert token == "cached-token"

    def test_connection_error_raises_vault_error(self):
        """httpx.RequestError → VaultError."""
        import httpx as _httpx
        provider = self._make_provider()

        with patch("httpx.get", side_effect=_httpx.ConnectError("refused")):
            with pytest.raises(VaultError, match="connection error"):
                provider.get_credentials("10.0.0.1", None)


# ── secrets/cyberark.py tests ─────────────────────────────────────────────────

class TestCyberArkProvider:
    """Unit tests for CyberArkProvider — HTTP calls are mocked."""

    def _make_provider(self, **kwargs):
        from secrets.cyberark import CyberArkProvider
        defaults = dict(
            url="https://cyberark.corp.com",
            app_id="NetOrch",
            safe="Network-Devices",
            object_name_pattern="{host}",
            verify_ssl=False,
        )
        defaults.update(kwargs)
        return CyberArkProvider(**defaults)

    def _mock_response(self, status_code=200, json_body=None, text=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.is_success = (200 <= status_code < 300)
        resp.json.return_value = json_body or {}
        resp.text = text
        return resp

    def test_get_credentials_by_host(self):
        """Happy path: account found by host."""
        provider = self._make_provider()
        resp = self._mock_response(json_body={
            "UserName": "netadmin",
            "Content":  "cark_pass",
        })

        with patch("httpx.get", return_value=resp):
            username, password = provider.get_credentials("10.0.0.2", None)

        assert username == "netadmin"
        assert password == "cark_pass"

    def test_get_credentials_host_404_falls_back_to_group(self):
        """Host account not found → group account used."""
        provider = self._make_provider()
        not_found = self._mock_response(status_code=404)
        group_resp = self._mock_response(json_body={
            "UserName": "grp_admin",
            "Content":  "grp_pass",
        })

        with patch("httpx.get", side_effect=[not_found, group_resp]):
            username, password = provider.get_credentials("10.0.0.2", "linux_servers")

        assert username == "grp_admin"
        assert password == "grp_pass"

    def test_both_404_raises_not_found(self):
        """Neither host nor group found → VaultSecretNotFound."""
        provider = self._make_provider()
        not_found = self._mock_response(status_code=404)

        with patch("httpx.get", return_value=not_found):
            with pytest.raises(VaultSecretNotFound, match="no account found"):
                provider.get_credentials("10.0.0.2", "linux_servers")

    def test_403_raises_vault_auth_error(self):
        """HTTP 403 → VaultAuthError with useful message."""
        provider = self._make_provider()
        resp = self._mock_response(status_code=403)

        with patch("httpx.get", return_value=resp):
            with pytest.raises(VaultAuthError, match="access denied"):
                provider.get_credentials("10.0.0.2", None)

    def test_empty_content_field_raises_vault_error(self):
        """CyberArk returns account but password (Content) is empty → VaultError."""
        provider = self._make_provider()
        resp = self._mock_response(json_body={
            "UserName": "admin",
            "Content":  "",   # empty password
        })

        with patch("httpx.get", return_value=resp):
            with pytest.raises(VaultError, match="empty password"):
                provider.get_credentials("10.0.0.2", None)

    def test_object_name_pattern_with_host(self):
        """object_name_pattern={host} produces correct object name."""
        provider = self._make_provider(object_name_pattern="netorch-{host}")
        resp = self._mock_response(json_body={"UserName": "u", "Content": "p"})

        with patch("httpx.get", return_value=resp) as mock_get:
            provider.get_credentials("192.168.1.1", None)
            call_kwargs = mock_get.call_args
            params = call_kwargs[1]["params"]
            assert params["Object"] == "netorch-192.168.1.1"

    def test_cyberark_error_json_extracted(self):
        """CyberArk error response JSON is extracted into the exception message."""
        from secrets.cyberark import _extract_cyberark_error
        resp = MagicMock()
        resp.json.return_value = {
            "ErrorCode": "APPAP004E",
            "ErrorMsg": "Application not found",
        }
        result = _extract_cyberark_error(resp)
        assert "APPAP004E" in result
        assert "Application not found" in result

    def test_connection_error_raises_vault_error(self):
        """httpx.RequestError → VaultError."""
        import httpx as _httpx
        provider = self._make_provider()

        with patch("httpx.get", side_effect=_httpx.ConnectError("timed out")):
            with pytest.raises(VaultError, match="connection error"):
                provider.get_credentials("10.0.0.2", None)

    def test_cert_path_passed_to_httpx(self):
        """cert_path + cert_key_path are forwarded to httpx as cert tuple."""
        provider = self._make_provider(
            cert_path="/certs/client.pem",
            cert_key_path="/certs/client.key",
        )
        assert provider._cert == ("/certs/client.pem", "/certs/client.key")

        resp = self._mock_response(json_body={"UserName": "u", "Content": "p"})
        with patch("httpx.get", return_value=resp) as mock_get:
            provider.get_credentials("10.0.0.2", None)
            call_kwargs = mock_get.call_args[1]
            assert call_kwargs["cert"] == ("/certs/client.pem", "/certs/client.key")


# ── Thread-safety smoke test ──────────────────────────────────────────────────

class TestThreadSafety:
    """Verify that concurrent workers don't cause races in vault providers."""

    def test_openbao_concurrent_token_fetch(self):
        """Multiple threads calling _get_token() simultaneously is safe."""
        from secrets.openbao import OpenBaoProvider
        provider = OpenBaoProvider(
            url="http://vault:8200",
            auth_method="token",
            token="static-token",
        )
        results = []
        errors = []

        def fetch():
            try:
                results.append(provider._get_token())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fetch) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert all(r == "static-token" for r in results)
        assert len(results) == 20
