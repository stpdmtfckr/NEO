"""Small music grammar with explicit ownership; no web/search or title guessing.

Queue questions are reads, even when they contain ``next``. Bare transport is
reported separately: only Agent's current media owner may resolve those words.
"""
from __future__ import annotations
import re
from functools import lru_cache
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell, negated_action

# Reviewed natural short and expanded forms, not generated politeness padding.
FORMS = {
 'queue_status': (
  'очередь музыка|очередь музыки|музыкальная очередь|очередь треков|какие треки дальше|какие песни дальше|что дальше в очереди|что играет дальше|что будет играть дальше|какие следующие треки|следующие песни|следующие треки|покажи следующие песни|назови следующие треки|что будет после текущей песни|покажи список следующих песен|покажи очередь воспроизведения',
  'music queue|song queue|track queue|what tracks are next|what songs are next|what is playing next|what will play next|show the next songs|show the playback queue|what comes after this song',
  'musikwarteschlange|musik warteschlange|welche titel kommen als nächstes|welche lieder kommen als nächstes|was wird als nächstes gespielt|zeige die nächsten lieder|zeige die wiedergabewarteschlange',
  "file de musique|file musicale|file d'attente musicale|quels titres viennent ensuite|quelles chansons viennent ensuite|quelle musique vient après|montre les prochaines chansons|montre la file de lecture",
  'cola de música|cola de canciones|qué pistas siguen|qué canciones siguen|qué se reproduce después|muestra las próximas canciones|muestra la cola de reproducción',
  'coda musicale|coda musica|coda dei brani|quali brani vengono dopo|quali canzoni vengono dopo|cosa viene riprodotto dopo|mostra le prossime canzoni|mostra la coda di riproduzione',
  'fila de música|fila de músicas|quais faixas vêm a seguir|quais músicas vêm a seguir|o que vai tocar depois|mostra as próximas músicas|mostra a fila de reprodução'),
 'status': (
  'сейчас играет|текущий трек|название песни|что играет|какой трек сейчас|какая песня играет|какая музыка сейчас играет|покажи название текущего трека',
  'current track|current song|now playing|what is playing|what song is playing now|show the current song title',
  'aktueller titel|aktuelles lied|was läuft gerade|welches lied läuft gerade|zeige den aktuellen songtitel',
  'titre actuel|chanson actuelle|musique en cours|quelle chanson joue en ce moment|montre le titre actuel',
  'pista actual|canción actual|qué está sonando|qué canción está sonando ahora|muestra el título actual',
  'brano attuale|canzone attuale|cosa sta suonando|quale canzone sta suonando ora|mostra il titolo attuale',
  'faixa atual|música atual|o que está tocando|qual música está tocando agora|mostra o título atual'),
 'next': (
  'следующий трек|следующая песня|переключи песню|переключи музыку|музыку дальше|давай следующий трек|переключи на следующую песню|пропусти текущую песню',
  'next track|next song|skip this song|switch to the next song|play the next track',
  'nächster titel|nächstes lied|überspringe dieses lied|wechsle zum nächsten lied',
  'titre suivant|chanson suivante|passe cette chanson|passe à la chanson suivante',
  'pista siguiente|canción siguiente|salta esta canción|cambia a la siguiente canción',
  'brano successivo|canzone successiva|salta questa canzone|passa alla prossima canzone',
  'faixa seguinte|música seguinte|salta esta música|muda para a próxima música'),
 'previous': (
  'предыдущий трек|предыдущая песня|верни предыдущую песню|музыку назад|переключи на предыдущий трек',
  'previous track|previous song|go back to the previous song|play the previous track',
  'vorheriger titel|vorheriges lied|spiele das vorherige lied|zurück zum vorherigen titel',
  'titre précédent|chanson précédente|reviens à la chanson précédente',
  'pista anterior|canción anterior|vuelve a la canción anterior',
  'brano precedente|canzone precedente|torna alla canzone precedente',
  'faixa anterior|música anterior|volta à música anterior'),
 'pause': (
  'пауза музыки|музыку на паузу|поставь музыку на паузу|приостанови текущую песню',
  'pause music|pause the music|put the music on pause|pause the current song',
  'musik pausieren|pausiere die musik|pausiere das aktuelle lied',
  'pause musique|mets la musique en pause|mets la chanson actuelle en pause',
  'pausa la música|pon la música en pausa|pausa la canción actual',
  'pausa musica|metti la musica in pausa|metti in pausa la canzone attuale',
  'pausa a música|põe a música em pausa|coloca a música atual em pausa'),
 'play': (
  'продолжить музыку|продолжи музыку|возобнови музыку|продолжи играть музыку|сними музыку с паузы',
  'resume music|resume the music|continue playing music|unpause the music',
  'musik fortsetzen|setze die musik fort|spiele die musik weiter',
  'reprends la musique|continue la musique|relance la musique en pause',
  'reanuda la música|continúa la música|sigue reproduciendo música',
  'riprendi la musica|continua la musica|riprendi la riproduzione musicale',
  'retoma a música|continua a música|retoma a reprodução da música'),
 'stop': (
  'стоп музыка|стоп музыке|останови музыку|выключи музыку|останови воспроизведение музыки',
  'stop music|stop the music|stop playing music',
  'musik stoppen|stoppe die musik|beende die musikwiedergabe',
  'arrête la musique|stop musique|arrête la lecture musicale',
  'detén la música|para la música|detén la reproducción musical',
  'ferma la musica|stop musica|interrompi la riproduzione musicale',
  'para a música|pare a música|interrompe a reprodução musical'),
}
BARE = {
 'next': ('дальше|следующая|следующий|скип','next|skip','weiter|nächster','suivant|suivante','siguiente','avanti|successivo','seguinte|próxima'),
 'previous': ('назад|предыдущая|предыдущий','previous|back','zurück|vorheriger','précédent|précédente','anterior|atrás','indietro|precedente','anterior|volta'),
 'pause': ('пауза|приостанови','pause','pause|pausiere','pause','pausa','pausa','pausa'),
 'play': ('продолжи|продолжить|продолжай|возобнови','resume|continue','fortsetzen','reprends|continue','reanuda|continúa','riprendi|continua','retoma|continua'),
 'stop': ('стоп|останови','stop','stopp|stoppe','arrête|stop','para|detén','ferma|stop','para|pare'),
}
# Scope syntax is anchored. A provider merely mentioned inside a title does not
# transfer ownership, and a sentence with two providers remains unresolved.
_SUFFIX = (
 r'(?:\s+(?:в|на|через|из))?\s+(?P<provider>яндекс(?:\s+музык[аеуыи])?|spotify|спотифай|discord|дискорд[еа]?|компьютер[ае]?|локальн(?:о|ая))',
 r'\s+(?:(?:in|on|from|using)\s+)?(?P<provider>yandex music|spotify|discord|computer|local player)',
 r'\s+(?:(?:in|auf|bei|von)\s+)?(?P<provider>yandex music|spotify|discord|computer|lokalen player)',
 r'\s+(?:(?:dans|sur|de)\s+)?(?P<provider>yandex music|spotify|discord|ordinateur|lecteur local)',
 r'\s+(?:(?:en|de)\s+)?(?P<provider>yandex music|spotify|discord|ordenador|reproductor local)',
 r'\s+(?:(?:in|su|da)\s+)?(?P<provider>yandex music|spotify|discord|computer|lettore locale)',
 r'\s+(?:(?:no|na|em|do|da)\s+)?(?P<provider>yandex music|spotify|discord|computador|leitor local)',
)
_QUEUE = (
 r'(?:(?:покажи|скажи|назови|перечисли)\s+(?:мне\s+)?)?(?:очередь\s+(?:музык[аиу]|треков|песен|воспроизведения)|(?:музыкальная|музыкальную)\s+очередь|(?:какие|какая)\s+(?:треки|песни|музыка)\s+(?:(?:будут|пойдут|играют|стоят)\s+)?(?:дальше|следующими|в\s+очереди)|(?:какие|какая)\s+следующие\s+(?:треки|песни|музыка))',
 r'(?:(?:show|tell|list)\s+(?:me\s+)?(?:the\s+)?)?(?:(?:music|song|track|playback)\s+queue|(?:what|which)\s+(?:songs|tracks)\s+(?:are\s+)?(?:next|in\s+(?:the\s+)?queue))',
 r'(?:(?:zeige|nenne)\s+(?:mir\s+)?(?:die\s+)?)?(?:musikwarteschlange|musik\s+warteschlange|(?:welche)\s+(?:titel|lieder)\s+(?:kommen|sind)\s+als\s+nächstes)',
 r"(?:(?:montre|affiche)\s+(?:moi\s+)?(?:la\s+)?)?(?:file\s+(?:d'attente\s+)?(?:musicale|de\s+musique)|quels\s+titres\s+(?:viennent\s+)?ensuite|quelles\s+chansons\s+(?:viennent\s+)?ensuite)",
 r'(?:(?:muestra|dime)\s+(?:la\s+)?)?(?:cola\s+de\s+(?:música|canciones|reproducción)|qué\s+(?:pistas|canciones)\s+(?:siguen|vienen\s+después))',
 r'(?:(?:mostra|dimmi)\s+(?:la\s+)?)?(?:coda\s+(?:musicale|musica|dei\s+brani|di\s+riproduzione)|quali\s+(?:brani|canzoni)\s+(?:vengono\s+)?dopo)',
 r'(?:(?:mostra|mostre|diz)\s+(?:a\s+)?)?(?:fila\s+de\s+(?:música|músicas|reprodução)|quais\s+(?:faixas|músicas)\s+(?:vêm\s+)?a\s+seguir)',
)

@lru_cache(maxsize=7)
def _index(code):
    index = LANGUAGES.index(code)
    return {norm(phrase): action for action, rows in FORMS.items() for phrase in rows[index].split('|')}

def bare_transport157(text: str, code: str) -> str | None:
    if code not in LANGUAGES or negated_action(text, code): return None
    value = norm(strip_request_shell(text, code)); index = LANGUAGES.index(code)
    return next((action for action, rows in BARE.items() if value in {norm(x) for x in rows[index].split('|')}), None)

def parse_music_request157(text: str, code: str) -> tuple[str, dict] | None:
    if code not in LANGUAGES or negated_action(text, code): return None
    value = norm(strip_request_shell(text, code)); index = LANGUAGES.index(code)
    # Literal quotes and URLs may be a song title or a search request.
    if re.search(r'["«»“”]|https?://', value): return None
    tool = 'music'
    suffix = re.search(_SUFFIX[index] + r'$', value, re.I)
    if suffix:
        provider = suffix['provider'].casefold()
        tool = ('discord' if provider.startswith(('discord','дискорд')) else
                'spotify' if provider in {'spotify','спотифай'} else
                'yandex_music' if provider.startswith(('yandex','яндекс')) else 'music')
        value = value[:suffix.start()].strip()
    action = _index(code).get(value)
    if not action and re.fullmatch(_QUEUE[index], value, re.I): action = 'queue_status'
    # A named provider disambiguates short transport; bare words are deferred.
    if not action and suffix: action = bare_transport157(value, code)
    if not action: return None
    if tool == 'discord':
        action = {'queue_status':'music_queue_status','status':'music_queue_status',
                  'next':'music_skip','previous':'music_previous','pause':'music_pause',
                  'play':'music_resume','stop':'music_stop'}[action]
    return tool, {'action':action}
