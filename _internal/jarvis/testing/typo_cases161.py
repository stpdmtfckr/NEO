"""Deterministic spelling stress cases with an independent canonical oracle.

Only fixed command words before the first slot are mutated. Expected tool/action
comes from canonical source data, never from the correction engine. Literal slot
contents use the existing typed atlas fixtures. Four reviewed collisions remain
negative tests because a correctly spelled *different* word is not a typo.
"""
from pathlib import Path
import json
import re
from jarvis.runtime.natural_commands import LANGUAGES

# A spelling mutation in an outgoing/destructive verb is a negative concern,
# not an expected automatic action. Pure spacing preserves the original verb.
_PROTECTED = frozenset('закрой закрыть удали удалить отправь отправить напиши выключи останови перезагрузи завершай перемести переименуй очисти скопируй заверши отправляй отформатируй отключи убей перезапусти close delete remove send write stop shutdown restart mute unmute erase format kill terminate move rename overwrite reset publish execute run schließe schliesse lösche loesche sende schreibe entferne verschiebe benenne beende ferme supprime envoie arrête écris efface renomme déplace écrivez cierra elimina envía borra escribe mueve renombra chiudi invia ferma cancella scrivi sposta rinomina feche fecha exclua envie pare apaga apague exclui escreve mova renomeie'.split())


def mutations(phrase):
    prefix=phrase.split('{',1)[0].rstrip()
    words=list(re.finditer(r'[^\W\d_]+',prefix))
    editable=[m for m in words if len(m[0])>=5 and not(m.start()==0 and m[0].casefold() in _PROTECTED)]
    if editable:
        found=editable[-1]; word=found[0]; at=max(1,len(word)//2-1)
        if word[at]==word[at+1]:
            at=next((i for i in range(1,len(word)-1) if word[i]!=word[i+1]),0)
        versions={'swap':word[:at]+word[at+1]+word[at]+word[at+2:],
                  'insert':word[:at]+word[at]+word[at:],
                  'delete':word[:at]+word[at+1:]}
        for kind,replacement in versions.items():
            if replacement!=word:
                yield kind,phrase[:found.start()]+replacement+phrase[found.end():]
    gap=re.search(r'(?<=\w)\s+(?=\w)',prefix)
    if gap:
        yield 'fusion',phrase[:gap.start()]+phrase[gap.end():]


def cases(internal,code):
    for path in sorted((Path(internal)/'config/canonical_phrases').glob('*.json')):
        for owner,phrases in json.loads(path.read_text(encoding='utf-8')).items():
            tool,action=owner.split('.',1)
            for kind,phrase in mutations(phrases[LANGUAGES.index(code)]):
                case={'locale':code,'phrase':phrase,'expected_tool':tool,'expected_action':action,
                      'source':'typo161_'+kind, 'scenario_id':'typo161_'+code+'_'+owner+'_'+kind}
                # The exact same letters can be a completed-action report;
                # whitespace recovery must never turn that report into music.
                narrative=code=='ru' and phrase.startswith('включив ')
                # "tack" is a valid English command-atlas word, not a misspelling
                # we can unconditionally replace by "track". Preserve no action.
                real_word=code=='en' and kind=='delete' and owner in {
                    'music.next','music.previous','music.repeat_on','music.repeat_track'}
                if narrative or real_word:
                    case['expected_kind']='no_action'
                yield case
