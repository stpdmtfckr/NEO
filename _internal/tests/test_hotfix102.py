# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import unittest
from pathlib import Path

from jarvis.runtime.media_events import publish_now_playing, latest_now_playing

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


from tests.release_contracts import assert_youtube_menu

class Hotfix102Regression(unittest.TestCase):
    def text(self, rel: str) -> str:
        return (INTERNAL / rel).read_text(encoding="utf-8")

    def test_settings_v150_and_developer_switch(self):
        cfg = json.loads((INTERNAL / "config/default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(cfg["settings_version"], 163)
        self.assertFalse(cfg["developer_mode"]["settings_enabled"])
        src = self.text("jarvis/ui/settings_dialog.py")
        self.assertIn('QCheckBox("Включить режим разработчика")', src)
        self.assertIn('developer_advanced_container.setVisible', src)
        self.assertIn('self._models_tab()', src)
        self.assertIn('self._developer_tab()', src)
        self.assertIn('developer["settings_enabled"]', src)

    def test_queue_activity_does_not_replace_now_playing(self):
        source = "discord_hotfix102_test"
        publish_now_playing(source, "Current Track", "Artist A", "track-current")
        before = latest_now_playing(source)
        self.assertIsNotNone(before)
        self.assertEqual(before.title, "Current Track")
        publish_now_playing(
            source, "Future Track", "Artist B", "track-future",
            reason="queue_added", playing=False, requester="Tester", queue_position=3,
        )
        after = latest_now_playing(source)
        self.assertIsNotNone(after)
        self.assertEqual(after.title, "Current Track")
        self.assertEqual(after.track_id, "track-current")

    def test_external_discord_play_publishes_requester(self):
        src = self.text("jarvis/integrations/discord_navigation.py")
        block = src.split('if action == "play":', 1)[1].split('elif action == "previous":', 1)[0]
        for required in ('reason="queue_added"', 'requester=requester', 'queue_position=position'):
            self.assertIn(required, block)
        self.assertIn("publish_music_state", src)
        # Publishing queue activity must not force recreation of the dashboard.
        self.assertNotIn("reanchor=True", block)

    def test_desktop_and_telegram_render_queue_activity(self):
        desktop = self.text("jarvis/ui/main_window.py")
        self.assertIn('reason == "queue_added"', desktop)
        # NEO 156 moved the chat line into the localized media_notice156 helper.
        self.assertIn('queue_added_notice(', desktop)
        from jarvis.runtime.media_notice156 import queue_added_notice
        self.assertEqual(queue_added_notice('Вася', '«X»', 2, 'ru'), 'Вася добавил в очередь «X» · позиция 2.')
        self.assertIn('mirror_telegram=False', desktop)
        telegram = self.text("jarvis/integrations/telegram_remote.py")
        self.assertIn('reason == "queue_added"', telegram)
        self.assertIn('➕ Discord:', telegram)
        self.assertIn('requester', telegram)

    def test_telegram_strict_single_menu(self):
        src = self.text("jarvis/integrations/telegram_remote.py")
        roll = src.split('def _roll_panel_to_bottom', 1)[1].split('# ------------------------------------------------------------------- config', 1)[0]
        self.assertIn('old panel is deleted *before* its replacement', roll)
        self.assertLess(roll.find('_delete_one(chat_key, old_id)'), roll.find('_show_view(chat_key'))
        self.assertNotIn('create the replacement panel at the current bottom, and only then delete the old panel', roll.casefold())
        self.assertIn('"panels": {', src)
        self.assertIn('_panel_locks', src)
        self.assertIn('with self._panel_lock(chat_key):', src)

    def test_youtube_menu_background_only(self):
        # Transport remains background-only; explicit layout controls are restored.
        assert_youtube_menu(self, (J / "integrations/telegram_remote.py").read_text(encoding="utf-8"))

    def test_youtube_relative_seek_one_gsmtc_session(self):
        src = self.text("jarvis/browser_control.py")
        exact = src.split('async def _control_exact_tab_media_session_async', 1)[1]
        self.assertIn('before_position = float(pos.total_seconds())', exact)
        self.assertIn('target = max(0.0, before_position + sign * requested)', exact)
        self.assertIn('try_change_playback_position_async', exact)
        control = src.split('def control_service_media', 1)[1].split('def selected_browser_tab', 1)[0]
        self.assertIn('youtube_background = {"play", "pause", "play_pause", "seek_forward", "seek_backward", "seek_to", "status", "next", "previous"}', control)  # NEO 187 HREQ-026
        self.assertIn('_control_exact_tab_media_session(passive, action, "video", seconds=seconds)', control)
        self.assertNotIn('pyautogui', control)
        self.assertNotIn('_youtube_active_tab(', control)

    def test_youtube_unsupported_removed_from_public_tool(self):
        src = self.text("jarvis/tools/domains/browser_media.py")
        enum = src.split('"action": _enum([', 1)[1].split(']', 1)[0]
        for removed in ('next', 'previous', 'shorts_next', 'shorts_previous', 'fullscreen', 'theater', 'set_speed', 'set_quality'):
            self.assertNotIn(removed, enum)
        phrases = self.text("jarvis/runtime/media_phrases.py")
        youtube = phrases.split('if has_youtube:', 1)[1].split('if has_spotify', 1)[0]
        for removed in ('youtube_shorts_next', 'youtube_fullscreen', 'youtube_theater', 'youtube_set_speed', 'youtube_set_quality', 'youtube_next', 'youtube_previous'):
            self.assertNotIn(removed, youtube)

    def test_discord_internal_bus_is_private(self):
        # Migration: the current shared bus is ephemeral, not a private channel.
        src = self.text("jarvis/integrations/background/discord_bot.py")
        send = src.split("async def _send_control", 1)[1].split("def send_control", 1)[0]
        self.assertNotIn("_ensure_sync_channel(", send)
        self.assertIn("await self._ensure_music_channel(guild)", send)
        self.assertIn('_public_control_data(packet["data"])', send)
        self.assertIn('body = "||" + raw_body + "||"', send)
        self.assertIn("await asyncio.sleep(0.12)", send)
        self.assertIn("await message.delete()", send)
        self.assertIn('declared != author_id', src)
        self.assertIn('not bool(getattr(author, "bot", False))', src)

    def test_discord_dashboard_no_mass_cleanup_on_reanchor(self):
        src = self.text("jarvis/integrations/background/discord_bot.py")
        publish = src.split("async def _publish_music_state", 1)[1].split("def publish_music_state", 1)[0]
        self.assertIn("if history_appended and message is not None:", publish)
        self.assertIn("asyncio.create_task(message.edit(content=text, view=view))", publish)
        self.assertIn("await asyncio.wait({edit_task}, timeout=1.25)", publish)
        self.assertNotIn("_clear_music_channel_history", publish)
        self.assertNotIn(".purge(", publish)
        self.assertIn("await self._append_played_track_history(channel, previous_current)", publish)


if __name__ == "__main__":
    unittest.main()
