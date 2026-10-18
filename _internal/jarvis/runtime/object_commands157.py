"""Reviewed complete object forms missed by the earlier template compiler."""
import re
from jarvis.runtime.natural_commands import strip_request_shell, negated_action


def _literal(value):
    value=value.strip()
    return value[1:-1] if len(value)>1 and (value[0],value[-1]) in {('"','"'),('«','»'),('“','”')} else value


def complete_object_request157(text,code):
    if code not in {'en','de'} or negated_action(text,code):
        return None
    raw=strip_request_shell(text,code).strip(' .!?')
    pattern=(r'make (?:the )?window (?P<title>.+?) (?P<width>\d+) wide and (?P<height>\d+) high' if code=='en' else
             r'mache das Fenster (?P<title>.+?) (?P<width>\d+) breit und (?P<height>\d+) hoch')
    match=re.fullmatch(pattern,raw,re.I)
    if match and 100<=int(match['width'])<=32767 and 100<=int(match['height'])<=32767:
        # The backend preserves the actual current x/y when those values are
        # absent. Resizing does not authorize a guessed screen position.
        return 'window',{'action':'move_resize','title':_literal(match['title']),'width':int(match['width']),'height':int(match['height'])}
    if code=='de':
        match=re.fullmatch(r'(?:hilf mir,?\s+)?(?P<query>.+?) auf diesem Computer zu finden',raw,re.I)
        if match:
            return 'desktop',{'action':'resolve','query':_literal(match['query'])}
        match=re.fullmatch(r'mache den Text der Anzeige (?P<name>.+?) (?P<font_size>\d+) Punkt groß',raw,re.I)
        if match and 8<=int(match['font_size'])<=96:
            return 'overlay',{'action':'style','name':_literal(match['name']),'font_size':int(match['font_size'])}
        # "Save" also introduces clipboard and email-draft requests. A full
        # reviewed template in another domain owns its literal payload before
        # the generic memory form may consider that payload a memory key.
        from jarvis.runtime.canonical_commands import _matching_templates
        if any(row.intent != 'memory.remember' for row,_ in _matching_templates(raw,code)):
            return None
        match=re.fullmatch(r'speichere (?:den Eintrag\s+)?(?P<key>[^:\r\n]{1,200}):\s*(?P<value>.+)',raw,re.I|re.S)
        if not match:
            match=re.fullmatch(r'(?P<key>[^:\r\n]{1,200}):\s*(?P<value>.+?)\s+(?:merken|speichern)',raw,re.I|re.S)
        if match:
            return 'memory',{'action':'remember','key':_literal(match['key']),'value':_literal(match['value'])}
    return None
