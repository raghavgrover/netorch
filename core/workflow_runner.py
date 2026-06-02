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

# Exit code sentinel: used internally to mark a step as skipped (when:/platform:
# condition was false). Never propagated to failed_devices.
_SKIP_CODE = -1

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
    is_shell = step.type in ("shell", "run_shell_script_locally")

    # ── when: check for shell steps (device steps check per-device in _execute_device_step)
    if step.when and is_shell:
        if not _evaluate_when(step.when, context):
            msg = f"Step skipped — when: condition not met: {step.when!r}"
            step_ctx.update({"output": msg, "exit_code": 0, "skipped": True})
            _store_step_output(job_id, step.name, host=None, output=msg, exit_code=0)
            context["steps"][step.name] = step_ctx
            log.info("step_skipped_when", job_id=job_id, step=step.name)
            return

    if is_shell and step.run == "once":
        output, exit_code = _execute_shell_once(step, context, job_id)
        if exit_code != 0:
            # Run rollback steps if configured
            rb_out = ""
            if step.on_error.rollback_steps:
                rb_out = _execute_rollback(
                    step.on_error.rollback_steps, context, job_id, options
                )
            full_output = output + (f"\n\n--- Rollback ---\n{rb_out}" if rb_out else "")
            step_ctx["output"]    = full_output
            step_ctx["exit_code"] = exit_code
            _store_step_output(job_id, step.name, host=None,
                               output=full_output, exit_code=exit_code)
            action  = step.on_error.action
            failure = step.on_error.message or (
                f"shell/once step '{step.name}' failed (exit {exit_code})"
            )
            if action == "continue":
                log.warning("step_failed_continue", job_id=job_id,
                            step=step.name, exit_code=exit_code)
            else:
                # "stop" and "rollback" both abort — rollback already ran above
                raise _JobAbortError(f"{failure}:\n{output[-500:]}")
        else:
            step_ctx["output"]    = output
            step_ctx["exit_code"] = exit_code
            _store_step_output(job_id, step.name, host=None,
                               output=output, exit_code=exit_code)

    elif is_shell and step.run == "per_device":
        active = [d for d in devices
                  if (d.host or f"[group:{d.group}]") not in context["failed_devices"]]
        results = _fan_out(
            _execute_shell_per_device,
            active, step, context, job_id, options,
        )
        failed_entries: list[DeviceEntry] = []
        for host, (out, code) in results.items():
            skipped = (code == _SKIP_CODE)
            actual  = 0 if skipped else code
            step_ctx["devices"][host] = {"output": out, "exit_code": actual,
                                         "skipped": skipped}
            _store_step_output(job_id, step.name, host=host, output=out, exit_code=actual)
            if not skipped and actual != 0:
                if step.on_error.action == "continue":
                    pass   # keep device active for subsequent steps
                else:
                    context["failed_devices"].add(host)
                    failed_entries.extend(d for d in active
                                          if (d.host or f"[group:{d.group}]") == host)
        # Run rollback against the devices that failed
        if failed_entries and step.on_error.rollback_steps:
            rb_out = _execute_rollback(
                step.on_error.rollback_steps, context, job_id, options,
                failed_devices=failed_entries,
            )
            for d in failed_entries:
                h = d.host or f"[group:{d.group}]"
                if h in step_ctx["devices"]:
                    step_ctx["devices"][h]["output"] += f"\n\n--- Rollback ---\n{rb_out}"
        _rollup_output(step_ctx)

    else:
        # device-scoped steps (device_commands, device_config, file_transfer,
        #                       device_runbook, wait_until)
        active = [d for d in devices
                  if (d.host or f"[group:{d.group}]") not in context["failed_devices"]]
        results = _fan_out(
            _execute_device_step,
            active, step, context, job_id, options,
        )
        failed_entries = []
        for host, (out, code) in results.items():
            skipped = (code == _SKIP_CODE)
            actual  = 0 if skipped else code
            step_ctx["devices"][host] = {"output": out, "exit_code": actual,
                                         "skipped": skipped}
            _store_step_output(job_id, step.name, host=host, output=out, exit_code=actual)
            if not skipped and actual != 0:
                if step.on_error.action == "continue":
                    pass   # keep device active for subsequent steps
                else:
                    context["failed_devices"].add(host)
                    failed_entries.extend(d for d in active
                                          if (d.host or f"[group:{d.group}]") == host)
            if not skipped:
                _update_device_result(job_id, host, step, out, actual)
        # Run rollback against the devices that failed
        if failed_entries and step.on_error.rollback_steps:
            rb_out = _execute_rollback(
                step.on_error.rollback_steps, context, job_id, options,
                failed_devices=failed_entries,
            )
            for d in failed_entries:
                h = d.host or f"[group:{d.group}]"
                if h in step_ctx["devices"]:
                    step_ctx["devices"][h]["output"] += f"\n\n--- Rollback ---\n{rb_out}"
        _rollup_output(step_ctx)

    # ── register: — apply after all output is collected and rolled up
    if step.register:
        _apply_register(step_ctx, step.register)

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


def _execute_rollback(
    rollback_steps: list,
    context: dict,
    job_id: str,
    options,
    failed_devices: Optional[list] = None,
) -> str:
    """
    Execute rollback steps after a step failure and return combined output.

    ``failed_devices`` is the list of DeviceEntry objects that failed.
    If None (shell/once parent), rollback shell steps run once; device
    rollback steps are skipped (no target).
    If provided, device-scoped rollback steps run against those devices.

    Rollback steps never abort the job themselves — all errors are captured
    in the output and execution continues through all rollback steps.
    """
    output_parts: list[str] = []
    for rb in rollback_steps:
        label = f"[rollback: {rb.name}]"
        try:
            is_shell = rb.type in ("shell", "run_shell_script_locally")
            if is_shell and rb.run == "once":
                out, code = _execute_shell_once(rb, context, job_id)
                output_parts.append(f"{label}\n{out}")
            elif is_shell and rb.run == "per_device" and failed_devices:
                results = _fan_out(
                    _execute_shell_per_device,
                    failed_devices, rb, context, job_id, options,
                )
                for host, (out, code) in results.items():
                    output_parts.append(f"{label} [{host}]\n{out}")
            elif not is_shell and failed_devices:
                results = _fan_out(
                    _execute_device_step,
                    failed_devices, rb, context, job_id, options,
                )
                for host, (out, code) in results.items():
                    output_parts.append(f"{label} [{host}]\n{out}")
            else:
                output_parts.append(f"{label} skipped — no matching devices")
        except Exception as e:
            output_parts.append(f"{label} ERROR: {e}")
    return "\n\n".join(output_parts)


def _evaluate_when(condition: str, context: dict, host: Optional[str] = None) -> bool:
    """
    Evaluate a ``when:`` condition string.  Returns True if the step should run.

    Supported forms:
      steps.STEP.output contains "STRING"
      steps.STEP.output matches "REGEX"
      steps.STEP.exit_code == 0
      vars.PARAM == "VALUE"
      ... (== and != comparisons)
    For device steps ``host`` is provided and per-device output is checked first.
    """
    if not condition:
        return True
    c = condition.strip()
    for op in (" contains ", " matches ", " == ", " != "):
        if op in c:
            lhs_str, _, rhs_str = c.partition(op)
            lhs_val = _resolve_lhs(lhs_str.strip(), context, host)
            rhs_val  = rhs_str.strip().strip("\"'")
            if op == " contains ":
                return rhs_val in lhs_val
            if op == " matches ":
                try:
                    return bool(re.search(rhs_val, lhs_val))
                except re.error:
                    return False
            if op == " == ":
                return lhs_val == rhs_val
            if op == " != ":
                return lhs_val != rhs_val
    # No operator — treat as truthy existence check
    return bool(_resolve_lhs(c, context, host))


def _resolve_lhs(path: str, context: dict, host: Optional[str]) -> str:
    """
    Resolve a dotted path such as ``steps.check.output`` from context.
    When ``host`` is provided and the path starts with ``steps.``, the
    per-device output for that host is tried before the step-level rollup.
    """
    parts = path.split(".")
    # Per-device awareness: steps.STEP.ATTR
    if len(parts) >= 3 and parts[0] == "steps" and host:
        step_data  = context.get("steps", {}).get(parts[1], {})
        attr       = parts[2]
        device_row = step_data.get("devices", {}).get(host, {})
        if attr in device_row:
            val: object = device_row[attr]
            for p in parts[3:]:
                val = val.get(p, "") if isinstance(val, dict) else ""
            return str(val) if val is not None else ""
        # Fall back to step-level rollup
        val = step_data.get(attr, "")
        for p in parts[3:]:
            val = val.get(p, "") if isinstance(val, dict) else ""
        return str(val) if val is not None else ""
    # Generic path traversal
    val = context
    for part in parts:
        val = val.get(part) if isinstance(val, dict) else None
        if val is None:
            return ""
    return str(val) if val is not None else ""


def _check_until(condition: str, output: str) -> bool:
    """
    Check a ``until:`` condition against a single command's output.
    Supported: ``output contains "X"`` and ``output matches "REGEX"``.
    """
    c = condition.strip()
    if " contains " in c:
        _, _, rhs = c.partition(" contains ")
        return rhs.strip().strip("\"'") in output
    if " matches " in c:
        _, _, rhs = c.partition(" matches ")
        try:
            return bool(re.search(rhs.strip().strip("\"'"), output))
        except re.error:
            return False
    # Bare string — just check non-empty output
    return bool(output.strip())


def _rollup_output(step_ctx: dict) -> None:
    """
    Create a step-level ``output`` key from per-device results so that
    subsequent steps can reference ``{{ steps.STEP.output }}`` regardless
    of whether the previous step was shell/once or device-scoped.
    Also sets ``step_ctx["exit_code"]`` (0 only when all devices succeeded).
    """
    devices = step_ctx.get("devices", {})
    if not devices:
        return
    parts = []
    for host, data in devices.items():
        if data.get("skipped"):
            continue
        out = data.get("output", "")
        if out:
            parts.append(f"[{host}]\n{out}")
    step_ctx["output"]    = "\n\n".join(parts)
    step_ctx["exit_code"] = 0 if all(
        d.get("exit_code", 1) == 0 for d in devices.values()
        if not d.get("skipped")
    ) else 1


def _apply_register(step_ctx: dict, register: dict) -> None:
    """
    Apply ``register:`` regex patterns to the step's rolled-up output
    and store named capture groups in ``step_ctx["captured"]``.
    Captured values are reachable via ``{{ steps.STEP.captured.VAR }}``.
    """
    if not register:
        return
    output = step_ctx.get("output", "")
    captured: dict[str, str] = {}
    for var_name, pattern in register.items():
        try:
            m = re.search(pattern, output, re.MULTILINE)
            if m:
                captured[var_name] = (
                    m.group(1) if m.lastindex and m.lastindex >= 1
                    else m.group(0)
                )
        except re.error:
            pass  # bad regex — skip silently
    if captured:
        step_ctx["captured"] = captured


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

    host = creds.host or device.host or ""

    # ── platform: filter
    if step.platform and creds.platform not in step.platform:
        return (
            f"Skipped — platform '{creds.platform}' not in {step.platform}",
            _SKIP_CODE,
        )

    # ── when: per-device check
    if step.when and not _evaluate_when(step.when, context, host=host):
        return (
            f"Skipped — when: {step.when!r} evaluated false for {host}",
            _SKIP_CODE,
        )

    merged  = _build_step_vars(step, context, creds)
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

            elif step.type == "wait_until":
                cmd          = substitute_vars((step.commands or [""])[0], merged)
                timeout_secs = min(
                    step.timeout or options.timeout_per_device,
                    options.timeout_per_device,
                )
                interval     = max(1, step.interval)
                start        = time.monotonic()
                attempt      = 0
                matched      = False

                while True:
                    if time.monotonic() - start >= timeout_secs:
                        break
                    attempt += 1
                    try:
                        out = driver.run_command(cmd)
                    except Exception as e:
                        out = f"ERROR: {e}"
                    output_parts.append(f"# [{attempt}] {cmd}\n{out}")
                    if _check_until(step.until or "", out):
                        matched = True
                        break
                    remaining = timeout_secs - (time.monotonic() - start)
                    if remaining <= 0:
                        break
                    time.sleep(min(interval, remaining))

                if not matched:
                    elapsed = round(time.monotonic() - start, 1)
                    if step.on_timeout == "continue":
                        output_parts.append(
                            f"# Timed out after {elapsed}s — continuing"
                        )
                    else:
                        output_parts.append(f"# Timed out after {elapsed}s")
                        return "\n".join(output_parts), 1

            elif step.type == "file_transfer":
                local  = substitute_vars(step.local_path or "", merged)
                remote = substitute_vars(step.remote_path or "", merged)
                try:
                    driver.transfer_file(local, remote)
                    output_parts.append(f"Transferred {local} → {remote}")
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
    """
    Run the shell script once per device on the relay, with device env vars.

    Credential lookup is attempted but NOT required — shell/per_device runs
    locally on the relay, not via SSH. If vault lookup fails we still inject
    basic device info (host, platform, group) so the script can reference them.
    """
    creds = None
    try:
        creds = resolve_credentials(
            host=device.host,
            group=device.group,
            platform_hint=device.platform,
        )
    except Exception:
        pass   # credentials optional for relay-side scripts

    # ── when: per-device check (same semantics as device steps)
    if step.when:
        dev_host = (creds.host if creds else None) or device.host or ""
        if not _evaluate_when(step.when, context, host=dev_host):
            return (
                f"Skipped — when: {step.when!r} evaluated false for {dev_host}",
                _SKIP_CODE,
            )

    merged = _build_step_vars(step, context, creds)
    env = _make_env(merged["vars"], job_id=job_id)
    if creds:
        _inject_device_env(env, creds)
    else:
        # Inject what we know without vault credentials
        env["TARGET_HOST"]     = device.host or ""
        env["DEVICE_PLATFORM"] = device.platform or ""
        env["DEVICE_GROUP"]    = device.group or ""
        env["DEVICE_SSH_PORT"] = "22"
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
