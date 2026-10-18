"""NVIDIA Instant Replay clips and recordings through the overlay hotkeys.

NEO never talks to NVIDIA services directly: it presses the overlay hotkeys the
user already uses (NVIDIA App, defaults Alt+F10 "save the
last minutes" and Alt+F9 "start/stop recording") and then looks for the new video
file so it can say where the clip was saved. The clip length is whatever Instant
Replay is set to in the NVIDIA overlay (for example 5 minutes).
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov"}
# The app UI can run with the actual in-game overlay disabled.
OVERLAY_PROCESSES = {"nvidia overlay.exe"}
DEFAULT_CLIP_HOTKEY = "alt+f10"
DEFAULT_RECORD_HOTKEY = "alt+f9"

_VK = {"alt": 0x12, "ctrl": 0x11, "control": 0x11, "shift": 0x10, "win": 0x5B}
_VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})
_VK.update({chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)})
_VK.update({str(n): 0x30 + n for n in range(10)})

MESSAGES = {
    "clip_saved": ("Клип сохранён: {path}", "Clip saved: {path}", "Clip gespeichert: {path}",
                   "Clip enregistré : {path}", "Clip guardado: {path}", "Clip salvata: {path}", "Clip guardado: {path}"),
    "clip_folder": ("Клип сохраняется через NVIDIA в папку: {folder}", "NVIDIA is saving the clip to: {folder}",
                    "NVIDIA speichert den Clip in: {folder}", "NVIDIA enregistre le clip dans : {folder}",
                    "NVIDIA guarda el clip en: {folder}", "NVIDIA sta salvando la clip in: {folder}",
                    "A NVIDIA está a guardar o clip em: {folder}"),
    "record_started": ("Запись NVIDIA начата. Чтобы остановить, скажите «стоп запись».",
                       "NVIDIA recording started. Say “stop recording” to finish.",
                       "NVIDIA-Aufnahme gestartet. Sag „Aufnahme stoppen“, um sie zu beenden.",
                       "Enregistrement NVIDIA lancé. Dites « arrête l’enregistrement » pour terminer.",
                       "Grabación de NVIDIA iniciada. Di «detén la grabación» para terminar.",
                       "Registrazione NVIDIA avviata. Di «ferma la registrazione» per terminare.",
                       "Gravação NVIDIA iniciada. Diz «para a gravação» para terminar."),
    "record_already": ("Запись уже идёт. Чтобы остановить, скажите «стоп запись».",
                       "Recording is already running. Say “stop recording” to finish.",
                       "Die Aufnahme läuft bereits. Sag „Aufnahme stoppen“, um sie zu beenden.",
                       "L’enregistrement est déjà en cours. Dites « arrête l’enregistrement ».",
                       "La grabación ya está en curso. Di «detén la grabación».",
                       "La registrazione è già in corso. Di «ferma la registrazione».",
                       "A gravação já está a decorrer. Diz «para a gravação»."),
    "record_saved": ("Запись остановлена и сохранена: {path}", "Recording stopped and saved: {path}",
                     "Aufnahme gestoppt und gespeichert: {path}", "Enregistrement arrêté et sauvegardé : {path}",
                     "Grabación detenida y guardada: {path}", "Registrazione fermata e salvata: {path}",
                     "Gravação parada e guardada: {path}"),
    "record_folder": ("Запись остановлена. NVIDIA сохраняет её в папку: {folder}",
                      "Recording stopped. NVIDIA is saving it to: {folder}",
                      "Aufnahme gestoppt. NVIDIA speichert sie in: {folder}",
                      "Enregistrement arrêté. NVIDIA le sauvegarde dans : {folder}",
                      "Grabación detenida. NVIDIA la guarda en: {folder}",
                      "Registrazione fermata. NVIDIA la salva in: {folder}",
                      "Gravação parada. A NVIDIA está a guardá-la em: {folder}"),
    "record_not_started": ("Я не запускал запись в этом сеансе, поэтому не нажимаю {hotkey}: это могло бы начать новую запись. Если запись идёт, нажмите {hotkey}.",
                           "I did not start a recording in this session, so I won't press {hotkey}: that could start a new one. If a recording is running, press {hotkey}.",
                           "Ich habe in dieser Sitzung keine Aufnahme gestartet und drücke {hotkey} daher nicht, sonst könnte eine neue beginnen. Läuft eine Aufnahme, drück {hotkey}.",
                           "Je n’ai pas lancé d’enregistrement dans cette session, donc je n’appuie pas sur {hotkey} : cela pourrait en démarrer un nouveau. S’il y en a un en cours, appuyez sur {hotkey}.",
                           "No inicié ninguna grabación en esta sesión, así que no pulso {hotkey}: podría empezar otra. Si hay una en curso, pulsa {hotkey}.",
                           "Non ho avviato registrazioni in questa sessione, quindi non premo {hotkey}: potrebbe avviarne una nuova. Se una è in corso, premi {hotkey}.",
                           "Não iniciei nenhuma gravação nesta sessão, por isso não carrego em {hotkey}: poderia começar outra. Se houver uma a decorrer, carrega em {hotkey}."),
    "overlay_missing": ("Оверлей NVIDIA не запущен. Включите «Внутриигровой оверлей» в NVIDIA App (для клипов ещё и «Мгновенный повтор») и повторите.",
                        "The NVIDIA overlay is not running. Turn on the in-game overlay in NVIDIA App (and Instant Replay for clips), then try again.",
                        "Das NVIDIA-Overlay läuft nicht. Aktiviere das In-Game-Overlay in der NVIDIA App (für Clips auch Instant Replay) und versuche es erneut.",
                        "L’overlay NVIDIA n’est pas lancé. Activez l’overlay en jeu dans NVIDIA App (et la relecture instantanée pour les clips), puis réessayez.",
                        "La superposición de NVIDIA no está activa. Activa la superposición en el juego en NVIDIA App (y la repetición instantánea para clips) y vuelve a intentarlo.",
                        "L’overlay NVIDIA non è attivo. Attiva l’overlay di gioco in NVIDIA App (e il replay istantaneo per le clip), poi riprova.",
                        "A sobreposição NVIDIA não está ativa. Ativa a sobreposição no jogo na NVIDIA App (e a repetição instantânea para clips) e tenta de novo."),
    "unsupported": ("Клипы и запись NVIDIA работают только в Windows.", "NVIDIA clips and recording work only on Windows.",
                    "NVIDIA-Clips und -Aufnahmen funktionieren nur unter Windows.",
                    "Les clips et enregistrements NVIDIA ne fonctionnent que sous Windows.",
                    "Los clips y grabaciones de NVIDIA solo funcionan en Windows.",
                    "Clip e registrazioni NVIDIA funzionano solo su Windows.",
                    "Os clips e gravações NVIDIA só funcionam no Windows."),
    "bad_hotkey": ("Не понимаю горячую клавишу «{hotkey}» в настройках NVIDIA.", "I can't read the hotkey “{hotkey}” in the NVIDIA settings.",
                   "Ich verstehe das Tastenkürzel „{hotkey}“ in den NVIDIA-Einstellungen nicht.",
                   "Je ne comprends pas le raccourci « {hotkey} » dans les réglages NVIDIA.",
                   "No entiendo el atajo «{hotkey}» de los ajustes de NVIDIA.",
                   "Non capisco la scorciatoia «{hotkey}» nelle impostazioni NVIDIA.",
                   "Não percebo o atalho «{hotkey}» nas definições NVIDIA."),
}
MESSAGES.update({
    "overlay_missing": ('Не удалось подтвердить доступность оверлея NVIDIA. Проверьте Alt+Z и настройку внутриигрового оверлея.', 'Could not confirm that the NVIDIA overlay is available. Check Alt+Z and the in-game overlay setting.', 'Verfügbarkeit des NVIDIA-Overlays nicht bestätigt. Alt+Z und die In-Game-Overlay-Einstellung prüfen.', 'Disponibilité de l’overlay NVIDIA non confirmée. Vérifiez Alt+Z et son activation dans les paramètres.', 'No se confirmó la disponibilidad de la superposición NVIDIA. Revisa Alt+Z y su configuración.', 'Disponibilità dell’overlay NVIDIA non confermata. Controlla Alt+Z e le impostazioni dell’overlay.', 'Disponibilidade da sobreposição NVIDIA não confirmada. Verifica Alt+Z e as definições da sobreposição.'),
    "clip_folder": ("Отправлена команда сохранения клипа NVIDIA, но новый файл не найден. Проверьте оверлей и папку: {folder}", "Sent the NVIDIA save-clip command, but no new file was found. Check the overlay and folder: {folder}", "NVIDIA-Clipbefehl gesendet, aber keine neue Datei gefunden. Overlay und Ordner prüfen: {folder}", "Commande de clip NVIDIA envoyée, mais aucun nouveau fichier trouvé. Vérifiez l’overlay et le dossier : {folder}", "Comando de clip NVIDIA enviado, pero no se encontró un archivo nuevo. Revisa la superposición y la carpeta: {folder}", "Comando clip NVIDIA inviato, ma nessun nuovo file trovato. Controlla overlay e cartella: {folder}", "Comando de clip NVIDIA enviado, mas nenhum ficheiro novo encontrado. Verifica a sobreposição e a pasta: {folder}"),
    "record_folder": ("Запись NVIDIA остановлена, но сохранённый файл не найден. Проверьте папку: {folder}", "NVIDIA recording stopped, but no saved file was found. Check the folder: {folder}", "NVIDIA-Aufnahme gestoppt, aber keine gespeicherte Datei gefunden. Ordner prüfen: {folder}", "Enregistrement NVIDIA arrêté, mais aucun fichier enregistré trouvé. Vérifiez le dossier : {folder}", "Grabación NVIDIA detenida, pero no se encontró un archivo guardado. Revisa la carpeta: {folder}", "Registrazione NVIDIA fermata, ma nessun file salvato trovato. Controlla la cartella: {folder}", "Gravação NVIDIA parada, mas nenhum ficheiro guardado encontrado. Verifica a pasta: {folder}"),
    "record_unknown": ("Не могу проверить, идёт ли запись NVIDIA. Не нажимаю {hotkey}: это переключатель. Проверьте запись в оверлее Alt+Z.", "I can't check whether NVIDIA is recording. I won't press {hotkey}: it is a toggle. Check recording in the Alt+Z overlay.", "NVIDIA-Aufnahmestatus unbekannt. Ich drücke {hotkey} nicht: Es schaltet um. Prüfe die Aufnahme im Alt+Z-Overlay.", "État d’enregistrement NVIDIA inconnu. Je n’appuie pas sur {hotkey} : c’est une bascule. Vérifiez dans l’overlay Alt+Z.", "Estado de grabación NVIDIA desconocido. No pulso {hotkey}: es una alternancia. Comprueba el estado en Alt+Z.", "Stato registrazione NVIDIA sconosciuto. Non premo {hotkey}: è un interruttore. Controlla nell’overlay Alt+Z.", "Estado de gravação NVIDIA desconhecido. Não carrego em {hotkey}: é um interruptor. Verifica na sobreposição Alt+Z."),
    "record_unverified": ("Отправил {hotkey}, но не могу подтвердить изменение записи NVIDIA. Проверьте оверлей Alt+Z.", "Sent {hotkey}, but could not verify the NVIDIA recording change. Check the Alt+Z overlay.", "{hotkey} gesendet, Änderung der NVIDIA-Aufnahme nicht bestätigt. Alt+Z-Overlay prüfen.", "{hotkey} envoyé, mais changement d’enregistrement NVIDIA non confirmé. Vérifiez l’overlay Alt+Z.", "Se envió {hotkey}, pero no se confirmó el cambio de grabación NVIDIA. Revisa Alt+Z.", "Inviato {hotkey}, ma modifica della registrazione NVIDIA non confermata. Controlla Alt+Z.", "Enviado {hotkey}, mas não foi confirmada a alteração da gravação NVIDIA. Verifica Alt+Z."),
    "record_already_off": ("Запись NVIDIA уже выключена.", "NVIDIA recording is already off.", "NVIDIA-Aufnahme ist bereits aus.", "L’enregistrement NVIDIA est déjà arrêté.", "La grabación NVIDIA ya está detenida.", "La registrazione NVIDIA è già ferma.", "A gravação NVIDIA já está parada."),
    "invalid_action": ("Неизвестная команда NVIDIA; клавиши не нажаты.", "Unknown NVIDIA action; no keys were pressed.", "Unbekannter NVIDIA-Befehl; keine Taste gedrückt.", "Commande NVIDIA inconnue ; aucune touche utilisée.", "Acción de NVIDIA desconocida; no se pulsaron teclas.", "Comando NVIDIA sconosciuto; nessun tasto premuto.", "Ação NVIDIA desconhecida; nenhuma tecla premida."),
    "press_failed": ("Не удалось отправить {hotkey} в NVIDIA. Проверьте оверлей и права доступа.", "Could not send {hotkey} to NVIDIA. Check the overlay and access permissions.", "{hotkey} konnte nicht an NVIDIA gesendet werden. Overlay und Zugriffsrechte prüfen.", "Impossible d’envoyer {hotkey} à NVIDIA. Vérifiez l’overlay et les autorisations.", "No se pudo enviar {hotkey} a NVIDIA. Revisa la superposición y los permisos.", "Impossibile inviare {hotkey} a NVIDIA. Controlla l’overlay e i permessi.", "Não foi possível enviar {hotkey} à NVIDIA. Verifica a sobreposição e as permissões."),
})

_LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")


def message(key: str, code: str, **values: Any) -> str:
    row = MESSAGES[key]
    text = row[_LANGUAGES.index(code)] if code in _LANGUAGES else row[1]
    return text.format(**values)


def parse_hotkey(combo: str) -> tuple[int, ...]:
    parts = str(combo or "").replace(" ", "").casefold().split("+")
    keys = tuple(_VK.get(part, -1) for part in parts)
    modifiers = {0x12, 0x11, 0x10, 0x5B}
    if not keys or -1 in keys or len(set(keys)) != len(keys) or keys[-1] in modifiers or any(key not in modifiers for key in keys[:-1]):
        raise ValueError(combo)
    return keys


def press_hotkey(combo: str) -> None:
    """Release every pressed key even if the injected adapter raises mid-chord.

    keybd_event has no acknowledgement from NVIDIA. Callers must verify the
    requested result separately rather than treating injection as success.
    """
    import ctypes
    keys = parse_hotkey(combo)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    pressed: list[int] = []
    try:
        for key in keys:
            pressed.append(key)
            user32.keybd_event(key, 0, 0, 0)
            time.sleep(0.02)
    finally:
        first_error = None
        for key in reversed(pressed):
            try:
                user32.keybd_event(key, 0, 2, 0)
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def overlay_running() -> bool | None:
    """Process evidence for overlay availability, never evidence of recording."""
    try:
        import psutil
        unknown = False
        for process in psutil.process_iter(["name"]):
            try:
                if str(process.info.get("name") or "").casefold() in OVERLAY_PROCESSES:
                    return True
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                unknown = True
        return None if unknown else False
    except Exception:
        return None


def video_folders(configured: str = "") -> list[Path]:
    roots: list[Path] = []
    if configured:
        roots.append(Path(os.path.expandvars(configured)).expanduser())
    try:
        from jarvis.runtime.file_index import known_folders
        roots.extend(Path(p) for p in known_folders("Videos"))
    except Exception:
        pass
    roots.append(Path.home() / "Videos")
    unique: list[Path] = []
    for root in roots:
        if root.is_dir() and root not in unique:
            unique.append(root)
    return unique


def _recent_videos(roots: Iterable[Path], since: float, depth: int = 3) -> list[tuple[float, int, Path]]:
    found = []
    visited = 0

    def walk(folder: Path, level: int) -> None:
        nonlocal visited
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 10000:
                        return
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if level < depth:
                                walk(Path(entry.path), level + 1)
                        elif Path(entry.name).suffix.casefold() in VIDEO_SUFFIXES:
                            stat = entry.stat()
                            if stat.st_mtime >= since - 1:
                                found.append((stat.st_mtime, stat.st_size, Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            return

    for root in roots:
        walk(root, 0)
    return sorted(found, reverse=True)


def wait_for_new_video(roots: list[Path], since: float, timeout: float,
                       sleep: Callable[[float], None] = time.sleep, *,
                       baseline: dict[Path, tuple[float, int]] | None = None,
                       clock: Callable[[], float] = time.monotonic) -> Path | None:
    """New/changed, nonempty video stable on two observations before timeout.

    Existing unchanged files are not evidence of this request. A file still
    growing when the timeout expires is not reported as a completed save.
    """
    import math
    timeout = float(timeout)
    timeout = max(0.0, min(30.0, timeout)) if math.isfinite(timeout) else 12.0
    deadline = clock() + timeout
    previous: tuple[Path, float, int] | None = None
    baseline = baseline or {}
    for _ in range(int(timeout / 0.5) + 2):
        rows = [row for row in _recent_videos(roots, since) if baseline.get(row[2]) != row[:2]]
        if rows:
            modified, size, path = rows[0]
            observation = (path, modified, size)
            if previous == observation and size > 0:
                return path
            previous = observation
        else:
            previous = None
        if clock() >= deadline:
            break
        sleep(0.5)
    return None


@dataclass
class _State:
    recording: bool | None = None
    started_at: float = 0.0


class NvidiaCapture:
    """Guard stateful toggles with actual observations; intent is not evidence.

    NVIDIA does not provide a documented recording-state reader in this
    integration. Start/stop require the read-only live-state UIA adapter;
    versions not exposing that state are safely refused and need Windows QA.
    """

    def __init__(self, config: Any, *, press: Callable[[str], None] = press_hotkey,
                 overlay: Callable[[], bool | None] = overlay_running, is_windows: bool | None = None,
                 wait: Callable[..., Path | None] = wait_for_new_video,
                 recording_reader: Callable[[], bool | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 ensure_overlay: Callable[[], bool] | None = None) -> None:
        self.config = config
        self._press = press
        if ensure_overlay is None:
            from jarvis.integrations.nvidia_startup184 import ensure_overlay_for_command
            ensure_overlay = lambda: ensure_overlay_for_command(self.config, self._overlay, sleep=self._sleep)
        self._ensure_overlay = ensure_overlay
        self._overlay = overlay
        self._windows = (os.name == "nt") if is_windows is None else is_windows
        self._wait = wait
        from jarvis.integrations.nvidia_state import recording_state
        self._recording_reader = recording_reader or recording_state
        self._sleep = sleep
        self._lock = threading.Lock()
        self.state = _State()

    def _get(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    def _code(self) -> str:
        code = str(self.config.get("app.language", "ru") or "ru").casefold().split("-")[0]
        return code if code in _LANGUAGES else "en"

    def _folders(self) -> list[Path]:
        return video_folders(str(self._get("capture.video_folder", "")))

    def _folder_label(self, folders: list[Path]) -> str:
        return str(folders[0]) if folders else str(Path.home() / "Videos")

    def _recording(self) -> bool | None:
        try:
            state = self._recording_reader() if self._recording_reader else None
            return state if isinstance(state, bool) else None
        except Exception:
            logger.debug("NVIDIA recording state unavailable", exc_info=True)
            return None

    def _overlay_ready(self) -> bool:
        try:
            return self._overlay() is True
        except Exception:
            return False

    def run(self, action: str) -> tuple[bool, str, dict[str, Any]]:
        code = self._code()
        if action not in {"clip", "record_start", "record_stop"}:
            return False, message("invalid_action", code), {"error_kind": "invalid", "pressed": False}
        if not self._windows:
            return False, message("unsupported", code), {"error_kind": "disabled", "pressed": False}
        clip_key = str(self.config.get("capture.nvidia_clip_hotkey", DEFAULT_CLIP_HOTKEY) or "")
        record_key = str(self.config.get("capture.nvidia_record_hotkey", DEFAULT_RECORD_HOTKEY) or "")
        with self._lock:
            hotkey = clip_key if action == "clip" else record_key
            try:
                parse_hotkey(hotkey)
            except ValueError:
                return False, message("bad_hotkey", code, hotkey=hotkey), {"error_kind": "invalid", "pressed": False}
            if (bool(self._get("capture.require_overlay", True)) and not self._overlay_ready()
                    and not self._ensure_overlay()):
                # NEO 185: with «launch NVIDIA App for commands» on, NEO starts it in the background first.
                return False, message("overlay_missing", code), {"error_kind": "disabled", "pressed": False}
            want = action == "record_start"
            if action != "clip":
                current = self._recording() if self._overlay_ready() else None
                self.state.recording = current
                if current is None:
                    return False, message("record_unknown", code, hotkey=hotkey.upper()), {"error_kind": "unverified", "recording": None, "pressed": False, "verified": False}
                if current is want:
                    return True, message("record_already" if want else "record_already_off", code), {"recording": current, "pressed": False, "verified": True}
            folders = self._folders()
            started = time.time()
            baseline = {p: (modified, size) for modified, size, p in _recent_videos(folders, started)} if self._wait is wait_for_new_video else {}
            try:
                self._press(hotkey)
            except Exception:
                logger.debug("NVIDIA capture hotkey failed", exc_info=True)
                return False, message("press_failed", code, hotkey=hotkey.upper()), {"error_kind": "input_failed", "pressed": None, "verified": False}
            if action != "clip":
                after = None
                deadline = time.monotonic() + 3.0
                for _ in range(12):
                    if time.monotonic() >= deadline:
                        break
                    self._sleep(0.25)
                    after = self._recording() if self._overlay_ready() else None
                    if after is want:
                        break
                self.state.recording = after
                if after is not want:
                    return False, message("record_unverified", code, hotkey=hotkey.upper()), {"error_kind": "unverified", "recording": after, "pressed": True, "verified": False}
                if want:
                    self.state.started_at = started
                    return True, message("record_started", code), {"recording": True, "hotkey": hotkey, "pressed": True, "verified": True}
            try:
                timeout = float(self._get("capture.save_wait_seconds", 12.0))
                import math
                timeout = max(0.0, min(30.0, timeout)) if math.isfinite(timeout) else 12.0
            except (TypeError, ValueError):
                timeout = 12.0
            try:
                if self._wait is wait_for_new_video:
                    path = self._wait(folders, started, timeout, baseline=baseline) if folders else None
                else:
                    path = self._wait(folders, started, timeout) if folders else None
            except Exception:
                logger.debug("NVIDIA saved video check failed", exc_info=True)
                path = None
            kind = "clip" if action == "clip" else "record"
            data = {"hotkey": hotkey, "recording": self.state.recording, "folder": self._folder_label(folders), "pressed": True}
            if path is not None:
                data.update(path=str(path), verified=True)
                return True, message(kind + "_saved", code, path=str(path)), data
            return False, message(kind + "_folder", code, folder=self._folder_label(folders)), {**data, "verified": False, "error_kind": "unverified"}
