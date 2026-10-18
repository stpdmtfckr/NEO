"""Match-ready notices and accepting a found match (NEO 167).

Detection costs nothing while nobody is queueing:

* Any matchmaking game: Windows flashes the game's taskbar button when a match
  is found while the game is in the background (Dota 2, Counter-Strike 2,
  Deadlock, Overwatch, …). NEO listens to that shell event (no polling).
* League of Legends: the League client's own local API reports the ready check
  and accepts it. NEO only looks for the client's lockfile every few seconds and
  asks the API once per second while the client is open.

Accepting is done only when the user asks for it («прими матч», the Telegram
button) or armed it in advance («прими матч, когда найдётся»). For games without
an API, NEO brings the game window forward, reads the button text with the
built-in Windows OCR and clicks the «Accept» button. Nothing is clicked when the
button is not found.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')

# Process name (casefolded) -> display name of games that queue for matches.
MATCH_GAMES = {
    'dota2.exe': 'Dota 2',
    'cs2.exe': 'Counter-Strike 2',
    'csgo.exe': 'Counter-Strike',
    'deadlock.exe': 'Deadlock',
    'project8.exe': 'Deadlock',
    'leagueclientux.exe': 'League of Legends',
    'leagueclient.exe': 'League of Legends',
    'league of legends.exe': 'League of Legends',
    'valorant-win64-shipping.exe': 'VALORANT',
    'riotclientux.exe': 'Riot Client',
    'overwatch.exe': 'Overwatch 2',
    'r5apex.exe': 'Apex Legends',
    'r5apex_dx12.exe': 'Apex Legends',
    'marvel-win64-shipping.exe': 'Marvel Rivals',
    'tslgame.exe': 'PUBG',
    'rainbowsix.exe': 'Rainbow Six Siege',
    'rainbowsix_vulkan.exe': 'Rainbow Six Siege',
    'destiny2.exe': 'Destiny 2',
    'darktide.exe': 'Warhammer 40,000: Darktide',
    'eldenring.exe': 'Elden Ring',
    'fortniteclient-win64-shipping.exe': 'Fortnite',
    'rocketleague.exe': 'Rocket League',
    'tf_win64.exe': 'Team Fortress 2',
    'deadbydaylight-win64-shipping.exe': 'Dead by Daylight',
    'battle.net.exe': 'Battle.net',
    'faceit.exe': 'FACEIT',
}
# Words of the «Accept» button in the supported interface languages.
ACCEPT_WORDS = frozenset({
    'accept', 'accepter', 'принять', 'annehmen', 'akzeptieren', 'aceptar',
    'accetta', 'accettare', 'aceitar',
})
_LEAGUE = 'League of Legends'
_LEAGUE_LOCKFILES = (
    r'C:\Riot Games\League of Legends\lockfile',
    r'D:\Riot Games\League of Legends\lockfile',
    r'E:\Riot Games\League of Legends\lockfile',
    r'C:\Program Files\Riot Games\League of Legends\lockfile',
    r'C:\Program Files (x86)\Riot Games\League of Legends\lockfile',
)

_TEXT = {
    'found': ('🎮 {game}: матч найден!', '🎮 {game}: match found!', '🎮 {game}: Match gefunden!',
              '🎮 {game} : match trouvé !', '🎮 {game}: ¡partida encontrada!', '🎮 {game}: partita trovata!',
              '🎮 {game}: partida encontrada!'),
    'maybe': ('🎮 {game} мигает на панели задач — похоже, матч найден.',
              '🎮 {game} is flashing on the taskbar — a match seems to be ready.',
              '🎮 {game} blinkt in der Taskleiste — ein Match scheint bereit zu sein.',
              '🎮 {game} clignote dans la barre des tâches — un match semble prêt.',
              '🎮 {game} parpadea en la barra de tareas: parece que hay partida.',
              '🎮 {game} lampeggia sulla barra delle applicazioni: sembra pronta una partita.',
              '🎮 {game} está a piscar na barra de tarefas — parece haver partida.'),
    'hint': ('Скажите «прими матч» или нажмите «Принять» в Telegram.',
             'Say “accept the match” or press “Accept” in Telegram.',
             'Sag „Match annehmen“ oder tippe in Telegram auf „Annehmen“.',
             'Dites « accepte le match » ou appuyez sur « Accepter » dans Telegram.',
             'Di «acepta la partida» o pulsa «Aceptar» en Telegram.',
             'Di’ «accetta la partita» o premi «Accetta» su Telegram.',
             'Diga «aceita a partida» ou toque em «Aceitar» no Telegram.'),
    'button': ('✅ Принять', '✅ Accept', '✅ Annehmen', '✅ Accepter', '✅ Aceptar', '✅ Accetta', '✅ Aceitar'),
    'accepted': ('✅ {game}: матч принят.', '✅ {game}: match accepted.', '✅ {game}: Match angenommen.',
                 '✅ {game} : match accepté.', '✅ {game}: partida aceptada.', '✅ {game}: partita accettata.',
                 '✅ {game}: partida aceite.'),
    'no_match': ('Сейчас нет найденного матча, который нужно принять.',
                 'There is no found match waiting to be accepted.',
                 'Es gibt gerade kein gefundenes Match zum Annehmen.',
                 'Aucun match trouvé n’attend d’être accepté.',
                 'Ahora no hay ninguna partida encontrada que aceptar.',
                 'Al momento non c’è nessuna partita trovata da accettare.',
                 'Neste momento não há nenhuma partida encontrada para aceitar.'),
    'no_button': ('{game}: не нашёл кнопку «Принять» на экране. Нажмите её вручную.',
                  '{game}: could not find the “Accept” button on screen. Please press it yourself.',
                  '{game}: die Schaltfläche „Annehmen“ wurde nicht gefunden. Bitte selbst drücken.',
                  '{game} : bouton « Accepter » introuvable à l’écran. Appuyez dessus vous-même.',
                  '{game}: no encontré el botón «Aceptar» en pantalla. Púlsalo tú.',
                  '{game}: non ho trovato il pulsante «Accetta» sullo schermo. Premilo tu.',
                  '{game}: não encontrei o botão «Aceitar» no ecrã. Carregue nele.'),
    'armed': ('Хорошо, приму матч автоматически, как только он найдётся (режим действует {minutes} мин).',
              'OK, I will accept the match as soon as it is found (for the next {minutes} min).',
              'OK, ich nehme das Match an, sobald es gefunden wird (für {minutes} Min.).',
              'D’accord, j’accepterai le match dès qu’il sera trouvé (pendant {minutes} min).',
              'De acuerdo, aceptaré la partida en cuanto se encuentre (durante {minutes} min).',
              'Va bene, accetterò la partita appena viene trovata (per {minutes} min).',
              'Está bem, aceito a partida assim que for encontrada (durante {minutes} min).'),
    'disarmed': ('Автопринятие матча выключено.', 'Match auto-accept is off.', 'Automatisches Annehmen ist aus.',
                 'Acceptation automatique désactivée.', 'Aceptación automática desactivada.',
                 'Accettazione automatica disattivata.', 'Aceitação automática desativada.'),
    'status_on': ('Автопринятие включено ещё {minutes} мин.', 'Auto-accept is on for {minutes} more min.',
                  'Automatisches Annehmen ist noch {minutes} Min. aktiv.',
                  'Acceptation automatique active encore {minutes} min.',
                  'Aceptación automática activa {minutes} min más.',
                  'Accettazione automatica attiva per altri {minutes} min.',
                  'Aceitação automática ativa por mais {minutes} min.'),
    'status_off': ('Автопринятие выключено.', 'Auto-accept is off.', 'Automatisches Annehmen ist aus.',
                   'Acceptation automatique désactivée.', 'Aceptación automática desactivada.',
                   'Accettazione automatica disattivata.', 'Aceitação automática desativada.'),
    'last': ('Последний найденный матч: {game}, {ago} назад.', 'Last found match: {game}, {ago} ago.',
             'Zuletzt gefundenes Match: {game}, vor {ago}.', 'Dernier match trouvé : {game}, il y a {ago}.',
             'Última partida encontrada: {game}, hace {ago}.', 'Ultima partita trovata: {game}, {ago} fa.',
             'Última partida encontrada: {game}, há {ago}.'),
    'disabled': ('Уведомления о матче выключены в настройках (gaming.match_ready.enabled).',
                 'Match notices are turned off in settings (gaming.match_ready.enabled).',
                 'Match-Hinweise sind in den Einstellungen aus (gaming.match_ready.enabled).',
                 'Les avis de match sont désactivés (gaming.match_ready.enabled).',
                 'Los avisos de partida están desactivados (gaming.match_ready.enabled).',
                 'Gli avvisi di partita sono disattivati (gaming.match_ready.enabled).',
                 'Os avisos de partida estão desativados (gaming.match_ready.enabled).'),
    'windows_only': ('Принятие матча работает только в Windows.', 'Accepting a match works only on Windows.',
                     'Das Annehmen funktioniert nur unter Windows.', 'L’acceptation ne fonctionne que sous Windows.',
                     'Aceptar partidas solo funciona en Windows.', 'L’accettazione funziona solo su Windows.',
                     'Aceitar partidas só funciona no Windows.'),
}


def message(key: str, code: str, **values: Any) -> str:
    index = _CODES.index(code) if code in _CODES else 1
    return _TEXT[key][index].format(**values)


def _ago(seconds: float, code: str) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return {'ru': f'{seconds} с', 'de': f'{seconds} s'}.get(code, f'{seconds} s')
    minutes = seconds // 60
    return {'ru': f'{minutes} мин', 'de': f'{minutes} Min.'}.get(code, f'{minutes} min')


def _norm_word(value: str) -> str:
    return ''.join(ch for ch in str(value).casefold() if ch.isalpha())


def find_accept_word(words) -> tuple[float, float] | None:
    """Centre of the first OCR word that reads «Accept» (any supported language).

    ``words`` are ``(text, x, y, width, height)`` tuples in image coordinates.
    The largest matching word wins: a button label is bigger than chat text.
    """
    best = None
    for text, x, y, w, h in words:
        if _norm_word(text) in ACCEPT_WORDS and w > 0 and h > 0:
            if best is None or w * h > best[2] * best[3]:
                best = (x, y, w, h)
    if best is None:
        return None
    x, y, w, h = best
    return x + w / 2.0, y + h / 2.0


@dataclass
class MatchEvent:
    game: str
    process: str = ''
    pid: int = 0
    hwnd: int = 0
    source: str = ''
    at: float = field(default_factory=time.monotonic)
    accepted: bool = False


class MatchReadyWatcher:
    """One per application. All platform calls are injectable for tests."""

    def __init__(self, config: Any, *, is_windows: bool | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.is_windows = os.name == 'nt' if is_windows is None else is_windows
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._hook_thread_id = 0
        self._armed_until = 0.0
        self._last: MatchEvent | None = None
        self._notified: dict[str, float] = {}
        self._notice: Callable[[str], None] | None = None
        self._telegram = None
        # Replaceable platform operations.
        self.league_accept: Callable[[], bool] = self._league_accept
        self.window_accept: Callable[[MatchEvent], bool] = self._window_accept

    # ------------------------------------------------------------ configuration
    def _code(self) -> str:
        code = str(self.config.get('app.language', 'en') or 'en').split('-')[0].casefold()
        return code if code in _CODES else 'en'

    def _setting(self, key: str, default: Any) -> Any:
        return self.config.get('gaming.match_ready.' + key, default)

    def enabled(self) -> bool:
        return bool(self.config.get('gaming.enabled', True)) and bool(self._setting('enabled', True))

    def set_notice_callback(self, callback: Callable[[str], None] | None) -> None:
        self._notice = callback

    def attach_telegram(self, remote: Any) -> None:
        self._telegram = remote

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if not self.is_windows or not self.enabled() or self._threads:
            return False
        self._stop.clear()
        for target, name in ((self._flash_loop, 'NEO match flash hook'), (self._league_loop, 'NEO League ready check'),
                             (self._armed_loop, 'NEO match auto-accept')):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._hook_thread_id and self.is_windows:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self._hook_thread_id, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass
        self._threads.clear()

    # ------------------------------------------------------------------ actions
    def arm(self, minutes: float | None = None) -> str:
        minutes = int(max(1, min(240, float(minutes or self._setting('auto_accept_minutes', 60) or 60))))
        with self._lock:
            self._armed_until = self.clock() + minutes * 60
            pending = self._last if self._last and not self._last.accepted and self.clock() - self._last.at < 45 else None
        text = message('armed', self._code(), minutes=minutes)
        if pending is not None:
            return text + '\n' + self.accept()[1]
        return text

    def disarm(self) -> str:
        with self._lock:
            self._armed_until = 0.0
        return message('disarmed', self._code())

    def armed(self) -> bool:
        with self._lock:
            return self._armed_until > self.clock()

    def status(self) -> str:
        code = self._code()
        if not self.enabled():
            return message('disabled', code)
        with self._lock:
            left = self._armed_until - self.clock()
            last = self._last
        lines = [message('status_on', code, minutes=max(1, -int(-left // 60))) if left > 0 else message('status_off', code)]
        if last is not None:
            lines.append(message('last', code, game=last.game, ago=_ago(self.clock() - last.at, code)))
        return '\n'.join(lines)

    def accept(self) -> tuple[bool, str]:
        code = self._code()
        if not self.is_windows:
            return False, message('windows_only', code)
        with self._lock:
            event = self._last
        fresh = event is not None and not event.accepted and self.clock() - event.at < 60
        if (fresh and event.game == _LEAGUE) or (not fresh and self._league_lockfile() is not None):
            if self.league_accept():
                self._mark_accepted(_LEAGUE)
                return True, message('accepted', code, game=_LEAGUE)
            if fresh:
                return False, message('no_button', code, game=_LEAGUE)
        target = event if fresh else self._foreground_game_event()
        if target is None:
            return False, message('no_match', code)
        if self.window_accept(target):
            self._mark_accepted(target.game)
            return True, message('accepted', code, game=target.game)
        return False, message('no_button', code, game=target.game)

    def _mark_accepted(self, game: str) -> None:
        with self._lock:
            if self._last is not None and self._last.game == game:
                self._last.accepted = True
            if not bool(self._setting('auto_accept_keep', False)):
                self._armed_until = 0.0

    # --------------------------------------------------------------- detection
    def on_match_found(self, event: MatchEvent, *, certain: bool) -> None:
        """Called from the watcher threads; notify once per match, accept if armed."""
        now = self.clock()
        with self._lock:
            if now - self._notified.get(event.game, -1e9) < float(self._setting('repeat_seconds', 20) or 20):
                return  # the same match keeps flashing
            self._notified[event.game] = now
            self._last = event
            armed = self._armed_until > now
        code = self._code()
        text = message('found' if certain else 'maybe', code, game=event.game)
        if armed:
            ok, result = self.accept()
            self._publish(text + '\n' + result, with_button=not ok)
        else:
            self._publish(text + '\n' + message('hint', code), with_button=True)

    def _publish(self, text: str, *, with_button: bool) -> None:
        notice = self._notice
        if notice is not None:
            try:
                notice(text)
            except Exception:
                logger.debug('Match notice callback failed', exc_info=True)
        remote = self._telegram
        if remote is not None and bool(self._setting('telegram', True)):
            send = getattr(remote, 'notify_with_buttons', None)
            if callable(send):
                rows = [[(message('button', self._code()), 'a:match:accept')]] if with_button else []
                try:
                    send(text, rows)
                except Exception:
                    logger.debug('Telegram match notice failed', exc_info=True)

    # ---- taskbar flash (event driven) ----
    def _flash_loop(self) -> None:
        try:
            self._run_shell_hook()
        except Exception:
            logger.warning('Match flash hook unavailable', exc_info=True)

    def _run_shell_hook(self) -> None:
        import ctypes
        from ctypes import wintypes
        # Private DLL objects: argtypes set here never leak into other modules.
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.restype = LRESULT
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        user32.CreateWindowExW.restype = wintypes.HWND
        for name in ('RegisterShellHookWindow', 'DeregisterShellHookWindow', 'DestroyWindow'):
            getattr(user32, name).argtypes = (wintypes.HWND,)
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                        ('cbWndExtra', ctypes.c_int), ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                        ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                        ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]

        shell_message = user32.RegisterWindowMessageW('SHELLHOOK')
        HSHELL_FLASH = 0x8006

        def window_proc(hwnd, msg, wparam, lparam):
            if msg == shell_message and wparam == HSHELL_FLASH and lparam:
                threading.Thread(target=self._on_flash, args=(int(lparam),), daemon=True).start()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = WNDPROC(window_proc)
        instance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = proc
        wc.hInstance = instance
        wc.lpszClassName = 'NEOMatchReadyHook'
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, 'NEO match hook', 0, 0, 0, 0, 0, None, None, instance, None)
        if not hwnd:
            raise OSError(ctypes.get_last_error(), 'CreateWindowExW failed')
        self._hook_thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterShellHookWindow(hwnd):
            raise OSError(ctypes.get_last_error(), 'RegisterShellHookWindow failed')
        msg = wintypes.MSG()
        try:
            while not self._stop.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.DeregisterShellHookWindow(hwnd)
            user32.DestroyWindow(hwnd)
            self._hook_thread_id = 0
            del proc

    def _on_flash(self, hwnd: int) -> None:
        pid, name = _window_process(hwnd)
        game = MATCH_GAMES.get(name)
        if game is None or game == _LEAGUE:
            return  # League is reported precisely by its client API.
        self.on_match_found(MatchEvent(game=game, process=name, pid=pid, hwnd=hwnd, source='taskbar_flash'), certain=False)

    # ---- League of Legends client API ----
    def _league_lockfile(self) -> Path | None:
        custom = str(self._setting('league_lockfile', '') or '')
        for raw in ((custom,) if custom else ()) + _LEAGUE_LOCKFILES:
            path = Path(raw)
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    def _league_request(self, method: str, endpoint: str):
        lockfile = self._league_lockfile()
        if lockfile is None:
            return None
        try:
            _name, _pid, port, password, protocol = lockfile.read_text(encoding='utf-8').strip().split(':')[:5]
        except (OSError, ValueError):
            return None
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # The client listens only on 127.0.0.1 with its own self-signed certificate.
        return requests.request(method, f'{protocol}://127.0.0.1:{int(port)}{endpoint}', auth=('riot', password),
                                verify=False, timeout=2.0)

    def _league_accept(self) -> bool:
        try:
            response = self._league_request('POST', '/lol-matchmaking/v1/ready-check/accept')
            return response is not None and response.status_code in (200, 204)
        except Exception:
            logger.debug('League accept failed', exc_info=True)
            return False

    def _league_loop(self) -> None:
        while not self._stop.wait(1.0 if self._league_lockfile() else 4.0):
            if not self.enabled() or self._league_lockfile() is None:
                continue
            try:
                response = self._league_request('GET', '/lol-matchmaking/v1/ready-check')
                data = response.json() if response is not None and response.status_code == 200 else {}
            except Exception:
                continue
            if data.get('state') == 'InProgress' and data.get('playerResponse') == 'None':
                self.on_match_found(MatchEvent(game=_LEAGUE, source='league_client_api'), certain=True)

    # ---- armed mode: games that never flash because they are in front ----
    def _armed_loop(self) -> None:
        while not self._stop.wait(2.0):
            if not self.armed():
                continue
            event = self._foreground_game_event(fallback=False)
            if event is None or event.game == _LEAGUE:
                continue
            if self._window_accept(event, require_foreground=True):
                self._mark_accepted(event.game)
                with self._lock:
                    self._last = event
                    event.accepted = True
                self._publish(message('found', self._code(), game=event.game) + '\n'
                              + message('accepted', self._code(), game=event.game), with_button=False)

    def _foreground_game_event(self, *, fallback: bool = True) -> MatchEvent | None:
        try:
            hwnd = int(_u32().GetForegroundWindow() or 0)
        except Exception:
            return None
        pid, name = _window_process(hwnd)
        game = MATCH_GAMES.get(name)
        if game is None and fallback:
            hwnd, pid, name, game = _first_game_window()
        if game is None:
            return None
        return MatchEvent(game=game, process=name, pid=pid, hwnd=hwnd, source='window')

    # ---- clicking the Accept button ----
    def _window_accept(self, event: MatchEvent, *, require_foreground: bool = False) -> bool:
        try:
            user32 = _u32()
            hwnd = int(event.hwnd or 0)
            if not hwnd or not user32.IsWindow(hwnd):
                return False
            if require_foreground:
                if int(user32.GetForegroundWindow() or 0) != hwnd:
                    return False
            else:
                _bring_to_front(hwnd)
                time.sleep(0.45)
            rect = _client_rect(hwnd)
            if rect is None:
                return False
            left, top, width, height = rect
            words, scale = _ocr_region(left, top, width, height)
            point = find_accept_word(words)
            if point is None:
                return False
            x, y = int(left + point[0] / scale), int(top + point[1] / scale)
            _click(x, y)
            logger.info('Match accept clicked: game=%s at=%s,%s', event.game, x, y)
            return True
        except Exception:
            logger.debug('Window accept failed', exc_info=True)
            return False


# ----------------------------------------------------------------- Win32 helpers
_USER32 = None


def _u32():
    """Private, fully typed user32 (64-bit handles, no shared argtypes)."""
    global _USER32
    if _USER32 is None:
        import ctypes
        from ctypes import wintypes
        dll = ctypes.WinDLL('user32', use_last_error=True)
        HWND = wintypes.HWND
        signatures = {
            'GetForegroundWindow': ((), HWND),
            'GetWindowThreadProcessId': ((HWND, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD),
            'IsWindow': ((HWND,), wintypes.BOOL), 'IsWindowVisible': ((HWND,), wintypes.BOOL),
            'IsIconic': ((HWND,), wintypes.BOOL), 'ShowWindow': ((HWND, ctypes.c_int), wintypes.BOOL),
            'SetForegroundWindow': ((HWND,), wintypes.BOOL),
            'GetWindowRect': ((HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL),
            'GetClientRect': ((HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL),
            'ClientToScreen': ((HWND, ctypes.POINTER(wintypes.POINT)), wintypes.BOOL),
            'SetCursorPos': ((ctypes.c_int, ctypes.c_int), wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result
        _USER32 = dll
    return _USER32


def _window_process(hwnd: int) -> tuple[int, str]:
    if not hwnd:
        return 0, ''
    try:
        import ctypes
        from ctypes import wintypes
        pid = wintypes.DWORD()
        _u32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil
        return int(pid.value), psutil.Process(int(pid.value)).name().casefold()
    except Exception:
        return 0, ''


def _first_game_window() -> tuple[int, int, str, str | None]:
    """Largest visible top-level window that belongs to a known matchmaking game."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = _u32()
        found: list[tuple[int, int, int, str]] = []
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                pid, name = _window_process(hwnd)
                if name in MATCH_GAMES and name not in {'battle.net.exe', 'faceit.exe'}:
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    found.append(((rect.right - rect.left) * (rect.bottom - rect.top), int(hwnd or 0), pid, name))
            return True

        user32.EnumWindows(EnumProc(visit), 0)
        if not found:
            return 0, 0, '', None
        _area, hwnd, pid, name = max(found)
        return hwnd, pid, name, MATCH_GAMES[name]
    except Exception:
        return 0, 0, '', None


def _bring_to_front(hwnd: int) -> None:
    user32 = _u32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # A background process may bring a window forward right after a key event.
    user32.keybd_event(0x12, 0, 0, 0)       # Alt down
    user32.keybd_event(0x12, 0, 0x0002, 0)  # Alt up
    user32.SetForegroundWindow(hwnd)


def _client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    import ctypes
    from ctypes import wintypes
    user32 = _u32()
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 200 or height < 150:
        return None
    return origin.x, origin.y, width, height


def _ocr_region(left: int, top: int, width: int, height: int):
    """Words on screen via Windows OCR: ([(text, x, y, w, h)], scale)."""
    from jarvis.runtime.screen_ocr168 import words_in_region
    return words_in_region(left, top, width, height, max_edge=1600)


def _click(x: int, y: int) -> None:
    user32 = _u32()
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    time.sleep(0.04)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up


_WATCHER: MatchReadyWatcher | None = None
_WATCHER_LOCK = threading.Lock()


def match_ready_watcher(config: Any) -> MatchReadyWatcher:
    global _WATCHER
    with _WATCHER_LOCK:
        if _WATCHER is None:
            _WATCHER = MatchReadyWatcher(config)
        return _WATCHER
