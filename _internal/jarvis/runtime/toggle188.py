"""NEO 188 atlas contrast «on vs off»: Bluetooth, Wi-Fi and the microphone.

The source atlas declares only the «on» phrases for Bluetooth/Wi-Fi and the
«mute» phrases for the microphone. Their opposites were left to word-by-word
translation, which asked «on or off?» or proposed a different device. This is
a closed, whole-phrase grammar per language: the verb decides the polarity,
the object decides the device, and anything longer is left to the router.
"""
from __future__ import annotations

from functools import lru_cache
import re

_BT = r"(?:the\s+|le\s+|el\s+|il\s+|o\s+|das\s+|den\s+)?(?:bluetooth|блютуз|блютус)"
_WIFI = r"(?:the\s+|le\s+|el\s+|il\s+|o\s+|das\s+|den\s+)?(?:wi-?fi|wlan|вай-?фай)"
_MIC = {
    'ru': r"(?:мой\s+)?микрофон", 'en': r"(?:the\s+|my\s+)?(?:microphone|mic)",
    'de': r"(?:das\s+|mein\s+)?mikro(?:fon)?", 'fr': r"(?:le\s+|mon\s+)?micro(?:phone)?",
    'es': r"(?:el\s+|mi\s+)?micr[óo]fono", 'it': r"(?:il\s+|mio\s+)?microfono", 'pt': r"(?:o\s+|meu\s+)?microfone",
}
# (verb before object, object before particle/verb) for ON and OFF.
_ON = {
    'ru': (r"включи|включить|активируй|активировать", None),
    'en': (r"turn\s+on|switch\s+on|enable|activate", r"(?:turn|switch)\s+{obj}\s+on"),
    'de': (r"aktiviere", r"(?:schalte|mach)\s+{obj}\s+(?:ein|an)|{obj}\s+(?:einschalten|anschalten|aktivieren)"),
    'fr': (r"active|activer|allume|allumer", None),
    'es': (r"activa|activar|enciende|encender", None),
    'it': (r"attiva|attivare|accendi|accendere", None),
    'pt': (r"ativa|ative|ativar|liga|ligue|ligar", None),
}
_OFF = {
    'ru': (r"выключи|выключить|отключи|отключить|деактивируй|деактивировать", None),
    'en': (r"turn\s+off|switch\s+off|disable|deactivate", r"(?:turn|switch)\s+{obj}\s+off"),
    'de': (r"deaktiviere", r"(?:schalte|mach)\s+{obj}\s+aus|{obj}\s+(?:ausschalten|deaktivieren)"),
    'fr': (r"désactive|desactive|désactiver|desactiver|coupe|couper|éteins|eteins|éteindre|eteindre", None),
    'es': (r"desactiva|desactivar|apaga|apagar", None),
    'it': (r"disattiva|disattivare|spegni|spegnere", None),
    'pt': (r"desativa|desative|desativar|desliga|desligue|desligar", None),
}
# Microphone-only phrases: «mute» means off, «unmute» means on.
_MIC_OFF = {
    'ru': ("(?:заглуши|замьють|замуть) {obj}",),
    'en': ("mute {obj}",),
    'de': ("schalte {obj} stumm", "stelle {obj} stumm", "{obj} stummschalten", "{obj} stummstellen"),
    'fr': ("coupe le son du micro(?:phone)?",),
    'es': ("(?:silencia|silenciar) {obj}", "quita el sonido del micr[óo]fono"),
    'it': ("(?:silenzia|silenziare) {obj}",),
    'pt': ("(?:silencia|silenciar) {obj}",),
}
_MIC_ON = {
    'ru': ("(?:размуть|размьють|верни) {obj}",),
    'en': ("unmute {obj}",),
    'de': ("schalte {obj} laut",),
    'fr': ("(?:réactive|reactive|réactiver|reactiver) {obj}",),
    'es': ("(?:reactiva|reactivar) {obj}",),
    'it': ("(?:riattiva|riattivare) {obj}",),
    'pt': ("(?:reativa|reative|reativar) {obj}",),
}
_TARGET = {'bluetooth': ('включи блютуз', 'выключи блютуз'), 'wifi': ('включи вайфай', 'выключи вайфай'),
           'mic': ('включи микрофон', 'выключи микрофон')}


def _whole(forms: list[str]) -> re.Pattern[str]:
    return re.compile(r"^(?:" + "|".join(forms) + r")$", re.I)


@lru_cache(maxsize=7)
def _rules(code: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    rules: list[tuple[re.Pattern[str], str]] = []
    objects = {'bluetooth': _BT, 'wifi': _WIFI, 'mic': _MIC[code]}
    for device, obj in objects.items():
        for table, index in ((_ON, 0), (_OFF, 1)):
            before, after = table[code]
            forms = [rf"(?:{before})\s+{obj}"]
            if after:
                forms.append(after.format(obj=obj))
            rules.append((_whole(forms), _TARGET[device][index]))
    for table, index in ((_MIC_ON, 0), (_MIC_OFF, 1)):
        forms = [template.replace(' ', r'\s+').format(obj=_MIC[code]) for template in table[code]]
        rules.append((_whole(forms), _TARGET['mic'][index]))
    return tuple(rules)


def toggle_command188(text: str, code: str) -> str | None:
    """The internal Russian command for a whole on/off request, or None."""
    from jarvis.runtime.natural_commands import strip_request_shell
    if code not in _ON:
        return None
    value = strip_request_shell(str(text or ""), code).strip().strip(" .!?¡¿,").casefold()
    value = re.sub(r"\s+", " ", value)
    if not value or len(value) > 60:
        return None
    for pattern, command in _rules(code):
        if pattern.match(value):
            return command
    return None
