"""
api/routes/host_enrichment.py — Optional enrichment of host entries.

Called by inventory_route.py list_hosts() to stamp last_job / last_status
onto each host dict from the in-memory job store.

Kept as a standalone module so the dependency between inventory and jobs
layers is isolated and easy to audit.
"""
from __future__ import annotations


def enrich_host_entries(entries: list[dict]) -> list[dict]:
    """
    Walk the job store (newest-first) and stamp the most recent job_id
    and device status onto each host entry.

    Performance: O(jobs × devices_per_job) — runs once per /inventory/hosts
    request. Typical times are well under 1ms even for large deployments
    because store._jobs is an in-memory dict.
    """
    try:
        from core.job_store import store
    except ImportError:
        return entries  # graceful if job_store not available

    # Build host → (job_id, device_status) walking newest jobs first
    host_last: dict[str, tuple[str, str]] = {}

    for job_status in store.list_jobs():          # list_jobs() returns newest first
        job_id  = job_status.job_id
        # get_detail returns JobDetailResponse with .devices list[DeviceResult]
        detail = store.get_detail(job_id)
        if not detail:
            continue
        for dr in detail.devices:
            if dr.host not in host_last:
                host_last[dr.host] = (job_id, dr.status.value)

    for entry in entries:
        if entry["host"] in host_last:
            jid, last_status = host_last[entry["host"]]
            entry["last_job"]    = jid
            entry["last_status"] = last_status

    return entries
