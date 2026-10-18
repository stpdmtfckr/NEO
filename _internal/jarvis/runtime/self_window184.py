"""Exact self-window commands, before lossy multilingual/ASR normalization.

Only a complete, selected-language imperative owns the assistant window. A
quoted phrase, a negation, a dictated payload or an unrelated application's
window must never become a visibility action through fuzzy matching.
"""
from __future__ import annotations

import re
from functools import lru_cache

from jarvis.runtime.utterance_purpose import strip_assistant_address


_PHRASES = {
    "ru": {
        "show": ("покажись", "покажис", "покожись", "покажысь", "появись", "вернись на экран", "покажись поверх", "покажись поверх окон", "покажись поверх всех окон",
                 "будь поверх всех окон", "поверх всех окон", "выйди поверх всех окон",
                 "покажи окно ассистента", "покажи окно neo", "покажи окно нео", "появись поверх окон", "появись поверх всех окон", "выйди на передний план"),
        "hide": ("спрячься", "спрчься", "спрячся", "спрячьса", "спрячсь", "сприячься", "прячься", "скройся", "скройса", "скрой окно ассистента", "скрой окно neo",
                 "скрой окно нео", "спрячь окно ассистента", "уйди в фон", "уйди за остальные окна", "уйди за другие окна", "спрячься за другие окна", "спрячься за остальные окна"),
    },
    "en": {
        "show": ("show assistant window", "show the assistant window", "show yourself", "bring up the assistant", "come back", "show up", "bring yourself in front of the windows", "come to the front", "bring yourself to the front"),
        "hide": ("hide assistant window", "hide the assistant window", "hide yourself", "go to background", "hide", "hide behind other windows", "hide behind the other windows", "put yourself behind the other windows", "go behind the other windows"),
    },
    "de": {
        "show": ("assistentenfenster anzeigen", "zeig das assistentenfenster", "zeig dich", "zeige dich", "komm vor die anderen fenster", "komm in den vordergrund"),
        "hide": ("assistentenfenster ausblenden", "versteck das assistentenfenster", "versteck dich", "verstecke dich", "verstecke dich hinter den anderen fenstern", "versteck dich hinter den anderen fenstern", "geh hinter die anderen fenster"),
    },
    "fr": {
        "show": ("affiche la fenêtre de l'assistant", "affiche la fenetre de l'assistant", "montre-toi", "passe devant les autres fenêtres", "passe devant les autres fenetres", "passe au premier plan"),
        "hide": ("masque la fenêtre de l'assistant", "masque la fenetre de l'assistant", "cache-toi", "cache-toi derrière les autres fenêtres", "cache-toi derriere les autres fenetres", "passe derrière les autres fenêtres", "passe derriere les autres fenetres"),
    },
    "es": {
        "show": ("muestra la ventana del asistente", "muéstrate", "muestrate", "ponte delante de las ventanas", "ponte delante de las demás ventanas", "ponte delante de las demas ventanas"),
        "hide": ("oculta la ventana del asistente", "escóndete", "escondete", "escóndete detrás de las otras ventanas", "escondete detras de las otras ventanas", "ponte detrás de las demás ventanas", "ponte detras de las demas ventanas"),
    },
    "it": {
        "show": ("mostra la finestra dell'assistente", "mostrati", "vieni davanti alle altre finestre", "vieni in primo piano"),
        "hide": ("nascondi la finestra dell'assistente", "nasconditi", "nasconditi dietro le altre finestre", "vai dietro le altre finestre"),
    },
    "pt": {
        "show": ("mostrar janela do assistente", "mostre a janela do assistente", "mostre-se", "mostra-te", "apareça à frente das outras janelas", "apareca a frente das outras janelas", "vem para a frente"),
        "hide": ("ocultar janela do assistente", "esconda a janela do assistente", "esconda-se", "esconde-te", "esconde-te atrás das outras janelas", "esconde-te atras das outras janelas", "fique atrás das outras janelas", "fique atras das outras janelas"),
    },
}
_POLITE = {
    "ru": ("пожалуйста", "будь добр", "будь добра"), "en": ("please",),
    "de": ("bitte",), "fr": ("s'il te plaît", "s'il vous plaît", "s'il te plait", "s'il vous plait"),
    "es": ("por favor",), "it": ("per favore",), "pt": ("por favor",),
}
_MESSAGES = {
    "ru": ("Показываю окно NEO поверх текущего приложения.",
           "Убираю окно NEO за другие окна. Чтобы вернуть его, скажите «покажись» или нажмите значок NEO в трее.",
           "Команды показа и скрытия окна отключены в настройках NEO.",
           "Не удалось передать команду окну NEO. Попробуйте ещё раз."),
    "en": ("Bringing the NEO window to the front.", "Moving the NEO window behind other windows. Say ‘show yourself’ or use the NEO tray icon to bring it back.", "NEO window visibility commands are disabled in Settings.", "Could not deliver the command to the NEO window. Please try again."),
    "de": ("Ich zeige das NEO-Fenster im Vordergrund.", "Ich verschiebe das NEO-Fenster hinter andere Fenster. Sage ‘zeig dich’ oder nutze das NEO-Symbol im Infobereich.", "Die Sichtbarkeitsbefehle für das NEO-Fenster sind in den Einstellungen deaktiviert.", "Der Befehl konnte nicht an das NEO-Fenster übermittelt werden. Versuche es erneut."),
    "fr": ("J'affiche la fenêtre NEO au premier plan.", "Je place la fenêtre NEO derrière les autres fenêtres. Dis ‘montre-toi’ ou utilise l'icône NEO de la zone de notification.", "Les commandes de visibilité de la fenêtre NEO sont désactivées dans les paramètres.", "Impossible de transmettre la commande à la fenêtre NEO. Réessaie."),
    "es": ("Muestro la ventana de NEO en primer plano.", "Muevo la ventana de NEO detrás de las otras ventanas. Di ‘muéstrate’ o usa el icono de NEO en la bandeja del sistema.", "Los comandos de visibilidad de NEO están desactivados en la configuración.", "No se pudo enviar el comando a la ventana de NEO. Inténtalo de nuevo."),
    "it": ("Mostro la finestra NEO in primo piano.", "Sposto la finestra NEO dietro le altre finestre. Di’ ‘mostrati’ o usa l'icona NEO nell'area di notifica.", "I comandi di visibilità di NEO sono disattivati nelle impostazioni.", "Impossibile inviare il comando alla finestra NEO. Riprova."),
    "pt": ("Mostro a janela do NEO em primeiro plano.", "Coloco a janela do NEO atrás das outras janelas. Diz ‘mostre-se’ ou usa o ícone do NEO na área de notificação.", "Os comandos de visibilidade do NEO estão desativados nas definições.", "Não foi possível enviar o comando à janela do NEO. Tenta novamente."),
}


def parse_self_window(text: str, code: str, *, names=()) -> str | None:
    """Return show/hide only for an entire raw, unquoted command."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 240 or any(c in raw for c in '\n\r;:"«»“”„`'):
        return None
    value = " ".join(raw.casefold().replace("ё", "е").replace("’", "'").split()).strip(" ,.!?")
    # Bounded removal of only addresses and reviewed politeness words. Never
    # strip a negative, discussion wrapper or arbitrary words after the command.
    for _ in range(3):
        previous = value
        value = strip_assistant_address(value, names).strip(" ,.!?")
        for word in _POLITE.get(code, ()):
            value = re.sub(r"^" + re.escape(word) + r"(?:\s+|,\s*)", "", value).strip(" ,.!?")
            value = re.sub(r"(?:\s+|,\s*)" + re.escape(word) + r"$", "", value).strip(" ,.!?")
        if value == previous:
            break
    for action, phrases in _PHRASES.get(code, {}).items():
        if value in phrases:
            return action
    return None


@lru_cache(maxsize=48)
def _configured_names(phrase: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    from jarvis.wake_matcher import wake_alias_is_strong, wake_alias_key, wake_alias_requires_standalone
    return tuple(sorted({wake_alias_key(alias) for alias in aliases
                  if wake_alias_is_strong(alias, phrase) and not wake_alias_requires_standalone(alias)}))


def configured_action(config, text: str) -> str | None:
    active = getattr(config, "active_wake_aliases", None)
    aliases = active() if callable(active) else config.get("wake_word.aliases", ()) or ()
    if isinstance(aliases, str):
        aliases = aliases.splitlines()
    phrase = config.get("wake_word.phrase", "NEO")
    names = _configured_names(phrase, tuple(aliases))
    return parse_self_window(text, str(config.get("app.language", "ru") or "ru"), names=(
        config.get("assistant_name", "NEO"), config.get("wake_word.phrase", ""), *names,
    ))


def visibility_reply(action: str, code: str, *, enabled=True, accepted=True) -> str:
    row = _MESSAGES.get(code, _MESSAGES["ru"])
    return row[2 if not enabled else 3 if not accepted else 0 if action == "show" else 1]


def has_literal_slot(agent, code: str) -> bool:
    """A pending text/name/path response is data, even when it says ‘hide’."""
    import time
    store = getattr(agent, "conversations", None)
    if store is None:
        return False
    pending = store.get(getattr(agent, "_conversation_key", "desktop")).pending_intent
    if pending is None or pending.language != code or time.monotonic() >= pending.expires_at:
        return False
    missing = pending.missing()
    return missing is not None and missing.kind in {"text", "name", "filename", "path", "absolute_source", "choice"}


def queue_visibility(action: str) -> bool:
    if action not in {"show", "hide"}:
        return False
    from jarvis.ui.desktop_events import request_desktop
    return request_desktop("assistant.window." + action)


# NEO 187 HREQ-003: a taskbar click on the active NEO window must minimize it
# and the next click restore it. Explorer sends SC_MINIMIZE only to a top-level
# window whose native style carries WS_MINIMIZEBOX; a frameless/layered Qt
# window can lose it when the HWND is recreated. Re-assert exactly these two
# bits on the assistant's own HWND; nothing else in the style is touched.
GWL_STYLE = -16
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000


def ensure_taskbar_minimize(hwnd: int, user32) -> bool:
    """Return True when the window can be minimized from the taskbar."""
    try:
        style = int(user32.GetWindowLongW(hwnd, GWL_STYLE)) & 0xFFFFFFFF
    except Exception:
        return False
    wanted = WS_SYSMENU | WS_MINIMIZEBOX
    if style & wanted == wanted:
        return True
    updated = style | wanted
    if updated >= 0x80000000:
        updated -= 0x100000000
    try:
        user32.SetWindowLongW(hwnd, GWL_STYLE, updated)
        # SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        return (int(user32.GetWindowLongW(hwnd, GWL_STYLE)) & wanted) == wanted
    except Exception:
        return False


# HIST-046/047: the local autoclicker shortcut acts only on a whole, positive
# order about the autoclicker itself.
_AUTOCLICK_ORDER = re.compile(
    r"^(?:(?:нео|neo|пожалуйста)[\s,]+)*(?:(?:включи(?:те)?|выключи(?:те)?|запусти(?:те)?|останови(?:те)?|отключи(?:те)?|"
    r"стоп|поставь|настрой|сделай|переключи)\s+)?автоклик\w*(?:\s+[\w.,%]+){0,6}$")
_AUTOCLICK_NEGATION = re.compile(r"(?:^|\s)(?:не|ни)\s+(?:надо\s+|нужно\s+)?(?:включ|выключ|запуск|останов|отключ|трогай|меняй)\w*")


def autoclicker_request_shape(raw: str, compact: str) -> str:
    """'command', 'negated' or 'data' for a phrase mentioning the autoclicker."""
    text = str(raw or "")
    if any(mark in text for mark in '"«»“”„:;'):
        return "data"
    if _AUTOCLICK_NEGATION.search(compact):
        return "negated"
    return "command" if _AUTOCLICK_ORDER.match(compact.strip(" .!?")) else "data"
