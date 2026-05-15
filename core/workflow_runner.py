"""
core/workflow_runner.py — YAML workflow execution engine.

Execution model
───────────────
Steps run sequentially.  Device-scoped steps fan out to all non-failed
devices in parallel (ThreadPoolExecutor).  shell/once steps run exactly
once on the relay.  shell/per_device runs one subprocess per device in
parallel.

If a shell/once step exits non-zero the entire job is aborted immediately.
If a device-scoped step fails for a specific device, that device is added
to context["failed_devices"] and skipped for all subsequent device steps.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from api.schemas import (
    DeviceEntry, DeviceResult, DeviceStatus,
    CommandResult, JobMode, JobStatus,
)
from core.config import executor as exec_cfg, logging_cfg
from core.job_store import store
from core.logger import get_logger
from core.workflow_parser import (
    WorkflowDefinition, StepDefinition,
    WorkflowParseError, build_context, substitute_vars,
)
from drivers import get_driver
from drivers.base import DeviceCredentials
from secrets.provider import resolve_credentials

log = get_logger("workflow_runner")

_active_jobs: dict[str, threading.Event] = {}
_active_lock  = threading.Lock()

RUNBOOKS_DIR = Path("/opt/netorch/runbooks")


def active_workflow_count() -> int:
    with _active_lock:
        return len(_active_jobs)


def submit_workflow(
    script_name: str,
    devices: list[DeviceEntry],
    parameters: dict[str, str],
    job_id: str,
    options,
    incident: Optional[str] = None,
) -> None:
    from core.workflow_parser import parse
    from core.config import logging_cfg
    from pathlib import Path

    wf_dir = Path("/opt/netorch/workflows")
    path = wf_dir / script_name
    try:
        definition = parse(path)
    except WorkflowParseError as e:
        raise ValueError(f"Cannot parse workflow '{script_name}': {e}") from e

    store.create(
        job_id=job_id,
        mode=JobMode.workflow,
        device_count=len(devices),
        incident=incident,
    )

    cancel_ev = threading.Event()
    with _active_lock:
        _active_jobs[job_id] = cancel_ev

    t = threading.Thread(
        target=_run_workflow_job,
        args=(definition, devices, parameters, job_id, options, cancel_ev, incident),
        daemon=True,
        name=f"wf-{job_id[:8]}",
    )
    t.start()
    log.info("workflow_submitted", job_id=job_id, workflow=script_name,
             device_count=len(devices))


def _run_workflow_job(
    definition: WorkflowDefinition,
    devices: list[DeviceEntry],
    parameters: dict[str, str],
    job_id: str,
    options,
    cancel_ev: threading.Event,
    incident: Optional[str],
) -> None:
    try:
        store.mark_running(job_id)
        context = build_context(definition, parameters)

        for step in definition.steps:
            if cancel_ev.is_set():
                log.info("workflow_cancelled_mid_step",
                         job_id=job_id, step=step.name)
                break
            try:
                _execute_step(
                    step=step,
                    devices=devices,
                    context=context,
                    job_id=job_id,
                    options=options,
                    cancel_ev=cancel_ev,
                )
            except _JobAbortError as exc:
                log.error("workflow_aborted", job_id=job_id,
                          step=step.name, reason=str(exc))
                store.mark_failed(job_id, reason=str(exc))
                return

        store.mark_complete(job_id)

    except Exception as e:
        log.exception("workflow_job_error", job_id=job_id, error=str(e))
        store.mark_failed(job_id, reason=str(e))
    finally:
        with _active_lock:
            _active_jobs.pop(job_id, None)


class _JobAbortError(RuntimeError):
    pass


def _execute_step(
    step: StepDefinition,
    devices: list[DeviceEntry],
    context: dict,
    job_id: str,
    options,
    cancel_ev: threading.Event,
) -> None:
    log.info("step_start", job_id=job_id, step=step.name, type=step.type)

    step_ctx: dict = {"devices": {}}

    if step.type == "shell" and step.run == "once":
        output, exit_code = _execute_shell_once(step, context, job_id)
        step_ctx["output"] = output
        step_ctx["exit_code"] = exit_code
        _store_step_output(job_id, step.name, host=None,
                           output=output, exit_code=exit_code)
        if exit_code != 0:
            raise _JobAbortError(
                f"shell/once step '{step.name}' failed with exit code "
                f"{exit_code}:\n{output[-500:]}"
            )

    elif step.type == "shell" and step.run == "per_device":
        active = [d for d in devices
                  if (d.host or f"[group:{d.group}]") not in context["failed_devices"]]
        results = _fan_out(
            _execute_shell_per_device,
            active, step, context, job_id, options,
        )
        for host, (out, code) in results.items():
            step_ctx["devices"][host] = {"output": out, "exit_code": code}
            _store_step_output(job_id, step.name, host=host,
                               output=out, exit_code=code)
            if code != 0:
                context["failed_devices"].add(host)

    else:
        # device-scoped step
        active = [d for d in devices
                  if (d.host or f"[group:{d.group}]") not in context["failed_devices"]]
        results = _fan_out(
            _execute_device_step,
            active, step, context, job_id, options,
        )
        for host, (out, code) in results.items():
            step_ctx["devices"][host] = {"output": out, "exit_code": code}
            _store_step_output(job_id, step.name, host=host,
                               output=out, exit_code=code)
            if code != 0:
                context["failed_devices"].add(host)
            _update_device_result(job_id, host, step, out, code)

    context["steps"][step.name] = step_ctx
    log.info("step_done", job_id=job_id, step=step.name,
             failed_devices=len(context["failed_devices"]))


def _fan_out(fn, devices, step, context, job_id, options) -> dict:
    """Run fn(device, step, context, job_id, options) for each device in parallel."""
    results: dict[str, tuple[str, int]] = {}
    if not devices:
        return results
    max_workers = min(options.max_workers, len(devices))
    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="wfstep") as pool:
        futures = {
            pool.submit(fn, d, step, context, job_id, options): d
            for d in devices
        }
        for fut in as_completed(futures):
            device = futures[fut]
            host = device.host or f"[group:{device.group}]"
            try:
                out, code = fut.result()
            except Exception as e:
                out, code = str(e), 1
            results[host] = (out, code)
    return results


def _execute_device_step(
    device: DeviceEntry,
    step: StepDefinition,
    context: dict,
    job_id: str,
    options,
) -> tuple[str, int]:
    """Execute a device-scoped step on a single device via SSH."""
    try:
        creds = resolve_credentials(
            host=device.host,
            group=device.group,
            platform_hint=device.platform,
        )
    except Exception as e:
        return str(e), 1

    merged = _build_step_vars(step, context, creds)
    timeout = options.timeout_per_device
    output_parts: list[str] = []

    try:
        driver = get_driver(creds, timeout=timeout)
        with driver:
            if step.type == "device_commands":
                cmds = [substitute_vars(c, merged) for c in (step.commands or [])]
                for cmd in cmds:
                    try:
                        out = driver.run_command(cmd)
                        output_parts.append(f"# {cmd}\n{out}")
                    except Exception as e:
                        output_parts.append(f"# {cmd}\nERROR: {e}")
                        return "\n".join(output_parts), 1

            elif step.type == "device_config":
                cmds = [substitute_vars(c, merged) for c in (step.commands or [])]
                try:
                    out = driver.run_config_commands(cmds)
                    output_parts.append(out)
                except Exception as e:
                    return str(e), 1

            elif step.type == "file_transfer":
                local  = substitute_vars(step.local_path or "", merged)
                remote = substitute_vars(step.remote_path or "", merged)
                try:
                    driver.transfer_file(local, remote)
                    output_parts.append(
                        f"Transferred {local} → {remote}"
                    )
                except Exception as e:
                    return str(e), 1
                if step.post_transfer_commands:
                    cmds = [substitute_vars(c, merged)
                            for c in step.post_transfer_commands]
                    try:
                        out = driver.run_config_commands(cmds)
                        output_parts.append(out)
                    except Exception as e:
                        output_parts.append(f"Post-transfer error: {e}")
                        return "\n".join(output_parts), 1

            elif step.type == "device_runbook":
                rb_name = substitute_vars(step.runbook or "", merged)
                rb_path = RUNBOOKS_DIR / rb_name
                if not rb_path.exists():
                    return f"Runbook not found: {rb_name}", 1
                rb_cmds = [
                    line.rstrip()
                    for line in rb_path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                for cmd in rb_cmds:
                    try:
                        out = driver.run_command(substitute_vars(cmd, merged))
                        output_parts.append(f"# {cmd}\n{out}")
                    except Exception as e:
                        output_parts.append(f"# {cmd}\nERROR: {e}")
                        return "\n".join(output_parts), 1

    except Exception as e:
        return str(e), 1

    return "\n".join(output_parts), 0


def _execute_shell_once(
    step: StepDefinition,
    context: dict,
    job_id: str,
) -> tuple[str, int]:
    """Run the shell script once on the relay server."""
    env = _make_env(context["vars"], job_id=job_id)
    _inject_step_outputs(env, context)
    script = substitute_vars(step.script or "", {"vars": context["vars"],
                                                  "steps": context["steps"]})
    return _run_bash(script, env)


def _execute_shell_per_device(
    device: DeviceEntry,
    step: StepDefinition,
    context: dict,
    job_id: str,
    options,
) -> tuple[str, int]:
    """Run the shell script once per device on the relay, with device env vars."""
    try:
        creds = resolve_credentials(
            host=device.host,
            group=device.group,
            platform_hint=device.platform,
        )
    except Exception as e:
        return str(e), 1

    merged = _build_step_vars(step, context, creds)
    env = _make_env(merged["vars"], job_id=job_id)
    _inject_device_env(env, creds)
    _inject_step_outputs(env, context)
    script = substitute_vars(step.script or "", merged)
    return _run_bash(script, env)


def _run_bash(script: str, env: dict) -> tuple[str, int]:
    try:
        proc = subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        out, _ = proc.communicate(timeout=300)
        return out.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        return "ERROR: script timed out after 300s", 1
    except Exception as e:
        return str(e), 1


def _make_env(vars_dict: dict, job_id: str = "") -> dict:
    env = os.environ.copy()
    for k, v in vars_dict.items():
        env[str(k).upper()] = str(v)
    if job_id:
        env["NETORCH_JOB_ID"] = job_id
    from core.config import server
    env["NETORCH_API_URL"] = f"http://localhost:{server.port}"
    try:
        from core.config import server as srv
        env["NETORCH_TOKEN"] = getattr(srv, "auth_token", "")
    except Exception:
        pass
    return env


def _inject_device_env(env: dict, creds: DeviceCredentials) -> None:
    env["TARGET_HOST"]         = creds.host or ""
    env["DEVICE_PLATFORM"]     = creds.platform or ""
    env["DEVICE_USERNAME"]     = creds.username or ""
    env["DEVICE_PASSWORD"]     = creds.password or ""
    env["DEVICE_ENABLE_SECRET"]= getattr(creds, "enable_secret", "") or ""
    env["DEVICE_GROUP"]        = getattr(creds, "group", "") or ""
    env["DEVICE_SSH_PORT"]     = str(getattr(creds, "port", 22) or 22)


def _inject_step_outputs(env: dict, context: dict) -> None:
    for step_name, step_data in context.get("steps", {}).items():
        key = "STEP_" + re.sub(r"\W+", "_", step_name.upper())
        if "output" in step_data:
            env[key + "_OUTPUT"] = str(step_data["output"])


def _build_step_vars(
    step: StepDefinition,
    context: dict,
    creds: Optional[DeviceCredentials] = None,
) -> dict:
    merged: dict[str, str] = {}
    merged.update(context.get("vars", {}))
    merged.update(step.vars)
    if creds:
        merged.update({
            "TARGET_HOST":          creds.host or "",
            "DEVICE_PLATFORM":      creds.platform or "",
            "DEVICE_USERNAME":      creds.username or "",
            "DEVICE_PASSWORD":      creds.password or "",
            "DEVICE_ENABLE_SECRET": getattr(creds, "enable_secret", "") or "",
            "DEVICE_GROUP":         getattr(creds, "group", "") or "",
            "DEVICE_SSH_PORT":      str(getattr(creds, "port", 22) or 22),
        })
    return {"vars": merged, "steps": context.get("steps", {})}


def _store_step_output(
    job_id: str,
    step_name: str,
    host: Optional[str],
    output: str,
    exit_code: int,
) -> None:
    from core.db import db
    db.insert_step_output(job_id, step_name, host, output, exit_code)


def _update_device_result(
    job_id: str,
    host: str,
    step: StepDefinition,
    output: str,
    exit_code: int,
) -> None:
    from api.schemas import DeviceStatus, CommandResult, DeviceResult
    ts = datetime.now(timezone.utc).isoformat()
    cmd_result = CommandResult(
        command=f"[step:{step.name}]",
        output=output,
        timestamp=ts,
        error=None if exit_code == 0 else f"Step exited {exit_code}",
    )
    result = DeviceResult(
        host=host,
        platform=None,
        status=DeviceStatus.success if exit_code == 0 else DeviceStatus.failed,
        duration_seconds=0,
        commands=[cmd_result],
        error=None if exit_code == 0 else f"Step '{step.name}' failed",
    )
    store.update_device(job_id, result)
