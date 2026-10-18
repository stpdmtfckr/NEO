"""NEO 188 NEO-ASR-001 / TZ-ASR-EXPECTED-SLOT-PROFILE-01.

Before the next voice answer is decoded, pick a bounded hint profile for the
field NEO is waiting for. The profile belongs to THAT request only (it is
passed per call, the shared recognizer is not changed), so hints never leak
into the next command:

* free text (note, message) and song titles → neutral: no command vocabulary
  that could «correct» the user's words into commands;
* an application name → the user's dictionary and installed application names;
* a duration / unit → the unit words of the selected language;
* yes/no → the confirmation words of the selected language;
* anything else → None (the ordinary command profile stays).

A hint is never a command, a permission or a proven slot value.
"""
from __future__ import annotations

from typing import Any

_UNITS = {'ru': 'секунд секунды минут минуты минуту часов часа час', 'en': 'seconds minutes hours second minute hour',
          'de': 'Sekunden Minuten Stunden Sekunde Minute Stunde', 'fr': 'secondes minutes heures seconde minute heure',
          'es': 'segundos minutos horas segundo minuto hora', 'it': 'secondi minuti ore secondo minuto ora',
          'pt': 'segundos minutos horas segundo minuto hora'}
_YESNO = {'ru': 'да нет верно отмена', 'en': 'yes no correct cancel', 'de': 'ja nein richtig abbrechen', 'fr': 'oui non exact annule',
          'es': 'sí no correcto cancela', 'it': 'sì no esatto annulla', 'pt': 'sim não certo cancela'}
_MAX_HINT_CHARS = 400


def profile_for(slot: Any, code: str, config: Any = None) -> dict[str, str] | None:
    if slot is None:
        return None
    kind, prompt = str(getattr(slot, 'kind', '') or ''), str(getattr(slot, 'prompt', '') or '')
    if kind == 'text' or prompt in {'query', 'search_query', 'queue_query', 'title', 'text', 'game'}:
        return {'initial_prompt': '', 'hotwords': '', 'profile': 'neutral'}
    if kind in {'duration', 'unit188'}:
        return {'initial_prompt': '', 'hotwords': _UNITS.get(code, _UNITS['en']), 'profile': 'duration'}
    if kind == 'confirm':
        return {'initial_prompt': '', 'hotwords': _YESNO.get(code, _YESNO['en']), 'profile': 'confirm'}
    if kind == 'name' and prompt in {'application', 'name'}:
        terms: list[str] = []
        try:
            from jarvis.runtime.dictionary import hotwords as personal, installed_application_terms
            terms = [str(x).strip() for x in personal(config) if str(x).strip()][:20] + list(installed_application_terms(30))
        except Exception:
            terms = []
        return {'initial_prompt': '', 'hotwords': ' '.join(terms)[:_MAX_HINT_CHARS], 'profile': 'application'}
    return None


_STALE = {'ru': 'Пока я распознавал ответ, вопрос стал неактуален — ответ не применён. Повторите, пожалуйста, команду.',
          'en': 'While I was recognizing the answer, the question was no longer current, so the answer was not applied. Please repeat the command.',
          'de': 'Während ich die Antwort erkannt habe, war die Frage nicht mehr aktuell – die Antwort wurde nicht übernommen. Bitte wiederhole den Befehl.',
          'fr': 'Pendant que je reconnaissais la réponse, la question n’était plus d’actualité : la réponse n’a pas été appliquée. Répète la commande, s’il te plaît.',
          'es': 'Mientras reconocía la respuesta, la pregunta dejó de estar vigente y la respuesta no se aplicó. Repite el comando, por favor.',
          'it': 'Mentre riconoscevo la risposta, la domanda non era più attuale e la risposta non è stata applicata. Ripeti il comando, per favore.',
          'pt': 'Enquanto eu reconhecia a resposta, a pergunta deixou de estar ativa e a resposta não foi aplicada. Repita o comando, por favor.'}


def stale_answer(asked: Any, agent: Any, code: str) -> bool:
    """True when the question a voice answer was decoded for is no longer the current one."""
    if asked is None:
        return False
    try:
        from jarvis.runtime.voice_followup156 import pending_question
        return pending_question(agent, code) != asked
    except Exception:
        return True


def stale_text(code: str) -> str:
    return _STALE.get(code, _STALE['en'])
