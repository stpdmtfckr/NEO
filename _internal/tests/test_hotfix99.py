# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import unittest
from pathlib import Path

from jarvis.integrations.music_provider import active_music_provider
from jarvis.integrations.spotify import authorize_spotify_pkce
from jarvis.integrations.youtube_api import YouTubeAPIAdapter
from jarvis.quick_media_commands import parse_quick_media_command
from jarvis.routing.ownership_firewall import classify_turn_ownership
from jarvis.runtime.router import HybridRouter
from jarvis.runtime.semantics import explicit_yandex_music

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


class DummyConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})
    def get(self, key, default=None):
        return self.values.get(key, default)


class Hotfix99Regression(unittest.TestCase):
    """HOTFIX99 baseline after HOTFIX100 retired Apple Music.

    The durable contracts from HOTFIX99 are Spotify + YouTube + a single local
    music provider. HOTFIX100 intentionally removes the Apple provider entirely.
    """

    def test_settings_v148_and_media_integrations(self):
        data = json.loads((INTERNAL / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(int(data.get("settings_version", 0)), 163)
        self.assertEqual(data["integrations"]["music"]["active_provider"], "")
        self.assertIn("spotify", data["integrations"])
        self.assertIn("youtube", data["integrations"])
        self.assertNotIn("apple_music", data["integrations"])
        version = (INTERNAL / "VERSION.txt").read_text(encoding="utf-8")
        from tests.release_contracts import assert_release
        assert_release(self, version)

    def test_active_music_provider_exactly_one(self):
        base = {
            "integrations.music.active_provider": "spotify",
            "integrations.yandex_music.enabled": False,
            "integrations.spotify.enabled": True,
        }
        self.assertEqual(active_music_provider(DummyConfig(base)), "spotify")
        base["integrations.spotify.enabled"] = False
        self.assertEqual(active_music_provider(DummyConfig(base)), "")

    def test_settings_ui_radio_group_and_collapsible(self):
        src = (J / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        for token in ("Активировать Яндекс Музыку", "Активировать Spotify", "Активировать YouTube"):
            self.assertIn(token, src)
        self.assertNotIn("Активировать Apple Music", src)
        self.assertIn("def _music_provider_toggled", src)
        self.assertIn("toggle.setChecked(False)", src)
        self.assertGreaterEqual(src.count("_bind_collapsible_group"), 3)

    def test_router_explicit_providers_and_youtube(self):
        router = HybridRouter()
        cases = {
            "включи Numb в Spotify": ("spotify", "play_track", "numb"),
            "включи Numb в Яндекс Музыке": ("yandex_music", "play_track", "numb"),
            "включи на YouTube обзор RTX 5060 Ti": ("media", "play_search", "обзор rtx 5060 ti"),
            "найди песню Numb": ("music", "search_track", "numb"),
            "найди песню Numb и включи": ("music", "play_track", "numb"),
            "включи плейлист тренировка": ("music", "playlist", "тренировка"),
            "включи плейлист тренировка в Spotify": ("spotify", "playlist", "тренировка"),
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                route = router.route(phrase)
                self.assertIsNotNone(route)
                self.assertEqual((route.tool, route.arguments.get("action"), route.arguments.get("query", "")), expected)

    def test_ambiguous_hand_edited_music_profile_fails_closed(self):
        cfg = DummyConfig({
            "integrations.music.active_provider": "",
            "integrations.yandex_music.enabled": True,
            "integrations.spotify.enabled": True,
        })
        self.assertEqual(active_music_provider(cfg), "")

    def test_explicit_firewall_owns_named_service(self):
        checks = {
            "включи музыку в Spotify": ("media_spotify", "spotify"),
            "включи музыку в Яндекс Музыке": ("media_yandex", "yandex_music"),
            "включи видео на YouTube": ("media_video", "media"),
            "включи мою волну в Discord": ("media_discord", "discord"),
        }
        for text, (owner, tool) in checks.items():
            claim = classify_turn_ownership(text)
            self.assertEqual(claim.owner, owner)
            self.assertTrue(claim.allows(tool))

    def test_playlist_no_longer_claimed_by_yandex_semantics(self):
        self.assertFalse(explicit_yandex_music("включи плейлист тренировка"))
        self.assertFalse(explicit_yandex_music("включи плейлист тренировка в Spotify"))
        self.assertTrue(explicit_yandex_music("включи плейлист тренировка в Яндекс Музыке"))
        self.assertTrue(explicit_yandex_music("включи мою волну"))

    def test_quick_media_targets_named_provider(self):
        self.assertEqual(parse_quick_media_command("пауза Spotify").target, "spotify")
        self.assertEqual(parse_quick_media_command("пауза Яндекс Музыка").target, "yandex_music")

    def test_youtube_direct_url_does_not_need_api_key(self):
        adapter = YouTubeAPIAdapter(DummyConfig({"integrations.youtube.api_key_encrypted": ""}))
        result = adapter.search_video("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(result["video_id"], "dQw4w9WgXcQ")
        self.assertTrue(result["verified"])

    def test_spotify_desktop_oauth_rejects_unsafe_redirect(self):
        with self.assertRaises(ValueError):
            authorize_spotify_pkce("client", "http://localhost:8765/callback", timeout_seconds=0.01)
        with self.assertRaises(ValueError):
            authorize_spotify_pkce("client", "https://example.com/callback", timeout_seconds=0.01)

    def test_apple_runtime_is_retired(self):
        self.assertFalse((J / "integrations" / "apple_music.py").exists())
        req = (INTERNAL / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("PyJWT[crypto]", req)
        self.assertNotIn("apple_music", json.loads((INTERNAL / "config" / "default_settings.json").read_text(encoding="utf-8"))["integrations"])

    def test_agent_has_generic_active_provider(self):
        src = (J / "agent.py").read_text(encoding="utf-8")
        self.assertIn('execution = self.tools.execute("music", arguments)', src)
        self.assertIn('music_provider = active_music_provider(cfg)', src)
        self.assertIn('return ["spotify"]', src)
        self.assertIn('return ["music"]', src)
        self.assertNotIn('return ["apple_music"]', src)

    def test_bare_youtube_url_routes_to_video_not_yandex_audio(self):
        src = (J / "agent.py").read_text(encoding="utf-8")
        start = src.index("youtube_url = extract_youtube_url")
        block = src[start:src.index("developer_bus().emit", start)]
        self.assertIn('self.tools.execute("media", {"action": "play_search"', block)
        self.assertNotIn('"action": "youtube_url"', block)


if __name__ == "__main__":
    unittest.main()
