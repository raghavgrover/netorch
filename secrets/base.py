"""
secrets/base.py — Abstract base class for vault credential providers.

All vault backends (OpenBao, CyberArk, …) implement this interface.
The only method callers use is get_credentials(), which returns a
(username, password) tuple given a host and optional group name.

Implementations should:
  - Raise VaultError (or a subclass) on any lookup failure
  - Cache tokens / sessions where appropriate to avoid hammering the vault
  - Be thread-safe — workers call this from a ThreadPoolExecutor
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class VaultError(RuntimeError):
    """Raised when vault lookup fails for any reason."""


class VaultAuthError(VaultError):
    """Raised when the vault rejects our credentials / token."""


class VaultSecretNotFound(VaultError):
    """Raised when neither host nor group secret exists in the vault."""


class VaultProvider(ABC):
    """
    Abstract interface every vault backend must implement.

    get_credentials(host, group) → (username, password)

    The caller (secrets/provider.py) decides when to call this — only when
    the inventory INI has no password for the device.
    """

    @abstractmethod
    def get_credentials(
        self,
        host: str,
        group: str | None,
    ) -> tuple[str, str]:
        """
        Fetch (username, password) for the given host.

        Lookup order is up to the implementation, but the convention is:
          1. Look up by host (exact IP or hostname)
          2. Fall back to group name if host not found
          3. Raise VaultSecretNotFound if neither found

        Args:
            host:  IP address or hostname of the target device.
            group: Inventory group name, used as fallback key.

        Returns:
            (username, password) as strings.

        Raises:
            VaultAuthError:       Vault rejected our token / credentials.
            VaultSecretNotFound:  No secret found for host or group.
            VaultError:           Any other vault communication failure.
        """
