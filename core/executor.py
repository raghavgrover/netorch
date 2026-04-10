"""
core/executor.py — Parallel job executor with queue depth control
and cancellation support.

By the time submit_job() is called, all DeviceEntry items in the
request have already been expanded from group-only entries into
concrete host entries by _expand_devices() in api/routes/jobs.py.
The executor simply fans them out to ssh_worker.run_device_job()
in parallel via a ThreadPoolExecutor.
"""
from __future__ import annotations
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, Future

from api.schemas import JobSubmitRequest
from core.job_store import store
from core.ssh_worker import run_device_job
from core.config import executor as exec_cfg
from core.logger import get_logger

log = get_logger("executor")

# Per-job cancellation signals
_cancel_events: dict[str, threading.Event] = {}
_cancel_lock   = threading.Lock()

# Global cap on concurrent active jobs
_queue_sem = threading.Semaphore(exec_cfg.max_queue_depth)


def _get_cancel_event(job_id: str) -> threading.Event:
    with _cancel_lock:
        if job_id not in _cancel_events:
            _cancel_events[job_id] = threading.Event()
        return _cancel_events[job_id]


def _cleanup_cancel_event(job_id: str) -> None:
    with _cancel_lock:
        _cancel_events.pop(job_id, None)


def cancel_job(job_id: str) -> bool:
    """Signal a job to cancel. Returns True if the job was found."""
    with _cancel_lock:
        event = _cancel_events.get(job_id)
    if event:
        event.set()
        log.info("job_cancel_requested", job_id=job_id)
        return True
    return False


def _run_job(request: JobSubmitRequest) -> None:
    """
    Background thread entry point. Acquires the queue semaphore,
    fans out one ssh_worker call per device, updates the job store
    in real time, then releases the semaphore.
    """
    job_id    = request.job_id
    cancel_ev = _get_cancel_event(job_id)

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
        log.info("job_started",
                 job_id=job_id,
                 mode=request.mode.value,
                 device_count=len(request.devices))

        max_workers = min(
            request.options.max_workers,
            exec_cfg.max_workers,
            len(request.devices),
        )

        futures: dict[Future, object] = {}

        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"netorch-{job_id[:8]}",
        ) as pool:

            for device in request.devices:
                if cancel_ev.is_set():
                    log.info("job_cancelled_mid_submission", job_id=job_id)
                    break
                f = pool.submit(
                    run_device_job,
                    device=device,
                    mode=request.mode,
                    commands=request.commands,
                    remediation_commands=request.remediation_commands,
                    file_transfers=request.file_transfers,
                    timeout=request.options.timeout_per_device,
                    backup_config=request.options.backup_config_before_change,
                    job_id=job_id,
                )
                futures[f] = device

            for future in as_completed(futures):
                if cancel_ev.is_set():
                    for f in futures:
                        f.cancel()
                result = future.result()   # ssh_worker never raises
                store.update_device(job_id, result)
                log.info("device_result_stored",
                         job_id=job_id,
                         host=result.host,
                         status=result.status.value)

        if cancel_ev.is_set():
            store.mark_cancelled(job_id)
            log.info("job_cancelled", job_id=job_id)
        else:
            store.mark_complete(job_id)
            status = store.get_status(job_id)
            log.info("job_complete",
                     job_id=job_id,
                     status=status.status.value if status else "unknown",
                     success=status.summary.success if status else 0,
                     failed=status.summary.failed if status else 0)

    except Exception as e:
        log.error("job_executor_error", job_id=job_id, error=str(e))
        store.mark_failed(job_id, reason=str(e))

    finally:
        _queue_sem.release()
        _cleanup_cancel_event(job_id)


def submit_job(request: JobSubmitRequest) -> None:
    """
    Register the job and launch the background thread.
    Returns immediately — callers poll GET /jobs/{id} for progress.
    """
    _get_cancel_event(request.job_id)
    store.create(
        job_id=request.job_id,
        mode=request.mode,
        device_count=len(request.devices),
    )
    t = threading.Thread(
        target=_run_job,
        args=(request,),
        name=f"netorch-job-{request.job_id[:8]}",
        daemon=True,
    )
    t.start()
    log.info("job_submitted", job_id=request.job_id)


def active_job_count() -> int:
    return exec_cfg.max_queue_depth - _queue_sem._value
