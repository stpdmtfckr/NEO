"""Reviewed locale-owned OBS grammar. Original input, never fuzzy authority."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
from pathlib import Path
import re

from jarvis.runtime.natural_commands import LANGUAGES,strip_request_shell

ROOT=Path(__file__).resolve().parents[2]/'config'
ACTIONS=('connect','reconnect','disconnect','status','record_start','record_stop','record_pause','record_resume',
         'replay_start','replay_stop','replay_save','stream_start','stream_stop','scenes_list','scene_switch',
         'input_mute','input_unmute','input_volume','source_show','source_hide')
OBS_MARKER=re.compile(r'(?<!\w)(?:OBS|ОБС)(?!\w)',re.I)
_INPUT=ContextVar('obs_original_input184',default=None)
_SLOT=re.compile(r'\{([a-z_]+)\}')
_NAME=r'(?:"[^"\r\n]+"|«[^»\r\n]+»|“[^”\r\n]+”|\x27[^\x27\r\n]+\x27|[^"«»“”\r\n]+?)'
_QUOTES={'"':'"',"'":"'",'«':'»','“':'”'}
_COMPOUND=re.compile(r'\s+(?:and|then|und|dann|et|puis|y|luego|e|poi|depois|и|затем)\s+(?:start|stop|show|hide|send|open|starte|stoppe|ouvre|envoie|démarre|inicia|avvia|начни|останови|отправь|включи|выключи)\b',re.I)
_NEGATIONS={
 'ru':('не','нет','не надо'),'en':('not','no',"don't",'dont','do not'),
 'de':('nicht','nein','kein','keine'),'fr':('ne','pas','non'),
 'es':('no','nunca'),'it':('non','no'),'pt':('não','nao','nunca'),
}
_DELAY_WORDS={
 'ru':(r'через',r'секунд\w*|сек|минут\w*|мин|час\w*|день|дня|дней|недел\w*'),
 'en':(r'in|after',r'seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?'),
 'de':(r'in|nach',r'sekunden?|minuten?|stunden?|tag(?:e|en)?|wochen?'),
 'fr':(r'dans|après',r'secondes?|minutes?|heures?|jours?|semaines?'),
 'es':(r'en|dentro de',r'segundos?|minutos?|horas?|días?|semanas?'),
 'it':(r'tra|fra',r'second[oi]|minut[oi]|or[ae]|giorn[oi]|settiman[ae]'),
 'pt':(r'em|daqui a',r'segundos?|minutos?|horas?|dias?|semanas?'),
}


def _slot_delayed(value,code):
    # A timing suffix is a request qualifier, not an immediately selected name.
    # Quoting the full name remains the explicit escape for literal OBS names.
    after,units=_DELAY_WORDS[code]
    return re.search(r'(?:^|\s)(?:'+after+r')\s*.{1,80}?(?:'+units+r')\b[ .!?…]*$',value,re.I) is not None


def _literal(text):
    value=''.join(r'\s+' if part.isspace() else re.escape(part).replace('’',"['’]").replace("\\'","['’]").replace('ß','(?:ß|ss)') for part in re.split(r'(\s+)',text))
    return value.replace('OBS','(?:OBS|ОБС)')


@lru_cache(maxsize=7)
def templates(code):
    if code not in LANGUAGES:return ()
    canonical=json.loads((ROOT/'canonical_phrases/obs184.json').read_text(encoding='utf-8'))
    variants=json.loads((ROOT/'command_variants/obs184.json').read_text(encoding='utf-8'))
    rows=[]
    for intent,translations in canonical.items():
        for phrase in [translations[LANGUAGES.index(code)],*variants.get(intent,{}).get(code,[])]:
            chunks=[];cursor=0
            for slot in _SLOT.finditer(phrase):
                chunks.append(_literal(phrase[cursor:slot.start()]))
                chunks.append('(?P<'+slot[1]+'>'+(r'\d{1,3}(?:[.,]\d{1,2})?' if slot[1]=='volume' else _NAME)+')')
                cursor=slot.end()
            chunks.append(_literal(phrase[cursor:]))
            rows.append((intent.split('.',1)[1],re.compile(''.join(chunks),re.I),len(_SLOT.sub('',phrase))))
    return tuple(rows)


def _slot_negated(value,code):
    words=value.casefold().replace('’',"'").strip(' .!?…')
    return any(words==token or words.startswith(token+' ') or words.endswith(' '+token) for token in _NEGATIONS.get(code,()))


def parse_obs_command(text,code,aliases=None):
    raw=str(text or '').strip().lstrip('¿¡')
    if (not raw or len(raw)>4096 or code not in LANGUAGES or not OBS_MARKER.search(raw)
            or '\n' in raw or '\r' in raw or raw[0] in _QUOTES):return None
    from jarvis.runtime.utterance_purpose import strip_assistant_address
    raw=strip_request_shell(strip_assistant_address(raw,aliases or ()),code)
    if not raw or raw[0] in _QUOTES:return None
    matches=[]
    for action,pattern,specificity in templates(code):
        match=pattern.fullmatch(raw)
        if match is None and not pattern.groupindex:match=pattern.fullmatch(raw.rstrip(' .!?…'))
        if match is None:continue
        args={'action':action};valid=True
        for key,value in match.groupdict().items():
            value=value.strip()
            if key=='volume':
                number=float(value.replace(',','.'))
                if not 0<=number<=100:valid=False;break
                args[key]=number;continue
            quoted=len(value)>=2 and _QUOTES.get(value[0])==value[-1]
            if quoted:value=value[1:-1]
            elif (any(c in value for c in '"«»“”') or _COMPOUND.search(value)
                    or _slot_negated(value,code) or _slot_delayed(value,code)):valid=False;break
            if not value.strip() or len(value)>512 or any(ord(c)<32 for c in value) or _SLOT.search(value):valid=False;break
            args[key]=value
        if valid:matches.append((specificity,args))
    if not matches:return None
    best=max(row[0] for row in matches)
    results={json.dumps(args,sort_keys=True,ensure_ascii=False):args for score,args in matches if score==best}
    return ('obs_control',next(iter(results.values()))) if len(results)==1 else None


@contextmanager
def obs_input_scope(text,code,aliases=None):
    raw=str(text or '')
    from jarvis.runtime.utterance_purpose import strip_assistant_address
    claim=strip_request_shell(strip_assistant_address(raw,aliases or ()),code)
    token=_INPUT.set((raw,code,parse_obs_command(raw,code,aliases),claim))
    try:yield
    finally:_INPUT.reset(token)


def original_obs_command():
    value=_INPUT.get()
    return value[2] if value else None


def original_obs_text():
    value=_INPUT.get()
    return value[3] if value else ''


def obs_tool_allowed(tool,args,claim_text=''):
    original=_INPUT.get()
    if tool=='capture':
        raw=original[0] if original else str(claim_text or '')
        return (False,'OBS cannot authorize NVIDIA capture') if OBS_MARKER.search(raw) else (True,'')
    if tool!='obs_control':return True,''
    if original is not None:
        parsed=original[2]
        valid=parsed is not None and parsed[1]==args
    else:
        valid=any((parsed:=parse_obs_command(claim_text,code)) is not None and parsed[1]==args for code in LANGUAGES)
    return (True,'') if valid else (False,'A complete original OBS command with unchanged arguments is required')


def confirmation_reply(text,code):
    answers={
      'ru':(('да','подтверждаю'),('нет','отмена')),'en':(('yes','confirm'),('no','cancel')),
      'de':(('ja','bestätigen'),('nein','abbrechen')),'fr':(('oui','confirme'),('non','annule')),
      'es':(('sí','si','confirmo'),('no','cancela')),'it':(('sì','si','confermo'),('no','annulla')),
      'pt':(('sim','confirmo'),('não','nao','cancela')),
    }
    value=str(text or '').strip().casefold().rstrip('.!?…').strip()
    yes,no=answers.get(code,((),()))
    if value in yes or value in no:
        return value in yes
    from jarvis.runtime.confirm_words187 import NO, YES, classify
    verdict=classify(text,code)  # NEO 187 HREQ-014: «да, верно»; «да нет» stays unanswered
    return True if verdict==YES else False if verdict==NO else None


def live_confirmation_text(code):
    return {
      'ru':'Начать прямую трансляцию OBS на настроенную платформу? Видео и звук станут доступны её зрителям.',
      'en':'Start the OBS live stream to its configured platform? Video and audio will become available to its viewers.',
      'de':'Den OBS-Livestream zur eingerichteten Plattform starten? Bild und Ton werden für deren Zuschauer verfügbar.',
      'fr':'Démarrer le direct OBS sur la plateforme configurée ? La vidéo et le son seront accessibles à ses spectateurs.',
      'es':'¿Iniciar la transmisión OBS en la plataforma configurada? El vídeo y el audio estarán disponibles para sus espectadores.',
      'it':'Avviare la diretta OBS sulla piattaforma configurata? Il video e l’audio saranno disponibili ai suoi spettatori.',
      'pt':'Iniciar a transmissão OBS na plataforma configurada? O vídeo e o áudio ficarão disponíveis para os seus espectadores.',
    }.get(code,'Confirm starting the OBS live stream?')


def live_confirmation_changed_text(code):
    return {
      'ru':'Запрос или подключение OBS изменились. Повторите команду начала трансляции и подтвердите её.',
      'en':'The OBS request or connection changed. Request the live stream again and confirm it.',
      'de':'Die OBS-Anfrage oder Verbindung wurde geändert. Fordere den Livestream erneut an und bestätige ihn.',
      'fr':'La demande ou la connexion OBS a changé. Redemandez le direct et confirmez-le.',
      'es':'La solicitud o conexión OBS ha cambiado. Solicita el directo de nuevo y confírmalo.',
      'it':'La richiesta o connessione OBS è cambiata. Richiedi di nuovo la diretta e confermala.',
      'pt':'O pedido ou a ligação OBS mudou. Peça novamente a transmissão e confirme-a.',
    }.get(code,'The OBS request changed. Please request and confirm it again.')
