"""NVIDIA Instant Replay on/off through the overlay's own toggle hotkey.

NEO does not talk to NVIDIA services. It presses the hotkey the NVIDIA overlay
already provides for switching Instant Replay (default Alt+Shift+F10) and reads
live state exposed by NVIDIA App's visible overlay. That hotkey is a toggle,
so NEO never presses it for "turn on"/"turn off" when the state is unknown: it
says so and the user can ask to toggle explicitly. The hotkey goes to the
NVIDIA overlay, not to a game.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

from jarvis.integrations.nvidia_capture import OVERLAY_PROCESSES, overlay_running, parse_hotkey, press_hotkey
from jarvis.integrations import nvidia_diagnostics184 as diagnostic

logger = logging.getLogger(__name__)

DEFAULT_TOGGLE_HOTKEY = "alt+shift+f10"
_LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")

MESSAGES = {
    "on": ("Мгновенный повтор NVIDIA включён.", "NVIDIA Instant Replay is on.",
           "NVIDIA Instant Replay ist an.", "La relecture instantanée NVIDIA est activée.",
           "La repetición instantánea de NVIDIA está activada.", "Il replay istantaneo NVIDIA è attivo.",
           "A repetição instantânea NVIDIA está ligada."),
    "off": ("Мгновенный повтор NVIDIA выключен. Клипы последних минут сейчас не сохранятся.",
            "NVIDIA Instant Replay is off. Clips of the last minutes won't be saved right now.",
            "NVIDIA Instant Replay ist aus. Clips der letzten Minuten werden gerade nicht gespeichert.",
            "La relecture instantanée NVIDIA est désactivée. Les clips des dernières minutes ne seront pas enregistrés.",
            "La repetición instantánea de NVIDIA está desactivada. Ahora no se guardarán clips de los últimos minutos.",
            "Il replay istantaneo NVIDIA è disattivato. Le clip degli ultimi minuti ora non verranno salvate.",
            "A repetição instantânea NVIDIA está desligada. Agora não serão guardados clips dos últimos minutos."),
    "unknown": ("Не могу узнать, включён ли мгновенный повтор: NVIDIA не сообщает его состояние на этом компьютере.",
                "I can't tell whether Instant Replay is on: NVIDIA doesn't report its state on this PC.",
                "Ich kann nicht erkennen, ob Instant Replay an ist: NVIDIA meldet den Zustand auf diesem PC nicht.",
                "Je ne peux pas savoir si la relecture instantanée est activée : NVIDIA n’indique pas son état sur ce PC.",
                "No puedo saber si la repetición instantánea está activada: NVIDIA no informa de su estado en este PC.",
                "Non riesco a capire se il replay istantaneo è attivo: NVIDIA non ne indica lo stato su questo PC.",
                "Não consigo saber se a repetição instantânea está ligada: a NVIDIA não indica o estado neste PC."),
    "already_on": ("Мгновенный повтор уже включён.", "Instant Replay is already on.", "Instant Replay ist bereits an.",
                   "La relecture instantanée est déjà activée.", "La repetición instantánea ya está activada.",
                   "Il replay istantaneo è già attivo.", "A repetição instantânea já está ligada."),
    "already_off": ("Мгновенный повтор уже выключен.", "Instant Replay is already off.", "Instant Replay ist bereits aus.",
                    "La relecture instantanée est déjà désactivée.", "La repetición instantánea ya está desactivada.",
                    "Il replay istantaneo è già disattivato.", "A repetição instantânea já está desligada."),
    "turned_on": ("Включил мгновенный повтор NVIDIA ({hotkey}).", "Turned NVIDIA Instant Replay on ({hotkey}).",
                  "NVIDIA Instant Replay eingeschaltet ({hotkey}).", "Relecture instantanée NVIDIA activée ({hotkey}).",
                  "Repetición instantánea de NVIDIA activada ({hotkey}).", "Replay istantaneo NVIDIA attivato ({hotkey}).",
                  "Repetição instantânea NVIDIA ligada ({hotkey})."),
    "turned_off": ("Выключил мгновенный повтор NVIDIA ({hotkey}).", "Turned NVIDIA Instant Replay off ({hotkey}).",
                   "NVIDIA Instant Replay ausgeschaltet ({hotkey}).", "Relecture instantanée NVIDIA désactivée ({hotkey}).",
                   "Repetición instantánea de NVIDIA desactivada ({hotkey}).", "Replay istantaneo NVIDIA disattivato ({hotkey}).",
                   "Repetição instantânea NVIDIA desligada ({hotkey})."),
    "no_change": ("Нажал {hotkey}, но состояние мгновенного повтора не изменилось. Проверьте, что в оверлее NVIDIA для переключения назначено именно это сочетание.",
                  "I pressed {hotkey}, but the Instant Replay state didn't change. Check that this is the toggle hotkey in the NVIDIA overlay.",
                  "Ich habe {hotkey} gedrückt, aber der Instant-Replay-Zustand hat sich nicht geändert. Prüfe, ob das im NVIDIA-Overlay das Umschalt-Kürzel ist.",
                  "J’ai appuyé sur {hotkey}, mais l’état de la relecture instantanée n’a pas changé. Vérifiez que c’est bien le raccourci de bascule dans l’overlay NVIDIA.",
                  "Pulsé {hotkey}, pero el estado de la repetición instantánea no cambió. Comprueba que ese es el atajo para activarla en la superposición de NVIDIA.",
                  "Ho premuto {hotkey}, ma lo stato del replay istantaneo non è cambiato. Controlla che sia la scorciatoia di attivazione nell’overlay NVIDIA.",
                  "Carreguei em {hotkey}, mas o estado da repetição instantânea não mudou. Verifica se é esse o atalho de alternância na sobreposição NVIDIA."),
    "toggled_unverified": ("Нажал {hotkey} — это переключает мгновенный повтор. Проверить результат не могу: NVIDIA не сообщает состояние на этом компьютере.",
                           "Pressed {hotkey}, which toggles Instant Replay. I can't verify the result: NVIDIA doesn't report the state on this PC.",
                           "{hotkey} gedrückt – das schaltet Instant Replay um. Das Ergebnis kann ich nicht prüfen: NVIDIA meldet den Zustand hier nicht.",
                           "J’ai appuyé sur {hotkey}, qui bascule la relecture instantanée. Je ne peux pas vérifier : NVIDIA n’indique pas l’état sur ce PC.",
                           "Pulsé {hotkey}, que alterna la repetición instantánea. No puedo comprobarlo: NVIDIA no informa del estado en este PC.",
                           "Ho premuto {hotkey}, che attiva o disattiva il replay istantaneo. Non posso verificarlo: NVIDIA non ne indica lo stato qui.",
                           "Carreguei em {hotkey}, que alterna a repetição instantânea. Não consigo verificar: a NVIDIA não indica o estado neste PC."),
    "unknown_no_press": ("Не нажимаю {hotkey}: это переключатель, а текущее состояние мгновенного повтора неизвестно, и я мог бы его выключить. Если нужно нажать всё равно, скажите «переключи мгновенный повтор».",
                         "I won't press {hotkey}: it's a toggle and the current Instant Replay state is unknown, so I could turn it off. To press it anyway, say “toggle instant replay”.",
                         "Ich drücke {hotkey} nicht: Es ist ein Umschalter und der Zustand ist unbekannt, ich könnte es also ausschalten. Um trotzdem zu drücken, sag „Instant Replay umschalten“.",
                         "Je n’appuie pas sur {hotkey} : c’est une bascule et l’état actuel est inconnu, je pourrais la désactiver. Pour appuyer quand même, dites « bascule la relecture instantanée ».",
                         "No pulso {hotkey}: es un interruptor y el estado actual es desconocido, podría desactivarla. Para pulsarlo igualmente, di «alterna la repetición instantánea».",
                         "Non premo {hotkey}: è un interruttore e lo stato attuale è sconosciuto, potrei disattivarlo. Per premerlo comunque, di «alterna il replay istantaneo».",
                         "Não carrego em {hotkey}: é um interruptor e o estado atual é desconhecido, poderia desligá-la. Para carregar mesmo assim, diz «alterna a repetição instantânea»."),
    "overlay_missing": ("Оверлей NVIDIA не запущен. Включите «Внутриигровой оверлей» в NVIDIA App и повторите.",
                        "The NVIDIA overlay is not running. Turn on the in-game overlay in NVIDIA App and try again.",
                        "Das NVIDIA-Overlay läuft nicht. Aktiviere das In-Game-Overlay in der NVIDIA App und versuche es erneut.",
                        "L’overlay NVIDIA n’est pas lancé. Activez l’overlay en jeu dans NVIDIA App, puis réessayez.",
                        "La superposición de NVIDIA no está activa. Actívala en NVIDIA App y vuelve a intentarlo.",
                        "L’overlay NVIDIA non è attivo. Attivalo in NVIDIA App e riprova.",
                        "A sobreposição NVIDIA não está ativa. Ativa-a na NVIDIA App e tenta de novo."),
    "bad_hotkey": ("Не понимаю горячую клавишу «{hotkey}» в настройках игр.", "I can't read the hotkey “{hotkey}” in the game settings.",
                   "Ich verstehe das Tastenkürzel „{hotkey}“ in den Spieleinstellungen nicht.",
                   "Je ne comprends pas le raccourci « {hotkey} » dans les réglages de jeu.",
                   "No entiendo el atajo «{hotkey}» de los ajustes de juegos.",
                   "Non capisco la scorciatoia «{hotkey}» nelle impostazioni di gioco.",
                   "Não percebo o atalho «{hotkey}» nas definições de jogos."),
    "unsupported": ("Мгновенный повтор NVIDIA доступен только в Windows.", "NVIDIA Instant Replay is available only on Windows.",
                    "NVIDIA Instant Replay gibt es nur unter Windows.", "La relecture instantanée NVIDIA n’existe que sous Windows.",
                    "La repetición instantánea de NVIDIA solo está en Windows.", "Il replay istantaneo NVIDIA è disponibile solo su Windows.",
                    "A repetição instantânea NVIDIA só existe no Windows."),
    "game_warning": ("⚠ Запущена {game}, а мгновенный повтор NVIDIA выключен — клипы последних минут не сохранятся. Скажите «включи мгновенный повтор».",
                     "⚠ {game} is running but NVIDIA Instant Replay is off — clips of the last minutes won't be saved. Say “turn on instant replay”.",
                     "⚠ {game} läuft, aber NVIDIA Instant Replay ist aus – Clips der letzten Minuten werden nicht gespeichert. Sag „Instant Replay einschalten“.",
                     "⚠ {game} est lancé mais la relecture instantanée NVIDIA est désactivée : aucun clip des dernières minutes. Dites « active la relecture instantanée ».",
                     "⚠ {game} está en marcha pero la repetición instantánea de NVIDIA está desactivada: no se guardarán clips. Di «activa la repetición instantánea».",
                     "⚠ {game} è avviato ma il replay istantaneo NVIDIA è disattivato: niente clip degli ultimi minuti. Di «attiva il replay istantaneo».",
                     "⚠ {game} está a correr mas a repetição instantânea NVIDIA está desligada: não haverá clips. Diz «liga a repetição instantânea»."),
    "game_unknown": ("Запущена {game}. Не могу проверить, включён ли мгновенный повтор NVIDIA: проверьте в оверлее (Alt+Z).",
                     "{game} is running. I can't check whether NVIDIA Instant Replay is on: check it in the overlay (Alt+Z).",
                     "{game} läuft. Ob NVIDIA Instant Replay an ist, kann ich nicht prüfen – schau im Overlay nach (Alt+Z).",
                     "{game} est lancé. Je ne peux pas vérifier la relecture instantanée NVIDIA : regardez dans l’overlay (Alt+Z).",
                     "{game} está en marcha. No puedo comprobar la repetición instantánea de NVIDIA: revísala en la superposición (Alt+Z).",
                     "{game} è avviato. Non posso verificare il replay istantaneo NVIDIA: controlla nell’overlay (Alt+Z).",
                     "{game} está a correr. Não consigo verificar a repetição instantânea NVIDIA: vê na sobreposição (Alt+Z)."),
    "changed_on": ("Мгновенный повтор NVIDIA включился.", "NVIDIA Instant Replay was turned on.",
                   "NVIDIA Instant Replay wurde eingeschaltet.", "La relecture instantanée NVIDIA a été activée.",
                   "Se activó la repetición instantánea de NVIDIA.", "Il replay istantaneo NVIDIA è stato attivato.",
                   "A repetição instantânea NVIDIA foi ligada."),
    "changed_off": ("⚠ Мгновенный повтор NVIDIA выключился — клипы последних минут не сохранятся.",
                    "⚠ NVIDIA Instant Replay was turned off — clips of the last minutes won't be saved.",
                    "⚠ NVIDIA Instant Replay wurde ausgeschaltet – Clips der letzten Minuten werden nicht gespeichert.",
                    "⚠ La relecture instantanée NVIDIA a été désactivée : plus de clips des dernières minutes.",
                    "⚠ Se desactivó la repetición instantánea de NVIDIA: no se guardarán clips de los últimos minutos.",
                    "⚠ Il replay istantaneo NVIDIA è stato disattivato: niente clip degli ultimi minuti.",
                    "⚠ A repetição instantânea NVIDIA foi desligada: não serão guardados clips dos últimos minutos."),
    # NEO 171: «Automatically turn Instant Replay on when a game starts» (Settings → Games).
    "auto_on": ("Запущена {game}: мгновенный повтор NVIDIA был выключен — я его включил ({hotkey}).",
                "{game} started: NVIDIA Instant Replay was off — I turned it on ({hotkey}).",
                "{game} gestartet: NVIDIA Instant Replay war aus – ich habe es eingeschaltet ({hotkey}).",
                "{game} est lancé : la relecture instantanée NVIDIA était désactivée — je l’ai activée ({hotkey}).",
                "{game} se ha iniciado: la repetición instantánea de NVIDIA estaba desactivada; la he activado ({hotkey}).",
                "{game} è avviato: il replay istantaneo NVIDIA era disattivato, l’ho attivato ({hotkey}).",
                "{game} foi iniciado: a repetição instantânea NVIDIA estava desligada — liguei-a ({hotkey})."),
    "auto_failed": ("⚠ Запущена {game}, мгновенный повтор NVIDIA выключен, и включить его автоматически не получилось. {reason}",
                    "⚠ {game} started, NVIDIA Instant Replay is off and I couldn't turn it on automatically. {reason}",
                    "⚠ {game} gestartet, NVIDIA Instant Replay ist aus und ließ sich nicht automatisch einschalten. {reason}",
                    "⚠ {game} est lancé, la relecture instantanée NVIDIA est désactivée et je n’ai pas pu l’activer automatiquement. {reason}",
                    "⚠ {game} se ha iniciado, la repetición instantánea de NVIDIA está desactivada y no pude activarla automáticamente. {reason}",
                    "⚠ {game} è avviato, il replay istantaneo NVIDIA è disattivato e non sono riuscito ad attivarlo automaticamente. {reason}",
                    "⚠ {game} foi iniciado, a repetição instantânea NVIDIA está desligada e não consegui ligá-la automaticamente. {reason}"),
    "auto_pressed": ("Запущена {game}: мгновенный повтор NVIDIA был выключен. {reason}",
                     "{game} started: NVIDIA Instant Replay was off. {reason}",
                     "{game} gestartet: NVIDIA Instant Replay war aus. {reason}",
                     "{game} est lancé : la relecture instantanée NVIDIA était désactivée. {reason}",
                     "{game} se ha iniciado: la repetición instantánea de NVIDIA estaba desactivada. {reason}",
                     "{game} è avviato: il replay istantaneo NVIDIA era disattivato. {reason}",
                     "{game} foi iniciado: a repetição instantânea NVIDIA estava desligada. {reason}"),
    "auto_hint": ("Можно включить в Настройки → Игры галочку «Автоматически включать мгновенный повтор».",
                  "You can tick “Turn Instant Replay on automatically” in Settings → Games.",
                  "In Einstellungen → Spiele kannst du „Instant Replay automatisch einschalten“ aktivieren.",
                  "Vous pouvez cocher « Activer automatiquement la relecture instantanée » dans Paramètres → Jeux.",
                  "Puedes marcar «Activar automáticamente la repetición instantánea» en Ajustes → Juegos.",
                  "Puoi spuntare «Attiva automaticamente il replay istantaneo» in Impostazioni → Giochi.",
                  "Podes marcar «Ligar automaticamente a repetição instantânea» em Definições → Jogos."),
}

MESSAGES.update({
    "overlay_missing": ('Не удалось подтвердить доступность оверлея NVIDIA. Проверьте Alt+Z и настройку внутриигрового оверлея.', 'Could not confirm that the NVIDIA overlay is available. Check Alt+Z and the in-game overlay setting.', 'Verfügbarkeit des NVIDIA-Overlays nicht bestätigt. Alt+Z und die In-Game-Overlay-Einstellung prüfen.', 'Disponibilité de l’overlay NVIDIA non confirmée. Vérifiez Alt+Z et son activation dans les paramètres.', 'No se confirmó la disponibilidad de la superposición NVIDIA. Revisa Alt+Z y su configuración.', 'Disponibilità dell’overlay NVIDIA non confermata. Controlla Alt+Z e le impostazioni dell’overlay.', 'Disponibilidade da sobreposição NVIDIA não confirmada. Verifica Alt+Z e as definições da sobreposição.'),
    "unknown": ("Не могу прочитать текущее состояние мгновенного повтора NVIDIA. Откройте Alt+Z и проверьте его; эта версия оверлея может не предоставлять доступное состояние.", "I can't read the current NVIDIA Instant Replay state. Open Alt+Z to check it; this overlay version may not expose an accessible state.", "Der aktuelle NVIDIA-Instant-Replay-Zustand ist nicht lesbar. Prüfe Alt+Z; diese Overlay-Version stellt den Zustand möglicherweise nicht bereit.", "Je ne peux pas lire l’état actuel de la relecture NVIDIA. Vérifiez Alt+Z ; cette version d’overlay peut ne pas exposer son état.", "No puedo leer el estado actual de la repetición NVIDIA. Compruébalo en Alt+Z; esta versión puede no exponer un estado accesible.", "Non posso leggere lo stato attuale del replay NVIDIA. Controlla Alt+Z; questa versione potrebbe non esporre uno stato accessibile.", "Não consigo ler o estado atual da repetição NVIDIA. Verifica Alt+Z; esta versão pode não disponibilizar um estado acessível."),
    "toggled_unverified": ("Отправил {hotkey} для переключения мгновенного повтора. Проверить результат не могу: текущее состояние недоступно NEO. Проверьте Alt+Z.", "Sent {hotkey} to toggle Instant Replay. I can't verify the result: NEO cannot read the current state. Check Alt+Z.", "{hotkey} zum Umschalten von Instant Replay gesendet. Ergebnis nicht bestätigt: NEO kann den Zustand nicht lesen. Alt+Z prüfen.", "{hotkey} envoyé pour basculer la relecture. Résultat non confirmé : NEO ne peut pas lire l’état. Vérifiez Alt+Z.", "Se envió {hotkey} para alternar la repetición. No puedo confirmar el resultado: NEO no puede leer el estado. Revisa Alt+Z.", "Inviato {hotkey} per alternare il replay. Risultato non verificabile: NEO non può leggere lo stato. Controlla Alt+Z.", "Enviado {hotkey} para alternar a repetição. Resultado não confirmado: NEO não consegue ler o estado. Verifica Alt+Z."),
    "auto_unavailable": ("Не удалось безопасно включить мгновенный повтор для {game}: состояние NVIDIA или оверлей недоступны. Проверьте Alt+Z; повторного нажатия не было.", "Could not safely enable Instant Replay for {game}: NVIDIA state or overlay is unavailable. Check Alt+Z; no retry key was pressed.", "Instant Replay für {game} ließ sich nicht sicher einschalten: NVIDIA-Zustand oder Overlay nicht verfügbar. Alt+Z prüfen; kein erneuter Tastendruck.", "Impossible d’activer sûrement la relecture pour {game} : état NVIDIA ou overlay indisponible. Vérifiez Alt+Z ; aucune nouvelle touche envoyée.", "No se pudo activar con seguridad la repetición para {game}: estado NVIDIA o superposición no disponible. Revisa Alt+Z; no se repitió la tecla.", "Impossibile attivare in sicurezza il replay per {game}: stato NVIDIA o overlay non disponibile. Controlla Alt+Z; nessun tasto ripetuto.", "Não foi possível ligar a repetição em segurança para {game}: estado NVIDIA ou sobreposição indisponível. Verifica Alt+Z; a tecla não foi repetida."),
    "invalid_action": ("Неизвестная команда NVIDIA; клавиши не нажаты.", "Unknown NVIDIA action; no keys were pressed.", "Unbekannter NVIDIA-Befehl; keine Taste gedrückt.", "Commande NVIDIA inconnue ; aucune touche utilisée.", "Acción de NVIDIA desconocida; no se pulsaron teclas.", "Comando NVIDIA sconosciuto; nessun tasto premuto.", "Ação NVIDIA desconhecida; nenhuma tecla premida."),
    "cancelled": ("Автовключение NVIDIA отменено.", "NVIDIA auto-enable was cancelled.", "Automatisches Einschalten von NVIDIA abgebrochen.", "Activation automatique NVIDIA annulée.", "Activación automática de NVIDIA cancelada.", "Attivazione automatica NVIDIA annullata.", "Ativação automática NVIDIA cancelada."),
    "press_failed": ("Не удалось отправить {hotkey} в NVIDIA. Проверьте оверлей и права доступа.", "Could not send {hotkey} to NVIDIA. Check the overlay and access permissions.", "{hotkey} konnte nicht an NVIDIA gesendet werden. Overlay und Zugriffsrechte prüfen.", "Impossible d’envoyer {hotkey} à NVIDIA. Vérifiez l’overlay et les autorisations.", "No se pudo enviar {hotkey} a NVIDIA. Revisa la superposición y los permisos.", "Impossibile inviare {hotkey} a NVIDIA. Controlla l’overlay e i permessi.", "Não foi possível enviar {hotkey} à NVIDIA. Verifica a sobreposição e as permissões."),
})


def message(key: str, code: str, /, **values: Any) -> str:
    row = MESSAGES[key]
    text = row[_LANGUAGES.index(code)] if code in _LANGUAGES else row[1]
    return text.format(**values)


def _code(config: Any) -> str:
    code = str(config.get("app.language", "ru") or "ru").casefold().split("-")[0]
    return code if code in _LANGUAGES else "en"


class InstantReplay:
    """One per process; safe across the tool worker and the game tracker."""

    def __init__(self, config: Any, *, press: Callable[[str], None] = press_hotkey,
                 overlay: Callable[[], bool | None] = overlay_running,
                 reader: Callable[[], bool | None] | None = None,
                 is_windows: bool | None = None, sleep: Callable[[float], None] = time.sleep,
                 ensure_overlay: Callable[[], bool] | None = None) -> None:
        self.config = config
        self._press = press
        if ensure_overlay is None:
            from jarvis.integrations.nvidia_startup184 import ensure_overlay_for_command
            ensure_overlay = lambda: ensure_overlay_for_command(self.config, self._overlay, sleep=self._sleep)
        self._ensure_overlay = ensure_overlay
        self._overlay = overlay
        from jarvis.integrations.nvidia_state import replay_state
        self._reader = reader or replay_state
        self._windows = (os.name == "nt") if is_windows is None else is_windows
        self._sleep = sleep
        self._lock = threading.Lock()
        self._observations = threading.local()

    def _hotkey(self) -> str:
        return str(self.config.get("gaming.instant_replay.toggle_hotkey", DEFAULT_TOGGLE_HOTKEY))

    def overlay_state(self) -> bool | None:
        try:
            value = self._overlay()
            return value if isinstance(value, bool) else None
        except Exception:
            logger.debug("NVIDIA overlay probe failed")
            return None

    def state(self) -> bool | None:
        observation = {"backend": "unavailable", "source": "unavailable", "reason": "not_windows",
                       "instant_replay": None, "overlay_process_present": None}
        self._observations.value = observation
        if not self._windows:
            return None
        overlay = self.overlay_state()
        observation["overlay_process_present"] = overlay
        if overlay is not True:
            observation["reason"] = "overlay_missing" if overlay is False else "overlay_probe_unknown"
            return None
        try:
            from jarvis.integrations.nvidia_state import replay_state, replay_observation
            if self._reader is replay_state:
                observation.update(replay_observation())
                value = observation.get("instant_replay")
            else:
                value = self._reader()
                observation.update(source="custom_reader",
                                   reason="observed" if type(value) is bool else "reader_unknown", instant_replay=value)
            return value if isinstance(value, bool) else None
        except Exception:
            observation.update(instant_replay=None, reason="reader_failed")
            logger.debug("Instant Replay state read failed")
            return None

    def last_observation(self) -> dict[str, Any]:
        """Current thread's already collected observation, without I/O."""
        return diagnostic.facts(getattr(self._observations, "value", {"reason": "not_sampled"}))

    def diagnostics(self) -> dict[str, Any]:
        """Read-only, bounded compatibility facts; no registry writes or key injection."""
        self.state()
        return {**self.last_observation(), "windows": self._windows,
                **diagnostic.settings(self.config)}

    def _wait_change(self, before: bool, timeout: float = 3.0) -> bool | None:
        # A short missing read during overlay startup is not a final result.
        now: bool | None = None
        deadline = time.monotonic() + timeout
        for _ in range(max(1, min(20, int(timeout / 0.25)))):
            if time.monotonic() >= deadline:
                break
            self._sleep(0.25)
            now = self.state()
            if now is not None and now != before:
                return now
        return now

    def run(self, action: str, *, guard: Callable[[], bool] | None = None) -> tuple[bool, str, dict[str, Any]]:
        self._observations.value = {"reason": "not_sampled"}
        result = self._run(action, guard=guard)
        if guard is None:
            ok, _text, data = result
            outcome = "observed" if action == "replay_status" else "verified_on" if ok and data.get("instant_replay") is True else data.get("error_kind", "observed" if ok else "unverified")
            observation = self.last_observation()
            if data.get("error_kind") == "input_failed":
                observation["instant_replay"] = None
            diagnostic.emit("check" if action == "replay_status" else "terminal", outcome,
                            observation, pressed=data.get("pressed"), **diagnostic.settings(self.config))
        return result

    def _run(self, action: str, *, guard: Callable[[], bool] | None = None) -> tuple[bool, str, dict[str, Any]]:
        code = _code(self.config)
        if action not in {"replay_on", "replay_off", "replay_toggle", "replay_status"}:
            return False, message("invalid_action", code), {"error_kind": "invalid", "pressed": False}
        if not self._windows:
            return False, message("unsupported", code), {"error_kind": "disabled", "pressed": False}
        # Read, decide, press and verify under one lock. Two concurrent enables
        # must not both observe OFF then toggle ON and straight back OFF.
        with self._lock:
            hotkey = self._hotkey()
            shown = hotkey.upper()
            # NEO 185: a user command (not the game watcher, which has its own launch)
            # may start NVIDIA App in the background first when the setting allows it.
            if (guard is None and action != "replay_status" and self.overlay_state() is not True
                    and self._ensure_overlay()):
                for _ in range(5):  # the replay state appears shortly after the overlay
                    if self.state() is not None:
                        break
                    self._sleep(1.0)
            current = self.state()
            if action == "replay_status":
                key = "unknown" if current is None else ("on" if current else "off")
                return current is not None, message(key, code), {"instant_replay": current, "verified": current is not None, "pressed": False}
            if guard is not None and not guard():
                return False, message("cancelled", code), {"cancelled": True, "pressed": False}
            want = {"replay_on": True, "replay_off": False}.get(action)
            if want is not None and current is want:
                return True, message("already_on" if want else "already_off", code), {"instant_replay": current, "pressed": False, "verified": True}
            try:
                parse_hotkey(hotkey)
            except ValueError:
                return False, message("bad_hotkey", code, hotkey=hotkey), {"error_kind": "invalid", "pressed": False}
            if bool(self.config.get("capture.require_overlay", True)) and self.overlay_state() is not True:
                return False, message("overlay_missing", code), {"error_kind": "disabled", "pressed": False, "overlay_available": False}
            if want is not None and current is None:
                return False, message("unknown_no_press", code, hotkey=shown), {"instant_replay": None, "pressed": False, "error_kind": "unverified"}
            if guard is not None and not guard():
                return False, message("cancelled", code), {"cancelled": True, "pressed": False}
            try:
                self._press(hotkey)
            except Exception:
                logger.debug("NVIDIA replay hotkey failed")
                return False, message("press_failed", code, hotkey=shown), {"error_kind": "input_failed", "pressed": None, "verified": False}
            after = self._wait_change(current) if current is not None else None
            if after is None:
                # An explicit toggle promises a key press, not a particular state.
                return action == "replay_toggle", message("toggled_unverified", code, hotkey=shown), {"instant_replay": None, "pressed": True, "verified": False, "error_kind": "unverified"}
            if after == current:
                return False, message("no_change", code, hotkey=shown), {"instant_replay": after, "pressed": True, "verified": True, "error_kind": "unverified"}
            return True, message("turned_on" if after else "turned_off", code, hotkey=shown), {"instant_replay": after, "pressed": True, "verified": True}


_SHARED: InstantReplay | None = None
_SHARED_LOCK = threading.Lock()


def shared(config: Any) -> InstantReplay:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None or _SHARED.config is not config:
            _SHARED = InstantReplay(config)
        return _SHARED


class ReplayWatch:
    """One opt-in application launch and safe enable attempt per game session.

    Readiness uses the existing game poll for at most 60 seconds. Short work
    runs off the UI thread when a notice callback is supplied. No permanent
    NVIDIA poller is created and a manually disabled replay is never restored.
    """

    def __init__(self, config: Any, replay: InstantReplay | None = None,
                 *, clock: Callable[[], float] = time.monotonic, startup=None) -> None:
        self.config = config
        self.replay = replay or shared(config)
        from jarvis.integrations.nvidia_startup184 import CaptureAppStartup
        self._startup = startup or CaptureAppStartup()
        self._clock = clock
        self._lock = threading.RLock()
        self._last: bool | None = None
        self._game = ""
        self._session: Any = None
        self._generation = 0
        self._profile = None
        self._probe_generation: int | None = None
        self._pending = False
        self._launch_pending = False
        self._launch_attempted = False
        self._busy = False
        self._deadline = 0.0
        self._notify: Callable[[str], None] | None = None
        self._diagnostic_observation = {}
        self._readiness_logged = False
        self._terminal_logged = False

    def _trace(self, event, outcome, *, pressed=None):
        if event == "terminal":
            if self._terminal_logged:
                return
            self._terminal_logged = True
        diagnostic.emit(event, outcome, self._diagnostic_observation,
                        pressed=pressed, **diagnostic.settings(self.config))

    def _profile_token(self):
        return id(self.config), str(getattr(getattr(self.config, "paths", None), "settings", ""))

    def _on(self, key: str, default: bool = True) -> bool:
        return (bool(self.config.get("gaming.enabled", True))
                and bool(self.config.get("gaming.notifications.nvidia_enabled", True))
                and bool(self.config.get("gaming.notifications." + key, default)))

    def auto_enable(self) -> bool:
        return bool(self.config.get("gaming.enabled", True)) and bool(self.config.get("gaming.instant_replay.auto_enable", False))

    def auto_launch(self) -> bool:
        return bool(self.config.get("gaming.enabled", True)) and bool(self.config.get("gaming.instant_replay.auto_launch_app", False))

    def _current(self, generation: int) -> bool:
        return (generation == self._generation and bool(self._game)
                and self._profile == self._profile_token()
                and bool(self.config.get("gaming.enabled", True)))

    def _allowed(self, generation: int) -> bool:
        with self._lock:
            return self._current(generation) and self._pending and self.auto_enable() and self._clock() <= self._deadline

    def _launch_allowed(self, generation: int) -> bool:
        with self._lock:
            return self._current(generation) and self._launch_pending and self.auto_launch() and self._clock() <= self._deadline

    def _dispatch(self, operation, generation: int) -> str | None:
        with self._lock:
            if not self._current(generation) or self._busy or self._probe_generation == generation:
                return None
            self._busy = True
            notify = self._notify

        def work() -> str | None:
            event = None
            try:
                event = operation(generation)
            except Exception:
                logger.debug("NVIDIA startup work failed")
            finally:
                with self._lock:
                    if generation == self._generation:
                        self._busy = False
            with self._lock:
                if not event or not self._current(generation):
                    return None
                key, text = event
                if not self._on(key, key != "replay_unknown_warning"):
                    return None
                if notify is None:
                    return text
            # Notification adapters must not hold up game_ended/UI shutdown.
            # The generation and current notification options were revalidated
            # after the external operation, immediately before this callback.
            try:
                notify(text)
            except Exception:
                logger.debug("NVIDIA notice callback failed")
            return None

        if notify is None:
            return work()
        threading.Thread(target=work, name="neo-replay-startup", daemon=True).start()
        return None

    def _launch_app(self, generation: int):
        with self._lock:
            if not self._launch_allowed(generation) or self._launch_attempted:
                return None
            self._launch_attempted = True
        result = self._startup.ensure_running(guard=lambda: self._launch_allowed(generation))
        with self._lock:
            if not self._launch_allowed(generation):
                return None
            status = result.status
            self._diagnostic_observation.update(
                app_process_present=True if status == "already_running" else None,
                launch_target="nvidia_app" if result.app == "NVIDIA App" else "unavailable")
            self._trace("launch", status)
            if status not in {"already_running", "launch_requested"}:
                # Consume only the optional application launch. The user or
                # NVIDIA may make the overlay ready during the same bounded
                # window, so independent safe replay auto-enable stays pending.
                self._launch_pending = False
            if status in {"cancelled", "unsupported"}:
                return None
            from jarvis.integrations.nvidia_startup_text184 import message as startup_message
            return "nvidia_startup", startup_message(status, _code(self.config), game=self._game, app=result.app)

    def _turn_on(self, generation: int):
        with self._lock:
            if not self._allowed(generation):
                return None
            game = self._game
        ok, text, data = self.replay.run("replay_on", guard=lambda: self._allowed(generation))
        with self._lock:
            if not self._current(generation):
                return None
            if data.get("cancelled"):
                self._pending = False
                self._trace("terminal", "cancelled")
                return None
            state = data.get("instant_replay")
            if state is not None:
                self._last = state
            # Unknown before injection can recover; an uncertain sent key is
            # never retried. Live state is still required by InstantReplay.run.
            self._pending = data.get("pressed") is False and (data.get("overlay_available") is False or state is None and data.get("error_kind") == "unverified")
            self._diagnostic_observation = self.replay.last_observation()
            if data.get("error_kind") == "input_failed":
                self._diagnostic_observation["instant_replay"] = None
            if not self._pending:
                self._trace("terminal", "verified_on" if ok and state is True else data.get("error_kind", "unverified"), pressed=data.get("pressed"))
            code = _code(self.config)
            if ok and state is True:
                return ("replay_changes", message("auto_on", code, game=game, hotkey=self.replay._hotkey().upper())) if data.get("pressed") else None
            if data.get("pressed") is True or data.get("error_kind") == "input_failed":
                key = "auto_pressed" if not data.get("verified") else "auto_failed"
                event = "replay_unknown_warning" if not data.get("verified") else "replay_off_warning"
                return event, message(key, code, game=game, reason=text)
            if self._pending:
                return None
            return "replay_off_warning", message("auto_failed", code, game=game, reason=text)

    def _probe(self, generation: int, replay, *, overlay_only: bool = False):
        """Probe without the watcher lock; callers commit/release under it."""
        try:
            if overlay_only:
                return None, replay.overlay_state()
            state = replay.state()
            with self._lock:
                if not self._current(generation):
                    return None, None
            overlay = replay.overlay_state()
            with self._lock:
                if self._current(generation):
                    self._diagnostic_observation = dict(replay.last_observation(), instant_replay=state,
                                                        overlay_process_present=overlay)
            return state, overlay
        except Exception:
            with self._lock:
                if self._probe_generation == generation:
                    self._probe_generation = None
            raise

    def _start_auto(self, generation: int | None = None) -> str | None:
        with self._lock:
            generation = self._generation if generation is None else generation
            if self._busy or self._probe_generation == generation or not self._allowed(generation):
                return None
            self._probe_generation = generation
            replay = self.replay
        _state, overlay = self._probe(generation, replay, overlay_only=True)
        with self._lock:
            if self._probe_generation == generation:
                self._probe_generation = None
            if not self._allowed(generation) or overlay is not True:
                return None
        # _dispatch and _turn_on revalidate the captured generation/settings.
        return self._dispatch(self._turn_on, generation)

    def game_started(self, game: str, notify: Callable[[str], None] | None = None, *, session: Any = None) -> str | None:
        with self._lock:
            if self._game == game and self._session == session:
                return None
            if self._pending or self._launch_pending:
                self._trace("terminal", "cancelled")
            self._generation += 1
            generation = self._generation
            self._game, self._notify, self._session = game, notify, session
            self._profile = self._profile_token()
            self._busy = False
            self._launch_attempted = False
            self._last = None
            self._diagnostic_observation = {}
            self._readiness_logged = self._terminal_logged = False
            self._pending = self.auto_enable()
            self._launch_pending = self.auto_launch()
            self._deadline = self._clock() + 60.0
            self._probe_generation = generation
            replay = self.replay
        state, overlay = self._probe(generation, replay)
        with self._lock:
            if self._probe_generation == generation:
                self._probe_generation = None
            if not self._current(generation):
                return None
            self._last = state
            in_window = self._clock() <= self._deadline
            self._pending = self._pending and self.auto_enable() and state is not True and in_window
            self._launch_pending = self._launch_pending and self.auto_launch() and overlay is not True and in_window
            self._trace("game_start", "pending" if self._pending or self._launch_pending else "observed")
            self._readiness_logged = overlay is True
            code = _code(self.config)
            launch = overlay is False and self._launch_pending
            auto = state is False and self._pending
            if not launch and overlay is False and self._on("nvidia_startup"):
                from jarvis.integrations.nvidia_startup_text184 import message as startup_message
                return startup_message("missing", code, game=game)
            if not launch and not auto:
                if state is False and self._on("replay_off_warning"):
                    return message("game_warning", code, game=game) + " " + message("auto_hint", code)
                if state is None and self._on("replay_unknown_warning", False):
                    return message("game_unknown", code, game=game)
        if launch:
            return self._dispatch(self._launch_app, generation)
        if auto:
            return self._start_auto(generation)
        return None

    def _expire_pending(self) -> tuple[bool, str | None]:
        """Called under _lock; no external probes or notification callbacks."""
        if not (self._pending or self._launch_pending) or self._clock() <= self._deadline:
            return False, None
        launch_timed_out = self._launch_pending
        self._pending = self._launch_pending = False
        self._trace("terminal", "timeout")
        if launch_timed_out and self._on("nvidia_startup"):
            from jarvis.integrations.nvidia_startup_text184 import message as startup_message
            return True, startup_message("timeout", _code(self.config), game=self._game)
        if not launch_timed_out and self._on("replay_unknown_warning", False):
            return True, message("auto_unavailable", _code(self.config), game=self._game)
        return True, None

    def poll(self) -> str | None:
        with self._lock:
            generation = self._generation
            if not self._current(generation):
                if self._pending or self._launch_pending:
                    self._trace("terminal", "cancelled")
                self._pending = self._launch_pending = False
                return None
            if not self.auto_enable():
                self._pending = False
            if not self.auto_launch():
                self._launch_pending = False
            if not self._pending and not self._launch_pending:
                self._trace("terminal", "disabled" if not self.auto_enable() and not self.auto_launch() else "observed")
            if self._busy or self._probe_generation == generation:
                return None
            expired, text = self._expire_pending()
            if expired:
                return text
            self._probe_generation = generation
            replay = self.replay
        state, overlay = self._probe(generation, replay)
        with self._lock:
            if self._probe_generation == generation:
                self._probe_generation = None
            if not self._current(generation):
                return None
            # Settings, time, and ownership can change while a provider blocks.
            if not self.auto_enable():
                self._pending = False
            if not self.auto_launch():
                self._launch_pending = False
            expired, text = self._expire_pending()
            if expired:
                return text
            if overlay is True:
                self._launch_pending = False
                if not self._readiness_logged:
                    self._readiness_logged = True
                    self._trace("check", "overlay_present")
                if not self._pending:
                    self._trace("terminal", "observed")
            launch = self._launch_pending and not self._launch_attempted and overlay is False
            if state is None:
                auto = False
                # Preserve actual observations over transient gaps.
                if not launch:
                    return None
            else:
                previous, self._last = self._last, state
                if state is True:
                    self._pending = False
                    if not self._launch_pending:
                        self._trace("terminal", "already_on")
                auto = state is False and self._pending
                if not auto and not launch:
                    if not self._on("replay_changes") or state == previous:
                        return None
                    return message(("on" if state else "off") if previous is None else ("changed_on" if state else "changed_off"), _code(self.config))
        if launch:
            return self._dispatch(self._launch_app, generation)
        if auto:
            return self._start_auto(generation)
        return None

    def game_ended(self) -> None:
        with self._lock:
            if self._pending or self._launch_pending:
                self._trace("terminal", "cancelled")
            self._generation += 1
            self._game = ""
            self._session = None
            self._probe_generation = None
            self._pending = self._launch_pending = False
            self._busy = False
            self._last = None


__all__ = ["DEFAULT_TOGGLE_HOTKEY", "InstantReplay", "ReplayWatch", "message", "shared", "OVERLAY_PROCESSES"]
