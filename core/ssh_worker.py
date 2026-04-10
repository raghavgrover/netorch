"""
core/ssh_worker.py — Per-device SSH worker with retry logic.

Accepts a DeviceEntry, resolves credentials from the local inventory,
then connects via the correct driver and runs audit/remediation commands.

Retries transient SSH failures up to executor.retry_attempts times
with executor.retry_delay seconds between attempts.
Auth failures and inventory misses are NOT retried.
"""
from __future__ import annotations
import os
import time
from datetime import datetime, timezone

from api.schemas import (
    CommandResult, DeviceResult, DeviceStatus, JobMode, DeviceEntry,
    FileTransferEntry,
)
from drivers import get_driver
from drivers.base import DeviceCredentials
from secrets.inventory import inventory_client
from core.config import executor as exec_cfg
from core.logger import get_logger

log = get_logger("worker")

_NO_RETRY_ERRORS = (
    "authentication failed",
    "auth failed",
    "no matching key exchange",
    "no inventory entry",
    "unsupported platform",
)


def _is_retryable(error: str) -> bool:
    lower = error.lower()
    return not any(phrase in lower for phrase in _NO_RETRY_ERRORS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attempt(
    creds: DeviceCredentials,
    mode: JobMode,
    commands: list[str],
    remediation_commands: list[str] | None,
    file_transfers: list[FileTransferEntry] | None,
    timeout: int,
    backup_config: bool,
) -> tuple[list[CommandResult], str | None]:
    """
    Single connection attempt. Returns (command_results, config_backup).
    Raises on any error — caller handles retry logic.
    """
    driver = get_driver(creds, timeout=timeout)
    command_results: list[CommandResult] = []
    config_backup: str | None = None

    with driver:
        if mode == JobMode.remediate and backup_config:
            try:
                config_backup = driver.get_running_config()
            except Exception as e:
                config_backup = f"[backup failed: {e}]"

        if file_transfers:
            for ft in file_transfers:
                ts = _now_iso()
                filename = os.path.basename(ft.local_path)
                label = f"[transfer: {filename}]"
                try:
                    driver.transfer_file(ft.local_path, ft.remote_path)
                    out = f"Transferred {filename} to {ft.remote_path}"
                    if ft.post_transfer_commands:
                        rem_out = driver.run_config_commands(ft.post_transfer_commands)
                        out += f"\n{rem_out}"
                    command_results.append(CommandResult(
                        command=label, output=out, timestamp=ts,
                    ))
                except Exception as e:
                    command_results.append(CommandResult(
                        command=label, output="", timestamp=ts, error=str(e),
                    ))

        for cmd in commands:
            ts = _now_iso()
            try:
                output = driver.run_command(cmd)
                command_results.append(CommandResult(
                    command=cmd, output=output, timestamp=ts,
                ))
            except Exception as e:
                command_results.append(CommandResult(
                    command=cmd, output="", timestamp=ts, error=str(e),
                ))

        if mode == JobMode.remediate and remediation_commands:
            ts = _now_iso()
            try:
                rem_output = driver.run_config_commands(remediation_commands)
                command_results.append(CommandResult(
                    command="[remediation]", output=rem_output, timestamp=ts,
                ))
            except Exception as e:
                command_results.append(CommandResult(
                    command="[remediation]", output="", timestamp=ts, error=str(e),
                ))

    return command_results, config_backup


def run_device_job(
    device: DeviceEntry,
    mode: JobMode,
    commands: list[str],
    remediation_commands: list[str] | None,
    file_transfers: list[FileTransferEntry] | None,
    timeout: int,
    backup_config: bool,
    job_id: str = "",
) -> DeviceResult:
    """
    Resolve credentials for the device, then execute the full job
    with retry on transient failures.
    Always returns a DeviceResult — never raises.
    """
    start = time.monotonic()
    max_attempts = max(1, exec_cfg.retry_attempts)

    # Resolve credentials from inventory
    try:
        creds: DeviceCredentials = inventory_client.get_credentials(
            host=device.host,
            group=device.group,
            platform_hint=device.platform,
        )
    except Exception as e:
        duration = round(time.monotonic() - start, 2)
        log.error("credential_lookup_failed",
                  job_id=job_id, host=device.host, error=str(e))
        return DeviceResult(
            host=device.host or f"[group:{device.group}]",
            platform=device.platform,
            status=DeviceStatus.failed,
            duration_seconds=duration,
            commands=[],
            error=str(e),
        )

    last_error: str = ""

    for attempt in range(1, max_attempts + 1):
        try:
            log.info("device_attempt",
                     job_id=job_id, host=creds.host,
                     attempt=attempt, max_attempts=max_attempts)

            command_results, config_backup = _attempt(
                creds=creds,
                mode=mode,
                commands=commands,
                remediation_commands=remediation_commands,
                file_transfers=file_transfers,
                timeout=timeout,
                backup_config=backup_config,
            )

            duration = round(time.monotonic() - start, 2)
            log.info("device_success",
                     job_id=job_id, host=creds.host,
                     duration=duration, attempt=attempt)

            return DeviceResult(
                host=creds.host,
                platform=creds.platform,
                status=DeviceStatus.success,
                duration_seconds=duration,
                commands=command_results,
                config_backup=config_backup,
            )

        except Exception as e:
            last_error = str(e)
            log.warning("device_attempt_failed",
                        job_id=job_id, host=creds.host,
                        attempt=attempt, error=last_error)

            if attempt < max_attempts and _is_retryable(last_error):
                log.info("device_retrying",
                         job_id=job_id, host=creds.host,
                         delay=exec_cfg.retry_delay)
                time.sleep(exec_cfg.retry_delay)
            else:
                break

    duration = round(time.monotonic() - start, 2)
    log.error("device_failed",
               job_id=job_id, host=creds.host,
               duration=duration, error=last_error)

    return DeviceResult(
        host=creds.host,
        platform=creds.platform,
        status=DeviceStatus.failed,
        duration_seconds=duration,
        commands=[],
        error=last_error,
    )
