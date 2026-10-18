from __future__ import annotations

import threading
import time
from pathlib import Path
import unittest

from jarvis.routing.ownership_firewall import classify_turn_ownership
from jarvis.runtime.integration_phrases import _weather_signal, parse_integration_phrase
from jarvis.wake_matcher import wake_alias_is_strong
from jarvis.integrations.yandex_music import YandexMusicAdapter
from jarvis.database import _compact_action_result


from tests.release_contracts import assert_release

class Hotfix127FieldLogAuditContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.internal = Path(__file__).resolve().parents[1]

    def text(self, relative: str) -> str:
        return (self.internal / relative).read_text(encoding="utf-8")

    def test_veteran_never_collides_with_wind_weather_namespace(self):
        phrase = "Какие способности есть у ветерана"
        self.assertFalse(_weather_signal(phrase.casefold()))
        self.assertNotEqual(classify_turn_ownership(phrase).owner, "weather")
        self.assertIsNone(parse_integration_phrase(phrase))
        self.assertTrue(_weather_signal("какой ветер сегодня"))
        self.assertEqual(classify_turn_ownership("какой ветер сегодня").owner, "weather")

    def test_agent_tool_selection_uses_shared_weather_guard(self):
        agent = self.text("jarvis/agent.py")
        self.assertIn("if _weather_signal(lowered)", agent)
        self.assertNotIn('add("weather", "погод", "температур", "прогноз", "дожд", "осад", "ветер"', agent)
        ownership = self.text("jarvis/routing/ownership_firewall.py")
        self.assertIn("if _weather_signal(t):", ownership)
        self.assertNotIn('осад\\w*|ветер\\w*|влажност', ownership)

    def test_old_semantic_wake_hallucinations_are_rescue_only(self):
        # These exact strings are present in the supplied historical logs/alias
        # corpus. They must never be accepted by the immediate wake fast path.
        # A custom long wake phrase keeps the phonetic rules (NEO 178 ships «NEO»).
        for value in ("Jeremy's", "Jeremy", "Service", "There it is", "Choice"):
            self.assertFalse(wake_alias_is_strong(value, "Jarvis"), value)
        for value in ("Jarvis", "Jervis", "Dzharvis", "Жарвис"):
            self.assertTrue(wake_alias_is_strong(value, "Jarvis"), value)

    def test_wave_refill_relaxes_history_only_after_strict_pool_exhaustion(self):
        # Exercise the asynchronous refill in isolation. The strict diversity
        # source is empty (the exact long-session failure from the field log),
        # but official Rotor still returns playable continuation tracks.
        obj = object.__new__(YandexMusicAdapter)
        obj._playback_lock = threading.RLock()
        obj._wave_refill_lock = threading.Lock()
        obj._wave_refill_active = False
        obj._station = "user:onyourwave"
        obj._station_batch_id = "old"
        obj._station_queue = []
        obj._current_track_obj = {"id": "current", "title": "Current", "artists": []}
        obj._current_track_meta = {"id": "current"}
        obj._history = [{"id": "old-1"}, {"id": "old-2"}]
        # Since NEO 15x the background refill asks the official Rotor continuation
        # (wave_tracks) directly; the relaxed-history tier lives in next() and is
        # covered by test_next_has_direct_rotor_continuity_before_failure.
        obj._wave_feedback_ready = threading.Event()
        obj._wave_feedback_ready.set()
        obj._wave_last_completed_id = "old-2"
        obj._wave_queue_generation = 0
        obj._playback_generation = 1
        obj._manual_pause_active = False
        obj._queue_reported_count = -1
        obj.wave_strict_repeat_policy = lambda: False
        obj._wave_playback_floor = lambda: (set(), set())
        obj._after_playback_effect = lambda *_a, **_kw: None
        obj._persist_playback = lambda *_a, **_kw: None
        calls: list[dict] = []

        def wave_tracks(**kw):
            calls.append(kw)
            return {"tracks": [
                {"id": "current", "title": "Current"},
                {"id": "next-1", "title": "One"},
                {"id": "next-2", "title": "Two"},
                {"id": "next-3", "title": "Three"},
                {"id": "next-4", "title": "Four"},
                {"id": "next-5", "title": "Five"},
                {"id": "next-6", "title": "Six"},
                {"id": "next-7", "title": "Seven"},
                {"id": "next-8", "title": "Eight"},
            ], "batch_id": "fresh-batch"}
        obj.wave_tracks = wave_tracks
        warmed: list[list[dict]] = []
        obj._start_prefetch_window = lambda items, limit=8: warmed.append(list(items[:limit]))

        YandexMusicAdapter._ensure_wave_queue_depth(obj, 8)
        deadline = time.time() + 2.0
        while obj._wave_refill_active and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(obj._wave_refill_active)
        self.assertGreaterEqual(len(obj._station_queue), 8)
        self.assertNotIn("current", {str(x.get("id", "")) for x in obj._station_queue})
        self.assertEqual(obj._station_batch_id, "old")  # The current track retains its ORIGINAL Rotor batch
        self.assertTrue(warmed)
        self.assertGreaterEqual(len(warmed[-1]), 5)
        # Rotor continuation starts from the last finished track and avoids the current one.
        self.assertEqual(calls[0]["queue_track_id"], "old-2")
        self.assertIn("current", calls[0]["avoid_ids"])

    def test_next_has_direct_rotor_continuity_before_failure(self):
        yandex = self.text("jarvis/integrations/yandex_music.py")
        marker = "Yandex My Wave continuity relaxed recent history after fresh pool exhaustion"
        self.assertIn(marker, yandex)
        self.assertIn('continuity_raw, continuity_batch = self._station_tracks(', yandex)
        self.assertIn('queue = usable(list(continuity_raw or []), strict_recent=False)', yandex)
        self.assertLess(yandex.index(marker), yandex.index('raise RuntimeError("Яндекс Музыка не предложила следующий доступный трек после повторных запросов")'))
        self.assertIn('if station == "user:onyourwave" and len(queue) < 5:', yandex)
        self.assertIn('self._ensure_wave_queue_depth(8)', yandex)

    def test_cached_music_owner_short_circuits_pause_resume_fanout(self):
        main = self.text("jarvis/ui/main_window.py")
        start = main.index("cached_success = False")
        block = main[start:main.index("# No music owner handled the command", start)]
        # Cached-first latency survives, but other confirmed active owners also act.
        self.assertLess(block.index("cached_success = run_owner(cached_owner)"), block.index("self._quick_media_statuses(include_video=False)"))
        fast = block.split("if cached_success:", 1)[1].split("elif not statuses:", 1)[0]
        self.assertIn("recent_music_states", fast)
        self.assertNotIn("_quick_media_statuses", fast)
        self.assertIn("owner not in attempted and active_for_transport(owner)", block)


    def test_manual_next_coalesces_with_natural_end_instead_of_failing(self):
        yandex = self.text("jarvis/integrations/yandex_music.py")
        self.assertIn('"coalesced": not auto', yandex)
        self.assertNotIn('raise RuntimeError("Переключение фонового трека уже выполняется")', yandex)
        tool = self.text("jarvis/tools/domains/browser_media.py")
        self.assertIn('bool(data.get("already_advancing", False))', tool)
        self.assertIn("Переключение трека уже выполняется.", tool)

    def test_telegram_polling_isolated_and_backed_off_on_network_failure(self):
        tg = self.text("jarvis/integrations/telegram_remote.py")
        self.assertIn("poll_session = requests.Session()", tg)
        self.assertIn("session_override=poll_session", tg)
        self.assertIn("failure_delay = 2.0", tg)
        self.assertIn("failure_delay = min(30.0, failure_delay * 1.7)", tg)

    def test_native_com_audio_has_explicit_serialized_shutdown(self):
        app = self.text("app.py")
        worker = self.text("jarvis/audio/control_worker.py")
        self.assertIn("shutdown_audio_control_executor(timeout=2.0)", app)
        self.assertIn('name="jarvis-audio-control"', worker)
        self.assertIn("CoUninitialize()", worker)


    def test_long_session_logs_are_rotated_and_rejected_wake_is_rate_limited(self):
        logging_setup = self.text("jarvis/logging_setup.py")
        self.assertIn("RotatingFileHandler", logging_setup)
        self.assertIn("maxBytes=12 * 1024 * 1024", logging_setup)
        self.assertIn("backupCount=4", logging_setup)
        main = self.text("jarvis/ui/main_window.py")
        # One INFO sample every 12 s, everything else at DEBUG (single emit call since NEO 16x).
        self.assertIn("emit = logger.info if now - self._last_wake_reject_log_at >= 12.0 else logger.debug", main)
        self.assertIn('emit("Wake rejected;', main)

    def test_persisted_media_action_results_drop_volatile_audio_and_long_urls(self):
        payload = {
            "success": True,
            "data": {
                "title": "Track",
                "artist": "Artist",
                "levels": [0.5] * 1000,
                "url": "https://example.invalid/" + ("x" * 5000),
                "queue": [{"title": "Next"}],
            },
        }
        import json
        compacted = _compact_action_result(json.dumps(payload, ensure_ascii=False))
        self.assertIn("Track", compacted)
        self.assertIn("Next", compacted)
        self.assertNotIn('"levels"', compacted)
        self.assertIn("<omitted long media URL>", compacted)
        self.assertLessEqual(len(compacted), 2400)
        database = self.text("jarvis/database.py")
        self.assertIn("MAX(id), 0) - 5000", database)

    def test_context_memory_drops_live_media_arrays_before_persistence(self):
        memory = self.text("jarvis/runtime/context_memory.py")
        self.assertIn('"levels", "audio_levels", "spectrum", "waveform", "pcm", "audio_b64"', memory)
        self.assertIn("<omitted long media URL>", memory)

    def test_release_marker(self):
        # Release names evolve; preserved feature contracts are tested below.
        assert_release(self, self.text("VERSION.txt"))


if __name__ == "__main__":
    unittest.main()
