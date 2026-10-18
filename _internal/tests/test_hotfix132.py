import unittest

from tests.release_contracts import assert_release
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class Hotfix132Contracts(unittest.TestCase):
    def test_version_marker(self):
        # Old hotfix markers were folded into NEO releases; the release contract is shared (see release_contracts).
        assert_release(self, (ROOT / 'VERSION.txt').read_text(encoding='utf-8'))

    def test_yandex_wave_has_personalized_continuity_fallback(self):
        text = (ROOT / 'jarvis/integrations/yandex_music.py').read_text(encoding='utf-8')
        self.assertIn('continuity_fallback_count', text)
        self.assertIn('continuity_v132', text)
        self.assertIn('floor_ids = set(floor_ids) | avoid', text)
        self.assertIn('self._wave_repeat_floor()', text)
        self.assertIn('user:onyourwave', text)

    def test_discord_wave_queue_is_hard_bounded(self):
        from tests.release_contracts import run_discord_wave_refill
        text = (ROOT / 'jarvis/integrations/discord_navigation.py').read_text(encoding='utf-8')
        # Initial pool and trimming paths keep the rolling window at 12.
        self.assertIn('if mode == "wave":\n            pool = pool[:12]', text)
        self.assertGreaterEqual(text.count('del state["items"][12:]'), 2)
        # The background refill never grows the queue past 12, however long Rotor's answer is.
        state, _calls = run_discord_wave_refill(queued=2, rotor_tracks=40)
        self.assertEqual(len(state["items"]), 12)

    def test_discord_wave_still_maintains_visible_depth(self):
        from tests.release_contracts import run_discord_wave_refill
        text = (ROOT / 'jarvis/integrations/discord_navigation.py').read_text(encoding='utf-8')
        self.assertIn('_ensure_wave_queue_depth_background(profile, 8)', text)
        state, calls = run_discord_wave_refill(queued=2, rotor_tracks=6)
        self.assertGreaterEqual(len(state["items"]), 8)
        ids = [x["track_id"] for x in state["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("now", ids)
        self.assertNotIn("recent", ids)
        self.assertEqual(calls[0]["queue_track_id"], "done")

    def test_discord_my_wave_remains_explicit_deterministic_phrase(self):
        text = (ROOT / 'jarvis/runtime/media_phrases.py').read_text(encoding='utf-8')
        self.assertIn('discord_music_my_wave', text)
        self.assertIn('music_my_wave', text)

if __name__ == '__main__':
    unittest.main()
