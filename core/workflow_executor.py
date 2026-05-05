"""
core/workflow_executor.py — Per-device workflow subprocess runner.

Execution model
───────────────
Each workflow job fans out one bash subprocess per target device.
Every subprocess receives its device context (host, platform, credentials)
plus all user-supplied parameters as environment variables, then runs the
workflow script from top to bottom.

The script controls its own logic — local relay-side steps run natively,
device SSH steps are delegated back to netorch via the netorch_exec helper
(/usr/local/bin/netorch_exec), and once-only blocks are coordinated via
file locks through the netorch_once helper (/usr/local/bin/netorch_once).

Per-device stdout/stderr is:
  1. Streamed line-by-line into workflow_logs (SQLite) for live UI tailing.
  2. Stored as a single CommandResult on the DeviceResult for the job detail view.

Exit code 0  → DeviceStatus.success
Exit code != 0 → DeviceStatus.failed  (error = last N lines of output)

Thread model
────────────
One background threading.Thread per workflow job (submit_workflow spawns it).
Inside that thread a ThreadPoolExecutor fans out one thread per device,
each thread running its own subprocess via _run_device_workflow().
Matches the pattern in core/executor.py exactly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from api.schemas import (
    DeviceEntry, DeviceResult, DeviceStatus, CommandResult,
    JobMode, WorkflowOptions,
)
from core.config import executor as exec_cfg, server as server_cfg
from core.job_store import store
from core.logger import get_logger
from secrets.provider import resolve_credentials

log = get_logger("workflow_executor")

WORKFLOWS_DIR = Path("/opt/netorch/workflows")

# Per-job cancellation signals — mirrors core/executor.py pattern
_cancel_events: dict[str, threading.Event] = {}
_cancel_lock   = threading.Lock()

# Global semaphore shared with the SSH executor so total active jobs
# (audit + remediate + workflow) never exceed max_queue_depth
_queue_sem = threading.Semaphore(exec_cfg.max_queue_depth)


# ── Cancellation helpers ──────────────────────────────────────────────────────

def _get_cancel_event(job_id: str) -> threading.Event:
    with _cancel_lock:
        if job_id not in _cancel_events:
            _cancel_events[job_id] = threading.Event()
        return _cancel_events[job_id]


def _cleanup_cancel_event(job_id: str) -> None:
    with _cancel_lock:
        _cancel_events.pop(job_id, None)


def cancel_workflow(job_id: str) -> bool:
    """Signal a workflow job to cancel. Returns True if the job was found."""
    with _cancel_lock:
        event = _cancel_events.get(job_id)
    if event:
        event.set()
        log.info("workflow_cancel_requested", job_id=job_id)
        return True
    return False


# ── Per-device worker ─────────────────────────────────────────────────────────

def _run_device_workflow(
    script_path: Path,
    device: DeviceEntry,
    parameters: dict[str, str],
    job_id: str,
    timeout: int,
    cancel_ev: threading.Event,
) -> DeviceResult:
    """
    Run the workflow script once for a single device.

    Returns a DeviceResult — never raises.  All errors are captured into
    the result so the executor can continue with remaining devices.
    """
    start_time = time.time()

    # ── Resolve credentials ───────────────────────────────────────────────────
    try:
        creds = resolve_credentials(device)
    except Exception as exc:
        host = device.host or device.group or "unknown"
        log.warning("workflow_cred_error", job_id=job_id, host=host, error=str(exc))
        return DeviceResult(
            host=host,
            platform=device.platform,
            status=DeviceStatus.failed,
            duration_seconds=time.time() - start_time,
            error=f"Credential resolution failed: {exc}",
        )

    host = creds.host or device.host or "unknown"

    # ── Build subprocess environment ──────────────────────────────────────────
    env = os.environ.copy()

    # Device context — always available inside the script
    env["TARGET_HOST"]            = host
    env["DEVICE_PLATFORM"]        = creds.platform or device.platform or ""
    env["DEVICE_USERNAME"]        = creds.username or ""
    env["DEVICE_PASSWORD"]        = creds.password or ""
    env["DEVICE_GROUP"]           = device.group or ""
    env["DEVICE_SSH_PORT"]        = str(getattr(creds, "port", 22) or 22)
    if getattr(creds, "enable_secret", None):
        env["DEVICE_ENABLE_SECRET"] = creds.enable_secret

    # netorch API context — used by netorch_exec to call back into the API
    env["NETORCH_API_URL"]        = f"http://localhost:{server_cfg.port}"
    env["NETORCH_TOKEN"]          = server_cfg.auth_token
    env["NETORCH_JOB_ID"]         = job_id
    env["NETORCH_DEVICE_TIMEOUT"] = str(timeout)

    # Shared lock directory for netorch_once coordination across device instances
    lock_dir = f"/tmp/netorch_workflow_{job_id}"
    env["NETORCH_LOCK_DIR"]       = lock_dir
    os.makedirs(lock_dir, exist_ok=True)

    # User-supplied parameters — injected last so they can override nothing
    # critical, but can add any workflow-specific vars the script needs
    for key, value in parameters.items():
        # Sanitise key: only allow alphanumeric + underscore
        safe_key = "".join(c if c.isalnum() or c == "_" else "_" for c in key).upper()
        env[safe_key] = str(value)

    # ── Execute subprocess ────────────────────────────────────────────────────
    output_lines: list[str] = []
    error_msg: Optional[str] = None
    exit_code = 1
    proc: Optional[subprocess.Popen] = None

    try:
        proc = subprocess.Popen(
            ["bash", str(script_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout
            text=True,
            bufsize=1,                  # line-buffered
        )

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n\r")
            output_lines.append(line)

            # Stream to DB in real time for live UI tailing
            try:
                store.append_workflow_log(job_id, host, line)
            except Exception:
                pass  # log streaming must never kill the subprocess

            # Check cancellation — send SIGTERM and stop reading
            if cancel_ev.is_set():
                proc.terminate()
                log.info("workflow_device_cancelled", job_id=job_id, host=host)
                break

        # Wait for process to finish (with timeout guard)
        try:
            proc.wait(timeout=max(10, timeout))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            error_msg = f"Workflow timed out after {timeout}s"
            log.warning("workflow_device_timeout", job_id=job_id, host=host, timeout=timeout)

        exit_code = proc.returncode if proc.returncode is not None else 1

    except FileNotFoundError:
        error_msg = f"Workflow script not found: {script_path}"
        log.error("workflow_script_missing", job_id=job_id, script=str(script_path))
    except Exception as exc:
        error_msg = str(exc)
        log.error("workflow_subprocess_error", job_id=job_id, host=host, error=str(exc))
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    duration = time.time() - start_time

    # Determine status — cancelled device gets failed status with note
    if cancel_ev.is_set() and exit_code != 0:
        error_msg = error_msg or "Cancelled"
        status = DeviceStatus.failed
    elif exit_code == 0:
        status = DeviceStatus.success
    else:
        status = DeviceStatus.failed
        if not error_msg and output_lines:
            # Use last 5 lines as the error summary
            error_msg = "\n".join(output_lines[-5:])

    log.info(
        "workflow_device_complete",
        job_id=job_id,
        host=host,
        exit_code=exit_code,
        status=status.value,
        duration=f"{duration:.1f}s",
    )

    # Store full output as a single CommandResult labelled "[workflow]"
    return DeviceResult(
        host=host,
        platform=creds.platform or device.platform,
        status=status,
        duration_seconds=duration,
        commands=[
            CommandResult(
                command="[workflow output]",
                output="\n".join(output_lines),
                timestamp=datetime.now(timezone.utc).isoformat(),
                error=error_msg if status == DeviceStatus.failed else None,
            )
        ],
        error=error_msg if status == DeviceStatus.failed else None,
    )


# ── Job runner ────────────────────────────────────────────────────────────────

def _run_workflow_job(
    script_name: str,
    devices: list[DeviceEntry],
    parameters: dict[str, str],
    job_id: str,
    options: WorkflowOptions,
) -> None:
    """
    Background thread entry point.

    Acquires the global queue semaphore, fans out one subprocess per device,
    updates the job store in real time, then releases the semaphore.
    Cleans up the per-job lock directory on completion.
    """
    cancel_ev = _get_cancel_event(job_id)
    script_path = WORKFLOWS_DIR / script_name

    acquired = _queue_sem.acquire(timeout=5)
    if not acquired:
        store.mark_failed(job_id, reason="Queue full — too many concurrent jobs")
        _cleanup_cancel_event(job_id)
        return

    try:
        if cancel_ev.is_set():
            store.mark_cancelled(job_id)
            return

        store.mark_running(job_id)
        log.info(
            "workflow_job_started",
            job_id=job_id,
            script=script_name,
            device_count=len(devices),
            max_workers=options.max_workers,
        )

        max_workers = min(
            options.max_workers,
            exec_cfg.max_workers,
            len(devices),
        )

        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"netorch-wf-{job_id[:8]}",
        ) as pool:

            futures = {}
            for device in devices:
                if cancel_ev.is_set():
                    log.info("workflow_cancelled_mid_submission", job_id=job_id)
                    break
                f = pool.submit(
                    _run_device_workflow,
                    script_path=script_path,
                    device=device,
                    parameters=parameters,
                    job_id=job_id,
                    timeout=options.timeout_per_device,
                    cancel_ev=cancel_ev,
                )
                futures[f] = device

            for future in as_completed(futures):
                if cancel_ev.is_set():
                    for f in futures:
                        f.cancel()
                result = future.result()   # never raises — errors are in result
                store.update_device(job_id, result)
                log.info(
                    "workflow_device_result_stored",
                    job_id=job_id,
                    host=result.host,
                    status=result.status.value,
                )

        if cancel_ev.is_set():
            store.mark_cancelled(job_id)
            log.info("workflow_job_cancelled", job_id=job_id)
        else:
            store.mark_complete(job_id)
            status = store.get_status(job_id)
            log.info(
                "workflow_job_complete",
                job_id=job_id,
                status=status.status.value if status else "unknown",
                success=status.summary.success if status else 0,
                failed=status.summary.failed if status else 0,
            )

    except Exception as exc:
        log.error("workflow_executor_error", job_id=job_id, error=str(exc))
        store.mark_failed(job_id, reason=str(exc))

    finally:
        _queue_sem.release()
        _cleanup_cancel_event(job_id)
        # Clean up shared lock directory used by netorch_once
        lock_dir = f"/tmp/netorch_workflow_{job_id}"
        shutil.rmtree(lock_dir, ignore_errors=True)


# ── Public API ────────────────────────────────────────────────────────────────

def submit_workflow(
    script_name: str,
    devices: list[DeviceEntry],
    parameters: dict[str, str],
    job_id: str,
    options: WorkflowOptions,
    incident: Optional[str] = None,
) -> None:
    """
    Register the workflow job in the store and launch the background thread.
    Returns immediately — callers poll GET /jobs/{id} for progress.
    """
    _get_cancel_event(job_id)
    store.create(
        job_id=job_id,
        mode=JobMode.workflow,
        device_count=len(devices),
        incident=incident,
    )
    t = threading.Thread(
        target=_run_workflow_job,
        args=(script_name, devices, parameters, job_id, options),
        name=f"netorch-wf-{job_id[:8]}",
        daemon=True,
    )
    t.start()
    log.info("workflow_submitted", job_id=job_id, script=script_name, device_count=len(devices))


def active_workflow_count() -> int:
    """Number of workflow slots currently in use (from the shared semaphore)."""
    return exec_cfg.max_queue_depth - _queue_sem._value
