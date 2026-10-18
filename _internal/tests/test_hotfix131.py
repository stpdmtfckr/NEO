from pathlib import Path
import unittest

from tests.release_contracts import assert_release

ROOT = Path(__file__).resolve().parents[1]

class Hotfix131StableDiscordDashboardContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot=(ROOT/'jarvis/integrations/background/discord_bot.py').read_text(encoding='utf-8')
        cls.main=(ROOT/'jarvis/ui/main_window.py').read_text(encoding='utf-8')
        cls.version=(ROOT/'VERSION.txt').read_text(encoding='utf-8')

    def test_dashboard_never_recreates_on_edit_timeout(self):
        self.assertIn('await asyncio.wait({edit_task}, timeout=1.25)', self.bot)
        self.assertIn('keeping canonical message', self.bot)
        self.assertIn('Creation is allowed only when no canonical dashboard exists', self.bot)

    def test_dashboard_has_current_and_next_five_not_history(self):
        self.assertIn('📋 **Следующие 5**', self.bot)
        self.assertIn('enumerate(queue[:5], 1)', self.bot)
        start=self.bot.index('def _music_dashboard_text')
        end=self.bot.index('async def _publish_music_state', start)
        body=self.bot[start:end]
        self.assertNotIn('Недавно играло', body)
        self.assertNotIn('Предыдущие', body)

    def test_history_is_permanent_channel_messages(self):
        self.assertIn('await self._append_played_track_history(channel, previous_current)', self.bot)
        self.assertIn('🕘 **Проиграно:**', self.bot)
        self.assertIn('preserves ordinary played-track messages forever', self.bot)

    def test_hide_show_keeps_same_hwnd(self):
        start=self.main.index('def _set_runtime_topmost')
        end=self.main.index('def _try_local_voice_control', start)
        body=self.main[start:end]
        self.assertIn('self.hide()', body)
        self.assertNotIn('self.close()', body)
        self.assertNotIn('self.setWindowFlags(', body)

    def test_release_marker(self):
        # Old hotfix markers were folded into NEO releases; the release contract is shared (see release_contracts).
        assert_release(self, self.version)

if __name__ == '__main__': unittest.main()
