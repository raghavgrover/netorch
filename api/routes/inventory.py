"""
api/routes/inventory.py — Inventory inspection and reload endpoints.

GET  /inventory/hosts              List all individual hosts
GET  /inventory/groups             List all groups
GET  /inventory/groups/{group}     List all hosts within a specific group
POST /inventory/reload             Force re-read of inventory.ini from disk
"""
from fastapi import APIRouter, HTTPException, status
from secrets.inventory import inventory_client
from core.config import inventory as inventory_cfg

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("/hosts", summary="List all hosts defined in inventory.ini")
def list_hosts():
    try:
        hosts = inventory_client.list_hosts()
        return {"count": len(hosts), "hosts": hosts}
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@router.get("/groups", summary="List all groups defined in inventory.ini")
def list_groups():
    try:
        groups = inventory_client.list_groups()
        return {"count": len(groups), "groups": groups}
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@router.get("/groups/{group}", summary="List all hosts within a group")
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


@router.get("/sources", summary="List loaded inventory files with per-file host/group counts")
def list_sources():
    try:
        inv = inventory_client._load()
        sources = inv.sources
        return {
            "sources": [
                {"file": fname, "hosts": info["hosts"], "groups": info["groups"]}
                for fname, info in sources.items()
            ],
            "total_hosts":  len(inv.by_host),
            "total_groups": len(inv.by_group),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@router.post("/reload", summary="Reload inventory.ini from disk without restarting")
def reload_inventory():
    inventory_client.reload()
    return {
        "status": "reloaded",
        "inventory_path": str(inventory_cfg.path),
    }
