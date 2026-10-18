# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.integrations.telegram_messages import TelegramMessageCenter
from jarvis.runtime.router import HybridRouter

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


class DummyConfig:
    def __init__(self, root: Path, values=None):
        self.paths = SimpleNamespace(root=root)
        self.values = dict(values or {})
    def get(self, key, default=None):
        return self.values.get(key, default)


class Hotfix98Regression(unittest.TestCase):
    def test_settings_v146_and_version_markers(self):
        data = json.loads((INTERNAL / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(int(data.get("settings_version", 0)), 146)
        version = (INTERNAL / "VERSION.txt").read_text(encoding="utf-8")
        from tests.release_contracts import assert_release
        assert_release(self, version)

    def test_telegram_update_is_handled_before_ack(self):
        src = (J / "integrations" / "telegram_remote.py").read_text(encoding="utf-8")
        loop = src[src.index('for update in result or []:'):src.index('except Exception as exc:', src.index('for update in result or []:'))]
        self.assertLess(loop.index('self._handle_update(update)'), loop.index('self._commit_update_id(update_id)'))

    def test_telegram_latest_is_all_locally_observed_history(self):
        with tempfile.TemporaryDirectory() as td:
            c = TelegramMessageCenter(DummyConfig(Path(td), {}))
            c.record_connection({'id':'bc','user':{'id':1,'first_name':'Owner'},'is_enabled':True,'rights':{'can_reply':True}})
            c.record_message({'business_connection_id':'bc','message_id':1,'date':1,'chat':{'id':2,'type':'private'},'from':{'id':2,'first_name':'Old'},'text':'старое'})
            c.record_message({'business_connection_id':'bc','message_id':2,'date':2,'chat':{'id':3,'type':'private'},'from':{'id':3,'first_name':'New'},'text':'последнее'})
            self.assertEqual(c.latest()['text'], 'последнее')
            self.assertIn('last_business_update_at', c.connection_status())

    def test_telegram_menu_pair_layout(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        block=src[src.index('def _home_keyboard'):src.index('def _panel',src.index('def _home_keyboard'))]
        self.assertIn('[("🎙 Ввод", "ui:mic"), ("🖱 Мышь", "ui:mouse"), ("🎧 Вывод", "ui:vol")]', block)
        self.assertIn('[("⌨️ Нажать клавишу", "ui:key"), ("🖱 Нажать мышью", "ui:rmouse")]', block)
        # TG02: the briefing now deliberately sits between mail and schedule.
        self.assertIn('[("📧 Почта", "ui:mail"), ("📰 Сводка дня", "ui:brief"), ("📅 Расписание", "ui:cal")]', block)

    def test_remote_mouse_monitor_starts_one_line_xy_without_button_screen(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        start=src.index('if data.startswith("a:rmouse:mon:")')
        block=src[start:src.index('# Legacy HOTFIX96 callback',start)]
        self.assertIn('"remote_mouse_xy"', block)
        self.assertNotIn('a:rmouse:btn:', block)
        self.assertIn('_remote_mouse_button_by_chat', src)

    def test_applications_menu_has_active_and_volume(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        block=src[src.index('def _show_apps_menu'):src.index('def _scenario_items')]
        self.assertIn('Активные приложения', block)
        self.assertIn('Громкость приложения', block)
        self.assertIn('list_sessions', block)

    def test_application_tool_lists_visible_ui(self):
        src=(J/'tools'/'implementations.py').read_text(encoding='utf-8')
        self.assertIn('if action == "list_active"', src)
        self.assertIn('_win32_top_level_windows(include_hidden=False)', src)
        self.assertIn('_window_item_is_user_facing', src)

    def test_audio_tool_exposes_sessions(self):
        src=(J/'tools'/'domains'/'audio.py').read_text(encoding='utf-8')
        self.assertIn('def _list_audio_sessions()', src)
        self.assertIn('if action == "list_sessions"', src)
        self.assertIn('AudioUtilities.GetAllSessions()', src)

    def test_discord_user_account_automation_removed_from_phone_menu(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        block=src[src.index('def _show_discord_media'):src.index('def _discord_server_names')]
        self.assertNotIn('Войти в голосовой чат', block)
        self.assertNotIn('Отправить в текстовый чат', block)
        self.assertIn('обычным Discord user-аккаунтом', src)

    def test_discord_user_account_commands_fail_closed(self):
        router=HybridRouter()
        r=router.route('зайди в голосовой канал Общий в Discord')
        self.assertEqual(r.tool,'discord')
        self.assertEqual(r.arguments.get('action'),'user_account_automation_unavailable')
        r=router.route('напиши в Discord сообщение привет')
        self.assertEqual(r.arguments.get('action'),'user_account_automation_unavailable')

    def test_secretary_mode_keeps_telegram_transport_alive_after_settings_save(self):
        src=(J/'ui'/'main_window.py').read_text(encoding='utf-8')
        self.assertIn('telegram_transport_needed = bool(', src)
        self.assertIn('integrations.telegram.messages_enabled', src)
        self.assertIn('telegram_remote.start()', src)

    def test_searchapi_enable_toggle_is_explicit_and_groups_collapsible(self):
        src=(J/'ui'/'settings_dialog.py').read_text(encoding='utf-8')
        self.assertIn('Включить игровой веб-поиск Google AI Mode через SearchAPI.io', src)
        self.assertIn('def _bind_collapsible_group', src)
        for token in ('openweather_enabled','gmail_enabled','mailru_enabled','game_web_enabled','yandex_integration_enabled','discord_integration_enabled','telegram_messages_enabled'):
            self.assertIn(token, src)

    def test_telegram_diagnostic_ui_exists(self):
        src=(J/'ui'/'settings_dialog.py').read_text(encoding='utf-8')
        self.assertIn('Проверить получение сообщений', src)
        self.assertIn('_telegram_check_messages_status', src)
        self.assertIn('Старую историю Telegram Bot API не выдаёт', src)


if __name__ == '__main__':
    unittest.main()
