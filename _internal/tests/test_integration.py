# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Integration checks for the lightweight Game Web Knowledge runtime."""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestWakeAliasesPreserved(unittest.TestCase):
    """The user's wake corpus is unrelated to the game rewrite and must survive."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = json.loads(
            (ROOT / "config" / "default_settings.json").read_text(encoding="utf-8")
        )

    def test_alias_list_is_intact(self):
        # NEO 178: the wake word is NEO; every spelling ships bare and with . ! ?
        from jarvis.runtime import wake178
        current = self.settings["wake_word"]["aliases"]
        self.assertEqual(self.settings["wake_word"]["phrase"], "NEO")
        self.assertEqual(current, wake178.aliases())
        for base in wake178.BASES:
            for suffix in ("", ".", "!", "?"):
                self.assertIn(base + suffix, current)

    def test_language_aliases_intact(self):
        self.assertTrue(self.settings["wake_word"].get("language_aliases"))

    KNOWN_V4_EXCLUSIONS = frozenset({"ciao to jest", "ciao w jest"})

    def test_no_alias_regressed(self):
        from jarvis.wake_matcher import match_wake_phrase

        aliases = self.settings["wake_word"]["aliases"]
        phrase = self.settings["wake_word"]["phrase"]
        failed = []
        for alias in aliases:
            if alias.strip().strip(".!?").casefold() in self.KNOWN_V4_EXCLUSIONS:
                continue
            if not match_wake_phrase(alias, phrase, aliases=aliases)[0]:
                failed.append(alias)
        self.assertEqual(failed, [], f"aliases that stopped matching: {failed[:20]}")

    def test_ordinary_speech_still_rejected(self):
        from jarvis.wake_matcher import match_wake_phrase

        aliases = self.settings["wake_word"]["aliases"]
        for text in (
            "включи следующий трек",
            "какая погода",
            "поставь музыку погромче",
            "Je vous dis à tous les jours.",
            "сделай скриншот",
        ):
            self.assertFalse(match_wake_phrase(text, "джарвис", aliases=aliases)[0])


class TestConfigMigration(unittest.TestCase):
    def test_searchapi_defaults(self):
        settings = json.loads(
            (ROOT / "config" / "default_settings.json").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(settings["settings_version"], 136)
        web = settings["gaming"]["web_knowledge"]
        self.assertTrue(web["enabled"])
        self.assertIn("searchapi_api_key_encrypted", web)
        self.assertEqual(web["searchapi_gl"], "us")
        self.assertGreaterEqual(int(web["searchapi_max_sources"]), 1)
        self.assertGreaterEqual(float(web["searchapi_timeout_seconds"]), 6.0)
        self.assertNotIn("searchapi_ai_overview_fallback", web)
        self.assertFalse(bool(web["show_sources"]))
        self.assertNotIn("tavily_api_key_encrypted", web)
        self.assertNotIn("ai_mode_backend", web)
        self.assertNotIn("serpapi_api_key_encrypted", web)
        self.assertEqual(settings["context_memory"]["gaming_poll_seconds"], 4.0)
        self.assertGreater(web["cache_max_mb"], 0)

    def test_config_module_declares_v136_migration(self):
        source = (ROOT / "jarvis" / "config.py").read_text(encoding="utf-8")
        self.assertIn("if version < 136:", source)
        self.assertIn('"searchapi_gl": "us"', source)
        self.assertIn('searchapi_defaults = {', source)
        versions = [
            int(x)
            for x in re.findall(r'self\.data\["settings_version"\] = (\d+)', source)
        ]
        self.assertTrue(versions and max(versions) >= 136)


class TestGameRuntimeSurface(unittest.TestCase):
    def test_process_only_gaming_runtime_has_a_small_explicit_surface(self):
        gaming = ROOT / "jarvis" / "gaming"
        files = {p.name for p in gaming.glob("*.py")}
        self.assertEqual(files, {
            "__init__.py", "history.py", "manager.py", "memory.py", "normalize.py",
            "presentation.py", "routing_firewall.py", "runtime.py", "speech_hints.py", "telemetry.py",
            "match_ready.py",
        })

    def test_game_web_runtime_has_only_current_searchapi_modules(self):
        game_web = ROOT / "jarvis" / "game_web"
        files = {p.name for p in game_web.glob("*.py")}
        self.assertEqual(files, {"__init__.py", "cache.py", "context.py", "models.py", "service.py", "searchapi.py"})

    def test_installation_surface_contains_no_game_build_tools(self):
        tools = ROOT / "tools"
        files = {p.name for p in tools.glob("*.py")} if tools.exists() else set()
        # The core diagnostic tools ship; later builds add their own (typo/spelling
        # builders, benchmarks, plugin check), so the list is a floor, not an exact set.
        self.assertLessEqual({"calibrate_wake.py", "verify_speech.py", "benchmark_routing.py",
                              "audit_ui_language.py", "inventory_settings_language.py", "render_qt_language.py",
                              "benchmark_windows.py", "atlas_all_test.py", "startup_test.py", "test_all.py"}, files)
        # What this test guards: nothing that builds, patches or injects into games.
        import re
        forbidden = re.compile(r"game|mod_?build|patch_?game|inject|cheat|trainer|memory_?edit|hook", re.I)
        self.assertEqual(sorted(f for f in files if forbidden.search(f)), [])


class TestGameWebWiring(unittest.TestCase):
    def test_agent_uses_game_web_before_generic_chat(self):
        source = (ROOT / "jarvis" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("from jarvis.game_web import GameWebKnowledgeService", source)
        self.assertIn("self.game_web = GameWebKnowledgeService", source)
        self.assertIn("self.game_web.owns(user_text)", source)
        self.assertIn("self.game_web.try_answer(user_text)", source)
        self.assertIn("Game web knowledge failed after ownership", source)

    def test_main_window_has_no_deferred_pack_replay(self):
        source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn('startswith("game_web.")', source)

    def test_settings_exposes_searchapi_and_cache(self):
        source = (ROOT / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertIn("SearchAPI.io API key", source)
        self.assertIn("searchapi_api_key_encrypted", source)
        self.assertIn("game_web_show_sources", source)
        self.assertIn("API-сервисы", source)
        self.assertNotIn("game_web_ai_overview", source)
        self.assertIn("Проверить ключ", source)
        self.assertIn("cache_max_mb", source)
        self.assertNotIn("Открыть профиль Google AI Mode", source)

    def test_runtime_imports_cleanly(self):
        from jarvis.game_web import GameWebKnowledgeService  # noqa: F401
        from jarvis.gaming.manager import GameManager

        manager = GameManager(enabled=True)
        ids = {adapter.game_id for adapter in manager.adapters}
        self.assertIn("darktide", ids)
        self.assertIn("warframe", ids)
        self.assertTrue(all("game_data" not in adapter.capabilities for adapter in manager.adapters))


if __name__ == "__main__":
    unittest.main(verbosity=2)
