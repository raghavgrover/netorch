"""
api/routes/inventory.py

Inventory endpoints — read, write, and manage inventory files.

Existing endpoints (unchanged behaviour):
  GET  /inventory/hosts       List all hosts (now with server-side pagination/search)
  GET  /inventory/groups      List all group names
  POST /inventory/reload      Reload all inventory files from disk

New endpoints (required by netorch-ui):
  GET  /inventory/sources             List all .ini files with host/group counts
  GET  /inventory/sources/{filename}  Read raw content of one file
  PUT  /inventory/sources/{filename}  Create or overwrite one file
  POST /inventory/sources             Upload a new file (same as PUT but always creates)
  DELETE /inventory/sources/{filename} Delete a file

Design notes for 50 000-host scale:
- /inventory/hosts accepts ?offset, ?limit, ?search, ?platform, ?group
  so the UI can page through hosts server-side — the browser never receives
  more rows than it asked for.
- File write operations invalidate the in-process inventory cache so the
  next /inventory/hosts call reflects the change immediately.
- Filenames are validated (must be .ini, no path separators) before any
  filesystem operation.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel

from core.config import inventory as inventory_cfg
from secrets.inventory import inventory_client

router = APIRouter(prefix="/inventory", tags=["inventory"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inventory_dir() -> Path:
    """Return the inventory directory, creating it if needed."""
    p = Path(inventory_cfg.path)
    # If path points at a file (legacy single-file setup), use its parent dir
    if p.is_file():
        p = p.parent
    p.mkdir(parents=True, exist_ok=True)
    return p


def _validate_filename(filename: str) -> str:
    """
    Validate and normalise an inventory filename.
    - Must end with .ini
    - No path separators (prevents directory traversal)
    - Returns the clean basename
    """
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="filename is required",
        )
    # Strip any leading path to prevent traversal
    basename = Path(filename).name
    if not basename.endswith(".ini"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Filename must end with .ini",
        )
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]+\.ini", basename):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Filename contains invalid characters. Use letters, digits, _ - . only.",
        )
    return basename


def _file_path(filename: str) -> Path:
    return _inventory_dir() / _validate_filename(filename)


def _count_ini_file(path: Path) -> dict:
    """
    Quick parse of a .ini inventory file to count hosts and groups.
    Does not use the full InventoryClient so it stays fast and independent
    of the main credential cache.
    """
    import configparser
    cfg = configparser.RawConfigParser(allow_no_value=True)
    try:
        cfg.read(str(path))
    except Exception:
        return {"hosts": 0, "groups": 0}

    groups = [s for s in cfg.sections() if s != "all:vars" and not s.endswith(":vars")]
    host_set: set[str] = set()
    for g in groups:
        for key in cfg.options(g):
            # In Ansible INI format the host is the key (option name)
            host_set.add(key)
    return {"hosts": len(host_set), "groups": len(groups)}


# ── Platform distribution helper ──────────────────────────────────────────────

def _platform_counts() -> dict[str, int]:
    """Aggregate platform counts across all inventory files."""
    import configparser
    inv_dir = _inventory_dir()
    counts: dict[str, int] = {}
    for f in sorted(inv_dir.glob("*.ini")):
        cfg = configparser.RawConfigParser(allow_no_value=True)
        try:
            cfg.read(str(f))
        except Exception:
            continue
        for section in cfg.sections():
            if section in ("all:vars",) or section.endswith(":vars"):
                continue
            for host, line_value in cfg.items(section):
                # Parse inline vars: "platform=cisco_ios  username=..."
                plat = "unknown"
                if line_value:
                    m = re.search(r"platform\s*=\s*(\S+)", line_value)
                    if m:
                        plat = m.group(1)
                counts[plat] = counts.get(plat, 0) + 1
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Existing endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/hosts", summary="List inventory hosts with optional pagination and search")
def list_hosts(
    offset:   int = Query(0,   ge=0,        description="Pagination offset"),
    limit:    int = Query(100, ge=1, le=500, description="Page size (max 500)"),
    search:   Optional[str] = Query(None,   description="Substring search on host/IP"),
    platform: Optional[str] = Query(None,   description="Filter by platform name"),
    group:    Optional[str] = Query(None,   description="Filter by group name"),
):
    """
    Return hosts from the inventory with server-side pagination.

    With 50 000 hosts this endpoint is the critical path — the UI should
    never request more than 500 rows at a time.  All filtering is done
    in-process against the cached inventory so there is no DB query.
    """
    try:
        all_host_entries = inventory_client.list_host_entries()
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))

    # Apply filters
    filtered = all_host_entries
    if search:
        q = search.lower()
        filtered = [h for h in filtered if q in h["host"].lower()]
    if platform:
        filtered = [h for h in filtered if h.get("platform", "") == platform]
    if group:
        filtered = [h for h in filtered if group in h.get("groups", [])]

    total = len(filtered)
    page  = filtered[offset: offset + limit]
    
    #Added below 2 lines to wire in the last_job/last_status enrichment
    from api.routes.host_enrichment import enrich_host_entries
    page = enrich_host_entries(page)
    
    return {
        "total":  total,
        "offset": offset,
        "limit":  limit,
        "hosts":  page,
    }


@router.get("/groups", summary="List all group names across all inventory files")
def list_groups():
    try:
        groups = inventory_client.list_groups()
        return {"count": len(groups), "groups": groups}
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@router.post("/reload", summary="Reload all inventory files from disk")
def reload_inventory():
    inventory_client.reload()
    return {
        "status": "reloaded",
        "inventory_path": str(_inventory_dir()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# New endpoints — inventory file management
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sources", summary="List all inventory .ini files with host/group counts")
def list_sources():
    """
    Returns metadata for every .ini file in the inventory directory.
    Also includes aggregate platform_counts for the dashboard.
    """
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


@router.get(
    "/sources/{filename}",
    summary="Read the raw text content of one inventory file",
)
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


# ── Write / update ────────────────────────────────────────────────────────────

@router.put(
    "/sources/{filename}",
    summary="Create or overwrite an inventory file",
    status_code=status.HTTP_200_OK,
)
def put_source(filename: str, body: SourceWriteBody):
    """
    Save `body.content` as the full text of the named inventory file.
    If the file already exists it is overwritten atomically (write to a
    .tmp file then rename so a crash cannot corrupt the existing file).
    Invalidates the in-process inventory cache after writing.
    """
    path = _file_path(filename)
    existed = path.exists()

    # Atomic write
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
    return {
        "status":   "updated" if existed else "created",
        "filename": filename,
        "path":     str(path),
    }


@router.post(
    "/sources",
    summary="Upload / create a new inventory file",
    status_code=status.HTTP_201_CREATED,
)
def post_source(body: SourceCreateBody):
    """
    Create a new inventory file.  Returns 409 if the file already exists
    (use PUT to overwrite).
    """
    path = _file_path(body.filename)
    if path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{body.filename}' already exists. Use PUT to overwrite.",
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
    return {
        "status":   "created",
        "filename": body.filename,
        "path":     str(path),
    }


@router.delete(
    "/sources/{filename}",
    summary="Delete an inventory file",
)
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
