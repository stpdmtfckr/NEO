# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import os
import hashlib
import hmac
import json
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jarvis.runtime.cancellation import OperationCancelled, cancellation_manager
from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.runtime.scenario_schema import (MAX_SECONDS, command_argv, failure_policy,
                                           step_kind, validate_scenario)


@dataclass
class ScenarioResult:
    name: str
    phrase: str
    success: bool
    steps: list[dict[str, Any]]
    run_id: str = ""
    status: str = "completed"
    error: str = ""
    summary: dict = field(default_factory=dict)
    outcomes: list[dict] = field(default_factory=list)
    retry_of: str = ""


class ScenarioCleanupError(RuntimeError):
    """An owned process did not acknowledge termination; never continue steps."""


_LOCK = threading.RLock()
_ACTIVE: dict[str, dict] = {}
_SUBSCRIBERS: set[Callable] = set()
_HISTORY: deque[dict] = deque(maxlen=300)
# Retry authority is session-only, bounded, and separate from display events.
_RUN_RECORDS: OrderedDict[str, dict] = OrderedDict()
MAX_RUN_RECORDS = 100
MAX_ACTIVE_PER_SCOPE = 1
MAX_ACTIVE_TOTAL = 4
# Even a command that spawns a descendant holding a pipe cannot leak unlimited
# reader threads. Its readers retain these slots until their pipes close.
_OUTPUT_SLOTS = threading.BoundedSemaphore(8)
_HIGH_OPEN_SUFFIXES = {".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".vbs", ".vbe",
                       ".js", ".jse", ".wsf", ".wsh", ".py", ".sh", ".lnk", ".url",
                       ".hta", ".pif", ".scr", ".reg", ".msi", ".msp", ".mst", ".cpl", ".com"}


def _scope(config):
    root = getattr(getattr(config, "paths", None), "root", None)
    return str(root) if root is not None else str(id(config))


def subscribe_scenarios(callback: Callable[[dict], None]) -> None:
    """Callbacks run on the producer thread; GUI consumers must use Qt signals."""
    with _LOCK:
        _SUBSCRIBERS.add(callback)


def unsubscribe_scenarios(callback: Callable[[dict], None]) -> None:
    with _LOCK:
        _SUBSCRIBERS.discard(callback)


def scenario_events(config=None) -> list[dict]:
    with _LOCK:
        return [deepcopy(e) for e in _HISTORY if config is None or e["scope"] == _scope(config)]


def active_scenarios(config=None) -> list[dict]:
    with _LOCK:
        return [deepcopy(r["event"]) for r in _ACTIVE.values()
                if config is None or r["event"]["scope"] == _scope(config)]


def cancel_scenario(run_id: str) -> bool:
    """Cancel a currently owned run, never an unrelated process or generation."""
    with _LOCK:
        running = _ACTIVE.get(str(run_id))
        if running is None:
            return False
        running["cancel"].set()
        return True


def _publish(event, callback=None):
    with _LOCK:
        _HISTORY.append(deepcopy(event))
        if event["run_id"] in _ACTIVE:
            _ACTIVE[event["run_id"]]["event"] = deepcopy(event)
        callbacks = tuple(_SUBSCRIBERS)
    for listener in (*callbacks, *((callback,) if callback else ())):
        try:
            listener(deepcopy(event))
        except Exception:
            # A closed GUI or observer must never break execution/cleanup.
            pass


def _open_path(path: str) -> dict[str, Any]:
    p = str(path or "").strip()
    if not p:
        return {"success": False, "error": "empty path", "effect_state": "none"}
    try:
        if os.name == "nt":
            os.startfile(p)
        else:
            subprocess.Popen([p])
        return {"success": True, "path": p, "effect_state": "opened"}
    except Exception as exc:
        # A launcher exception alone cannot prove that no handler/app started.
        return {"success": False, "path": p, "error": str(exc), "effect_state": "unknown"}


def _move_opened_window(path: str, monitor_number: int, title_hint: str = "", check=None) -> dict[str, Any]:
    check = check or (lambda: None)
    def pause(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            check()
            time.sleep(min(0.05, max(0, end - time.monotonic())))
    if monitor_number <= 0:
        return {"moved": False}
    if os.name != "nt":
        return {"moved": False, "error": "Window placement is only available on Windows"}
    try:
        import mss
        import pygetwindow as gw
        with mss.mss() as sct:
            monitors=list(sct.monitors[1:])
        if monitor_number > len(monitors):
            return {"moved": False, "error": f"monitor {monitor_number} unavailable"}
        # An explicit title owns the target; the executable stem must not
        # select a different window ahead of it. Never move an arbitrary match.
        hint = str(title_hint or "").strip().casefold() or Path(str(path)).stem.casefold()
        target=None
        deadline=time.monotonic()+4.0
        while time.monotonic()<deadline and target is None:
            check()
            windows=[w for w in gw.getAllWindows() if str(getattr(w,"title","") or "").strip()]
            exact = [w for w in windows if str(w.title).strip().casefold() == hint] if hint else []
            matches = exact or ([w for w in windows if hint in str(w.title).casefold()] if hint else [])
            if len(matches) > 1:
                return {"moved": False, "error": "Multiple matching windows; specify a unique window title"}
            if matches:
                target = matches[0]
            if target is None: pause(0.15)
        if target is None: return {"moved":False,"error":"window not found"}
        mon=monitors[monitor_number-1]
        was_max=bool(getattr(target,"isMaximized",False))
        if was_max:
            try: target.restore(); pause(0.08)
            except (OperationCancelled, TimeoutError): raise
            except Exception: pass
        width=max(320,min(int(getattr(target,"width",1000) or 1000),int(mon["width"]*0.92)))
        height=max(240,min(int(getattr(target,"height",700) or 700),int(mon["height"]*0.92)))
        x=int(mon["left"]+(mon["width"]-width)/2); y=int(mon["top"]+(mon["height"]-height)/2)
        check()
        target.moveTo(x,y)
        check()
        if not was_max: target.resizeTo(width,height)
        else:
            pause(0.08); target.maximize()
        return {"moved":True,"monitor":monitor_number,"title":str(target.title)}
    except (OperationCancelled, TimeoutError):
        raise
    except Exception as exc:
        return {"moved":False,"error":str(exc)}


def find_scenario(config: Any, name_or_phrase: str) -> dict:
    value = str(name_or_phrase or "").casefold().strip()
    scenarios = config.get("scenarios.items", []) or []
    if not isinstance(scenarios, list):
        raise ValueError("Invalid scenario collection")
    matching, by_id, normalized_matches = [], [], []
    from jarvis.runtime.semantics import norm as phrase_norm
    normalized = phrase_norm(str(name_or_phrase or ""))
    for raw in scenarios:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("id", "")).casefold() == value and value:
            by_id.append(raw)
        aliases = raw.get("aliases", [])
        aliases = aliases if isinstance(aliases, list) else []
        names = [raw.get("name", ""), raw.get("phrase", ""), *aliases]
        if value and value in {str(x).casefold().strip() for x in names if str(x).strip()}:
            matching.append(raw)
        if normalized and any(phrase_norm(str(name)) == normalized for name in names if str(name).strip()):
            normalized_matches.append(raw)
    # Prefer literal identity. Normalization is only a unique-owner fallback.
    matching = by_id or matching or normalized_matches
    if not matching:
        raise ValueError(f"Сценарий не найден: {name_or_phrase}")
    if len(matching) > 1:
        raise ValueError("Several scenarios match; use a unique scenario ID")
    return matching[0]


def scenario_risk(config: Any, args: dict):
    from jarvis.security import Risk
    action = str(args.get("action", "")).casefold()
    if action not in ("run", "retry"):
        return Risk.SAFE
    if action == "retry":
        with _LOCK:
            _retry_plan(config, args)  # Never downgrade an invalid/stale retry.
    try:
        chosen = deepcopy(find_scenario(config, str(args.get("id") or args.get("name", ""))))
    except ValueError:
        if args.get("fingerprint"):
            raise ValueError("Scenario changed before risk assessment")
        return Risk.SAFE  # an unbound/missing target cannot execute
    if args.get("fingerprint") and args["fingerprint"] != scenario_fingerprint(chosen):
        raise ValueError("Scenario changed before risk assessment")
    if args.get("id") and args.get("name"):
        aliases = chosen.get("aliases", [])
        aliases = aliases if isinstance(aliases, list) else []
        names = {str(x).casefold().strip() for x in [chosen.get("name", ""), chosen.get("phrase", ""), *aliases]}
        if str(args["name"]).casefold().strip() not in names:
            raise ValueError("Scenario ID and name refer to different definitions")
    from jarvis.runtime.scenario_inputs188 import all_target_paths
    for step in chosen.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        # Windows strips trailing dots/spaces on ordinary file paths. Classify
        # the same effective suffix so a script cannot hide behind whitespace.
        path = str(step.get("path", "")).strip().rstrip(" .")
        if step_kind(step) == "command" or Path(path).suffix.casefold() in _HIGH_OPEN_SUFFIXES:
            return Risk.HIGH
    # NEO 188 NEO-SCENARIO-001: every object a target input may choose is classified too.
    if any(Path(str(p).strip().rstrip(" .")).suffix.casefold() in _HIGH_OPEN_SUFFIXES for p in all_target_paths(chosen)):
        return Risk.HIGH
    return Risk.MEDIUM


def scenario_fingerprint(raw: dict) -> str:
    """Bind approval to every field of the exact JSON scenario definition."""
    encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dry_run_scenario(config: Any, name_or_phrase: str) -> dict:
    """Validation only: no actions, path existence checks, or provider probes."""
    chosen = deepcopy(find_scenario(config, name_or_phrase))
    report = validate_scenario(chosen).to_dict()
    report.update(id=chosen.get("id", ""), name=chosen.get("name", ""),
                  enabled=chosen.get("enabled", True), dry_run=True)
    return report


def _command(step, check, deadline):
    """No shell. Poll a single owned child; bounded, continuously drained output."""
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    slots = _OUTPUT_SLOTS
    readers, acquired, started_readers = [], 0, set()
    proc = None
    try:
        for _ in tails:
            if not slots.acquire(blocking=False):
                raise RuntimeError("Scenario process output limit reached; wait for earlier commands to finish")
            acquired += 1
        check()
        proc = subprocess.Popen(command_argv(step), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=False, **hidden_process_kwargs())
        def drain(stream, key):
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    tails[key].extend(chunk)
                    del tails[key][:-8192]
            except (OSError, ValueError):
                pass
            finally:
                try: stream.close()
                except Exception: pass
                slots.release()
        for key in tails:
            reader = threading.Thread(target=drain, args=(getattr(proc, key), key), daemon=True,
                                      name="neo-scenario-output")
            reader.start()
            readers.append(reader)
            started_readers.add(key)
        end = min(deadline, time.monotonic() + float(step.get("timeout", 15)))
        while True:
            check()
            code = proc.poll()
            if code is not None:
                break
            if time.monotonic() >= end:
                raise TimeoutError("Scenario command timed out")
            time.sleep(min(0.05, max(0, end - time.monotonic())))
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.5)
            except Exception:
                try: proc.kill()
                except Exception: pass
                try: proc.wait(timeout=0.5)
                except Exception: pass
        for reader in readers:
            reader.join(timeout=0.1)
        for _ in range(acquired - len(started_readers)):
            slots.release()
        if proc is not None:
            for key in tails.keys() - started_readers:
                try: getattr(proc, key).close()
                except Exception: pass
            if proc.poll() is None:
                raise ScenarioCleanupError("Could not confirm termination of the owned scenario command")
    return {"success": code == 0, "returncode": code,
            **{key: bytes(value).decode("utf-8", errors="replace")[-2000:] for key, value in tails.items()}}


def _latest_record(config, scenario_id):
    scope = _scope(config)
    return next((record for record in reversed(_RUN_RECORDS.values())
                 if record["scope"] == scope and record["scenario_id"].casefold() == str(scenario_id).casefold()), None)


def latest_scenario_result(config, scenario_id):
    """A display snapshot contains no executable authority or mutable references."""
    with _LOCK:
        record = _latest_record(config, scenario_id)
        if record is None:
            return None
        return deepcopy({key: record[key] for key in
                         ("run_id", "scenario_id", "fingerprint", "status", "outcomes", "summary", "retry_of")})


def _retry_reason(record, outcome):
    if record["status"] == "cancelled":
        return "retry_cancelled"
    if record["status"] not in ("failed", "partial") or outcome.get("success") or outcome.get("not_run") or outcome.get("skipped"):
        return "retry_unavailable"
    if outcome.get("type") == "command":
        return "retry_command"
    if outcome.get("placement_requested") or outcome.get("placement") is not None or outcome.get("retry_mode") == "placement":
        return "retry_placement"
    if outcome.get("opened") or outcome.get("effect_state") != "none" or not outcome.get("retry_mode"):
        return "retry_unknown"
    return ""


def _retry_plan(config, args):
    """Called with _LOCK; validate exact run, step, profile, definition and latest run."""
    record = _RUN_RECORDS.get(str(args.get("run_id", "")))
    index = args.get("step_index")
    if (record is None or record["scope"] != _scope(config)
            or record["consumed"] or isinstance(index, bool) or not isinstance(index, int)
            or args.get("id") != record["scenario_id"]
            or args.get("fingerprint") != record["fingerprint"]
            or _latest_record(config, record["scenario_id"]) is not record):
        raise ValueError("retry_unavailable")
    try:
        source = find_scenario(config, record["scenario_id"])
        unchanged = scenario_fingerprint(source) == record["fingerprint"]
    except (ValueError, TypeError):
        unchanged = False
    if not unchanged or source.get("enabled", True) is False:
        raise ValueError("retry_unavailable")
    outcome = next((row for row in record["outcomes"] if row.get("step_index") == index), None)
    if outcome is None:
        raise ValueError("retry_unavailable")
    steps = source.get("steps", [])
    if not 1 <= index <= len(steps) or not isinstance(steps[index - 1], dict):
        raise ValueError("retry_unavailable")
    step = steps[index - 1]
    kind, mode = step_kind(step), outcome.get("retry_mode")
    if kind == "command":
        raise ValueError("retry_command")
    reason = _retry_reason(record, outcome)
    if reason:
        raise ValueError(reason)
    if (kind != outcome.get("type") or step.get("enabled", True) is False
            or (mode == "open" and (kind != "open" or outcome.get("opened")))
            or (mode == "wait" and kind != "wait")
            or mode not in ("open", "wait")):
        raise ValueError("retry_unavailable")
    return record, deepcopy(outcome)


def scenario_retry_request(config, run_id, step_index):
    """Prepare an immutable request for the normal risk/confirmation/tool path."""
    with _LOCK:
        record = _RUN_RECORDS.get(str(run_id))
        if record is None:
            raise ValueError("retry_unavailable")
        args = dict(action="retry", id=record["scenario_id"], run_id=str(run_id),
                    step_index=step_index, fingerprint=record["fingerprint"])
        _retry_plan(config, args)
        return args


def retry_scenario(config, arguments, *, on_event=None):
    manager = cancellation_manager()
    with manager.bound_generation(manager.current_generation()):
        with _LOCK:
            record, _ = _retry_plan(config, arguments)
            # NEO 188 NEO-SCENARIO-001: a retry repeats the step with THAT run's values.
            inputs = deepcopy(record.get("inputs"))
        return _run_scenario_bound(config, record["scenario_id"], on_event=on_event,
                                   expected_fingerprint=arguments["fingerprint"], retry_args=deepcopy(arguments),
                                   inputs=inputs, expected_inputs=record.get("inputs_fingerprint"))


def run_scenario(config: Any, name_or_phrase: str, *, on_event=None, expected_fingerprint=None,
                 inputs=None, expected_inputs=None) -> ScenarioResult:
    # Capture even an unbound caller's epoch once. A later reset in another
    # thread must not resurrect this run after Stop. Restore pooled-thread state.
    manager = cancellation_manager()
    with manager.bound_generation(manager.current_generation()):
        return _run_scenario_bound(config, name_or_phrase, on_event=on_event,
                                   expected_fingerprint=expected_fingerprint, inputs=inputs, expected_inputs=expected_inputs)


def _run_scenario_bound(config: Any, name_or_phrase: str, *, on_event=None, expected_fingerprint=None, retry_args=None,
                        inputs=None, expected_inputs=None) -> ScenarioResult:
    source = find_scenario(config, name_or_phrase)
    chosen = deepcopy(source)
    name, phrase = str(chosen.get("name", "") or name_or_phrase), str(chosen.get("phrase", "") or "")
    report = validate_scenario(chosen)
    if not report.valid:
        return ScenarioResult(name, phrase, False,
                              [{"type": "validation", "success": False, **report.to_dict()}],
                              status="invalid", error="Scenario validation failed")
    fingerprint = scenario_fingerprint(chosen)
    from jarvis.runtime import scenario_inputs188
    run_values, inputs_fp = None, ""
    if scenario_inputs188.has_inputs(chosen):
        # NEO 188 NEO-SCENARIO-001: resolve this run's values into a copy at the execution
        # boundary again; the approval must cover the same template AND the same values.
        resolution = scenario_inputs188.resolve(chosen, inputs)
        if not resolution.complete:
            return ScenarioResult(name, phrase, False, [], status="inputs",
                                  error="Scenario inputs are missing or invalid")
        inputs_fp = scenario_inputs188.inputs_fingerprint(fingerprint, resolution.values)
        try:
            same_values = expected_inputs is not None and hmac.compare_digest(str(expected_inputs), inputs_fp)
        except (ValueError, TypeError):
            same_values = False
        if not same_values:
            return ScenarioResult(name, phrase, False, [], status="changed",
                                  error="Scenario inputs changed after confirmation; confirm the updated values")
        chosen, run_values = resolution.effective, dict(resolution.values)
        report = validate_scenario(chosen)
        if not report.valid:
            return ScenarioResult(name, phrase, False,
                                  [{"type": "validation", "success": False, **report.to_dict()}],
                                  status="invalid", error="Scenario validation failed")
    elif inputs:
        return ScenarioResult(name, phrase, False, [], status="inputs", error="This scenario has no inputs")
    if expected_fingerprint is not None:
        try:
            matches = hmac.compare_digest(str(expected_fingerprint), fingerprint)
        except (ValueError, TypeError):
            matches = False
        if not matches:
            return ScenarioResult(name, phrase, False, [], status="changed",
                                  error="Scenario changed after confirmation; confirm the updated steps")
    if chosen.get("enabled", True) is False:
        return ScenarioResult(name, phrase, False, [], status="disabled", error="Scenario is disabled")
    run_id = uuid.uuid4().hex
    scope, scenario_id = _scope(config), str(chosen.get("id") or name.casefold())
    local_cancel, global_cancel = threading.Event(), cancellation_manager()
    started = time.monotonic()
    deadline = started + float(chosen.get("max_seconds", MAX_SECONDS))
    total = len(chosen["steps"])
    base = dict(run_id=run_id, scenario_id=scenario_id, name=name, scope=scope,
                fingerprint=fingerprint, state="started", step_index=0, total=total,
                kind="", elapsed=0.0, error="", retry_of=str((retry_args or {}).get("run_id", "")))
    previous = None
    with _LOCK:
        if (any(r["key"] == (scope, scenario_id.casefold()) for r in _ACTIVE.values())
                or len(_ACTIVE) >= MAX_ACTIVE_TOTAL
                or sum(r["key"][0] == scope for r in _ACTIVE.values()) >= MAX_ACTIVE_PER_SCOPE):
            return ScenarioResult(name, phrase, False, [], status="busy",
                                  error="Scenario runner is busy; wait for the active scenario to finish")
        if retry_args:
            previous, _ = _retry_plan(config, retry_args)
            previous["consumed"] = True  # Atomic with reservation: a double click cannot launch twice.
        outcomes = deepcopy(previous["outcomes"]) if previous else [
            dict(type=step_kind(step), step_index=n, success=False, not_run=True)
            for n, step in enumerate(chosen["steps"], 1)]
        record = dict(run_id=run_id, scenario_id=scenario_id, scope=scope,
                      fingerprint=fingerprint, status="started", outcomes=outcomes, summary={},
                      consumed=False, retry_of=base["retry_of"], inputs=deepcopy(run_values),
                      inputs_fingerprint=inputs_fp)
        _RUN_RECORDS[run_id] = record
        while len(_RUN_RECORDS) > MAX_RUN_RECORDS:
            _RUN_RECORDS.popitem(last=False)
        _ACTIVE[run_id] = {"key": (scope, scenario_id.casefold()), "cancel": local_cancel, "event": base.copy()}
    def emit(state, index=0, kind="", **extra):
        event = {**base, "state": state, "step_index": index, "kind": kind,
                 "elapsed": round(time.monotonic() - started, 3), **extra}
        _publish(event, on_event)
    def check():
        global_cancel.raise_if_cancelled()
        if local_cancel.is_set():
            raise OperationCancelled("Scenario cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Scenario execution budget exceeded")
        if retry_args:
            # Actual definition edits during approval, queuing or placement invalidate retry.
            current = find_scenario(config, scenario_id)
            if scenario_fingerprint(current) != fingerprint:
                raise ValueError("retry_unavailable")
    def wait(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            check()
            time.sleep(min(0.05, max(0, end - time.monotonic())))
        check()
    def remember(result, index, kind):
        result.update(type=kind, step_index=index)
        out.append(result)
        if 1 <= index <= total:
            outcomes[index - 1] = deepcopy(result)
    out, status, error = [], "completed", ""
    index, kind, result, step_begun = 0, "", {}, False
    planned = [(retry_args["step_index"], chosen["steps"][retry_args["step_index"] - 1])] if retry_args else list(enumerate(chosen["steps"], 1))
    try:
        emit("started")
        check()
        for index, step in planned:
            kind, result, step_begun = step_kind(step), {}, False
            check()
            if step.get("enabled", True) is False:
                emit("step_skipped", index, kind)
                remember({"success": True, "skipped": True}, index, kind)
                continue
            step_begun = True
            emit("step_started", index, kind)
            check()
            try:
                if kind == "open":
                    result = _open_path(step["path"])
                    monitor = int(step.get("monitor", 0))
                    if result.get("success"):
                        # Preserve effects even if placement/wait is interrupted.
                        result.update(opened=True, effect_state="opened")
                        if monitor:
                            result["placement_requested"] = True
                            wait(float(step.get("launch_wait", 0.7)))
                            result["placement"] = _move_opened_window(step["path"], monitor, str(step.get("window_title", "")), check)
                            if not result["placement"].get("moved"):
                                result["success"] = False
                                result["error"] = result["placement"].get("error") or "Window placement failed"
                    elif result.get("effect_state") == "none" and not result.get("opened"):
                        # Only an adapter-certified no-effect failure may reopen.
                        result["retry_mode"] = "open"
                elif kind == "wait":
                    result = {"success": False, "retry_mode": "wait", "effect_state": "none"}
                    wait(float(step.get("seconds", 0.5)))
                    result.update(success=True, seconds=step.get("seconds", 0.5))
                else:
                    result = _command(step, check, deadline)
            except (OperationCancelled, ScenarioCleanupError):
                raise
            except Exception as exc:
                result.update(success=False, error=str(exc)[:1000])
            if result.get("success"):
                result.pop("retry_mode", None)
            remember(result, index, kind)
            emit("step_finished", index, kind, success=bool(result.get("success")),
                 error=str(result.get("error", ""))[:1000], placement=result.get("placement"), opened=result.get("opened", False))
            if not result.get("success") and failure_policy(chosen, step) == "stop":
                break
        check()
    except OperationCancelled as exc:
        status, error = "cancelled", str(exc)[:1000]
        # Do not replace a completed step if cancellation arrived between steps.
        if step_begun and index and not any(row.get("step_index") == index for row in out):
            result.update(success=False, cancelled=True, error=error)
            remember(result, index, kind or "cancel")
        else:
            out.append({"type": "cancel", "success": False, "cancelled": True, "error": error, "step_index": 0})
    except Exception as exc:
        status, error = "failed", str(exc)[:1000]
        if step_begun and index and not any(row.get("step_index") == index for row in out):
            result.update(success=False, error=error)
            remember(result, index, kind or "runtime")
        else:
            out.append({"type": "limit" if isinstance(exc, TimeoutError) else "runtime", "success": False, "error": error, "step_index": 0})
    finally:
        summary = dict(completed=sum(bool(row.get("success")) and not row.get("skipped", False) for row in outcomes),
                       failed=sum(not row.get("success") and not row.get("not_run", False) for row in outcomes),
                       skipped=sum(bool(row.get("skipped")) for row in outcomes),
                       not_run=sum(bool(row.get("not_run")) for row in outcomes))
        if status == "completed":
            status = "failed" if summary["failed"] else "partial" if summary["not_run"] else "completed"
        with _LOCK:
            record.update(status=status, outcomes=deepcopy(outcomes), summary=summary)
        try:
            emit(status, index, kind, error=error, outcomes=outcomes, summary=summary)
        finally:
            with _LOCK:
                _ACTIVE.pop(run_id, None)
    return ScenarioResult(name, phrase, status == "completed", out, run_id, status, error,
                          summary, deepcopy(outcomes), base["retry_of"])
