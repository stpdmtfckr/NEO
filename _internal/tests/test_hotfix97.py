# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import unittest
from pathlib import Path

from jarvis.routing.ownership_firewall import classify_turn_ownership
from jarvis.runtime.router import HybridRouter

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / 'jarvis'


class Hotfix97Regression(unittest.TestCase):
    def test_discord_destination_owns_my_wave(self):
        claim = classify_turn_ownership('включи моя волна в дискорд')
        self.assertEqual(claim.owner, 'media_discord')
        self.assertTrue(claim.allows('discord'))
        self.assertFalse(claim.allows('yandex_music'))

    def test_router_keeps_my_wave_in_discord(self):
        route = HybridRouter().route('включи моя волна в дискорд')
        self.assertIsNotNone(route)
        self.assertEqual(route.tool, 'discord')
        self.assertEqual(route.arguments.get('action'), 'music_my_wave')

    def test_generic_pause_is_multi_music_fanout(self):
        src = (J / "ui/main_window.py").read_text(encoding="utf-8")
        self.assertIn("fanout_music_state", src)
        start = src.index("cached_success = False")
        block = src[start:src.index("# No music owner handled the command", start)]
        self.assertIn("statuses = recent_music_states(max_age_seconds=30.0)", block)
        self.assertIn("owner not in attempted and active_for_transport(owner)", block)
        self.assertLess(block.index("run_owner(cached_owner)"), block.index("recent_music_states("))
        self.assertNotIn('if cached_owner and run_owner(cached_owner):\n                    pass', block)

    def test_discord_dashboard_is_stable_in_place(self):
        src = (J / 'integrations' / 'background' / 'discord_bot.py').read_text(encoding='utf-8')
        self.assertIn('_music_dashboard_signatures', src)
        self.assertIn('signature_changed', src)
        self.assertTrue(
            'await message.edit(content=text, view=view)' in src
            or 'asyncio.create_task(message.edit(content=text, view=view))' in src
        )
        self.assertNotIn('reanchor=True', src)
        self.assertNotIn('_music_reanchor_tasks', src)
        self.assertIn('async def _publish_music_state(self, profile: dict[str, Any], state: dict[str, Any], *, reanchor: bool = False)', src)

    def test_panel_callback_recovers_configured_token_profile(self):
        src = (J / 'integrations' / 'discord_navigation.py').read_text(encoding='utf-8')
        start = src.index('def _handle_music_panel_action')
        block = src[start:src.index('def _bus_bypass', start)]
        self.assertIn('configured_profile = self._background_profile(server)', block)
        self.assertIn('profile = configured_profile', block)

    def test_hotfix97_version_marker(self):
        from tests.release_contracts import assert_release
        assert_release(self, (INTERNAL / "VERSION.txt").read_text(encoding="utf-8"))


if __name__ == '__main__':
    unittest.main()
