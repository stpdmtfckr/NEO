# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations
import json, re, unittest
from pathlib import Path

INTERNAL=Path(__file__).resolve().parents[1]
J=INTERNAL/'jarvis'

from tests.release_contracts import assert_release

from tests.release_contracts import assert_youtube_menu

class Hotfix101Regression(unittest.TestCase):
    def text(self, rel): return (INTERNAL/rel).read_text(encoding='utf-8')

    def test_first_launch_english_and_yandex_disabled(self):
        cfg=json.loads((INTERNAL/'config/default_settings.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(cfg['settings_version'],163)
        self.assertEqual(cfg['app']['language'],'en')
        self.assertEqual(cfg['voice']['command_language'],'en')
        self.assertFalse(cfg['integrations']['yandex_music']['enabled'])
        self.assertEqual(cfg['integrations']['music']['active_provider'],'')
        self.assertNotIn('apple_music',cfg['integrations'])

    def test_yandex_runtime_uses_enabled_provider_in_all_languages(self):
        # 145: the requested language parity replaces the former RU-only gate;
        # switching language must not rewrite or enable a user's account choice.
        from types import SimpleNamespace
        from jarvis.integrations.music_provider import active_music_provider
        for language in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
            with self.subTest(language=language):
                values = {'app.language': language, 'integrations.music.active_provider': 'yandex_music',
                          'integrations.yandex_music.enabled': True, 'integrations.spotify.enabled': False}
                config = SimpleNamespace(get=values.get)
                before = dict(values)
                self.assertEqual(active_music_provider(config), 'yandex_music')
                self.assertEqual(values, before)
                values['integrations.yandex_music.enabled'] = False
                self.assertEqual(active_music_provider(config), '')
                self.assertFalse(values['integrations.yandex_music.enabled'])

    def test_settings_cleanup(self):
        src=self.text('jarvis/ui/settings_dialog.py')
        self.assertNotIn('Прозрачность сцены робота',src)
        self.assertNotIn('Standard HOTFIX',src)
        self.assertIn('self.developer_settings_enabled = QCheckBox("Включить режим разработчика")',src)
        self.assertIn('self._adopt_scroll_sections(developer_advanced_layout, self._models_tab())',src)
        self.assertIn('setPlaceholderText("Иван")',src)
        self.assertIn('setPlaceholderText("Семья; Проект")',src)
        self.assertIn('OAuth подключён',src)
        self.assertIn('Дополнительные возможности',src)

    def test_robot_scene_follows_window_opacity(self):
        src=self.text('jarvis/ui/main_window.py')
        self.assertNotIn('robot_background_opacity_percent',src)
        self.assertIn('self.robot.set_background_opacity(effective_window_opacity)',src)

    def test_telegram_layout_and_dpi(self):
        src=self.text('jarvis/integrations/telegram_remote.py')
        home=src.split('def _home_keyboard',1)[1].split('def _panel',1)[0]
        self.assertIn('("🎵 Музыка", "ui:mm")',home)
        self.assertIn('("📸 Скриншот", "ui:shot")',home)
        self.assertIn('[("🎙 Ввод", "ui:mic"), ("🖱 Мышь", "ui:mouse"), ("🎧 Вывод", "ui:vol")]',home)
        self.assertIn('[("⌨️ Нажать клавишу", "ui:key"), ("🖱 Нажать мышью", "ui:rmouse")]',home)
        apps=src.split('def _show_apps_menu',1)[1].split('def _show_app_volume_menu',1)[0]
        self.assertIn('Работа с окнами',apps)
        self.assertIn('{400:4, 800:8, 1200:10, 1600:12, 2400:16, 3200:20}',src)
        self.assertIn('аппаратный DPI сенсора',src)

    def test_telegram_music_volume_and_single_panel(self):
        src=self.text('jarvis/integrations/telegram_remote.py')
        music=src.split('def _show_music_menu',1)[1].split('def _show_youtube_menu',1)[0]
        self.assertNotIn('Громкость Windows',music)
        self.assertIn('a:musicvol:100',music)
        self.assertIn('"action":"set_volume"',src)
        self.assertIn('_panel_roll_in_progress',src)
        self.assertIn('pre-stop tracked-history cleanup',src)

    def test_youtube_relative_seek_current_plus_delta(self):
        src=self.text('jarvis/browser_control.py')
        exact=src.split('async def _control_exact_tab_media_session_async',1)[1]
        self.assertIn('target = max(0.0, before_position + sign * requested)', exact)
        self.assertIn('try_change_playback_position_async', exact)
        ctl=src.split('def control_service_media',1)[1].split('def selected_browser_tab',1)[0]
        self.assertIn('"seek_forward", "seek_backward", "seek_to"', ctl)
        self.assertIn('_control_exact_tab_media_session(passive, action, "video", seconds=seconds)', ctl)
        self.assertNotIn('_youtube_keyboard_seek(tab', ctl)

    def test_youtube_unsupported_navigation_removed(self):
        # Transport remains background-only; explicit layout controls are restored.
        assert_youtube_menu(self, (J / "integrations/telegram_remote.py").read_text(encoding="utf-8"))

    def test_telegram_core_button_localization_complete(self):
        src=self.text('jarvis/integrations/telegram_remote.py')
        for label in (
            '15 с','15 с ⏩','30 с','30 с ⏩','1 мин','1 мин ⏩','7 дней',
            'Включить','Выключить','Выключить дисплеи','Гибернация','Перезагрузка',
            'Заблокировать','Разблокировать','План питания','Энергосбережение',
            'Обновить погоду','Всё расписание','Сегодня','Завтра','Удалить',
            'Редактировать','Сопряжение','Яркость','Своё / текст / комбинация'
        ):
            self.assertIn(f'"{label}":{{"en":', src, label)

    def test_no_runtime_apple_music(self):
        self.assertFalse((J/'integrations/apple_music.py').exists())
        settings=self.text('jarvis/ui/settings_dialog.py')
        self.assertNotIn('Активировать Apple Music',settings)

    def test_version_marker(self):
        # Release names evolve; preserved feature contracts are tested below.
        assert_release(self, (INTERNAL / "VERSION.txt").read_text(encoding="utf-8"))

if __name__=='__main__': unittest.main()
