from __future__ import annotations

import json
from pathlib import Path
import unittest

from jarvis.quick_media_commands import parse_quick_media_command


class Hotfix125Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.internal = Path(__file__).resolve().parents[1]

    def text(self, relative: str) -> str:
        return (self.internal / relative).read_text(encoding="utf-8")

    def test_settings_version_and_security_migration_order(self):
        defaults = json.loads(self.text("config/default_settings.json"))
        self.assertGreaterEqual(defaults["settings_version"], 163)
        config = self.text("jarvis/config.py")
        self.assertIn(f'serializable["settings_version"] = {defaults["settings_version"]}', config)
        self.assertIn(f'self.data["settings_version"] = {defaults["settings_version"]}', config)
        self.assertLess(config.index("# HOTFIX124:"), config.index("# HOTFIX125:"))
        self.assertIn("if version < 159:", config)
        self.assertIn("if version < 160:", config)

    def test_phrase_atlas_is_developer_only_and_default_enabled(self):
        defaults = json.loads(self.text("config/default_settings.json"))
        self.assertTrue(defaults["routing"]["atlas2_enabled"])
        settings = self.text("jarvis/ui/settings_dialog.py")
        self.assertIn('atlas_group = QGroupBox("Phrase Atlas 2.0")', settings)
        self.assertIn("developer_advanced_layout.addWidget(atlas_group)", settings)
        self.assertIn("self.developer_advanced_container.setVisible(self.developer_settings_enabled.isChecked())", settings)
        self.assertEqual(settings.count("Phrase Atlas 2.0"), 1)
        self.assertNotIn("язык ответов и Phrase Atlas", settings)
        self.assertNotIn("удалённый вход в тот же Phrase Atlas", settings)

    def test_hotkey_and_storage_reset_controls_exist(self):
        settings = self.text("jarvis/ui/settings_dialog.py")
        self.assertIn('clear = QPushButton("Очистить")', settings)
        self.assertIn('reset = QPushButton("По умолчанию")', settings)
        self.assertIn('self.default_button = QPushButton("По умолчанию")', settings)

    def test_fast_volume_repeat_shuffle_commands(self):
        self.assertEqual(parse_quick_media_command("громче", "ru").action, "volume_up")
        self.assertEqual(parse_quick_media_command("тише", "ru").action, "volume_down")
        self.assertEqual(parse_quick_media_command("повтор", "ru").action, "repeat_toggle")
        self.assertEqual(parse_quick_media_command("перемешай", "ru").action, "shuffle_toggle")
        main = self.text("jarvis/ui/main_window.py")
        self.assertIn("if value > 10:", main)
        self.assertIn("value - 10", main)
        self.assertIn("if value > 5:", main)
        self.assertIn("value - 5", main)
        self.assertIn("value - 1", main)
        self.assertIn('if target == "discord":', main)
        # 145 keeps the Discord boundary, but framing now lives in the native
        # message catalogue. Exercise the real branch without external effects.
        from types import SimpleNamespace
        from unittest.mock import Mock
        from jarvis.quick_media_commands import QuickMediaCommand
        from jarvis.ui.main_window import MainWindow
        from jarvis.ui.quick_media_messages import message
        for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
            for action in ('volume_up', 'volume_down', 'repeat_toggle', 'shuffle_toggle'):
                with self.subTest(language=code, action=action):
                    transport = Mock()
                    window = SimpleNamespace(_language_code=lambda: code, agent=SimpleNamespace(tools=SimpleNamespace(execute=transport)))
                    result = MainWindow._execute_quick_media_command(window, QuickMediaCommand(action, 'discord', action))
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['text'], message('not_discord', code))
                    transport.assert_not_called()

    def test_quick_music_settings_and_wake_latency_defaults(self):
        defaults = json.loads(self.text("config/default_settings.json"))
        self.assertTrue(defaults["voice"]["quick_media_volume_enabled"])
        self.assertTrue(defaults["voice"]["quick_media_repeat_shuffle_enabled"])
        wake = defaults["wake_word"]
        self.assertLessEqual(float(wake["probe_interval_seconds"]), 0.14)
        self.assertLessEqual(float(wake["fast_asr_probe_interval_seconds"]), 0.20)
        self.assertLessEqual(float(wake["fast_asr_probe_min_audio_seconds"]), 0.18)
        self.assertEqual(int(wake["fast_asr_probe_max_per_utterance"]), 2)

    def test_quick_command_working_state_without_spurious_pause(self):
        main = self.text("jarvis/ui/main_window.py")
        self.assertIn('self.robot.set_state("working")', main)
        # 146: the visual ACK replaces the old artificial pause before a switch.
        # A switch failure must not leave music paused by an unrelated command.
        self.assertNotIn('self.agent.tools.execute(owner, {"action": "pause"})', main)
        self.assertIn('self._quick_media_pool.setMaxThreadCount(1)', main)
        self.assertIn('target_interval = 125 if bool(media.get("playing", False)) else 750', main)

    def test_bare_play_music_routes_without_empty_track_query(self):
        router = self.text("jarvis/runtime/router.py")
        self.assertIn('RoutedIntent("music_play_generic", "music", {"action": "play_music"}', router)
        media = self.text("jarvis/tools/domains/browser_media.py")
        self.assertIn('"play_song": "play_track", "play_music": "my_wave"', media)
        yandex = self.text("jarvis/integrations/yandex_music.py")
        self.assertIn('"play_song": "play_track", "play_music": "my_wave"', yandex)

    def test_yandex_wave_and_single_prefetch_window(self):
        yandex = self.text("jarvis/integrations/yandex_music.py")
        self.assertIn('"play_music": "my_wave"', yandex)
        self.assertIn('self._station_tracks("user:onyourwave"', yandex)
        self.assertIn("def _ensure_wave_queue_depth(self, minimum: int = 8)", yandex)
        self.assertIn("minimum = max(5, min(10", yandex)
        self.assertIn("self._prefetch_window_thread", yandex)
        self.assertIn("self._prefetch_window_event.wait()", yandex)
        self.assertEqual(yandex.count('name="neo-yandex-prefetch-window"'), 1)

    def test_telegram_fast_menu_volume_timer_and_single_home(self):
        tg = self.text("jarvis/integrations/telegram_remote.py")
        # TG02 moves briefing between mail/calendar; settings replaces its old slot.
        self.assertIn('("🧹 Очистить историю", "a:tg:clearhistory"), ("⚙️ Настройки", "ui:settings"), ("⏱ Таймер", "ui:timer")', tg)
        for marker in ("a:musicvolrel:+10", "a:musicvolrel:+5", "a:musicvolrel:-5", "a:musicvolrel:-10",
                       "a:dcvolrel:+10", "a:dcvolrel:+5", "a:dcvolrel:-5", "a:dcvolrel:-10"):
            self.assertIn(marker, tg)
        self.assertIn("def _show_timer_menu", tg)
        self.assertIn('self._cleanup_chat(chat_id, recreate_home=False, aggressive=True)\n            self._show_home(chat_id, 0)', tg)
        self.assertIn('if data == "ui:timer": self._show_timer_menu(chat_id, message_id); return', tg)
        self.assertIn("_media_history_by_source", tg)
        self.assertIn("def _media_history_suffix", tg)

    def test_timer_precise_one_second_and_appearance(self):
        main = self.text("jarvis/ui/main_window.py")
        self.assertIn("self._timer_event_timer.setTimerType(Qt.PreciseTimer)", main)
        self.assertIn("self._timer_event_timer.setInterval(1000)", main)
        self.assertIn("math.ceil(item.due_at-now)", main)
        settings = self.text("jarvis/ui/settings_dialog.py")
        for label in ("Прозрачность", "Стиль", "Цвет текста", "Цвет фона", "Цвет рамки"):
            self.assertIn(label, settings)
        self.assertIn('("Сверху по центру", "top_center")', settings)
        self.assertIn('("По центру", "center")', settings)

    def test_youtube_transport_does_not_require_data_api(self):
        media = self.text("jarvis/tools/domains/browser_media.py")
        self.assertIn('if action == "play_search":', media)
        api_gate = 'if not bool(config.get("integrations.youtube.enabled", True)):'
        self.assertEqual(media.count(api_gate), 1)
        self.assertIn("Пауза/продолжение видео работают независимо от YouTube Data API", media)

    def test_discord_dashboard_preserves_history_and_timers(self):
        bot = self.text("jarvis/integrations/background/discord_bot.py")
        self.assertIn("await self._append_played_track_history(channel, previous_current)", bot)
        self.assertIn("async def _set_timer_summaries", bot)
        self.assertIn("self._timer_dashboard_messages", bot)
        self.assertNotIn("await self._clear_music_channel_history(guild)", bot)
        self.assertIn("_music_track_history", bot)
        self.assertIn("_music_current_meta", bot)
        self.assertIn("self._music_current_meta.get(gid)", bot)

    def test_discord_wave_keeps_five_warm_queue_items(self):
        nav = self.text("jarvis/integrations/discord_navigation.py")
        self.assertIn("def _ensure_wave_queue_depth_background", nav)
        self.assertIn("wave_background_refill_active", nav)
        self.assertIn("for entry in items[:5]", nav)
        import re
        depths = re.findall(r"_ensure_wave_queue_depth_background\(profile, (\d+)\)", nav)
        self.assertTrue(depths)
        self.assertTrue(all(5 <= int(depth) <= 10 for depth in depths))

    def test_robot_music_animation_exists(self):
        robot = self.text("jarvis/ui/robot_widget.py")
        self.assertIn('"music_bop"', robot)
        self.assertIn("drop = (1.0 - intro) * 88.0 * scale", robot)
        self.assertIn("lane_xs = _four_lanes(left_start, left_end) + _four_lanes(right_start, right_end)", robot)

    def test_user_facing_core_brand_is_neo(self):
        paths = [
            "app.py", "jarvis/ui/main_window.py", "jarvis/ui/settings_dialog.py",
            "jarvis/ui/chat_view.py", "jarvis/ui/log_viewer.py", "jarvis/integrations/telegram_remote.py",
        ]
        for relative in paths:
            value = self.text(relative)
            self.assertNotIn("NEO JARVIS", value, relative)
        self.assertIn('app.setApplicationName("NEO")', self.text("app.py"))
        self.assertIn('self.setWindowTitle("NEO")', self.text("jarvis/ui/main_window.py"))


    def test_visible_branding_and_legacy_names_are_not_runtime_identity(self):
        agent = self.text("jarvis/agent.py")
        self.assertNotIn('assistant_name", "Jarvis"', agent)
        self.assertNotIn("assistant_name', 'Jarvis'", agent)
        startup = self.text("jarvis/windows_startup.py")
        self.assertIn('TASK_NAME = r"NEO Priority"', startup)
        self.assertIn('WAKE_TASK_NAME = r"NEO Wake Service"', startup)
        unlock = self.text("jarvis/windows_voice_unlock.py")
        self.assertIn('_NATIVE_PROVIDER_NAME = "NEO Voice Unlock"', unlock)
        settings = self.text("jarvis/ui/settings_dialog.py")
        self.assertNotIn("Переменные SEARCHAPI_API_KEY и NEO_JARVIS_SEARCHAPI_API_KEY", settings)

    def test_yandex_quick_background_status_is_local_only(self):
        yandex = self.text("jarvis/integrations/yandex_music.py")
        marker = 'if action == "background_status":'
        start = yandex.index(marker)
        end = yandex.index('raise ValueError(', start)
        block = yandex[start:end]
        self.assertNotIn("self.api_status()", block)
        self.assertIn('"volume_percent"', block)
        self.assertIn('"repeat_mode"', block)
        self.assertIn('"shuffle"', block)
        self.assertIn('"media_source"', block)

    def test_long_session_media_ownership_and_telegram_ui_fast_path(self):
        main = self.text("jarvis/ui/main_window.py")
        self.assertIn("recent_music_owner(max_age_seconds=24 * 60 * 60)", main)
        self.assertIn("local_player().cached_status()", main)  # UI snapshots must not block on IPC
        tg = self.text("jarvis/integrations/telegram_remote.py")
        callback_start = tg.index("def _handle_callback")
        callback = tg[callback_start:]
        self.assertLess(callback.index('if data == "ui:h":'), callback.index('# Yandex Music.'))
        self.assertIn('if data == "ui:timer": self._show_timer_menu(chat_id, message_id); return', callback)
        self.assertIn("self._answer_callback(callback_id)", callback[:3000])

    def test_command_atlas_has_no_visible_wake_brand_prefixes(self):
        import re
        import json
        commands_dir = self.internal / "jarvis" / "locales" / "commands"
        for path in sorted(commands_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for phrases in (payload.get("action_phrases", {}) or {}).values():
                for phrase in phrases if isinstance(phrases, list) else []:
                    self.assertIsNone(re.search(r"\b(?:jarvis|джарвис)\b", str(phrase), re.I), f"{path.name}: {phrase}")

    def test_spotify_background_status_has_short_cache(self):
        spotify = self.text("jarvis/integrations/spotify.py")
        self.assertIn("self._status_cache", spotify)
        self.assertIn('if action == "background_status":', spotify)
        self.assertIn("allow_cached=True", spotify)


if __name__ == "__main__":
    unittest.main()
