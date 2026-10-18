# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import unittest
from pathlib import Path

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


class Hotfix91Regression(unittest.TestCase):
    def test_settings_v141_and_defaults(self):
        data = json.loads((INTERNAL / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["settings_version"], 141)
        self.assertEqual(data["hotkeys"]["chat_scroll_up"], "Ctrl+Alt+Up")
        self.assertTrue(data["autoclicker"]["enabled"])
        config_src = (J / "config.py").read_text(encoding="utf-8")
        self.assertRegex(config_src, r'self\.data\["settings_version"\] = (?:14[2-9]|1[5-9]\d)')

    def test_mic_meter_is_smooth_but_bounded(self):
        backend = (J / "audio_backend.py").read_text(encoding="utf-8")
        main = (J / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn('else 0.080', backend)
        self.assertIn('floor_ceiling = max(0.0022', backend)
        self.assertIn('self._mic_meter_timer.setInterval(33)', main)
        self.assertNotIn('Permanent standby receives only a couple of meter events/second', main)

    def test_audio_stall_watchdog(self):
        src = (J / "ui" / "audio_capture.py").read_text(encoding="utf-8")
        self.assertIn('now - self._last_event_at > 4.5', src)
        self.assertIn('Аудиопоток автоматически восстановлен после зависания.', src)

    def test_chat_follow_bottom_detaches(self):
        src = (J / "ui" / "chat_view.py").read_text(encoding="utf-8")
        self.assertIn('self._follow_bottom = True', src)
        self.assertIn('def _scroll_value_changed', src)
        self.assertIn('if not self._follow_bottom:', src)
        self.assertIn('def scroll_by_hotkey', src)

    def test_autoclicker_tool_and_settings(self):
        runtime = (J / "runtime" / "autoclicker.py").read_text(encoding="utf-8")
        tools = (J / "tools" / "domains" / "system_interaction.py").read_text(encoding="utf-8")
        settings = (J / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertIn('class AutoClickerController', runtime)
        self.assertIn('"autoclicker"', tools)
        self.assertIn('self.autoclicker_cps', settings)
        self.assertIn('self.autoclicker_button', settings)

    def test_global_scroll_hotkeys_and_topmost_voice(self):
        src = (J / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn('_chat_scroll_up_hotkey_id', src)
        self.assertIn('no_repeat=not repeat', src)
        self.assertIn('Qt.WA_ShowWithoutActivating', src)
        self.assertIn('SWP_NOACTIVATE', src)
        self.assertIn('self.hide()', src)
        self.assertIn('SW_SHOWNA', src)
        visibility = (J / "runtime" / "self_window184.py").read_text(encoding="utf-8")
        self.assertIn('будь поверх всех окон', visibility)
        self.assertIn('спрячься', visibility)

    def test_discord_slash_play_and_empty_leave(self):
        bot = (J / "integrations" / "background" / "discord_bot.py").read_text(encoding="utf-8")
        nav = (J / "integrations" / "discord_navigation.py").read_text(encoding="utf-8")
        self.assertIn('discord.app_commands.Group(name="play"', bot)   # NEO 180: /play now | next | last
        self.assertIn('humans = [m for m in', bot)
        self.assertIn('await live_voice.disconnect(force=True)', bot)
        self.assertIn('action == "voice_empty"', nav)
        self.assertIn('action == "play"', nav)

    def test_my_wave_survives_manual_queue_and_stop(self):
        nav = (J / "integrations" / "discord_navigation.py").read_text(encoding="utf-8")
        yandex = (J / "integrations" / "yandex_music.py").read_text(encoding="utf-8")
        self.assertIn('wave_active = str(state.get("source_mode"', nav)
        self.assertIn('elif wave_active:', nav)
        self.assertIn('while insert_at < len(items) and bool(items[insert_at].get("_force_next")):', nav)
        self.assertIn('without destroying the selected station', yandex)
        self.assertIn('transport_action = "pause" if preserve_wave_stop else mapped', nav)
        stop_block = yandex[yandex.index('if action == "stop":'):yandex.index('if action == "next":')]
        self.assertNotIn('_clear_background_sequence', stop_block)

    def test_queue_status_uses_nested_player_truth(self):
        src = (J / "tools" / "domains" / "browser_media.py").read_text(encoding="utf-8")
        self.assertIn('if action == "queue_status" and isinstance(data, dict) and not title:', src)
        self.assertIn('player_meta.get("title"', src)


if __name__ == "__main__":
    unittest.main()
