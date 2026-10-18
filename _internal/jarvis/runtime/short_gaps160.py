"""Reviewed omitted-slot gaps found by the 420-function head audit.

A prefix never inherits a provider or queue position from an omitted suffix.
Only explicit, local typed slots continue to the original registered actions.
"""
from functools import lru_cache
import re
from jarvis.runtime.natural_commands import LANGUAGES,norm,strip_request_shell
from jarvis.runtime.clarification_contracts157 import slot

_SCOPED={'music.playlist','spotify.playlist','yandex_music.playlist','discord.music_playlist',
 'music.queue_add','music.queue_next','spotify.queue_add','spotify.queue_next','yandex_music.queue_add','yandex_music.queue_next','discord.music_queue_add','discord.music_queue_next',
 'music.set_volume','spotify.set_volume','yandex_music.set_volume','discord.music_set_volume',
 'music.seek','spotify.seek','media.seek_to','activity.search','game_locations.find','game_locations.open','mini_browser.zoom'}

@lru_cache(maxsize=7)
def heads(code):
    from jarvis.runtime.canonical_commands import templates
    result={}
    for row in templates(code):
        match=re.search(r'\{\w+\}',row.phrase)
        if row.intent not in _SCOPED or match is None:
            continue
        head=norm(row.phrase[:match.start()])
        if len(head.split())>=2:
            result.setdefault(head,set()).add(row.intent)
    aliases={
        'activity.search': ('найди в истории активности','search activity history','suche im Aktivitätsverlauf','cherche dans l’historique d’activité','busca en el historial de actividad','cerca nella cronologia attività','procura no histórico de atividade'),
        'mini_browser.zoom': ('масштаб мини-браузера','mini browser zoom','stelle den Zoom des Mini-Browsers ein','zoom du mini-navigateur','zoom del mini navegador','zoom del mini browser','zoom do mini navegador'),
    }
    for intent, phrases in aliases.items():
        result.setdefault(norm(phrases[LANGUAGES.index(code)]),set()).add(intent)
    return result


def provider(value,code):
    if re.search(r'(?<!\w)(?:discord|дискорд\w*)(?!\w)',value):return 'discord'
    if re.search(r'(?<!\w)(?:spotify|спотифай)(?!\w)',value):return 'spotify'
    if re.search(r'(?<!\w)(?:yandex|яндекс\w*)(?!\w)',value):return 'yandex_music'
    return 'music'


def partial(text,code):
    if code not in LANGUAGES:return None
    value=norm(strip_request_shell(text,code));i=LANGUAGES.index(code)
    intents=heads(code).get(value,set())
    if value==norm(('позиция музыки','music position','Musikposition','position dans la musique','posición de la música','posizione nella musica','posição na música')[i]):
        return 'music',{'action':'seek'},(slot('seconds','number157:0:86400','field157:seconds'),)
    if value==norm(('позиция видео YouTube','YouTube video position','Position im YouTube-Video','position dans la vidéo YouTube','posición en el vídeo YouTube','posizione nel video YouTube','posição no vídeo YouTube')[i]):
        return 'media',{'action':'seek_to','target':'youtube'},(slot('position_seconds','number157:0:86400','field157:position_seconds'),)
    if value in {'перейди к'} and code=='ru':
        return 'command_choice160',{},(('command','choice','choose','',(
            ('Моменту в видео YouTube','перемотай видео к'),('Вкладке браузера','активируй вкладку по названию'),('Окну приложения','активируй окно'))),)
    if value=='перемотай видео к' and code=='ru':
        return 'media',{'action':'seek_to','target':'youtube'},(slot('position_seconds','number157:0:86400','field157:position_seconds'),)
    if value=='активируй вкладку по названию' and code=='ru':
        return 'browser',{'action':'activate_tab_title'},(slot('value','name','tab_title'),)
    if value=='активируй окно' and code=='ru':
        return 'window',{'action':'focus'},(slot('title','name','window156'),)
    if not intents:return None
    if 'activity.search' in intents:
        return 'activity',{'action':'search'},(slot('query','text','activity_query160'),)
    if 'mini_browser.zoom' in intents:
        return 'mini_browser',{'action':'zoom'},(slot('zoom','zoom160','zoom160'),)
    if any(x.startswith('game_locations.') for x in intents):
        from jarvis.runtime.feature_commands import _GAME_KIND_WORDS
        kinds=[kind for kind,words in _GAME_KIND_WORDS.items() if re.search(r'(?<!\w)(?:'+words[i]+r')(?!\w)',value,re.I)]
        # Overlapping Portuguese "gravações" / "gravações de vídeo" chooses
        # the longest literal expression. Full game name is still required.
        if 'videos' in kinds:kinds=['videos']
        action='open' if 'game_locations.open' in intents else 'find'
        return 'game_locations',{'action':action,'kind':kinds[0] if len(kinds)==1 else 'all'},(slot('name','name','location160'),)
    media=[x for x in intents if x.split('.')[0] in {'music','spotify','yandex_music','discord'}]
    if any(x.endswith(('.playlist','.music_playlist')) for x in media):
        owner=provider(value,code)
        return owner,{'action':'music_playlist' if owner=='discord' else 'playlist'},(slot('playlist','name','playlist'),)
    if any(x.endswith(('.set_volume','.music_set_volume')) for x in media):
        owner=provider(value,code)
        return owner,{'action':'music_set_volume' if owner=='discord' else 'set_volume'},(slot('volume','percent156','percent156'),)
    queue=[x for x in media if x.endswith(('queue_add','queue_next'))]
    if queue:
        # A truncated "play the song" is already a complete playback *head*;
        # removing "next"/provider must not turn it into queue mutation.
        additions=(r'добавь|добавить',r'queue|add',r'reihe|füge',r'ajoute|ajouter',r'añade|añadir|agrega',r'aggiungi|aggiungere',r'adiciona|adicione|adicionar')
        if not re.match(r'(?:'+additions[i]+r')\b',value,re.I):return None
        owner=provider(value,code);prefix='music_' if owner=='discord' else ''
        next_markers=(r'следующ|далее',r'next',r'nächst',r'prochain|juste après',r'siguiente|a continuación',r'prossim|subito dopo',r'a seguir|próxim')
        end_markers=(r'в (?:конец )?очеред',r'(?:to|in) (?:the )?(?:end|queue)',r'(?:ans ende|in die warteschlange)',r'(?:fin de|dans la) file',r'(?:final|a la cola)',r'(?:in fondo|nella coda)',r'(?:fim|na fila)')
        selected='queue_next' if re.search(next_markers[i],value,re.I) else 'queue_add' if re.search(end_markers[i],value,re.I) else ''
        if selected:
            return owner,{'action':prefix+selected},(slot('query','name','queue_query'),)
        labels=(('В конец очереди','Следующим'),('At the end','Play next'),('Ans Ende','Als Nächstes'),('En fin de file','Juste après'),('Al final','A continuación'),('In fondo alla coda','Subito dopo'),('No fim da fila','A seguir'))[i]
        return owner,{},(('action','choice','choose','',tuple(zip(labels,(prefix+'queue_add',prefix+'queue_next')))),slot('query','name','queue_query'))
    if 'media.seek_to' in intents and any(x.endswith('.seek') for x in media) and not re.search(r'youtube|ютуб',value,re.I):
        music_head=('позиция музыки','music position','Musikposition','position dans la musique','posición de la música','posizione nella musica','posição na música')[i]
        video_head=('позиция видео YouTube','YouTube video position','Position im YouTube-Video','position dans la vidéo YouTube','posición en el vídeo YouTube','posizione nel video YouTube','posição no vídeo YouTube')[i]
        return 'command_choice160',{},(('command','choice','choose','',((music_head,music_head),(video_head,video_head))),)
    if 'media.seek_to' in intents:
        return 'media',{'action':'seek_to','target':'youtube'},(slot('position_seconds','number157:0:86400','field157:position_seconds'),)
    if any(x.endswith('.seek') for x in media):
        return provider(value,code),{'action':'seek'},(slot('seconds','number157:0:86400','field157:seconds'),)
    return None
