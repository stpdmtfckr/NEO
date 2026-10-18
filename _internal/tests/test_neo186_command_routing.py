"""186: payload provenance and command routing (inert tools, no device, no network)."""
from __future__ import annotations

import pytest

from jarvis.runtime import everyday172, local_chat169, payload186, reply_memory168
from jarvis.runtime.duration import parse_duration
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    with external_io_blocked():
        everyday172.clear_offer(); reply_memory168.clear(); local_chat169.clear_history()
        value = AtlasSession(tmp_path_factory.mktemp("routing186") / "run")
        value.config.data["local_chat"]["live_small_talk"] = False
        yield value
        value.finish()
        everyday172.clear_offer(); reply_memory168.clear(); local_chat169.clear_history()


def _run(session, code, text):
    with external_io_blocked():
        session.reset_case(code)
        turn = session.agent.handle(text)
        return [(c["tool"], dict(c["arguments"])) for c in session.calls], turn


# ------------------------------------------------------------ durations / reminders

@pytest.mark.parametrize("code,text,seconds", [
    ("ru", "час с четвертью", 4500), ("ru", "полторы минуты", 90), ("ru", "полтора часа", 5400),
    ("en", "one and a half minutes", 90), ("en", "a minute and a half", 90), ("en", "an hour and a quarter", 4500),
    ("de", "eineinhalb Minuten", 90), ("fr", "une minute et demie", 90), ("es", "un minuto y medio", 90),
    ("es", "una hora y cuarto", 4500), ("it", "un minuto e mezzo", 90), ("it", "1 ora 15 minuti", 4500),
    ("pt", "um minuto e meio", 90), ("ru", "час", 3600), ("de", "7 Minuten", 420),
])
def test_fractional_and_compound_durations_are_read_whole(code, text, seconds):
    assert parse_duration(text, code) == seconds


@pytest.mark.parametrize("code,text,seconds,label", [
    ("ru", "напомни через 7 минут: Blue mug 731", 420, "Blue mug 731"),
    ("en", "remind me in 7 minutes: Blue mug 731", 420, "Blue mug 731"),
    ("de", "erinnere mich in 7 Minuten: Blue mug 731", 420, "Blue mug 731"),
    ("fr", "rappelle-moi dans 7 minutes : Blue mug 731", 420, "Blue mug 731"),
    ("es", "recuérdame en 7 minutos: Blue mug 731", 420, "Blue mug 731"),
    ("it", "ricordami tra 7 minuti: Blue mug 731", 420, "Blue mug 731"),
    ("pt", "lembra-me daqui a 7 minutos: Blue mug 731", 420, "Blue mug 731"),
    ("ru", "напомни через час с четвертью: Amber key 953", 4500, "Amber key 953"),
    ("en", "remind me in one and a half minutes: Amber key 953", 90, "Amber key 953"),
    ("it", "ricordami tra un minuto e mezzo: Amber key 953", 90, "Amber key 953"),
    ("fr", "rappelle-moi dans une minute et demie : Amber key 953", 90, "Amber key 953"),
    ("it", "ricordami tra 1 ora 15 minuti: Amber key 953", 4500, "Amber key 953"),
])
def test_reminder_keeps_literal_label_and_whole_duration(session, code, text, seconds, label):
    calls, _turn = _run(session, code, text)
    assert calls and calls[-1][0] == "timer" and calls[-1][1]["action"] == "reminder"
    assert calls[-1][1]["seconds"] == seconds and calls[-1][1]["label"] == label


def test_reminder_label_without_colon_and_label_first():
    assert payload186.reminder("напомни через 20 минут выпить воды", "ru") == {"seconds": 1200.0, "label": "выпить воды"}
    assert payload186.reminder("remind me to call mom in 10 minutes", "en") == {"seconds": 600.0, "label": "call mom"}
    assert payload186.reminder("напомни завтра в 18:00 позвонить", "ru") is None


@pytest.mark.parametrize("code,text", [("de", "stelle einen Timer auf 7 Minuten"), ("es", "pon un temporizador de 7 minutos"),
                                       ("pt", "define um temporizador de 7 minutos")])
def test_timer_minutes_never_become_seconds(code, text):
    args = {"action": "timer", "seconds": 7}
    payload186.repair("timer", args, text, code)
    assert args["seconds"] == 420


# ------------------------------------------------------------ notes / checklists

@pytest.mark.parametrize("code,text,expected", [
    ("ru", "добавь в заметку Trip731: Blue mug 731", ("overlay", {"action": "append_text", "name": "Trip731", "text": "Blue mug 731"})),
    ("ru", "добавь в чек-лист Tasks731: Bread731", ("overlay", {"action": "checklist_add", "name": "Tasks731", "item": "Bread731"})),
    ("pt", "por favor, adiciona à lista de tarefas Tasks731: Bread731", ("overlay", {"action": "checklist_add", "name": "Tasks731", "item": "Bread731"})),
])
def test_add_to_named_object_keeps_target_and_literal_case(session, code, text, expected):
    calls, _turn = _run(session, code, text)
    assert calls[-1][0] == expected[0]
    assert {k: calls[-1][1].get(k) for k in expected[1]} == expected[1]


@pytest.mark.parametrize("code,text", [("ru", "сделай список задач Tasks731 с пунктом Bread731"),
                                       ("es", "haz una lista de tareas llamada Tasks731 con Bread731"),
                                       ("pt", "faz uma lista de tarefas chamada Tasks731 com Bread731")])
def test_checklist_create_separates_title_and_first_item(session, code, text):
    calls, _turn = _run(session, code, text)
    assert calls[-1][0] == "overlay" and calls[-1][1]["action"] == "checklist_create"
    assert calls[-1][1]["name"] == "Tasks731" and calls[-1][1]["items"] == ["Bread731"]


def test_named_note_is_a_neo_note_not_dictation(session):
    calls, _turn = _run(session, "en", "write down Cedar cup 842 as a new note named Trip842")
    assert calls == [("shelf", {"action": "create", "kind": "note", "title": "Trip842", "content": "Cedar cup 842"})]


# ------------------------------------------------------------ information namespace / help / widgets

@pytest.mark.parametrize("code,text,tool,action", [
    ("ru", "покажи состояние сети", "system_monitor", "network"),
    ("ru", "покажи уровень заряда батареи", "system_monitor", "battery"),
    ("en", "show network status", "system_monitor", "network"),
    ("ru", "покажи состояние таймеров", "timer", "list"),
])
def test_information_requests_stay_in_their_goal(session, code, text, tool, action):
    calls, _turn = _run(session, code, text)
    assert calls and calls[-1][0] == tool and calls[-1][1]["action"] == action
    assert all(c[0] != "application" for c in calls)


@pytest.mark.parametrize("code,text", [("en", "show me an example command for NEO"), ("ru", "покажи мне пример команды для NEO")])
def test_help_example_is_answered_without_launching(session, code, text):
    calls, turn = _run(session, code, text)
    assert calls == [] and turn.text == payload186.HELP_EXAMPLE_TEXT[code]


@pytest.mark.parametrize("code,text,action", [("en", "show the widget Notes", "show"), ("de", "zeige das Widget Notes", "show"),
                                              ("en", "show the NEO widget Notes", "show"), ("fr", "masque le widget Notes", "hide"),
                                              ("es", "muestra el widget Notes", "show"), ("it", "nascondi il widget Notes", "hide"),
                                              ("pt", "mostra o widget Notes", "show")])
def test_widget_word_names_a_neo_panel(session, code, text, action):
    calls, _turn = _run(session, code, text)
    assert calls == [("overlay", {"action": action, "name": "Notes"})]


# ------------------------------------------------------------ browser / compound

def test_double_typo_browser_launch_uses_configured_browser(session):
    calls, _turn = _run(session, "ru", "бразуре заусти")
    assert calls and calls[0] == ("application", {"action": "open", "name": session.agent._preferred_browser(), "kind": "app"})


@pytest.mark.parametrize("code,text", [
    ("en", "open the browser and open the application OrbitQuest"),
    ("de", "öffne den Browser und öffne die Anwendung OrbitQuest"),
    ("ru", "открой браузер и открой приложение OrbitQuest"),
    ("pt", "abre o navegador e abre a aplicação OrbitQuest"),
])
def test_compound_first_step_opens_the_configured_browser_without_empty_url(session, code, text):
    from jarvis.runtime import tasks169
    agent = session.agent
    steps = tasks169.plan(text, code, lambda part: agent._task_parse169(part, code), verbs=agent._step_verbs170(code))
    assert steps and len(steps) == 2
    assert (steps[0].tool, steps[0].arguments) == ("application", {"action": "open", "name": agent._preferred_browser(), "kind": "app"})
    assert steps[1].tool == "application" and steps[1].arguments["name"] == "OrbitQuest"


@pytest.mark.parametrize("code,text", [
    ("en", "please, open the folder Downloads and then open the application Notepad"),
    ("en", "open the folder Downloads and please open the application Notepad"),
    ("es", "abre la carpeta Descargas y abre la aplicación Notepad, por favor"),
    ("de", "bitte, öffne den Ordner Downloads und öffne die Anwendung Notepad"),
])
def test_politeness_neither_adds_nor_hides_a_step(session, code, text):
    from jarvis.runtime import tasks169
    agent = session.agent
    steps = tasks169.plan(text, code, lambda part: agent._task_parse169(part, code), verbs=agent._step_verbs170(code))
    assert steps and [s.tool for s in steps] == ["desktop", "application"]
    assert steps[1].arguments["name"] == "Notepad"


# ------------------------------------------------------------ weather / dates / calendar

@pytest.mark.parametrize("code,text", [
    ("en", "what is the weather in Helsinki now"), ("en", "Helsinki weather right now"),
    ("de", "Wie ist das Wetter in Helsinki jetzt"), ("fr", "quel temps fait-il à Helsinki maintenant"),
    ("es", "qué tiempo hace en Helsinki ahora"), ("it", "che tempo fa a Helsinki adesso"),
    ("pt", "como está o tempo em Helsinki agora"),
])
def test_weather_keeps_the_named_city(code, text):
    assert payload186.weather_city(text, code) == "Helsinki"
    args = {"action": "summary", "city": "right"}
    payload186.repair("weather", args, text, code)
    assert args["city"] == "Helsinki"


def test_weather_without_city_uses_home():
    args = {"action": "summary", "city": "now"}
    payload186.repair("weather", args, "what is the weather now", "en")
    assert args["city"] == ""


@pytest.mark.parametrize("code,text", [("fr", "quel jour de la semaine tombe le 15 novembre 2026"),
                                       ("es", "qué día de la semana es el 15 de noviembre de 2026"),
                                       ("pt", "que dia da semana é 15 de novembro de 2026")])
def test_weekday_question_keeps_its_date(session, code, text):
    calls, _turn = _run(session, code, text)
    assert calls == [("date_info", {"action": "date_info", "text": text})]


def test_current_date_is_an_explicit_today_marker():
    args = {"action": "date_info"}
    payload186.repair("date_info", args, "скажи текущую дату", "ru")
    assert args["text"] == "today"


@pytest.mark.parametrize("code,text", [("ru", "какие события в календаре в следующие 7 дней"),
                                       ("fr", "quels événements sont au calendrier dans les 7 prochains jours")])
def test_calendar_seven_day_range_survives(session, code, text):
    calls, _turn = _run(session, code, text)
    assert calls == [("google_calendar", {"action": "upcoming", "days": 7})]


# ------------------------------------------------------------ translation / units

@pytest.mark.parametrize("text,answer", [("переведи 5 km в метры", "5 км = 5000 м."), ("переведи 5 километров в метры", "5 км = 5000 м.")])
def test_unit_conversion_is_not_language_translation(session, text, answer):
    calls, turn = _run(session, "ru", text)
    assert calls == [] and turn.text == answer


def test_incompatible_units_are_refused_honestly():
    assert "разные величины" in payload186.unit_conversion("переведи 5 км в кг", "ru")


@pytest.mark.parametrize("code,text,literal", [("en", 'translate "Blue sky" into French', "Blue sky"),
                                               ("ru", "переведи Hello на русский", "Hello")])
def test_translation_text_keeps_letter_case(session, code, text, literal):
    from jarvis.integrations.local_translation import parse_translation_request
    calls, _turn = _run(session, code, text)
    assert calls and calls[-1][0] == "translate"
    assert parse_translation_request(calls[-1][1]["text"], ui_language=code)[0] == literal


@pytest.mark.parametrize("text,ui", [('übersetze "Bonjour" aus dem Französischen ins Englische', "de"),
                                     ("traduis \"Bonjour\" du français en anglais", "fr"),
                                     ("traduis \"Hallo\" de l'allemand en anglais", "fr"),
                                     ("traduci \"Bonjour\" dal francese all'inglese", "it")])
def test_explicit_source_language_with_article(text, ui):
    from jarvis.integrations.local_translation import parse_translation_request
    _text, target, source = parse_translation_request(text, ui_language=ui)
    assert target == "en" and source in {"fr", "de"} and source != ""


# ------------------------------------------------------------ settings sections / game kinds / scenarios

@pytest.mark.parametrize("code,text,section", [
    ("ru", "открой настройки книжной полки", "widgets"), ("ru", "открой дополнительные настройки", "advanced"),
    ("ru", "открой настройки окна", "window"), ("ru", "открой настройки робота", "robot"),
    ("ru", "открой настройки безопасности", "security"), ("en", "open bookshelf settings", "widgets"),
    ("de", "öffne die Ablageeinstellungen", "widgets"), ("pt", "abra as configurações da estante", "widgets"),
])
def test_settings_commands_name_a_stable_page(session, code, text, section):
    calls, _turn = _run(session, code, text)
    assert calls == [("desktop_features", {"action": "settings", "section": section})]


@pytest.mark.parametrize("code,text", [("ru", "открой настройки звука"), ("ru", "открой настройки микрофона")])
def test_windows_settings_pages_are_not_taken(session, code, text):
    calls, _turn = _run(session, code, text)
    assert calls and calls[0][0] == "winsettings"


def test_settings_dialog_resolves_page_ids_not_indices():
    from jarvis.ui.settings_dialog import SettingsDialog
    dialog = SettingsDialog.__new__(SettingsDialog)
    dialog._settings_page_ids186 = ("microphone", "media", "widgets", "advanced")
    assert dialog.page_index186("bookshelf") == 2 and dialog.page_index186("advanced") == 3
    assert dialog.page_index186("missing") is None


@pytest.mark.parametrize("code,text,kind", [("ru", "найди сохранения игры QA_Action_731", "saves"),
                                            ("en", "bring up installation folder for QA_Action_731", "install"),
                                            ("ru", "покажи папку установки игры QA_Action_731", "install")])
def test_game_location_kind_is_kept(session, code, text, kind):
    calls, _turn = _run(session, code, text)
    assert calls and calls[-1][0] == "game_locations" and calls[-1][1]["kind"] == kind


@pytest.mark.parametrize("text,name", [("QA Review Notes", "QA Review Notes"), ("Cedar Notes Pack", "Cedar Notes Pack"),
                                       ("run scenario QA Review Notes", "QA Review Notes")])
def test_exact_scenario_name_keeps_identity(session, text, name):
    rows = [{"id": "sc-qa-1", "name": "QA Review Notes", "phrase": "", "enabled": True, "steps": [{"kind": "wait", "seconds": 1}]},
            {"id": "sc-qa-2", "name": "Cedar Notes Pack", "phrase": "", "enabled": True, "steps": [{"kind": "wait", "seconds": 1}]}]
    with external_io_blocked():
        session.reset_case("en")
        session.config.data.setdefault("scenarios", {})["items"] = rows
        try:
            session.agent.handle(text)
            calls = [(c["tool"], dict(c["arguments"])) for c in session.calls]
        finally:
            session.config.data["scenarios"]["items"] = []
    assert calls and calls[-1][0] == "scenario" and calls[-1][1]["name"] == name
    assert calls[-1][1]["id"] == {"QA Review Notes": "sc-qa-1", "Cedar Notes Pack": "sc-qa-2"}[name]


# ------------------------------------------------------------ literal file names (found while classifying the catalogue)

@pytest.mark.parametrize("phrase,name", [
    ("прочитай документ отчёт.docx", "отчёт.docx"),
    ("прочитай файл отчёт.docx", "отчёт.docx"),
    ("прочитай файл годовой отчёт.docx", "годовой отчёт.docx"),
])
def test_file_name_is_never_repaired_into_another_word(session, phrase, name):
    with external_io_blocked():
        session.reset_case("ru")
        session.agent.handle(phrase)
    sources = [str((c["arguments"] or {}).get("source", "")) for c in session.calls if c["tool"] == "filesystem"]
    assert sources == [name], session.calls


def test_typo_rescue_leaves_file_name_tokens_alone():
    from jarvis.runtime.command_typos165 import _editable_tokens
    words = [m.group(0) for m in _editable_tokens("прочитай файл отчёт.docx", "ru")]
    assert "отчёт" not in words and "прочитай" in words
