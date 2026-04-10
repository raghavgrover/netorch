"""
api/schemas.py — All Pydantic request/response models for netorch.
"""
from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator
import uuid


class JobMode(str, Enum):
    audit      = "audit"
    remediate  = "remediate"


class JobStatus(str, Enum):
    queued          = "queued"
    running         = "running"
    completed       = "completed"
    partial_failure = "partial_failure"
    failed          = "failed"
    cancelled       = "cancelled"


class DeviceStatus(str, Enum):
    success = "success"
    failed  = "failed"
    skipped = "skipped"


# ---------------------------------------------------------------------------
# Job submission
# ---------------------------------------------------------------------------

class DeviceEntry(BaseModel):
    """
    Identifies a target device. Three accepted forms:

    1. Host only          — {"host": "10.0.0.1"}
    2. Host + group       — {"host": "10.0.0.1", "group": "core_switches"}
                           (group used as credential fallback)
    3. Group only         — {"group": "core_switches"}
                           (expands to all hosts in that group at submission)

    `platform` is always optional — looked up from inventory.ini.
    """
    host:     Optional[str] = Field(
        None,
        description="IP address or hostname. Omit to target all hosts in the group.",
    )
    group:    Optional[str] = Field(
        None,
        description=(
            "Inventory group name. "
            "If host is omitted, all hosts in the group are targeted. "
            "If host is present, group is used as a credential fallback."
        ),
    )
    platform: Optional[str] = Field(
        None,
        description="cisco_ios | cisco_xe | cisco_xr | linux. Looked up from inventory.ini if omitted.",
    )

    @model_validator(mode="after")
    def host_or_group_required(self) -> "DeviceEntry":
        if not self.host and not self.group:
            raise ValueError("Each device entry must have at least 'host' or 'group'.")
        return self


class JobOptions(BaseModel):
    timeout_per_device:          int  = Field(30, ge=5, le=300)
    max_workers:                 int  = Field(50, ge=1, le=500)
    backup_config_before_change: bool = Field(True)


class FileTransferEntry(BaseModel):
    """A single file to push to each target device before running commands."""
    local_path:              str            = Field(..., description="Absolute path on the relay filesystem.")
    remote_path:             str            = Field(..., description="Destination path/file-system on the device.")
    post_transfer_commands:  Optional[list[str]] = Field(
        None,
        description="Commands to run on the device after the file arrives.",
    )


class JobSubmitRequest(BaseModel):
    job_id: Optional[str] = Field(
        default_factory=lambda: f"job-{uuid.uuid4().hex[:8]}",
    )
    mode:     JobMode          = Field(JobMode.audit)
    devices:  list[DeviceEntry] = Field(..., min_length=1)
    commands: list[str]         = Field(default_factory=list)
    remediation_commands: Optional[list[str]] = Field(None)
    file_transfers:       Optional[list[FileTransferEntry]] = Field(None)
    options:  JobOptions        = Field(default_factory=JobOptions)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class JobSubmitResponse(BaseModel):
    job_id:       str
    status:       JobStatus
    device_count: int
    log_path:     str


class JobSummary(BaseModel):
    total:       int
    success:     int
    failed:      int
    in_progress: int


class JobStatusResponse(BaseModel):
    job_id:       str
    status:       JobStatus
    mode:         JobMode
    started_at:   Optional[str]
    completed_at: Optional[str]
    summary:      JobSummary


class JobListResponse(BaseModel):
    total:  int
    offset: int
    limit:  int
    jobs:   list[JobStatusResponse]


class CommandResult(BaseModel):
    command:   str
    output:    str
    timestamp: str
    error:     Optional[str] = None


class DeviceResult(BaseModel):
    host:             str
    platform:         Optional[str]
    status:           DeviceStatus
    duration_seconds: float
    commands:         list[CommandResult] = []
    config_backup:    Optional[str]       = None
    error:            Optional[str]       = None


class JobDetailResponse(BaseModel):
    job_id:       str
    mode:         JobMode
    status:       JobStatus
    started_at:   Optional[str]
    completed_at: Optional[str]
    summary:      JobSummary
    devices:      list[DeviceResult]
    error:        Optional[str] = None


class CancelResponse(BaseModel):
    job_id:  str
    message: str


class SystemStatsResponse(BaseModel):
    active_jobs:     int
    max_queue_depth: int
    version:         str
