"""GUI-side response lifecycle: is an answer still running, and the strict voice «стоп»."""
from __future__ import annotations

from jarvis.runtime.answer_stop179 import stop_phrase


class ResponseOutputMixin:
    def _response_active(self) -> bool:
        return bool(getattr(self, '_busy', False)
            or getattr(self, '_ambient_typing_bubble', None) is not None
            or bool(getattr(self, '_ambient_typing_queue', ()))
            or getattr(self, '_briefing_active', False)
            or bool(getattr(getattr(self, '_response_voice_service', None), 'active', False)))

    def _strict_stop_phrase(self, text: str) -> bool:
        return stop_phrase(text, self._language_code(), addresses=(
            self.config.get('assistant_name', 'NEO'),
            self.config.get('wake_word.phrase', 'NEO'), 'NEO', 'Нео'))

    def _output_stop_from_voice(self, text: str) -> bool:
        if not self._response_active():
            return False
        if self._strict_stop_phrase(text):
            self._interrupt_current_operation(source='voice-stop')
            return True
        return False

    def _ensure_response_voice(self):
        service = getattr(self, '_response_voice_service', None)
        if service is None:
            from jarvis.response_voice.service import VoiceService
            from PySide6.QtCore import QTimer
            service = self._response_voice_service = VoiceService(self.config)
            self._response_voice_timer = QTimer(self)
            self._response_voice_timer.setInterval(100)
            self._response_voice_timer.timeout.connect(self._poll_response_voice)
        return service

    def _queue_response_voice(self, text):
        if not self.config.get('response_voice.enabled', False) or getattr(self, '_quitting', False):
            return
        service = self._ensure_response_voice()
        if service.submit(text, self._language_code()):
            self._response_voice_timer.start()

    def _poll_response_voice(self):
        service = getattr(self, '_response_voice_service', None)
        if service is None or not service.active:
            self._response_voice_timer.stop()
            if not getattr(self, '_quitting', False) and not getattr(self, '_busy', False):
                self._rearm_voice_after_answer()
