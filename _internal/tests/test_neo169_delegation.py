"""NEO 169: a tool may use another tool as part of its own work without the turn
ownership firewall calling it an «unrelated action» (bug from 168:
«turn ownership 'native:ui' forbids tool 'input_control'»)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis.routing.ownership_firewall import bind_exact_tool_ownership, ownership_claim_scope, OwnershipClaim
from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution, ToolRegistry

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}, "name": {"type": "string"}}, "required": ["action"]}


@pytest.fixture()
def registry(monkeypatch):
    import jarvis.tools.registry as module
    monkeypatch.setattr(module, "note_tool_execution", lambda *a, **kw: None)
    reg = object.__new__(ToolRegistry)
    reg.config = SimpleNamespace(get=lambda key, default=None: "ru" if key == "app.language" else default)
    reg.database = Mock()
    reg.context_memory = Mock()
    reg.execution_guard = None
    reg.execution_transport = "local"
    reg.calls = []

    def tool(name, inner=None):
        def handler(args):
            reg.calls.append(name)
            if inner:
                nested = reg.execute(inner, {"action": "click_control", "name": "Продолжить"})
                return ToolExecution(nested.success, nested.text, nested.data)
            return ToolExecution(True, "ok", {"verified": True})
        return ToolDefinition(name, "test", SCHEMA, handler, lambda _a: Risk.SAFE)

    reg._tools = {"ui": tool("ui", "input_control"), "input_control": tool("input_control", "screen"),
                  "screen": tool("screen"), "filesystem": tool("filesystem"), "bad": tool("bad", "filesystem")}
    from jarvis.runtime.cancellation import cancellation_manager
    cancellation_manager().reset()
    return reg


def run_owned(reg, tool):
    with ownership_claim_scope(OwnershipClaim()):
        bind_exact_tool_ownership(tool, text="нажми Продолжить")
        return reg.execute(tool, {"action": "press", "name": "Продолжить"})


def test_ui_may_press_a_button_through_input_control_and_screen(registry):
    result = run_owned(registry, "ui")
    assert result.success, result.text
    assert "Маршрутизация заблокировала" not in result.text
    assert registry.calls == ["ui", "input_control", "screen"]


def test_input_control_may_fall_back_to_screen(registry):
    result = run_owned(registry, "input_control")
    assert result.success and registry.calls == ["input_control", "screen"]


def test_a_direct_call_outside_the_owner_is_still_blocked(registry):
    with ownership_claim_scope(OwnershipClaim()):
        bind_exact_tool_ownership("ui", text="нажми Продолжить")
        result = registry.execute("input_control", {"action": "click_control", "name": "x"})
    assert not result.success and "forbids tool 'input_control'" in result.text
    assert registry.calls == []


def test_unreviewed_pairs_are_still_blocked(registry):
    with ownership_claim_scope(OwnershipClaim()):
        bind_exact_tool_ownership("bad", text="x")
        result = registry.execute("bad", {"action": "x"})
    assert not result.success and "forbids tool 'filesystem'" in result.text
    assert registry.calls == ["bad"]


def test_delegation_ends_with_the_handler(registry):
    run_owned(registry, "ui")
    with ownership_claim_scope(OwnershipClaim()):
        bind_exact_tool_ownership("ui", text="x")
        result = registry.execute("screen", {"action": "x"})
    assert not result.success


@pytest.mark.parametrize("phrase,tool,check", [
    ("нажми клавишу esc", "ui", lambda a: a.get("key") == "esc"),
    ("нажми esc", "ui", lambda a: a.get("key") == "esc"),
    ("нажми enter", "ui", lambda a: a.get("key") == "enter"),
])
def test_keys_are_keys(phrase, tool, check):
    import json
    from pathlib import Path
    from jarvis.runtime.canonical_commands import parse_canonical_command
    catalogue = json.loads((Path(__file__).parent / "fixtures/command_catalogue.json").read_text(encoding="utf-8"))
    schemas = {row["tool"]: row["schema"] for row in catalogue["tools"]}
    parsed = parse_canonical_command(phrase, "ru", schemas.get)
    assert parsed is not None and parsed.tool == tool and check(parsed.arguments), (phrase, parsed)


def test_a_key_named_as_a_button_presses_the_key(monkeypatch):
    """«нажми кнопку esc» must press Esc, not search NEO's chat for an element called «esc»."""
    from jarvis.runtime import input168
    spec = input168.parse_key_spec("esc", "ru")
    assert spec is not None
    assert input168.parse_key_spec("Продолжить", "ru") is None
