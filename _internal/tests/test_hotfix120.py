from __future__ import annotations

import json
from pathlib import Path

from jarvis.quick_media_commands import parse_quick_media_command

ROOT = Path(__file__).resolve().parents[1]


def test_hotfix120_wake_latency_defaults_are_bounded():
    data = json.loads((ROOT / "config" / "default_settings.json").read_text(encoding="utf-8"))
    wake = data["wake_word"]
    assert data["settings_version"] >= 157
    assert float(wake["silence_seconds"]) <= 0.12
    assert float(wake["fast_asr_probe_interval_seconds"]) <= 0.24
    assert float(wake["fast_asr_probe_min_audio_seconds"]) <= 0.22
    assert int(wake["fast_asr_probe_max_per_utterance"]) >= 2
    assert wake["fast_probe_language"] == "auto"


def test_hotfix120_short_transport_commands_use_quick_parser():
    expected = {
        "дальше": "next",
        "продолжить": "resume",
        "предыдущий": "previous",
        "пауза": "pause",
    }
    for phrase, action in expected.items():
        parsed = parse_quick_media_command(phrase, "ru")
        assert parsed is not None
        assert parsed.action == action


def test_hotfix120_raw_partial_asr_is_not_committed_to_chat():
    source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
    start = source.index("def _prepare_command_capture_ui")
    end = source.index("def _duck_for_listening", start)
    block = source[start:end]
    assert "self._live_user_bubble = None" in block
    assert "self.chat.add_user" not in block
    assert "SpeechUnderstanding/command repair" in block


def test_hotfix120_rolling_probe_handles_quick_media_before_wake_match():
    source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
    start = source.index("def _fast_wake_probe_transcribed")
    end = source.index("def _wake_probe_scored", start)
    block = source[start:end]
    assert block.index("parse_quick_media_command") < block.index("match_wake_phrase")
    assert "_dispatch_quick_media_command" in block


def test_hotfix120_telegram_callbacks_do_not_block_actions():
    source = (ROOT / "jarvis" / "integrations" / "telegram_remote.py").read_text(encoding="utf-8")
    start = source.index("def _answer_callback(")
    end = source.index("def _edit_panel", start)
    block = source[start:end]
    assert "put_nowait" in block
    assert "answerCallbackQuery" not in block
    assert "def _callback_ack_loop" in source


def test_hotfix120_telegram_has_direct_short_media_lane_and_separate_poll_session():
    source = (ROOT / "jarvis" / "integrations" / "telegram_remote.py").read_text(encoding="utf-8")
    shared_start = source.index("def _try_shared_media_phrase")
    shared_end = source.index("def _media_queue_suffix", shared_start)
    shared = source[shared_start:shared_end]
    assert "parse_quick_media_command" in shared
    assert "recent_music_owner" in shared
    assert '{"pause", "resume", "next", "previous"}' in shared
    run_start = source.index("def _run(self)")
    run_block = source[run_start:run_start + 9000]
    assert "poll_session = requests.Session()" in run_block
    assert "session_override=poll_session" in run_block
