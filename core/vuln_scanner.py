"""
core/vuln_scanner.py — Vulnerability scan orchestrator.

Flow per scan:
  1. Create vuln_scan record, mark running
  2. Fan out SSH workers (ThreadPoolExecutor) — one per device
  3. Per device: SSH → 'show version' → parse ostype + version
  4. For each (ostype, version) pair: check DB advisory cache;
     if stale/missing, call psirt_client.get_advisories_by_version()
  5. Store device facts, advisories, and findings in DB
  6. Mark scan complete/partial_failure/failed
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from core.config import psirt as psirt_cfg, executor as exec_cfg
from core.db import db
from core.logger import get_logger
from core.psirt_client import (
    PLATFORM_TO_OSTYPE, SUPPORTED_OSTYPES,
    PSIRTNotConfiguredError, PSIRTAPIError, PSIRTRateLimitError,
    get_advisories_by_version,
)
from drivers import get_driver
from secrets.provider import resolve_credentials

log = get_logger("vuln_scanner")

# ── Version parsers per PSIRT ostype ─────────────────────────────────────────
# Each pattern extracts the canonical version string from 'show version' output.
# Patterns are tried in order; first match wins.

_VERSION_PATTERNS: dict[str, list[str]] = {
    "iosxe": [
        r"Cisco IOS XE Software,\s+Version\s+(\S+),",
        r"Version\s+(\d+\.\d+\.\d+\S*),",
        r"Version\s+(\S+),",
    ],
    "ios": [
        r"Cisco IOS Software.*Version\s+(\S+),",
        r"Version\s+(\S+),",
    ],
    "xr": [
        r"Cisco IOS XR Software,\s+Version\s+(\S+)\b",
        r"Version\s+(\d+\.\d+\.\d+\S*)\b",
    ],
    "nxos": [
        r"NXOS:\s+version\s+(\S+)",
        r"system:\s+version\s+(\S+)",
        r"version\s+(\d+\.\d+\S*)",
    ],
}


def _parse_version(ostype: str, raw_output: str) -> Optional[str]:
    """Extract version string for the given ostype from 'show version' output."""
    patterns = _VERSION_PATTERNS.get(ostype, [])
    for pat in patterns:
        m = re.search(pat, raw_output, re.IGNORECASE)
        if m:
            return m.group(1).rstrip(",;")
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Per-device worker ─────────────────────────────────────────────────────────

def _scan_device(scan_id: str, host: str, platform: str,
                 cancel_event: threading.Event) -> dict:
    """
    Collect 'show version' from one device, return a result dict.
    Never raises — all errors are captured in the returned dict.
    """
    result = {
        "host":       host,
        "platform":   platform,
        "ostype":     None,
        "version":    None,
        "raw_output": None,
        "status":     "error",
        "error":      None,
    }
    if cancel_event.is_set():
        result["error"] = "Scan cancelled"
        return result

    ostype = PLATFORM_TO_OSTYPE.get(platform)
    if not ostype:
        result["error"] = f"Platform '{platform}' not supported for vulnerability scanning."
        return result
    if ostype not in SUPPORTED_OSTYPES:
        result["error"] = f"ostype '{ostype}' not covered by PSIRT API."
        return result

    result["ostype"] = ostype

    # Fetch device credentials
    try:
        creds = resolve_credentials(host)
    except Exception as e:
        result["error"] = f"Credential lookup failed: {e}"
        return result

    # SSH and run 'show version'
    driver = get_driver(creds)
    try:
        driver.connect()
        raw = driver.run_command("show version")
        driver.disconnect()
    except Exception as e:
        result["error"] = f"SSH error: {e}"
        try:
            driver.disconnect()
        except Exception:
            pass
        return result

    result["raw_output"] = raw
    version = _parse_version(ostype, raw)
    if not version:
        result["error"] = (
            f"Could not parse version from 'show version' output "
            f"(ostype={ostype}). First 200 chars: {raw[:200]}"
        )
        return result

    result["version"] = version
    result["status"]  = "collected"
    return result


# ── Advisory lookup (cached) ──────────────────────────────────────────────────

# In-process cache of (ostype, version) → list[advisory] to avoid redundant
# PSIRT calls within the same scan run.
_scan_advisory_cache: dict[tuple, list] = {}
_advisory_cache_lock = threading.Lock()


def _lookup_advisories(ostype: str, version: str) -> tuple[list[dict], Optional[str]]:
    """
    Return (advisories, error). Checks DB cache first, then PSIRT API.
    Thread-safe: only one thread fetches a given (ostype, version) pair.
    """
    key = (ostype, version)
    with _advisory_cache_lock:
        if key in _scan_advisory_cache:
            return _scan_advisory_cache[key], None

    # Check persistent DB cache
    cached = db.get_cached_advisories(ostype, version, psirt_cfg.cache_ttl)
    if cached:
        advisories = [
            {
                "advisory_id": r["advisory_id"],
                "cvss_score":  r["cvss_score"],
                "severity":    r["severity"],
                "title":       r["title"],
                "summary":     r["summary"],
                "cve_list":    json.loads(r["cve_list"] or "[]"),
                "first_fixed": json.loads(r["first_fixed"] or "[]"),
                "pub_url":     r["pub_url"],
            }
            for r in cached
        ]
        with _advisory_cache_lock:
            _scan_advisory_cache[key] = advisories
        return advisories, None

    # Fetch from PSIRT API
    try:
        advisories = get_advisories_by_version(ostype, version)
    except PSIRTNotConfiguredError as e:
        return [], str(e)
    except PSIRTRateLimitError as e:
        return [], f"Rate limit hit: {e}"
    except PSIRTAPIError as e:
        return [], f"PSIRT API error: {e}"
    except Exception as e:
        return [], f"Unexpected PSIRT error: {e}"

    # Persist to DB advisory cache
    fetched_at = _now()
    for adv in advisories:
        try:
            db.upsert_vuln_advisory(
                advisory_id=adv["advisory_id"],
                ostype=ostype,
                version=version,
                cvss_score=adv["cvss_score"],
                severity=adv["severity"],
                title=adv["title"],
                summary=adv["summary"],
                cve_list=json.dumps(adv["cve_list"]),
                first_fixed=json.dumps(adv["first_fixed"]),
                pub_url=adv["pub_url"],
                fetched_at=fetched_at,
            )
        except Exception as exc:
            log.warning("advisory_cache_write_failed",
                        advisory_id=adv["advisory_id"], error=str(exc))

    with _advisory_cache_lock:
        _scan_advisory_cache[key] = advisories

    return advisories, None


# ── Scan runner ───────────────────────────────────────────────────────────────

def run_scan(scan_id: str, devices: list[dict],
             cancel_event: threading.Event) -> None:
    """
    Background thread entry point.  Updates DB throughout execution.
    `devices` is a list of dicts: {host, platform}
    """
    _scan_advisory_cache.clear()   # fresh in-process cache per scan

    db.mark_vuln_scan_running(scan_id, _now())
    log.info("vuln_scan_started", scan_id=scan_id, device_count=len(devices))

    success_count  = 0
    error_count    = 0
    finding_count  = 0

    max_workers = min(exec_cfg.max_workers, len(devices), 20)

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f"vuln-{scan_id[:8]}",
    ) as pool:
        futures = {
            pool.submit(_scan_device, scan_id, d["host"], d["platform"], cancel_event): d
            for d in devices
        }

        for future in as_completed(futures):
            if cancel_event.is_set():
                for f in futures:
                    f.cancel()
                break

            dev = future.result()
            host     = dev["host"]
            platform = dev["platform"]
            ostype   = dev.get("ostype")
            version  = dev.get("version")

            # Persist device fact
            db.upsert_vuln_device_fact(
                scan_id=scan_id,
                host=host,
                platform=platform,
                ostype=ostype,
                version=version,
                raw_output=dev.get("raw_output"),
                status=dev["status"],
                error=dev.get("error"),
                collected_at=_now(),
            )

            if dev["status"] != "collected":
                error_count += 1
                log.warning("vuln_device_error", scan_id=scan_id,
                            host=host, error=dev.get("error"))
                continue

            # Lookup advisories for this (ostype, version)
            advisories, api_err = _lookup_advisories(ostype, version)
            if api_err:
                log.warning("vuln_advisory_lookup_failed", scan_id=scan_id,
                            host=host, ostype=ostype, version=version, error=api_err)
                db.upsert_vuln_device_fact(
                    scan_id=scan_id, host=host, platform=platform,
                    ostype=ostype, version=version,
                    raw_output=dev.get("raw_output"),
                    status="error", error=api_err, collected_at=_now(),
                )
                error_count += 1
                continue

            # Resolve first_seen_scan_id per advisory
            for adv in advisories:
                aid = adv["advisory_id"]
                first_seen = db.get_first_seen_scan(host, aid) or scan_id
                db.insert_vuln_finding(
                    scan_id=scan_id,
                    host=host,
                    advisory_id=aid,
                    ostype=ostype,
                    version=version,
                    first_seen_scan_id=first_seen,
                )
                finding_count += 1

            success_count += 1
            log.info("vuln_device_done", scan_id=scan_id, host=host,
                     version=version, findings=len(advisories))

    if cancel_event.is_set():
        db.mark_vuln_scan_complete(scan_id, "cancelled", _now())
    elif success_count == 0 and error_count > 0:
        db.mark_vuln_scan_complete(scan_id, "failed", _now())
    elif error_count > 0:
        db.mark_vuln_scan_complete(scan_id, "partial_failure", _now())
    else:
        db.mark_vuln_scan_complete(scan_id, "completed", _now())

    log.info("vuln_scan_done", scan_id=scan_id,
             success=success_count, errors=error_count, findings=finding_count)


# ── Public entry point ────────────────────────────────────────────────────────

def submit_scan(devices: list[dict], incident: Optional[str] = None,
                triggered_by: str = "api") -> str:
    """
    Create a scan record and start the background scan thread.
    Returns the scan_id.
    """
    scan_id = f"scan-{uuid.uuid4().hex[:12]}"
    cancel_event = threading.Event()

    db.create_vuln_scan(
        scan_id=scan_id,
        device_count=len(devices),
        incident=incident,
        triggered_by=triggered_by,
    )

    t = threading.Thread(
        target=run_scan,
        args=(scan_id, devices, cancel_event),
        name=f"vuln-{scan_id[:12]}",
        daemon=True,
    )
    t.start()
    log.info("vuln_scan_submitted", scan_id=scan_id, device_count=len(devices))
    return scan_id
