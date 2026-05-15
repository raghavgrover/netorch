"""
core/workflow_parser.py — Parse and validate workflow YAML files.

Workflow files are stored as .yaml under /opt/netorch/workflows/.
See PART 2 of the design doc for the full schema.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


VALID_STEP_TYPES = {
    "device_commands",
    "device_config",
    "device_runbook",
    "file_transfer",
    "shell",
}
VALID_RUN_VALUES = {"once", "per_device"}

# Regex for {{ VAR_NAME }} substitution
_VAR_RE = re.compile(r"\{\{\s*(\S+?)\s*\}\}")


@dataclass
class StepDefinition:
    name: str
    type: str
    commands: Optional[list[str]] = None
    local_path: Optional[str] = None
    remote_path: Optional[str] = None
    post_transfer_commands: Optional[list[str]] = None
    runbook: Optional[str] = None
    run: Optional[str] = None          # "once" | "per_device" (shell only)
    script: Optional[str] = None
    vars: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkflowDefinition:
    name: str
    description: str
    parameters: list[str]
    vars: dict[str, str]
    steps: list[StepDefinition]
    raw_content: str = ""


class WorkflowParseError(ValueError):
    pass


def parse(path: Path) -> WorkflowDefinition:
    """Read a .yaml workflow file and return a validated WorkflowDefinition."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise WorkflowParseError(f"Cannot read workflow file: {e}")

    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise WorkflowParseError(f"YAML parse error: {e}")

    if not isinstance(doc, dict):
        raise WorkflowParseError("Workflow file must be a YAML mapping.")

    name        = str(doc.get("name", path.stem))
    description = str(doc.get("description", ""))
    parameters  = _parse_parameters(doc.get("parameters", []))
    wf_vars     = _parse_vars(doc.get("vars", {}))
    steps       = _parse_steps(doc.get("steps", []))

    wf = WorkflowDefinition(
        name=name,
        description=description,
        parameters=parameters,
        vars=wf_vars,
        steps=steps,
        raw_content=raw,
    )
    _validate(wf)
    return wf


def _parse_parameters(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(p) for p in raw]
    raise WorkflowParseError("'parameters' must be a list of strings.")


def _parse_vars(raw) -> dict[str, str]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    raise WorkflowParseError("'vars' must be a key/value mapping.")


def _parse_steps(raw) -> list[StepDefinition]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise WorkflowParseError("'steps' must be a list.")
    steps = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise WorkflowParseError(f"Step {i+1} must be a mapping.")
        steps.append(_parse_step(item, i + 1))
    return steps


def _parse_step(item: dict, idx: int) -> StepDefinition:
    name = str(item.get("name", f"Step {idx}"))
    stype = str(item.get("type", ""))
    if not stype:
        raise WorkflowParseError(f"Step '{name}': 'type' is required.")

    def _str_list(key) -> Optional[list[str]]:
        v = item.get(key)
        if v is None:
            return None
        if isinstance(v, list):
            return [str(x) for x in v]
        return [str(v)]

    return StepDefinition(
        name=name,
        type=stype,
        commands=_str_list("commands"),
        local_path=str(item["local_path"]) if "local_path" in item else None,
        remote_path=str(item["remote_path"]) if "remote_path" in item else None,
        post_transfer_commands=_str_list("post_transfer_commands"),
        runbook=str(item["runbook"]) if "runbook" in item else None,
        run=str(item["run"]) if "run" in item else None,
        script=str(item["script"]) if "script" in item else None,
        vars=_parse_vars(item.get("vars", {})),
    )


def _validate(wf: WorkflowDefinition) -> None:
    for step in wf.steps:
        if step.type not in VALID_STEP_TYPES:
            raise WorkflowParseError(
                f"Step '{step.name}': unknown type '{step.type}'. "
                f"Valid types: {sorted(VALID_STEP_TYPES)}"
            )
        if step.type == "shell":
            if step.run not in VALID_RUN_VALUES:
                raise WorkflowParseError(
                    f"Step '{step.name}': shell step must have "
                    f"run: once|per_device (got {step.run!r})."
                )
            if not step.script:
                raise WorkflowParseError(
                    f"Step '{step.name}': shell step requires 'script'."
                )
        if step.type in ("device_commands", "device_config"):
            if not step.commands:
                raise WorkflowParseError(
                    f"Step '{step.name}': {step.type} requires 'commands'."
                )
        if step.type == "file_transfer":
            if not step.local_path or not step.remote_path:
                raise WorkflowParseError(
                    f"Step '{step.name}': file_transfer requires "
                    "'local_path' and 'remote_path'."
                )
        if step.type == "device_runbook":
            if not step.runbook:
                raise WorkflowParseError(
                    f"Step '{step.name}': device_runbook requires 'runbook'."
                )


def substitute_vars(template: str, context: dict) -> str:
    """
    Replace {{ VAR_NAME }} tokens in *template* using values from *context*.

    Nested dotted paths like {{ steps.Step1.output }} are supported.
    Missing keys are left as-is (no error).
    """
    def _resolve(m: re.Match) -> str:
        key = m.group(1)
        parts = key.split(".")
        val = context
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
            if val is None:
                return m.group(0)   # leave unreplaced
        return str(val)

    return _VAR_RE.sub(_resolve, template)


def build_context(wf: WorkflowDefinition, parameters: dict[str, str]) -> dict:
    """Build the initial execution context from workflow vars + user parameters."""
    merged_vars: dict[str, str] = {}
    merged_vars.update(wf.vars)
    merged_vars.update(parameters)
    return {
        "vars": merged_vars,
        "steps": {},
        "failed_devices": set(),
    }
