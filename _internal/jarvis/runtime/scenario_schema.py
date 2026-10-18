"""Pure, side-effect-free validation for saved scenarios.

Unknown metadata is intentionally retained by editors. Validation never opens a
file, checks a provider, resolves a monitor, or runs an executable.
"""
from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

MAX_STEPS = 100
MAX_SECONDS = 300.0
MAX_WAIT = 30.0
MAX_TIMEOUT = 120.0
MAX_MONITOR = 32
MAX_TEXT = 262144
MAX_ISSUES = 100


@dataclass(frozen=True)
class ScenarioIssue:
    code: str
    field: str
    step_index: int = 0
    value: str = ""


@dataclass(frozen=True)
class ScenarioValidation:
    valid: bool
    issues: list[ScenarioIssue]
    step_count: int
    estimated_wait_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def step_kind(step: dict) -> str:
    return str(step.get("type", "open") or "open").strip().casefold()


def command_argv(step: dict) -> list[str]:
    """Legacy strings mean one executable, never shell syntax or eval."""
    value = step.get("command", [])
    return [value] if isinstance(value, str) else value


def failure_policy(scenario: dict, step: dict) -> str:
    if "on_failure" in step:
        return step["on_failure"]
    if "required" in step:
        return "stop" if step["required"] else "continue"
    return scenario.get("on_failure", "continue")


def validate_scenario(raw: Any) -> ScenarioValidation:
    issues: list[ScenarioIssue] = []
    waits = 0.0
    text_size, too_large = 0, False
    def issue(code, field, index=0, value=""):
        if len(issues) < MAX_ISSUES:
            issues.append(ScenarioIssue(code, field, index, str(value)[:100]))
    def number(value, low, high, field, index=0, integer=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value) or (integer and int(value) != value):
            issue("number", field, index, f"{low:g}–{high:g}")
            return False
        return True
    def text(value, limit, field, index=0, required=True):
        nonlocal text_size, too_large
        if isinstance(value, str):
            text_size += len(value)
            if text_size > MAX_TEXT:
                if not too_large:
                    issue("size", "steps", value=MAX_TEXT)
                    too_large = True
                return False
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value) or (required and not value.strip()):
            issue("text", field, index, limit)
            return False
        return True
    if not isinstance(raw, dict):
        return ScenarioValidation(False, [ScenarioIssue("object", "scenario")], 0, 0)
    text(raw.get("name", ""), 200, "name")
    if "id" in raw:
        text(raw["id"], 200, "id")
    text(raw.get("phrase", ""), 500, "phrase", required=False)
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        issue("boolean", "enabled")
    if raw.get("on_failure", "continue") not in ("stop", "continue"):
        issue("policy", "on_failure")
    number(raw.get("max_seconds", MAX_SECONDS), 1, MAX_SECONDS, "max_seconds")
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 64:
        issue("aliases", "aliases")
    else:
        for alias in aliases:
            text(alias, 500, "aliases")
    steps = raw.get("steps", [])
    if not isinstance(steps, list):
        return ScenarioValidation(False, issues + [ScenarioIssue("steps", "steps")], 0, 0)
    if not 1 <= len(steps) <= MAX_STEPS:
        issue("steps", "steps", value=MAX_STEPS)
    enabled_count = 0
    for index, step in enumerate(steps[:MAX_STEPS + 1], 1):
        if not isinstance(step, dict):
            issue("object", "step", index)
            continue
        enabled_count += step.get("enabled", True) is not False
        if "enabled" in step and not isinstance(step["enabled"], bool):
            issue("boolean", "enabled", index)
        if "required" in step and not isinstance(step["required"], bool):
            issue("boolean", "required", index)
        if failure_policy(raw, step) not in ("stop", "continue"):
            issue("policy", "on_failure", index)
        if "label" in step:
            text(step["label"], 200, "label", index, required=False)
        kind = step_kind(step)
        if kind == "open":
            text(step.get("path", ""), 8192, "path", index)
            number(step.get("monitor", 0), 0, MAX_MONITOR, "monitor", index, integer=True)
            number(step.get("launch_wait", 0.7), 0, 2, "launch_wait", index)
            text(step.get("window_title", ""), 500, "window_title", index, required=False)
        elif kind == "wait":
            value = step.get("seconds", 0.5)
            if number(value, 0, MAX_WAIT, "seconds", index) and step.get("enabled", True):
                waits += value
        elif kind == "command":
            argv = command_argv(step)
            if not isinstance(argv, list) or not argv or len(argv) > 128:
                issue("argv", "command", index)
            else:
                for n, arg in enumerate(argv):
                    text(arg, 8192, "command", index, required=n == 0)
            number(step.get("timeout", 15), 1, MAX_TIMEOUT, "timeout", index)
        else:
            issue("kind", "type", index, kind)
    if steps and not enabled_count:
        issue("empty", "steps")
    if "inputs" in raw or any(isinstance(step, dict) and "bind" in step for step in steps[:MAX_STEPS + 1]):
        # NEO 188 NEO-SCENARIO-001: typed run inputs; legacy scenarios never reach this.
        from jarvis.runtime.scenario_inputs188 import validation_issues
        for code, field, index, value in validation_issues(raw):
            issue(code, field, index, value)
    budget = raw.get("max_seconds", MAX_SECONDS)
    if isinstance(budget, (int, float)) and not isinstance(budget, bool) and waits > budget:
        issue("duration", "max_seconds")
    return ScenarioValidation(not issues, issues, len(steps), waits)


def copy_scenario(raw: dict) -> dict:
    """Lossless copy; no migration discards extension keys or legacy fields."""
    return deepcopy(raw)
