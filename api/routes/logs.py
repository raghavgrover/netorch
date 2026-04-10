"""
api/routes/logs.py — Log retrieval endpoints.

GET /logs/{job_id}          Structured JSON results for a job
GET /logs/{job_id}/raw      Download the raw JSON log file
GET /logs/{job_id}/device/{host}  Results for a single device
"""
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
import orjson

from core.job_store import store

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get(
    "/{job_id}",
    summary="Structured JSON results for a completed job",
)
def get_log(job_id: str):
    path = store.log_path(job_id)
    if not path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Log for job '{job_id}' not found.",
        )
    data = orjson.loads(path.read_bytes())
    return JSONResponse(content=data)


@router.get(
    "/{job_id}/raw",
    summary="Download raw JSON log file (for BigFix harvesting)",
)
def get_log_raw(job_id: str):
    path = store.log_path(job_id)
    if not path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Log for job '{job_id}' not found.",
        )
    return FileResponse(
        path=str(path),
        media_type="application/json",
        filename=f"{job_id}.json",
    )


@router.get(
    "/{job_id}/device/{host:path}",
    summary="Result for a single device within a job",
)
def get_device_log(job_id: str, host: str):
    path = store.log_path(job_id)
    if not path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Log for job '{job_id}' not found.",
        )
    data = orjson.loads(path.read_bytes())
    device_data = data.get("devices", {}).get(host)
    if not device_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No result for device '{host}' in job '{job_id}'.",
        )
    return JSONResponse(content=device_data)
