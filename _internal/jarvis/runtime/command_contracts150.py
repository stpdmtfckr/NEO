"""Reviewed complete/short forms; raw native payloads before token normalization.
No execution, no model, no permission to execute arbitrary sentence fragments.
"""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import json
import re
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell, negated_action

_SHORT = {
 'ru': {'очередь дискорд':('discord','music_queue_status'),'очеред дискорд':('discord','music_queue_status'),'история песен дискорд':('discord','music_queue_status'),'дискорд очередь':('discord','music_queue_status'),'текущий буфер обмена':('clipboard','current'),'буфер обмена':('clipboard','current'),'покажи список панелей':('overlay','list'),'список панелей':('overlay','list'),'активные таймеры':('timer','list'),'список таймеров':('timer','list'),'какие приложения сейчас активны':('application','list_active'),'подключи discord-бота к голосовому каналу':('discord','bot_join_voice'),'отключи discord-бота от голосового канала':('discord','bot_leave_voice')},
 'en': {'discord queue':('discord','music_queue_status'),'discord music history':('discord','music_queue_status'),'current clipboard':('clipboard','current'),'open applications':('application','list_active'),'active timers':('timer','list')},
 'de': {'discord warteschlange':('discord','music_queue_status'),'discord musikverlauf':('discord','music_queue_status'),'aktuelle zwischenablage':('clipboard','current'),'aktive timer':('timer','list')},
 'fr': {'file discord':('discord','music_queue_status'),'file d’attente discord':('discord','music_queue_status'),'historique musical discord':('discord','music_queue_status'),'presse-papiers actuel':('clipboard','current'),'minuteurs actifs':('timer','list')},
 'es': {'cola discord':('discord','music_queue_status'),'historial de música discord':('discord','music_queue_status'),'portapapeles actual':('clipboard','current'),'temporizadores activos':('timer','list')},
 'it': {'coda discord':('discord','music_queue_status'),'cronologia musica discord':('discord','music_queue_status'),'appunti attuali':('clipboard','current'),'timer attivi':('timer','list')},
 'pt': {'fila discord':('discord','music_queue_status'),'histórico de música discord':('discord','music_queue_status'),'área de transferência atual':('clipboard','current'),'temporizadores ativos':('timer','list')},
}
_READS={('application','list'),('application','list_active'),('clipboard','current'),('clipboard','list'),('overlay','list'),('timer','list'),('discord','list_channels'),('discord','text_channel_status'),('system','top_processes'),('system_monitor','summary'),('browser','list_tabs'),('browser','context'),('browser_advanced','context'),('transfer','clipboard_to_discord'),('transfer','selected_explorer_file_to_discord'),('discord','user_account_automation_unavailable')}
_TRANSPORT={'my_tracks','my_wave','status','background_status','pause','stop','next','previous','play','queue_status','queue_clear','shuffle_on','shuffle_off','repeat_track','repeat_playlist','repeat_off','like','dislike','unlike','volume_up','volume_down'}
_DISCORD={'music_queue_status','music_queue_clear','music_pause','music_resume','music_stop','music_skip','music_previous','music_my_wave','music_my_tracks','music_repeat_track','music_repeat_playlist','music_repeat_off','music_shuffle_on','music_shuffle_off','music_like','music_dislike','music_volume_up','music_volume_down','bot_join_voice','bot_leave_voice','list_channels','background_status','text_channel_status'}
_NATIVE={('discord',a) for a in ('music_bot_play','music_queue_add','music_queue_next','music_playlist','music_youtube_url','music_set_volume')}|{('music',a) for a in ('play_track','search_track','queue_add','queue_next','playlist','set_volume')}|{(t,a) for t in ('spotify','yandex_music') for a in ('play_track','search_track','queue_add','queue_next','playlist','play_playlist','set_volume')}|{('email','mark_read'),('clipboard','set'),('application','info'),('browser','open_url'),('browser','activate_tab_title'),('overlay','checklist_create'),('overlay','checklist_check'),('overlay','checklist_uncheck'),('transfer','clipboard_to_discord'),('transfer','selected_explorer_file_to_discord')}
_READS |= {('email', a) for a in ('providers','folders','open_inbox','status','unread','unread_list','unread_today','unread_today_list','unread_recent24h','unread_recent24h_list','recent24h','recent24h_list','today','today_list','total','latest')}
# These native, exact requests have useful defaults and require no user-owned
# payload. Bind them before translation/fuzzy fallback: otherwise a calendar
# status request can become an application title, or air temperature can become
# a hardware temperature request. This does not authorize arbitrary fragments.
_READS |= {
    ('autoclicker', 'status'), ('audio', 'list_devices'),
    ('briefing', 'summary'), ('briefing', 'startup_summary'),
    ('diagnostics', 'self_test'), ('diagnostics', 'latency'),
    ('diagnostics', 'plugins'), ('diagnostics', 'objects'),
    ('filesystem', 'list_drives'), ('filesystem', 'disk_usage'),
    ('hardware', 'status'), ('input_control', 'list_controls'),
    ('network', 'info'), ('memory', 'list'), ('scenario', 'list'),
    ('history', 'recent'), ('window', 'active'), ('window', 'list'),
    ('system', 'status'), ('system', 'list_power_plans'),
    ('system', 'get_power_plan'), ('system', 'get_mouse_sensitivity'),
} | {('system_monitor', a) for a in ('cpu', 'gpu', 'memory', 'disks', 'network')} \
  | {('google_calendar', a) for a in ('status', 'today', 'tomorrow', 'upcoming', 'list', 'all', 'next')} \
  | {('telegram_messages', a) for a in ('status', 'count', 'recent', 'latest', 'diagnostic')} \
  | {('weather', a) for a in ('summary', 'current', 'temperature', 'forecast', 'rain', 'precipitation', 'humidity', 'wind', 'sunrise', 'sunset')}
# Explicit native catalog entries whose implementations need no object/number.
# Every call still traverses the registry's normal schema and risk policy.
_READS |= {
 ('audio','mute'), ('audio','restore_ducking'), ('audio','duck_system'),
 ('clipboard','clear'), ('clipboard','copy_page_url'),
 ('history','undo'), ('text_edit','fix_selection'), ('media','fullscreen'), ('media','theater'), ('browser','open'),
 ('game','status'), ('game','state'), ('game','session_info'), ('game','show_session'),
 ('music','play_music'),
 ('browser_advanced','pin_current'), ('browser_advanced','move_to_new_window'),
 ('autoclicker','start'), ('autoclicker','stop'), ('autoclicker','toggle'),
 ('automation','cancel'),
 ('browser','close_tab'), ('browser','new_tab'), ('browser','clear_history'), ('browser','full_cleanup'),
 ('browser_advanced','close_others'), ('browser_advanced','restore_closed'),
 ('network','flush_dns'),
 ('overlay','hide_all'), ('overlay','delete_all'),
 ('screen','capture_window'), ('screen','analyze'), ('screen','analyze_window'),
 ('spotify','open'),
 ('system','sleep'), ('system','hibernate'), ('system','turn_off_displays'),
 ('system','open_task_manager'), ('system','open_settings'), ('system','cancel_shutdown'),
 ('text_edit','select_previous_paragraph'), ('filesystem','undo_last_change'),
}
_NATIVE |= {('audio','change_target_volume'), ('clipboard','copy_history'),('clipboard','paste_history'),('spotify','seek'),('music','seek')}
_NATIVE.add(('overlay','style'))
# This existing action requests the Spotify connection settings panel; it does
# not perform OAuth or connect an account without the user's browser flow.
_SETTINGS = {('spotify', 'authorize')}
def short_request(text, code):
    # Kept for backwards imports; dispatch uses the compiled map below.
    return _short_index(code).get(norm(text))

@lru_cache(maxsize=7)
def _short_index(code):
    data=json.loads((Path(__file__).resolve().parents[2]/'config/short_commands153.json').read_text(encoding='utf-8'))
    result={norm(k):tuple(v) for k,v in data['languages'].get(code,{}).items()}
    result.update({norm(k):v for k,v in _SHORT.get(code,{}).items()})
    extra=json.loads((Path(__file__).resolve().parents[2]/'config/short_commands155.json').read_text(encoding='utf-8'))
    result.update({norm(k):tuple(v) for k,v in extra['languages'].get(code,{}).items()})
    reviewed=json.loads((Path(__file__).resolve().parents[2]/'config/short_commands156.json').read_text(encoding='utf-8'))
    result.update({norm(k):tuple(v) for k,v in reviewed['languages'].get(code,{}).items()})
    return result

_NUMERIC={'font_size','volume','step','index','limit','offset','value','seconds'}

def _target(tool,action):
    from jarvis.tools.argument_validation import normalize_tool_call
    t,a=normalize_tool_call(tool,{'action':action});return t,a.get('action',action)


_MISSING = {
 ('application','info'): (('name','name','application'),),
 ('discord','music_queue_next'): (('query','name','queue_query'),),
 ('discord','music_queue_add'): (('query','name','queue_query'),),
 ('discord','music_playlist'): (('playlist','name','playlist'),),
 ('music','queue_next'): (('query','name','queue_query'),),
 ('music','queue_add'): (('query','name','queue_query'),),
 ('email','mark_read'): (('message_id','name','email_id'),),
 ('email','draft'): (('to','email','email_to'),('subject','text','email_subject'),('body','text','email_body')),
 ('clipboard','set'): (('text','text','clipboard_text'),),
 ('browser','open_url'): (('value','url','url'),),
 ('overlay','checklist_create'): (('name','name','panel_name'),),
 ('overlay','checklist_check'): (('name','name','panel_name'),('item','positive_integer','item_index')),
 ('overlay','checklist_uncheck'): (('name','name','panel_name'),('item','positive_integer','item_index')),
 ('filesystem','create_folder'): (('folder','path','destination'),('name','filename','new_name')),
 ('filesystem','create_text_file'): (('folder','path','destination'),('name','filename','new_name'),('content','text','text')),
 ('timer','alarm'): (('text','clock','alarm_time'),),
 ('spotify','queue_add'): (('query','name','queue_query'),),
 ('spotify','playlist'): (('playlist','name','playlist'),),
 ('yandex_music','playlist'): (('playlist','name','playlist'),),
 ('discord','music_youtube_url'): (('query','url','url'),),
}

@lru_cache(maxsize=7)
def _missing_index(code):
 data=json.loads((Path(__file__).resolve().parents[1]/'locales/commands'/f'{code}.json').read_text(encoding='utf-8'))
 owners={}
 for entry,phrases in data.get('action_phrases',{}).items():
  target=_target(*entry.split('.',1))
  if target not in _MISSING:continue
  for phrase in phrases:
   if '{' in phrase or '}' in phrase:continue
   owners.setdefault(norm(strip_request_shell(phrase,code)),set()).add(target)
 return {key:next(iter(v)) for key,v in owners.items() if len(v)==1}

@lru_cache(maxsize=7)
def _index(code):
    data=json.loads((Path(__file__).resolve().parents[1]/'locales/commands'/f'{code}.json').read_text(encoding='utf-8'))
    owners={};eligible_targets=set();patterns=[];seen=set()
    for entry,phrases in data.get('action_phrases',{}).items():
        tool,action=entry.split('.',1);target=_target(tool,action)
        eligible=target in _READS or target in _SETTINGS or (target[0] in {'music','spotify','yandex_music','media'} and target[1] in _TRANSPORT) or (target[0]=='discord' and target[1] in _DISCORD) or target in {('chance','roll_die'),('chance','flip_coin'),('system','restart'),('system','shutdown')}
        if eligible:eligible_targets.add(target)
        for phrase in phrases:
            value=strip_request_shell(phrase,code)
            fields=re.findall(r'\{(\w+)\}',value)
            if not fields:
                # A phrase shared by two different actions is ambiguous even
                # when only one of them is eligible for exact no-payload use.
                owners.setdefault(norm(value),set()).add(target)
                continue
            if target not in _NATIVE or len(set(fields))!=len(fields):continue
            if value.lstrip().startswith('{') and target!=('audio','change_target_volume'):continue  # only reviewed German target-first volume syntax
            if target==('overlay','style') and not re.search(r'размер текста|font size|text size|schriftgr|taille.*(?:texte|police)|tama.o.*(?:texto|fuente)|dimensione.*(?:testo|carattere)|tamanho.*(?:texto|fonte)',value,re.I):continue
            if not set(fields)<= {'query','playlist','name','text','message_id','url','title','volume','value','index','server','channel','font_size','target','seconds','position_ms'}:continue
            tokens=re.split(r'(\{\w+\})',value);parts=[]
            for token in tokens:
                if re.fullmatch(r'\{\w+\}',token):parts.append('(?P<'+token[1:-1]+'>'+('[0-9]+(?:[.,][0-9]+)?' if token[1:-1] in _NUMERIC else '.+?')+')')
                else:parts.append(re.sub(r'\\\s+', r'\\s+', re.escape(token)))
            pattern=''.join(parts)
            key=(target,pattern)
            if key not in seen:
                seen.add(key);patterns.append((len(re.sub(r'\{\w+\}','',value)),re.compile('^'+pattern+'$',re.I),target,fields))
    exact={k:next(iter(v)) for k,v in owners.items() if len(v)==1 and next(iter(v)) in eligible_targets}
    exact.update({norm(k):v for k,v in _SHORT.get(code,{}).items()})
    return exact,tuple(sorted(patterns,key=lambda x:x[0],reverse=True))

def _value(x):
    v=x.strip()
    if len(v)>1 and (v[0],v[-1]) in {('"','"'),('«','»'),('“','”')}:v=v[1:-1]
    return v

_PLAY=(r'включи|поставь|проиграй|запусти',r'play|start',r'spiele|starte',r'joue|lance|mets|lis',r'reproduce|pon|toca',r'riproduci|metti|avvia',r'toca|toque|reproduz|reproduza|inicie|inicia|põe')
_SEARCH=(r'найди(?:те)?|поищи(?:те)?',r'find|search',r'finde|suche',r'trouve|cherche',r'busca|encuentra',r'cerca|trova',r'procura|procure|encontra|encontre|busca')
_PROVIDER=r'discord|дискорд(?:е|а)?|spotify|спотифай|yandex music|яндекс музык(?:а|у|е)|яндекс мьюзик'
_PREP=r'в|на|in|on|dans|sur|en|nel|su|no|na|em|im|bei|auf'
_SONG=(r'(?:песню|трек|музыку|композицию)',
 r'(?:for\s+)?(?:(?:a|the)\s+)?(?:song|track|music)',
 r'(?:nach\s+)?(?:(?:ein|eine|einen|das|dem|einem|den|der)\s+)?(?:lied|musik|song|titel)',
 r'(?:(?:une|un|le|la|de la)\s+)?(?:chanson|musique|morceau|titre)',
 r'(?:(?:una|un|la|el)\s+)?(?:canción|música|tema|pista)',
 r'(?:(?:una|un|la|il)\s+)?(?:canzone|musica|brano|traccia|pezzo)',
 r'(?:(?:uma|um|a|o)\s+)?(?:música|musica|canção|faixa)')

def provider_request(text,code):
    """Only explicit command + syntactically identified provider. Quoted data opaque."""
    i=LANGUAGES.index(code);v=strip_request_shell(text,code).strip(' .!?¿¡')
    m=re.match(r'^(?P<verb>'+_PLAY[i]+'|'+_SEARCH[i]+r')(?:\s+(?:сейчас|now|jetzt|maintenant|ahora|ora|agora))?\s+(?P<tail>.+)$',v,re.I)
    if not m:return None
    tail=m['tail'];protected=re.sub(r'"[^"]*"|«[^»]*»|“[^”]*”',lambda x:' '*len(x[0]),tail)
    hits=list(re.finditer(r'\b(?:'+_PREP+r')\s+(?P<provider>'+_PROVIDER+r')(?=$|\s|[,;])',protected,re.I))
    if len(hits)!=1:return None
    hit=hits[0];name=hit['provider'].casefold();tool='discord' if name.startswith(('discord','дискорд')) else 'spotify' if name in {'spotify','спотифай'} else 'yandex_music'
    query=(tail[:hit.start()]+' '+tail[hit.end():]).strip(' ,;')
    query=re.sub(r'^'+_SONG[i]+r'(?:\s+|$)','',query,flags=re.I).strip()
    is_search=bool(re.fullmatch(_SEARCH[i],m['verb'],re.I))
    if tool=='discord' and is_search:return None  # no imaginary Discord search tool
    playlist=bool(re.match(r'^(?:(?:a|the|une|la|una|uma|eine|die)\s+)?(?:плейлист|playlist|lista de reprodução|lista de reproducción|wiedergabeliste)(?:\s+|$)',query,re.I))
    if playlist:
        query=re.sub(r'^(?:(?:a|the|une|la|una|uma|eine|die)\s+)?(?:плейлист|playlist|lista de reprodução|lista de reproducción|wiedergabeliste)(?:\s+|$)','',query,flags=re.I)
    if playlist and norm(query) in {'par son nom','by name','by its name','по названию','по имени','por nome','pelo nome','por su nombre','per nome','nach namen'}:query=''
    if not playlist and query and norm(query) in {'моя волна','мою волну','minha onda','my wave','mes titres','мои треки'}:return None
    action='music_playlist' if tool=='discord' and playlist else 'music_bot_play' if tool=='discord' else 'playlist' if playlist else 'search_track' if is_search else 'play_track'
    field='playlist' if playlist else 'query'
    return tool,{'action':action,**({field:_value(query)} if query.strip() else {})},field

def compound_music(text,code):
    """Explicit find-and-play is playback; ordinary search remains read-only."""
    i=LANGUAGES.index(code)
    connectors=('и','and','und','et','y','e','e')
    value=strip_request_shell(text,code).strip(' .!?¿¡')
    match=re.fullmatch(r'(?:'+_SEARCH[i]+r')\s+'+connectors[i]+r'\s+(?P<play>'+_PLAY[i]+r')\s+(?P<tail>.+)',value,re.I)
    if not match:return None
    request=provider_request(match['play']+' '+match['tail'],code)
    if request and request[2] in request[1] and request[1]['action'] in {'play_track','music_bot_play'}:
        return request[:2]
    return None

def partial(text,code):
    if code not in LANGUAGES or negated_action(text,code):return None
    v=norm(strip_request_shell(text,code))
    request=provider_request(text,code)
    if request and request[2] not in request[1]:
        tool,args,field=request;prompt='playlist' if 'playlist' in args['action'] else 'search_query' if args['action']=='search_track' else 'query'
        return tool,args,((field,'name',prompt),)
    target=_missing_index(code).get(v)
    if target:return target[0],{'action':target[1]},_MISSING[target]
    if code!='ru':return None
    checks=[(r'(?:добавь|создай|поставь|запусти)(?: сейчас| мне)? таймер','timer',{'action':'timer'},(('seconds','duration','duration'),)),
      (r'(?:добавь|создай|поставь)(?: сейчас| мне)? будильник','timer',{'action':'alarm'},(('text','clock','alarm_time'),)),
      (r'(?:добавь|создай|подготовь)(?: сейчас| мне)? черновик(?: письма)?','email',{'action':'draft'},(('to','email','email_to'),('subject','text','email_subject'),('body','text','email_body'))),
      (r'(?:создай|добавь)(?: сейчас)?(?: новую)? папку','filesystem',{'action':'create_folder'},(('folder','path','destination'),('name','filename','new_name'))),
      (r'(?:создай|добавь)(?: сейчас)?(?: новый)? текстовый файл','filesystem',{'action':'create_text_file'},(('folder','path','destination'),('name','filename','new_name'),('content','text','text'))),
      (r'(?:включи|поставь)(?: сейчас| мне)? музыку','music',{'action':'play_track'},(('query','name','query'),)),
      (r'(?:отметь|пометь) письмо как прочитанное','email',{'action':'mark_read'},(('message_id','name','email_id'),)),
      (r'(?:установи|заполни) буфер обмена','clipboard',{'action':'set'},(('text','text','clipboard_text'),))]
    for pat,t,a,s in checks:
        if re.fullmatch(pat,v,re.I):return t,a,s
    return None

_PAYLOAD_FIELDS183=frozenset({'text','content','body','message','note','value','reply'})

def complete(text,code):
    result=_complete150(text,code)
    if result is not None and result[0] in {"music","spotify","yandex_music","discord","media"}:
        from jarvis.runtime.canonical_commands import _MODE_WORD186
        payload=str(result[1].get("query","") or result[1].get("playlist","") or "").strip()
        if payload and _MODE_WORD186.fullmatch(payload):
            # MEDIA185-U03: «включи перемешивание в Spotify» is a mode command.
            return None
    return result


def _complete150(text,code):
    if code not in LANGUAGES or negated_action(text,code):return None
    from jarvis.runtime.object_commands157 import complete_object_request157
    object_request=complete_object_request157(text,code)
    if object_request is not None:return object_request
    full=strip_request_shell(text,code).strip(' ¿¡')        # NEO 183: text payloads keep «!», «?», «…»
    if code == 'ru':
        # Courtesy inflection changes the command verb, never a song payload.
        full=re.sub(r'^(?:найдите|поищите)(?=\s+(?:песню|трек|музыку)\b)', 'найди', full, count=1, flags=re.I)
    v=full.strip(' .!?¿¡')
    if code == 'de':
        archive = re.fullmatch(r'(?P<source>(?:[A-Za-z]:[\\/]|/).+?) in das Archiv (?P<destination>(?:[A-Za-z]:[\\/]|/).+?) (?:komprimieren|packen)', v, re.I)
        if archive:
            return 'filesystem', {'action':'zip', **{k:_value(x) for k,x in archive.groupdict().items()}}
    from jarvis.runtime.script_requests157 import script_request157
    script = script_request157(text, code)
    if script is not None:return script
    from jarvis.runtime.everyday_requests157 import everyday_request157
    everyday = everyday_request157(v, code)
    if everyday is not None:return everyday
    from jarvis.runtime.music_requests157 import parse_music_request157
    music_request = parse_music_request157(v, code)
    if music_request is not None:return music_request
    target=short_request(v,code)
    if target:return target[0],{'action':target[1]}
    exact,patterns=_index(code);target=exact.get(norm(v))
    if target:return target[0],{'action':target[1]}
    # Specific native forms (queue position, collections, font settings) must
    # win BEFORE the generic play <text> request. Never include command syntax
    # in the track title merely to obtain a valid tool schema.
    compound=compound_music(v,code)
    if compound:return compound
    if partial(v,code):return None
    matches=[]
    explicit_request=provider_request(v,code)
    for size,pattern,target,fields in patterns:
        if explicit_request and target[0] in {"music","spotify","yandex_music","discord"} and target[0]!=explicit_request[0]:continue
        m=pattern.fullmatch(full) or pattern.fullmatch(v)
        if not m:continue
        args={'action':target[1]};okay=True
        for field in fields:
            value=_value(m[field])
            if field not in _PAYLOAD_FIELDS183:
                value=value.rstrip(' .!?…')
            field='query' if field=='name' and target[0] in {'music','spotify','yandex_music','discord'} else field
            if field in _NUMERIC:
                try:
                    value=float(value.replace(',','.'))
                    if not value.is_integer():okay=False;break
                    value=int(value)
                except ValueError:okay=False;break
                if field=='value' and 'volume' in target[1]:field='volume'
            if target[0]=='clipboard' and target[1] in {'copy_history','paste_history'} and field=='index':
                if value<1:okay=False;break
                field='offset';value-=1
            if target==('overlay','style') and field=='value':field='font_size'
            if target[0]=='browser' and field in {'url','title'}:field='value'
            if target in {('overlay','checklist_check'),('overlay','checklist_uncheck')} and field=='index':field='item';value=str(value)
            args[field]=value
        if okay and target==('audio','change_target_volume') and re.fullmatch(r'\d{1,3}\s*(?:%|процент\w*|pour\s*cent|per\s*cento|por\s*cento|por\s*ciento|prozent|percent)?',str(args.get('target','')).strip(),re.I):
            okay=False  # NEO 188: «baisse le volume de 10» is a step of 10, not an application named «10»
        if okay:
            if target==('audio','change_target_volume'):args.setdefault('value',-15)
            matches.append((size,target[0],args))
        if matches and size<matches[0][0]:break
    if matches:
        top=[(t,a) for s,t,a in matches if s==matches[0][0]]
        if all(x==top[0] for x in top):return top[0]
    request=provider_request(v,code)
    if request and request[2] in request[1]:return request[:2]
    return None
