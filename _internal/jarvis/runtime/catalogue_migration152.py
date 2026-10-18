"""Reviewed semantic correction of explicitly compound search-and-play examples."""
from __future__ import annotations
import re
from .natural_commands import strip_request_shell

def is_find_and_play(phrase, code):
    verbs={'ru':r'(?:найди|поищи)\s+и\s+(?:включи|поставь|проиграй)',
           'en':r'(?:find|search)\s+and\s+(?:play|start)',
           'de':r'(?:finde|suche)\s+und\s+(?:spiele|starte)',
           'fr':r'(?:trouve|cherche)\s+et\s+(?:lance|joue|lis)',
           'es':r'(?:busca|encuentra)\s+y\s+(?:reproduce|pon|toca)',
           'it':r'(?:cerca|trova)\s+e\s+(?:riproduci|metti|avvia)',
           'pt':r'(?:procura|procure|encontra|encontre|busca)\s+e\s+(?:toca|toque|reproduz|reproduza)'}
    return bool(re.match(r'^(?:'+verbs[code]+r')\s+',strip_request_shell(phrase,code),re.I))

def migrate(actions, code):
    moved=[]
    for key in list(actions):
        tool,_,action=key.partition('.')
        if tool not in {'spotify','yandex_music','music'} or action not in {'search_track','find_track','find_song'}:continue
        for phrase in list(actions[key]):
            if not is_find_and_play(phrase,code):continue
            target=tool+'.play_track'
            actions[key].remove(phrase)
            if phrase not in actions.setdefault(target,[]):actions[target].append(phrase)
            moved.append({'phrase':phrase,'from':key,'to':target})
    return moved
