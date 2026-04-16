"""
api/routes/inventory.py — Inventory inspection and file management endpoints.

Existing endpoints (behaviour preserved exactly):
  GET  /inventory/hosts              List all hosts (now with server-side pagination)
  GET  /inventory/groups             List all groups
  GET  /inventory/groups/{group}     List all hosts in a group
  POST /inventory/reload             Force re-read from disk

New endpoints (required by netorch-ui):
  GET    /inventory/sources                  List .ini files with counts
  GET    /inventory/sources/{filename}       Read raw content of one file
  PUT    /inventory/sources/{filename}       Create or overwrite a file
  POST   /inventory/sources                  Upload a new file (creates only)
  DELETE /inventory/sources/{filename}       Delete a file
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from secrets.inventory import inventory_client
from core.config import inventory as inventory_cfg

router = APIRouter(prefix="/inventory", tags=["inventory"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inventory_dir() -> Path:
    """Return the inventory directory, creating it if it doesn't exist."""
    p = Path(inventory_cfg.path)
    if p.is_file():
        p = p.parent
    p.mkdir(parents=True, exist_ok=True)
    return p


def _validated_filename(filename: str) -> str:
    """
    Validate a filename for an inventory file:
    - Must end in .ini
    - No path separators (prevents directory traversal)
    - Only safe characters
    Returns the clean basename.
    """
    basename = Path(filename).name  # strips any leading path
    if not basename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="filename is required",
        )
    if not basename.endswith(".ini"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Filename must end with .ini",
        )
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]+\.ini", basename):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Filename may only contain letters, digits, _ - and .",
        )
    return basename


def _file_path(filename: str) -> Path:
    return _inventory_dir() / _validated_filename(filename)


def _count_ini_file(path: Path) -> dict:
    """Quick parse to count hosts and groups without touching inventory_client."""
    import configparser
    cfg = configparser.RawConfigParser(allow_no_value=True)
    try:
        cfg.read(str(path))
    except Exception:
        return {"hosts": 0, "groups": 0}
    groups   = [s for s in cfg.sections() if s != "all:vars" and not s.endswith(":vars")]
    host_set: set[str] = set()
    for g in groups:
        for key in cfg.options(g):
            host_set.add(key)
    return {"hosts": len(host_set), "groups": len(groups)}


def _platform_counts() -> dict[str, int]:
    """Aggregate platform counts across all inventory files for the dashboard."""
    import configparser
    counts: dict[str, int] = {}
    for f in sorted(_inventory_dir().glob("*.ini")):
        cfg = configparser.RawConfigParser(allow_no_value=True)
        try:
            cfg.read(str(f))
        except Exception:
            continue
        for section in cfg.sections():
            if section in ("all:vars",) or section.endswith(":vars"):
                continue
            for _host, line_value in cfg.items(section):
                plat = "unknown"
                if line_value:
                    m = re.search(r"platform\s*=\s*(\S+)", line_value)
                    if m:
                        plat = m.group(1)
                counts[plat] = counts.get(plat, 0) + 1
    return counts


# ── Existing endpoints ────────────────────────────────────────────────────────

@router.get("/hosts", summary="List inventory hosts with optional pagination and search")
def list_hosts(
    offset:   int = Query(0,   ge=0),
    limit:    int = Query(100, ge=1, le=500, description="Max rows per page (cap 500)"),
    search:   Optional[str] = Query(None, description="Substring match on host/IP"),
    platform: Optional[str] = Query(None, description="Filter by platform"),
    group:    Optional[str] = Query(None, description="Filter by group name"),
):
    """
    Paginated host list. With 50 000 hosts the browser must never request
    more than 500 rows at a time. All filtering is in-process.
    """
    try:
        all_entries = inventory_client.list_host_entries()
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))

    # Optionally enrich with last_job / last_status from the job store
    try:
        from api.routes.host_enrichment import enrich_host_entries
        all_entries = enrich_host_entries(list(all_entries))
    except Exception:
        pass  # enrichment is best-effort; never fail the core response

    # Apply filters
    filtered = all_entries
    if search:
        q = search.lower()
        filtered = [h for h in filtered if q in h["host"].lower()]
    if platform:
        filtered = [h for h in filtered if h.get("platform") == platform]
    if group:
        filtered = [h for h in filtered if group in h.get("groups", [])]

    total = len(filtered)
    page  = filtered[offset: offset + limit]

    return {"total": total, "offset": offset, "limit": limit, "hosts": page}


@router.get("/groups", summary="List all groups defined in inventory")
def list_groups():
    try:
        groups = inventory_client.list_groups()
        return {"count": len(groups), "groups": groups}
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@router.get("/groups/{group}", summary="List all hosts within a specific group")
def get_group_hosts(group: str):
    try:
        creds_list = inventory_client.get_group_hosts(group)
        hosts = [
            {"host": c.host, "platform": c.platform, "port": c.port}
            for c in creds_list
        ]
        return {"group": group, "count": len(hosts), "hosts": hosts}
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@router.post("/reload", summary="Reload all inventory files from disk without restarting")
def reload_inventory():
    inventory_client.reload()
    return {"status": "reloaded", "inventory_path": str(inventory_cfg.path)}


# ── New endpoints — inventory file management ─────────────────────────────────

@router.get("/sources", summary="List all .ini files with host/group counts")
def list_sources():
    inv_dir = _inventory_dir()
    sources = []
    for f in sorted(inv_dir.glob("*.ini")):
        counts = _count_ini_file(f)
        sources.append({
            "file":   f.name,
            "path":   str(f),
            "hosts":  counts["hosts"],
            "groups": counts["groups"],
        })
    return {
        "sources":         sources,
        "total_files":     len(sources),
        "platform_counts": _platform_counts(),
    }


@router.get("/sources/{filename}", summary="Read raw content of one inventory file")
def get_source(filename: str):
    path = _file_path(filename)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Inventory file '{filename}' not found",
        )
    return {
        "filename": filename,
        "path":     str(path),
        "content":  path.read_text(encoding="utf-8"),
    }


# ── Request bodies ────────────────────────────────────────────────────────────

class SourceWriteBody(BaseModel):
    content: str


class SourceCreateBody(BaseModel):
    filename: str
    content:  str


# ── Write endpoints ───────────────────────────────────────────────────────────

@router.put("/sources/{filename}", summary="Create or overwrite an inventory file")
def put_source(filename: str, body: SourceWriteBody):
    """Atomic write (tmp → rename). Invalidates inventory cache after write."""
    path   = _file_path(filename)
    existed = path.exists()
    tmp    = path.with_suffix(".tmp")
    try:
        tmp.write_text(body.content, encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not write file: {e}",
        )
    inventory_client.reload()
    return {"status": "updated" if existed else "created", "filename": filename, "path": str(path)}


@router.post("/sources", status_code=status.HTTP_201_CREATED,
             summary="Upload a new inventory file (fails if already exists)")
def post_source(body: SourceCreateBody):
    path = _file_path(body.filename)
    if path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{body.filename}' already exists. Use PUT /inventory/sources/{body.filename} to overwrite.",
        )
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(body.content, encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not write file: {e}",
        )
    inventory_client.reload()
    return {"status": "created", "filename": body.filename, "path": str(path)}


@router.delete("/sources/{filename}", summary="Delete an inventory file")
def delete_source(filename: str):
    path = _file_path(filename)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Inventory file '{filename}' not found",
        )
    path.unlink()
    inventory_client.reload()
    return {"status": "deleted", "filename": filename}
