from pathlib import Path
import unittest

from tests.release_contracts import assert_release

ROOT = Path(__file__).resolve().parents[1]

class Hotfix129MusicQueueDiscordVisualContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.robot = (ROOT / "jarvis/ui/robot_widget.py").read_text(encoding="utf-8")
        cls.discord = (ROOT / "jarvis/integrations/discord_navigation.py").read_text(encoding="utf-8")
        cls.bot = (ROOT / "jarvis/integrations/background/discord_bot.py").read_text(encoding="utf-8")
        cls.main = (ROOT / "jarvis/ui/main_window.py").read_text(encoding="utf-8")
        cls.yandex = (ROOT / "jarvis/integrations/yandex_music.py").read_text(encoding="utf-8")
        cls.tools = (ROOT / "jarvis/tools/domains/browser_media.py").read_text(encoding="utf-8")

    def test_music_notes_are_dense_and_fast(self):
        self.assertIn("eight independent lanes", self.robot)
        self.assertIn("self._elapsed / 1.34", self.robot)
        self.assertIn("zip(lane_xs, phase_offsets)", self.robot)
        self.assertIn("x = lane_x", self.robot)
        self.assertNotIn("self._elapsed / 3.85", self.robot)

    def test_track_switch_never_reanchors_discord_dashboard(self):
        self.assertIn("profile, latest, reanchor=False", self.discord)
        self.assertNotIn("profile, latest, reanchor=True", self.discord)
        self.assertIn("message.edit(content=text, view=view)", self.bot)

    def test_discord_shared_snapshot_carries_five_track_history(self):
        self.assertIn('"history": history_items', self.discord)
        self.assertIn('list(snapshot.get("history") or [])[-5:]', self.discord)
        self.assertIn('"history": history_items', self.discord)
        self.assertIn('await self._append_played_track_history(channel, previous_current)', self.bot)

    def test_discord_wave_keeps_eight_track_cushion(self):
        # The synchronous 5-attempt fill of HOTFIX129 became the background Rotor
        # refill; the cushion itself is checked on the real code path.
        from tests.release_contracts import run_discord_wave_refill
        self.assertIn("_ensure_wave_queue_depth_background(profile, 8)", self.discord)
        state, calls = run_discord_wave_refill(queued=0, rotor_tracks=10)
        self.assertGreaterEqual(len(state["items"]), 8)
        self.assertIn("recent", calls[0]["avoid_ids"])   # recent history is avoided, not ignored

    def test_quick_next_previous_include_local_queue_in_chat(self):
        # Queue rendering remains required; language-specific hardcoded source
        # prose is replaced by a real result test with unchanged track payloads.
        from types import SimpleNamespace
        from jarvis.quick_media_commands import QuickMediaCommand
        from jarvis.ui.main_window import MainWindow
        from jarvis.ui.quick_media_messages import message
        for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
            for action in ('next', 'previous'):
                with self.subTest(language=code, action=action):
                    calls = []
                    def execute(tool, args):
                        calls.append(args['action'])
                        return SimpleNamespace(success=True, text='', data={
                            'title': 'Current Title', 'artist': 'Artist', 'content_provider': 'youtube',
                            'queue': [{'title': f'Queue Title {n}', 'artist': 'Original Artist'} for n in range(1, 7)]})
                    window = SimpleNamespace(_language_code=lambda: code, _quick_media_server=lambda: '',
                        agent=SimpleNamespace(tools=SimpleNamespace(execute=execute)),
                        config=SimpleNamespace(get=lambda key, default=None: default))
                    result = MainWindow._execute_quick_media_command(window, QuickMediaCommand(action, 'yandex_music', action))
                    self.assertTrue(result['ok'])
                    self.assertIn('Current Title', result['text'])
                    self.assertIn(message('queue', code), result['text'])
                    for n in range(1, 6):
                        self.assertIn(f'{n}. Original Artist — Queue Title {n}', result['text'])
                    self.assertNotIn('Queue Title 6', result['text'])
                    self.assertIn(action, calls)
                    self.assertNotIn('background_status', calls)
        self.assertIn('"queue": list(snapshot.get("queue") or [])[:8]', self.discord)
        # The preview now runs as an after-playback effect (outside the playback lock), same 8/5 limits.
        self.assertIn('"queue_preview", self._queue_preview_state, queue_limit=8, history_limit=5', self.yandex)

    def test_normal_discord_music_responses_show_queue(self):
        self.assertIn('"music_bot_play": (f"Включил в Discord: {now}."', self.tools)
        self.assertIn("+ discord_queue_suffix", self.tools)
        self.assertNotIn("discord_history_suffix", self.tools)

    def test_release_marker(self):
        # Old hotfix markers were folded into NEO releases; the release contract is shared (see release_contracts).
        assert_release(self, (ROOT / "VERSION.txt").read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
