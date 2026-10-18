"""Native grammar through the real registry boundary, using inert handlers."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.runtime.canonical_commands import parse_canonical_command, templates, _SLOT_NAMES, _bind
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.tools.argument_validation import validate
from jarvis.tools.registry import ToolDefinition, ToolExecution, ToolRegistry
from jarvis.security import Risk
from jarvis.routing.ownership_firewall import bind_exact_tool_ownership, ownership_claim_scope

CATALOGUE = json.loads((Path(__file__).resolve().parent / "fixtures" / "command_catalogue.json").read_text(encoding="utf-8"))
SCHEMAS = {row["tool"]: row["schema"] for row in CATALOGUE["tools"]}


def example(row):
    properties = SCHEMAS[row.intent.split(".", 1)[0]]["properties"]
    def replace(match):
        key = _SLOT_NAMES.get(row.intent, {}).get(match[1], match[1])
        schema = properties.get(key, {})
        if schema.get("type") in {"number", "integer"}:
            return str(max(2, schema.get("minimum", 2)))
        if schema.get("enum"):
            return schema["enum"][0]
        return "Ctrl+S" if key == "keys" else "Example"
    return re.sub(r"\{(\w+)\}", replace, row.phrase)


@pytest.mark.parametrize("code", LANGUAGES)
def test_all_authored_native_actions_pass_real_registry_contract_without_os(code, monkeypatch):
    import jarvis.tools.registry as registry_module
    monkeypatch.setattr(registry_module, "note_tool_execution", lambda *a, **kw: None)
    registry = object.__new__(ToolRegistry)
    registry.config = SimpleNamespace(get=lambda key, default=None: code if key == "app.language" else default)
    registry.database = Mock()
    registry.context_memory = Mock()
    registry.execution_guard = None
    registry.execution_transport = "local"
    calls = []
    def inert_handler(args):
        calls.append(dict(args))
        return ToolExecution(True, "ok", {"verified": True})
    registry._tools = {name: ToolDefinition(name, "inert contract test", schema, inert_handler, lambda args: Risk.SAFE) for name, schema in SCHEMAS.items()}
    from jarvis.runtime.cancellation import cancellation_manager
    cancellation_manager().reset()
    successful = 0
    incomplete = 0
    for row in templates(code):
        from jarvis.runtime.multilingual_commands import command_language_violation
        command = example(row)
        assert not command_language_violation(command, code)[0], (code, row.intent, command)
        parsed = parse_canonical_command(command, code, SCHEMAS.__getitem__)
        assert parsed is not None, row.intent
        if row.intent in {"automation.queue", "automation.parallel"}:
            assert parsed.error == "invalid_slots"
            incomplete += 1
            continue
        assert not parsed.error, (row.intent, parsed)
        captures = row.pattern.fullmatch(command).groupdict()
        expected_tool, expected_args = _bind(row, captures, SCHEMAS[row.intent.split('.')[0]], command, code)
        assert (parsed.tool, parsed.arguments) == (expected_tool, expected_args), (code, row.intent, command, parsed, expected_args)
        assert not validate(parsed.arguments, SCHEMAS[parsed.tool]), row.intent
        with ownership_claim_scope(None):
            bind_exact_tool_ownership(parsed.tool)
            result = registry.execute(parsed.tool, parsed.arguments)
        assert result.success, (row.intent, result.text)
        successful += 1
    assert successful == len(templates(code)) - incomplete
    assert len(calls) == successful >= 428


@pytest.mark.parametrize("code,phrase", [
    ("ru", "удали файл C:\\Работа\\Don't Stop.txt в корзину"),
    ("en", "move file C:\\Work\\Don't Stop.txt to the recycle bin"),
    ("de", "verschiebe die Datei C:\\Arbeit\\Don't Stop.txt in den Papierkorb"),
    ("fr", "place le fichier C:\\Travail\\Don't Stop.txt dans la corbeille"),
    ("es", "mueve el archivo C:\\Trabajo\\Don't Stop.txt a la papelera"),
    ("it", "sposta il file C:\\Lavoro\\Don't Stop.txt nel cestino"),
    ("pt", "move o ficheiro C:\\Trabalho\\Don't Stop.txt para a reciclagem"),
])
def test_destructive_native_route_requires_confirmation_and_preserves_path(code, phrase):
    from jarvis.agent import Agent
    from jarvis.runtime.router import RoutedIntent
    parsed = parse_canonical_command(phrase, code, SCHEMAS.__getitem__)
    assert parsed and not parsed.error
    assert parsed.tool == "filesystem" and parsed.arguments["action"] == "delete"
    assert parsed.arguments["source"].endswith("Don't Stop.txt")
    agent = object.__new__(Agent)
    agent.tools = Mock()
    agent.tools.risk_for.return_value = Risk.HIGH
    agent.tools.execute.side_effect = AssertionError("unconfirmed side effect")
    agent._needs_confirmation = lambda name, args, risk: risk >= Risk.HIGH
    agent._confirmation_summary = lambda *args: "delete requested file"
    agent._confirmation_prompt = lambda summary: "Confirm: " + summary
    with ownership_claim_scope(None):
        bind_exact_tool_ownership(parsed.tool)
        turn = agent._execute_routed_intent(RoutedIntent(parsed.intent, parsed.tool, parsed.arguments, 990), phrase)
    assert turn.pending is not None
    assert agent._pending_context["call"]["function"]["arguments"] == parsed.arguments
    agent.tools.execute.assert_not_called()


@pytest.mark.parametrize("phrase", [
    '"delete file C:\\draft.txt"', "I said delete file C:\\draft.txt", "do not delete file C:\\draft.txt",
    "tell me what delete file means", "don't restart the computer", "the previous command was open browser Firefox",
])
def test_negative_quoted_and_conversation_history_are_not_commands(phrase):
    assert parse_canonical_command(phrase, "en", SCHEMAS.__getitem__) is None


def test_bad_numeric_slots_and_unfilled_templates_fail_closed():
    for phrase in ("set the volume to lots percent", "set the volume to NaN percent", "move file {source} to the recycle bin"):
        result = parse_canonical_command(phrase, "en", SCHEMAS.__getitem__)
        assert result and result.error
    assert parse_canonical_command("set the volume to 12,5 percent", "en", SCHEMAS.__getitem__).arguments["value"] == 12.5


def test_template_specificity_and_semantic_aliases_do_not_depend_on_file_order():
    result = parse_canonical_command("find a file containing Weather Yesterday", "en", SCHEMAS.__getitem__)
    assert result.tool == "search" and result.arguments == {"action": "content", "query": "Weather Yesterday"}
    result = parse_canonical_command("find file Weather Yesterday.txt", "en", SCHEMAS.__getitem__)
    assert result.arguments == {"action": "files", "query": "Weather Yesterday.txt"}
    result = parse_canonical_command("play the playlist Don't Stop on Yandex Music", "en", SCHEMAS.__getitem__)
    assert result.arguments["playlist"] == "Don't Stop" and result.arguments["action"] == "playlist"


def test_real_collision_clarifies_instead_of_first_winner(monkeypatch):
    import jarvis.runtime.canonical_commands as grammar
    rows = (grammar._compile("system.shutdown", "do the operation"), grammar._compile("system.restart", "do the operation"))
    monkeypatch.setattr(grammar, "_fast_index", lambda code: ({"do the operation": rows}, {}))
    result = grammar.parse_canonical_command("do the operation", "en", SCHEMAS.__getitem__)
    assert result.error == "ambiguous" and len(result.candidates) == 2


def test_mature_compound_planner_keeps_priority_and_written_text_is_opaque():
    assert parse_canonical_command("open application Discord and Steam", "en", SCHEMAS.__getitem__) is None
    assert parse_canonical_command("open application Steam and restart the computer", "en", SCHEMAS.__getitem__) is None
    result = parse_canonical_command("type this dictation open Steam and restart the computer", "en", SCHEMAS.__getitem__)
    assert result.arguments["text"] == "open Steam and restart the computer"


def test_exact_native_claim_cannot_execute_an_unrelated_tool():
    from jarvis.routing.ownership_firewall import tool_allowed
    with ownership_claim_scope(None):
        bind_exact_tool_ownership("filesystem")
        assert tool_allowed("filesystem", {"action": "read_text", "path": "x"})[0]
        assert not tool_allowed("system", {"action": "shutdown"})[0]


@pytest.mark.parametrize("tool,action,risk", [("browser", "full_cleanup", Risk.CRITICAL), ("browser", "clear_history", Risk.HIGH), ("email", "send", Risk.HIGH), ("filesystem", "delete_permanent", Risk.HIGH), ("system", "shutdown", Risk.HIGH)])
def test_real_confirmation_policy_preserves_high_risk_contracts(tool, action, risk):
    from jarvis.agent import Agent
    agent = object.__new__(Agent)
    agent._confirmation_policy = None
    agent.config = SimpleNamespace(get=lambda key, default=None: default)
    assert agent._needs_confirmation(tool, {"action": action}, risk)


def test_pending_file_target_does_not_absorb_a_new_feature_or_factual_question():
    from jarvis.agent import Agent
    from jarvis.runtime.conversation import ConversationStore
    import time
    agent = object.__new__(Agent)
    agent.conversations = ConversationStore()
    agent.config = SimpleNamespace(get=lambda key, default=None: default)
    agent.tools = SimpleNamespace(get=lambda name: SimpleNamespace(parameters=SCHEMAS[name]))
    for command in ("show downloads", "What is entropy?"):
        state = agent.conversations.get("desktop")
        state.pending_shell, state.pending_until = "open file", time.monotonic() + 40
        text, answer = agent._dialogue_preflight(command, "en")
        assert text == command and answer is None
        assert not state.pending_shell


def test_alias_can_target_a_new_feature_but_cannot_replace_builtin_grammar():
    from jarvis.agent import Agent
    from jarvis.runtime.conversation import ConversationStore, PersonalAliases
    agent = object.__new__(Agent)
    agent.conversations = ConversationStore()
    agent.config = SimpleNamespace(get=lambda key, default=None: default)
    agent.tools = SimpleNamespace(get=lambda name: SimpleNamespace(parameters=SCHEMAS[name]))
    agent.router = Mock()
    agent.router.route.return_value = None
    memory = {}
    db = SimpleNamespace(get_memory=lambda key, default: memory.get(key, default), set_memory=lambda key, value: memory.__setitem__(key, value))
    agent.personal_aliases = PersonalAliases(db)
    _, answer = agent._dialogue_preflight('remember alias "my transfer board" as "show downloads"', "en")
    assert "Save this alias" in answer
    agent._dialogue_preflight("yes", "en")
    assert agent.personal_aliases.resolve("my transfer board", "en") == "show downloads"
    _, answer = agent._dialogue_preflight('remember alias "show downloads" as "show shelf"', "en")
    assert "Save this alias" not in answer


@pytest.mark.parametrize("code,question", [("ru", "Что такое энтропия?"), ("en", "What is entropy?"), ("de", "Was ist Entropie?"), ("fr", "Qu'est-ce que l'entropie ?"), ("es", "¿Qué es la entropía?"), ("it", "Che cos'è l'entropia?"), ("pt", "O que é entropia?")])
def test_factual_questions_all_languages_belong_to_web_not_semantic_actions(code, question):
    from jarvis.runtime.semantics import looks_like_general_question
    from jarvis.runtime.atlas2 import PhraseAtlas2
    assert looks_like_general_question(question)
    atlas = PhraseAtlas2(SimpleNamespace(get=lambda key, default=None: default))
    matched = atlas.semantic_match(question, language=code)
    assert matched is None or not matched.executable


def test_literal_provider_and_browser_defaults_match_user_intent():
    youtube = parse_canonical_command("pause the YouTube video", "en", SCHEMAS.__getitem__)
    assert youtube.arguments == {"action": "pause", "target": "youtube"}
    browser = parse_canonical_command("open browser Firefox", "en", SCHEMAS.__getitem__)
    assert browser.tool == "application" and browser.arguments == {"action": "open", "kind": "app", "name": "Firefox"}
    row = next(row for row in templates("en") if row.intent == "discord.music_youtube_url")
    discord = parse_canonical_command(example(row), "en", SCHEMAS.__getitem__)
    assert discord.arguments["play_now"] is True


def test_native_punctuation_does_not_modify_captured_user_data():
    email = parse_canonical_command("¿Cuántos correos hay en total en mi buzón?", "es", SCHEMAS.__getitem__)
    assert email and email.arguments["action"] == "total"
    row = next(row for row in templates("fr") if row.intent == "email.read")
    phrase = example(row).replace("’", "'")
    result = parse_canonical_command(phrase, "fr", SCHEMAS.__getitem__)
    assert result and result.arguments["message_id"] == "Example"


@pytest.mark.parametrize("code,close,pin,unpin,zoom", [
    ("ru", "закрой изображение", "закрепи изображение", "открепи изображение", "масштаб изображения 150 процентов"),
    ("en", "close the image", "pin the image", "unpin the image", "set the image zoom to 150 percent"),
    ("de", "schließe das Bild", "fixiere das Bild", "löse die Fixierung des Bildes", "setze die Bildvergrößerung auf 150 Prozent"),
    ("fr", "ferme l'image", "épingle l'image", "désépingle l'image", "règle le zoom de l'image à 150 pour cent"),
    ("es", "cierra la imagen", "fija la imagen", "desfija la imagen", "ajusta el zoom de la imagen al 150 por ciento"),
    ("it", "chiudi l'immagine", "fissa l'immagine", "sblocca l'immagine", "imposta lo zoom dell'immagine al 150 per cento"),
    ("pt", "fecha a imagem", "fixa a imagem", "desfixa a imagem", "define o zoom da imagem para 150 por cento"),
])
def test_image_viewer_grammar_targets_image_without_touching_browser(code, close, pin, unpin, zoom):
    from jarvis.runtime.feature_commands import parse_feature_command
    assert parse_feature_command(close, code) == ("mini_browser", {"action": "close", "id": "image"})
    assert parse_feature_command(pin, code) == ("mini_browser", {"action": "pin", "id": "image", "pinned": True})
    assert parse_feature_command(unpin, code) == ("mini_browser", {"action": "pin", "id": "image", "pinned": False})
    assert parse_feature_command(zoom, code) == ("mini_browser", {"action": "zoom", "id": "image", "zoom": 150.0})


def test_cancelled_previous_turn_does_not_disable_native_command():
    from jarvis.agent import Agent
    from jarvis.runtime.cancellation import cancellation_manager
    from jarvis.runtime.conversation import ConversationStore
    import threading
    agent = object.__new__(Agent)
    agent._lock = threading.RLock()
    agent.conversations = ConversationStore()
    agent.config = SimpleNamespace(get=lambda key, default=None: "en" if key == "app.language" else default)
    agent.context_memory = Mock()
    agent.history = []
    agent.tools = Mock()
    agent.tools.risk_for.return_value = Risk.SAFE
    agent.tools.execute.return_value = ToolExecution(True, "No active downloads.")
    agent._needs_confirmation = lambda *a: False
    agent._remember_execution_context = Mock()
    cancellation_manager().cancel("previous operation")
    with ownership_claim_scope(None):
        turn = agent._handle_stream_owned("show downloads")
    assert turn.text == "No active downloads."
    agent.tools.execute.assert_called_once_with("downloads", {"action": "list"})
