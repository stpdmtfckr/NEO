"""Repair the *literal grammar* of a command, never its user-owned slots.

The vocabulary is compiled from every shipped canonical/action/short template.
A bounded word alignment handles adjacent swaps and missing/extra spaces. It
cannot drop/add words, cross a slot boundary, or change an already valid word.
No execution or entity enumeration occurs here. The existing router validates
candidates and asks if different interpretations remain equally plausible.
"""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import threading
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell
from jarvis.runtime.command_repair160 import STATEMENTS, EXCLUDED, distance, descriptive_statement

_TOKEN = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*|[^\s]+", re.UNICODE)
_SLOT = re.compile(r'\{[^}]+\}')
# No spelling guess may create one of these outgoing/destructive actions.
# Exact risky verbs can still have their object shell repaired; the ordinary
# typed schema, object selection and confirmation policy remain in control.
# NEO 175: replying is outgoing too («отметь письмо…» with a typo was guessed as «ответь…»).
_RISK_HEADS = EXCLUDED | set('ответь ответить напиши написать отправь reply answer respond write antworte réponds reponds responde rispondi responda '
                              'перешли forward weiterleiten transfère reenvía inoltra encaminha '
                              'перемести переименуй очисти скопируй заверши отправляй отформатируй отключи убей перезапусти delete erase format kill terminate move rename overwrite reset send publish execute run schreibe entferne verschiebe benenne beende écris efface renomme déplace envoie écrivez elimina borra escribe mueve renombra invia cancella scrivi sposta rinomina apaga apague exclui escreve envie mova renomeie'.split())
# These nouns belong to grammar, while an arbitrary app/file/song name does not.
# They are sourced from complete templates below; valid native words are never
# replaced merely because another fixed word is nearby in edit distance.

@dataclass(frozen=True)
class Shell:
    literal: str
    words: tuple[str, ...]
    slotted: bool
    owners: tuple[str, ...]

@dataclass(frozen=True)
class Candidate:
    text: str
    cost: int
    literal_words: int
    owners: tuple[str, ...]


def _words(value):
    return tuple(norm(m[0]) for m in _TOKEN.finditer(value) if norm(m[0]))


_READY: set[str] = set()
_BUILD_LOCK = threading.RLock()


def ready(code) -> bool:
    """HIST-023: True once the catalogue of this language is built (never builds it)."""
    return code in _READY or code not in LANGUAGES


def catalog(code):
    """One immutable catalogue per selected language, built once (single flight).

    HIST-023: concurrent first callers wait for the one build instead of
    building it twice; ready() makes the readiness explicit for the GUI."""
    if code in _READY:
        return _catalog_build(code)
    with _BUILD_LOCK:
        value = _catalog_build(code)
        _READY.add(code)
    return value


def _clear_catalog() -> None:
    _catalog_build.cache_clear()
    _READY.clear()


catalog.cache_clear = _clear_catalog          # type: ignore[attr-defined]


@lru_cache(maxsize=7)
def _catalog_build(code):
    """One immutable catalogue per selected language; no settings/profile I/O."""
    if code not in LANGUAGES:
        return (), {}, frozenset(), frozenset()
    from jarvis.runtime.canonical_commands import templates
    from jarvis.runtime.command_contracts150 import _short_index
    rows = [(row.intent, row.phrase) for row in templates(code)]
    root = Path(__file__).resolve().parents[1]
    pack = json.loads((root/'locales'/'commands'/(code+'.json')).read_text(encoding='utf-8'))
    rows += [(owner, phrase) for owner, phrases in pack.get('action_phrases', {}).items() for phrase in phrases]
    rows += [('.'.join(owner), phrase) for phrase, owner in _short_index(code).items()]
    from jarvis.runtime.command_contracts149 import _index as contract_index
    for incomplete in (False, True):
        rows += [(owner[0]+'.'+str(owner[1].get('action','')), phrase)
                 for phrase, owner in contract_index(code,incomplete).items()]
    # Native short grammar is declared as verb/object alternatives rather than
    # JSON templates. Expand that same grammar, including optional articles;
    # don't maintain a disconnected typo-only list of function names.
    from jarvis.runtime.natural_commands import VERBS, OBJECTS, _ARTICLES
    from jarvis.runtime.short_requests160 import LAUNCH
    articles=[''] + [piece for piece in _ARTICLES[code].removeprefix('(?:').removesuffix(')').split('|')]
    verbs=set(word for group in VERBS[code] for word in group.split('|')) | set(LAUNCH[LANGUAGES.index(code)].split('|'))
    for verb in verbs:
        for group in OBJECTS[code]:
            for obj in group.split('|'):
                for article in articles:
                    rows.append(('native.object', ' '.join(x for x in (verb,article,obj,'{value}') if x)))
    grouped = {}
    for owner, phrase in rows:
        phrase = strip_request_shell(phrase, code).strip(' .!?¿¡')
        slot = _SLOT.search(phrase)
        literal = phrase[:slot.start()].rstrip() if slot else phrase
        # A literal word attached to a slot isn't a separable command boundary.
        if slot and (not phrase[:slot.start()].endswith(' ') or not literal):
            continue
        words = _words(literal)
        if not words or len(words)>24:
            continue
        grouped.setdefault((literal, words, slot is not None), set()).add(owner)
    shells = tuple(Shell(literal, words, slotted, tuple(sorted(owners))) for (literal,words,slotted),owners in grouped.items())
    heads, vocabulary = {}, set()
    for index, shell in enumerate(shells):
        heads.setdefault(shell.words[0], []).append(index)
        vocabulary.update(shell.words)
    return shells, {key:tuple(value) for key,value in heads.items()}, frozenset(vocabulary), frozenset(heads)


def _safe_surface(value, code):
    if not value or len(value)>8000 or value[0] in '\"«“\'':
        return False
    if descriptive_statement(value,code):
        return False
    first = _words(value)[:1]
    return bool(first)


def _word_cost(source, target, vocabulary, *, head=False):
    if source == target:
        return 0
    if (len(source)<4 or len(target)<4 or source in vocabulary or
            (head and target in _RISK_HEADS)):
        return None
    limit = 2 if (min(len(source),len(target))>=8 or (min(len(source),len(target))>=5 and sorted(source)==sorted(target))) else 1
    if abs(len(source)-len(target))>limit:
        return None
    score=distance(source,target)
    return score*10 if score<=limit else None


def _alignment(tokens, target, vocabulary):
    """Return (consumed input words, cost); at most two repaired word groups.

    1:1 typo or 1:N/N:1 whitespace change. Group transitions can only join
    dictionary pieces, not invent/drop particles, negation or a target name.
    """
    @lru_cache(maxsize=192)
    def visit(i,j,repairs):
        if j==len(target):
            return ((i,0),)
        if i>=len(tokens) or repairs>2:
            return ()
        moves=[]
        cost=_word_cost(tokens[i],target[j],vocabulary,head=j==0)
        if cost is not None:
            moves.append((1,1,cost))
        for count in (2,3,4):
            if j+count<=len(target):
                joined=''.join(target[j:j+count]); source=tokens[i]
                if len(source)>=3 and abs(len(source)-len(joined))<=1:
                    delta=distance(source,joined)
                    # Fused native words are not edited as a different action.
                    if delta<=1 and (delta==0 or len(joined)>=9) and source not in vocabulary and not (delta and j==0 and target[0] in _RISK_HEADS):
                        moves.append((1,count,delta*10+2*(count-1)))
            if i+count<=len(tokens):
                source=''.join(tokens[i:i+count]); joined=target[j]
                if len(joined)>=6 and abs(len(source)-len(joined))<=1:
                    delta=distance(source,joined)
                    if delta<=1 and (delta==0 or len(joined)>=9) and not (delta and j==0 and joined in _RISK_HEADS):
                        moves.append((count,1,delta*10+2*(count-1)))
        out=[]
        for di,dj,cost in moves:
            if repairs+(cost>0)>2:
                continue
            for consumed, later in visit(i+di,j+dj,repairs+(cost>0)):
                if cost+later<=24:
                    out.append((consumed,cost+later))
        return tuple(out)
    return visit(0,0,0)


@lru_cache(maxsize=2048)
def _candidate_heads(word, second, code):
    _, _, vocabulary, heads = catalog(code)
    result=[]
    for target in heads:
        cost=_word_cost(word,target,vocabulary,head=True)
        if cost is not None:
            result.append(target); continue
        # A first token can contain the verb plus up to three fixed words.
        if len(word)>=len(target)+1 and (target not in _RISK_HEADS or word.startswith(target)) and word[:min(len(target),max(2,len(target)-1))]==target[:min(len(target),max(2,len(target)-1))]:
            result.append(target); continue
        # Or a single verb may have acquired an internal space.
        joined=word+second
        if second and len(target)>=6 and abs(len(joined)-len(target))<=1 and (target not in _RISK_HEADS or joined==target) and distance(joined,target)<=1:
            result.append(target)
    return tuple(result)


@lru_cache(maxsize=2048)
def candidates(text, code):
    value=strip_request_shell(str(text),code).strip()
    if code not in LANGUAGES or not _safe_surface(value,code):
        return ()
    positions=[m for m in _TOKEN.finditer(value) if norm(m[0])]
    tokens=tuple(norm(m[0]) for m in positions)
    if not tokens or len(tokens)>80:
        return ()
    shells, heads, vocabulary, _ = catalog(code)
    if tokens[0] in STATEMENTS.get(code,'').split() and tokens[0] not in heads:
        return ()
    # A lost space after a report does not turn a report into an imperative:
    # «включивмузыку», «запустилиигру», «openedfolder». The remaining letters
    # must themselves be a known fixed word, so «включимая волна» is not
    # misclassified as the future-tense «включим» plus arbitrary letters.
    for cut in range(4,min(22,len(tokens[0])-2)):
        prefix,rest=tokens[0][:cut],tokens[0][cut:]
        if rest in vocabulary and (descriptive_statement(prefix+' object',code) or
                (prefix in STATEMENTS.get(code,'').split() and prefix not in heads)):
            return ()
    selected=[]
    for head in _candidate_heads(tokens[0],tokens[1] if len(tokens)>1 else '',code):
        selected.extend(heads[head])
    # A valid fixed shell is never spelling-corrected into another command.
    # One-word open/search/play shells are deliberately too generic to prove
    # that a following misspelled *object type* is a user-owned title.
    exact_prefix=0
    for index in heads.get(tokens[0],()):
        row=shells[index]; n=len(row.words)
        if row.words==tokens[:n]:
            if not row.slotted and len(tokens)==n:
                return ()
            if row.slotted and len(tokens)>=n:
                exact_prefix=max(exact_prefix,n)
    found={}
    for index in selected:
        row=shells[index]
        if len(row.words)==1 and not row.slotted and ((len(tokens[0])<5 or len(row.words[0])<6) or row.words[0] in _RISK_HEADS):
            continue  # noisy short social words must not become an action
        if len(row.words)<=exact_prefix:
            continue  # nothing beyond the already recognized literal grammar
        # Cheap whole-phrase length gate before bounded alignment.
        if not row.slotted and abs(sum(map(len,tokens))-sum(map(len,row.words)))>2:
            continue
        for consumed,cost in _alignment(tokens[:min(len(tokens),len(row.words)+3)],row.words,vocabulary):
            if not cost or (not row.slotted and consumed!=len(tokens)):
                continue
            tail=value[positions[consumed-1].end():] if consumed else value
            # No argument byte can disappear: literal prefix + original tail.
            punctuation=re.search(r'[:;,]+$',row.literal)
            if punctuation and tail.lstrip().startswith(punctuation[0]):
                tail=tail.lstrip()[len(punctuation[0]):]
            candidate=row.literal+tail
            if candidate==value:
                continue
            item=Candidate(candidate,cost,len(row.words),row.owners)
            previous=found.get(candidate)
            if previous is None or (item.cost,-item.literal_words)<(previous.cost,-previous.literal_words):
                found[candidate]=item
    if not found:
        return ()
    ranked=sorted(found.values(),key=lambda row:(row.cost,-row.literal_words,row.text))
    # Keep all ties for semantic validation, bounded against catalogue accidents.
    best=ranked[0].cost
    return tuple(row for row in ranked if row.cost==best)[:12]


def repair(text, code, proposal):
    options=candidates(text,code)
    if not options:
        return text, ()
    original=proposal(text)
    # An existing typed missing-slot contract is authoritative, even when its
    # verb lives in the generated clarification grammar rather than the phrase
    # catalogue. In particular, “reschedule timer” must keep its timer-id and
    # duration slots; it is not a misspelling of “schedule ...”. Generic complete
    # app/name guesses (two-item identities) can still yield to a more specific
    # fixed-object repair such as “открой пакпу”.
    if isinstance(original,tuple) and len(original)==3:
        # The legacy file-mutation fallback accepts an arbitrary unresolved
        # tail after copy/move/rename and asks for an absolute source. That is
        # not precise ownership of “copy to clibpoard ...” or “move the windwo”.
        # Keep the normal absolute-path/confirmation checks after the specific
        # corrected grammar chooses its domain.
        generic_source = original[0]=='filesystem' and any(kind=='absolute_source' for _,kind in original[2])
        if not generic_source:
            return text, ()
    found=[]
    for row in options:
        identity=proposal(row.text)
        if identity is not None:
            found.append((row,identity))
    if not found:
        return text, ()
    # Equivalent surface forms must agree on the actual typed route, not merely
    # on their atlas labels (some labels intentionally share an implementation).
    identities={repr(identity) for _,identity in found}
    if len(identities)==1:
        return found[0][0].text, ()
    return text, tuple(dict.fromkeys(row.text for row,_ in found))[:6]


def coverage(code):
    rows,heads,vocabulary,_=catalog(code)
    return {'language':code,'literal_shells':len(rows),'heads':len(heads),
            'fixed_words':len(vocabulary),'owners':len({x for row in rows for x in row.owners}),
            'slotted_shells':sum(row.slotted for row in rows)}
