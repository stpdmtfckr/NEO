from __future__ import annotations

from jarvis.runtime.speech_understanding import SpeechUnderstandingEngine


class Config:
    def __init__(self, values=None):
        self.values = values or {}
    def get(self, key, default=None):
        return self.values.get(key, default)


class Memory:
    def state_snapshot(self):
        return {"last_application": "Discord", "recent_processes": ["Telegram", "Steam"]}


class Agent:
    context_memory = Memory()


def engine():
    obj = SpeechUnderstandingEngine(Config(), agent=Agent())
    # Avoid platform-dependent Start Menu/process enumeration in unit tests.
    obj._snapshot_at = 10**12
    obj._entities = obj._refresh_entities() if False else []
    from jarvis.runtime.speech_understanding import _Entity
    obj._entities = [
        _Entity("Discord", ("discord", "дискорд", "дискорт"), "builtin", active=True, recent=True),
        _Entity("Telegram", ("telegram", "телеграм", "телеграмм"), "builtin", active=False, recent=True),
        _Entity("Steam", ("steam", "стим", "стеам"), "builtin", active=False, recent=False),
        _Entity("Yandex Music", ("yandex music", "яндекс музыка", "яндекс музыку"), "builtin", active=False, recent=False),
    ]
    return obj


def test_contextual_app_repair():
    result = engine().resolve("открой дискорт", language="ru")
    assert result.text == "открой Discord"


def test_explicit_self_correction_keeps_action():
    result = engine().resolve("открой телеграм, нет, дискорд", language="ru")
    assert result.text == "открой Discord"


def test_explicit_self_correction_can_restart_command():
    result = engine().resolve("открой телеграм, нет, закрой дискорд", language="ru")
    assert result.text == "закрой Discord"


def test_free_song_payload_is_not_rewritten():
    result = engine().resolve("найди песню дискорт", language="ru")
    assert result.text == "найди песню дискорт"


def test_file_payload_is_not_rewritten():
    result = engine().resolve("удали файл Discord.txt", language="ru")
    assert result.text == "удали файл Discord.txt"


def test_alternate_hypothesis_can_win_on_known_entity():
    result = engine().resolve(
        "открой с ним",
        alternatives=[{"text": "открой стим", "score": -0.2, "source": "same_language_rescue"}],
        language="ru",
    )
    assert result.text == "открой Steam"

class DiscordMemory:
    def state_snapshot(self):
        return {"last_domain": "discord", "last_discord_channel": "general"}


class DiscordAgent:
    context_memory = DiscordMemory()
    adaptation = None


def test_discord_channel_uses_recent_domain_without_global_autocorrect():
    from jarvis.runtime.speech_understanding import _Entity
    obj = SpeechUnderstandingEngine(Config(), agent=DiscordAgent())
    obj._snapshot_at = 10**12
    obj._entities = [
        _Entity("general", ("general", "дженерал"), "discord", recent=True, kind="discord_channel"),
    ]
    result = obj.resolve("перейди в дженерал", language="ru")
    assert result.text == "перейди в general"


def test_telegram_recipient_repair_does_not_touch_message_body():
    from jarvis.runtime.speech_understanding import _Entity
    class TelegramMemory:
        def state_snapshot(self):
            return {"last_domain": "telegram_messages"}
    class TelegramAgent:
        context_memory = TelegramMemory()
        adaptation = None
    obj = SpeechUnderstandingEngine(Config(), agent=TelegramAgent())
    obj._snapshot_at = 10**12
    obj._entities = [
        _Entity("Иван", ("иван", "ивану", "ивана"), "telegram", recent=True, kind="telegram_contact"),
        _Entity("Discord", ("дискорт",), "builtin", kind="application"),
    ]
    result = obj.resolve("напиши ивану что дискорт опять завис", language="ru")
    assert result.text == "напиши Иван что дискорт опять завис"


def test_current_folder_entity_can_repair_only_file_command(tmp_path):
    target = tmp_path / "TelegramBridge.py"
    target.write_text("print('ok')", encoding="utf-8")
    class FileMemory:
        def state_snapshot(self):
            return {"last_folder_path": str(tmp_path)}
        def system_snapshot(self):
            return {}
    class FileAgent:
        context_memory = FileMemory()
        adaptation = None
    obj = SpeechUnderstandingEngine(Config(), agent=FileAgent())
    obj._snapshot_at = 10**12
    obj._entities = []
    result = obj.resolve("открой файл телеграм бридж", language="ru")
    assert result.text == "открой файл TelegramBridge.py"
    untouched = obj.resolve("найди песню телеграм бридж", language="ru")
    assert untouched.text == "найди песню телеграм бридж"


def test_confirmed_adaptive_correction_is_visible_before_chat_dispatch():
    class Resolved:
        text = "открой Brakk"
    class Adaptive:
        def resolve(self, text, **kwargs):
            return Resolved() if text == "открой брак" else type("R", (), {"text": text})()
    class AdaptiveMemory:
        def state_snapshot(self):
            return {}
        def record_speech_resolution(self, payload):
            self.payload = payload
    memory = AdaptiveMemory()
    class AdaptiveAgent:
        context_memory = memory
        adaptation = Adaptive()
        def _active_game_id(self):
            return "testgame"
    # HOTFIX143 made direct learned rewriting opt-in; test the enabled feature
    # explicitly instead of depending on the old unsafe default.
    obj = SpeechUnderstandingEngine(Config({"voice.speech_adaptive_rewrite_enabled": True}), agent=AdaptiveAgent())
    obj._snapshot_at = 10**12
    obj._entities = []
    result = obj.resolve("открой брак", language="ru")
    assert result.text == "открой Brakk"
    assert memory.payload["raw"] == "открой брак"
    assert memory.payload["text"] == "открой Brakk"


def test_non_command_conversation_is_not_entity_rewritten():
    from jarvis.runtime.speech_understanding import _Entity
    obj = engine()
    obj._entities.append(_Entity("Steam", ("с ним",), "builtin", kind="application"))
    result = obj.resolve("я вчера говорил с ним", language="ru")
    assert result.text == "я вчера говорил с ним"


def test_german_command_can_repair_known_application_name():
    from jarvis.runtime.speech_understanding import _Entity
    obj = SpeechUnderstandingEngine(Config(), agent=Agent())
    obj._snapshot_at = 10**12
    obj._entities = [_Entity("Telegram", ("telegram", "telegam"), "builtin", kind="application")]
    result = obj.resolve("öffne telegam", language="de")
    assert result.text == "öffne Telegram"


def test_file_name_in_normal_conversation_is_not_rewritten(tmp_path):
    (tmp_path / "TelegramBridge.py").write_text("x", encoding="utf-8")
    class FileMemory:
        def state_snapshot(self): return {"last_folder_path": str(tmp_path)}
        def system_snapshot(self): return {}
    class FileAgent:
        context_memory = FileMemory()
        adaptation = None
    obj = SpeechUnderstandingEngine(Config(), agent=FileAgent())
    obj._snapshot_at = 10**12
    obj._entities = []
    result = obj.resolve("этот файл телеграм бридж мне не нужен", language="ru")
    assert result.text == "этот файл телеграм бридж мне не нужен"


def test_contextual_rescue_terms_trigger_only_for_plausible_local_entity():
    from jarvis.runtime.speech_understanding import _Entity
    obj = SpeechUnderstandingEngine(Config(), agent=Agent())
    obj._snapshot_at = 10**12
    obj._entities = [_Entity("Firefox", ("firefox", "файрфокс"), "installed", kind="application")]
    assert obj.contextual_rescue_terms("открой файрфох", language="ru") == ["Firefox"]
    assert obj.contextual_rescue_terms("найди песню файрфох", language="ru") == []
