"""Bounded command-head spelling repair. Never edit a target or payload.

Only locale-owned imperative words are considered; a correction must produce
an existing typed command/clarification. Valid words and other locales stay
unchanged. Sending, deleting and system shutdown verbs are not repair targets.
This is a text-layer fallback, not an acoustic recognizer or a general spellcheck.
"""
from __future__ import annotations
from functools import lru_cache
import re
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell

WORDS = {
 'ru': 'открой запусти покажи выведи найди поищи прочитай включи поставь продолжи возобнови переключи разверни сверни',
 'en': 'open launch start show find search read play pause resume switch',
 'de': 'öffne oeffne starte zeige finde suche lies spiele pausiere',
 'fr': 'ouvre lance montre affiche trouve cherche lis joue reprends',
 'es': 'abre inicia lanza muestra busca encuentra lee reproduce reanuda',
 'it': 'apri avvia mostra trova cerca leggi riproduci riprendi',
 'pt': 'abre abra inicia inicie mostra mostre encontra encontre procura procure leia reproduza retome',
}
# Do not reinterpret an explicit, valid opposite/risky imperative as a typo in
# a benign one. A turn containing any of these as its first word is opaque.
EXCLUDED = set('закрой закрыть удали удалить отправь отправить напиши выключи останови перезагрузи завершай close delete remove send write stop shutdown restart mute unmute schließe schliesse lösche loesche sende ferme supprime envoie arrête cierra elimina envía chiudi invia ferma feche fecha exclua envie pare'.split())
# Common inflected statements and nouns near imperatives are valid language,
# not errors: e.g. «включил музыку» describes a completed action.
STATEMENTS = {
 'ru': 'пока показ показывал показал показала покажу показывает открыто открыл открыла открою открывает открывал откроет запуск запустил запустила запущу запускает включил включила включу включает прочитал прочитала прочитаю читает поставил поставила поставлю ставит продолжил продолжила продолжу продолжает свернул свернула сверну развернул развернула разверну нашел нашёл нашла найду поиск поиски',
 'en': 'opened opens opening launched launches launching started starts starting showed shows showing found finds finding searched searches searching read reads reading played plays playing paused pauses pausing resumed resumes resuming switched switches switching',
 'de': 'öffnet öffnete öffnest startet startete startest zeigt zeigte zeigst findet fand findest sucht suchte suchst liest las spielt spielte spielst',
 'fr': 'ouvres ouvert ouverte ouvrait lancé lancée lançait montres montré montrait affiché affichait trouvé trouvait cherché cherchait lu joues joué jouait repris',
 'es': 'abro abres abrió abierta abierto iniciado inicié muestra mostró mostrado busco busqué buscó leído leyó reprodujo reproducido',
 'it': 'apro apre aperto aperta avvio avviato avviata mostrato mostrata trovato trovata cercato cercata letto letta riprodotto',
 'pt': 'abro abriu aberto aberta inicio iniciou iniciado iniciada mostrou mostrado mostrada encontrou encontrado encontrada procurou procurado leu lido reproduziu',
}
STATEMENTS['ru'] += ' включит включим включишь включите включат включу поставит поставим поставишь поставите поставят поставлю запустит запустим запустишь запустите запустят запущу прочитает прочитаем прочитаешь прочитаете прочитают прочитаю продолжит продолжим продолжишь продолжите продолжат продолжу переключил переключила переключит переключим переключишь переключите переключат переключу возобновил возобновила возобновит возобновим возобновишь возобновите возобновят возобновлю поищу поищет поищем поищешь поищете поищут свернет свернёт свернем свернём свернешь свернёшь свернете свернёте свернут развернет развернёт развернем развернём развернешь развернёшь развернете развернёте развернут выведет выведем выведешь выведете выведут выведу выведено'
# Productive Russian past/converb/participle endings. Do not infer a command
# from valid morphology merely because the verb stem resembles an imperative.
# The stem list is tied to this module's finite repair vocabulary; this is not
# an unrestricted language model or a blanket suffix ban on object names.
_RU_DESCRIPTIVE = re.compile(
    r'^(?:откры|запусти|показа|выве|поиска|прочита|включи|постави|продолжи|возобнови|переключи|разверну|сверну)'
    r'(?:л|ла|ло|ли|в|вши|вший|вшая|вшее|вшие|вшего|вшему|вшим|вших|вшую|вшею|вшей|вшими)$')
ALL_WORDS = {norm(w) for words in WORDS.values() for w in words.split()} | EXCLUDED


@lru_cache(maxsize=2048)
def distance(a, b):
    """Optimal string alignment distance: one adjacent swap counts as one typo."""
    rows = [list(range(len(b)+1))]
    for i, x in enumerate(a, 1):
        row = [i]
        for j, y in enumerate(b, 1):
            score = min(rows[-1][j]+1, row[-1]+1, rows[-1][j-1]+(x != y))
            if i>1 and j>1 and x==b[j-2] and a[i-2]==y:
                score = min(score,rows[-2][j-2]+1)
            row.append(score)
        rows.append(row)
    return rows[-1][-1]



@lru_cache(maxsize=7)
def declared_head_words(code):
    """Native grammar owns its valid words, even outside our repair targets.

    Build once per language from the shipped exact templates and action atlas.
    This protects imperative/infinitive variants such as Portuguese reproduz
    without rewriting payloads or treating an omitted synonym as a spelling error.
    """
    import json
    from pathlib import Path
    from jarvis.runtime.canonical_commands import templates
    phrases=[row.phrase for row in templates(code)]
    pack=Path(__file__).resolve().parents[1]/'locales'/'commands'/(code+'.json')
    catalogue=json.loads(pack.read_text(encoding='utf-8'))
    phrases.extend(phrase for group in catalogue.get('action_phrases',{}).values() for phrase in group)
    words=set()
    for phrase in phrases:
        head=re.match(r"^([^\W\d_]+)(?=\s|$)",strip_request_shell(phrase,code),re.UNICODE)
        if head:words.add(norm(head[1]))
    return frozenset(words)


def candidates(text, code):
    """Return a few nearest head-only alternatives, never arbitrary substrings."""
    if code not in LANGUAGES or len(text)>8000:
        return ()
    value = strip_request_shell(text,code)
    match = re.match(r'^([^\W\d_]+)(?=\s|$)', value, re.UNICODE)
    if not match:
        return ()
    word = norm(match[0]); tail=value[match.end():]
    if (word in ALL_WORDS or word in STATEMENTS[code].split() or len(word)<4 or len(word)>20
            or (code=='ru' and (_RU_DESCRIPTIVE.fullmatch(word) or re.fullmatch(r'постав(?:ка|ки|ку|кам|ками|ках)',word)))):
        return ()
    # Require a target/task noun. A misspelled single conversational word must
    # not become a command, and a truncated bare action remains ambiguous.
    if not tail.strip():
        return ()
    options=[]
    for target in WORDS[code].split():
        if target[:2] != word[:2]:
            continue
        limit=2 if min(len(word),len(target))>=7 else 1
        score=distance(word,target)
        prefix=len(word)>=4 and len(word)>=len(target)*.6 and target.startswith(word)
        if score<=limit or prefix:
            options.append((score if not prefix else min(score,1), target+tail))
    if not options or word in declared_head_words(code):
        return ()
    best=min(score for score,_ in options)
    return tuple(candidate for score,candidate in options if score==best)[:4]


def repair(text, code, proposal):
    """Accept only one semantic result; tied distinct actions are returned to ask."""
    options=candidates(text,code)
    if not options or proposal(text) is not None:
        return text, ()  # an already-owned complete/typed command outranks correction
    found=[]
    for candidate in options:
        identity=proposal(candidate)
        if identity is not None:
            found.append((candidate, identity))
    if not found:
        return text, ()
    # Equivalent spellings (Portuguese imperative variants) may share one route.
    identities={repr(identity) for _,identity in found}
    if len(identities)==1:
        return found[0][0], ()
    return text, tuple(candidate for candidate,_ in found)


def command_after_greeting(text,code,proposal):
    """A greeting is a wrapper only before a separately recognized imperative."""
    greetings={'ru':r'привет|здравствуй|здравствуйте','en':r'hello|hi|hey','de':r'hallo|guten tag','fr':r'bonjour|salut','es':r'hola','it':r'ciao|salve','pt':r'olá|ola|oi'}
    if code not in greetings:
        return text
    match=re.match(r'^(?:'+greetings[code]+r')\s*[,!:]\s*(.+)$',text,re.I|re.S)
    if not match:
        return text
    from jarvis.runtime.utterance_purpose import strip_assistant_address
    tail=strip_assistant_address(match[1])
    if proposal(tail) is not None:
        return tail
    repaired,alternatives=repair(tail,code,proposal)
    return repaired if repaired!=tail and not alternatives else text


def descriptive_statement(text,code):
    """Protect explicit Russian finite/past statements from older fuzzy lanes too.

    Match the original first word, not a polite request wrapper and not a
    pending literal title. Thus «можешь ...» remains a request construction.
    """
    if code!='ru':return False
    match=re.match(r'^([^\W\d_]+)\s+\S',str(text).strip(),re.UNICODE)
    if not match:return False
    word=norm(match[1])
    future=r'(?:включ(?:у|ит|им|ат)|запуст(?:ит|им|ят)|запущу|постав(?:ит|им|ят)|поставлю|прочита(?:ю|ет|ем|ют)|продолж(?:ит|им|ат)|продолжу|переключ(?:ит|им|ат)|переключу|поищ(?:у|ет|ем|ут)|найду|откро(?:ю|ет|ем|ют)|покаж(?:у|ет|ем|ут))'
    return bool(_RU_DESCRIPTIVE.fullmatch(word) or re.fullmatch(future,word))
