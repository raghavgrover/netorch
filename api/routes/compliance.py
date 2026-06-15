"""
api/routes/compliance.py — Vulnerability scanning REST endpoints.

POST /compliance/scans                  Submit a new scan
GET  /compliance/scans                  List scans (paginated)
GET  /compliance/scans/{id}             Scan status + summary
GET  /compliance/scans/{id}/results     Per-device findings
GET  /compliance/scans/{id}/results/csv Per-device findings as CSV
GET  /compliance/advisories             Cached advisory catalogue
"""
from __future__ import annotations

import csv
import io
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.db import db
from core.vuln_scanner import submit_scan
from secrets.inventory import inventory_client

router = APIRouter(prefix="/compliance", tags=["compliance"])


# ── Pydantic request models ───────────────────────────────────────────────────

class ScanDevice(BaseModel):
    host:  Optional[str] = None
    group: Optional[str] = None


class ScanRequest(BaseModel):
    devices:      list[ScanDevice] = Field(..., min_items=1)
    incident:     Optional[str]    = None
    triggered_by: str              = "api"


# ── Helper: expand devices ────────────────────────────────────────────────────

def _expand_devices(raw: list[ScanDevice]) -> list[dict]:
    """Expand group entries into individual {host, platform} dicts."""
    expanded: list[dict] = []
    seen: set[str] = set()

    for entry in raw:
        if entry.host:
            if entry.host not in seen:
                try:
                    creds = inventory_client.get_credentials(entry.host, group=entry.group)
                    platform = creds.platform or ""
                except Exception:
                    platform = ""
                expanded.append({"host": entry.host, "platform": platform})
                seen.add(entry.host)
        elif entry.group:
            try:
                members = inventory_client.get_group_hosts(entry.group)
            except Exception:
                members = []
            for creds in members:
                if creds.host not in seen:
                    expanded.append({"host": creds.host, "platform": creds.platform or ""})
                    seen.add(creds.host)

    return expanded


# ── Summary helper ────────────────────────────────────────────────────────────

def _build_summary(scan_id: str) -> dict:
    s = db.get_vuln_scan_summary(scan_id)
    total = (s.get("critical", 0) + s.get("high", 0) +
             s.get("medium", 0) + s.get("low", 0) + s.get("informational", 0))
    return {
        "total_findings":   total,
        "critical_count":   s.get("critical", 0),
        "high_count":       s.get("high", 0),
        "medium_count":     s.get("medium", 0),
        "low_count":        s.get("low", 0),
        "informational_count": s.get("informational", 0),
        "devices_with_findings": s.get("devices_with_findings", 0),
    }


def _parse_json_field(val) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except Exception:
        return []


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/scans", status_code=202)
def submit_vuln_scan(body: ScanRequest):
    devices = _expand_devices(body.devices)
    if not devices:
        raise HTTPException(400, "No devices resolved — check group names and inventory.")

    scan_id = submit_scan(
        devices=devices,
        incident=body.incident,
        triggered_by=body.triggered_by,
    )
    return {"scan_id": scan_id, "device_count": len(devices), "status": "queued"}


@router.get("/scans")
def list_scans(
    limit:  int = Query(50,  ge=1, le=200),
    offset: int = Query(0,   ge=0),
):
    total = db.count_vuln_scans()
    rows  = db.list_vuln_scans(limit=limit, offset=offset)
    items = []
    for r in rows:
        summary = _build_summary(r["scan_id"])
        items.append({
            "scan_id":      r["scan_id"],
            "status":       r["status"],
            "incident":     r["incident"],
            "triggered_by": r["triggered_by"],
            "device_count": r["device_count"],
            "started_at":   r["started_at"],
            "completed_at": r["completed_at"],
            **summary,
        })
    return {"total": total, "scans": items}


@router.get("/scans/{scan_id}")
def get_scan(scan_id: str):
    row = db.get_vuln_scan(scan_id)
    if not row:
        raise HTTPException(404, f"Scan '{scan_id}' not found.")
    summary = _build_summary(scan_id)
    return {
        "scan_id":      row["scan_id"],
        "status":       row["status"],
        "incident":     row["incident"],
        "triggered_by": row["triggered_by"],
        "device_count": row["device_count"],
        "started_at":   row["started_at"],
        "completed_at": row["completed_at"],
        "error":        row.get("error"),
        **summary,
    }


@router.get("/scans/{scan_id}/results")
def get_scan_results(scan_id: str):
    row = db.get_vuln_scan(scan_id)
    if not row:
        raise HTTPException(404, f"Scan '{scan_id}' not found.")

    facts    = db.get_vuln_device_facts(scan_id)
    findings = db.get_vuln_findings_with_advisories(scan_id)

    by_host: dict[str, list] = {}
    for f in findings:
        by_host.setdefault(f["host"], []).append(f)

    devices = []
    for fact in facts:
        h = fact["host"]
        advisories = []
        for f in by_host.get(h, []):
            advisories.append({
                "advisory_id":        f["advisory_id"],
                "severity":           f["severity"],
                "cvss_score":         f["cvss_score"],
                "title":              f["title"],
                "summary":            f["summary"],
                "cve_list":           _parse_json_field(f["cve_list"]),
                "first_fixed":        _parse_json_field(f["first_fixed"]),
                "pub_url":            f["pub_url"],
                "first_seen_scan_id": f["first_seen_scan_id"],
            })
        advisories.sort(key=lambda a: (a["cvss_score"] or 0), reverse=True)
        devices.append({
            "host":         h,
            "platform":     fact["platform"],
            "ostype":       fact["ostype"],
            "version":      fact["version"],
            "status":       fact["status"],
            "error":        fact["error"],
            "collected_at": fact["collected_at"],
            "findings":     advisories,
            "finding_count": len(advisories),
        })

    return {"scan_id": scan_id, "device_count": len(devices), "devices": devices}


@router.get("/scans/{scan_id}/results/csv")
def get_scan_results_csv(scan_id: str):
    row = db.get_vuln_scan(scan_id)
    if not row:
        raise HTTPException(404, f"Scan '{scan_id}' not found.")

    findings = db.get_vuln_findings_with_advisories(scan_id)
    facts    = {f["host"]: f for f in db.get_vuln_device_facts(scan_id)}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Scan ID", "Host", "Platform", "OS Type", "Version",
        "Advisory ID", "Severity", "CVSS Score",
        "Title", "CVEs", "First Fixed", "Publication URL", "First Seen Scan",
    ])
    for f in sorted(findings, key=lambda x: (x["cvss_score"] or 0), reverse=True):
        fact = facts.get(f["host"], {})
        writer.writerow([
            scan_id,
            f["host"],
            fact.get("platform", ""),
            f["ostype"],
            f["version"],
            f["advisory_id"],
            f["severity"],
            f["cvss_score"],
            f["title"],
            "; ".join(_parse_json_field(f["cve_list"])),
            "; ".join(_parse_json_field(f["first_fixed"])),
            f["pub_url"],
            f["first_seen_scan_id"],
        ])

    buf.seek(0)
    filename = f"vuln-scan-{scan_id}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/advisories")
def list_advisories(
    ostype:   Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    limit:    int           = Query(200, ge=1, le=1000),
    offset:   int           = Query(0,   ge=0),
):
    """Return the cached advisory catalogue."""
    rows = db.list_vuln_advisories(ostype=ostype, severity=severity,
                                   limit=limit, offset=offset)
    return {
        "total": len(rows),
        "advisories": [
            {
                **dict(r),
                "cve_list":    _parse_json_field(r.get("cve_list")),
                "first_fixed": _parse_json_field(r.get("first_fixed")),
            }
            for r in rows
        ],
    }
