"""A question may open one normal command capture, never a permanent session."""
import time


def _pending(agent, language, now):
    store = getattr(agent, 'conversations', None)
    if store is None:
        return None
    pending = store.get('desktop').pending_intent
    if pending is None or pending.language != language:
        return None
    now = time.monotonic() if now is None else now
    if now >= pending.expires_at:
        return None
    return pending


def _game_title(agent, language, now):
    service = getattr(agent, 'game_web', None)
    read = getattr(service, 'pending_game_title', None)
    return read(language, scope='desktop', now=now) if callable(read) else None


def _study188(agent, language):
    """NEO 188 NEO-STUDY-001: an asked practice question awaits a free-text answer (desktop only)."""
    session = (getattr(agent, '_study_sessions188', None) or {}).get('desktop')
    if session is None or getattr(session, 'state', '') != 'asked' or getattr(session, 'language', language) != language:
        return None
    return session


def pending_slot(agent, language, *, now=None):
    pending = _pending(agent, language, now)
    if pending is not None:
        return pending.missing()
    if _study188(agent, language) is not None:
        from jarvis.runtime.dialogue_tasks import Slot
        return Slot('study_answer', 'text', 'text')
    if _game_title(agent, language, now):
        from jarvis.runtime.dialogue_tasks import Slot
        return Slot('game_title', 'name', 'game')
    return None


def pending_question(agent, language, *, now=None):
    pending = _pending(agent, language, now)
    study = _study188(agent, language) if pending is None else None
    if study is not None:
        current = study.current
        return (id(study), 'study_answer', getattr(current, 'number', 0), len(getattr(study, 'retry_queue', ()) or ()))
    if pending is None:
        title = _game_title(agent, language, now)
        return (id(agent.game_web), 'game_title', title[1]) if title else None
    slot = pending.missing()
    if slot is None:
        return None
    return (id(pending), slot.name, pending.expires_at)


def silence_retry188(window, recording, *, now=None):
    """Decide and perform the single silence retry (see MainWindow._silence_retry188)."""
    if str(getattr(recording, 'end_reason', '') or '') != 'no_speech_timeout':
        return False                                   # Stop / errors / unknown origin: never retry
    if (not getattr(window, '_clarification_voice_origin156', False) or getattr(window, '_continuous_voice_session', False)
            or getattr(window, '_app_mic_muted', False) or getattr(window, '_quitting', False)
            or not bool(window.config.get('voice.enabled', True))):
        return False
    agent = getattr(window, 'agent', None)
    if agent is None or getattr(agent, '_pending_context', None):
        return False                                   # a pending action confirmation is not a field question
    language = window._language_code()
    question = pending_question(agent, language, now=now)
    if question is None or getattr(window, '_clarification_armed156', None) != (window._agent_generation, question):
        return False                                   # this capture did not belong to THIS question
    pending = _pending(agent, language, now)
    slot = pending.missing() if pending is not None else None
    if slot is None or slot.kind in {'confirm', 'text'} or pending.purpose in {'neo_model169', 'correction', 'param188'}:
        return False
    if getattr(window, '_silence_retry_used188', None) == question:
        return False                                   # exactly one retry per open field
    window._silence_retry_used188 = question
    restore = getattr(window, '_restore_listening_audio', None)
    if callable(restore):
        restore()
    window._clear_listening_bubble()
    window._set_busy(False, "", "curious")
    window._append_assistant(pending.question(), mirror_telegram=False, localize=False)
    generation = window._agent_generation

    from PySide6.QtCore import QTimer
    started = __import__('time').monotonic()

    def rearm():
        # Re-check everything at the moment the microphone would open again.
        if (window._agent_generation != generation or getattr(window, '_busy', False) or getattr(window, '_quitting', False)
                or getattr(window, '_app_mic_muted', False) or pending_question(agent, language) != question):
            window._resume_wake_listening(delay_ms=40)
            return
        voice = getattr(window, '_response_voice_service', None)
        if voice is not None and (getattr(voice, 'active', False) or getattr(voice, 'echo_guard_active', False)):
            # NEO-TTS-001 / VSR: open the microphone only after the spoken question ended.
            if __import__('time').monotonic() - started < 20:
                QTimer.singleShot(250, rearm)
            else:
                window._resume_wake_listening(delay_ms=40)
            return
        window._start_command_capture(seamless=False)

    QTimer.singleShot(int(window.config.get('voice.silence_retry_delay_ms', 900) or 900), rearm)
    return True
