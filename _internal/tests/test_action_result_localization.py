"""Typed response localization preserves status and user/provider payloads."""
import pytest
from jarvis.runtime.localized_text import render_action_result, localize_runtime_text, ACTION_CATEGORIES


@pytest.mark.parametrize("lang", ["en", "de", "fr", "es", "it", "pt"])
def test_localized_file_write_preserves_unicode_payload(lang):
    data = {"path": r"C:\Документы\письмо.txt", "size_bytes": 17}
    result = render_action_result("filesystem", {"action": "write_text"}, True, "Запись завершена", data, lang)
    assert data["path"] in result and "17" in result
    assert "not localized" not in result and "ещё" not in result
    assert data == {"path": r"C:\Документы\письмо.txt", "size_bytes": 17}


@pytest.mark.parametrize("lang", ["en", "de", "fr", "es", "it", "pt"])
def test_failed_action_is_never_rendered_as_completed(lang):
    result = render_action_result("application", {"action": "close", "name": "Редактор"}, False,
                                  "Доступ запрещён: код 5", {"verified": False}, lang)
    assert "Редактор" in result and "5" in result
    assert not result.startswith(("Closed", "Geschlossen", "Fermé", "Cerrado", "Chiuso", "Fechado"))


@pytest.mark.parametrize("lang", ["en", "de", "fr", "es", "it", "pt"])
def test_unknown_outcome_not_claimed_as_playing(lang):
    result = render_action_result("discord", {"action": "music_skip"}, True, "Переключение отправлено",
                                  {"outcome_pending": True, "title": "Песня"}, lang)
    assert result.splitlines()[0] not in {"Next track", "Nächster Titel", "Piste suivante", "Pista siguiente", "Traccia successiva", "Faixa seguinte"}
    assert "Песня" in result


def test_requested_translation_and_file_content_not_retranslated():
    assert render_action_result("translate", {}, True, "Guten Morgen", {}, "es") == "Guten Morgen"
    assert render_action_result("filesystem", {"action": "read_text"}, True, "Код = 123", {}, "en") == "Код = 123"
    assert render_action_result("shelf", {"action": "read"}, True, "Заметка: не забудь ключи", {}, "en") == "Заметка: не забудь ключи"


def test_known_message_retains_cyrillic_entity():
    assert localize_runtime_text("Открыл приложение: Редактор", "en") == "Opened: Редактор"


def test_structured_projection_never_dumps_secret_fields():
    result = render_action_result("network", {"action": "info"}, True, "Данные сети",
                                  {"name": "Ethernet", "access_token": "must-not-appear", "password": "never-show"}, "de")
    assert "Ethernet" in result
    assert "must-not-appear" not in result and "never-show" not in result


def test_all_principal_action_families_have_semantic_projection():
    for action in ("music_skip", "write_text", "capture", "queue_next", "self_test", "send_message", "checklist_check", "terminate_process", "set_input_volume", "undo_last_change"):
        assert action in ACTION_CATEGORIES


def test_native_new_tool_response_is_preserved():
    source = "Nota guardada: Proyecto"
    assert render_action_result("shelf", {"action": "create"}, True, source, {"title": "Proyecto"}, "es") == source
