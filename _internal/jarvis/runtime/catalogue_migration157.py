"""Repair nine legacy French search labels, without turning search into play.

149's generated audit retained polite recherche/trouve heads under play_track.
A pure search for a missing song asks for its name, then calls search_track.
This is catalogue reclassification, not evidence of nine fixed runtime bugs.
"""
import re
from jarvis.runtime.natural_commands import norm,strip_request_shell

def migrate_search_heads157(actions,code):
    moved=[]
    if code!='fr':return moved
    for tool in ('music','spotify','yandex_music'):
        key=tool+'.play_track';target=tool+'.search_track'
        for phrase in list(actions.get(key,())):
            value=norm(strip_request_shell(phrase,code))
            if not re.fullmatch(r'(?:recherche|trouve) (?:une|un|la|le) (?:piste|chanson|titre|morceau) (?:sur le service musical actif|dans (?:spotify|yandex music))',value):continue
            actions[key].remove(phrase)
            if phrase not in actions.setdefault(target,[]):actions[target].append(phrase)
            moved.append({'phrase':phrase,'from':key,'to':target,'reason':'pure song search without playback request','version':157})
    return moved
