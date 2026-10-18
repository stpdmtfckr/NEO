# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from jarvis.config import ConfigManager
from jarvis.game_web.cache import CACHE_PROVENANCE, CACHE_SCHEMA, GameWebCache
from jarvis.game_web.context import GameWebContextStore
from jarvis.game_web.models import SearchResponse, SearchResult
from jarvis.game_web.service import GameWebKnowledgeService
from jarvis.game_web.searchapi import SEARCHAPI_ACCOUNT_URL, SEARCHAPI_SEARCH_URL, SearchAPIGoogleProvider
from jarvis.gaming.manager import GameAdapter, GameManager, GamePresenceService, GameProcess
from jarvis.gaming.memory import GamingContextTracker
from jarvis.secrets import protect_secret


class FakeConfig:
    def __init__(self, root: Path):
        self.paths = SimpleNamespace(root=root / "data", cache=root / "cache")
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.cache.mkdir(parents=True, exist_ok=True)
        self.values = {
            "app.language": "ru",
            "gaming.enabled": True,
            "gaming.web_knowledge.enabled": True,
            "gaming.web_knowledge.searchapi_api_key_encrypted": "",
            "gaming.web_knowledge.searchapi_gl": "us",
            "gaming.web_knowledge.searchapi_max_sources": 8,
            "gaming.web_knowledge.searchapi_timeout_seconds": 8.0,
            "gaming.web_knowledge.show_sources": False,
            "gaming.web_knowledge.cache_ttl_hours": 72,
            "gaming.web_knowledge.dynamic_cache_ttl_minutes": 30,
            "gaming.web_knowledge.stale_cache_days": 14,
            "gaming.web_knowledge.cache_max_mb": 20,
            "gaming.web_knowledge.similar_cache_enabled": True,
            "gaming.web_knowledge.followup_context_seconds": 600,
            "gaming.web_knowledge.suggest_followup": False,
            "context_memory.gaming_poll_seconds": 4.0,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeOllama:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return {"message": {"content": "Какие способности есть у Скитария?"}, "done_reason": "stop"}


class FakeSearch:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append((query, dict(kwargs)))
        if not self.responses:
            raise AssertionError("unexpected network call")
        return self.responses.pop(0)

    def close(self):
        pass


class FakeHTTPResponse:
    def __init__(self, status_code=200, payload=None, raw=b"{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = raw

    def json(self):
        return self._payload


class FakeHTTPSession:
    def __init__(self, responses=None, exc=None):
        self.headers = {}
        self.responses = list(responses or [])
        self.exc = exc
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class SearchAPIProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = FakeConfig(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _set_key(self, key="searchapi-test-key"):
        self.cfg.values["gaming.web_knowledge.searchapi_api_key_encrypted"] = protect_secret(key)

    def test_missing_key_fails_closed_without_http(self):
        session = FakeHTTPSession()
        result = SearchAPIGoogleProvider(self.cfg, session).search("Darktide Psyker weapons")
        self.assertEqual(result.error, "searchapi_key_missing")
        self.assertEqual(session.calls, [])

    def test_standard_environment_key_is_supported_when_gui_empty(self):
        payload = {"answer_box": {"answer": "A"}}
        session = FakeHTTPSession([FakeHTTPResponse(200, payload)])
        with patch.dict(os.environ, {"SEARCHAPI_API_KEY": "env-key"}, clear=False):
            result = SearchAPIGoogleProvider(self.cfg, session).search("Warframe Rhino")
        self.assertEqual(result.direct_answer, "A")
        self.assertEqual(session.calls[0][1]["headers"]["Authorization"], "Bearer env-key")

    def test_gui_dpapi_key_wins_over_stale_environment_override(self):
        self._set_key("gui-key")
        session = FakeHTTPSession([FakeHTTPResponse(200, {"answer_box": {"answer": "A"}})])
        with patch.dict(os.environ, {"SEARCHAPI_API_KEY": "stale-env"}, clear=False):
            SearchAPIGoogleProvider(self.cfg, session).search("q")
        self.assertEqual(session.calls[0][1]["headers"]["Authorization"], "Bearer gui-key")

    def test_google_ai_mode_request_is_small_get_and_key_not_in_url_params(self):
        self._set_key()
        session = FakeHTTPSession([FakeHTTPResponse(200, {"markdown": "Ответ"}, b"payload")])
        SearchAPIGoogleProvider(self.cfg, session).search("Darktide Psyker weapons", language="ru")
        url, kwargs = session.calls[0]
        self.assertEqual(url, SEARCHAPI_SEARCH_URL)
        self.assertEqual(kwargs["params"]["engine"], "google")
        self.assertEqual(kwargs["params"]["q"], "Darktide Psyker weapons")
        self.assertEqual(kwargs["params"]["hl"], "ru")
        self.assertEqual(kwargs["params"]["gl"], "us")
        self.assertNotIn("api_key", kwargs["params"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer searchapi-test-key")

    def test_parses_answer_box_and_organic_sources(self):
        self._set_key()
        payload = {
            "answer_box": {"answer": "Псайкер использует доступное ему оружие.\n\nЛучшие варианты"},
            "organic_results": [
                {"title": "Darktide Wiki", "link": "https://example.com/psyker", "snippet": "ref"},
                {"title": "Guide", "link": "https://example.org/guide", "snippet": "guide"},
            ],
            "search_metadata": {"id": "search_1", "total_time_taken": 3.2, "status": "Success"},
        }
        session = FakeHTTPSession([FakeHTTPResponse(200, payload, json.dumps(payload).encode())])
        result = SearchAPIGoogleProvider(self.cfg, session).search("Darktide Psyker weapons")
        self.assertEqual(result.provider, "searchapi_google")
        self.assertEqual(result.direct_answer_kind, "answer_box")
        self.assertIn("Псайкер", result.direct_answer)
        self.assertIn("Лучшие", result.direct_answer)
        self.assertEqual(len(result.results), 2)
        self.assertEqual(result.results[0].provider, "searchapi_google")
        self.assertEqual(result.metadata["search_id"], "search_1")
        self.assertFalse(result.error)

    def test_generated_text_blocks_are_not_used_for_factual_search(self):
        self._set_key()
        payload = {"text_blocks": [
            {"type": "paragraph", "answer": "Первый абзац."},
            {"type": "header", "answer": "Заголовок"},
            {"type": "unordered_list", "items": [
                {"type": "paragraph", "answer": "Пункт один"},
                {"type": "paragraph", "answer": "Пункт два"},
            ]},
        ]}
        result = SearchAPIGoogleProvider(self.cfg, FakeHTTPSession([FakeHTTPResponse(200, payload)])).search("q")
        self.assertFalse(result.direct_answer)
        self.assertEqual(result.error, "searchapi_no_results")

    def test_401_is_actionable_auth_error(self):
        self._set_key()
        result = SearchAPIGoogleProvider(self.cfg, FakeHTTPSession([FakeHTTPResponse(401, {"message": "Unauthorized"}, b"x")])).search("q")
        self.assertEqual(result.error, "searchapi_auth_error")
        self.assertEqual(result.attempts[0]["status"], 401)
        self.assertEqual(result.attempts[0].get("detail"), "Unauthorized")

    def test_402_is_billing_error(self):
        self._set_key()
        result = SearchAPIGoogleProvider(self.cfg, FakeHTTPSession([FakeHTTPResponse(402, {"message": "credits"}, b"x")])).search("q")
        self.assertEqual(result.error, "searchapi_billing_required")

    def test_429_is_rate_limit(self):
        self._set_key()
        result = SearchAPIGoogleProvider(self.cfg, FakeHTTPSession([FakeHTTPResponse(429, {"message": "limit"}, b"x")])).search("q")
        self.assertEqual(result.error, "searchapi_rate_limited")

    def test_timeout_is_fail_closed(self):
        self._set_key()
        result = SearchAPIGoogleProvider(self.cfg, FakeHTTPSession(exc=requests.Timeout())).search("q")
        self.assertEqual(result.error, "searchapi_timeout")
        self.assertFalse(result.direct_answer)

    def test_sources_without_google_ai_answer_are_not_treated_as_answer(self):
        self._set_key()
        payload = {"organic_results": [{"title": "x", "link": "https://x.example"}]}
        result = SearchAPIGoogleProvider(self.cfg, FakeHTTPSession([FakeHTTPResponse(200, payload)])).search("q")
        self.assertEqual(result.error, "searchapi_answer_missing")
        self.assertFalse(result.direct_answer)
        self.assertEqual(len(result.results), 1)

    def test_ai_mode_has_no_ai_overview_fallback(self):
        self._set_key()
        # A stale pre-V7.6 setting must not resurrect the retired overview path.
        self.cfg.values["gaming.web_knowledge.searchapi_ai_overview_fallback"] = True
        session = FakeHTTPSession([FakeHTTPResponse(200, {})])
        result = SearchAPIGoogleProvider(self.cfg, session).search("q")
        self.assertFalse(result.direct_answer)
        self.assertIn(result.error, {"searchapi_answer_missing", "searchapi_no_results"})
        self.assertEqual([c[1]["params"]["engine"] for c in session.calls], ["google"])

    def test_account_probe_uses_lightweight_me_endpoint(self):
        self._set_key()
        session = FakeHTTPSession([FakeHTTPResponse(200, {"ok": True})])
        data = SearchAPIGoogleProvider(self.cfg, session).account_status()
        self.assertTrue(data["ok"])
        self.assertEqual(session.calls[0][0], SEARCHAPI_ACCOUNT_URL)


class GameWebCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = FakeConfig(Path(self.tmp.name))
        self.cache = GameWebCache(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _put(self, q="какое оружие у псайкера", answer="A"):
        return self.cache.put(
            "darktide", "Darktide", q, search_query="Darktide " + q, answer=answer,
            sources=[], provider="searchapi_google",
        )

    def test_exact_answer_cache(self):
        self._put()
        row = self.cache.get("darktide", "какое оружие у псайкера")
        self.assertIsNotNone(row)
        self.assertEqual(row["schema"], CACHE_SCHEMA)
        self.assertEqual(row["provenance"], CACHE_PROVENANCE)
        self.assertEqual(row["answer"], "A")

    def test_similar_question_cache(self):
        self._put("какое оружие доступно псайкеру")
        row = self.cache.get("darktide", "какое оружие доступно псайкеру?")
        self.assertIsNotNone(row)

    def test_rejects_non_searchapi_provider(self):
        with self.assertRaises(ValueError):
            self.cache.put("x", "X", "q", search_query="q", answer="bad", sources=[], provider="google_ai_mode_browser")

    def test_old_google_cache_and_dedicated_profile_are_invalidated(self):
        old_dir = self.cache.base / "warframe"
        old_dir.mkdir(parents=True, exist_ok=True)
        old = old_dir / "bad.json"
        old.write_text(json.dumps({"schema": 2, "provenance": "google_ai_mode_v1", "provider": "google_ai_mode_browser", "answer": "bad"}), encoding="utf-8")
        profile = self.cache.base / "google_ai_profile"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "Cookies").write_text("retired", encoding="utf-8")
        fresh = GameWebCache(self.cfg)
        self.assertFalse(old.exists())
        self.assertFalse(profile.exists())
        self.assertEqual(fresh.stats()["entries"], 0)

    def test_old_schema_inside_answers_is_invalidated(self):
        game_dir = self.cache.root / "darktide"
        game_dir.mkdir(parents=True, exist_ok=True)
        p = game_dir / "legacy.json"
        p.write_text(json.dumps({"schema": 2, "provenance": "google_ai_mode_v1", "provider": "serpapi_google_ai_mode", "answer": "old"}), encoding="utf-8")
        fresh = GameWebCache(self.cfg)
        self.assertFalse(p.exists())
        self.assertEqual(fresh.stats()["entries"], 0)

    def test_dynamic_query_has_shorter_ttl(self):
        self.assertLess(
            self.cache.ttl_seconds("какое сейчас оружие после последнего патча"),
            self.cache.ttl_seconds("какое оружие доступно псайкеру"),
        )


class GameWebServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = FakeConfig(Path(self.tmp.name))
        self.ollama = FakeOllama()
        self.service = GameWebKnowledgeService(self.cfg, ollama=self.ollama)
        self.game = {
            "game_id": "warframe", "game_name": "Warframe", "pid": 123,
            "process_name": "Warframe.x64.exe", "session_started_at": time.time() - 10,
        }
        self.service._runtime_game = lambda: dict(self.game)

    def tearDown(self):
        self.service.close(); self.tmp.cleanup()

    @staticmethod
    def google_ai(answer="Google AI exact answer", related=None):
        return SearchResponse(
            query="q", provider="searchapi_google", direct_answer=answer,
            direct_answer_kind="answer_box",
            results=[SearchResult("Wiki", "https://wiki.warframe.com/w/Rhino", provider="searchapi_google", rank=1, score=0.97)],
            related_questions=list(related or []),
        )

    def test_active_game_question_prepends_game_name(self):
        self.service.searcher = FakeSearch([self.google_ai(related=["Где найти Шакала?"])])
        owned, answer, meta = self.service.try_answer("как выбить Рино")
        self.assertTrue(owned)
        self.assertTrue(self.service.searcher.calls[0][0].startswith("Warframe "))
        self.assertEqual(meta["game_id"], "warframe")
        self.assertIn("Google AI exact answer", answer)
        self.assertEqual(meta["intent"], "google_ai_search")

    def test_failed_refresh_uses_localized_stale_notice_and_preserves_cached_data(self):
        saved = {
            "answer": "Previously retrieved game facts.",
            "sources": [{"title": "Official source", "url": "https://example.test/game", "rank": 1}],
            "provider": "searchapi_google",
            "search_query": "Warframe previous query",
            "related_questions": [],
        }
        unchanged = json.dumps(saved, sort_keys=True)
        self.service.cache = SimpleNamespace(
            get=lambda _game, _question, **options: saved if options.get("allow_stale") else None
        )
        starts = {
            "ru": "Не удалось обновить поиск.",
            "en": "The search could not be refreshed.",
            "de": "Die Suche konnte nicht aktualisiert werden.",
            "fr": "La recherche n’a pas pu être actualisée.",
            "es": "No se ha podido actualizar la búsqueda.",
            "it": "Non è stato possibile aggiornare la ricerca.",
            "pt": "Não foi possível atualizar a pesquisa.",
        }
        for language, expected_start in starts.items():
            with self.subTest(language=language):
                self.cfg.values["app.language"] = language
                self.service.reset_context()
                self.service.searcher = FakeSearch([
                    SearchResponse(query="q", provider="searchapi_google", error="searchapi_timeout")
                ])
                owned, answer, meta = self.service.try_answer("как выбить Рино в Warframe")
                self.assertTrue(owned)
                self.assertTrue(answer.startswith(expected_start))
                self.assertIn(saved["answer"], answer)
                self.assertIn(saved["sources"][0]["url"], answer)
                notice = answer.split("\n\n", 1)[0].casefold()
                for misleading in ("google ai", "подтвержд", "verified", "bestätigt", "confirmé", "verificad", "verificat"):
                    self.assertNotIn(misleading, notice)
                self.assertEqual(meta["intent"], "google_ai_stale_cache")
                self.assertTrue(meta["cached"])
                self.assertTrue(meta["cache_stale"])
                self.assertEqual(meta["provider"], saved["provider"])
                self.assertEqual(meta["sources"], saved["sources"])
                self.assertEqual(meta["search_query"], saved["search_query"])
                self.assertEqual(len(self.service.searcher.calls), 1)
                self.assertEqual(self.ollama.calls, 0)
                self.assertEqual(json.dumps(saved, sort_keys=True), unchanged)

    def test_explicit_game_query_is_sent_unchanged(self):
        self.service.searcher = FakeSearch([self.google_ai()])
        raw = "как выбить Рино в Warframe"
        self.service.try_answer(raw)
        self.assertEqual(self.service.searcher.calls[0][0], raw)

    def test_typo_warfrme_still_resolves_explicit_warframe(self):
        self.game = {"game_id": "darktide", "game_name": "Warhammer 40,000: Darktide"}
        self.service.searcher = FakeSearch([self.google_ai()])
        raw = "как получить компоненты вафрфрейма рино в игре warfrme"
        owned, _answer, meta = self.service.try_answer(raw)
        self.assertTrue(owned)
        self.assertEqual(meta["game_id"], "warframe")
        self.assertEqual(self.service.searcher.calls[0][0], raw)

    def test_music_queue_command_is_never_owned_by_active_game(self):
        owned, meta = self.service.owns("покажи очередь треков")
        self.assertFalse(owned)
        self.assertEqual(meta, {})

    def test_asr_corrupted_question_word_still_routes_explicit_game_cue(self):
        self.game = {"game_id": "darktide", "game_name": "Warhammer 40,000: Darktide"}
        self.service.searcher = FakeSearch([self.google_ai("Rhino web answer")])
        raw = "так выбить брак в Warframe"
        owned, answer, meta = self.service.try_answer(raw)
        self.assertTrue(owned)
        self.assertEqual(meta["game_id"], "warframe")
        self.assertEqual(self.service.searcher.calls[0][0], raw)
        self.assertIn("Rhino web answer", answer)

    def test_general_web_lookup_uses_searchapi_but_explicit_browser_does_not(self):
        self.service.searcher = FakeSearch([self.google_ai("DLSS answer")])
        owned, answer, meta = self.service.try_general_answer("загугли что такое DLSS")
        self.assertTrue(owned)
        self.assertIn("DLSS answer", answer)
        self.assertEqual(meta["intent"], "general_google_ai_search")
        self.assertFalse(self.service.owns_general_web("найди в браузере DLSS"))
        game_owned, game_meta = self.service.owns("найди в браузере как выбить Рино в Warframe")
        self.assertFalse(game_owned)
        self.assertEqual(game_meta, {})

    def test_explicit_other_game_is_not_polluted_by_active_game(self):
        self.service.searcher = FakeSearch([self.google_ai()])
        raw = "как получить Сатори в игре Cyberpunk 2077"
        owned, _answer, meta = self.service.try_answer(raw)
        self.assertTrue(owned)
        self.assertNotEqual(meta["game_id"], "warframe")
        self.assertEqual(self.service.searcher.calls[0][0], raw)

    def test_google_ai_answer_is_not_rewritten_by_local_model(self):
        self.cfg.values["gaming.web_knowledge.suggest_followup"] = False
        exact = "Первая строка Google AI.\n\nВторая строка Google AI."
        self.service.searcher = FakeSearch([self.google_ai(exact)])
        _owned, answer, meta = self.service.try_answer("как выбить Рино")
        self.assertIn(exact, answer)
        self.assertEqual(self.ollama.calls, 0)
        self.assertEqual(meta["intent"], "google_ai_search")

    def test_sources_are_visible_by_default_without_rewriting_answer(self):
        raw = "Ответ Google AI. [Wiki](https://wiki.example/item)\n\nИсточники:\n- https://wiki.example/item"
        self.service.searcher = FakeSearch([self.google_ai(raw)])
        _owned, answer, meta = self.service.try_answer("как выбить Рино")
        self.assertIn("Ответ Google AI.", answer)
        self.assertIn("Wiki", answer)
        self.assertIn("https://wiki.warframe.com/w/Rhino", answer)
        self.assertTrue(meta["sources"])

    def test_sources_can_be_enabled_without_refetching_cached_answer(self):
        raw = "Ответ Google AI. [Wiki](https://wiki.example/item)"
        self.service.searcher = FakeSearch([self.google_ai(raw)])
        self.service.try_answer("как выбить Рино")
        self.cfg.values["gaming.web_knowledge.show_sources"] = True
        self.service.searcher = FakeSearch([])
        _owned, answer, meta = self.service.try_answer("как выбить Рино")
        self.assertTrue(meta["cached"])
        self.assertIn("Источники:", answer)
        self.assertIn("https://wiki.warframe.com/w/Rhino", answer)

    def test_missing_searchapi_key_message_is_actionable(self):
        self.service.searcher = FakeSearch([SearchResponse(query="q", provider="searchapi_google", error="searchapi_key_missing")])
        owned, answer, meta = self.service.try_answer("как выбить Рино")
        self.assertTrue(owned)
        self.assertIn("SearchAPI.io API key", answer)
        self.assertIn("Настройки", answer)
        self.assertFalse(meta["grounded"])
        self.assertEqual(self.ollama.calls, 0)

    def test_searchapi_auth_error_message_is_actionable(self):
        self.service.searcher = FakeSearch([SearchResponse(query="q", provider="searchapi_google", error="searchapi_auth_error")])
        _owned, answer, meta = self.service.try_answer("как выбить Рино")
        self.assertIn("отклонил авторизацию", answer)
        self.assertFalse(meta["grounded"])

    def test_no_google_ai_answer_fails_closed_without_ollama_fact_answer(self):
        self.service.searcher = FakeSearch([SearchResponse(query="q", provider="searchapi_google", error="searchapi_answer_missing")])
        owned, answer, meta = self.service.try_answer("как выбить Рино")
        self.assertTrue(owned)
        self.assertIn("не нашёл данных", answer.casefold())
        self.assertEqual(self.ollama.calls, 0)
        self.assertFalse(meta["grounded"])

    def test_cached_google_ai_answer_avoids_network(self):
        self.service.searcher = FakeSearch([self.google_ai("cached exact")])
        self.service.try_answer("как выбить Рино")
        self.service.searcher = FakeSearch([])
        owned, answer, meta = self.service.try_answer("как выбить Рино")
        self.assertTrue(owned)
        self.assertIn("cached exact", answer)
        self.assertTrue(meta["cached"])
        self.assertEqual(self.service.searcher.calls, [])

    def test_yes_resumes_pending_question_and_keeps_original_game(self):
        self.cfg.values["gaming.web_knowledge.suggest_followup"] = True
        first = self.google_ai("Rhino answer", related=["Какие способности есть у Рино?"])
        second = self.google_ai("Abilities answer")
        search = FakeSearch([first, second])
        self.service.searcher = search
        self.service.try_answer("как выбить Рино")
        resumed = self.service.resume_pending_followup("да")
        self.assertEqual(resumed, "Какие способности есть у Рино?")
        self.game = {"game_id": "darktide", "game_name": "Warhammer 40,000: Darktide"}
        owned, answer, meta = self.service.try_answer(resumed)
        self.assertTrue(owned)
        self.assertEqual(meta["game_id"], "warframe")
        self.assertIn("Предыдущий вопрос", search.calls[1][0])
        self.assertNotIn("Darktide", search.calls[1][0])
        self.assertIn("Abilities answer", answer)

    def test_short_followup_uses_saved_dialogue_not_new_active_process(self):
        first = self.google_ai("Rhino is a Warframe. This answer contains enough context.")
        second = self.google_ai("Follow-up")
        search = FakeSearch([first, second]); self.service.searcher = search
        self.service.try_answer("как выбить Рино")
        self.game = {"game_id": "darktide", "game_name": "Warhammer 40,000: Darktide"}
        owned, _answer, meta = self.service.try_answer("а где его фармить?")
        self.assertTrue(owned)
        self.assertEqual(meta["game_id"], "warframe")
        self.assertIn("Предыдущий вопрос", search.calls[1][0])
        self.assertNotIn("Darktide", search.calls[1][0])


class GameWebContextAndPresenceTests(unittest.TestCase):
    def test_active_game_context_is_small_json(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = FakeConfig(Path(td)); store = GameWebContextStore(cfg)
            store.record({"game_id": "warframe", "game_name": "Warframe", "pid": 1234}, status="running")
            row = store.read()
            self.assertEqual(row["game_id"], "warframe")
            self.assertEqual(row["status"], "running")
            self.assertNotIn("facts", row)
            self.assertNotIn("entities", row)

    def test_wallpaper_engine_store_utility_is_not_a_game_adapter(self):
        manager = GameManager(enabled=True)
        proc = GameProcess(10460, "wallpaper64.exe", r"C:\Steam\steamapps\common\wallpaper_engine\wallpaper64.exe", 20.0)
        self.assertIsNone(manager._store_adapter(proc))

    def test_background_generic_store_process_does_not_seed_active_game(self):
        manager = GameManager(enabled=True)
        generic = GameAdapter("store_some_tool", "Some Store Utility", ("tool.exe",))
        proc = GameProcess(5, "tool.exe", r"C:\Steam\steamapps\common\Some Store Utility\tool.exe", 20.0)
        manager.detect_all = lambda: [(generic, proc)]
        service = GamePresenceService(manager)
        with patch("jarvis.gaming.manager._foreground_pid", return_value=None):
            snap = service.snapshot(enabled=True, force=True)
        self.assertIsNone(snap)

    def test_foreground_generic_store_game_can_still_be_adopted(self):
        manager = GameManager(enabled=True)
        generic = GameAdapter("store_indie_game", "Indie Game", ("indie.exe",))
        proc = GameProcess(7, "indie.exe", r"C:\Steam\steamapps\common\Indie Game\indie.exe", 30.0)
        manager.detect_all = lambda: [(generic, proc)]
        service = GamePresenceService(manager)
        with patch("jarvis.gaming.manager._foreground_pid", return_value=7):
            snap = service.snapshot(enabled=True, force=True)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.game_id, "store_indie_game")
        self.assertTrue(snap.foreground_verified)

    def test_multiple_known_games_without_foreground_needs_disambiguation(self):
        manager = GameManager(enabled=True)
        a = GameAdapter("old", "Old Game", ("old.exe",))
        b = GameAdapter("new", "New Game", ("new.exe",))
        oldp = GameProcess(1, "old.exe", "C:/Steam/old.exe", 10.0)
        newp = GameProcess(2, "new.exe", "C:/Steam/new.exe", 20.0)
        manager.detect_all = lambda: [(a, oldp), (b, newp)]
        service = GamePresenceService(manager)
        with patch("jarvis.gaming.manager._foreground_pid", return_value=None):
            snap = service.snapshot(enabled=True, force=True)
        self.assertIsNone(snap)

    def test_tracker_notice_matches_session_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = FakeConfig(Path(td)); notices = []
            tracker = GamingContextTracker(cfg, notice_callback=notices.append)
            tracker._on_new_game({"game_name": "Warframe", "game_id": "warframe", "pid": 1, "session_started_at": 1.0})
            self.assertTrue(notices)
            self.assertIn("Warframe", notices[0])
            self.assertIn("Хорошей игры", notices[0])


class ArchitectureAuditTests(unittest.TestCase):
    def test_game_web_runtime_is_searchapi_google_ai_only(self):
        internal = Path(__file__).resolve().parents[1]
        game_web = internal / "jarvis" / "game_web"
        files = {p.name for p in game_web.glob("*.py")}
        self.assertEqual(files, {"__init__.py", "cache.py", "context.py", "models.py", "service.py", "searchapi.py"})
        source = (game_web / "service.py").read_text(encoding="utf-8")
        self.assertIn("SearchAPIGoogleProvider", source)
        self.assertNotIn("GoogleAIModeCoordinator", source)
        self.assertFalse((game_web / "google_ai.py").exists())
        self.assertFalse((game_web / "search.py").exists())
        self.assertFalse((game_web / "fetch.py").exists())

    def test_agent_resumes_pending_followup_before_fast_local_yes(self):
        agent = (Path(__file__).resolve().parents[1] / "jarvis" / "agent.py").read_text(encoding="utf-8")
        self.assertLess(agent.index("resumed = self._resume_game_followup"), agent.index("local = self._fast_local_reply", agent.index("resumed = self._resume_game_followup")))

    def test_agent_game_fact_guard_names_google_ai_not_tavily(self):
        agent = (Path(__file__).resolve().parents[1] / "jarvis" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("Game Web / Google AI Mode", agent)
        self.assertNotIn("Game Web / Tavily", agent)

    def test_settings_exposes_searchapi_key_and_no_browser_profile(self):
        source = (Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertIn("SearchAPI.io API key", source)
        self.assertIn("Открыть SearchAPI", source)
        self.assertIn("game_web_show_sources", source)
        self.assertIn("API-сервисы", source)
        self.assertNotIn("game_web_ai_overview", source)
        self.assertNotIn("Открыть профиль Google AI Mode", source)
        self.assertNotIn("game_web_serpapi_key", source)
        self.assertNotIn("game_web_browser_path", source)

    def test_current_version_profile_migrates_to_searchapi_and_retires_old_settings(self):
        internal = Path(__file__).resolve().parents[1]
        defaults = internal / "config" / "default_settings.json"
        with tempfile.TemporaryDirectory() as td:
            bootstrap = Path(td) / "NeoJarvis"
            bootstrap.mkdir(parents=True, exist_ok=True)
            (bootstrap / "settings.json").write_text(json.dumps({
                "settings_version": 134,
                "gaming": {
                    "enabled": True,
                    "game_data": {"v5_enabled": True},
                    "packs": {"darktide": {}},
                    "v6": {"enabled": True},
                    "web_knowledge": {
                        "enabled": True,
                        "ai_mode_backend": "browser",
                        "ai_mode_browser_path": "C:/chrome.exe",
                        "serpapi_api_key_encrypted": "old",
                        "tavily_api_key_encrypted": "retired",
                    },
                },
            }), encoding="utf-8")
            with patch.dict(os.environ, {"APPDATA": td, "LOCALAPPDATA": td}, clear=False):
                cfg = ConfigManager(defaults)
            gaming = cfg.get("gaming", {})
            self.assertNotIn("game_data", gaming)
            self.assertNotIn("packs", gaming)
            self.assertNotIn("v6", gaming)
            web = gaming["web_knowledge"]
            self.assertNotIn("ai_mode_backend", web)
            self.assertNotIn("ai_mode_browser_path", web)
            self.assertNotIn("serpapi_api_key_encrypted", web)
            self.assertNotIn("tavily_api_key_encrypted", web)
            self.assertIn("searchapi_api_key_encrypted", web)
            self.assertEqual(web["searchapi_gl"], "us")
            self.assertGreaterEqual(cfg.get("settings_version"), 142)

    def test_default_settings_are_searchapi_google_ai_only(self):
        internal = Path(__file__).resolve().parents[1]
        data = json.loads((internal / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["settings_version"], 142)
        web = data["gaming"]["web_knowledge"]
        self.assertIn("searchapi_api_key_encrypted", web)
        self.assertEqual(web["searchapi_gl"], "us")
        self.assertEqual(web["searchapi_timeout_seconds"], 18.0)
        self.assertNotIn("tavily_api_key_encrypted", web)
        self.assertNotIn("serpapi_api_key_encrypted", web)
        self.assertNotIn("ai_mode_backend", web)
        self.assertNotIn("ai_mode_browser_path", web)

    def test_web_followup_does_not_invoke_local_ollama_generation(self):
        source = (Path(__file__).resolve().parents[1] / "jarvis" / "game_web" / "service.py").read_text(encoding="utf-8")
        start = source.index("def _suggest_followup")
        end = source.index("def _append_invitation", start)
        self.assertNotIn("self.ollama.chat", source[start:end])

    def test_wake_path_uses_bounded_asr_probes_and_low_idle_meter_rate(self):
        internal = Path(__file__).resolve().parents[1]
        audio = (internal / "jarvis" / "audio_backend.py").read_text(encoding="utf-8")
        main = (internal / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("wake_probe_max_per_utterance", audio)
        self.assertIn("wake_probe_window_seconds", audio)
        self.assertIn("else 0.080", audio)
        self.assertIn("if self.status.text() == value", main)
        self.assertIn("self._wake_worker_active or self._wake_probe_worker_active", main)
        self.assertIn("self._typing_timer.setInterval(max(frame_ms, desired_typewriter_ms))", main)

    def test_robot_global_fps_is_user_selectable_and_capped_at_60(self):
        internal = Path(__file__).resolve().parents[1]
        source = (internal / "jarvis" / "ui" / "robot_widget.py").read_text(encoding="utf-8")
        settings = (internal / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        defaults = json.loads((internal / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertIn("def set_frame_rate", source)
        self.assertIn("self._target_fps = 30 if value <= 30 else 60", source)
        self.assertIn('for fps in (30, 60):', settings)
        self.assertIn('self.animation_fps.addItem(f"{fps} FPS", fps)', settings)
        self.assertEqual(defaults["ui"]["animation_fps"], 60)

    def test_cpu_safe_defaults_bound_repeat_wake_probe_and_visualizer_rate(self):
        internal = Path(__file__).resolve().parents[1]
        data = json.loads((internal / "config" / "default_settings.json").read_text(encoding="utf-8"))
        # HOTFIX133 deliberately makes the first retry quicker while preserving
        # the strict two-probe cap and one-thread Whisper budget.
        self.assertGreaterEqual(float(data["wake_word"]["fast_asr_probe_interval_seconds"]), 0.14)
        self.assertLessEqual(float(data["wake_word"]["fast_asr_probe_interval_seconds"]), 0.20)
        self.assertEqual(int(data["wake_word"]["fast_asr_probe_max_per_utterance"]), 2)
        self.assertEqual(int(data["wake_word"]["cpu_threads"]), 1)
        self.assertEqual(int(data["desktop"]["overlay"]["styles"]["music"]["visualizer_fps"]), 60)


    def test_v77_domain_firewall_and_web_browser_contract(self):
        internal = Path(__file__).resolve().parents[1]
        service = (internal / "jarvis" / "game_web" / "service.py").read_text(encoding="utf-8")
        semantics = (internal / "jarvis" / "runtime" / "semantics.py").read_text(encoding="utf-8")
        legacy = (internal / "jarvis" / "routing" / "legacy_direct_actions.py").read_text(encoding="utf-8")
        self.assertIn("LOCAL MEDIA OWNERSHIP FIREWALL", (internal / "jarvis" / "agent.py").read_text(encoding="utf-8"))
        self.assertIn("explicit_game_cue", service)
        self.assertIn("def owns_general_web", service)
        self.assertIn("def looks_like_web_lookup", semantics)
        self.assertIn("def looks_like_browser_search", semantics)
        self.assertNotIn('(?:найди|загугли|поищи)\\s+(?:там', legacy)

    def test_v77_queue_discord_telegram_and_mirroring_contract(self):
        internal = Path(__file__).resolve().parents[1]
        agent = (internal / "jarvis" / "agent.py").read_text(encoding="utf-8")
        yandex = (internal / "jarvis" / "integrations" / "yandex_music.py").read_text(encoding="utf-8")
        discord_bot = (internal / "jarvis" / "integrations" / "background" / "discord_bot.py").read_text(encoding="utf-8")
        telegram = (internal / "jarvis" / "integrations" / "telegram_remote.py").read_text(encoding="utf-8")
        main = (internal / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn('"queue_status": "queue_status"', agent)
        self.assertIn('owner = music_provider or "music"', agent)
        self.assertIn("def queue_status", yandex)
        self.assertIn("def queue_clear", yandex)
        self.assertIn("🧹 Очистить очередь", discord_bot)
        self.assertIn("def _clear_music_channel_history", discord_bot)
        # HOTFIX125: leaving voice/shutdown must preserve the canonical music
        # dashboard and recent-track history instead of deleting it.
        self.assertNotIn("await self._clear_music_channel_history(guild)", discord_bot)
        self.assertIn("await self._append_played_track_history(channel, previous_current)", discord_bot)
        self.assertIn("def set_desktop_tools", telegram)
        self.assertIn("_tools_for_direct_action", telegram)
        self.assertIn("def _try_shared_media_phrase", telegram)
        self.assertIn("def _media_queue_suffix", telegram)
        self.assertIn("def _mirror_assistant_to_telegram", main)
        self.assertIn("self._append_assistant(value, mirror_telegram=False)", main)
        self.assertIn("def _now_playing_queue_preview", main)
        self.assertIn("explicit_local_music = action_kind in", agent)

    def test_v77_mic_meter_is_interpolated_at_ui_fps_not_dsp_fps(self):
        internal = Path(__file__).resolve().parents[1]
        main = (internal / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        audio = (internal / "jarvis" / "audio_backend.py").read_text(encoding="utf-8")
        capture = (internal / "jarvis" / "ui" / "audio_capture.py").read_text(encoding="utf-8")
        self.assertIn("_mic_meter_smooth_tick", main)
        self.assertIn("self._mic_level_target", main)
        self.assertIn("self._mic_meter_timer.setInterval(33)", main)
        self.assertIn('if self._voice_mode in {"wake", "command"} and not self._mic_meter_timer.isActive():', main)
        self.assertIn("self._mic_meter_timer.stop()", main)
        self.assertIn("else 0.080", audio)
        self.assertIn("np.linalg.norm(block)", audio)
        self.assertIn("worker_event = Signal(object)", capture)
        self.assertIn("self.worker_event.emit(payload)", capture)
        self.assertNotIn("setInterval(25)", capture)

    def test_windows_setup_bootstrap_is_complete_and_does_not_require_websocket(self):
        internal = Path(__file__).resolve().parents[1]
        setup = (internal / "setup.ps1").read_text(encoding="utf-8-sig")
        probe = (internal / "_bootstrap_import_probe.py").read_text(encoding="utf-8")
        requirements = (internal / "requirements.txt").read_text(encoding="utf-8")
        guard = (internal / "dependency_guard.py").read_text(encoding="utf-8")
        self.assertGreater(len(setup.splitlines()), 300)
        self.assertIn("function Invoke-NeoProcess", setup)
        self.assertIn("function Find-CompatiblePython", setup)
        self.assertIn("function Install-WinSdk312Wheel", setup)
        self.assertIn('Write-Step "Installing NEO dependencies"', setup)
        self.assertIn('Write-Step "Installation complete"', setup)
        self.assertNotIn('"websocket"', probe)
        self.assertNotIn("websocket-client", requirements)
        self.assertNotIn('"websocket"', guard)

    def test_process_detector_has_no_game_specific_knowledge_probes(self):
        internal = Path(__file__).resolve().parents[1]
        source = (internal / "jarvis" / "gaming" / "manager.py").read_text(encoding="utf-8")
        for token in ("session_probe", "_minecraft_version_from_process", "_tarkov_mode_from_process"):
            self.assertNotIn(token, source)

    def test_adaptive_runtime_has_no_entity_corpus_layer(self):
        internal = Path(__file__).resolve().parents[1]
        adaptation = internal / "jarvis" / "adaptation"
        self.assertFalse((adaptation / "vocabulary.py").exists())
        self.assertFalse((adaptation / "entity_learning.py").exists())
        combined = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in adaptation.glob("*.py"))
        self.assertNotIn("DynamicVocabulary", combined)
        self.assertNotIn("EntityAliasLearner", combined)
        self.assertNotIn("entity_match", combined)

    def test_production_tree_has_no_retired_game_database_runtime(self):
        internal = Path(__file__).resolve().parents[1]
        targets = [internal / "jarvis", internal / "app.py", internal / "config" / "default_settings.json", internal / "setup.ps1"]
        banned = (
            "PackRegistry", "pack_manager", "knowledge.sqlite", "duckdb", "qdrant",
            "jarvis.gaming.v5", "jarvis.gaming.v6", "GameV5", "data_pack_shared",
            "pack_import_", "GraphReasoner", "typed_facts", "knowledge_docs",
            "is_game_knowledge_shape", "DynamicVocabulary", "EntityAliasLearner",
            "pack_generation", "game_ready", "should_defer_pack_freshness",
        )
        hits = []
        for target in targets:
            paths = target.rglob("*") if target.is_dir() else [target]
            for path in paths:
                if not path.is_file() or path.suffix.lower() not in {".py", ".json", ".ps1"}:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for token in banned:
                    if token.casefold() in text.casefold():
                        hits.append(f"{path.relative_to(internal)}:{token}")
        self.assertEqual(hits, [])
        self.assertFalse((internal / "jarvis" / "gaming" / "v5").exists())
        self.assertFalse((internal / "jarvis" / "gaming" / "v6").exists())
        self.assertFalse((internal / "data" / "game_data_packs").exists())

    def test_v78_quick_media_phrase_matrix(self):
        from jarvis.quick_media_commands import parse_quick_media_command

        expected = {
            "стоп": "pause",
            "стоп музыка": "pause",
            "продолжи воспроизведение": "resume",
            "возобнови воспроизведение": "resume",
            "следующая": "next",
            "next": "next",
            "назад": "previous",
            "предыдущая": "previous",
            "продолжи": "resume",
        }
        for phrase, action in expected.items():
            with self.subTest(phrase=phrase):
                command = parse_quick_media_command(phrase)
                self.assertIsNotNone(command)
                self.assertEqual(command.action, action)

    def test_v78_quick_media_discards_cancelled_rolling_result(self):
        internal = Path(__file__).resolve().parents[1]
        main = (internal / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertNotIn("self._dispatch_quick_media_command(stale_fast)", main)
        self.assertIn("Discarding stale voice capture", main)
        self.assertIn("generation != self._wake_generation", main)

    def test_v78_meaning_is_web_not_translation(self):
        from jarvis.integrations.local_translation import looks_like_translation_request, parse_translation_request
        self.assertFalse(looks_like_translation_request("что значит семейство кошачьих"))
        self.assertFalse(looks_like_translation_request("что означает семейство кошачьих"))
        self.assertFalse(looks_like_translation_request("what does feline family mean"))
        from jarvis.runtime.semantics import looks_like_general_question
        self.assertTrue(looks_like_general_question("что значит семейство кошачьих"))
        self.assertTrue(looks_like_general_question("what does feline family mean"))
        self.assertTrue(looks_like_translation_request("переведи привет мир"))
        self.assertEqual(parse_translation_request("переведи привет мир", ui_language="ru")[1], "en")
        self.assertEqual(parse_translation_request("translate hello", ui_language="ru")[1], "ru")

    def test_v78_telegram_flat_home_bluetooth_and_web_search(self):
        internal = Path(__file__).resolve().parents[1]
        telegram = (internal / "jarvis" / "integrations" / "telegram_remote.py").read_text(encoding="utf-8")
        home_start = telegram.index("def _home_keyboard")
        home_end = telegram.index("def _panel", home_start)
        home = telegram[home_start:home_end]
        self.assertNotIn('("🎵 Медиа", "ui:med")', home)
        for token in ("🎵 Музыка", "🎧 Discord", "▶️ YouTube", "🎙 Ввод", "🖱 Нажать мышью", "🎧 Вывод", "📡 Сеть", "🔵 Bluetooth", "🔎 Поиск информации", "🖱 Мышь"):
            self.assertIn(token, home)
        self.assertIn('"i:web:q": ("web_search"', telegram)
        self.assertIn("def _show_bluetooth_menu", telegram)
        self.assertIn("set_device_connection", telegram)
        self.assertIn("self._roll_panel_to_bottom(chat_key, view, old_panel)", telegram)

    def test_v78_briefing_bluetooth_and_weather_settings_contract(self):
        internal = Path(__file__).resolve().parents[1]
        defaults = json.loads((internal / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(defaults["settings_version"], 142)
        for key in ("include_time", "include_weather", "include_system", "include_processes", "include_devices", "include_bluetooth"):
            self.assertIn(key, defaults["briefing"])
        self.assertTrue(defaults["security"]["bluetooth_connect_notifications"])
        self.assertEqual(defaults["security"]["bluetooth_battery_threshold_percent"], 20)
        self.assertTrue(defaults["ui"]["robot_info_temperature_show_city"])
        settings = (internal / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertIn("Показывать название города рядом с погодой", settings)
        self.assertIn("Bluetooth-уведомления", settings)
        main = (internal / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        robot = (internal / "jarvis" / "ui" / "robot_widget.py").read_text(encoding="utf-8")
        self.assertIn("def _weather_emoji", main)
        self.assertIn("temperature_icon", robot)
        self.assertIn("def _poll_bluetooth_devices_async", main)

    def test_v79_briefing_is_spacious_and_reuses_detailed_system_formatter(self):
        internal = Path(__file__).resolve().parents[1]
        integrations = (internal / "jarvis" / "tools" / "integrations.py").read_text(encoding="utf-8")
        monitor = (internal / "jarvis" / "integrations" / "system_monitor.py").read_text(encoding="utf-8")
        self.assertIn("return format_snapshot(value, 'summary', code), value", integrations)
        self.assertIn('divider = "━━━━━━━━━━━━━━━━━━━━━━━━"', integrations)
        self.assertIn("section_name('devices', code)", integrations)
        self.assertIn("section_name('bluetooth', code)", integrations)
        self.assertIn(r'["\n".join(cpu_lines), *gpu_blocks, "\n".join(mem_lines), "\n".join(disk_lines), "\n".join(net_lines)]', monitor)

    def test_v79_device_inventory_forces_utf8_and_filters_pnp_plumbing(self):
        internal = Path(__file__).resolve().parents[1]
        source = (internal / "jarvis" / "integrations" / "bluetooth_devices.py").read_text(encoding="utf-8")
        self.assertIn("[System.Text.UTF8Encoding]::new($false)", source)
        self.assertIn("def _decode_powershell_output", source)
        self.assertIn("def _clean_present_device_name", source)
        self.assertNotIn("'HIDClass'", source[source.index('def list_present_devices'):source.index('def set_device_connection')])

    def test_v79_device_text_decoder_and_cleaner_are_functional(self):
        from jarvis.integrations.bluetooth_devices import _clean_present_device_name, _decode_powershell_output

        text = "Клавиатура HyperX"
        self.assertEqual(_decode_powershell_output(text.encode("cp866")), text)
        self.assertEqual(_clean_present_device_name("HID-compliant consumer control device"), "")
        self.assertEqual(_clean_present_device_name("  HyperX SoloCast  "), "HyperX SoloCast")
        self.assertNotIn("�", _clean_present_device_name("HyperX � SoloCast"))

    def test_v79_settings_layout_moves_briefing_and_panels(self):
        internal = Path(__file__).resolve().parents[1]
        source = (internal / "jarvis" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        init = source[source.index('self.tabs = ScrollableSettingsTabs()'):source.index('layout.addWidget(self.tabs, 1)')]
        self.assertNotIn('"Дополнительные панели"', init)
        games = source[source.index('def _games_tab'):source.index('def _test_searchapi_key')]
        self.assertNotIn('Показывать сводку на день при запуске', games)
        appearance = source[source.index('def _appearance_tab'):source.index('def _robot_tab')]
        self.assertIn('self._append_briefing_settings(root)', appearance)
        additional = source[source.index('def _additional_tab'):source.index('def _developer_tab')]
        # Release145 groups every panel appearance control under Widgets.
        # Actual widget ancestry and persisted values are checked in test_widgets145.
        self.assertNotIn('self._append_additional_panels(features_root)', additional)
        widgets = source[source.index('def _shelf_tab'):source.index('def _append_desktop_robot_settings')]
        self.assertIn('self._append_additional_panels(root)', widgets)
        self.assertIn('self.timer_widgets_group', widgets)

    def test_v79_telegram_home_pair_layout(self):
        from tests.release_contracts import home_callbacks
        internal = Path(__file__).resolve().parents[1]
        source = (internal / "jarvis/integrations/telegram_remote.py").read_text(encoding="utf-8")
        callbacks = home_callbacks(source)
        for required in ("ui:sys", "ui:shot", "a:info:sys", "a:info:proc", "ui:net", "ui:bt"):
            self.assertIn(required, callbacks)
        self.assertEqual(len(callbacks), len(set(callbacks)), "Each home action appears once")

    def test_v79_all_desktop_assistant_events_share_typewriter_and_media_dedup(self):
        internal = Path(__file__).resolve().parents[1]
        main = (internal / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("self._ambient_typing_queue", main)
        self.assertIn("def _ambient_typewriter_tick", main)
        self.assertIn('bubble = self.chat.add_assistant("" if use_pacing else text)', main)
        self.assertIn("self._arm_media_notice_suppression_for_text(text)", main)
        self.assertIn("Suppressed duplicate now-playing bubble", main)
        self.assertIn('self._append_assistant("Слушаю", mirror_telegram=False, paced=False)', main)
        self.assertIn('self.chat.update_bubble(self._listening_bubble, "Думаю")', main)

    def test_game_web_source_has_no_retired_search_provider_code(self):
        internal = Path(__file__).resolve().parents[1]
        game_web = internal / "jarvis" / "game_web"
        combined = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in game_web.glob("*.py"))
        for token in ("serpapi.com", "bing.com", "duckduckgo.com", "GoogleAIModeCoordinator", "chromedriver", "DevToolsActivePort"):
            self.assertNotIn(token, combined)
        self.assertIn("https://www.searchapi.io/api/v1/search", combined)
        self.assertNotIn("api.tavily.com", combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
