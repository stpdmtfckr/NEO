from pathlib import Path
import unittest

from tests.release_contracts import assert_release

ROOT = Path(__file__).resolve().parents[1]


class Hotfix130DiscordHistoryWindowVisibilityContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = (ROOT / "jarvis/integrations/background/discord_bot.py").read_text(encoding="utf-8")
        cls.main = (ROOT / "jarvis/ui/main_window.py").read_text(encoding="utf-8")
        cls.robot = (ROOT / "jarvis/ui/robot_widget.py").read_text(encoding="utf-8")
        cls.tools = (ROOT / "jarvis/tools/domains/browser_media.py").read_text(encoding="utf-8")
        cls.version = (ROOT / "VERSION.txt").read_text(encoding="utf-8")

    def test_discord_dashboard_is_current_plus_five_only(self):
        self.assertIn('enumerate(queue[:5], 1)', self.bot)
        self.assertIn('📋 **Следующие 5**', self.bot)
        self.assertNotIn('🕘 **Предыдущие**', self.bot)
        # Timers now have a separate live panel and are not mixed into music controls.
        dashboard_start = self.bot.index('def _music_dashboard_text')
        dashboard_end = self.bot.index('async def _publish_music_state', dashboard_start)
        dashboard = self.bot[dashboard_start:dashboard_end]
        self.assertNotIn('Активные таймеры', dashboard)

    def test_played_tracks_are_persistent_messages(self):
        self.assertIn('await self._append_played_track_history(channel, previous_current)', self.bot)
        self.assertIn('🕘 **Проиграно:**', self.bot)
        self.assertIn('Discord music history preserved', self.bot)
        cleanup_start = self.bot.index('async def _cleanup_music_channel_clutter')
        cleanup_end = self.bot.index('@staticmethod\n    def _music_entry_key', cleanup_start)
        cleanup = self.bot[cleanup_start:cleanup_end]
        self.assertNotIn('is_transition', cleanup)
        self.assertNotIn('сейчас играет:', cleanup.casefold())
        self.assertNotIn('reanchor=True', self.bot)

    def test_timer_panel_is_separate(self):
        self.assertIn('async def _find_timer_dashboard', self.bot)
        self.assertIn('⏱ **Активные таймеры NEO**', self.bot)
        self.assertIn('self._timer_dashboard_messages', self.bot)

    def test_hide_show_never_recreates_native_window(self):
        from tests.release_contracts import assert_existing_window_visibility
        assert_existing_window_visibility(self, self.main)

    def test_theme_does_not_rebuild_hwnd(self):
        self.assertIn('never reset top-level WindowFlags', self.main)
        # One setWindowFlags remains in constructor only; theme/runtime paths must not add more.
        self.assertEqual(self.main.count('self.setWindowFlags('), 1)

    def test_notes_are_faster_and_denser(self):
        self.assertIn('eight independent lanes', self.robot)
        self.assertIn('self._elapsed / 1.34', self.robot)
        self.assertIn('zip(lane_xs, phase_offsets)', self.robot)

    def test_visible_music_queue_is_five_in_dialog(self):
        self.assertIn('def _now_playing_queue_preview(self, source: str, *, limit: int = 5)', self.main)
        self.assertIn('for idx, row in enumerate(rows[:5], 1):', self.tools)
        self.assertNotIn('discord_history_suffix', self.tools)

    def test_release_marker(self):
        # Old hotfix markers were folded into NEO releases; the release contract is shared (see release_contracts).
        assert_release(self, self.version)


if __name__ == '__main__':
    unittest.main()
