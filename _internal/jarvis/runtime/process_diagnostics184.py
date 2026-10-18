"""Parent-only, spawn-time process attribution; no discovery or readiness probe.

Only fixed labels, bounded numbers and validated build fields reach the existing
parent logger. Workers never call this module's logging entry points. Configured
capture policy is not a claim about the actual device or a successfully opened
stream. The cached identity describes the parent startup subset, not child bytes.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ROLES = frozenset({"gui", "microphone_capture", "stt_command", "stt_wake",
                    "stt_ambient_system", "stt_unspecified", "system_loopback"})
_STT_ROLES = {"NeoJarvis-STT-Command": "stt_command", "NeoJarvis-STT-Wake": "stt_wake",
              "NeoJarvis-STT-AmbientSystem": "stt_ambient_system"}
_BOOL_FIELDS = frozenset({"configured_voice_enabled", "configured_wake_enabled",
                        "configured_ambient_enabled", "configured_echo_enabled",
                        "configured_system_transcription_enabled"})
_INT_FIELDS = {"configured_target_rate_hz": (8000, 192000),
               "configured_command_cpu_threads": (1, 256),
               "configured_wake_cpu_threads": (1, 256)}
_context = {"build_revision": "unavailable", "parent_subset_sha256": "unavailable",
            "parent_subset_status": "unavailable"}


def stt_role(worker_name: str) -> str:
    # A caller-supplied thread name is never copied into the diagnostic log.
    return _STT_ROLES.get(worker_name, "stt_unspecified") if type(worker_name) is str else "stt_unspecified"


def _integer(value, low, high):
    if type(value) is int and low <= value <= high:
        return value
    if type(value) is str and len(value) <= 6 and value.isascii() and value.isdecimal():
        parsed = int(value)
        if low <= parsed <= high:
            return parsed
    return "unknown"


def _config(config, key, default):
    try:
        return config.get(key, default)
    except Exception:
        return None


def _fields(values):
    safe = {}
    for key, value in values.items():
        if key in _BOOL_FIELDS:
            safe[key] = ("true" if value else "false") if type(value) is bool else "unknown"
        elif key in _INT_FIELDS:
            safe[key] = _integer(value, *_INT_FIELDS[key])
    return safe


def _emit(role, pid, owner_pid, generation, event, fields):
    try:
        if role not in _ROLES or event not in {"start", "spawn"}:
            return
        if any(type(value) is not int or value <= 0 for value in (pid, generation)):
            return
        if type(owner_pid) is not int or owner_pid < 0:
            return
        row = {"event": event, "role": role, "pid": pid, "owner_pid": owner_pid or "unknown",
               "process_generation": generation, "readiness": "not_checked",
               "identity_scope": "parent_startup_subset", **_context, **fields}
        logger.info("NEO process diagnostic: %s", " ".join(f"{key}={value}" for key, value in row.items()))
    except Exception:
        # Observability must not strand a successfully spawned native child.
        return


def record_gui_start(config, identity):
    """Consume the already calculated startup identity; never read source files."""
    global _context
    try:
        revision = identity.get("packaged_revision")
        digest = identity.get("local_subset_sha256")
        status = identity.get("subset_status")
        _context = {
            "build_revision": revision if type(revision) is str and _REVISION.fullmatch(revision) else "unavailable",
            "parent_subset_sha256": digest if type(digest) is str and _SHA256.fullmatch(digest) else "unavailable",
            "parent_subset_status": status if type(status) is str and status in {"match", "mismatch", "uncompared", "unavailable"} else "unavailable",
        }
        values = {}
        for field, key, default in (
            ("configured_voice_enabled", "voice.enabled", True),
            ("configured_wake_enabled", "wake_word.enabled", False),
            ("configured_ambient_enabled", "ambient_learning.enabled", True),
            ("configured_echo_enabled", "ambient_learning.system_echo_guard_enabled", True),
            ("configured_system_transcription_enabled", "ambient_learning.system_audio_transcription_enabled", False),
            ("configured_target_rate_hz", "voice.sample_rate", 16000),
            ("configured_command_cpu_threads", "voice.stt_cpu_threads", 4),
            ("configured_wake_cpu_threads", "wake_word.cpu_threads", 2),
        ):
            values[field] = _config(config, key, default)
        fields = _fields(values)
        fields.update(coverage="core_owned_only", fast_media_owner="stt_wake", partial_asr_owner="stt_wake")
        _emit("gui", os.getpid(), os.getppid(), 1, "start", fields)
    except Exception:
        return


def record_spawn(role, pid, generation, **configured):
    """One record for this successful Popen, even if it exits before readiness."""
    try:
        fields = _fields(configured)
        if role == "microphone_capture":
            fields.update(capture_rate_policy="native_then_48000_44100_16000",
                          capture_block_policy="2048_first_ge_32000_else_1024",
                          actual_capture_rate_hz="unknown", actual_capture_block_frames="unknown")
        elif role == "system_loopback":
            fields.update(configured_capture_rate_hz=48000, configured_capture_block_frames=2048,
                          configured_echo_reference_min_interval_ms=140,
                          actual_capture_rate_hz="unknown", actual_capture_block_frames="unknown")
        elif role in {"stt_command", "stt_wake", "stt_ambient_system", "stt_unspecified"}:
            fields.update(asr_input_policy="float32_resampled_to_16000")
        _emit(role, pid, os.getpid(), generation, "spawn", fields)
    except Exception:
        return
