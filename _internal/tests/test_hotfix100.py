# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import unittest
from pathlib import Path

from jarvis.runtime.router import HybridRouter
from jarvis.integrations.music_provider import MUSIC_PROVIDERS

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


from tests.release_contracts import assert_release, assert_youtube_menu

class Hotfix100Regression(unittest.TestCase):
    def test_markers_and_schema(self):
        assert_release(self, (INTERNAL / "VERSION.txt").read_text(encoding="utf-8"))
        cfg = json.loads((INTERNAL / "config/default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(cfg["settings_version"], 163)
        self.assertNotIn("apple_music", cfg["integrations"])
        self.assertEqual(tuple(MUSIC_PROVIDERS), ("yandex_music", "spotify"))

    def test_telegram_home_layout(self):
        src = (J / "integrations" / "telegram_remote.py").read_text(encoding="utf-8")
        home = src[src.index("def _home_keyboard"):src.index("def _panel", src.index("def _home_keyboard"))]
        self.assertIn('[("🎵 Музыка", "ui:mm"), ("🎧 Discord", "ui:md")]', home)
        self.assertIn('[("🌐 Браузер", "ui:br"), ("▶️ YouTube", "ui:yt"), ("🔎 Поиск информации", "i:web:q")]', home)
        self.assertIn('[("🎙 Ввод", "ui:mic"), ("🖱 Мышь", "ui:mouse"), ("🎧 Вывод", "ui:vol")]', home)
        self.assertNotIn('"ui:mv"', home)

    def test_youtube_telegram_buttons_background_only(self):
        # Transport remains background-only; explicit layout controls are restored.
        assert_youtube_menu(self, (J / "integrations/telegram_remote.py").read_text(encoding="utf-8"))

    def test_search_title_and_author_are_separate_steps(self):
        src = (J / "integrations" / "telegram_remote.py").read_text(encoding="utf-8")
        self.assertIn('"youtube_search_title"', src)
        self.assertIn('"youtube_search_author"', src)
        api = (J / "integrations" / "youtube_api.py").read_text(encoding="utf-8")
        self.assertIn('def search_video(self, query: str, author: str = "")', api)
        self.assertIn('search_query = " ".join(part for part in (query, author)', api)
        self.assertIn('"author":author', src)

    def test_youtube_tool_surface(self):
        src = (J / "tools" / "domains" / "browser_media.py").read_text(encoding="utf-8")
        for action in ("status", "play_search", "play", "pause", "seek_forward", "seek_backward", "seek_to"):
            self.assertIn(f'"{action}"', src)
        enum = src.split('"action": _enum([',1)[1].split(']',1)[0]
        for removed in ("shorts_next", "shorts_previous", "fullscreen", "theater", "set_speed", "set_quality", "next", "previous"):
            self.assertNotIn(removed, enum)
        self.assertIn('author = str(args.get("author"', src)

    def test_deep_youtube_voice_routes(self):
        router = HybridRouter()
        cases = {
            "перемотай youtube на 1:30": ("seek_to", {"position_seconds": 90.0}),
            "перемотай youtube на 30 секунд вперед": ("seek_forward", {"seconds": 30.0}),
            "поставь видео youtube на паузу": ("pause", {}),
        }
        for text, (action, expected) in cases.items():
            with self.subTest(text=text):
                route = router.route(text)
                self.assertIsNotNone(route)
                self.assertEqual(route.tool, "media")
                self.assertEqual(route.arguments.get("action"), action)
                for key, value in expected.items():
                    self.assertEqual(route.arguments.get(key), value)

    def test_youtube_public_control_is_background_only(self):
        src = (J / "browser_control.py").read_text(encoding="utf-8")
        control = src[src.index("def control_service_media"):src.index("def selected_browser_tab")]
        # NEO 187 HREQ-026 (later user decision): next/previous VIDEO return
        # through the same verified background session; still no keys/mouse.
        self.assertIn('youtube_background = {"play", "pause", "play_pause", "seek_forward", "seek_backward", "seek_to", "status", "next", "previous"}', control)
        self.assertIn('youtube_removed = {"shorts_next", "shorts_previous", "set_speed", "set_quality"}', control)
        self.assertIn('youtube_layout = {"fullscreen", "theater"}', control)
        self.assertNotIn('_youtube_active_tab(', control)
        self.assertNotIn('_youtube_send_key(', control)
        self.assertNotIn('pyautogui', control)

    def test_no_user_facing_apple_music(self):
        paths = [
            J / "ui" / "settings_dialog.py", J / "agent.py", J / "runtime" / "media_phrases.py",
            J / "routing" / "ownership_firewall.py", INTERNAL / "config" / "default_settings.json",
        ]
        combined = "\n".join(p.read_text(encoding="utf-8") for p in paths)
        self.assertNotIn("Apple Music", combined)
        self.assertNotIn("media_apple", combined)


if __name__ == "__main__":
    unittest.main()
