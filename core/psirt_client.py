"""
core/psirt_client.py — Cisco PSIRT openVuln API client.

Authentication: OAuth2 Client Credentials flow.
  Token endpoint : https://id.cisco.com/oauth2/default/v1/token
  API base       : https://apix.cisco.com/security/advisories/v2
  Token TTL      : 3600 s  (cached in-memory; refreshed automatically)

Rate limits (enforced by Cisco):
  5 req/s · 30 req/min · 5,000 req/day

Advisory results are cached per (ostype, version) in the DB for
`psirt.cache_ttl` seconds (default 24 h) to stay within the daily cap
when many devices share the same software version.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import urllib.request
import urllib.parse
import urllib.error

from core.config import psirt as psirt_cfg
from core.logger import get_logger

log = get_logger("psirt_client")

# ── Constants ─────────────────────────────────────────────────────────────────

_TOKEN_URL = "https://id.cisco.com/oauth2/default/v1/token"
_API_BASE  = "https://apix.cisco.com/security/advisories/v2"

# Supported PSIRT ostypes — maps from netorch platform key
PLATFORM_TO_OSTYPE: dict[str, str] = {
    "cisco_ios":   "iosxe",
    "cisco_xe":    "iosxe",
    "cisco_xr":    "xr",
    "cisco_nxos":  "nxos",
    "cisco_wlc":   "wlc",    # AireOS — limited PSIRT coverage
}

SUPPORTED_OSTYPES = {"ios", "iosxe", "xr", "nxos", "asa", "ftd"}


class PSIRTAuthError(Exception):
    pass


class PSIRTRateLimitError(Exception):
    pass


class PSIRTAPIError(Exception):
    pass


class PSIRTNotConfiguredError(Exception):
    pass


# ── Token cache ───────────────────────────────────────────────────────────────

class _TokenCache:
    def __init__(self) -> None:
        self._lock      = threading.Lock()
        self._token:    Optional[str] = None
        self._expires:  float = 0.0          # monotonic time

    def get(self, client_id: str, client_secret: str) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires - 60:
                return self._token
            self._token   = self._fetch(client_id, client_secret)
            self._expires = time.monotonic() + 3600
            return self._token

    @staticmethod
    def _fetch(client_id: str, client_secret: str) -> str:
        data = urllib.parse.urlencode({
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        }).encode()
        req = urllib.request.Request(
            _TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise PSIRTAuthError(
                f"PSIRT token fetch failed: HTTP {e.code} — {e.read().decode(errors='replace')}"
            )
        except Exception as e:
            raise PSIRTAuthError(f"PSIRT token fetch error: {e}")
        token = body.get("access_token")
        if not token:
            raise PSIRTAuthError(f"No access_token in PSIRT response: {body}")
        log.info("psirt_token_refreshed")
        return token


_token_cache = _TokenCache()


# ── Rate limiter (token-bucket per second + per minute) ───────────────────────

class _RateLimiter:
    """Simple sliding-window rate limiter: 5/s and 30/min."""
    def __init__(self) -> None:
        self._lock     = threading.Lock()
        self._second_ts: list[float] = []
        self._minute_ts: list[float] = []

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._second_ts = [t for t in self._second_ts if now - t < 1.0]
            self._minute_ts = [t for t in self._minute_ts if now - t < 60.0]

            wait = 0.0
            if len(self._second_ts) >= 5:
                wait = max(wait, 1.0 - (now - self._second_ts[0]))
            if len(self._minute_ts) >= 30:
                wait = max(wait, 60.0 - (now - self._minute_ts[0]))

        if wait > 0:
            log.debug("psirt_rate_limit_wait", seconds=round(wait, 2))
            time.sleep(wait)

        with self._lock:
            now = time.monotonic()
            self._second_ts.append(now)
            self._minute_ts.append(now)


_rate_limiter = _RateLimiter()


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _api_get(path: str, token: str) -> dict:
    """GET {_API_BASE}{path} with auth. Returns parsed JSON dict."""
    _rate_limiter.acquire()
    url = f"{_API_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 429:
            raise PSIRTRateLimitError(f"PSIRT rate limit: {body}")
        if e.code in (401, 403):
            raise PSIRTAuthError(f"PSIRT auth error {e.code}: {body}")
        raise PSIRTAPIError(f"PSIRT HTTP {e.code}: {body}")
    except Exception as e:
        raise PSIRTAPIError(f"PSIRT request error: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

def get_advisories_by_version(ostype: str, version: str) -> list[dict]:
    """
    Query Cisco PSIRT API for all advisories affecting `ostype` at `version`.

    Returns list of advisory dicts with keys:
        advisory_id, cvss_score, severity, title, summary,
        cve_list (list[str]), first_fixed (list[str]), pub_url
    """
    if not psirt_cfg.enabled:
        raise PSIRTNotConfiguredError(
            "PSIRT client_id/client_secret not set in netorch.toml [psirt] section."
        )
    if ostype not in SUPPORTED_OSTYPES:
        raise PSIRTAPIError(
            f"ostype '{ostype}' not supported by PSIRT API. "
            f"Supported: {sorted(SUPPORTED_OSTYPES)}"
        )

    token = _token_cache.get(psirt_cfg.client_id, psirt_cfg.client_secret)
    path  = f"/OSType/{ostype}?version={urllib.parse.quote(version, safe='')}"

    log.info("psirt_query", ostype=ostype, version=version)
    try:
        data = _api_get(path, token)
    except PSIRTAuthError:
        # Token may have been revoked mid-session — force refresh once
        log.warning("psirt_auth_retry", reason="token may be stale")
        _token_cache._token = None
        token = _token_cache.get(psirt_cfg.client_id, psirt_cfg.client_secret)
        data  = _api_get(path, token)

    advisories = data.get("advisories", [])
    log.info("psirt_result", ostype=ostype, version=version, count=len(advisories))
    return [_parse_advisory(a) for a in advisories]


def _parse_advisory(raw: dict) -> dict:
    """Normalise a raw advisory dict from the PSIRT API."""
    # PSIRT returns cves as a comma-separated string or a list
    cves_raw = raw.get("cves") or raw.get("CVE") or []
    if isinstance(cves_raw, str):
        cves = [c.strip() for c in cves_raw.split(",") if c.strip()]
    else:
        cves = list(cves_raw)

    # firstFixed: list or comma-separated string
    ff_raw = raw.get("firstFixed") or []
    if isinstance(ff_raw, str):
        first_fixed = [v.strip() for v in ff_raw.split(",") if v.strip()]
    else:
        first_fixed = list(ff_raw)

    # cvssBaseScore may be a string ("8.1") or float
    try:
        cvss = float(raw.get("cvssBaseScore") or 0)
    except (TypeError, ValueError):
        cvss = 0.0

    return {
        "advisory_id": raw.get("advisoryId") or raw.get("advisory_id", ""),
        "cvss_score":  cvss,
        "severity":    raw.get("sir") or raw.get("severity", ""),
        "title":       raw.get("advisoryTitle") or raw.get("title", ""),
        "summary":     (raw.get("summary") or "")[:2000],
        "cve_list":    cves,
        "first_fixed": first_fixed,
        "pub_url":     raw.get("publicationUrl") or raw.get("pub_url", ""),
    }
