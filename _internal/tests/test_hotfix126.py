from __future__ import annotations

from pathlib import Path
import unittest


class Hotfix126VoiceStatusBubbleContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.internal = Path(__file__).resolve().parents[1]
        cls.main = (cls.internal / "jarvis/ui/main_window.py").read_text(encoding="utf-8")
        cls.chat = (cls.internal / "jarvis/ui/chat_view.py").read_text(encoding="utf-8")

    def test_one_voice_status_bubble_changes_listening_to_thinking(self):
        self.assertIn('self._append_assistant("Слушаю", mirror_telegram=False, paced=False)', self.main)
        self.assertIn('self.chat.update_bubble(self._listening_bubble, "Слушаю")', self.main)
        self.assertIn('self.chat.update_bubble(self._listening_bubble, "Думаю")', self.main)
        self.assertIn('self._show_thinking_bubble()', self.main)
        # Recording completion must no longer delete the listening row before ASR.
        audio_finished = self.main[self.main.index("def _audio_finished"):self.main.index("def _transcribe_command")]
        self.assertNotIn("_clear_listening_bubble()", audio_finished)

    def test_voice_agent_reuses_status_bubble_as_response(self):
        marker = 'if input_source == "voice" and self._listening_bubble is not None:'
        self.assertIn(marker, self.main)
        self.assertIn('self._pending_response_bubble = self._listening_bubble', self.main)
        self.assertIn('self._listening_bubble = None', self.main)
        self.assertIn('self.chat.update_bubble(self._pending_response_bubble, "Думаю")', self.main)

    def test_resolved_user_message_keeps_normal_chat_order(self):
        self.assertIn("def move_bubble_to_bottom", self.chat)
        self.assertIn("self.chat.move_bubble_to_bottom(self._listening_bubble)", self.main)
        self.assertIn("self.items.append(item)", self.chat)

    def test_manual_and_wake_command_capture_both_show_listening(self):
        prepare = self.main[self.main.index("def _prepare_command_capture_ui"):self.main.index("def _duck_for_listening")]
        self.assertIn("self._show_listening_bubble()", prepare)

    def test_noise_and_system_echo_do_not_leave_stale_thinking_row(self):
        echo = self.main[self.main.index("def _discard_system_echo_command"):self.main.index("def _wake_hypothesis_needs_verification")]
        self.assertIn("self._clear_listening_bubble()", echo)
        voice = self.main[self.main.index("def _voice_recognized"):self.main.index("def _send_recognized_text")]
        self.assertGreaterEqual(voice.count("self._clear_listening_bubble()"), 3)

    def test_quick_media_can_finish_the_same_voice_status_row(self):
        failed = self.main[self.main.index("def _quick_media_failed"):self.main.index("def _quick_media_finished")]
        finished = self.main[self.main.index("def _quick_media_finished"):self.main.index("def _launch_wake_transcription")]
        self.assertIn("self._listening_bubble is not None", failed)
        self.assertIn("self.chat.update_bubble(bubble, failure_text)", failed)
        self.assertIn("self._listening_bubble is not None", finished)
        self.assertIn("self.chat.update_bubble(bubble, display_text)", finished)


if __name__ == "__main__":
    unittest.main()
