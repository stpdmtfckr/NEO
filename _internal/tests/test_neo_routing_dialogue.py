"""Behavioural regressions; no network, models, desktop actions or credentials."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from jarvis.runtime.natural_commands import LANGUAGES, normalize_natural_command, negated_action, missing_target
from jarvis.runtime.multilingual_commands import normalize_multilingual_command, command_language_violation
from jarvis.runtime.social_atlas import SocialAtlas, catalog, category_count
from jarvis.runtime.feature_commands import parse_feature_command, exact_phrases
from jarvis.runtime.conversation import ConversationStore, PersonalAliases
from jarvis.tools.argument_validation import validate, normalize_arguments, normalize_tool_call


@pytest.mark.parametrize("code,text", [
    ("ru", "не закрывай приложение Steam"), ("en", "don't open application Steam"),
    ("de", "öffne das Programm Steam nicht"), ("fr", "n'ouvre pas l'application Steam"),
    ("es", "no abras la aplicación Steam"), ("it", "non aprire l'applicazione Steam"),
    ("pt", "não abra o aplicativo Steam"), ("ru", "не хочу выключать компьютер"),
    ("en", "do not play https://youtube.com/watch?v=abc"),
])
def test_negative_imperative_never_becomes_positive(code, text):
    assert negated_action(text, code)
    assert normalize_natural_command(text, code) is None
    assert normalize_multilingual_command(text, code) == text


@pytest.mark.parametrize("code,text", [
    ("ru", "Не мог бы ты открыть файл Don't Stop.txt"),
    ("en", "Could you open the file Don't Stop.txt"),
    ("de", "Bitte öffne die Datei Don't Stop.txt"),
    ("fr", "Peux-tu ouvrir le fichier Don't Stop.txt"),
    ("es", "Podrías abrir el archivo Don't Stop.txt"),
    ("it", "Puoi aprire il file Don't Stop.txt"),
    ("pt", "Poderia abrir o arquivo Don't Stop.txt"),
])
def test_native_scaffold_preserves_foreign_payload(code, text):
    assert normalize_multilingual_command(text, code) == "открой файл Don't Stop.txt"
    assert not negated_action(text, code)


@pytest.mark.parametrize("code,text,target", [
    ("en", 'find file "Weather Yesterday.txt"', '"Weather Yesterday.txt"'),
    ("es", 'abre la carpeta C:\\Users\\Pause\\Music', 'C:\\Users\\Pause\\Music'),
    ("fr", 'ouvre le fichier météo.txt', 'météo.txt'),
    ("de", 'öffne das Programm Größe', 'Größe'),
])
def test_slot_case_accent_and_path_are_not_translated(code, text, target):
    assert normalize_multilingual_command(text, code).endswith(target)


@pytest.mark.parametrize("code", LANGUAGES)
def test_sixty_social_categories_are_local_and_anti_repeat(code):
    atlas = SocialAtlas()
    assert category_count() >= 50
    assert len({v[0] for v in catalog(code).values()}) >= 50
    for phrase in catalog(code):
        first = atlas.reply(phrase, code, session="test")
        second = atlas.reply(phrase, code, session="test")
        assert first and second and first != second
    assert atlas.reply("open application Steam", code) is None
    assert atlas.reply("привет открой Steam", code) is None


@pytest.mark.parametrize("code", LANGUAGES)
def test_optional_feature_routes_all_locales(code):
    for phrase, (tool, action) in exact_phrases(code).items():
        assert parse_feature_command(phrase, code) == (tool, {"action": action})


@pytest.mark.parametrize("code,phrase", [
    ("ru", "добавь заметку на полку Работа: Не удалять steam.txt"),
    ("en", "add note to shelf Work: Do not delete steam.txt"),
    ("es", "añade una nota en la estantería Trabajo: No borrar steam.txt"),
])
def test_shelf_content_is_data_not_a_second_action(code, phrase):
    tool, args = parse_feature_command(phrase, code)
    assert tool == "shelf" and args["action"] == "create"
    assert args["text"].endswith("steam.txt")


def test_session_expiry_isolation_and_bound():
    sessions = ConversationStore(limit=2, ttl=100)
    first = sessions.get("desktop", now=1)
    first.pending_shell = "open file"
    first.pending_until = 4
    assert not sessions.get("telegram:1", now=2).pending_shell
    assert sessions.get("desktop", now=3).pending_shell
    assert not sessions.get("desktop", now=5).pending_shell
    sessions.get("telegram:2", now=6)
    assert len(sessions._states) == 2
    assert sessions.get("desktop", now=1000) is not first


def test_alias_requires_confirmation_persists_and_is_locale_scoped():
    database = Mock()
    memory = {}
    database.get_memory.side_effect = lambda key, default: memory.get(key, default)
    database.set_memory.side_effect = lambda key, value: memory.__setitem__(key, value)
    aliases = PersonalAliases(database)
    with pytest.raises(ValueError):
        aliases.add("work mode", "open application Steam", "en")
    aliases.add("work mode", "open application Steam", "en", confirmed=True)
    assert PersonalAliases(database).resolve("WORK MODE", "en") == "open application Steam"
    assert not aliases.resolve("work mode", "ru")
    aliases.set_enabled("work mode", "en", False)
    assert not aliases.resolve("work mode", "en")
    assert aliases.delete("work mode", "en")


def test_strict_tool_validation_and_legacy_aliases():
    schema = {"type": "object", "required": ["action"], "properties": {"action": {"type": "string", "enum": ["open"]}, "amount": {"type": "integer", "minimum": 0, "maximum": 100}}}
    assert not validate(normalize_arguments("filesystem", {"action": "open_file", "amount": 0}), schema)
    assert validate({"action": "open", "amount": True}, schema)
    assert validate({"action": "erase_everything"}, schema)
    assert validate({"action": "open", "amount": 101}, schema)
    assert validate({"action": "open", "amount": "1"}, schema)


def test_historical_local_music_facade_keeps_ownership_boundary():
    from jarvis.routing.ownership_firewall import claim_for_owner, tool_allowed
    name, args = normalize_tool_call("media", {"action": "play", "target": "yandex_music"})
    assert name == "yandex_music"
    assert tool_allowed(name, args, claim_for_owner("media_generic"))[0]
    assert not tool_allowed(name, args, claim_for_owner("media_video"))[0]
    assert normalize_tool_call("media", {"action": "play", "target": "youtube"})[0] == "media"


def test_agent_sessions_do_not_share_security_confirmation():
    from jarvis.agent import Agent
    agent = object.__new__(Agent)
    agent._lock = threading.RLock()
    agent.conversations = ConversationStore()
    agent._conversation_key = "desktop"
    agent._pending_context = {"call": "delete_desktop_file"}
    agent._last_file_path = "C:\\private.txt"
    agent.history = [{"role": "user", "content": "private"}]
    agent._activate_conversation("telegram:123")
    assert agent._pending_context is None and agent._last_file_path == ""
    assert agent.history == []
    agent._pending_context = {"call": "send_telegram_message"}
    agent._activate_conversation("desktop")
    assert agent._pending_context == {"call": "delete_desktop_file"}
    assert agent._last_file_path == "C:\\private.txt"


def test_disabled_legacy_model_entrypoint_never_touches_model():
    from jarvis.agent import Agent
    agent = object.__new__(Agent)
    agent.config = SimpleNamespace(get=lambda key, default=None: "en" if key == "app.language" else default)
    agent.ollama = Mock()
    agent.ollama.chat_stream.side_effect = AssertionError("model was called")
    turn = agent._agent_loop([{"role": "user", "content": "anything"}], tool_names=["filesystem"])
    assert "routing.model_fallback_disabled" in turn.tool_details
    agent.ollama.chat_stream.assert_not_called()


def test_agent_native_pending_slot_does_not_enable_foreign_atlas():
    from jarvis.agent import Agent
    agent = object.__new__(Agent)
    agent.conversations = ConversationStore()
    agent._conversation_key = "desktop"
    _text, prompt = agent._dialogue_preflight("open the file", "en")
    assert prompt
    text, prompt = agent._dialogue_preflight("Informe.pdf", "en")
    assert prompt is None
    assert not command_language_violation(text, "en")[0]
    assert normalize_multilingual_command(text, "en") == "открой файл Informe.pdf"


@pytest.mark.parametrize("code", LANGUAGES)
@pytest.mark.parametrize("style", ("friendly", "conversational", "business", "military", "street"))
def test_all_styles_keep_response_variation(code, style):
    atlas = SocialAtlas()
    phrase = next(p for p, (category, _) in catalog(code).items() if category == "presence")
    assert atlas.reply(phrase, code, style=style) != atlas.reply(phrase, code, style=style)


@pytest.mark.parametrize("code", LANGUAGES)
def test_documented_feature_examples_match_actual_parser(code):
    from export_command_examples import EXAMPLES
    index = LANGUAGES.index(code)
    for identifier, rows in EXAMPLES.items():
        tool, action = identifier.split(".")[:2]
        parsed = parse_feature_command(rows[index], code)
        assert parsed and parsed[0] == tool and parsed[1]["action"] == action


def test_shelf_zero_index_is_not_silently_changed_to_first_item():
    assert parse_feature_command("отметь пункт 0 в чек-листе Сборы", "ru") is None
