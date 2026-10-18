"""186: reply phrases — no false completion, structural localization, exact user data."""
from __future__ import annotations

import re

import pytest

from jarvis.runtime.localized_text import render_action_result
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

FOREIGN = ("en", "de", "fr", "es", "it", "pt")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@pytest.fixture()
def session(tmp_path):
    with external_io_blocked():
        value = AtlasSession(tmp_path / "run")
        value.reset_case("ru")
        yield value
        value.finish()


# ------------------------------------------------------------ USER185-01 / PHRASE185-STATE-01

def test_generic_launch_gets_no_second_launch_claim(session):
    from jarvis.agent_types import AgentTurn
    agent = session.agent
    turn = agent._socialize_direct_turn(AgentTurn("Открыл приложение Cheat Engine.", tool_details=["application: ok"], success=True), "открой cheat engine")
    assert turn.text == "Открыл приложение Cheat Engine."


def test_unverified_launch_keeps_its_uncertainty(session):
    from jarvis.agent_types import AgentTurn
    agent = session.agent
    agent._last_execution186 = (int(getattr(agent, "_turn_serial186", 0)), {"launch_pending": True, "verified": False})
    turn = agent._socialize_direct_turn(AgentTurn("Запуск отправлен, игра ещё не подтвердила старт.", tool_details=["application: ok"], success=True),
                                        "запусти игру Warframe")
    assert turn.text == "Запуск отправлен, игра ещё не подтвердила старт."


def test_name_prefix_never_claims_completion():
    from jarvis.runtime.addressing import _PREFIXES
    claims = {"Hecho", "Fatto", "Feito", "Готово", "Done", "Erledigt", "Fait"}
    assert all(not (set(row["action"]) & claims) for row in _PREFIXES.values())


# ------------------------------------------------------------ PHRASE185-L10N-01

@pytest.mark.parametrize("code", FOREIGN)
@pytest.mark.parametrize("focused", [True, False])
def test_app_launch_reply_is_fully_localized_and_keeps_the_name(code, focused):
    source = "Открыл приложение OrbitQuest и вывел его окно на экран." if focused else "Открыл приложение OrbitQuest."
    text = render_action_result("application", {"action": "open", "name": "OrbitQuest"}, True, source,
                                {"label": "OrbitQuest", "verified": True, "focused": focused}, code)
    assert "OrbitQuest" in text and not CYRILLIC.search(text)


@pytest.mark.parametrize("code", FOREIGN)
def test_app_not_found_is_localized_with_the_requested_name(code):
    text = render_action_result("application", {"action": "open", "name": "Ёжик QA"}, False,
                                "Не удалось найти исполняемое приложение для «Ёжик QA». Я не открывал похожие папки или файлы.",
                                {"kind": "app", "query": "Ёжик QA", "verified": False, "app_not_found": True}, code)
    assert "«Ёжик QA»" in text and not CYRILLIC.search(text.replace("Ёжик QA", ""))


@pytest.mark.parametrize("code", FOREIGN)
def test_calendar_placeholders_follow_the_language_and_user_text_stays(code):
    from jarvis.integrations.google_calendar import calendar_heading, format_calendar_events
    empty = format_calendar_events([], heading=calendar_heading("days", code, 7), lang=code)
    assert not CYRILLIC.search(empty)
    rows = [{"summary": "", "location": "Москва", "start": {"date": "2026-10-10"}}, {"summary": "Встреча QA", "start": {"date": "2026-10-11"}}]
    text = format_calendar_events(rows, heading=calendar_heading("days", code, 1), lang=code)
    assert "Москва" in text and "Встреча QA" in text
    assert not CYRILLIC.search(text.replace("Москва", "").replace("Встреча QA", ""))
    assert render_action_result("google_calendar", {"action": "upcoming"}, True, text, {"events": rows, "localized186": True}, code) == text


def test_calendar_day_grammar():
    from jarvis.integrations.google_calendar import calendar_heading
    assert calendar_heading("days", "ru", 1) == "Расписание на 1 день"
    assert calendar_heading("days", "ru", 3) == "Расписание на 3 дня"
    assert calendar_heading("days", "ru", 7) == "Расписание на 7 дней"
    assert calendar_heading("days", "en", 1) == "Schedule for the next day"


# ------------------------------------------------------------ PHRASE185-OVERLAY-01

@pytest.mark.parametrize("code", FOREIGN)
@pytest.mark.parametrize("action", ["checklist_add", "checklist_check", "checklist_uncheck"])
def test_checklist_reply_names_item_and_panel_without_snapshot_dump(code, action):
    data = {"name": "Shopping QA", "updated_at": 1700000000.0, "items": [{"text": "Buy milk", "checked": action == "checklist_check"}]}
    text = render_action_result("overlay", {"action": action, "name": "Shopping QA", "item": "Buy milk"}, True, "Готово", data, code)
    assert "Buy milk" in text and "Shopping QA" in text and "1700000000" not in text and not CYRILLIC.search(text)


def test_checklist_check_by_number_names_the_item():
    data = {"name": "Shopping QA", "items": [{"text": "Bread", "checked": False}, {"text": "Buy milk", "checked": True}]}
    text = render_action_result("overlay", {"action": "checklist_check", "name": "Shopping QA", "index": 1}, True, "Готово", data, "en")
    assert text == "Checked «Buy milk» in «Shopping QA»."


# ------------------------------------------------------------ PHRASE185-TIMER-01

def _timer_handler(session):
    # The production handler itself (the session only makes ``execute`` inert).
    return session.agent.tools.get("timer").handler


@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_cancel_named_timer_names_it_and_keeps_the_earlier_one(session, code):
    from jarvis.runtime.timers import timer_manager
    session.agent.config.data["app"]["language"] = code
    handler = _timer_handler(session)
    manager = timer_manager(session.agent.config.paths.root, code)
    for item in list(manager.list()):
        manager.cancel(item.id)
    first = manager.create(600, "QA_FIRST")
    later = manager.create(1200, "QA_LATER")
    try:
        result = handler({"action": "cancel", "id": later.id})
        assert result.success and "QA_LATER" in result.text and "QA_FIRST" not in result.text
        assert [x.id for x in manager.list()] == [first.id]
        missing = handler({"action": "cancel", "id": "no-such-id"})
        assert not missing.success and missing.data["remaining"] == 1
        assert [x.id for x in manager.list()] == [first.id]
    finally:
        for item in list(manager.list()):
            manager.cancel(item.id)


def test_timer_event_carries_its_language_for_the_chat():
    from jarvis.runtime.timers import TimerEvent
    assert TimerEvent("chat", "Reminder: Купить хлеб", "x", "en").language == "en"


# ------------------------------------------------------------ CLARIFY / CONFIRM copy

@pytest.mark.parametrize("code,needle", [("ru", "скопировать"), ("en", "copy"), ("de", "kopieren"), ("fr", "copier"),
                                         ("es", "copio"), ("it", "copiare"), ("pt", "copiar")])
def test_copy_without_source_asks_which_file_to_copy(session, code, needle):
    with external_io_blocked():
        session.reset_case(code)
        phrase = {"ru": "скопируй файл", "en": "copy the file", "de": "kopiere die Datei", "fr": "copie le fichier",
                  "es": "copia el archivo", "it": "copia il file", "pt": "copia o ficheiro"}[code]
        turn = session.agent.handle(phrase)
    assert session.calls == [] and needle in turn.text


@pytest.mark.parametrize("code", ["en", "de", "fr", "es", "it", "pt"])
def test_confirmation_risk_level_is_a_word_of_the_ui_language(code):
    from jarvis.agent import _risk_label186
    from jarvis.security import Risk
    assert _risk_label186(Risk.MEDIUM, code) != "medium" or code == "en"
    assert _risk_label186(Risk.HIGH, code) and _risk_label186(Risk.HIGH, "ru") == "высокий"


def test_street_style_keeps_english_i_and_german_nouns():
    from jarvis.runtime.social_atlas import _after_comma186
    assert _after_comma186("I’m here and listening", 1) == "I’m here and listening"
    assert _after_comma186("Musik läuft", 2) == "Musik läuft"
    assert _after_comma186("Tell me", 1) == "tell me"


def test_german_sleep_and_hibernate_are_different_words():
    from jarvis.runtime.localized_text import _ACTION_ROWS
    assert _ACTION_ROWS["sleep"][2] != _ACTION_ROWS["hibernate"][2]
