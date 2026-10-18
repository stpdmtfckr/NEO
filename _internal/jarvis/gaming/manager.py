# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Lightweight process-only game session detection.

This module deliberately does not read game saves, logs, plugins, process memory,
or game-specific state.  It only verifies a running executable, identifies a small
set of common titles (or local store metadata), and measures process uptime.
"""

import difflib
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import psutil



@dataclass
class GameSnapshot:
    game_id: str
    game_name: str
    pid: int
    process_name: str
    executable: str = ""
    game_version: str = ""
    game_mode: str = ""
    session_started_at: float = 0.0
    session_seconds: int = 0
    capabilities: list[str] = field(default_factory=lambda: ["session"])
    notes: list[str] = field(default_factory=list)
    foreground_verified: bool = False
    game_name_source: str = "unspecified"
    game_name_status: str = "unverified"
    game_store_app_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GameProcess:
    pid: int
    name: str
    exe: str
    create_time: float
    cmdline: tuple[str, ...] = ()


@dataclass(frozen=True)
class GameAdapter:
    game_id: str
    display_name: str
    process_names: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ("session",)
    process_markers: tuple[str, ...] = ()
    marker_required_processes: tuple[str, ...] = ()
    game_name_source: str = "builtin_process_signature"
    game_name_status: str = "verified"
    game_store_app_id: str = ""

    def matches(self, process_name: str) -> bool:
        name = str(process_name or "").casefold()
        return any(name == candidate.casefold() for candidate in self.process_names)

    def matches_process(self, process: GameProcess) -> bool:
        if not self.matches(process.name):
            return False
        required = {str(x).casefold() for x in self.marker_required_processes}
        if str(process.name or "").casefold() in required:
            text = " ".join(process.cmdline).casefold()
            return any(str(marker).casefold() in text for marker in self.process_markers)
        return True

    def snapshot(self, process: GameProcess) -> GameSnapshot:
        # Session identity is derived only from the verified process. Gameplay
        # version/mode knowledge belongs to live web retrieval, not process heuristics.
        return GameSnapshot(
            game_id=self.game_id,
            game_name=self.display_name,
            pid=process.pid,
            process_name=process.name,
            executable=process.exe,
            session_started_at=process.create_time,
            session_seconds=max(0, int(time.time() - process.create_time)),
            capabilities=list(self.capabilities),
            game_name_source=self.game_name_source,
            game_name_status=self.game_name_status,
            game_store_app_id=self.game_store_app_id,
        )


def _foreground_pid() -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value or 0) or None
    except Exception:
        return None


class GameManager:
    """Process-only detector used by the optional session timer/overlay."""

    _KNOWN: tuple[GameAdapter, ...] = (
        GameAdapter(
            "slay_the_spire", "Slay the Spire",
            ("SlayTheSpire.exe", "SlayTheSpire"),
            ("slay the spire", "slay spire", "слей зе спайр", "слей спайр"),
        ),
        GameAdapter(
            "elden_ring", "Elden Ring",
            ("eldenring.exe", "eldenring"),
            ("elden ring", "элден ринг", "елден ринг"),
        ),
        GameAdapter(
            "world_of_warcraft", "World of Warcraft",
            ("Wow.exe", "WowClassic.exe", "WowClassicT.exe"),
            ("world of warcraft", "wow", "вау", "ворлд оф варкрафт"),
        ),
        GameAdapter(
            "darktide", "Warhammer 40,000: Darktide",
            ("Darktide.exe", "darktide.exe", "Darktide"),
            ("darktide", "dark tide", "warhammer 40k darktide", "warhammer 40000 darktide", "warhammer 40 000 darktide", "дарктайд", "дарк тайд", "вархаммер дарктайд"),
            ("session", "web_knowledge"),
        ),
        GameAdapter(
            "warframe", "Warframe",
            ("Warframe.x64.exe", "Warframe.exe"),
            ("warframe", "war frame", "варфрейм", "вар фрейм", "варфрэйм", "варфрем", "ворфрейм"),
            ("session", "web_knowledge"),
        ),
        GameAdapter(
            "minecraft", "Minecraft Java Edition",
            ("javaw.exe", "java.exe", "Minecraft.exe", "MinecraftLauncher.exe"),
            ("minecraft", "minecraft java", "майнкрафт", "майнкрафт java", "майн крафт", "майнкраф"),
            ("session", "web_knowledge"),
        ),
        GameAdapter(
            "tarkov", "Escape from Tarkov",
            ("EscapeFromTarkov.exe",),
            ("escape from tarkov", "tarkov", "eft", "тарков", "эскейп фром тарков"),
            ("session", "web_knowledge"),
        ),
    )

    _IGNORED_PROCESSES = {
        "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "goggalaxy.exe",
        "gamingservices.exe", "gamebar.exe", "gamebarftserver.exe", "explorer.exe",
        "neo_jarvis.exe", "neo.exe", "neo_wake_service.exe", "python.exe", "pythonw.exe", "ollama.exe",
    }

    # Storefront folders can contain non-game utilities.  They must never become
    # the implicit game namespace merely because they live under Steam/Epic/GOG.
    # Wallpaper Engine caused a real production failure where a Psyker question
    # was rewritten as "wallpaper engine ...".  Keep this denylist focused on
    # persistent utility/background products; actual unknown games remain
    # detectable when their window is foreground.
    _STORE_UTILITY_DENY_TOKENS = {
        "wallpaper engine", "wallpaper_engine", "wallpaperengine",
        "lossless scaling", "borderless gaming", "fps monitor",
        "steamvr", "steam vr", "soundpad", "obs studio",
    }

    @staticmethod
    def _is_generic_store_adapter(adapter: GameAdapter | None) -> bool:
        return bool(adapter is not None and str(adapter.game_id or "").startswith("store_"))

    def __init__(self, *, enabled: bool = True, config=None) -> None:
        self.enabled = bool(enabled)
        self._config = config
        self.adapters: list[GameAdapter] = list(self._KNOWN)

    def refresh_adapters(self, *, force: bool = False, interval_seconds: float = 10.0) -> bool:
        """Refresh the built-in process signatures; no knowledge files are consulted."""
        before = tuple((a.game_id, a.process_names, a.aliases, a.capabilities) for a in self.adapters)
        self.adapters = list(self._KNOWN)
        after = tuple((a.game_id, a.process_names, a.aliases, a.capabilities) for a in self.adapters)
        return before != after

    @staticmethod
    def _normalize_query(value: str) -> str:
        text = str(value or "").casefold().replace("ё", "е")
        text = re.sub(r"[^a-zа-я0-9]+", " ", text, flags=re.I)
        return re.sub(r"\s+", " ", text).strip()

    def adapter_for_query(self, query: str) -> GameAdapter | None:
        q = self._normalize_query(query)
        if not q:
            return None
        best: tuple[float, GameAdapter] | None = None
        for adapter in self.adapters:
            values = [adapter.display_name, adapter.game_id.replace("_", " "), *adapter.aliases]
            values.extend(Path(name).stem for name in adapter.process_names)
            score = 0.0
            for value in values:
                v = self._normalize_query(value)
                if not v:
                    continue
                if q == v:
                    score = max(score, 1.0)
                elif q in v or v in q:
                    score = max(score, 0.93)
                else:
                    score = max(score, difflib.SequenceMatcher(None, q, v).ratio())
            if best is None or score > best[0]:
                best = (score, adapter)
        return best[1] if best and best[0] >= 0.68 else None

    def _processes(self) -> Iterable[GameProcess]:
        # Windows cmdline() reads another process's parameters and is not
        # cached by psutil. The old sweep did that for every PID, every time.
        # Only adapters with an ambiguous executable need those parameters.
        marker_names = {name.casefold() for adapter in self.adapters
                        for name in adapter.marker_required_processes}
        for proc in psutil.process_iter(["pid", "name", "exe", "create_time"]):
            try:
                info = proc.info
                name = str(info.get("name") or "")
                if name.casefold() in self._IGNORED_PROCESSES:
                    continue
                command = ()
                if name.casefold() in marker_names:
                    try:
                        command = tuple(str(x) for x in (proc.cmdline() or ()))
                    except (psutil.Error, OSError):
                        pass  # Missing marker evidence must fail closed.
                yield GameProcess(
                    pid=int(info.get("pid") or proc.pid),
                    name=name,
                    exe=str(info.get("exe") or ""),
                    create_time=float(info.get("create_time") or proc.create_time()),
                    cmdline=command,
                )
            except (psutil.Error, OSError, ValueError, TypeError):
                continue

    @classmethod
    def _store_adapter(cls, process: GameProcess, *, language: str = "en") -> GameAdapter | None:
        name = process.name.casefold()
        if not process.exe or name in cls._IGNORED_PROCESSES:
            return None
        normalized = process.exe.replace("/", "\\")
        low = normalized.casefold()
        markers = ("\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\", "\\xboxgames\\")
        marker = next((m for m in markers if m in low), "")
        if not marker:
            return None
        try:
            tail = normalized[low.index(marker) + len(marker):]
            folder = tail.split("\\", 1)[0].strip() or Path(process.exe).stem
        except Exception:
            folder = Path(process.exe).stem
        display = folder.replace("_", " ").strip() or Path(process.exe).stem or "Игра"
        utility_haystack = " ".join((display, folder, process.name, Path(process.exe).stem)).casefold()
        if any(token in utility_haystack for token in cls._STORE_UTILITY_DENY_TOKENS):
            return None
        safe_id = re.sub(r"[^a-z0-9]+", "_", display.casefold()).strip("_") or "store_game"
        # Keep the existing directory-derived ID/aliases: saved preferences and
        # session identity must not migrate when stronger display evidence appears.
        from jarvis.gaming.store_metadata import StoreTitle, steam_title_for
        title = steam_title_for(process.exe) if marker == "\\steamapps\\common\\" else StoreTitle(status="unsupported_store_metadata")
        if title.name and any(token in title.name.casefold() for token in cls._STORE_UTILITY_DENY_TOKENS):
            return None
        # Directory names are untrusted presentation input, not product names.
        from jarvis.language import game_directory_label
        folder_label = " ".join("".join(c if c.isprintable() else " " for c in display).split())[:96] or "?"
        public_name = title.name or game_directory_label(folder_label, language)
        return GameAdapter(
            f"store_{safe_id}", public_name, (process.name,), (display,),
            game_name_source=title.source, game_name_status=title.status,
            game_store_app_id=title.app_id,
        )

    def detect_all(self) -> list[tuple[GameAdapter, GameProcess]]:
        """Return all process-verified games from one process enumeration.

        Selection policy belongs to GamePresenceService.  Keeping discovery and
        arbitration separate avoids rescanning psutil several times and, more
        importantly, prevents `newest process wins` from silently becoming the
        definition of the user's active game.
        """
        if not self.enabled:
            return []
        matches: list[tuple[GameAdapter, GameProcess]] = []
        for process in self._processes():
            adapter = next((item for item in self.adapters if item.matches_process(process)), None)
            if adapter is None:
                from jarvis.language import language_code
                adapter = self._store_adapter(process, language=language_code(self._config))
            if adapter is not None:
                matches.append((adapter, process))
        matches.sort(key=lambda item: item[1].create_time, reverse=True)
        return matches

    def detect(self, preferred_pid: int | None = None, *, require_preferred: bool = False) -> tuple[GameAdapter, GameProcess] | None:
        matches = self.detect_all()
        if not matches:
            return None
        if preferred_pid:
            preferred = next((item for item in matches if item[1].pid == preferred_pid), None)
            if preferred is not None:
                return preferred
            if require_preferred:
                return None
        elif require_preferred:
            return None
        return matches[0]

    def wait_for_game(self, query: str = "", *, timeout: float = 30.0) -> tuple[GameAdapter, GameProcess] | None:
        expected = self.adapter_for_query(query) if query else None
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            found = self.detect()
            if found is not None:
                adapter, _process = found
                if expected is None or adapter.game_id == expected.game_id:
                    return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.20)

    def wait_for_snapshot(self, query: str = "", *, timeout: float = 15.0) -> GameSnapshot | None:
        found = self.wait_for_game(query, timeout=timeout)
        if found is None:
            return None
        adapter, process = found
        return adapter.snapshot(process)

    def snapshot(self, preferred_pid: int | None = None, *, require_preferred: bool = False) -> GameSnapshot | None:
        found = self.detect(preferred_pid, require_preferred=require_preferred)
        if found is None:
            return None
        adapter, process = found
        return adapter.snapshot(process)

    def supported_games(self) -> list[dict[str, object]]:
        return [
            {
                "id": adapter.game_id,
                "name": adapter.display_name,
                "process_names": list(adapter.process_names),
                "capabilities": list(adapter.capabilities),
            }
            for adapter in self.adapters
        ]

    @staticmethod
    def short_name(snap: GameSnapshot | str, game_id: str = "") -> str:
        if isinstance(snap, GameSnapshot):
            game_id = snap.game_id
            name = snap.game_name
        else:
            name = str(snap or "Игра")
        mapping = {
            "darktide": "Darktide",
            "warframe": "Warframe",
            "minecraft": "Minecraft",
            "tarkov": "Tarkov",
            "elden_ring": "Elden Ring",
            "world_of_warcraft": "WoW",
            "slay_the_spire": "Slay the Spire",
        }
        return mapping.get(str(game_id or "").casefold(), name[:42])

    @staticmethod
    def format_duration(seconds: int) -> str:
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours} ч {minutes:02d} мин {secs:02d} с"
        if minutes:
            return f"{minutes} мин {secs:02d} с"
        return f"{secs} с"

    @classmethod
    def session_text(cls, snap: GameSnapshot) -> str:
        return cls.simple_session_text(snap)

    @classmethod
    def simple_session_text(cls, snap: GameSnapshot) -> str:
        try:
            started = datetime.fromtimestamp(float(snap.session_started_at)).strftime("%H:%M:%S")
        except Exception:
            started = "—"
        return "\n".join([
            f"Игра: {snap.game_name}",
            f"Запущена: {started}",
            f"Сессия: {cls.format_duration(snap.session_seconds)}",
        ])

# ---------------------------------------------------------------------------
# Process-global live game presence cache.
# ---------------------------------------------------------------------------

class GamePresenceService:
    """Cheap authoritative view of the currently running game.

    Process detection is centralised below language routing so a freshly started
    game is available to the very first user question. Any caller
    can ask for a snapshot; a short process-global TTL prevents repeated psutil
    enumerations across Desktop/Telegram/other Agent instances.  The returned
    GameSnapshot is process evidence, never a language-model guess.
    """

    def __init__(self, manager: GameManager | None = None) -> None:
        import threading
        self._lock = threading.RLock()
        self._manager = manager or GameManager(enabled=True)
        self._snapshot: GameSnapshot | None = None
        self._checked_monotonic = 0.0
        self._foreground_checked_pid = None

    def snapshot(
        self,
        *,
        enabled: bool = True,
        max_age_seconds: float = 1.75,
        force: bool = False,
        preferred_pid: int | None = None,
        preferred_game_id: str = "",
    ) -> GameSnapshot | None:
        now = time.monotonic()
        ttl = max(0.10, float(max_age_seconds))
        fg_pid = _foreground_pid()
        with self._lock:
            if not enabled:
                self._snapshot = None
                self._checked_monotonic = now
                self._foreground_checked_pid = fg_pid
                return None
            cache_fresh = bool(self._checked_monotonic and now - self._checked_monotonic <= ttl
                               and fg_pid == self._foreground_checked_pid)
            if not force and cache_fresh:
                cached = self._snapshot
                # A newly process-verified tracker hint is stronger than an old
                # cached ambiguity/selection. Never let the TTL hide a changed
                # preferred PID/game on the very turn where the user asks.
                pid_matches = not preferred_pid or (cached is not None and int(cached.pid or 0) == int(preferred_pid))
                gid = str(preferred_game_id or "").casefold()
                game_matches = not gid or (cached is not None and str(cached.game_id or "").casefold() == gid)
                identity_valid = True
                if cached is not None and os.name == 'nt':
                    from jarvis.gaming.runtime import process_identity_matches
                    identity_valid = process_identity_matches(cached.as_dict())
                if pid_matches and game_matches and identity_valid:
                    return cached
            self._manager.enabled = bool(enabled)
            snap = None
            if enabled:
                matches = self._manager.detect_all()
                # Any process-verified game may provide the active context for
                # Google AI Mode; no game-specific knowledge capability is needed.
                candidates = matches
                chosen = None
                if fg_pid:
                    chosen = next((m for m in candidates if m[1].pid == fg_pid), None)
                if chosen is not None:
                    snap = chosen[0].snapshot(chosen[1])
                    snap.foreground_verified = True

                # Voice/UI focus can move to the JARVIS overlay immediately
                # before a query arrives.  In that case the foreground HWND no
                # longer belongs to the game, but the low-frequency tracker may
                # already have a fresh process-verified PID.  Treat that PID as
                # evidence, not as a language-model guess.
                allow_background = not fg_pid or fg_pid == os.getpid()
                if snap is None and allow_background and preferred_pid:
                    preferred = next((m for m in candidates if m[1].pid == int(preferred_pid)
                        and (not preferred_game_id or m[0].game_id.casefold()==str(preferred_game_id).casefold())), None)
                    if preferred is not None:
                        snap = preferred[0].snapshot(preferred[1])

                # A caller may know only the previously verified game id (for
                # example after a process restart changed PID).  Accept it only
                # when it identifies exactly one live candidate.
                if snap is None and allow_background and preferred_game_id:
                    gid = str(preferred_game_id or "").casefold()
                    preferred_games = [m for m in candidates if str(m[0].game_id or "").casefold() == gid]
                    if len(preferred_games) == 1:
                        snap = preferred_games[0][0].snapshot(preferred_games[0][1])

                # When JARVIS itself owns foreground focus, preserve the last
                # process-verified game if its PID is still alive. This prevents
                # scope hopping between concurrently running games.
                if snap is None and allow_background and self._snapshot is not None:
                    prev_pid = int(self._snapshot.pid or 0)
                    previous = next((m for m in candidates if m[1].pid == prev_pid
                        and abs(float(m[1].create_time)-float(self._snapshot.session_started_at))<.01
                        and m[0].game_id==self._snapshot.game_id), None)
                    if previous is not None:
                        snap = previous[0].snapshot(previous[1])
                # Known game signatures may safely seed context without a
                # foreground HWND. Generic storefront processes are intentionally
                # stricter: they must have been foreground/preferred/previously
                # verified above. This prevents persistent utilities living in
                # Steam folders from becoming the active game namespace.
                known_candidates = [m for m in candidates if not self._manager._is_generic_store_adapter(m[0])]
                if snap is None and allow_background and len(known_candidates) == 1:
                    snap = known_candidates[0][0].snapshot(known_candidates[0][1])
            self._snapshot = snap
            self._foreground_checked_pid = fg_pid
            self._checked_monotonic = time.monotonic()
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._checked_monotonic = 0.0

    def adopt_manager(self, manager: GameManager) -> None:
        """Reuse an already-created detector when the tracker owns one."""
        if not isinstance(manager, GameManager):
            return
        with self._lock:
            if self._manager is manager:
                return
            equivalent = (type(self._manager) is GameManager and type(manager) is GameManager
                          and self._manager.enabled == manager.enabled
                          and self._manager.adapters == manager.adapters)
            self._manager = manager
            # Desktop/Telegram agents construct equivalent built-in managers.
            # Re-adopting one must not invalidate the shared TTL on every turn.
            if not equivalent:
                self._checked_monotonic = 0.0


_PRESENCE_SERVICE: GamePresenceService | None = None
_PRESENCE_LOCK = None


def game_presence_service(manager: GameManager | None = None) -> GamePresenceService:
    global _PRESENCE_SERVICE, _PRESENCE_LOCK
    if _PRESENCE_LOCK is None:
        import threading
        _PRESENCE_LOCK = threading.RLock()
    with _PRESENCE_LOCK:
        if _PRESENCE_SERVICE is None:
            _PRESENCE_SERVICE = GamePresenceService(manager)
        elif manager is not None:
            _PRESENCE_SERVICE.adopt_manager(manager)
        return _PRESENCE_SERVICE
