from pathlib import Path
import json
import unittest

from tests.release_contracts import assert_release

ROOT = Path(__file__).resolve().parents[1]


from tests.release_contracts import assert_existing_window_visibility

from tests.release_contracts import assert_headphone_geometry

class Hotfix133Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = (ROOT / "jarvis/agent.py").read_text(encoding="utf-8")
        cls.ollama = (ROOT / "jarvis/ollama_client.py").read_text(encoding="utf-8")
        cls.main = (ROOT / "jarvis/ui/main_window.py").read_text(encoding="utf-8")
        cls.robot = (ROOT / "jarvis/ui/robot_widget.py").read_text(encoding="utf-8")
        cls.telegram = (ROOT / "jarvis/integrations/telegram_remote.py").read_text(encoding="utf-8")
        cls.mouse = (ROOT / "jarvis/integrations/telegram_mouse.py").read_text(encoding="utf-8")
        cls.discord = (ROOT / "jarvis/integrations/background/discord_bot.py").read_text(encoding="utf-8")
        cls.timeline = (ROOT / "jarvis/runtime/system_timeline.py").read_text(encoding="utf-8")
        start_bat = ROOT.parent / "START_JARVIS.bat"
        cls.start_bat = start_bat.read_text(encoding="utf-8") if start_bat.exists() else ""
        cls.settings = json.loads((ROOT / "config/default_settings.json").read_text(encoding="utf-8"))
        cls.version = (ROOT / "VERSION.txt").read_text(encoding="utf-8")

    def test_neural_network_is_hard_disabled(self):
        # Updated policy: text fallback is prohibited, explicit local vision works.
        from unittest.mock import Mock
        from jarvis.ollama_client import OllamaClient, OllamaError
        cfg = Mock()
        cfg.get.side_effect = lambda key, default=None: self.settings["ollama"].get(key.split(".")[-1], default)
        client = OllamaClient(cfg)
        client._session = Mock()
        self.assertFalse(self.settings["ollama"]["requests_enabled"])
        self.assertTrue(self.settings["ollama"]["vision_enabled"])
        self.assertFalse(self.settings["ollama"]["preload_model"])
        self.assertEqual(self.settings["ollama"]["vision_keep_alive"], 0)
        with self.assertRaises(OllamaError):
            client.generate_text("question")
        with self.assertRaises(OllamaError):
            client.chat([{"role": "user", "content": "question"}])
        with self.assertRaises(OllamaError):
            list(client.chat_stream([{"role": "user", "content": "question"}]))
        client.warmup()
        client._session.post.assert_not_called()
        client._session.get.assert_not_called()

    def test_unknown_input_clarifies_instead_of_model_fallback(self):
        self.assertIn("Agent route=no_model_fallback", self.agent)
        self.assertIn('message = locale_message("clarify", code)', self.agent)
        self.assertIn("SearchAPI.io", self.agent)
        # The final fallback section is model-free.
        start = self.agent.index("# HOTFIX133: local neural-model fallback")
        end = self.agent.index("\n    def ", start)
        body = self.agent[start:end]
        self.assertNotIn("_agent_loop(", body)
        self.assertNotIn("ollama.", body)

    def test_neo_tracks_asr_alias_routes_as_my_tracks(self):
        self.assertIn("(?:мои|neo|нео)\\s+(?:треки|песни)", self.agent)
        self.assertIn('action_kind = "my_tracks"', self.agent)

    def test_music_visual_is_continuous_and_only_real_playback_driven(self):
        self.assertIn('music_active = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))', self.robot)
        self.assertIn('action = "music_bop" if music_visible else self._idle_action', self.robot)
        self.assertIn("Music is a persistent physical layer", self.robot)
        self.assertIn("4.25 <= phase <= 4.90", self.robot)
        # Random idle scenes must never put headphones on by themselves.
        scheduler = self.robot[self.robot.index("self._idle_action = random.choice"): self.robot.index("self._idle_action_started_at", self.robot.index("self._idle_action = random.choice"))]
        self.assertNotIn('"music_bop"', scheduler)

    def test_headphones_scale_and_fit_extra_envelope(self):
        assert_headphone_geometry(self, self.robot)
        self.assertIn("cy - 108.0 * scale", self.robot)
        self.assertIn("fit_w_units = 300.0 if music_active else 282.0", self.robot)
        self.assertIn("fit_h_units = 258.0 if music_active else 226.0", self.robot)

    def test_notes_are_randomized_per_spawn_but_do_not_jitter(self):
        start = self.robot.index('note_glyphs = ("♪", "♫", "♩", "♬", "♭", "♯")')
        end = self.robot.index('elif action == "solar_system"', start)
        body = self.robot[start:end]
        self.assertIn("cycle_index", body)
        self.assertIn("glyph = note_glyphs", body)
        self.assertIn("x = lane_x", body)
        self.assertNotIn("math.sin", body)

    def test_telegram_has_one_panel_and_one_line_xy_click(self):
        start = self.telegram.index("def _send_message")
        end = self.telegram.index("def _callback_ack_loop", start)
        send_body = self.telegram[start:end]
        self.assertNotIn("_schedule_panel_bottom", send_body)
        home_start = self.telegram.index("def _show_home")
        home_end = self.telegram.index("\n    def ", home_start + 10)
        home = self.telegram[home_start:home_end]
        self.assertIn('self._tg_text("NEO Remote")', home)
        self.assertNotIn("Выбери раздел", home)
        self.assertIn('"remote_mouse_xy"', self.telegram)
        self.assertIn('re.fullmatch(r"\\s*(-?\\d+)\\s+(-?\\d+)\\s*", value)', self.telegram)
        self.assertIn("capture_coordinate_grid(self.config, monitor)", self.telegram)
        self.assertIn("ImageFont.truetype", self.mouse)
        self.assertIn("font_px = max(18", self.mouse)
        self.assertIn("user32.mouse_event", self.mouse)

    def test_discord_controls_exist_only_while_voice_connected(self):
        self.assertIn("async def _remove_music_dashboard", self.discord)
        self.assertIn("played history preserved", self.discord)
        self.assertIn("await self._remove_music_dashboard(guild)", self.discord)
        self.assertIn('if "connected" in state and not bool(state.get("connected")):', self.discord)
        self.assertIn("live_connected = (", self.discord)
        self.assertIn("if not live_connected:", self.discord)
        self.assertIn("history_appended = True", self.discord)
        self.assertIn("await self._append_played_track_history(channel, previous_current)", self.discord)
        self.assertIn("await message.delete()", self.discord)
        self.assertIn("member_voice", self.discord)

    def test_hotkey_uses_z_order_without_focus_or_hwnd_rebuild(self):
        # Real hide stops paint work; the existing HWND and audio remain alive.
        assert_existing_window_visibility(self, self.main)

    def test_cpu_defaults_are_reduced(self):
        self.assertEqual(self.settings["ui"]["animation_fps"], 60)
        self.assertFalse(self.settings["adaptation"]["ambient_learning"]["system_audio_transcription_enabled"] if "adaptation" in self.settings and "ambient_learning" in self.settings["adaptation"] else self.settings["ambient_learning"]["system_audio_transcription_enabled"])
        self.assertFalse(self.settings["context_memory"]["explorer_folder_tracking_enabled"])
        self.assertIn("explorer_folder_tracking_enabled", self.timeline)

    def test_version_marker(self):
        # Old hotfix markers were folded into NEO releases; the release contract is shared (see release_contracts).
        assert_release(self, self.version)


if __name__ == "__main__":
    unittest.main()
