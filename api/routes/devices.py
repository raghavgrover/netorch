"""
api/routes/devices.py — Device-level query endpoints.

GET /devices/{host}/status    Last known result for a device across all jobs
"""
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
import orjson

from core.config import logging_cfg

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get(
    "/{host:path}/status",
    summary="Last known job result for a specific device",
)
def get_device_status(host: str):
    """
    Scans all job log files for the most recent entry containing this host.
    Returns the latest DeviceResult found, or 404 if the device has never
    appeared in any job.
    """
    log_dir = logging_cfg.log_dir
    latest_entry = None
    latest_time = ""

    for log_file in sorted(log_dir.glob("*.json"), reverse=True):
        try:
            data = orjson.loads(log_file.read_bytes())
            device_data = data.get("devices", {}).get(host)
            if device_data:
                completed_at = data.get("completed_at") or ""
                if completed_at > latest_time:
                    latest_time = completed_at
                    latest_entry = {
                        "job_id": data.get("job_id"),
                        "completed_at": completed_at,
                        "device": device_data,
                    }
        except Exception:
            continue

    if not latest_entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job history found for device '{host}'.",
        )
    return JSONResponse(content=latest_entry)
