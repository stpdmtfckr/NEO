# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.settings_write_lock import profile_write_lock
from jarvis.settings_storage184 import prepare_settings, recover_settings, snapshot_settings, rollback_settings, SETTINGS_FOLDER

DEFAULT_ASSISTANT_NAME = "NEO"
DEFAULT_USER_NAME = ""          # NEO 183: empty until the user types a name
DEFAULT_ASSISTANT_PROMPT = (
    "You are the user's desktop assistant and helpful companion. Help with the computer and communicate naturally and calmly. "
    "You may discuss games, music, plans, the user's day, and ordinary conversation. Be unobtrusive, practical, and concise when appropriate."
)

import threading as _threading182
_SAVE_LOCK182 = _threading182.RLock()
_PROFILE_LOCKS186 = _threading182.local()


class _OwnedProfileLock186:
    """186: the sidecar file lock is not re-entrant; a recovery that runs inside an
    already protected Save/rollback reuses the ownership of that writer instead of
    acquiring the lock a second time."""

    def __init__(self, path):
        self.key = str(path)
        self._inner = None

    def __enter__(self):
        held = getattr(_PROFILE_LOCKS186, "held", None)
        if held is None:
            held = _PROFILE_LOCKS186.held = {}
        if held.get(self.key):
            held[self.key] += 1
            return self
        self._inner = profile_write_lock(Path(self.key))
        self._inner.__enter__()
        held[self.key] = 1
        return self

    def __exit__(self, *exc):
        held = _PROFILE_LOCKS186.held
        held[self.key] -= 1
        if held[self.key] <= 0:
            held.pop(self.key, None)
            if self._inner is not None:
                return self._inner.__exit__(*exc)
        return False

@dataclass(frozen=True)
class AppPaths:
    root: Path
    settings: Path
    database: Path
    logs: Path
    screenshots: Path
    backups: Path
    cache: Path


class ConfigManager:
    def __init__(self, default_settings_path: Path):
        self.default_settings_path = default_settings_path
        # Packaged defaults are immutable for the lifetime of a running build.
        # Cache them once instead of reparsing a ~large JSON tree on every voice turn.
        self._defaults_cache: dict[str, Any] = self._read_json(default_settings_path)
        self._settings_signature: tuple[int, int] | None = None
        self._active_wake_aliases_cache: tuple[str, tuple[str, ...]] | None = None
        # HOTFIX143-STORAGE: NEO owns exactly one per-user data root.  Earlier
        # builds split permanent state under Roaming\NeoJarvis while runtime,
        # bootstrap, instance and startup data lived under Local\NeoJarvis.  That
        # made diagnostics confusing and clean-install preservation error-prone.
        # Keep *all* NEO-owned state below LOCALAPPDATA\NeoJarvis and migrate the
        # legacy Roaming tree once, before settings/logging are opened.
        local_base = Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".local")))
        self._bootstrap_root = local_base / "NeoJarvis"
        self._bootstrap_root.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_roaming_root()
        self._settings_path184 = prepare_settings(self._bootstrap_root, self.default_settings_path, fresh_model_offer=True)
        self.paths = AppPaths(
            root=self._bootstrap_root,
            settings=self._settings_path184,
            database=self._bootstrap_root / "assistant.db",
            logs=self._bootstrap_root / "logs",
            screenshots=self._bootstrap_root / "screenshots",
            backups=self._bootstrap_root / "backups",
            cache=self._bootstrap_root / "cache",
        )
        for directory in (self.paths.root, self.paths.logs, self.paths.screenshots, self.paths.backups, self.paths.cache):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.paths.settings.exists():
            shutil.copy2(self.default_settings_path, self.paths.settings)
        self.data: dict[str, Any] = {}
        self.recovered_settings_backup = ""
        self.recovered_settings_reason = ""
        self.repaired_settings: list[str] = []
        self.reload()


    @staticmethod
    def _merge_legacy_directory(source: Path, destination: Path) -> None:
        """Merge an old NEO data tree without overwriting newer local files.

        This runs before logging/database initialization.  Conflicting files are
        retained beside the canonical copy with a ``migrated_roaming_`` prefix so
        migration never destroys diagnostics or user data.
        """
        if not source.exists() or source.resolve(strict=False) == destination.resolve(strict=False):
            return
        destination.mkdir(parents=True, exist_ok=True)
        for item in list(source.iterdir()):
            target = destination / item.name
            try:
                if item.is_dir():
                    if target.exists() and target.is_dir():
                        ConfigManager._merge_legacy_directory(item, target)
                    elif not target.exists():
                        shutil.move(str(item), str(target))
                    else:
                        # A file blocks the old directory name; preserve it under
                        # a deterministic migration name instead of deleting data.
                        alt = destination / f"migrated_roaming_{item.name}"
                        suffix = 1
                        while alt.exists():
                            alt = destination / f"migrated_roaming_{suffix}_{item.name}"
                            suffix += 1
                        shutil.move(str(item), str(alt))
                    continue
                if not target.exists():
                    shutil.move(str(item), str(target))
                    continue
                try:
                    same = item.stat().st_size == target.stat().st_size and item.read_bytes() == target.read_bytes()
                except Exception:
                    same = False
                if same:
                    item.unlink(missing_ok=True)
                    continue
                # settings.json is special: if Local already contains only the
                # packaged default while Roaming has a user's older profile, the
                # migration happens before a Local default is created, so normal
                # move above wins.  Any later conflict is preserved, not guessed.
                alt = destination / f"migrated_roaming_{item.name}"
                suffix = 1
                while alt.exists():
                    alt = destination / f"migrated_roaming_{suffix}_{item.name}"
                    suffix += 1
                shutil.move(str(item), str(alt))
            except Exception:
                # Migration is best-effort and must never prevent NEO startup.
                continue
        try:
            source.rmdir()
        except OSError:
            pass

    def _migrate_legacy_roaming_root(self) -> None:
        roaming = str(os.getenv("APPDATA", "") or "").strip()
        if not roaming:
            return
        legacy = Path(roaming) / "NeoJarvis"
        try:
            if legacy.exists() and legacy.resolve(strict=False) != self._bootstrap_root.resolve(strict=False):
                self._merge_legacy_directory(legacy, self._bootstrap_root)
                # Remove now-empty legacy parents only at the NeoJarvis level;
                # never touch APPDATA itself.
                try:
                    if legacy.exists() and not any(legacy.iterdir()):
                        legacy.rmdir()
                except OSError:
                    pass
        except Exception:
            pass

    @staticmethod
    def _expand_storage_path(value: Any, fallback: Path) -> Path:
        text = str(value or "").strip()
        if not text:
            return fallback
        try:
            expanded = os.path.expandvars(os.path.expanduser(text))
            return Path(expanded).resolve(strict=False)
        except Exception:
            return fallback

    def _apply_storage_paths(self) -> None:
        # HOTFIX143: storage is intentionally not splittable anymore.  Runtime,
        # cache, logs and durable data share one canonical LocalAppData root.
        # Keep legacy keys normalized for compatibility with old Settings files.
        storage = self.data.setdefault("storage", {})
        if not isinstance(storage, dict):
            storage = {}
            self.data["storage"] = storage
        data_root = self._bootstrap_root
        logs = data_root / "logs"
        screenshots = data_root / "screenshots"
        backups = data_root / "backups"
        cache = data_root / "cache"
        storage.update({
            "data_dir": str(data_root),
            "settings_dir": str(self.paths.settings.parent),
            "logs_dir": str(logs),
            "screenshots_dir": str(screenshots),
            "backups_dir": str(backups),
            "cache_dir": str(cache),
        })
        self.paths = AppPaths(
            root=data_root,
            settings=getattr(self, "_settings_path184", self.paths.settings),
            database=data_root / "assistant.db",
            logs=logs,
            screenshots=screenshots,
            backups=backups,
            cache=cache,
        )
        for directory in (self.paths.root, self.paths.logs, self.paths.screenshots, self.paths.backups, self.paths.cache):
            directory.mkdir(parents=True, exist_ok=True)
        # Legacy media helpers that cannot receive ConfigManager directly use this
        # process-local pointer. It is set before integrations are constructed.
        os.environ["NEOJARVIS_CACHE_DIR"] = str(self.paths.cache)

    def _settings_file_signature(self) -> tuple[int, int] | None:
        try:
            st = self.paths.settings.stat()
            return int(st.st_mtime_ns), int(st.st_size)
        except OSError:
            return None

    def reload(self) -> dict[str, Any]:
        # Settings live in the same canonical LocalAppData NeoJarvis root as all other NEO state.
        settings_path = self.paths.settings
        try:
            self._observed_settings186 = settings_path.read_bytes() if settings_path.exists() else None
        except OSError:
            self._observed_settings186 = None
        try:
            current = self._read_json(settings_path)
            if not isinstance(current, dict):
                raise ValueError("settings.json must hold a JSON object")
            self.data = self._deep_merge(self._defaults_cache, current)
            self._repair_types183()
            self._migrate_settings()
            # An explicit model selection is user data, including in old profiles.
            selected_chat = current.get("ollama", {}).get("chat_model") if isinstance(current.get("ollama"), dict) else None
            if isinstance(selected_chat, str) and selected_chat.strip():
                self.data.setdefault("ollama", {})["chat_model"] = selected_chat
            from jarvis.runtime.model_defaults184 import migrate as migrate_model_defaults
            migrate_model_defaults(self.data, current)
            self._apply_storage_paths()
            self._expand_allowed_roots()
        except Exception as exc:
            # NEO 182: a damaged settings.json no longer blocks the start. The file is kept beside
            # the new one for diagnosis and NEO starts with the default settings.
            # NEO 183: the whole preparation (paths, allowed folders) is covered, not only the reading.
            self._recovery_deferred187 = False
            self.recovered_settings_backup = self._quarantine_settings182(settings_path, exc)
            self.recovered_settings_reason = f"{type(exc).__name__}: {exc}"[:300]
            recovered = {} if self._recovery_deferred187 else self._read_json(settings_path)
            self.data = self._deep_merge(self._defaults_cache, recovered)
            self._repair_types183()
            self._migrate_settings()
            selected_chat = recovered.get("ollama", {}).get("chat_model") if isinstance(recovered.get("ollama"), dict) else None
            if isinstance(selected_chat, str) and selected_chat.strip():
                self.data.setdefault("ollama", {})["chat_model"] = selected_chat
            from jarvis.runtime.model_defaults184 import migrate as migrate_model_defaults
            migrate_model_defaults(self.data, recovered)
            self._apply_storage_paths()
            self._expand_allowed_roots()
        self._settings_signature = self._settings_file_signature()
        self._active_wake_aliases_cache = None
        return self.data

    def _repair_types183(self) -> None:
        """NEO 183: one field of the wrong type is put back to its default instead of discarding
        every other (valid) setting. The names of the repaired fields are kept for the UI notice."""
        repaired: list[str] = []

        def walk(defaults: dict, data: dict, prefix: str) -> None:
            for key, default in defaults.items():
                if key not in data:
                    continue
                value = data[key]
                name = f"{prefix}{key}"
                if isinstance(default, dict):
                    if not isinstance(value, dict):
                        data[key] = json.loads(json.dumps(default))
                        repaired.append(name)
                    else:
                        walk(default, value, name + ".")
                elif isinstance(default, list) and not isinstance(value, list):
                    data[key] = list(default)
                    repaired.append(name)
                elif isinstance(default, bool) and not isinstance(value, bool):
                    data[key] = default
                    repaired.append(name)
                elif isinstance(default, (int, float)) and not isinstance(default, bool):
                    invalid_number = isinstance(value, float) and not math.isfinite(value)
                    if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
                        try:
                            invalid_number = not math.isfinite(float(value))
                        except OverflowError:
                            invalid_number = True
                    if invalid_number:
                        data[key] = default
                        repaired.append(name)
                        continue
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        try:
                            converted = type(default)(value)
                            if isinstance(converted, float) and not math.isfinite(converted):
                                raise ValueError("Non-finite numeric setting")
                            data[key] = converted
                        except (TypeError, ValueError, OverflowError):
                            data[key] = default
                            repaired.append(name)
                elif isinstance(default, str) and not isinstance(value, str):
                    data[key] = default
                    repaired.append(name)

        walk(self._defaults_cache, self.data, "")
        roots = (self.data.get("security") or {}).get("allowed_roots")
        if isinstance(roots, list) and not all(isinstance(x, str) for x in roots):
            self.data["security"]["allowed_roots"] = [x for x in roots if isinstance(x, str)]
            repaired.append("security.allowed_roots")
        self.repaired_settings = repaired

    def _quarantine_settings182(self, settings_path: Path, exc: BaseException) -> str:
        # 186: automatic recovery joins the writer serialization and restores only
        # while the primary still holds the generation that failed to load; a
        # newer successful Save is never replaced by an older copy.
        with _SAVE_LOCK182:
            lock = None
            for _attempt in range(3):
                try:
                    lock = _OwnedProfileLock186(settings_path)
                    lock.__enter__()
                    break
                except (OSError, TimeoutError):
                    lock = None
                    time.sleep(0.2)
            if lock is None:
                # NEO 187: without the cross-process lock another NEO process may
                # be saving right now. Never replace the primary file blindly:
                # start on defaults in memory and leave the files untouched.
                self._recovery_deferred187 = True
                self.recovered_settings_source = ""
                return ""
            try:
                backup, restored_from = recover_settings(
                    settings_path, self._bootstrap_root, self.default_settings_path,
                    observed=getattr(self, "_observed_settings186", None), check_generation=True)
            finally:
                if lock is not None:
                    lock.__exit__(None, None, None)
        self.recovered_settings_source = restored_from if restored_from != str(settings_path) else ""
        return backup

    def rollback_settings(self, backup: Path | None = None) -> dict[str, Any]:
        """Restore a valid local backup explicitly, preserving the displaced profile."""
        with _SAVE_LOCK182, _OwnedProfileLock186(self.paths.settings):
            rollback_settings(self.paths.settings, backup)
            return self.reload()

    def reload_if_changed(self) -> dict[str, Any]:
        """Reload settings only when settings.json actually changed on disk.

        Agent.handle_stream is called for every command. Re-reading and deeply
        merging the complete settings tree there was deterministic but wasted CPU,
        allocations and disk-cache traffic. A nanosecond mtime + size signature is
        enough because all in-app writes are atomic replacements.
        """
        signature = self._settings_file_signature()
        if signature is not None and signature == self._settings_signature:
            return self.data
        return self.reload()

    # NEO 183: OAuth tokens that rotate in the background. An ordinary save (Settings «Apply») keeps
    # whatever is newest on disk for them; only a save that really carries new credentials
    # (``credentials=True``: OAuth sign-in, token refresh) may change them.
    _RUNTIME_CREDENTIALS183 = (
        ("integrations", "spotify", "access_token_encrypted"),
        ("integrations", "spotify", "refresh_token_encrypted"),
        ("integrations", "spotify", "token_expires_at"),
    )

    def save(self, data: dict[str, Any], *, credentials: bool = False) -> None:
        # NEO 182: one writer at a time (Settings, Spotify token refresh, Telegram remote…).
        with _SAVE_LOCK182:
            with _OwnedProfileLock186(self.paths.settings):
                if not credentials:
                    data = self._keep_disk_credentials183(data)
                autostart_changed = self._save_locked182(data)
            self._apply_saved_system_settings(autostart_changed)

    def update(self, mutate, *, credentials: bool = False) -> bool:
        """NEO 183: read-modify-write under the save lock, starting from the newest file on disk.

        ``mutate(data)`` changes the copy in place; returning ``False`` cancels the write."""
        with _SAVE_LOCK182:
            with _OwnedProfileLock186(self.paths.settings):
                self.reload_if_changed()
                data = json.loads(json.dumps(self.data))
                if mutate(data) is False:
                    return False
                if not credentials:
                    data = self._keep_disk_credentials183(data)
                autostart_changed = self._save_locked182(data)
            self._apply_saved_system_settings(autostart_changed)
            return True

    def _keep_disk_credentials183(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            disk = json.loads(self.paths.settings.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return data
        if not isinstance(disk, dict):
            return data
        data = json.loads(json.dumps(data))
        for path in self._RUNTIME_CREDENTIALS183:
            node_disk, node_new = disk, data
            for part in path[:-1]:
                node_disk = node_disk.get(part) if isinstance(node_disk, dict) else None
                node_new = node_new.setdefault(part, {}) if isinstance(node_new, dict) else None
            if isinstance(node_disk, dict) and path[-1] in node_disk and isinstance(node_new, dict):
                node_new[path[-1]] = node_disk[path[-1]]
        return data

    def _save_locked182(self, data: dict[str, Any]) -> bool:
        previous_autostart_enabled = bool(self.get("ui.start_with_windows", False))
        previous_priority_enabled = bool(self.get("ui.priority_startup", False))
        serializable = json.loads(json.dumps(data))
        serializable["settings_version"] = 179
        serializable.setdefault("app", {})["display_name"] = "NEO"
        serializable.setdefault("ollama", {})["requests_enabled"] = False
        serializable["ollama"]["preload_model"] = False
        serializable["ollama"]["vision_keep_alive"] = 0
        gaming_save = serializable.setdefault("gaming", {})
        if not isinstance(gaming_save, dict):
            gaming_save = {}
            serializable["gaming"] = gaming_save
        for retired_key in ("game_data", "packs", "pack_root", "database", "knowledge", "rag", "v5", "v6"):
            gaming_save.pop(retired_key, None)
        web_save = gaming_save.setdefault("web_knowledge", {})
        if isinstance(web_save, dict):
            for retired_key in (
                "ai_mode_backend", "ai_mode_browser_path", "ai_mode_browser_timeout_seconds",
                "ai_mode_virtual_time_ms", "ai_mode_cdp_timeout_seconds",
                "ai_mode_auto_open_profile_setup", "ai_mode_require_signed_in_profile",
                "serpapi_api_key_encrypted", "serpapi_timeout_seconds",
                "tavily_api_key_encrypted", "tavily_search_depth", "tavily_answer_mode",
                "tavily_max_results", "tavily_chunks_per_source", "tavily_timeout_seconds",
                "searchapi_ai_overview_fallback",
            ):
                web_save.pop(retired_key, None)
            web_save.setdefault("searchapi_api_key_encrypted", "")
            web_save.setdefault("searchapi_gl", "us")
            web_save.setdefault("searchapi_max_sources", 8)
            web_save.setdefault("searchapi_timeout_seconds", 18.0)
            web_save.setdefault("show_sources", False)
        # HOTFIX99: exactly one desktop music provider may be active. The UI
        # enforces radio-like toggles, and save() repeats the invariant so
        # hand-edited or stale settings can never enable several providers.
        _ints = serializable.setdefault("integrations", {})
        _ints.pop("apple_music", None)  # HOTFIX100: purge retired Apple Music credentials/settings
        _music = _ints.setdefault("music", {})
        # Custom media/wake controls: keep remote wake backwards-compatible for
        # existing settings files while exposing an independent Settings toggle.
        _telegram = _ints.setdefault("telegram", {})
        _telegram.setdefault("remote_wake_enabled", True)
        _providers = ("yandex_music", "spotify")
        _enabled = [name for name in _providers if bool((_ints.get(name) or {}).get("enabled", False))]
        _active = str(_music.get("active_provider", "") or "").strip().casefold()
        if _active not in _providers or _active not in _enabled:
            _active = _enabled[0] if _enabled else ""
        for _name in _providers:
            _ints.setdefault(_name, {})["enabled"] = bool(_active and _name == _active)
        _music["active_provider"] = _active
        serializable.setdefault("desktop", {})["preferred_music_app"] = _active or "yandex_music"
        _storage = serializable.setdefault("storage", {})
        _root = self._bootstrap_root
        _storage.update({
            "data_dir": str(_root),
            "settings_dir": str(self.paths.settings.parent),
            "logs_dir": str(_root / "logs"),
            "screenshots_dir": str(_root / "screenshots"),
            "backups_dir": str(_root / "backups"),
            "cache_dir": str(_root / "cache"),
        })
        app = serializable.setdefault("app", {})
        language = str(app.get("language", "en") or "en").strip().casefold()
        if language not in {"ru", "en", "de", "fr", "es", "it", "pt"}:
            language = "en"
        app["language"] = language
        # Provider availability depends on credentials/capability, never UI locale.
        voice = serializable.setdefault("voice", {})
        voice.setdefault("language", "auto")
        voice["command_language"] = language
        voice.setdefault("noise_rejection_percent", 55)
        voice.setdefault("stt_model", "small")
        voice.setdefault("partial_stt_model", "tiny")
        voice.setdefault("stt_device", "cpu")
        voice.setdefault("stt_compute_type", "int8")
        voice["use_live_result_as_final"] = False
        from jarvis.runtime.numeric_schema186 import clamp as _clamp186
        voice["silence_seconds"] = _clamp186("voice.silence_seconds", voice.get("silence_seconds", 1.05) or 1.05)
        voice.setdefault("fast_media_enabled", True)
        voice.setdefault("quick_media_volume_enabled", True)
        voice.setdefault("quick_media_repeat_shuffle_enabled", True)
        voice.setdefault("fast_media_stt_model", "tiny")
        voice["fast_media_max_seconds"] = max(1.2, min(4.0, float(voice.get("fast_media_max_seconds", 2.4) or 2.4)))
        voice.setdefault("adaptive_endpoint_enabled", True)
        voice["short_command_silence_seconds"] = max(0.32, min(0.80, float(voice.get("short_command_silence_seconds", 0.40) or 0.40)))
        voice["normal_command_silence_seconds"] = max(0.52, min(1.15, float(voice.get("normal_command_silence_seconds", 0.68) or 0.68)))
        voice["stt_worker_backend"] = "subprocess"
        voice["audio_backend"] = "sounddevice_subprocess_blocking"
        voice["audio_safe_host_fallback"] = True
        voice["live_partial_max_updates"] = max(0, min(2, int(voice.get("live_partial_max_updates", 0) or 0)))
        voice["post_answer_rearm_ms"] = max(0, min(500, int(voice.get("post_answer_rearm_ms", 20) or 0)))
        mode = str(voice.get("interaction_mode", "") or "").strip().casefold()
        if mode not in {"continuous", "wake_each_command"}:
            mode = "continuous" if bool(voice.get("continuous_after_wake", False)) else "wake_each_command"
        voice["interaction_mode"] = mode
        # Keep the old boolean as a compatibility mirror for older modules.
        voice["continuous_after_wake"] = mode == "continuous"
        wake = serializable.setdefault("wake_word", {})
        packaged_wake = self._defaults_cache.get("wake_word", {}) or {}
        default_phrase = str(packaged_wake.get("phrase", "") or "").strip()
        wake.setdefault("stt_model", str(packaged_wake.get("stt_model", "tiny") or "tiny"))
        phrase = str(wake.get("phrase", default_phrase) or "").strip()
        if len("".join(ch for ch in phrase if ch.isalpha())) < 3:
            wake["phrase"] = default_phrase
        aliases_raw = wake.get("aliases", []) or []
        if isinstance(aliases_raw, str):
            aliases_raw = aliases_raw.splitlines()
        # One lexical wake spelling always expands into the requested three
        # editable variants: bare, period and exclamation. Matching remains
        # punctuation-normalized, so this improves ASR coverage without changing
        # the fuzzy threshold.
        bases: list[str] = []
        seen_bases: set[str] = set()
        for item in aliases_raw:
            value = str(item or "").strip().rstrip(".!?").strip()
            normalized = "".join(ch for ch in value.casefold() if ch.isalnum())
            if len(normalized) < 2 or normalized in seen_bases:
                continue
            seen_bases.add(normalized)
            bases.append(value)
        phrase_value = str(wake.get("phrase", default_phrase) or default_phrase).strip().rstrip(".!?").strip()
        phrase_key = "".join(ch for ch in phrase_value.casefold() if ch.isalnum())
        if phrase_key and phrase_key not in seen_bases:
            bases.insert(0, phrase_value)
        aliases: list[str] = []
        for base in bases:
            # NEO 178: «?» too — speech recognition writes «Нео?» as often as «Нео.» and «Нео!».
            aliases.extend((base, base + ".", base + "!", base + "?"))
        # Preserve exact user-visible spellings in addition to normalized triples.
        # The matcher is case/punctuation normalized, but the Settings field must
        # not silently drop observations that differ only by case (for example the
        # requested lowercase primary ``джарвис`` entry).
        exact_seen = set(aliases)
        for raw in aliases_raw:
            exact = str(raw or "").strip()
            if exact and exact not in exact_seen:
                aliases.append(exact)
                exact_seen.add(exact)
        wake["aliases"] = aliases[:900]
        # Some multilingual Whisper spellings are useful only while a particular
        # command language is active. Keep these exact observed aliases out of the
        # global corpus so ordinary English/German/etc. speech cannot wake JARVIS.
        packaged_language_aliases = packaged_wake.get("language_aliases", {}) or {}
        language_aliases = wake.get("language_aliases", {}) or {}
        if not isinstance(language_aliases, dict):
            language_aliases = {}
        normalized_language_aliases: dict[str, list[str]] = {}
        for lang in ("ru", "en", "de", "fr", "es", "it", "pt"):
            raw_values = language_aliases.get(lang, packaged_language_aliases.get(lang, [])) or []
            if isinstance(raw_values, str):
                raw_values = raw_values.splitlines()
            lang_bases: list[str] = []
            lang_seen: set[str] = set()
            for item in raw_values:
                base = str(item or "").strip().rstrip(".!?").strip()
                key = "".join(ch for ch in base.casefold() if ch.isalnum())
                if len(key) < 2 or key in lang_seen:
                    continue
                lang_seen.add(key)
                lang_bases.append(base)
            expanded: list[str] = []
            for base in lang_bases:
                expanded.extend((base, base + ".", base + "!", base + "?"))
            if expanded:
                normalized_language_aliases[lang] = expanded[:180]
        wake["language_aliases"] = normalized_language_aliases
        wake.setdefault("acoustic_model_phrase", str(packaged_wake.get("acoustic_model_phrase", "") or ""))
        from jarvis.runtime.numeric_schema186 import clamp as _clamp186
        wake["chunk_seconds"] = _clamp186("wake_word.chunk_seconds", wake.get("chunk_seconds", 0.95) or 0.95)
        wake["silence_seconds"] = _clamp186("wake_word.silence_seconds", wake.get("silence_seconds", 0.12) or 0.12)
        wake["fuzzy_threshold"] = max(0.82, min(0.96, float(wake.get("fuzzy_threshold", 0.88) or 0.88)))
        wake["acoustic_enabled"] = bool(wake.get("acoustic_enabled", False))
        wake["probe_interval_seconds"] = max(0.14, min(0.45, float(wake.get("probe_interval_seconds", 0.18) or 0.18)))
        wake["fast_asr_probe_enabled"] = bool(wake.get("fast_asr_probe_enabled", True))
        wake["fast_asr_probe_interval_seconds"] = max(0.14, min(0.75, float(wake.get("fast_asr_probe_interval_seconds", 0.18) or 0.18)))
        wake["fast_asr_probe_min_audio_seconds"] = max(0.18, min(0.80, float(wake.get("fast_asr_probe_min_audio_seconds", 0.22) or 0.22)))
        wake["fast_asr_probe_window_seconds"] = max(0.50, min(1.40, float(wake.get("fast_asr_probe_window_seconds", 0.78) or 0.78)))
        probe_language = str(wake.get("fast_probe_language", packaged_wake.get("fast_probe_language", "en")) or "en").strip().casefold()
        wake["fast_probe_language"] = probe_language if probe_language in {"auto", "en", "ru"} else "en"
        wake["preroll_seconds"] = max(0.75, min(1.8, float(wake.get("preroll_seconds", 1.15) or 1.15)))
        wake.setdefault("quick_commands_enabled", True)
        wake["acoustic_threshold"] = max(0.05, min(0.50, float(wake.get("acoustic_threshold", 0.12) or 0.12)))
        wake["cpu_threads"] = max(1, min(4, int(wake.get("cpu_threads", 1) or 1)))
        wake["fast_asr_probe_max_per_utterance"] = max(1, min(3, int(wake.get("fast_asr_probe_max_per_utterance", 2) or 2)))
        wake.pop("scan_interval_seconds", None)
        ollama = serializable.setdefault("ollama", {})
        ollama.setdefault("chat_model", "qwen2.5:0.5b")
        ollama.setdefault("vision_model", "qwen3.5:4b")
        ollama.setdefault("num_ctx", 4096)
        ollama.setdefault("vision_num_ctx", 8192)
        ollama.setdefault("num_predict", 512)
        try:
            inferred_unlimited = int(ollama.get("num_predict", 512) or 512) < 0
        except Exception:
            inferred_unlimited = False
            ollama["num_predict"] = 512
        ollama.setdefault("unlimited_response", inferred_unlimited)
        ollama["stream_responses"] = True
        ollama.setdefault("think", False)
        ollama["num_thread"] = max(1, min(16, int(ollama.get("num_thread", 4) or 4)))
        ollama["num_batch"] = max(16, min(512, int(ollama.get("num_batch", 128) or 128)))
        try:
            ollama["num_gpu"] = int(ollama.get("num_gpu", -1))
        except Exception:
            ollama["num_gpu"] = -1
        for obsolete in ("tts_enabled", "tts_rate", "tts_volume", "tts_voice_contains", "silence_threshold"):
            voice.pop(obsolete, None)
        # v4.0.11 media architecture: Yandex and Discord are API/background-only.
        # These are safety/architecture invariants, not user-togglable fallbacks.
        integrations = serializable.setdefault("integrations", {})
        yandex = integrations.setdefault("yandex_music", {})
        # background_playback / background_bot_enabled are user preferences
        # (Settings checkboxes): keep an explicit False, default to True.
        yandex["background_playback"] = bool(yandex.get("background_playback", True))
        yandex["never_steal_input"] = True
        yandex["ui_fallback"] = False
        yandex["protocol_fallback"] = False
        discord = integrations.setdefault("discord", {})
        discord["background_bot_enabled"] = bool(discord.get("background_bot_enabled", True))
        discord["never_steal_input"] = True
        discord["ui_fallback"] = False
        discord["shared_music_queue_enabled"] = True
        discord.setdefault("music_control_channel", "neo-music")
        discord.setdefault("voice_connect_timeout_seconds", 6.0)
        serializable.setdefault("desktop", {})["auto_launch_music_app"] = False
        ui = serializable.setdefault("ui", {})
        ui.setdefault("robot_accessory_coffee", False)
        ui.setdefault("robot_accessory_cigarette", False)
        ui.setdefault("robot_accessory_mug_color", str(ui.get("robot_shell_color", "#DDE7F4") or "#DDE7F4"))
        ui.setdefault("robot_accessory_cigarette_color", str(ui.get("robot_trim_color", "#718198") or "#718198"))
        ui.setdefault("robot_accessory_hand_color", str(ui.get("robot_body_color", "#313C4F") or "#313C4F"))
        ui.pop("speak_responses", None)
        roots = serializable.get("security", {}).get("allowed_roots", [])
        serializable["security"]["allowed_roots"] = [self._collapse_root(x) for x in roots]
        discord = serializable.setdefault("integrations", {}).setdefault("discord", {})
        profiles_raw = discord.get("servers", []) or []
        if not isinstance(profiles_raw, list):
            profiles_raw = []
        profiles: list[dict[str, str]] = []
        seen_profiles: set[str] = set()
        for raw in profiles_raw:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "") or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen_profiles:
                continue
            seen_profiles.add(key)
            profiles.append({
                "name": name,
                "bot_token_encrypted": str(raw.get("bot_token_encrypted", "") or "").strip(),
                "guild_id": str(raw.get("guild_id", "") or "").strip(),
                "discord_user_id": str(raw.get("discord_user_id", "") or "").strip(),
                "voice_channel": str(raw.get("voice_channel", "") or "").strip(),
                "voice_channel_id": str(raw.get("voice_channel_id", "") or "").strip(),
                "music_text_channel": str(raw.get("music_text_channel", "") or "").strip(),
                "text_channel_id": str(raw.get("text_channel_id", "") or "").strip(),
                "music_bot_name": str(raw.get("music_bot_name", "") or "").strip(),
                "jarvis_bot_name": str(raw.get("jarvis_bot_name", "") or "").strip(),
            })
        default_server = str(discord.get("default_server", "") or "").strip()
        if profiles and not any(p["name"].casefold() == default_server.casefold() for p in profiles):
            default_server = profiles[0]["name"]
        discord["servers"] = profiles
        discord["default_server"] = default_server
        # NEO 182: a unique temporary file in the same folder, then an atomic replace.
        import tempfile
        handle, tmp_name = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=str(self.paths.settings.parent))
        os.close(handle)
        tmp = Path(tmp_name)
        new_autostart_enabled = bool(ui.get("start_with_windows", False))
        new_priority_enabled = bool(ui.get("priority_startup", False)) and new_autostart_enabled
        autostart_changed = (
            previous_autostart_enabled != new_autostart_enabled
            or previous_priority_enabled != new_priority_enabled
        )
        try:
            with tmp.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(serializable, ensure_ascii=False, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            snapshot_settings(self.paths.settings)
            os.replace(tmp, self.paths.settings)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        self.reload()
        return autostart_changed

    def _apply_saved_system_settings(self, autostart_changed: bool) -> None:
        # Native service helpers can take seconds. They must never hold the
        # cross-process profile lock after the disk transaction has committed.
        # HOTFIX107: the remote wake companion follows Telegram Remote rather
        # than the heavy JARVIS autostart switch. Saving Settings refreshes its
        # current archive target immediately and idempotently.
        self.apply_wake_service()
        # 186: the JSON is already committed here. A failing native effect is a
        # separate, partial outcome: it is reported (``native_apply_error186``)
        # and kept as a bounded pending retry of only that effect, instead of
        # surfacing as «settings were not saved».
        self.native_apply_error186 = ""
        if autostart_changed or getattr(self, "_autostart_retry186", False):
            try:
                self.apply_autostart()
                self._autostart_retry186 = False
            except Exception as exc:  # noqa: BLE001 - reported to the caller as a partial result
                from jarvis.runtime.diagnostics import safe_error
                self._autostart_retry186 = True
                self.native_apply_error186 = safe_error(exc)[:300]
                logging.getLogger(__name__).warning("Autostart could not be applied after the settings were saved", exc_info=True)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        value: Any = self.data
        for part in dotted_key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def active_wake_aliases(self) -> tuple[str, ...]:
        """Return a cached global+language-scoped wake alias tuple."""
        language = str(self.get("app.language", "ru") or "ru").strip().casefold()
        cached = self._active_wake_aliases_cache
        if cached is not None and cached[0] == language:
            return cached[1]
        values = list(self.get("wake_word.aliases", []) or [])
        scoped = self.get("wake_word.language_aliases", {}) or {}
        if isinstance(scoped, dict):
            # Some field-observed aliases are intentionally visible in the global
            # editable corpus (the user asked to see every observed spelling) but
            # remain safe only in a specific command language. Filter those
            # normalized forms out of runtime matching unless their language is
            # active, then append the active scoped entries below.
            scoped_owners: dict[str, set[str]] = {}
            for scoped_lang, scoped_values in scoped.items():
                if isinstance(scoped_values, str):
                    scoped_values = scoped_values.splitlines()
                for item in scoped_values or []:
                    raw_key = str(item or "").strip().rstrip(".!?").casefold()
                    key = "".join(ch for ch in raw_key if ch.isalnum())
                    if key:
                        scoped_owners.setdefault(key, set()).add(str(scoped_lang).casefold())
            filtered_values: list[str] = []
            for item in values:
                raw_key = str(item or "").strip().rstrip(".!?").casefold()
                key = "".join(ch for ch in raw_key if ch.isalnum())
                owners = scoped_owners.get(key)
                if owners and language not in owners:
                    continue
                filtered_values.append(item)
            values = filtered_values
            extra = scoped.get(language, []) or []
            if isinstance(extra, str):
                extra = extra.splitlines()
            values.extend(extra)
            # The exact matcher historically scans up to two lexical tokens. For
            # a requested three-token scoped alias such as "There it is", also
            # add a synthetic compact spelling. It has the same normalized value
            # but is classified as an exact-only observed alias, never fuzzy-safe.
            for item in extra:
                raw = str(item or "").strip().rstrip(".!")
                if len(raw.split()) >= 3:
                    compact = "".join(ch for ch in raw if ch.isalnum())
                    if compact:
                        values.append(compact)
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            value = str(item or "").strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                out.append(value)
        result = tuple(out)
        self._active_wake_aliases_cache = (language, result)
        return result


    def _migrate_settings(self) -> None:
        """Normalize settings created by the first project version."""
        version = int(self.data.get("settings_version", 1) or 1)
        if version < 165 and self.paths.settings.exists():
            backup=self.paths.backups / f"settings-before-v165-v{version}.json"
            if not backup.exists():
                shutil.copy2(self.paths.settings,backup)
        voice = self.data.setdefault("voice", {})
        if not isinstance(voice.get("input_device", ""), str):
            voice["input_device"] = ""
        for obsolete in ("tts_enabled", "tts_rate", "tts_volume", "tts_voice_contains", "silence_threshold"):
            voice.pop(obsolete, None)
        self.data.setdefault("ui", {}).pop("speak_responses", None)
        if version < 4:
            version = 4
        if version < 5:
            wake = self.data.setdefault("wake_word", {})
            wake["force_cpu"] = bool(wake.get("force_cpu", True))
            wake["cpu_threads"] = max(1, min(4, int(wake.get("cpu_threads", 2) or 2)))
            ollama = self.data.setdefault("ollama", {})
            if str(ollama.get("keep_alive", "0")) == "0":
                ollama["keep_alive"] = "10m"
            if int(ollama.get("num_ctx", 4096) or 4096) > 3072:
                ollama["num_ctx"] = 2048
            ollama.setdefault("num_predict", 192)
            ollama["stream_responses"] = True
            voice.setdefault("partial_stt_model", "tiny")
            voice["sensitivity"] = max(72, int(voice.get("sensitivity", 68) or 68))
            wake = self.data.setdefault("wake_word", {})
            wake.setdefault("overlap_seconds", 0.8)
            wake.setdefault("fuzzy_threshold", 0.68)
            version = 5
        if version < 6:
            # v0.5 stored PortAudio numeric indices. They are not stable between
            # processes and often point to a silent duplicate endpoint on Windows.
            # Return to the Windows/Qt default device unless the user had selected
            # a human-readable device name.
            selected = str(voice.get("input_device", "") or "")
            if selected.startswith("index:"):
                voice["input_device"] = ""
            voice.setdefault("audio_backend", "qt")
            voice["sensitivity"] = max(82, int(voice.get("sensitivity", 78) or 78))
            voice.setdefault("manual_always_transcribe", True)
            voice.setdefault("stt_cpu_threads", 4)
            voice["live_partial_seconds"] = min(0.6, float(voice.get("live_partial_seconds", 0.9) or 0.9))
            voice["transcription_timeout_seconds"] = min(75, int(voice.get("transcription_timeout_seconds", 90) or 90))
            ollama = self.data.setdefault("ollama", {})
            ollama.setdefault("first_token_timeout_seconds", 35)
            ollama["num_ctx"] = min(1536, int(ollama.get("num_ctx", 2048) or 2048))
            ollama["num_predict"] = min(128, int(ollama.get("num_predict", 192) or 192))
            if str(ollama.get("keep_alive", "10m")) in {"0", "1m", "5m"}:
                ollama["keep_alive"] = "15m"
            wake = self.data.setdefault("wake_word", {})
            wake["chunk_seconds"] = min(2.0, float(wake.get("chunk_seconds", 2.5) or 2.5))
            wake["overlap_seconds"] = min(0.8, float(wake.get("overlap_seconds", 0.8) or 0.8))
            version = 6
        if version < 7:
            ollama = self.data.setdefault("ollama", {})
            ollama["num_ctx"] = min(1024, int(ollama.get("num_ctx", 1536) or 1536))
            ollama["num_predict"] = min(96, int(ollama.get("num_predict", 128) or 128))
            ollama["first_token_timeout_seconds"] = min(20, float(ollama.get("first_token_timeout_seconds", 35) or 35))
            if str(ollama.get("keep_alive", "15m")) in {"0", "1m", "5m", "10m", "15m"}:
                ollama["keep_alive"] = "30m"
            voice = self.data.setdefault("voice", {})
            voice["live_partial_seconds"] = min(0.38, float(voice.get("live_partial_seconds", 0.6) or 0.6))
            voice["silence_seconds"] = min(0.78, float(voice.get("silence_seconds", 1.0) or 1.0))
            voice["start_timeout_seconds"] = min(8.0, float(voice.get("start_timeout_seconds", 12) or 12))
            voice.setdefault("use_live_result_as_final", True)
            wake = self.data.setdefault("wake_word", {})
            wake["chunk_seconds"] = min(1.7, float(wake.get("chunk_seconds", 2.0) or 2.0))
            wake.setdefault("scan_interval_seconds", 0.50)
            ui = self.data.setdefault("ui", {})
            ui.setdefault("sleep_after_seconds", 55)
            ui.setdefault("idle_animations", True)
            version = 7
        if version < 8:
            ollama = self.data.setdefault("ollama", {})
            # Ollama accepts the untagged model name and resolves its default tag.
            # Keep the user-facing/configured name exactly as "gemma4".
            for key in ("chat_model", "vision_model"):
                model = str(ollama.get(key, "gemma4") or "gemma4").strip()
                if model.casefold() == "gemma4:latest":
                    ollama[key] = "gemma4"
            ollama["num_ctx"] = min(1024, int(ollama.get("num_ctx", 1024) or 1024))
            ollama["num_predict"] = min(64, int(ollama.get("num_predict", 96) or 96))
            ollama["first_token_timeout_seconds"] = min(
                12, float(ollama.get("first_token_timeout_seconds", 20) or 20)
            )
            ollama["request_timeout_seconds"] = min(
                45, float(ollama.get("request_timeout_seconds", 60) or 60)
            )
            if str(ollama.get("keep_alive", "30m")) in {"0", "1m", "5m", "10m", "15m", "30m"}:
                ollama["keep_alive"] = "60m"
            voice = self.data.setdefault("voice", {})
            selected = str(voice.get("input_device", "") or "")
            if selected.startswith(("qt:", "index:")):
                voice["input_device"] = ""
            voice["audio_backend"] = "sounddevice_process"
            voice["live_partial_seconds"] = min(
                0.32, float(voice.get("live_partial_seconds", 0.38) or 0.38)
            )
            voice["silence_seconds"] = min(
                0.65, float(voice.get("silence_seconds", 0.72) or 0.72)
            )
            voice["start_timeout_seconds"] = min(
                6.0, float(voice.get("start_timeout_seconds", 7.0) or 7.0)
            )
            wake = self.data.setdefault("wake_word", {})
            wake["chunk_seconds"] = min(
                1.55, float(wake.get("chunk_seconds", 1.6) or 1.6)
            )
            wake["scan_interval_seconds"] = max(
                0.55, min(0.70, float(wake.get("scan_interval_seconds", 0.5) or 0.5))
            )
            version = 8
        if version < 9:
            # v0.9 removes multiprocessing.spawn for both microphone and Whisper.
            # This fixes WinError 5 / DuplicateHandle failures in PyCharm and on
            # systems with strict Windows process-handle policies.
            voice = self.data.setdefault("voice", {})
            voice["audio_backend"] = "sounddevice_thread"
            voice["stt_worker_backend"] = "thread"
            # Keep latency low without flooding the single shared recognizer.
            voice["live_partial_seconds"] = max(
                0.38, min(0.55, float(voice.get("live_partial_seconds", 0.42) or 0.42))
            )
            wake = self.data.setdefault("wake_word", {})
            wake["scan_interval_seconds"] = max(
                0.62, min(0.85, float(wake.get("scan_interval_seconds", 0.68) or 0.68))
            )
            version = 9
        if version < 10:
            # v0.9.1 consolidates all speech paths onto one faster-whisper tiny
            # instance and enables Gemma 4 thinking-stream UI support.
            version = 10
        if version < 11:
            # v0.9.2 always performs a high-quality final command pass and removes
            # the Liquid Glass/alpha settings that could make the window transparent.
            voice = self.data.setdefault("voice", {})
            voice["use_live_result_as_final"] = False
            voice["silence_seconds"] = max(
                0.9, float(voice.get("silence_seconds", 1.0) or 1.0)
            )
            voice["live_partial_seconds"] = max(
                0.60, float(voice.get("live_partial_seconds", 0.70) or 0.70)
            )
            wake = self.data.setdefault("wake_word", {})
            wake["chunk_seconds"] = max(
                2.4, float(wake.get("chunk_seconds", 2.6) or 2.6)
            )
            wake["scan_interval_seconds"] = min(
                0.55, float(wake.get("scan_interval_seconds", 0.45) or 0.45)
            )
            wake["fuzzy_threshold"] = min(
                0.60, float(wake.get("fuzzy_threshold", 0.58) or 0.58)
            )
            ui = self.data.setdefault("ui", {})
            for obsolete_ui in (
                "glass_mode",
                "background_opacity",
                "panel_opacity",
                "border_opacity",
                "transparent_window",
                "transparent_panels",
                "transparent_border",
            ):
                ui.pop(obsolete_ui, None)
            version = 11
        if version < 12:
            # v0.9.3 isolates native CTranslate2 in one restartable subprocess and
            # replaces overlapping wake windows with utterance-based segmentation.
            voice = self.data.setdefault("voice", {})
            voice["stt_worker_backend"] = "subprocess"
            voice["live_partial_seconds"] = max(
                1.8, float(voice.get("live_partial_seconds", 2.0) or 2.0)
            )
            voice["live_partial_max_updates"] = 1
            voice["transcription_timeout_seconds"] = max(
                60, float(voice.get("transcription_timeout_seconds", 70) or 70)
            )
            wake = self.data.setdefault("wake_word", {})
            wake["chunk_seconds"] = max(
                3.0, float(wake.get("chunk_seconds", 3.2) or 3.2)
            )
            wake["silence_seconds"] = max(
                0.45, min(0.9, float(wake.get("silence_seconds", 0.58) or 0.58))
            )
            wake.pop("scan_interval_seconds", None)
            wake["fuzzy_threshold"] = min(
                0.56, float(wake.get("fuzzy_threshold", 0.54) or 0.54)
            )
            ui = self.data.setdefault("ui", {})
            ui["chat_color"] = str(ui.get("panel_color", "#111722") or "#111722")
            version = 12
        if version < 13:
            # v0.9.4 moves PortAudio itself out of the Qt process. A native heap
            # corruption now only terminates the helper and triggers one safe-host
            # restart instead of closing the whole application.
            voice = self.data.setdefault("voice", {})
            voice["audio_backend"] = "sounddevice_subprocess_blocking"
            voice["audio_safe_host_fallback"] = True
            version = 13

        if version < 14:
            # v1.0 desktop: bilingual/code-switched speech, separate wake/command
            # recognizers, lower latency and a configurable local model.
            voice = self.data.setdefault("voice", {})
            voice.setdefault("language", "auto")
            if str(voice.get("stt_model", "tiny")) == "tiny":
                voice["stt_model"] = "small"
            voice.setdefault("partial_stt_model", "tiny")
            voice.setdefault("stt_device", "cpu")
            voice.setdefault("stt_compute_type", "int8")
            voice["silence_seconds"] = min(0.75, max(0.50, float(voice.get("silence_seconds", 0.62) or 0.62)))
            voice["live_partial_max_updates"] = 0
            voice.setdefault("post_answer_rearm_ms", 20)
            voice.setdefault("command_after_wake_same_utterance", True)
            wake = self.data.setdefault("wake_word", {})
            wake.setdefault("stt_model", "small")
            wake["silence_seconds"] = min(0.65, max(0.42, float(wake.get("silence_seconds", 0.50) or 0.50)))
            wake["fuzzy_threshold"] = max(0.64, float(wake.get("fuzzy_threshold", 0.64) or 0.64))
            ollama = self.data.setdefault("ollama", {})
            for key in ("chat_model", "vision_model"):
                legacy_model = str(ollama.get(key, "gemma4") or "gemma4").casefold()
                if legacy_model in {"gemma4", "gemma4:latest"}:
                    ollama[key] = "qwen3-vl:4b"
            # Previous builds enabled model reasoning by default, which was a major
            # source of latency for short desktop commands.
            ollama["think"] = False
            version = 14

        if version < 15:
            # v1.1 desktop reliability: Qwen3.5 is a newer unified multimodal/tool
            # model and behaves more consistently as a desktop agent than the old
            # qwen3-vl default. Keep custom user-selected models untouched.
            ollama = self.data.setdefault("ollama", {})
            for key in ("chat_model", "vision_model"):
                model = str(ollama.get(key, "") or "").casefold()
                if model in {"qwen3-vl:4b", "qwen3-vl"}:
                    ollama[key] = "qwen3.5:4b"
            ollama["think"] = False
            voice = self.data.setdefault("voice", {})
            voice["sensitivity"] = max(92, int(voice.get("sensitivity", 90) or 90))
            version = 15

        if version < 16:
            # RC3 could persist a PortAudio host-specific token that represented a
            # silent duplicate of the user's physical microphone. On the first RC4
            # start, return once to the Windows default endpoint. The old token is
            # kept for diagnostics and the user can still choose a device manually.
            voice = self.data.setdefault("voice", {})
            selected = str(voice.get("input_device", "") or "")
            if selected.startswith("sd:"):
                voice["previous_input_device"] = selected
                voice["input_device"] = ""
            voice["sensitivity"] = max(94, int(voice.get("sensitivity", 92) or 92))
            version = 16

        if version < 17:
            # RC5: old installations could still carry a 1024/2048 context from
            # early performance experiments, which is too small even for one
            # screenshot. Wake recognition also moves from tiny to base because
            # the extra accuracy matters much more than ~100-200 ms on a wake word.
            ollama = self.data.setdefault("ollama", {})
            ollama["num_ctx"] = max(4096, int(ollama.get("num_ctx", 4096) or 4096))
            ollama["vision_num_ctx"] = max(8192, int(ollama.get("vision_num_ctx", 8192) or 8192))
            ollama["think"] = False
            wake = self.data.setdefault("wake_word", {})
            if str(wake.get("stt_model", "tiny") or "tiny").casefold() == "tiny":
                wake["stt_model"] = "base"
            wake["chunk_seconds"] = min(3.2, max(2.2, float(wake.get("chunk_seconds", 2.6) or 2.6)))
            wake["silence_seconds"] = min(0.55, max(0.40, float(wake.get("silence_seconds", 0.42) or 0.42)))
            wake["fuzzy_threshold"] = min(0.64, max(0.54, float(wake.get("fuzzy_threshold", 0.58) or 0.58)))
            version = 17

        if version < 18:
            # RC6: stronger wake-word recall, friend/personality prompt, direct text
            # file editing and user-selectable UI transparency/font scaling.
            self.data.setdefault(
                "assistant_prompt",
                "Ты ассистент пользователя и его дружелюбный игровой напарник. Общайся естественно, тепло и по-дружески, как хороший знакомый; пользователь любит видеоигры и музыку. Не переигрывай роль и оставайся полезным помощником.",
            )
            security = self.data.setdefault("security", {})
            security.setdefault("auto_file_edits_without_confirmation", True)
            ui = self.data.setdefault("ui", {})
            ui.setdefault("font_scale_percent", 100)
            ui.setdefault("transparent_background", False)
            ui.setdefault("transparent_borders", False)
            wake = self.data.setdefault("wake_word", {})
            wake["stt_model"] = "small"
            wake["chunk_seconds"] = min(3.2, max(1.8, float(wake.get("chunk_seconds", 2.4) or 2.4)))
            wake["silence_seconds"] = min(0.60, max(0.28, float(wake.get("silence_seconds", 0.34) or 0.34)))
            wake["fuzzy_threshold"] = min(0.58, max(0.46, float(wake.get("fuzzy_threshold", 0.48) or 0.48)))
            voice = self.data.setdefault("voice", {})
            voice["sensitivity"] = max(96, int(voice.get("sensitivity", 94) or 94))
            version = 18

        if version < 19:
            # RC7: hybrid dedicated wake detector plus a more permissive short-name
            # ASR fallback. This migration targets installations where the mic meter
            # moves and dictation works but the isolated wake name is frequently lost.
            wake = self.data.setdefault("wake_word", {})
            wake["acoustic_enabled"] = False
            wake["acoustic_threshold"] = min(0.16, max(0.08, float(wake.get("acoustic_threshold", 0.12) or 0.12)))
            wake["stt_model"] = "base"
            wake["chunk_seconds"] = min(2.8, max(1.6, float(wake.get("chunk_seconds", 2.2) or 2.2)))
            wake["silence_seconds"] = min(0.42, max(0.22, float(wake.get("silence_seconds", 0.30) or 0.30)))
            wake["fuzzy_threshold"] = min(0.48, max(0.38, float(wake.get("fuzzy_threshold", 0.42) or 0.42)))
            version = 19

        if version < 20:
            # RC11 stable wake profile: keep the ONNX acoustic path opt-in and
            # re-arm almost immediately because the assistant never speaks aloud.
            wake = self.data.setdefault("wake_word", {})
            wake["acoustic_enabled"] = False
            voice = self.data.setdefault("voice", {})
            voice["post_answer_rearm_ms"] = 20
            version = 20

        if version < 21:
            # RC12 precision wake profile. RC11 biased Whisper with Jarvis hotwords
            # and a Jarvis-only initial prompt, which made unrelated speech decode
            # as the wake word. The code no longer uses that bias; migrate old
            # permissive matching thresholds to a precision-first value.
            wake = self.data.setdefault("wake_word", {})
            wake["acoustic_enabled"] = False
            wake["fuzzy_threshold"] = max(0.88, float(wake.get("fuzzy_threshold", 0.88) or 0.88))
            wake["stt_model"] = "base"
            version = 21

        if version < 23:
            # RC15: broaden explicit stop-session phrases without using a broad
            # fuzzy match that could swallow normal media/system commands.
            voice = self.data.setdefault("voice", {})
            existing = list(voice.get("stop_phrases", []) or [])
            defaults = [
                "стоп", "стап", "хватит", "перестань слушать", "перестань меня слушать",
                "не слушай", "заверши диалог", "закрой диалог", "закончи диалог",
                "заверши разговор", "закончи разговор", "жди wake word", "жди вейк ворд",
                "stop", "stop listening", "stop conversation", "end conversation",
            ]
            seen = {str(x).casefold().strip() for x in existing}
            for item in defaults:
                if item.casefold() not in seen:
                    existing.append(item)
                    seen.add(item.casefold())
            voice["stop_phrases"] = existing
            version = 23

        if version < 29:
            gaming = self.data.setdefault("gaming", {})
            gaming["telemetry_refresh_seconds"] = 1.0
            version = 29

        if version < 33:
            # v3.2 retires the experimental yandexmusic:// My Wave deep-link as a
            # default. Current desktop builds may reject it with “Такой страницы
            # нет”, so existing profiles are migrated to the verified UI route.
            integrations = self.data.setdefault("integrations", {})
            integrations.setdefault("yandex_music", {})["protocol_fallback"] = False
            version = 33

        if version < 34:
            # v3.3 is a routing/reliability migration. No user preferences need
            # to be overwritten; bump the schema so repaired panel/Discord/Yandex
            # behaviour is distinguishable in diagnostics.
            version = 34

        if version < 35:
            # v3.4 repairs native interaction routing (Yandex search, Discord,
            # panel names, known folders and verified media seek). No persisted
            # user preference needs to be changed; this is a diagnostic schema bump.
            version = 35

        if version < 36:
            # v3.5 restores tolerant native navigation while keeping exact target
            # verification: two-stage Yandex Search, exact Tracks navigation,
            # Discord slash-picker state transitions and safer folder resolution.
            # No user preference is overwritten.
            version = 36

        # v3.9.2 migration must run before the stable voice invariants. Preserve
        # the legacy interaction choice before defaults are normalized; v112 later
        # intentionally migrates the product default to wake_each_command.
        if version < 39:
            legacy_voice = self.data.setdefault("voice", {})
            legacy_voice["interaction_mode"] = (
                "continuous" if bool(legacy_voice.get("continuous_after_wake", True))
                else "wake_each_command"
            )

        # Stable invariants shared by new and migrated settings.
        voice = self.data.setdefault("voice", {})
        voice.setdefault("language", "auto")
        voice.setdefault("stt_model", "small")
        voice.setdefault("partial_stt_model", "tiny")
        voice.setdefault("stt_device", "cpu")
        voice.setdefault("stt_compute_type", "int8")
        voice["use_live_result_as_final"] = False
        voice["stt_worker_backend"] = "subprocess"
        voice["audio_backend"] = "sounddevice_subprocess_blocking"
        voice["audio_safe_host_fallback"] = True
        from jarvis.runtime.numeric_schema186 import clamp as _clamp186
        voice["silence_seconds"] = _clamp186("voice.silence_seconds", voice.get("silence_seconds", 0.62) or 0.62)
        voice["live_partial_max_updates"] = max(0, min(2, int(voice.get("live_partial_max_updates", 0) or 0)))
        voice["post_answer_rearm_ms"] = max(0, min(500, int(voice.get("post_answer_rearm_ms", 20) or 0)))
        wake = self.data.setdefault("wake_word", {})
        packaged_wake = self._read_json(self.default_settings_path).get("wake_word", {}) or {}
        default_phrase = str(packaged_wake.get("phrase", "") or "").strip()
        wake.setdefault("stt_model", "base")
        phrase = str(wake.get("phrase", default_phrase) or "").strip()
        if len("".join(ch for ch in phrase if ch.isalpha())) < 3:
            wake["phrase"] = default_phrase
        wake["chunk_seconds"] = _clamp186("wake_word.chunk_seconds", wake.get("chunk_seconds", 0.95) or 0.95)
        wake["silence_seconds"] = _clamp186("wake_word.silence_seconds", wake.get("silence_seconds", 0.30) or 0.30)
        wake["fuzzy_threshold"] = max(0.82, min(0.96, float(wake.get("fuzzy_threshold", 0.88) or 0.88)))
        wake["acoustic_enabled"] = bool(wake.get("acoustic_enabled", False))
        wake["acoustic_threshold"] = max(0.05, min(0.50, float(wake.get("acoustic_threshold", 0.12) or 0.12)))
        wake.pop("scan_interval_seconds", None)
        ui = self.data.setdefault("ui", {})
        for obsolete_ui in (
            "glass_mode", "background_opacity", "panel_opacity", "border_opacity",
            "transparent_window", "transparent_panels", "transparent_border",
        ):
            ui.pop(obsolete_ui, None)
        self.data.setdefault(
            "assistant_prompt",
            "Ты локальный Windows-ассистент пользователя. Общайся естественно, тепло и по-дружески, но в первую очередь будь быстрым, точным и надёжным оператором компьютера. Игровой режим используй только как простой трекер сессии; заметки и чек-листы универсальны.",
        )
        security = self.data.setdefault("security", {})
        security.setdefault("auto_file_edits_without_confirmation", True)
        security.setdefault("full_user_file_access", True)
        security.setdefault("recycle_delete_without_confirmation", True)
        # RC13: ordinary file/desktop operations are direct voice controls by default.
        # NEO 182: only a default — an explicit «confirm ordinary file changes» choice is kept.
        security.setdefault("confirm_medium_risk", False)
        voice.setdefault("continuous_after_wake", False)
        voice.setdefault("interaction_mode", "continuous" if bool(voice.get("continuous_after_wake", False)) else "wake_each_command")
        if str(voice.get("interaction_mode", "") or "").strip() not in {"continuous", "wake_each_command"}:
            voice["interaction_mode"] = "continuous" if bool(voice.get("continuous_after_wake", False)) else "wake_each_command"
        voice["continuous_after_wake"] = voice["interaction_mode"] == "continuous"
        voice.setdefault("continuous_rearm_ms", 35)
        voice.setdefault("command_min_voice_ms", 85)
        voice.setdefault("ignore_nonlinguistic_noise", True)
        voice.setdefault("command_fuzzy_repair", True)
        voice.setdefault("command_variation_repair", True)
        voice.setdefault("stop_phrases", list((self._read_json(self.default_settings_path).get("voice", {}) or {}).get("stop_phrases", []) or []))
        desktop = self.data.setdefault("desktop", {})
        desktop.setdefault("preferred_browser", "firefox")
        desktop.setdefault("preferred_music_app", "yandex_music")
        desktop.setdefault("auto_launch_music_app", True)
        desktop.setdefault("screen_aware_actions", True)
        desktop.setdefault("vision_for_ambiguous_actions", True)
        desktop.setdefault("browser_tab_timeout_seconds", 1.5)
        overlay = desktop.setdefault("overlay", {})
        overlay.setdefault("enabled", True)
        overlay.setdefault("font_family", "Segoe UI")
        overlay.setdefault("font_size", 18)
        overlay.setdefault("text_color", "#F2F6FF")
        overlay.setdefault("background_color", "#0A0F18")
        overlay.setdefault("border_color", "#57D7FF")
        overlay.setdefault("opacity", 72)
        overlay.setdefault("width", 420)
        overlay.setdefault("monitor", 1)
        overlay.setdefault("position", "top_left")
        overlay.setdefault("click_through", True)
        overlay.setdefault("height", 0)
        panel_styles = overlay.setdefault("styles", {})
        base_panel_style = {
            "font_family": overlay.get("font_family", "Segoe UI"),
            "font_size": overlay.get("font_size", 18),
            "text_color": overlay.get("text_color", "#F2F6FF"),
            "background_color": overlay.get("background_color", "#0A0F18"),
            "border_color": overlay.get("border_color", "#57D7FF"),
            "opacity": overlay.get("opacity", 72),
            "width": overlay.get("width", 420),
            "height": overlay.get("height", 0),
            "click_through": overlay.get("click_through", True),
        }
        for panel_type in ("session", "checklist", "notes"):
            current_style = panel_styles.setdefault(panel_type, {})
            for key, value in base_panel_style.items():
                current_style.setdefault(key, value)
        integrations = self.data.setdefault("integrations", {})
        yandex_integration = integrations.setdefault("yandex_music", {})
        yandex_integration.setdefault("enabled", True)
        yandex_integration.setdefault("api_enabled", True)
        yandex_integration.setdefault("background_playback", True)
        yandex_integration.setdefault("never_steal_input", True)
        yandex_integration.setdefault("ui_fallback", False)
        yandex_integration.setdefault("protocol_fallback", False)
        yandex_integration.setdefault("token_encrypted", "")
        yandex_integration.setdefault("background_volume_percent", 100)
        yandex_integration.setdefault("shuffle_enabled", False)
        music_integration = integrations.setdefault("music", {})
        music_integration.setdefault("active_provider", "")
        spotify_integration = integrations.setdefault("spotify", {})
        spotify_integration.setdefault("enabled", False)
        spotify_integration.setdefault("client_id_encrypted", "")
        spotify_integration.setdefault("access_token_encrypted", "")
        spotify_integration.setdefault("refresh_token_encrypted", "")
        spotify_integration.setdefault("token_expires_at", 0.0)
        spotify_integration.setdefault("redirect_uri", "http://127.0.0.1:8765/callback")
        youtube_integration = integrations.setdefault("youtube", {})
        # Existing NEO profiles already had browser YouTube controls. Keep that
        # feature enabled through migration; only API search needs a key.
        youtube_integration.setdefault("enabled", True)
        youtube_integration.setdefault("api_key_encrypted", "")
        integrations.pop("apple_music", None)  # HOTFIX100: Apple Music removed
        discord_integration = integrations.setdefault("discord", {})
        discord_integration.setdefault("enabled", True)
        discord_integration.setdefault("default_server", "")
        discord_integration.setdefault("music_text_channel", "")
        discord_integration.setdefault("default_voice_channel", "")
        discord_integration.setdefault("music_bot_name", "")
        discord_integration.setdefault("music_bot_mode", "slash")
        discord_integration.setdefault("music_play_template", "/play {query}")
        discord_integration.setdefault("music_query_option", "query")
        discord_integration.setdefault("verify_actions", True)
        discord_integration.setdefault("background_bot_enabled", True)
        discord_integration.setdefault("auto_follow_user_voice", True)
        discord_integration.setdefault("never_steal_input", True)
        discord_integration.setdefault("ui_fallback", False)
        discord_integration.setdefault("bot_token_encrypted", "")
        discord_integration.setdefault("ffmpeg_path", "")
        discord_integration.setdefault("servers", [])
        discord_integration.setdefault("music_shuffle_enabled", False)
        gmail = integrations.setdefault("gmail", {})
        gmail.setdefault("enabled", False)
        gmail.setdefault("client_id_encrypted", "")
        gmail.setdefault("client_secret_encrypted", "")
        gmail.setdefault("refresh_token_encrypted", "")
        gmail.setdefault("access_token_encrypted", "")
        gmail.setdefault("token_expires_at", 0.0)
        gmail.setdefault("account_email", "")
        gmail.setdefault("notifications_enabled", True)
        gmail.setdefault("include_inbox", True)
        gmail.setdefault("additional_folders", [])
        gmail.setdefault("notify_total_count", False)
        gmail.setdefault("notify_unread_count", True)
        gmail.setdefault("notify_recent24h_count", True)
        gmail.setdefault("message_display_detail", gmail.get("notification_detail", "sender_subject"))
        gmail.setdefault("include_in_briefing", True)
        gmail.setdefault("briefing_show_senders", False)
        gmail.setdefault("briefing_show_folders", True)
        gmail.setdefault("poll_seconds", 60)
        gmail.setdefault("timeout_seconds", 10.0)
        mailru = integrations.setdefault("mailru", {})
        mailru.setdefault("enabled", False)
        mailru.setdefault("email", "")
        mailru.setdefault("app_password_encrypted", "")
        mailru.setdefault("notifications_enabled", True)
        mailru.setdefault("include_inbox", True)
        mailru.setdefault("additional_folders", [])
        mailru.setdefault("notify_total_count", False)
        mailru.setdefault("notify_unread_count", True)
        mailru.setdefault("notify_recent24h_count", True)
        mailru.setdefault("message_display_detail", mailru.get("notification_detail", "sender_subject"))
        mailru.setdefault("include_in_briefing", True)
        mailru.setdefault("briefing_show_senders", False)
        mailru.setdefault("briefing_show_folders", True)
        mailru.setdefault("poll_seconds", 60)
        mailru.setdefault("timeout_seconds", 10.0)
        telegram = integrations.setdefault("telegram", {})
        telegram.setdefault("remote_control_enabled", bool(telegram.get("enabled", False)))
        telegram.setdefault("messages_enabled", False)
        telegram.setdefault("messages_notifications_enabled", True)
        telegram.setdefault("messages_notification_detail", "sender_text")
        telegram.setdefault("include_in_briefing", False)
        telegram.setdefault("briefing_show_recent24h", True)
        telegram.setdefault("briefing_show_total", False)
        telegram.setdefault("briefing_show_senders", False)
        telegram.setdefault("query_show_senders", True)
        telegram.setdefault("messages_history_limit", 5000)
        telegram.setdefault("outbound_allowed_people", [])
        telegram.setdefault("outbound_allowed_groups", [])
        # HOTFIX96 removed the paid cloud translation provider. Translation remains local/Ollama.
        integrations.pop("google_translate", None)
        google_calendar = integrations.setdefault("google_calendar", {})
        google_calendar.setdefault("enabled", False)
        google_calendar.setdefault("refresh_token_encrypted", "")
        google_calendar.setdefault("access_token_encrypted", "")
        google_calendar.setdefault("token_expires_at", 0.0)
        google_calendar.setdefault("timeout_seconds", 12.0)
        google_calendar.setdefault("include_in_briefing", False)
        google_calendar.setdefault("briefing_period", "today")
        gaming = self.data.setdefault("gaming", {})
        gaming.setdefault("enabled", True)
        gaming.setdefault("auto_detect", True)
        gaming.setdefault("initial_launch_wait_seconds", 8)
        gaming.setdefault("launch_wait_seconds", 240)
        gaming.setdefault("followup_wait_seconds", 45)
        gaming.setdefault("launch_context_seconds", 1200)
        gaming.setdefault("telemetry_refresh_seconds", 1.0)
        gaming.setdefault("history_touch_seconds", 30.0)
        gaming.setdefault("overlay_clock_refresh_ms", 1000)
        # HOTFIX37 fresh/missing-key defaults for the compact assistant window.
        # setdefault preserves deliberate user customisation; migration 82 below
        # only converts the old packaged baseline values.
        ui.setdefault("window_width", 322)
        ui.setdefault("animation_fps", 60)
        ui.setdefault("window_height", 476)
        ui.setdefault("drag_enabled", True)
        ui.setdefault("always_on_top", False)
        ui.setdefault("start_with_windows", False)
        ui.setdefault("show_tool_details", False)
        ui.setdefault("robot_only_mode", False)
        ui.setdefault("idle_animations", True)
        ui.setdefault("sleep_after_seconds", 20)
        ui.setdefault("typewriter_interval_ms", 8)
        ui.setdefault("response_stream_chunk_chars", 18)
        ui.setdefault("response_stream_hard_max_chars", 48)
        ui.setdefault("response_stream_flush_ms", 33)
        ui.setdefault("corner_radius", 26)
        ui.setdefault("font_scale_percent", 100)  # legacy compatibility mirror
        ui.setdefault("chat_font_size_px", 14)
        ui.setdefault("robot_moon_color", "#DDE7F4")
        ui.setdefault("robot_cloud_color", "#DCE7F3")
        ui.setdefault("robot_rain_color", "#91CFFF")
        ui.setdefault("robot_starfall_color", "#EAF6FF")
        ui.setdefault("robot_starry_sky_color", ui.get("robot_starfall_color", "#EAF6FF"))
        ui.setdefault("robot_snow_color", "#F6FAFF")
        ui.setdefault("transparent_background", False)
        ui.setdefault("transparent_borders", False)
        hotkeys_runtime = self.data.setdefault("hotkeys", {})
        hotkeys_runtime.setdefault("app_microphone_toggle", "Ctrl+Alt+M")
        hotkeys_runtime.setdefault("autoclicker_toggle", "Ctrl+Alt+C")
        hotkeys_runtime.setdefault("chat_scroll_up", "Ctrl+Alt+Up")
        hotkeys_runtime.setdefault("chat_scroll_down", "Ctrl+Alt+Down")
        hotkeys_runtime.setdefault("window_topmost_toggle", "Ctrl+Alt+T")
        ollama = self.data.setdefault("ollama", {})
        ollama.setdefault("chat_model", "qwen2.5:0.5b")
        ollama.setdefault("vision_model", "qwen3.5:4b")
        ollama["num_ctx"] = max(4096, int(ollama.get("num_ctx", 4096) or 4096))
        ollama["vision_num_ctx"] = max(8192, int(ollama.get("vision_num_ctx", 8192) or 8192))
        ollama.setdefault("num_predict", 320)
        ollama["stream_responses"] = True
        ollama.setdefault("think", False)
        ollama.setdefault("auto_install", True)
        ollama.setdefault("auto_start", True)
        ollama.setdefault("start_tray_app", True)
        ollama.setdefault("auto_pull_models", True)
        ollama.setdefault("startup_timeout_seconds", 20)
        ollama.setdefault("model_pull_timeout_seconds", 7200)
        if version < 38:
            packaged = self._read_json(self.default_settings_path)
            wake_defaults = packaged.get("wake_word", {}) or {}
            wake = self.data.setdefault("wake_word", {})
            wake.setdefault("aliases", list(wake_defaults.get("aliases", []) or []))
            wake.setdefault("acoustic_model_phrase", str(wake_defaults.get("acoustic_model_phrase", "") or ""))
            # Older schemas stored wake-name combinations inside stop_phrases.
            # v38 stops generating them; stop+assistant is matched dynamically
            # from the live wake aliases. Keep user values but add generic defaults.
            generic_stops = list((packaged.get("voice", {}) or {}).get("stop_phrases", []) or [])
            existing_stops = list(self.data.setdefault("voice", {}).get("stop_phrases", []) or [])
            seen = {str(x).casefold().strip() for x in existing_stops}
            for item in generic_stops:
                if str(item).casefold().strip() not in seen:
                    existing_stops.append(item)
            self.data["voice"]["stop_phrases"] = existing_stops
            version = 38
        if version < 39:
            # v3.9.2: explicit voice-interaction mode and per-server Discord profiles.
            voice = self.data.setdefault("voice", {})
            voice["interaction_mode"] = "continuous" if bool(voice.get("continuous_after_wake", False)) else "wake_each_command"
            discord = self.data.setdefault("integrations", {}).setdefault("discord", {})
            profiles = discord.get("servers", []) or []
            if not isinstance(profiles, list):
                profiles = []
            if not profiles:
                legacy_name = str(discord.get("default_server", "") or "").strip()
                legacy_voice = str(discord.get("default_voice_channel", "") or "").strip()
                legacy_music = str(discord.get("music_text_channel", "") or "").strip()
                legacy_bot = str(discord.get("music_bot_name", "") or "").strip()
                if any((legacy_name, legacy_voice, legacy_music, legacy_bot)):
                    profiles = [{
                        "name": legacy_name,
                        "voice_channel": legacy_voice,
                        "music_text_channel": legacy_music,
                        "music_bot_name": legacy_bot,
                    }]
            discord["servers"] = profiles
            version = 39
        if version < 40:
            # v3.9.7: harden continuous command capture against room-noise STT
            # hallucinations. Preserve intentionally customized higher values, but
            # upgrade the old 85 ms default that could be triggered by fans/clicks.
            voice = self.data.setdefault("voice", {})
            try:
                old_min_voice = int(voice.get("command_min_voice_ms", 85) or 85)
            except Exception:
                old_min_voice = 85
            if old_min_voice <= 100:
                voice["command_min_voice_ms"] = 150
            version = 40
        if version < 50:
            # v4.0: additive architecture settings.  These defaults intentionally
            # preserve all v3.10 desktop/media/Discord behavior unless the user
            # explicitly enables an optional feature.
            voice = self.data.setdefault("voice", {})
            voice.setdefault("interrupt_on_wake_while_busy", True)
            voice.setdefault("interrupt_without_wake", True)
            voice.setdefault("interrupt_phrases", ["стоп", "отмена", "отмени", "не надо", "хватит", "stop", "cancel"])
            voice.setdefault("duck_system_while_listening", False)
            voice.setdefault("listening_duck_percent", 18)
            self.data.setdefault("routing", {}).update({
                k: self.data.get("routing", {}).get(k, v) for k, v in {
                    "hybrid_enabled": True, "offline_first": True, "llm_only_when_ambiguous": True,
                    "intent_priority": ["interrupt", "media_state", "window_move", "panel", "filesystem", "search", "application"],
                }.items()
            })
            self.data.setdefault("personal_dictionary", {}).setdefault("enabled", True)
            self.data.setdefault("personal_dictionary", {}).setdefault("entries", [])
            self.data.setdefault("personal_dictionary", {}).setdefault("safe_match_threshold", 0.88)
            dev = self.data.setdefault("developer_mode", {})
            for k, v in {
                "enabled": False, "settings_enabled": False, "always_on_top": True, "show_raw_speech": True, "show_normalized": True,
                "show_intent": True, "show_entities": True, "show_tool": True, "show_fallback": True,
                "show_latency": True, "show_result": True, "slow_threshold_seconds": 2.0,
            }.items(): dev.setdefault(k, v)
            self.data.setdefault("scenarios", {}).setdefault("items", [])
            self.data.setdefault("scenarios", {}).setdefault("confirm_dangerous_steps", True)
            self.data.setdefault("plugins", {}).setdefault("enabled", True)
            self.data.setdefault("plugins", {}).setdefault("directory", "plugins")
            self.data.setdefault("startup_self_test", {}).setdefault("enabled", True)
            self.data.setdefault("startup_self_test", {}).setdefault("show_only_on_failure", True)
            self.data.setdefault("startup_self_test", {}).setdefault("quick", True)
            self.data.setdefault("crash_recovery", {}).setdefault("enabled", True)
            self.data.setdefault("crash_recovery", {}).setdefault("recent_action_limit", 100)
            self.data.setdefault("automation", {}).setdefault("parallel_enabled", True)
            self.data.setdefault("automation", {}).setdefault("max_parallel", 4)
            self.data.setdefault("automation", {}).setdefault("max_queue", 12)
            self.data.setdefault("history", {}).setdefault("undo_enabled", True)
            self.data.setdefault("history", {}).setdefault("max_actions", 100)
            self.data.setdefault("email", {}).setdefault("default_provider", "mailru")
            self.data.setdefault("screen", {}).setdefault("vision_fallback", True)
            self.data.setdefault("screen", {}).setdefault("game_adviser", True)
            self.data.setdefault("safety", {}).setdefault("dangerous_preview", True)
            self.data.setdefault("safety", {}).setdefault("mass_file_threshold", 25)
            self.data.setdefault("audio_control", {}).setdefault("session_restore_memory", True)
            self.data.setdefault("audio_control", {}).setdefault("device_switch_backend", "auto")
            version = 50
        if version < 51:
            routing = self.data.setdefault("routing", {})
            routing.setdefault("semantic_firewall_enabled", True)
            routing.setdefault("fuzzy_command_repair", True)
            routing.setdefault("compound_before_single_intent", True)
            routing.setdefault("explicit_domain_wins_over_context", True)
            routing.setdefault("command_corpus_gate", True)
            version = 51
        if version < 52:
            integrations = self.data.setdefault("integrations", {})
            yandex = integrations.setdefault("yandex_music", {})
            yandex.setdefault("background_playback", True)
            yandex.setdefault("never_steal_input", True)
            discord = integrations.setdefault("discord", {})
            discord.setdefault("background_bot_enabled", True)
            discord.setdefault("never_steal_input", True)
            discord.setdefault("ui_fallback", False)
            discord.setdefault("bot_token_encrypted", "")
            discord.setdefault("ffmpeg_path", "")
            # IDs are additive; existing name-based server profiles stay valid.
            version = 53
        if version < 54:
            integrations = self.data.setdefault("integrations", {})
            yandex = integrations.setdefault("yandex_music", {})
            yandex.setdefault("background_volume_percent", 100)
            discord = integrations.setdefault("discord", {})
            discord.setdefault("auto_follow_user_voice", True)
            profiles = discord.get("servers", []) or []
            if isinstance(profiles, list):
                for profile in profiles:
                    if isinstance(profile, dict):
                        profile.setdefault("discord_user_id", "")
            version = 54
        if version < 55:
            # v4.0.8 moves the official BOT token from one global setting into
            # each Discord server profile.  Migrate the old token to the selected
            # default profile so existing v4.0.7 installations keep working.
            discord = self.data.setdefault("integrations", {}).setdefault("discord", {})
            legacy_token = str(discord.get("bot_token_encrypted", "") or "").strip()
            default_server = str(discord.get("default_server", "") or "").casefold().strip()
            profiles = discord.get("servers", []) or []
            if isinstance(profiles, list):
                selected = None
                for profile in profiles:
                    if not isinstance(profile, dict):
                        continue
                    profile.setdefault("bot_token_encrypted", "")
                    if default_server and str(profile.get("name", "") or "").casefold().strip() == default_server:
                        selected = profile
                if selected is None and len(profiles) == 1 and isinstance(profiles[0], dict):
                    selected = profiles[0]
                if selected is not None and legacy_token and not str(selected.get("bot_token_encrypted", "") or "").strip():
                    selected["bot_token_encrypted"] = legacy_token
                if legacy_token and any(
                    isinstance(profile, dict) and str(profile.get("bot_token_encrypted", "") or "").strip()
                    for profile in profiles
                ):
                    # Once migration succeeded, remove the duplicate global copy
                    # so changing the default row later cannot accidentally copy
                    # one server's token into another server.
                    discord["bot_token_encrypted"] = ""
            version = 55
        if version < 56:
            # v4.0.11 permanently removes desktop/UI fallbacks from Yandex Music
            # and Discord integrations. YouTube remains the only media domain that
            # may interact with a visible browser tab.
            voice = self.data.setdefault("voice", {})
            voice.setdefault("command_language", "ru")
            integrations = self.data.setdefault("integrations", {})
            yandex = integrations.setdefault("yandex_music", {})
            yandex.setdefault("background_playback", True)
            yandex["never_steal_input"] = True
            yandex["ui_fallback"] = False
            yandex["protocol_fallback"] = False
            discord = integrations.setdefault("discord", {})
            discord.setdefault("background_bot_enabled", True)
            discord["never_steal_input"] = True
            discord["ui_fallback"] = False
            self.data.setdefault("desktop", {})["auto_launch_music_app"] = False
            version = 56
        if version < 57:
            # v4.0.15 visual-only migration. Preserve every existing custom color,
            # but introduce independent Window/Robot preset selectors and true
            # alpha controls. The main-window geometry migration is applied once
            # by MainWindow because it also respects the user's current size.
            ui = self.data.setdefault("ui", {})
            neo_window = {
                "accent": "#57D7FF", "window_color": "#0C1017", "panel_color": "#111722",
                "border_color": "#57D7FF", "text_color": "#EAF2FF", "muted_text_color": "#8F9DB2",
                "element_color": "#192333", "element_hover_color": "#243247", "chat_color": "#111722",
                "chat_user_color": "#23465A", "chat_assistant_color": "#182232",
            }
            neo_robot = {
                "robot_body_color": "#313C4F", "robot_shell_color": "#DDE7F4", "robot_screen_color": "#101721",
                "robot_eye_color": "#57D7FF", "robot_trim_color": "#718198", "robot_glow_color": "#57D7FF",
            }
            ui["window_theme_preset"] = "NEO Cyan" if all(str(ui.get(k, v)).upper() == v.upper() for k, v in neo_window.items()) else "Custom"
            ui["robot_theme_preset"] = "NEO Cyan" if all(str(ui.get(k, v)).upper() == v.upper() for k, v in neo_robot.items()) else "Custom"
            ui["window_opacity_percent"] = 0 if bool(ui.get("transparent_background", False)) else int(ui.get("window_opacity_percent", 100) or 100)
            ui.setdefault("panel_opacity_percent", 100)
            ui.setdefault("element_opacity_percent", 100)
            ui.setdefault("chat_opacity_percent", 100)
            ui.setdefault("bubble_opacity_percent", 100)
            ui["border_opacity_percent"] = 0 if bool(ui.get("transparent_borders", False)) else int(ui.get("border_opacity_percent", 100) or 100)
            ui.setdefault("show_robot", True)
            dev_mode = self.data.setdefault("developer_mode", {})
            dev_mode.setdefault("settings_enabled", False)
            if not bool(dev_mode.get("settings_enabled", False)):
                dev_mode["enabled"] = False
            version = 57
        if version < 58:
            # v4.0.19: shared Discord queue/source handoff and configurable acoustic
            # rejection. Expand only observed wake-ASR variants; the acoustic model
            # remains opt-in to avoid trading false rejects for false activations.
            voice = self.data.setdefault("voice", {})
            voice.setdefault("noise_rejection_percent", 55)
            discord = self.data.setdefault("integrations", {}).setdefault("discord", {})
            discord["shared_music_queue_enabled"] = True
            wake = self.data.setdefault("wake_word", {})
            aliases = list(wake.get("aliases", []) or [])
            seen = {str(item or "").casefold().strip() for item in aliases}
            for item in (
                "jarwis", "jarwies", "jarwiec", "jarbiaz", "czerwios", "czerwis",
                "charvis", "derivis", "jarvies", "jarwice", "jarwiez", "jarmias", "jarewis", "jariviz", "чарвец", "чарвес", "дарый вис", "джар вниз", "джарбис", "джорвис", "джарриц", "javise", "джарвиз",
            ):
                if item.casefold() not in seen:
                    aliases.append(item)
                    seen.add(item.casefold())
            wake["aliases"] = aliases[:80]
            version = 58
        if version < 59:
            # v4.0.20: wake reliability + low-power defaults and robot-only window mode.
            # Preserve explicit heavier wake models, but migrate the old packaged Base
            # default to Tiny and compensate with exact observed ASR aliases.
            wake = self.data.setdefault("wake_word", {})
            if str(wake.get("stt_model", "base") or "base").strip().casefold() == "base":
                wake["stt_model"] = "tiny"
            wake.setdefault("cpu_threads", 2)
            aliases = list(wake.get("aliases", []) or [])
            seen = {"".join(ch for ch in str(item or "").casefold() if ch.isalnum()) for item in aliases}
            for item in (
                "джарви", "jarvi", "jarv", "djarvis", "djarwis", "zarvis", "zarwis", "czarvis",
                "jarius", "jarmiu", "narwis", "uderwies", "czarom jest", "jarviss",
            ):
                key = "".join(ch for ch in item.casefold() if ch.isalnum())
                if key and key not in seen:
                    aliases.append(item)
                    seen.add(key)
            wake["aliases"] = aliases[:80]
            self.data.setdefault("ui", {}).setdefault("robot_only_mode", False)
            old_packaged_prompts = {
                "Ты локальный Windows-ассистент пользователя. Общайся естественно, тепло и по-дружески, но в первую очередь будь быстрым, точным и надёжным оператором компьютера. Помогай голосом управлять приложениями, окнами, файлами, браузером, медиа, заметками и чек-листами. Игры можно обсуждать как обычную тему, а игровой режим используется только для простого трекера сессии. Не будь навязчивым и не утверждай об успехе действия, пока оно не подтверждено.",
                "Ты ассистент пользователя и его дружелюбный игровой напарник. Общайся естественно, тепло и по-дружески, как хороший знакомый; пользователь любит видеоигры и музыку. Не переигрывай роль и оставайся полезным помощником.",
            }
            current_prompt = str(self.data.get("assistant_prompt", "") or "").strip()
            if not current_prompt or current_prompt in old_packaged_prompts:
                self.data["assistant_prompt"] = (
                    "Ты десктопный ассистент и друг пользователя. Помогай с компьютером, но общайся естественно и по-дружески: "
                    "можно обсуждать игры, музыку, планы, как прошёл день, чем пользователь занимался, как у него дела, или просто разговаривать. "
                    "Будь спокойным, ненавязчивым и полезным."
                )
            version = 59
        if version < 60:
            # v4.0.20 HOTFIX2: preserve quiet final words, expand observed wake-ASR
            # spellings, and keep explicit dotted aliases requested by the user.
            voice = self.data.setdefault("voice", {})
            try:
                current_silence = float(voice.get("silence_seconds", 0.62) or 0.62)
            except (TypeError, ValueError):
                current_silence = 0.62
            # Only migrate the old fast default. Respect users who already chose a
            # longer/custom endpoint pause.
            if current_silence <= 0.70:
                voice["silence_seconds"] = 1.0

            wake = self.data.setdefault("wake_word", {})
            aliases_raw = wake.get("aliases", []) or []
            if isinstance(aliases_raw, str):
                aliases_raw = aliases_raw.splitlines()
            aliases = [str(item or "").strip() for item in aliases_raw if str(item or "").strip()]
            seen_exact = {item.casefold() for item in aliases}
            for item in ("jervies", "джарвез", "джарбиз", "daravés", "jarłys", "gior visa", "дармез"):
                if item.casefold() not in seen_exact:
                    aliases.append(item)
                    seen_exact.add(item.casefold())
            for item in list(aliases):
                dotted = item if item.endswith(".") else item + "."
                if dotted.casefold() not in seen_exact:
                    aliases.append(dotted)
                    seen_exact.add(dotted.casefold())
            wake["aliases"] = aliases[:900]
            version = 60
        if version < 61:
            # v4.0.20 HOTFIX3: short media commands can be recognized by the same
            # low-power wake ASR stream without requiring the wake word.  This
            # is opt-out in settings and does not start another microphone/STT loop.
            wake = self.data.setdefault("wake_word", {})
            wake.setdefault("quick_commands_enabled", True)
            version = 61
        if version < 62:
            # HOTFIX4: keep more of softly-spoken command endings, and make every
            # wake spelling visible in the three punctuation forms Whisper actually
            # emits in the field (bare / period / exclamation).  Matching still
            # normalizes punctuation, so this expands observed spelling coverage
            # without lowering the fuzzy threshold.
            voice = self.data.setdefault("voice", {})
            try:
                current_silence = float(voice.get("silence_seconds", 1.0) or 1.0)
            except (TypeError, ValueError):
                current_silence = 1.0
            if current_silence <= 1.05:
                voice["silence_seconds"] = 1.25
            voice.setdefault("command_language", "ru")

            wake = self.data.setdefault("wake_word", {})
            aliases_raw = wake.get("aliases", []) or []
            if isinstance(aliases_raw, str):
                aliases_raw = aliases_raw.splitlines()
            # Existing HOTFIX2/3 profiles may contain only the older capped alias
            # subset. Merge the current packaged list before adding punctuation so
            # upgrading a real user profile receives every observed wake spelling.
            packaged_now = self._read_json(self.default_settings_path)
            packaged_aliases = list((packaged_now.get("wake_word", {}) or {}).get("aliases", []) or [])
            bases: list[str] = []
            seen_bases: set[str] = set()
            for raw in [*aliases_raw, *packaged_aliases, "Doris"]:
                value = str(raw or "").strip().rstrip(".!").strip()
                key = value.casefold()
                if value and key not in seen_bases:
                    seen_bases.add(key)
                    bases.append(value)
            aliases: list[str] = []
            for value in bases:
                aliases.extend((value, value + ".", value + "!"))
            wake["aliases"] = aliases[:900]
            wake.setdefault("quick_commands_enabled", True)
            version = 62
        if version < 63:
            # HOTFIX11 multilingual application/assistant language. User-authored
            # wake words, prompts, paths, tokens and dictionary text remain untouched.
            app = self.data.setdefault("app", {})
            app.setdefault("language", "ru")
            version = 63
        if version < 64:
            # HOTFIX12: schema marker for noise-aware command endpointing and
            # additional field-observed wake spellings. Wake aliases themselves are
            # merged additively below, so user-authored wake text is never replaced.
            voice = self.data.setdefault("voice", {})
            voice.setdefault("ignore_nonlinguistic_noise", True)
            version = 64

        # HOTFIX7+ keeps wake spelling coverage additive across schema upgrades., but wake spelling coverage is
        # additive data rather than a schema change. Merge current packaged wake
        # aliases into existing profiles on every load so users upgrading from
        # HOTFIX6 receive new field-observed spellings without deleting their own.
        wake = self.data.setdefault("wake_word", {})
        aliases_raw = wake.get("aliases", []) or []
        if isinstance(aliases_raw, str):
            aliases_raw = aliases_raw.splitlines()
        packaged_now = self._read_json(self.default_settings_path)
        packaged_aliases = list((packaged_now.get("wake_word", {}) or {}).get("aliases", []) or [])
        bases: list[str] = []
        seen_bases: set[str] = set()
        for raw in [*aliases_raw, *packaged_aliases]:
            value = str(raw or "").strip().rstrip(".!?").strip()
            key = value.casefold()
            if value and key not in seen_bases:
                seen_bases.add(key)
                bases.append(value)
        merged_aliases: list[str] = []
        for value in bases:
            merged_aliases.extend((value, value + ".", value + "!", value + "?"))
        # Preserve the exact packaged/user corpus in the editable field even when
        # two entries normalize to the same wake key. Runtime matching still
        # de-duplicates normalized forms in active_wake_aliases().
        exact_seen = set(merged_aliases)
        for raw in [*aliases_raw, *packaged_aliases]:
            exact = str(raw or "").strip()
            if exact and exact not in exact_seen:
                merged_aliases.append(exact)
                exact_seen.add(exact)
        wake["aliases"] = merged_aliases[:900]
        app = self.data.setdefault("app", {})
        language = str(app.get("language", "en") or "en").strip().casefold()
        if language not in {"ru", "en", "de", "fr", "es", "it", "pt"}:
            language = "en"
        app["language"] = language
        self.data.setdefault("voice", {})["command_language"] = language
        if version < 65:
            self.data.setdefault("hotkeys", {}).setdefault("app_microphone_toggle", "Ctrl+Alt+M")
            version = 65
        if version < 66:
            # HOTFIX14: additive wake aliases and rolling-utterance wake matching.
            # The actual aliases are merged from packaged defaults above, therefore
            # user-authored aliases remain untouched.
            version = 66
        if version < 67:
            # HOTFIX15: user-selectable persistent-data directories and a one-time
            # 20% robot enlargement requested for the compact main-window layout.
            self.data.setdefault("storage", {})
            ui = self.data.setdefault("ui", {})
            if not bool(ui.get("robot_scale_hotfix15_applied", False)):
                try:
                    current = int(ui.get("robot_scale_percent", 100) or 100)
                except (TypeError, ValueError):
                    current = 100
                ui["robot_scale_percent"] = max(60, min(180, int(round(current * 1.20))))
                ui["robot_scale_hotfix15_applied"] = True
            version = 67
        if version < 68:
            # HOTFIX16: application/game routing recovery plus additional
            # field-observed wake spellings. Aliases are merged additively from
            # packaged defaults above, so no user-authored wake text is replaced.
            version = 68
        if version < 72:
            # HOTFIX23: external API/productivity integrations. Secrets remain
            # DPAPI-encrypted; only schema/default values are added here.
            integrations = self.data.setdefault("integrations", {})
            integrations.setdefault("openweather", {"enabled": False, "api_key_encrypted": "", "city": "", "country_code": "", "units": "metric"})
            self.data.setdefault("briefing", {"enabled": True, "show_on_startup": True})
            self.data.setdefault("timers", {"enabled": True})
            version = 72
        if version < 73:
            # HOTFIX24: legacy mail API and Discord fallback/shared-queue
            # configuration were removed; keep the official voice bot identifiers.
            email = self.data.setdefault("email", {})
            if str(email.get("default_provider", "") or "").casefold() == "gmail":
                email["default_provider"] = "mailru"
            discord = self.data.setdefault("integrations", {}).setdefault("discord", {})
            discord["shared_music_queue_enabled"] = True
            discord["ui_fallback"] = False
            discord["music_text_channel"] = ""
            discord["default_voice_channel"] = ""
            discord["music_bot_name"] = ""
            discord["music_bot_mode"] = "api_only"
            discord["music_play_template"] = ""
            discord["music_query_option"] = ""
            servers = discord.get("servers", []) or []
            if isinstance(servers, list):
                cleaned = []
                for item in servers:
                    if not isinstance(item, dict):
                        continue
                    cleaned.append({k: v for k, v in item.items() if k in {"name", "bot_token_encrypted", "guild_id", "discord_user_id"}})
                discord["servers"] = cleaned
            version = 73
        if version < 74:
            # HOTFIX25 migration: remove the legacy Google calendar integration.
            # HOTFIX26 no longer creates any replacement local Schedule state.
            self.data.setdefault("integrations", {}).pop("google", None)
            ui = self.data.setdefault("ui", {})
            ui.setdefault("timer_overlay_enabled", True)
            ui.setdefault("timer_overlay_position", "bottom_left")
            ui.setdefault("timer_overlay_font_size", 12)
            ui.setdefault("timer_overlay_opacity_percent", 88)
            ui.setdefault("timer_overlay_style", "glass")
            ui.setdefault("timer_overlay_text_color", "#EAF2FF")
            ui.setdefault("timer_overlay_background_color", "#0A0F18")
            ui.setdefault("timer_overlay_border_color", "#57D7FF")
            version = 74
        if version < 75:
            # HOTFIX26: Schedule is removed. Integrations move behind a stable
            # plugin boundary and panels gain independent alpha/placement fields.
            self.data.pop("schedule", None)
            integrations = self.data.setdefault("integrations", {})
            overlay = self.data.setdefault("desktop", {}).setdefault("overlay", {})
            legacy_opacity = int(overlay.get("opacity", 72) or 72)
            overlay.setdefault("background_opacity", legacy_opacity)
            overlay.setdefault("border_opacity", 100)
            overlay.setdefault("drag_enabled", True)
            styles = overlay.setdefault("styles", {})
            for category in ("session", "checklist", "notes", "music"):
                style = styles.setdefault(category, {})
                legacy = int(style.get("opacity", legacy_opacity) or legacy_opacity)
                style.setdefault("background_opacity", legacy)
                style.setdefault("border_opacity", int(overlay.get("border_opacity", 100) or 100))
                style.setdefault("drag_enabled", bool(overlay.get("drag_enabled", True)))
                style.setdefault("monitor", int(overlay.get("monitor", 1) or 1))
                style.setdefault("position", str(overlay.get("position", "top_left") or "top_left"))
            self.data.setdefault("ui", {}).setdefault("robot_active_animation", "off")
            plugins = self.data.setdefault("plugins", {})
            plugins.setdefault("disabled_builtin", [])
            version = 75
        if version < 76:
            overlay = self.data.setdefault("desktop", {}).setdefault("overlay", {})
            styles = overlay.setdefault("styles", {})
            music = styles.setdefault("music", {})
            music.setdefault("controls_visible", True)
            music.setdefault("control_color", "#F2F6FF")
            music.setdefault("control_background_color", "#0A0F18")
            music.setdefault("control_background_opacity", 0)
            music.setdefault("control_border_color", "#F2F6FF")
            music.setdefault("control_border_opacity", 80)
            music.setdefault("visualizer_fps", 60)
            version = 76
        if version < 77:
            # HOTFIX28: lower-cost background telemetry. The old packaged 1 s
            # poll launched sensor probes too often on gaming systems; preserve
            # explicit slower user choices and migrate only the old fast default.
            gaming = self.data.setdefault("gaming", {})
            try:
                refresh = float(gaming.get("telemetry_refresh_seconds", 1.0) or 1.0)
            except (TypeError, ValueError):
                refresh = 1.0
            if refresh <= 1.0:
                gaming["telemetry_refresh_seconds"] = 2.0
            version = 77
        if version < 78:
            # HOTFIX29 removes obsolete external-provider credentials and
            # migrates newly observed wake-word spellings into existing profiles.
            integrations = self.data.setdefault("integrations", {})
            integrations.pop("steam", None)
            integrations.pop("google_" + "translate", None)
            wake = self.data.setdefault("wake_word", {})
            aliases = wake.get("aliases", []) or []
            if isinstance(aliases, str):
                aliases = aliases.splitlines()
            existing = {str(item or "").strip().casefold() for item in aliases}
            for alias in ("Дарьбез", "Дарьбез.", "Дарьбез!"):
                if alias.casefold() not in existing:
                    aliases.append(alias)
            wake["aliases"] = aliases
            disabled = self.data.setdefault("plugins", {}).setdefault("disabled_builtin", [])
            if isinstance(disabled, list):
                self.data["plugins"]["disabled_builtin"] = [
                    item for item in disabled if str(item) not in {"integrations." + "steam"}
                ]
            version = 78
        if version < 79:
            # HOTFIX30: migrate newly observed Whisper spellings into existing
            # profiles and seed independent visual presets for all four panels.
            wake = self.data.setdefault("wake_word", {})
            aliases = wake.get("aliases", []) or []
            if isinstance(aliases, str):
                aliases = aliases.splitlines()
            existing = {str(item or "").strip().casefold() for item in aliases}
            for wake_base in ("Zarwiec", "Зарвез", "Jerviet", "Доримец", "Doró jest", "Дорвец", "Dorous", "Дариваться"):
                for suffix in ("", ".", "!"):
                    alias = wake_base + suffix
                    if alias.casefold() not in existing:
                        aliases.append(alias)
                        existing.add(alias.casefold())
            wake["aliases"] = aliases
            styles = self.data.setdefault("desktop", {}).setdefault("overlay", {}).setdefault("styles", {})
            for panel_key in ("session", "checklist", "notes", "music"):
                panel_style = styles.setdefault(panel_key, {})
                if isinstance(panel_style, dict):
                    panel_style.setdefault("theme_preset", "NEO Cyan")
            music_style = styles.setdefault("music", {})
            if isinstance(music_style, dict):
                try:
                    music_style["visualizer_fps"] = max(60, int(music_style.get("visualizer_fps", 60) or 60))
                except (TypeError, ValueError):
                    music_style["visualizer_fps"] = 60
            version = 79
        if version < 80:
            # HOTFIX31: make the music waveform truly continuous at high refresh
            # and add the latest Whisper wake spellings to existing profiles.
            wake = self.data.setdefault("wake_word", {})
            aliases = wake.get("aliases", []) or []
            if isinstance(aliases, str):
                aliases = aliases.splitlines()
            existing = {str(item or "").strip().casefold() for item in aliases}
            for wake_base in ("Dżorbez", "Dlaczego jest", "Czaremy z", "ジャルース"):
                for suffix in ("", ".", "!"):
                    alias = wake_base + suffix
                    if alias.casefold() not in existing:
                        aliases.append(alias)
                        existing.add(alias.casefold())
            wake["aliases"] = aliases
            speech = self.data.setdefault("speech", {})
            speech_aliases = speech.get("wake_aliases", []) or []
            if isinstance(speech_aliases, str):
                speech_aliases = speech_aliases.splitlines()
            speech_existing = {str(item or "").strip().casefold() for item in speech_aliases}
            for wake_base in ("Dżorbez", "Dlaczego jest", "Czaremy z", "ジャルース"):
                for suffix in ("", ".", "!"):
                    alias = wake_base + suffix
                    if alias.casefold() not in speech_existing:
                        speech_aliases.append(alias)
                        speech_existing.add(alias.casefold())
            speech["wake_aliases"] = speech_aliases
            music_style = self.data.setdefault("desktop", {}).setdefault("overlay", {}).setdefault("styles", {}).setdefault("music", {})
            if isinstance(music_style, dict):
                # Upgrade the old 60-FPS default to the smoother 120-FPS preset;
                # explicit custom values above 60 remain capped by the widget.
                try:
                    current_fps = int(music_style.get("visualizer_fps", 120) or 120)
                except (TypeError, ValueError):
                    current_fps = 120
                if current_fps <= 60:
                    music_style["visualizer_fps"] = 120
            version = 80
        if version < 81:
            # HOTFIX36: preserve newly observed Whisper spellings and mark the
            # Yandex/network + transparent-window input reliability migration.
            wake = self.data.setdefault("wake_word", {})
            aliases = wake.get("aliases", []) or []
            if isinstance(aliases, str):
                aliases = aliases.splitlines()
            existing = {str(item or "").strip().casefold() for item in aliases}
            for wake_base in ("Zaróget", "Джоргет", "Jargetz", "ジャルース"):
                for suffix in ("", ".", "!"):
                    alias = wake_base + suffix
                    if alias.casefold() not in existing:
                        aliases.append(alias)
                        existing.add(alias.casefold())
            wake["aliases"] = aliases
            speech = self.data.setdefault("speech", {})
            speech_aliases = speech.get("wake_aliases", []) or []
            if isinstance(speech_aliases, str):
                speech_aliases = speech_aliases.splitlines()
            speech_existing = {str(item or "").strip().casefold() for item in speech_aliases}
            for wake_base in ("Zaróget", "Джоргет", "Jargetz", "ジャルース"):
                for suffix in ("", ".", "!"):
                    alias = wake_base + suffix
                    if alias.casefold() not in speech_existing:
                        speech_aliases.append(alias)
                        speech_existing.add(alias.casefold())
            speech["wake_aliases"] = speech_aliases
            version = 81
        if version < 82:
            # HOTFIX37: compact default geometry, independent chat px sizing,
            # atmospheric palette controls, smoother robot scenes and shuffle.
            ui = self.data.setdefault("ui", {})
            # Migrate only the former packaged defaults so existing deliberate
            # custom geometry is preserved. Always-on-top is removed from the UI
            # and intentionally disabled for everyone.
            if int(ui.get("window_width", 460) or 460) == 460:
                ui["window_width"] = 322
            if int(ui.get("window_height", 630) or 630) == 630:
                ui["window_height"] = 476
            if int(ui.get("sleep_after_seconds", 55) or 55) == 55:
                ui["sleep_after_seconds"] = 20
            ui["always_on_top"] = False
            ui.setdefault("typewriter_interval_ms", 8)
            ui.setdefault("corner_radius", 26)
            ui.setdefault("idle_animations", True)
            if "chat_font_size_px" not in ui:
                try:
                    legacy_scale = float(ui.get("font_scale_percent", 100) or 100)
                except (TypeError, ValueError):
                    legacy_scale = 100.0
                ui["chat_font_size_px"] = max(8, min(36, int(round(14.0 * legacy_scale / 100.0))))
            ui.setdefault("robot_moon_color", "#DDE7F4")
            ui.setdefault("robot_cloud_color", "#DCE7F3")
            ui.setdefault("robot_rain_color", "#91CFFF")
            ui.setdefault("robot_starfall_color", "#EAF6FF")
            ui.setdefault("robot_starry_sky_color", ui.get("robot_starfall_color", "#EAF6FF"))
            ui.setdefault("robot_snow_color", "#F6FAFF")
            if str(ui.get("robot_active_animation", "off") or "off").casefold() == "sun":
                ui["robot_active_animation"] = "off"

            integrations = self.data.setdefault("integrations", {})
            integrations.setdefault("yandex_music", {}).setdefault("shuffle_enabled", False)
            integrations.setdefault("discord", {}).setdefault("music_shuffle_enabled", False)

            for section_name, key in (("wake_word", "aliases"), ("speech", "wake_aliases"), ("voice", "wake_aliases")):
                section = self.data.setdefault(section_name, {})
                values = section.get(key, []) or []
                if isinstance(values, str):
                    values = values.splitlines()
                existing = {str(item or "").strip().casefold() for item in values}
                for wake_base in ("Жарились", "Darylis", "Dzieżywizje"):
                    for suffix in ("", ".", "!"):
                        alias = wake_base + suffix
                        if alias.casefold() not in existing:
                            values.append(alias)
                            existing.add(alias.casefold())
                section[key] = values
            version = 82
        if version < 83:
            # HOTFIX39: durable local dialogue/action context and broader wake
            # recovery. Existing user choices are preserved via setdefault.
            context = self.data.setdefault("context_memory", {})
            if not isinstance(context, dict):
                context = {}
                self.data["context_memory"] = context
            context.setdefault("enabled", True)
            context.setdefault("persist_dialogue", True)
            context.setdefault("persist_actions", True)
            context.setdefault("restore_recent_dialogue", True)
            context.setdefault("recent_dialogue_messages", 8)
            context.setdefault("prompt_recent_requests", 6)
            context.setdefault("prompt_recent_actions", 8)

            wake_bases = (
                "Ciao to you guys",
                "Jarvis", "Jaarvis", "Jarvys", "Jarviz", "Jervis", "Jerviss", "Jarves",
                "Djarvis", "Djarwis", "Dzharvis", "Dżarvis", "Dżarwis", "Džarvis",
                "Zharvis", "Zharwis", "Zarvis", "Zarwis", "Garvis", "Garwis", "Gervis", "Gerwis",
                "Yarvis", "Yarwis", "Charvis", "Sharvis", "Járvis", "Järvis",
                "Джарвис", "Джервис", "Джарвису", "Джарвиса", "Джарвиз", "Джарвэс",
                "Жарвис", "Жервис", "Жарвиз", "Чарвис", "Шарвис", "Гарвис", "Гервис", "Ярвис",
                "Джар вис", "Жар вис", "Jar vis", "Djar vis", "Dzhar vis",
                "Жарились", "Darylis", "Dzieżywizje", "Darız.Gerviyiz", "Gerviyiz",
                "Zaróget", "Джоргет", "Jargetz",
            )
            for section_name, key in (("wake_word", "aliases"), ("speech", "wake_aliases"), ("voice", "wake_aliases")):
                section = self.data.setdefault(section_name, {})
                values = section.get(key, []) or []
                if isinstance(values, str):
                    values = values.splitlines()
                existing = {str(item or "").strip().casefold() for item in values}
                for wake_base in wake_bases:
                    for suffix in ("", ".", "!"):
                        alias = wake_base + suffix
                        if alias.casefold() not in existing:
                            values.append(alias)
                            existing.add(alias.casefold())
                section[key] = values
            version = 83
        if version < 84:
            # HOTFIX40: independent dialog font family and precision-first wake.
            ui = self.data.setdefault("ui", {})
            ui.setdefault("chat_font_family", "Segoe UI")
            wake = self.data.setdefault("wake_word", {})
            wake["fuzzy_threshold"] = max(0.92, float(wake.get("fuzzy_threshold", 0.88) or 0.88))
            version = 84
        if version < 85:
            # HOTFIX41: field-log wake atlas refresh. Short ASR substitutions stay
            # exact-only at safe utterance edges in wake_matcher; this migration
            # only makes the newly observed forms available to existing profiles.
            wake_bases = (
                "Даримис", "Жаривец", "Джорвис", "Доровис", "Дорвис", "Дорвец",
                "Зарвез", "Джаревес", "Дармез", "Dziarowysł", "Dziar wys", "Dżar wys",
                "Gerwis", "Gerwiste", "Czarywis", "Jareves", "Jörbüsel",
                "Doris", "Janice", "Jeremy's", "Jeremys", "Дорис", "Джанис", "Джеремис",
                "Жарились", "Darylis", "Dzieżywizje",
            )
            for section_name, key in (("wake_word", "aliases"), ("speech", "wake_aliases"), ("voice", "wake_aliases")):
                section = self.data.setdefault(section_name, {})
                values = section.get(key, []) or []
                if isinstance(values, str):
                    values = values.splitlines()
                existing = {str(item or "").strip().casefold() for item in values}
                for wake_base in wake_bases:
                    for suffix in ("", ".", "!"):
                        alias = wake_base + suffix
                        if alias.casefold() not in existing:
                            values.append(alias)
                            existing.add(alias.casefold())
                section[key] = values[:900]
            version = 85
        if version < 86:
            # HOTFIX43: low-latency wake/media path.  Keep the high-quality small
            # command model, but stop paying 1.25 s of endpoint silence and enable
            # the isolated openWakeWord Jarvis detector for the packaged wake word.
            voice = self.data.setdefault("voice", {})
            try:
                old_silence = float(voice.get("silence_seconds", 1.25) or 1.25)
            except Exception:
                old_silence = 1.25
            if old_silence >= 0.95:
                voice["silence_seconds"] = 0.72
            voice.setdefault("fast_media_enabled", True)
            voice.setdefault("fast_media_stt_model", "tiny")
            voice.setdefault("fast_media_max_seconds", 2.4)
            voice.setdefault("adaptive_endpoint_enabled", True)
            voice.setdefault("short_command_silence_seconds", 0.40)
            voice.setdefault("normal_command_silence_seconds", 0.62)
            try:
                if int(voice.get("command_min_voice_ms", 150) or 150) >= 145:
                    voice["command_min_voice_ms"] = 120
            except Exception:
                voice["command_min_voice_ms"] = 120
            try:
                if float(voice.get("start_timeout_seconds", 5.0) or 5.0) >= 5.0:
                    voice["start_timeout_seconds"] = 4.0
            except Exception:
                voice["start_timeout_seconds"] = 4.0

            wake = self.data.setdefault("wake_word", {})
            packaged = self._read_json(self.default_settings_path).get("wake_word", {}) or {}
            phrase = str(wake.get("phrase", "") or "").strip().casefold()
            trained = str(wake.get("acoustic_model_phrase", packaged.get("acoustic_model_phrase", "джарвис")) or "").strip().casefold()
            # HOTFIX119: the legacy native acoustic detector is retired from the
            # product surface. Historical migrations must never reactivate it.
            if phrase and trained and phrase == trained:
                wake["acoustic_enabled"] = False
            try:
                old_wake_silence = float(wake.get("silence_seconds", 0.30) or 0.30)
            except Exception:
                old_wake_silence = 0.30
            if old_wake_silence >= 0.28:
                wake["silence_seconds"] = 0.20
            try:
                old_chunk = float(wake.get("chunk_seconds", 2.2) or 2.2)
            except Exception:
                old_chunk = 2.2
            if old_chunk >= 2.1:
                wake["chunk_seconds"] = 1.8
            wake.setdefault("probe_interval_seconds", 0.18)
            wake.setdefault("preroll_seconds", 1.15)
            if abs(float(wake.get("acoustic_threshold", 0.12) or 0.12) - 0.12) < 1e-6:
                wake["acoustic_threshold"] = 0.16
            version = 86
        if version < 87:
            # HOTFIX43.1 STABILITY: HOTFIX43 enabled openWakeWord by default. On
            # affected Windows systems field logs showed the classifier returning
            # 0.000 continuously and the application exiting without a Python
            # traceback shortly after the native ONNX worker/preloads came online.
            # Keep the feature available for explicit opt-in, but migrate existing
            # HOTFIX43 profiles back to the stable tiny-Whisper wake path.
            wake = self.data.setdefault("wake_word", {})
            wake["acoustic_enabled"] = False
            voice = self.data.setdefault("voice", {})
            voice.setdefault("fast_media_enabled", True)
            voice.setdefault("fast_media_stt_model", "tiny")
            voice.setdefault("adaptive_endpoint_enabled", True)
            version = 87
        if version < 88:
            # HOTFIX43.2: low-latency wake no longer requires the unstable native
            # ONNX detector. Reuse the isolated tiny Whisper model on rolling
            # speech, and trim only the old default endpoint delays. Explicit user
            # custom values are preserved.
            wake = self.data.setdefault("wake_word", {})
            wake.setdefault("fast_asr_probe_enabled", True)
            wake.setdefault("fast_asr_probe_interval_seconds", 0.42)
            wake["acoustic_enabled"] = False
            voice = self.data.setdefault("voice", {})
            try:
                if abs(float(voice.get("short_command_silence_seconds", 0.40) or 0.40) - 0.40) < 1e-6:
                    voice["short_command_silence_seconds"] = 0.34
                if abs(float(voice.get("normal_command_silence_seconds", 0.62) or 0.62) - 0.62) < 1e-6:
                    voice["normal_command_silence_seconds"] = 0.50
                if int(voice.get("command_min_voice_ms", 120) or 120) == 120:
                    voice["command_min_voice_ms"] = 100
            except Exception:
                pass
            version = 88
        if version < 89:
            # HOTFIX44: keep the stable Whisper wake path but sample it earlier,
            # and add a Task Scheduler based high-priority startup option.
            wake = self.data.setdefault("wake_word", {})
            try:
                current_probe = float(wake.get("fast_asr_probe_interval_seconds", 0.42) or 0.42)
                if any(abs(current_probe - packaged) < 1e-6 for packaged in (0.42, 0.28)):
                    wake["fast_asr_probe_interval_seconds"] = 0.20
            except Exception:
                wake.setdefault("fast_asr_probe_interval_seconds", 0.20)
            wake.setdefault("fast_asr_probe_min_audio_seconds", 0.24)
            ui = self.data.setdefault("ui", {})
            ui.setdefault("priority_startup", False)
            ollama = self.data.setdefault("ollama", {})
            ollama.setdefault("background_bootstrap", True)
            version = 89
        if version < 90:
            # HOTFIX45: sample the rolling Whisper wake probe a bit earlier,
            # tighten the post-command rearm, and preserve short trailing words
            # more reliably in the main command recognizer.
            wake = self.data.setdefault("wake_word", {})
            try:
                current_probe = float(wake.get("fast_asr_probe_interval_seconds", 0.20) or 0.20)
                if any(abs(current_probe - packaged) < 1e-6 for packaged in (0.42, 0.20, 0.16)):
                    wake["fast_asr_probe_interval_seconds"] = 0.16
            except Exception:
                wake.setdefault("fast_asr_probe_interval_seconds", 0.16)
            try:
                current_min_audio = float(wake.get("fast_asr_probe_min_audio_seconds", 0.24) or 0.24)
                if any(abs(current_min_audio - packaged) < 1e-6 for packaged in (0.24, 0.22, 0.18)):
                    wake["fast_asr_probe_min_audio_seconds"] = 0.18
            except Exception:
                wake.setdefault("fast_asr_probe_min_audio_seconds", 0.18)
            voice = self.data.setdefault("voice", {})
            try:
                if int(voice.get("post_answer_rearm_ms", 20) or 20) == 20:
                    voice["post_answer_rearm_ms"] = 12
                if int(voice.get("continuous_rearm_ms", 35) or 35) == 35:
                    voice["continuous_rearm_ms"] = 20
                if abs(float(voice.get("short_command_silence_seconds", 0.34) or 0.34) - 0.34) < 1e-6:
                    voice["short_command_silence_seconds"] = 0.30
                if abs(float(voice.get("normal_command_silence_seconds", 0.50) or 0.50) - 0.50) < 1e-6:
                    voice["normal_command_silence_seconds"] = 0.46
            except Exception:
                pass
            version = 90
        if version < 91:
            # HOTFIX46: opt-in voice unlock configuration. Password remains an
            # empty DPAPI slot until the user explicitly enables the feature.
            unlock = self.data.setdefault("voice_unlock", {})
            unlock.setdefault("enabled", False)
            unlock.setdefault("account", "")
            unlock.setdefault("password_encrypted", "")
            unlock.setdefault("phrase", "")
            unlock.setdefault("aliases", [])
            unlock.setdefault("locked_session_only", True)
            unlock.setdefault("tscon_fallback", True)
            unlock.setdefault("cooldown_seconds", 4.0)
            version = 91
        if version < 92:
            # HOTFIX47: configurable live information inside the robot stage.
            ui = self.data.setdefault("ui", {})
            ui.setdefault("robot_info_clock_enabled", False)
            ui.setdefault("robot_info_clock_position", "top_center")
            ui.setdefault("robot_info_track_enabled", False)
            ui.setdefault("robot_info_track_position", "bottom_center")
            ui.setdefault("robot_info_temperature_enabled", False)
            ui.setdefault("robot_info_temperature_position", "top_right")
            ui.setdefault("robot_info_font_size", 12)
            unlock = self.data.setdefault("voice_unlock", {})
            unlock.setdefault("native_provider_preferred", True)
            unlock.setdefault("revalidate_password_before_unlock", False)
            version = 92
        if version < 93:
            # HOTFIX48: faster English-constrained rolling Jarvis probe.
            ui = self.data.setdefault("ui", {})
            music_style = self.data.setdefault("desktop", {}).setdefault("overlay", {}).setdefault("styles", {}).setdefault("music", {})
            if isinstance(music_style, dict):
                music_style.setdefault("visualizer_mode", "symmetric")
            wake = self.data.setdefault("wake_word", {})
            try:
                current_probe = float(wake.get("fast_asr_probe_interval_seconds", 0.16) or 0.16)
                if current_probe >= 0.16 - 1e-6:
                    wake["fast_asr_probe_interval_seconds"] = 0.12
            except Exception:
                wake["fast_asr_probe_interval_seconds"] = 0.12
            try:
                current_min = float(wake.get("fast_asr_probe_min_audio_seconds", 0.18) or 0.18)
                if current_min >= 0.18 - 1e-6:
                    wake["fast_asr_probe_min_audio_seconds"] = 0.14
            except Exception:
                wake["fast_asr_probe_min_audio_seconds"] = 0.14
            wake.setdefault("fast_probe_language", "en")
            version = 93
        if version < 94:
            # HOTFIX49: direct manipulation of robot HUD and chat split. Layout
            # uses normalized coordinates so it survives DPI/window changes.
            ui = self.data.setdefault("ui", {})
            ui.setdefault("robot_layout_edit_enabled", False)
            ui.setdefault("chat_height_px", 0)
            ui.setdefault("robot_info_layout", {
                "wave": {"x": 0.27, "y": 0.06, "w": 0.48, "h": 0.16},
                "temperature": {"x": 0.69, "y": 0.02, "w": 0.28, "h": 0.09},
                "track": {"x": 0.18, "y": 0.82, "w": 0.64, "h": 0.10},
                "clock": {"x": 0.82, "y": 0.82, "w": 0.15, "h": 0.10},
            })
            wake = self.data.setdefault("wake_word", {})
            try:
                if float(wake.get("fast_asr_probe_interval_seconds", 0.12) or 0.12) >= 0.12 - 1e-6:
                    wake["fast_asr_probe_interval_seconds"] = 0.10
                if float(wake.get("fast_asr_probe_min_audio_seconds", 0.14) or 0.14) >= 0.14 - 1e-6:
                    wake["fast_asr_probe_min_audio_seconds"] = 0.12
            except Exception:
                wake["fast_asr_probe_interval_seconds"] = 0.10
                wake["fast_asr_probe_min_audio_seconds"] = 0.12
            version = 94
        if version < 95:
            # HOTFIX50: automatic multi-install Discord queue coordination and
            # confirmed field wake spellings. Manual Guild/User IDs are no longer
            # required by the settings UI.
            discord = self.data.setdefault("integrations", {}).setdefault("discord", {})
            discord["shared_music_queue_enabled"] = True
            wake = self.data.setdefault("wake_word", {})
            aliases = list(wake.get("aliases", []) or [])
            existing = {str(x or "").casefold().strip() for x in aliases}
            for base in ("Jörves", "Jorves"):
                for value in (base, base + ".", base + "!"):
                    if value.casefold() not in existing:
                        aliases.append(value)
                        existing.add(value.casefold())
            wake["aliases"] = aliases[:900]
            version = 95
        if version < 96:
            # HOTFIX57: scenarios are now exposed through the Telegram button UI.
            # HOTFIX54-56 persisted "scenario" in blocked_tools, so an upgraded
            # installation would otherwise keep silently blocking the new menu
            # even though the packaged defaults no longer do. Preserve every
            # other user-selected remote block and remove only this legacy item.
            telegram = self.data.setdefault("integrations", {}).setdefault("telegram", {})
            blocked = telegram.get("blocked_tools", []) or []
            if isinstance(blocked, list):
                telegram["blocked_tools"] = [
                    item for item in blocked
                    if str(item or "").strip().casefold() != "scenario"
                ]
            telegram.setdefault("cleanup_backfill_message_ids", 3000)
            version = 96
        if version < 97:
            # Preserve additional field-observed wake spellings.
            wake_bases = (
                "Дорогас", "Дорюс", "Güzel birisi", "자르메스", "ジャルーズ", "자 루개스", "자루개스",
            )
            for section_name, key in (("wake_word", "aliases"), ("speech", "wake_aliases"), ("voice", "wake_aliases")):
                section = self.data.setdefault(section_name, {})
                values = section.get(key, []) or []
                if isinstance(values, str):
                    values = values.splitlines()
                existing = {str(item or "").strip().casefold() for item in values}
                for wake_base in wake_bases:
                    for suffix in ("", ".", "!"):
                        alias = wake_base + suffix
                        if alias.casefold() not in existing:
                            values.append(alias)
                            existing.add(alias.casefold())
                section[key] = values[:900]
            version = 97
        if version < 98:
            # Alpha-Beta: new wake aliases plus explicit per-panel visibility and
            # restored remote shutdown (still protected as a HIGH-risk action).
            wake_bases = ("Дадавис", "Ciao")
            for section_name, key in (("wake_word", "aliases"), ("speech", "wake_aliases"), ("voice", "wake_aliases")):
                section = self.data.setdefault(section_name, {})
                values = section.get(key, []) or []
                if isinstance(values, str):
                    values = values.splitlines()
                existing = {str(item or "").strip().casefold() for item in values}
                for wake_base in wake_bases:
                    for suffix in ("", ".", "!"):
                        alias = wake_base + suffix
                        if alias.casefold() not in existing:
                            values.append(alias)
                            existing.add(alias.casefold())
                section[key] = values[:900]
            overlay = self.data.setdefault("desktop", {}).setdefault("overlay", {})
            styles = overlay.setdefault("styles", {})
            for style_key in ("session", "checklist", "notes", "music"):
                style = styles.setdefault(style_key, {})
                if isinstance(style, dict):
                    style.setdefault("enabled", True)
            self.data.setdefault("integrations", {}).setdefault("telegram", {})["allow_remote_shutdown"] = True
            version = 98
        if version < 99:
            # NEO Alpha-Beta Atlas 2.0: additive offline-first routing. Existing
            # installs keep their current behavior, while semantic/context layers
            # and lazy vision are enabled with conservative thresholds.
            routing = self.data.setdefault("routing", {})
            routing.setdefault("atlas2_enabled", True)
            routing.setdefault("static_hash_enabled", True)
            routing.setdefault("semantic_enabled", True)
            routing.setdefault("semantic_threshold", 0.43)
            routing.setdefault("semantic_margin", 0.05)
            routing.setdefault("semantic_dimension", 384)
            routing.setdefault("lazy_vision_enabled", True)
            memory = self.data.setdefault("context_memory", {})
            memory.setdefault("system_poll_seconds", 1.5)
            memory.setdefault("gaming_poll_seconds", 8.0)
            version = 99
        if version < 108:
            # Refresh packaged wake aliases while preserving user additions and
            # removing conversational phrases that proved too broad as wake words.
            unsafe_wake = {
                "ciao", "ciao.", "ciao!",
                "ciao everyone", "ciao everyone.", "ciao everyone!",
                "ciao to you guys", "ciao to you guys.", "ciao to you guys!",
            }
            defaults = self._read_json(self.default_settings_path)
            # Drop obsolete keys from the two tightly managed routing/session
            # sections so removed experiments cannot survive in upgraded profiles.
            for managed_name in ("gaming", "routing"):
                managed = self.data.get(managed_name)
                packaged_managed = defaults.get(managed_name, {}) if isinstance(defaults, dict) else {}
                if isinstance(managed, dict) and isinstance(packaged_managed, dict):
                    for stale_key in tuple(managed):
                        if stale_key not in packaged_managed:
                            managed.pop(stale_key, None)
            for section_name, key in (("wake_word", "aliases"), ("speech", "wake_aliases"), ("voice", "wake_aliases")):
                section = self.data.setdefault(section_name, {})
                values = section.get(key, []) or []
                if isinstance(values, str):
                    values = values.splitlines()
                default_section = defaults.get(section_name, {}) if isinstance(defaults, dict) else {}
                packaged = default_section.get(key, []) if isinstance(default_section, dict) else []
                if isinstance(packaged, str):
                    packaged = packaged.splitlines()
                merged = []
                seen = set()
                for alias in [*packaged, *values]:
                    value = str(alias or "").strip()
                    low = value.casefold()
                    if not value or low in unsafe_wake or low in seen:
                        continue
                    merged.append(value)
                    seen.add(low)
                section[key] = merged[:1600]
            version = 108
        if version < 109:
            # Version 109 is intentionally reserved. The experimental assistant
            # speech-output module was removed from the product.
            version = 109
        if version < 110:
            # v110 latency safety: the previous desktop defaults inherited
            # Ollama's num_predict=-1 (infinite generation).  That is unsuitable
            # for a voice assistant because a malformed/overthinking turn can keep
            # the UI in «Думает…» indefinitely.  Existing profiles are migrated
            # to a bounded interactive default; long tasks still receive larger
            # per-turn budgets from Agent._interactive_generation_budget().
            ollama = self.data.setdefault("ollama", {})
            try:
                current_predict = int(ollama.get("num_predict", -1) or -1)
            except Exception:
                current_predict = -1
            if current_predict < 0 or current_predict > 4096:
                ollama["num_predict"] = 512
            ollama["unlimited_response"] = False
            try:
                timeout = float(ollama.get("first_token_timeout_seconds", 15) or 15)
            except Exception:
                timeout = 15.0
            ollama["first_token_timeout_seconds"] = max(8.0, min(20.0, timeout))
            version = 110
        if version < 111:
            # v111: compact visual/performance profile. Remove settings belonging
            # to the retired assistant speech output and standalone robot wave;
            # keep voice INPUT/wake-word settings untouched.
            voice = self.data.setdefault("voice", {})
            for stale_key in tuple(voice):
                if str(stale_key).startswith("tts_"):
                    voice.pop(stale_key, None)
            try:
                old_short = float(voice.get("short_command_silence_seconds", 0.30) or 0.30)
            except Exception:
                old_short = 0.30
            if abs(old_short - 0.30) < 1e-6:
                voice["short_command_silence_seconds"] = 0.24
            ui = self.data.setdefault("ui", {})
            for stale_key in ("robot_info_wave_enabled", "robot_info_wave_position", "robot_info_wave_mode"):
                ui.pop(stale_key, None)
            layout = ui.get("robot_info_layout")
            if isinstance(layout, dict):
                layout.pop("wave", None)
            version = 111
        if version < 112:
            # v112: wake-word-per-command is the product default again. The
            # previous continuous-conversation default kept the microphone in a
            # command loop after every answer and consumed more background work.
            # Apply the new default once to existing profiles; users can still
            # explicitly switch back to continuous mode in Settings afterwards.
            voice = self.data.setdefault("voice", {})
            voice["interaction_mode"] = "wake_each_command"
            voice["continuous_after_wake"] = False
            music_style = self.data.setdefault("desktop", {}).setdefault("overlay", {}).setdefault("styles", {}).setdefault("music", {})
            if isinstance(music_style, dict):
                music_style["visualizer_fps"] = 120
            ui = self.data.setdefault("ui", {})
            try:
                current_typewriter = int(ui.get("typewriter_interval_ms", 8) or 8)
            except Exception:
                current_typewriter = 8
            # 1 ms caused up to 1000 GUI wakeups/s. 8 ms aligns the visible text
            # animation with the 120 Hz UI frame budget without changing content.
            if current_typewriter < 8:
                ui["typewriter_interval_ms"] = 8
            version = 112
        if version < 113:
            # v113: language-scoped observed wake aliases + resource/routing audit.
            # These ordinary English-looking ASR renderings are requested only for
            # Russian command mode, so never add them to the global alias corpus.
            wake = self.data.setdefault("wake_word", {})
            language_aliases = wake.setdefault("language_aliases", {})
            if not isinstance(language_aliases, dict):
                language_aliases = {}
                wake["language_aliases"] = language_aliases
            ru_aliases = language_aliases.get("ru", []) or []
            if isinstance(ru_aliases, str):
                ru_aliases = ru_aliases.splitlines()
            requested = ["There it is", "Service", "Choice", "Coice"]
            seen = {str(x).strip().rstrip(".!").casefold() for x in ru_aliases if str(x).strip()}
            for base in requested:
                if base.casefold() not in seen:
                    ru_aliases.extend((base, base + ".", base + "!"))
                    seen.add(base.casefold())
            language_aliases["ru"] = ru_aliases

            ollama = self.data.setdefault("ollama", {})
            ollama.setdefault("num_thread", 4)
            ollama.setdefault("num_batch", 128)
            ollama.setdefault("num_gpu", -1)  # -1 = let Ollama choose; do not force VRAM usage.
            # 30 minutes kept several GB resident long after a chat. Ten minutes
            # preserves fast follow-ups but releases RAM/VRAM much sooner.
            if str(ollama.get("keep_alive", "30m") or "30m").strip().casefold() in {"30m", "10m", "15m", "60m"}:
                ollama["keep_alive"] = "5m"
            # Resource-first profile: deterministic commands/social replies do not
            # need the 4B model, so do not reserve RAM/VRAM just because JARVIS
            # started. The first genuinely generative turn loads it on demand.
            ollama["preload_model"] = False
            memory = self.data.setdefault("context_memory", {})
            if float(memory.get("system_poll_seconds", 1.5) or 1.5) < 2.0:
                memory["system_poll_seconds"] = 2.0
            if float(memory.get("gaming_poll_seconds", 4.0) or 4.0) < 3.0:
                memory["gaming_poll_seconds"] = 4.0
            version = 113
        if version < 114:
            # v114: fast-response profile requested by the user. Keep the mature
            # quick-media vocabulary unchanged, preload Ollama after startup, and
            # retain it long enough that ordinary follow-up turns do not repeatedly
            # pay the multi-second model load cost seen in Windows logs.
            ollama = self.data.setdefault("ollama", {})
            ollama["preload_model"] = True
            keep = str(ollama.get("keep_alive", "30m") or "30m").strip().casefold()
            if keep in {"0", "1m", "5m", "10m", "15m"}:
                ollama["keep_alive"] = "30m"
            wake = self.data.setdefault("wake_word", {})
            try:
                current_probe = float(wake.get("fast_asr_probe_interval_seconds", 0.10) or 0.10)
            except Exception:
                current_probe = 0.10
            wake["fast_asr_probe_interval_seconds"] = min(current_probe, 0.08)
            try:
                current_min_audio = float(wake.get("fast_asr_probe_min_audio_seconds", 0.12) or 0.12)
            except Exception:
                current_min_audio = 0.12
            wake["fast_asr_probe_min_audio_seconds"] = min(current_min_audio, 0.10)
            version = 114
        # Reserved historical schema numbers. Their retired settings are intentionally
        # not recreated in current profiles.
        if version < 115:
            version = 115
        if version < 116:
            version = 116
        if version < 117:
            version = 117
        if version < 118:
            version = 118
        if version < 119:
            version = 119
        if version < 120:
            version = 120

        if version < 121:
            # v121: observed wake-word spellings requested during V4.2 testing.
            # Keep them in the global corpus because they are uncommon proper-name
            # ASR renderings, and expand punctuation variants through the normal
            # wake normalization path. Existing persistent profiles receive them
            # once during migration; no manual Settings edit is required.
            wake = self.data.setdefault("wake_word", {})
            raw_aliases = wake.setdefault("aliases", [])
            if isinstance(raw_aliases, str):
                raw_aliases = raw_aliases.splitlines()
            if not isinstance(raw_aliases, list):
                raw_aliases = list(raw_aliases or [])
            seen = {str(x).strip().rstrip(".!").casefold() for x in raw_aliases if str(x).strip()}
            for base in ("Doravus", "Joruz"):
                if base.casefold() not in seen:
                    raw_aliases.extend((base, base + ".", base + "!"))
                    seen.add(base.casefold())
            wake["aliases"] = raw_aliases
            version = 121

        if version < 122:
            # v122: one response-stream transport for desktop/voice/Telegram
            # assistant turns. Existing configs that disabled streaming are
            # migrated back to the project invariant, while UI repaint/chunk
            # budgets receive conservative defaults.
            ollama = self.data.setdefault("ollama", {})
            ollama["stream_responses"] = True
            ui = self.data.setdefault("ui", {})
            ui.setdefault("response_stream_chunk_chars", 18)
            ui.setdefault("response_stream_hard_max_chars", 48)
            ui.setdefault("response_stream_flush_ms", 33)
            version = 122

        if version < 123:
            # Preserve only the wake matcher settings introduced in this historical schema.
            wake = self.data.setdefault("wake_word", {})
            wake.setdefault("phonetic_recovery_enabled", True)
            wake.setdefault("phonetic_threshold", 0.66)
            version = 123

        if version < 124:
            version = 124

        if version < 125:
            # v125: dedicated Discord music dashboard channel.  The channel name
            # is data, not a hard-coded guild assumption, and older profiles keep
            # working without any manual settings edit.
            discord = self.data.setdefault("integrations", {}).setdefault("discord", {})
            discord.setdefault("music_control_channel", "neo-music")
            version = 125

        if version < 126:
            # v126: internal Discord queue coordination no longer uses arbitrary
            # user chat channels, and voice connect retries are bounded so a bad
            # handshake cannot block My Wave for ~30 seconds.
            discord = self.data.setdefault("integrations", {}).setdefault("discord", {})
            discord.setdefault("voice_connect_timeout_seconds", 6.0)
            version = 126

        if version < 127:
            # v127 / V5.5 polish: #neo-music is the single Discord music surface.
            # Remove the temporary V5.4 sync-channel setting and merge the full
            # observed wake-word corpus into existing user profiles.
            discord = self.data.setdefault("integrations", {}).setdefault("discord", {})
            discord.pop("sync_channel", None)
            wake = self.data.setdefault("wake_word", {})
            raw_aliases = wake.setdefault("aliases", [])
            if isinstance(raw_aliases, str):
                raw_aliases = raw_aliases.splitlines()
            if not isinstance(raw_aliases, list):
                raw_aliases = list(raw_aliases or [])
            additions = (
                "джарвис",
                "There it is", "There it is.", "There it is!",
                "Service", "Service.", "Service!",
                "Choice", "Choice.", "Choice!",
                "Coice", "Coice.", "Coice!",
                "Thereitis", "Thereitis.", "Thereitis!",
                "Derwiz", "Derwiz.", "Derwiz!",
            )
            exact_seen = {str(x) for x in raw_aliases}
            for alias in additions:
                if alias not in exact_seen:
                    raw_aliases.append(alias)
                    exact_seen.add(alias)
            wake["aliases"] = raw_aliases
            version = 127

        if version < 128:
            # V5.7: startup is consolidated on one NEO_JARVIS.exe. No user
            # preference changes are required; bumping the schema makes old
            # profiles explicitly converge to the single-launcher contract.
            version = 128

        if version < 129:
            # V5.8: Windows startup is single-owner. If priority startup is
            # requested, the scheduled task becomes canonical and any legacy
            # Startup-folder launcher is removed once the task is verified.
            # The app/kernel named mutex remains the final atomic protection if
            # Windows happens to invoke two stale registrations during migration.
            version = 129

        if version < 130:
            # v130 / Adaptive Intelligence.
            #
            # Every value below has a working default, so no user has to edit
            # JSON before the first launch (spec section 81), and nothing that
            # already existed is modified (sections 79, 99).
            adaptation = self.data.setdefault("adaptation", {})
            adaptation.setdefault("enabled", True)
            # Empty means "next to the other user data"; resolved at runtime.
            adaptation.setdefault("store_path", "")
            adaptation.setdefault("max_events", 20000)
            adaptation.setdefault("dynamic_vocabulary", True)
            adaptation.setdefault("vocabulary_terms", 48)
            speech = adaptation.setdefault("speech", {})
            # A correction is applied only well above the shadow threshold: a
            # false repair costs the user a wrong answer, a missed one costs a
            # rephrase, so the asymmetry is deliberate.
            speech.setdefault("accept_threshold", 0.86)
            speech.setdefault("shadow_threshold", 0.72)
            speech.setdefault("max_corrections", 2)
            maintenance = adaptation.setdefault("maintenance", {})
            maintenance.setdefault("min_interval_seconds", 900)
            wake_learning = adaptation.setdefault("wake", {})
            wake_learning.setdefault("learning_enabled", True)
            wake_learning.setdefault("shadow_only", False)

            version = 130

        if version < 134:
            # Historical pre-v134 cleanup. Do not recreate the short-lived Tavily
            # configuration from V7.4: current releases migrate directly toward
            # SearchAPI in the next block. We keep only destructive cleanup here so
            # old browser/SerpApi/local-game fields cannot survive an upgrade.
            gaming = self.data.setdefault("gaming", {})
            gaming.pop("game_data", None)
            for retired_key in ("packs", "pack_root", "database", "knowledge", "rag", "v5", "v6"):
                gaming.pop(retired_key, None)
            web = gaming.setdefault("web_knowledge", {})
            for retired_key in (
                "search_provider", "use_search_direct_answers", "fetch_top_pages",
                "fetch_top_pages_count", "max_results", "search_timeout_seconds",
                "page_timeout_seconds", "page_max_bytes", "page_max_chars",
                "max_evidence_chars",
                "ai_mode_backend", "ai_mode_browser_path", "ai_mode_browser_timeout_seconds",
                "ai_mode_virtual_time_ms", "ai_mode_cdp_timeout_seconds",
                "ai_mode_auto_open_profile_setup", "ai_mode_require_signed_in_profile",
                "serpapi_api_key_encrypted", "serpapi_timeout_seconds",
            ):
                web.pop(retired_key, None)
            self.data.setdefault("context_memory", {})["gaming_poll_seconds"] = 4.0
            version = 134

        if version < 135:
            # v135: retire Tavily and use SearchAPI.io Google AI Mode. The app
            # stores the SearchAPI key with Windows DPAPI; environment variables
            # remain developer fallbacks only. Also apply safe CPU reductions that
            # do not slow the command/audio pipeline.
            gaming = self.data.setdefault("gaming", {})
            web = gaming.setdefault("web_knowledge", {})
            for retired_key in (
                "tavily_api_key_encrypted", "tavily_search_depth", "tavily_answer_mode",
                "tavily_max_results", "tavily_chunks_per_source", "tavily_timeout_seconds",
            ):
                web.pop(retired_key, None)
            searchapi_defaults = {
                "enabled": True,
                "searchapi_api_key_encrypted": "",
                "searchapi_gl": "us",
                "searchapi_max_sources": 8,
                "searchapi_timeout_seconds": 18.0,
                "cache_ttl_hours": 72,
                "dynamic_cache_ttl_minutes": 60,
                "stale_cache_days": 14,
                "cache_max_mb": 100,
                "similar_cache_enabled": True,
                "followup_context_seconds": 600,
                "suggest_followup": True,
                "stt_context_hints": True,
            }
            for key, value in searchapi_defaults.items():
                web.setdefault(key, value)

            wake = self.data.setdefault("wake_word", {})
            try:
                old_fast_probe = float(wake.get("fast_asr_probe_interval_seconds", 0.28) or 0.28)
            except Exception:
                old_fast_probe = 0.28
            # The first probe is still immediate. Only repeated retries are spaced
            # out, sharply reducing tiny-Whisper CPU during ambient speech/noise.
            wake["fast_asr_probe_interval_seconds"] = max(0.20, min(0.75, old_fast_probe if old_fast_probe >= 0.20 else 0.28))

            try:
                music = self.data.setdefault("desktop", {}).setdefault("overlay", {}).setdefault("styles", {}).setdefault("music", {})
                fps = int(music.get("visualizer_fps", 60) or 60)
                # Keep an explicitly lowered value; migrate the old 120 FPS default
                # to 60. Users can still select 120 manually after migration.
                if fps >= 120:
                    music["visualizer_fps"] = 60
            except Exception:
                pass
            version = 135

        if version < 136:
            # V7.6: microphone CPU + response presentation/settings cleanup.
            # Keep a cheap continuous audio/VAD loop, but bound expensive tiny-Whisper
            # rolling probes per utterance. This preserves fast wake detection while
            # preventing ambient speech from launching inference every few hundred ms.
            wake = self.data.setdefault("wake_word", {})
            wake.setdefault("fast_asr_probe_enabled", True)
            wake["fast_asr_probe_interval_seconds"] = max(0.45, float(wake.get("fast_asr_probe_interval_seconds", 0.55) or 0.55))
            wake["fast_asr_probe_min_audio_seconds"] = max(0.24, float(wake.get("fast_asr_probe_min_audio_seconds", 0.30) or 0.30))
            wake.setdefault("fast_asr_probe_max_per_utterance", 2)
            wake.setdefault("fast_asr_probe_window_seconds", 0.95)

            ui = self.data.setdefault("ui", {})
            ui.setdefault("character_streaming_enabled", True)
            ui.setdefault("typewriter_interval_ms", 8)

            web = self.data.setdefault("gaming", {}).setdefault("web_knowledge", {})
            web.pop("searchapi_ai_overview_fallback", None)
            web.setdefault("show_sources", False)
            version = 136

        if version < 137:
            # V7.7: visual smoothness is user-controlled independently from audio
            # CPU. Restore the historic 120-FPS default while retaining 30/60
            # options in Window settings.
            ui = self.data.setdefault("ui", {})
            try:
                fps = int(ui.get("animation_fps", 120) or 120)
            except Exception:
                fps = 120
            ui["animation_fps"] = fps if fps in {30, 60, 120} else 120
            try:
                music = self.data.setdefault("desktop", {}).setdefault("overlay", {}).setdefault("styles", {}).setdefault("music", {})
                music["visualizer_fps"] = ui["animation_fps"]
            except Exception:
                pass
            version = 137

        if version < 138:
            # V7.8: configurable briefing, Bluetooth notifications and compact
            # weather presentation. All defaults are backward-compatible.
            briefing = self.data.setdefault("briefing", {})
            briefing.setdefault("include_time", True)
            briefing.setdefault("include_weather", True)
            briefing.setdefault("include_system", True)
            briefing.setdefault("include_processes", False)
            briefing.setdefault("include_devices", True)
            briefing.setdefault("include_bluetooth", True)
            security = self.data.setdefault("security", {})
            security.setdefault("bluetooth_connect_notifications", True)
            security.setdefault("bluetooth_battery_notifications", True)
            security.setdefault("bluetooth_battery_threshold_percent", 20)
            self.data.setdefault("ui", {}).setdefault("robot_info_temperature_show_city", True)
            version = 138

        if version < 139:
            # V7.9: presentation-only migration. Briefing controls moved to Window,
            # optional panel controls moved to Additional, and every non-ephemeral
            # desktop assistant message now shares the configured typewriter lane.
            self.data.setdefault("ui", {}).setdefault("character_streaming_enabled", True)
            version = 139

        if version < 140:
            # HF90: microphone standby CPU budget. One fast tiny-Whisper probe is
            # enough for low-latency wake promotion; the robust end-of-utterance
            # wake pass remains as fallback. A single wake STT CPU thread also
            # prevents ambient speech from creating large CPU spikes.
            wake = self.data.setdefault("wake_word", {})
            wake["fast_asr_probe_max_per_utterance"] = 1
            wake["cpu_threads"] = 1
            version = 140

        if version < 141:
            hotkeys = self.data.setdefault("hotkeys", {})
            hotkeys.setdefault("autoclicker_toggle", "Ctrl+Alt+C")
            hotkeys.setdefault("chat_scroll_up", "Ctrl+Alt+Up")
            hotkeys.setdefault("chat_scroll_down", "Ctrl+Alt+Down")
            clicker = self.data.setdefault("autoclicker", {})
            clicker.setdefault("enabled", True)
            clicker.setdefault("cps", 10.0)
            clicker.setdefault("button", "left")
            self.data.setdefault("ui", {}).setdefault("voice_window_visibility_commands_enabled", True)
            version = 141

        if version < 142:
            # HOTFIX93: global topmost toggle and first-class Gmail/Mail.ru
            # service configuration. Secrets are stored only in DPAPI-protected
            # settings fields; fresh mail monitors establish a baseline before
            # showing new-message notifications.
            self.data.setdefault("hotkeys", {}).setdefault("window_topmost_toggle", "Ctrl+Alt+T")
            integrations = self.data.setdefault("integrations", {})
            gmail = integrations.setdefault("gmail", {})
            gmail.setdefault("enabled", False)
            gmail.setdefault("client_id_encrypted", "")
            gmail.setdefault("client_secret_encrypted", "")
            gmail.setdefault("refresh_token_encrypted", "")
            gmail.setdefault("access_token_encrypted", "")
            gmail.setdefault("token_expires_at", 0.0)
            gmail.setdefault("account_email", "")
            gmail.setdefault("notifications_enabled", True)
            gmail.setdefault("notify_new_count", True)
            gmail.setdefault("notify_unread_count", True)
            gmail.setdefault("notify_recent24h_count", False)
            gmail.setdefault("notification_detail", "sender")
            gmail.setdefault("include_in_briefing", True)
            gmail.setdefault("briefing_show_senders", False)
            gmail.setdefault("poll_seconds", 60)
            gmail.setdefault("timeout_seconds", 10.0)
            mailru = integrations.setdefault("mailru", {})
            mailru.setdefault("enabled", False)
            mailru.setdefault("email", "")
            mailru.setdefault("app_password_encrypted", "")
            mailru.setdefault("notifications_enabled", True)
            mailru.setdefault("notify_new_count", True)
            mailru.setdefault("notify_unread_count", True)
            mailru.setdefault("notify_recent24h_count", False)
            mailru.setdefault("notification_detail", "sender")
            mailru.setdefault("include_in_briefing", True)
            mailru.setdefault("briefing_show_senders", False)
            mailru.setdefault("poll_seconds", 60)
            mailru.setdefault("timeout_seconds", 10.0)
            version = 142

        if version < 143:
            # HOTFIX94: configurable mail folders/labels, aggregate read-only
            # queries across Gmail + Mail.ru, richer display formats and startup
            # briefing-first presentation. Legacy "new since poll" counters are
            # retired; notifications can optionally show total mailbox count.
            integrations = self.data.setdefault("integrations", {})
            for provider in ("gmail", "mailru"):
                mail_cfg = integrations.setdefault(provider, {})
                # During v142 -> v143 migration prefer the legacy field when it
                # exists. Defaults may already have injected message_display_detail,
                # but the user-selected v142 notification_detail must win.
                old_detail = str(mail_cfg.get("notification_detail", mail_cfg.get("message_display_detail", "sender_subject")) or "sender_subject")
                if old_detail not in {"count_only", "sender", "sender_subject", "sender_preview", "sender_subject_preview"}:
                    old_detail = "sender_subject"
                mail_cfg.setdefault("include_inbox", True)
                mail_cfg.setdefault("additional_folders", [])
                mail_cfg.setdefault("notify_total_count", False)
                mail_cfg.setdefault("notify_unread_count", True)
                mail_cfg["notify_recent24h_count"] = bool(mail_cfg.get("notify_recent24h_count", True))
                mail_cfg["message_display_detail"] = old_detail
                mail_cfg.setdefault("briefing_show_folders", True)
                mail_cfg.pop("notify_new_count", None)
                mail_cfg.pop("notification_detail", None)
            version = 143

        # Runtime invariant: even a hand-edited current profile
        # cannot restore any retired local gameplay-data configuration.
        gaming_runtime = self.data.setdefault("gaming", {})
        if not isinstance(gaming_runtime, dict):
            gaming_runtime = {}
            self.data["gaming"] = gaming_runtime
        for retired_key in ("game_data", "packs", "pack_root", "database", "knowledge", "rag", "v5", "v6"):
            gaming_runtime.pop(retired_key, None)

        # Interactive safety invariant.  Even a hand-edited/current-version
        # settings.json cannot silently restore Ollama's infinite -1 generation
        # mode for the desktop assistant.
        ollama = self.data.setdefault("ollama", {})
        try:
            bounded_predict = int(ollama.get("num_predict", 512) or 512)
        except Exception:
            bounded_predict = 512
        ollama["num_predict"] = max(64, min(8192, bounded_predict if bounded_predict > 0 else 512))
        ollama["unlimited_response"] = False
        try:
            bounded_first_token = float(ollama.get("first_token_timeout_seconds", 15) or 15)
        except Exception:
            bounded_first_token = 15.0
        ollama["first_token_timeout_seconds"] = max(8.0, min(20.0, bounded_first_token))
        discord_runtime = self.data.setdefault("integrations", {}).setdefault("discord", {})
        discord_runtime.setdefault("music_control_channel", "neo-music")
        discord_runtime.pop("sync_channel", None)
        try:
            voice_timeout = float(discord_runtime.get("voice_connect_timeout_seconds", 6.0) or 6.0)
        except Exception:
            voice_timeout = 6.0
        discord_runtime["voice_connect_timeout_seconds"] = max(3.0, min(10.0, voice_timeout))
        if version < 151:
            # HOTFIX106: strengthen the English/Russian wake family with short
            # Whisper variants observed in real logs.  These are aliases only;
            # command routing still uses the selected Phrase Atlas locale.
            wake_bases = (
                "Jarvis", "Jari", "Jaris", "Jariz", "Zariz", "Zaris", "Zarvis",
                "Djarvis", "Dzharvis", "Jervis", "Garvis", "Yarvis",
                "Джарвис", "Джарвиз", "Жарвис", "Зарвиз",
            )
            for section_name, key in (("wake_word", "aliases"), ("speech", "wake_aliases"), ("voice", "wake_aliases")):
                section = self.data.setdefault(section_name, {})
                values = section.get(key, []) or []
                if isinstance(values, str):
                    values = values.splitlines()
                existing = {str(item or "").strip().casefold() for item in values}
                for base in wake_bases:
                    for suffix in ("", ".", "!"):
                        alias = base + suffix
                        if alias.casefold() not in existing:
                            values.append(alias); existing.add(alias.casefold())
                section[key] = values
            # "Dar isso" is a real Portuguese phrase, so keep that Whisper
            # misrecognition language-scoped instead of polluting the PT/global
            # wake corpus. It was observed while the English UI was active.
            wake = self.data.setdefault("wake_word", {})
            scoped = wake.setdefault("language_aliases", {})
            for lang in ("en", "ru"):
                vals = scoped.get(lang, []) or []
                if isinstance(vals, str): vals = vals.splitlines()
                existing = {str(x or "").strip().casefold() for x in vals}
                for base in ("Dar isso", "Darisso", "Darizo"):
                    if base.casefold() not in existing:
                        vals.append(base); existing.add(base.casefold())
                scoped[lang] = vals
            version = 151

        # Current-version invariant: retired provider knobs stay gone even
        # if a hand-edited profile tries to restore them.
        web_runtime = gaming_runtime.setdefault("web_knowledge", {})
        if isinstance(web_runtime, dict):
            for retired_key in (
                "ai_mode_backend", "ai_mode_browser_path", "ai_mode_browser_timeout_seconds",
                "ai_mode_virtual_time_ms", "ai_mode_cdp_timeout_seconds",
                "ai_mode_auto_open_profile_setup", "ai_mode_require_signed_in_profile",
                "serpapi_api_key_encrypted", "serpapi_timeout_seconds",
                "tavily_api_key_encrypted", "tavily_search_depth", "tavily_answer_mode",
                "tavily_max_results", "tavily_chunks_per_source", "tavily_timeout_seconds",
                "searchapi_ai_overview_fallback",
            ):
                web_runtime.pop(retired_key, None)
            web_runtime.setdefault("searchapi_api_key_encrypted", "")
            web_runtime.setdefault("searchapi_gl", "us")
            web_runtime.setdefault("searchapi_max_sources", 8)
            web_runtime.setdefault("searchapi_timeout_seconds", 18.0)
            web_runtime.setdefault("show_sources", False)
        _integrations = self.data.setdefault("integrations", {})
        _integrations.pop("google_translate", None)
        _cal = _integrations.setdefault("google_calendar", {})
        for _retired in ("use_gmail_oauth_client", "client_id_encrypted", "client_secret_encrypted"):
            _cal.pop(_retired, None)
        _cal.setdefault("include_in_briefing", False)
        _cal.setdefault("briefing_period", "today")
        _tg = _integrations.setdefault("telegram", {})
        _tg.setdefault("outbound_allowed_people", [])
        _tg.setdefault("outbound_allowed_groups", [])
        self.data.setdefault("developer_mode", {}).setdefault("settings_enabled", False)
        # HOTFIX99 invariant for migrated/current profiles: one and only one
        # enabled desktop music provider. If no provider is enabled, music
        # commands fail closed until the user enables one in Media settings.
        _music = _integrations.setdefault("music", {})
        _providers = ("yandex_music", "spotify")
        _enabled = [name for name in _providers if bool((_integrations.get(name) or {}).get("enabled", False))]
        _active = str(_music.get("active_provider", "") or "").strip().casefold()
        if _active not in _providers or _active not in _enabled:
            _active = _enabled[0] if _enabled else ""
        for _name in _providers:
            _integrations.setdefault(_name, {})["enabled"] = bool(_active and _name == _active)
        _music["active_provider"] = _active
        self.data.setdefault("desktop", {})["preferred_music_app"] = _active or "yandex_music"
        # HOTFIX116: packaged identity/wake defaults are language-neutral English values.
        # Migrate only historical packaged defaults; preserve genuine user customizations.
        if version < 155:
            _assistant_name = str(self.data.get("assistant_name", "") or "").strip()
            if not _assistant_name or _assistant_name.casefold() in {"джарвис", "jarvis"}:
                self.data["assistant_name"] = DEFAULT_ASSISTANT_NAME
            _old_user = str(self.data.get("user_name", "") or "").strip()
            if not _old_user or _old_user.casefold() in {"босс", "boss"}:
                self.data["user_name"] = DEFAULT_USER_NAME
            _wake116 = self.data.setdefault("wake_word", {})
            if str(_wake116.get("phrase", "") or "").strip().casefold() in {"", "джарвис", "jarvis"}:
                _wake116["phrase"] = "Jarvis"
            if str(_wake116.get("acoustic_model_phrase", "") or "").strip().casefold() in {"", "джарвис", "jarvis"}:
                _wake116["acoustic_model_phrase"] = "Jarvis"
            _known_packaged_prompts = {
                "Ты десктопный ассистент и друг пользователя. Помогай с компьютером, но общайся естественно и по-дружески: можно обсуждать игры, музыку, планы, как прошёл день, чем пользователь занимался, как у него дела, или просто разговаривать. Будь спокойным, ненавязчивым и полезным.",
                DEFAULT_ASSISTANT_PROMPT,
            }
            _current_prompt116 = str(self.data.get("assistant_prompt", "") or "").strip()
            if not _current_prompt116 or _current_prompt116 in _known_packaged_prompts or re.search(r"[А-Яа-яЁё]", _current_prompt116):
                self.data["assistant_prompt"] = DEFAULT_ASSISTANT_PROMPT
            version = 155
        if version < 156:
            # HOTFIX119: experimental ONNX/openWakeWord detector is retired from
            # user settings. The stable isolated Whisper wake path remains.
            self.data.setdefault("wake_word", {})["acoustic_enabled"] = False
            version = 156
        if version < 157:
            # HOTFIX120-LATENCY: one very-early probe can fire before the complete
            # wake word exists. Keep CPU bounded at two attempts, but allow a
            # second rescue while the user is still speaking instead of waiting
            # for end-of-utterance robust ASR. Existing profiles receive the
            # latency fix without overriding a user's already-faster values.
            _wake120 = self.data.setdefault("wake_word", {})
            try:
                _wake120["silence_seconds"] = min(float(_wake120.get("silence_seconds", 0.16) or 0.16), 0.12)
            except Exception:
                _wake120["silence_seconds"] = 0.12
            try:
                _wake120["fast_asr_probe_interval_seconds"] = min(float(_wake120.get("fast_asr_probe_interval_seconds", 0.55) or 0.55), 0.24)
            except Exception:
                _wake120["fast_asr_probe_interval_seconds"] = 0.24
            try:
                _wake120["fast_asr_probe_min_audio_seconds"] = min(float(_wake120.get("fast_asr_probe_min_audio_seconds", 0.30) or 0.30), 0.22)
            except Exception:
                _wake120["fast_asr_probe_min_audio_seconds"] = 0.22
            _wake120["fast_asr_probe_max_per_utterance"] = max(2, min(3, int(_wake120.get("fast_asr_probe_max_per_utterance", 1) or 1)))
            _wake120["fast_probe_language"] = "auto"
            try:
                _wake120["fast_asr_probe_window_seconds"] = min(float(_wake120.get("fast_asr_probe_window_seconds", 0.95) or 0.95), 0.90)
            except Exception:
                _wake120["fast_asr_probe_window_seconds"] = 0.90
            _tg120 = self.data.setdefault("integrations", {}).setdefault("telegram", {})
            try:
                _tg120["min_command_interval_seconds"] = min(float(_tg120.get("min_command_interval_seconds", 0.35) or 0.35), 0.15)
            except Exception:
                _tg120["min_command_interval_seconds"] = 0.15
            version = 157
        if version < 158:
            # HOTFIX123: lifelong ambient intelligence. Defaults are deliberately
            # resource-bounded: microphone learning reuses wake ASR, system-audio
            # STT is tiny/CPU/one-thread and starts only on an explicit loopback
            # endpoint while the resource governor allows background work.
            _ambient = self.data.setdefault("ambient_learning", {})
            _ambient_defaults = {
                "enabled": True,
                "microphone_enabled": True,
                "system_context_enabled": True,
                "system_audio_activity_enabled": True,
                "system_audio_activity_poll_seconds": 5.0,
                "system_audio_transcription_enabled": True,
                "system_audio_device": "auto",
                "system_audio_start_delay_seconds": 0.75,
                "system_audio_sensitivity": 78,
                "system_audio_chunk_seconds": 8.0,
                "system_audio_silence_seconds": 0.85,
                "system_audio_min_speech_seconds": 1.2,
                "system_audio_min_stt_gap_seconds": 6.0,
                "stt_model": "tiny",
                "stt_cpu_threads": 1,
                "stt_idle_unload_seconds": 120.0,
                "pause_while_assistant_busy": True,
                "pause_during_game": True,
                "max_background_cpu_percent": 48.0,
                "min_text_chars": 6,
                "learned_term_min_count": 2,
                "prompt_context_limit": 5,
                "observation_ttl_days": 60,
                "max_observations": 6000,
                "store_raw_audio": False,
            }
            for _key, _value in _ambient_defaults.items():
                _ambient.setdefault(_key, _value)
            version = 158
        if version < 159:
            # HOTFIX124: hard microphone/system-audio provenance boundary. System
            # playback is true WASAPI loopback only and is used for passive
            # learning/echo reference, never as a command transport.
            _ambient = self.data.setdefault("ambient_learning", {})
            _ambient.setdefault("system_audio_require_true_loopback", True)
            _ambient.setdefault("system_echo_guard_enabled", True)
            _ambient.setdefault("system_echo_similarity_threshold", 0.84)
            _ambient.setdefault("system_echo_reference_horizon_seconds", 1.65)
            try:
                if float(_ambient.get("system_audio_start_delay_seconds", 12.0) or 12.0) >= 12.0:
                    _ambient["system_audio_start_delay_seconds"] = 0.75
            except Exception:
                _ambient["system_audio_start_delay_seconds"] = 0.75
            version = 159
        if version < 160:
            # HOTFIX125: UI identity is NEO everywhere except the dedicated wake
            # phrase settings. Also migrate existing profiles onto the faster but
            # still two-probe bounded wake cadence and expose new opt-in quick
            # music controls without changing their other media configuration.
            _name125 = str(self.data.get("assistant_name", "") or "").strip()
            if not _name125 or _name125.casefold() in {"jarvis", "neo jarvis", "джарвис"}:
                self.data["assistant_name"] = "NEO"
            _wake125 = self.data.setdefault("wake_word", {})
            try:
                _wake125["fast_asr_probe_interval_seconds"] = min(float(_wake125.get("fast_asr_probe_interval_seconds", 0.24) or 0.24), 0.20)
            except Exception:
                _wake125["fast_asr_probe_interval_seconds"] = 0.20
            try:
                _wake125["fast_asr_probe_min_audio_seconds"] = min(float(_wake125.get("fast_asr_probe_min_audio_seconds", 0.22) or 0.22), 0.18)
            except Exception:
                _wake125["fast_asr_probe_min_audio_seconds"] = 0.18
            try:
                _wake125["probe_interval_seconds"] = min(float(_wake125.get("probe_interval_seconds", 0.18) or 0.18), 0.14)
            except Exception:
                _wake125["probe_interval_seconds"] = 0.14
            _voice125 = self.data.setdefault("voice", {})
            _voice125.setdefault("quick_media_volume_enabled", True)
            _voice125.setdefault("quick_media_repeat_shuffle_enabled", True)
            _ui125 = self.data.setdefault("ui", {})
            _ui125.setdefault("timer_overlay_opacity_percent", 88)
            _ui125.setdefault("timer_overlay_style", "glass")
            _ui125.setdefault("timer_overlay_text_color", "#EAF2FF")
            _ui125.setdefault("timer_overlay_background_color", "#0A0F18")
            _ui125.setdefault("timer_overlay_border_color", "#57D7FF")
            version = 160

        if version < 161:
            # HOTFIX142-SPEECH-ARCHITECTURE: tune *useful* wake probes rather than
            # firing Whisper on an incomplete ~100 ms syllable. Two serialized
            # attempts stay CPU-bounded; the UI keeps the freshest second probe
            # instead of dropping it while the first decode is in flight. Command
            # endpoint hangover is intentionally a little longer so quiet Russian
            # word endings survive capture before Whisper sees the utterance.
            _voice161 = self.data.setdefault("voice", {})
            _voice161["short_command_silence_seconds"] = 0.40
            _voice161["normal_command_silence_seconds"] = 0.68
            try:
                _voice161["silence_seconds"] = max(1.05, float(_voice161.get("silence_seconds", 1.05) or 1.05))
            except Exception:
                _voice161["silence_seconds"] = 1.05
            _wake161 = self.data.setdefault("wake_word", {})
            _wake161["silence_seconds"] = 0.12
            _wake161["probe_interval_seconds"] = 0.18
            _wake161["fast_asr_probe_interval_seconds"] = 0.18
            _wake161["fast_asr_probe_min_audio_seconds"] = 0.22
            _wake161["fast_asr_probe_window_seconds"] = 0.78
            _wake161["fast_asr_probe_max_per_utterance"] = 2
            _wake161["cpu_threads"] = 1
            version = 161

        if version < 162:
            # HOTFIX143: restore the pre-regression command decoder quality,
            # accelerate the isolated tiny wake probe, and stop contextual
            # vocabulary from behaving like an autocorrect engine for arbitrary
            # speech.  Existing profiles receive the same values as new installs.
            _voice162 = self.data.setdefault("voice", {})
            _voice162["speech_adaptive_rewrite_enabled"] = False
            _voice162["speech_understanding_entity_threshold"] = max(
                0.86, float(_voice162.get("speech_understanding_entity_threshold", 0.86) or 0.86)
            )
            _voice162["speech_understanding_hypothesis_margin"] = max(
                0.14, float(_voice162.get("speech_understanding_hypothesis_margin", 0.14) or 0.14)
            )
            _wake162 = self.data.setdefault("wake_word", {})
            _wake162["silence_seconds"] = 0.12
            _wake162["probe_interval_seconds"] = 0.14
            _wake162["fast_asr_probe_interval_seconds"] = 0.14
            _wake162["fast_asr_probe_min_audio_seconds"] = 0.15
            _wake162["fast_asr_probe_window_seconds"] = 0.72
            _wake162["fast_asr_probe_max_per_utterance"] = 2
            _wake162["cpu_threads"] = 1
            _storage162 = self.data.setdefault("storage", {})
            _root162 = self._bootstrap_root
            _storage162.update({
                "data_dir": str(_root162),
                "logs_dir": str(_root162 / "logs"),
                "screenshots_dir": str(_root162 / "screenshots"),
                "backups_dir": str(_root162 / "backups"),
                "cache_dir": str(_root162 / "cache"),
            })
            version = 162


        # HOTFIX110/116: the direct-address name is user data; new packaged default is Boss.
        _user_name = str(self.data.get("user_name", DEFAULT_USER_NAME) or "").strip()
        self.data["user_name"] = _user_name[:80] or DEFAULT_USER_NAME
        # HOTFIX111: personal addressing is opt-out for backward compatibility.
        # Existing profiles that already configured user_name keep the previous
        # behavior, while the new checkbox can disable all name-based salutations.
        self.data["user_name_enabled"] = bool(self.data.get("user_name_enabled", True))
        # HOTFIX133 CPU invariant: visual rendering is capped at 60 FPS even when
        # an older profile still contains the historical 120-FPS value. Audio/ASR
        # sampling is independent, so this removes redundant GUI wakeups without
        # slowing recognition or playback.
        _ui = self.data.setdefault("ui", {})
        try:
            _fps = int(_ui.get("animation_fps", 60) or 60)
        except Exception:
            _fps = 60
        _ui["animation_fps"] = _fps if _fps in {30, 60} else 60
        try:
            _music = self.data.setdefault("desktop", {}).setdefault("overlay", {}).setdefault("styles", {}).setdefault("music", {})
            _music["visualizer_fps"] = min(60, max(30, int(_music.get("visualizer_fps", _ui["animation_fps"]) or _ui["animation_fps"])))
        except Exception:
            pass

        # Keep the known stable profile for legacy installs. Once migrated, an
        # explicit acoustic opt-in with a locally validated model survives save.
        if version < 163:
            self.data.setdefault("wake_word", {})["acoustic_enabled"] = False
            if str(self.data.get("assistant_name","")).casefold() in {"neo","jarvis","джарвис",""}:
                self.data["assistant_name"] = "Jarvis"
        self.data.setdefault("app", {})["display_name"] = "NEO"
        ollama163=self.data.setdefault("ollama", {})
        from jarvis.runtime.vision_policy import migrate_vision_startup
        migrate_vision_startup(ollama163, settings_version=version)
        security163=self.data.setdefault("security", {})
        for key,value in {"smart_downloads_enabled":False,"vpn_notifications_enabled":False,"latency_notifications_enabled":False,"latency_threshold_ms":250}.items():
            security163.setdefault(key,value)
        self.data.setdefault("downloads", {}).setdefault("directories", [])
        activity163=self.data.setdefault("activity", {})
        for key,value in {"enabled":True,"retention_days":30,"directories":[],"excluded_paths":[],"excluded_apps":["keepass.exe","keepassxc.exe","1password.exe","bitwarden.exe"]}.items():
            activity163.setdefault(key,value)
        self.data.setdefault("dialogue", {}).setdefault("style","friendly")
        memory145 = self.data["dialogue"].setdefault("memory", {})
        for key, value in {"enabled": True, "retention_days": 7, "max_turns": 200}.items():
            memory145.setdefault(key, value)
        shelf145 = self.data.setdefault("shelf", {})
        for key, value in {
            "section_width": 360, "section_height": 420,
            "cell_width": 180, "cell_height": 100, "cell_shape": "rectangle",
            "font_family": "Segoe UI", "font_size": 14,
            "font_bold": False, "font_italic": False, "text_alignment": "left",
            "editor_width": 420, "editor_height": 360, "layout": {}, "layout_editable": True,
            "background_color": "#172536", "background_opacity_percent": 85,
        }.items():
            shelf145.setdefault(key, value)
        # Generic addressing inherited from the old persona is not a user name.
        # Keep explicitly chosen personal names and allow opting in again later.
        if version < 165 and _user_name.casefold() in {"boss", "босс"}:
            self.data["user_name_enabled"] = False
        self.data.setdefault("file_index", {}).setdefault("enabled",True)
        # HOTFIX115-NO-NEWS: preserve the Daily Briefing itself, but remove the
        # retired gaming/world-news subtree from existing HOTFIX113/114 profiles.
        _brief = self.data.setdefault("briefing", {})
        _brief.setdefault("repeat_on_every_start", False)
        _brief.setdefault("include_services", False)
        _brief.pop("news", None)
        # NEO 168: on-screen control keys and performance profiles (missing keys only).
        from jarvis.runtime import settings168
        settings168.migrate(self.data, version)
        # NEO 169: local conversation, memory, file search, plugins, command model (missing keys only).
        from jarvis.runtime import settings169
        settings169.migrate(self.data, version, settings_path=self.paths.settings, backups=self.paths.backups)
        # NEO 170: the small NEO agent model and its local fine-tuning (missing keys only).
        from jarvis.runtime import settings170
        settings170.migrate(self.data, version)
        # NEO 171: automatic Instant Replay at game start, structured web answers (missing keys only).
        from jarvis.runtime import settings171
        settings171.migrate(self.data, version)
        # NEO 172: screen help, streaming answers, memory offers, measured learning (missing keys only).
        from jarvis.runtime import settings172
        settings172.migrate(self.data, version)
        # NEO 173: faster services, the NEO model only while needed, context (missing keys; one default).
        from jarvis.runtime import settings173
        settings173.migrate(self.data, version)
        # NEO 174: faster speech recognition and typo repair (missing keys only).
        from jarvis.runtime import settings174
        settings174.migrate(self.data, version)
        # NEO 175: speech recognition back to the configured CPU threads.
        from jarvis.runtime import settings175
        settings175.migrate(self.data, version)
        # NEO 177: learned typos, bounded Yandex network recovery, the cigarette option.
        from jarvis.runtime import settings177
        settings177.migrate(self.data, version)
        # NEO 178: the wake word is NEO; the Jarvis wake word and its spellings are retired.
        from jarvis.runtime import wake178
        wake178.migrate(self.data, version)
        # NEO 179: no spoken answers — the voice output settings are removed.
        from jarvis.runtime import settings179
        settings179.migrate(self.data, version)
        # NEO 184: Settings → Translation (the local translation model and its options).
        from jarvis.runtime import settings184
        settings184.migrate(self.data, version)
        self.data["settings_version"] = 179

    def reset_to_defaults(self, *, keep_connections: bool = False) -> dict[str, Any]:
        """Restore the packaged settings atomically and refresh runtime paths.

        NEO 184: a full reset also removes the connections (tokens, keys, Client IDs) —
        they are written as the defaults instead of being carried over from the disk.
        ``keep_connections`` keeps the integrations section and the SearchAPI key as they are."""
        defaults = self._read_json(self.default_settings_path)

        def rebuild(current: dict[str, Any]) -> None:
            # HIST-026: ``current`` is the newest file on disk, read under the same
            # save lock that writes the reset; a credential stored a moment ago by
            # another writer is kept, never replaced by an older in-memory snapshot.
            fresh = json.loads(json.dumps(defaults))
            if keep_connections:
                if isinstance(current.get("integrations"), dict):
                    fresh["integrations"] = current["integrations"]
                streaming = current.get("streaming")
                if isinstance(streaming, dict):
                    for name in ("enabled", "host", "port", "password_encrypted"):
                        if name in streaming:
                            fresh.setdefault("streaming", {})[name] = streaming[name]
                key = ((current.get("gaming") or {}).get("web_knowledge") or {}).get("searchapi_api_key_encrypted")
                if key:
                    fresh.setdefault("gaming", {}).setdefault("web_knowledge", {})["searchapi_api_key_encrypted"] = key
            current.clear()
            current.update(fresh)

        self.update(rebuild, credentials=True)
        if not keep_connections:
            from jarvis.integrations.obs_service184 import set_session_password
            set_session_password(self, "")
        self.reload()
        return self.data

    def stored_credentials(self) -> list[str]:
        """Dotted names of non-empty secrets currently stored on disk (used to verify a reset)."""
        try:
            disk = json.loads(self.paths.settings.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        found: list[str] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    dotted = f"{path}.{key}" if path else key
                    if isinstance(value, dict):
                        walk(value, dotted)
                    elif key.endswith("_encrypted") and value:
                        found.append(dotted)
        walk(disk, "")
        return found

    def _write_standard_autostart_launcher(self, gui_launcher: Path) -> Path:
        startup = Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        startup.mkdir(parents=True, exist_ok=True)
        launcher = startup / "NEO.vbs"
        target = str(gui_launcher).replace('"', '""')
        launcher.write_text(
            'Set WshShell = CreateObject("WScript.Shell")\n'
            'WshShell.Environment("PROCESS")("NEO_JARVIS_AUTOSTART") = "1"\n'
            f'WshShell.Run """{target}""", 0, False\n',
            encoding="utf-16",  # WScript preserves Unicode package paths.
        )
        # The old display name must not leave a second Startup entry active.
        (startup / "NEO_JARVIS.vbs").unlink(missing_ok=True)
        return launcher

    def refresh_autostart_target(self) -> bool:
        """Refresh an enabled autostart path to this extracted release without UAC.

        Updating archives must not leave Startup/priority launchers pointing at an
        older folder. This method never creates an elevated task; it only refreshes
        an existing priority trampoline or rewrites the standard per-user VBS.
        """
        if os.name != "nt" or not bool(self.get("ui.start_with_windows", False)):
            return False
        from jarvis.runtime.distribution164 import project_root as distribution_root
        project_root = distribution_root()
        gui_launcher = project_root / "NEO.exe"
        if not gui_launcher.exists():
            return False
        priority = bool(self.get("ui.priority_startup", False))
        if priority:
            try:
                from jarvis.windows_startup import refresh_priority_launcher
                if refresh_priority_launcher(gui_launcher):
                    # A working priority task owns autostart; remove a stale
                    # standard fallback so two NEO processes cannot race at logon.
                    startup = Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
                    (startup / "NEO.vbs").unlink(missing_ok=True)
                    (startup / "NEO_JARVIS.vbs").unlink(missing_ok=True)
                    (startup / "NEO_JARVIS.cmd").unlink(missing_ok=True)
                    return True
            except Exception:
                pass
        # Standard mode, or a missing/broken priority task: preserve autostart by
        # refreshing the silent per-user fallback to this release.
        try:
            self._write_standard_autostart_launcher(gui_launcher)
            startup = Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
            (startup / "NEO_JARVIS.cmd").unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def apply_wake_service(self) -> bool:
        if os.name != "nt":
            return False
        try:
            from jarvis.runtime.distribution164 import project_root as distribution_root
            project_root = distribution_root()
            gui_launcher = project_root / "NEO.exe"
            wake_executable = project_root / "NEO_WAKE_SERVICE.exe"
            telegram_enabled = bool(self.get("integrations.telegram.enabled", False))
            remote_enabled = bool(self.get("integrations.telegram.remote_control_enabled", False))
            wake_enabled = bool(self.get("integrations.telegram.remote_wake_enabled", True))
            from jarvis.windows_startup import ensure_wake_service
            return bool(ensure_wake_service(
                wake_executable, gui_launcher,
                enabled=telegram_enabled and remote_enabled and wake_enabled,
            ))
        except Exception:
            return False

    def refresh_wake_service_target(self) -> bool:
        """Refresh/start the wake companion for this extracted release."""
        return self.apply_wake_service()

    def apply_autostart(self) -> None:
        if os.name != "nt":
            return
        startup = Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        startup.mkdir(parents=True, exist_ok=True)
        legacy_cmd = startup / "NEO_JARVIS.cmd"
        launcher = startup / "NEO.vbs"
        from jarvis.runtime.distribution164 import project_root as distribution_root
        project_root = distribution_root()
        gui_launcher = project_root / "NEO.exe"
        enabled = bool(self.get("ui.start_with_windows", False))
        priority = bool(self.get("ui.priority_startup", False)) and enabled

        # Priority mode is an interactive ONLOGON scheduled task with RL=HIGHEST.
        # It starts as soon as Windows creates the user's desktop session and does
        # not flash cmd/PowerShell. Creation may require one UAC consent.
        priority_ready = False
        try:
            from jarvis.windows_startup import ensure_priority_task, remove_priority_task
            if priority:
                priority_ready = bool(ensure_priority_task(gui_launcher))
            else:
                remove_priority_task()
        except Exception:
            priority_ready = False

        # Standard startup remains a silent WScript fallback. If a requested
        # priority task could not be created, keep this fallback so autostart is
        # never lost just because UAC was declined.
        use_standard = enabled and (not priority or not priority_ready)
        if use_standard:
            self._write_standard_autostart_launcher(gui_launcher)
        else:
            launcher.unlink(missing_ok=True)
        try:
            legacy_cmd.unlink(missing_ok=True)
            (startup / "NEO_JARVIS.vbs").unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8-sig"))

    @classmethod
    def _deep_merge(cls, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = cls._deep_merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    def _expand_allowed_roots(self) -> None:
        replacements = {
            "{HOME}": Path.home(),
            "{DESKTOP}": Path.home() / "Desktop",
            "{DOCUMENTS}": Path.home() / "Documents",
            "{DOWNLOADS}": Path.home() / "Downloads",
            "{PICTURES}": Path.home() / "Pictures",
            "{VIDEOS}": Path.home() / "Videos",
            "{MUSIC}": Path.home() / "Music",
        }
        roots = self.data.setdefault("security", {}).setdefault("allowed_roots", [])
        expanded: list[str] = []
        for item in roots:
            path = str(item)
            for token, replacement in replacements.items():
                path = path.replace(token, str(replacement))
            expanded.append(str(Path(path).expanduser().resolve(strict=False)))
        self.data["security"]["allowed_roots"] = expanded

    @staticmethod
    def _collapse_root(value: str) -> str:
        path = str(Path(value).expanduser().resolve(strict=False))
        mapping = {
            str((Path.home() / "Desktop").resolve(strict=False)): "{DESKTOP}",
            str((Path.home() / "Documents").resolve(strict=False)): "{DOCUMENTS}",
            str((Path.home() / "Downloads").resolve(strict=False)): "{DOWNLOADS}",
            str((Path.home() / "Pictures").resolve(strict=False)): "{PICTURES}",
            str((Path.home() / "Videos").resolve(strict=False)): "{VIDEOS}",
            str((Path.home() / "Music").resolve(strict=False)): "{MUSIC}",
            str(Path.home().resolve(strict=False)): "{HOME}",
        }
        return mapping.get(path, value)
