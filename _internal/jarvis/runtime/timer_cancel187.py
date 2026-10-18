"""NEO 187: cancelling timers and pending delayed actions in natural phrases.

TZ-TIMER-CANCEL-MULTILINGUAL-01 / REC-CANCEL-SCOPE-01. A finite, per-language
grammar decides one of five meanings before any general route sees the text:

* ``quoted``      — «запиши фразу «отмени таймер»», «что будет, если я скажу …»:
                    words about a command, never cancellation authority;
* ``keep``        — «не отменяй таймер»: nothing changes;
* ``cancel_all``  — «отмени все мои таймеры»: an explicit, separate operation;
* ``cancel_one``  — «отмени таймер [имя]»: one exactly chosen timer, or a question;
* ``negated``     — «не выключай компьютер»: cancels only a matching *pending*
                    delayed action; otherwise it is a plain «won't do».

Only timers (kind ``timer``) are touched by timer phrases — reminders,
schedules and delayed actions keep their own commands. Nothing here creates
an opposite action or executes anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.runtime.natural_commands import LANGUAGES, locale_message, norm

_QUOTES = '«»“”„"'

_NOUN = {
    'ru': r'таймер(?:ы|а|ов)?', 'en': r'timers?', 'de': r'timer', 'fr': r'minuteurs?|minuterie',
    'es': r'temporizador(?:es)?', 'it': r'timer', 'pt': r'temporizador(?:es)?',
}
_NOUN_WORDS = {'ru': ('таймер',), 'en': ('timer',), 'de': ('timer',), 'fr': ('minuteur',),
               'es': ('temporizador',), 'it': ('timer',), 'pt': ('temporizador',)}
_VERB = {
    'ru': r'отмени(?:те)?|удали(?:те)?|сбрось(?:те)?|убери(?:те)?',
    'en': r'cancel|delete|remove|clear',
    'de': r'lösche|losche|entferne|beende',
    'fr': r'annule(?:z)?|supprime(?:z)?',
    'es': r'cancela|elimina|borra|quita',
    'it': r'annulla|cancella|elimina|rimuovi',
    'pt': r'cancela|cancele|apaga|apague|remove|remova|elimina',
}
_ART = {
    'ru': r'(?:мой |мои |этот |эти )?', 'en': r'(?:the |my |this )?', 'de': r'(?:den |meinen |diesen )?',
    'fr': r'(?:le |mon |ce |la )?', 'es': r'(?:el |mi |este )?', 'it': r'(?:il |mio |il mio |questo )?',
    'pt': r'(?:o |meu |o meu |este )?',
}
_ALL = {
    'ru': r'(?:все|всё)(?: мои)?', 'en': r'all(?: of)?(?: my| the)?', 'de': r'alle(?: meine)?',
    'fr': r'tous (?:mes|les)', 'es': r'todos (?:mis|los)', 'it': r'tutti (?:i miei|i)', 'pt': r'todos (?:os meus|os)',
}
_KEEP = {
    'ru': r'не (?:отменяй|отмени|удаляй|удали|сбрасывай|сбрось|трогай)(?:те)? .*таймер.*',
    'en': r"(?:don't|do not|dont) (?:cancel|delete|remove|clear|touch) .*timer.*",
    'de': r'(?:brich|breche) .*timer.* nicht ab|(?:lösche|losche|entferne) .*timer.* nicht',
    'fr': r"(?:n'annule|ne supprime|ne touche) pas .*minuteur.*",
    'es': r'no (?:canceles|cancele|borres|elimines|quites|toques) .*temporizador.*',
    'it': r'non (?:annullare|cancellare|eliminare|rimuovere|toccare) .*timer.*',
    'pt': r'(?:não|nao) (?:canceles|cancele|apagues|apague|removas|remova|toques) .*temporizador.*',
}
_ABOUT = {   # a head that quotes a command instead of giving it
    'ru': r'(?:запиши|напиши|запомни|сохрани|скопируй|набери|напечатай)(?:те)? (?:фразу|текст|слова|строку)|что (?:будет|произойдет|случится),? если (?:я )?(?:скажу|напишу)',
    'en': r'(?:write down|write|note down|note|type|save|copy) (?:the )?(?:phrase|text|words|line)|what (?:happens|will happen|would happen),? if i (?:say|type|write)',
    'de': r'(?:notiere|schreibe|schreib|speichere|tippe) (?:den satz|den text|die worte|die zeile)|was passiert,? wenn ich',
    'fr': r"(?:note|écris|ecris|enregistre|tape) (?:la phrase|le texte|les mots)|que se passe-t-il,? si je dis|qu'est-ce qui se passe,? si je dis",
    'es': r'(?:anota|escribe|guarda|teclea) (?:la frase|el texto|las palabras)|¿?qu[eé] pasa,? si digo',
    'it': r'(?:scrivi|annota|salva|digita) (?:la frase|il testo|le parole)|che cosa succede,? se dico|cosa succede,? se dico',
    'pt': r'(?:escreve|escreva|anota|anote|guarda|digita) (?:a frase|o texto|as palavras)|o que acontece,? se eu disser',
}

_COMPUTER = {'ru': r'(?:компьютер|комп|пк|ноутбук)', 'en': r'(?:the |my )?(?:computer|pc|laptop)',
             'de': r'(?:den )?(?:computer|pc|rechner)', 'fr': r"(?:l'|le )?(?:ordinateur|pc)",
             'es': r'(?:el |la |mi )?(?:ordenador|computadora|computador|pc)', 'it': r'(?:il )?(?:computer|pc)',
             'pt': r'(?:o )?(?:computador|pc)'}


def _negations(code):
    c = _COMPUTER[code]
    return {
        # NEO 188 atlas: the infinitive («не выключать компьютер») is the same refusal.
        'ru': (('shutdown', rf'не (?:выключай|выключать|отключай|отключать|вырубай|вырубать|гаси|гасить) {c}'),
               ('sleep', rf'не (?:усыпляй|усыплять|отправляй в сон|отправлять в сон|переводи в сон|переводить в сон) {c}'
                         rf'|не (?:переводи|переводить) {c} в (?:сон|спящий режим)'),
               ('hibernate', rf'не (?:переводи|переводить) {c} в гибернацию|не (?:гибернируй) {c}'),
               ('restart', rf'не (?:перезагружай|перезагружать|перезапускай|перезапускать) {c}'),
               ('close', r'не (?:закрывай|закрывать) (?P<t>.+)'),
               ('launch', r'не (?:запускай|запускать|открывай|открывать|включай|включать) (?P<t>.+)'),
               ('stop_media', r'не (?:останавливай|останавливать|выключай|выключать|ставь на паузу|ставить на паузу|паузи) '
                              r'(?P<t>музыку|песню|трек|видео|ролик)')),
        'en': (('shutdown', rf"(?:don't|do not|dont) (?:shut down|shutdown|turn off|power off) {c}"),
               ('sleep', rf"(?:don't|do not|dont) put {c} to sleep|(?:don't|do not|dont) sleep {c}"),
               ('hibernate', rf"(?:don't|do not|dont) hibernate {c}"),
               ('restart', rf"(?:don't|do not|dont) (?:restart|reboot) {c}"),
               ('close', r"(?:don't|do not|dont) close (?P<t>.+)"), ('launch', r"(?:don't|do not|dont) (?:launch|open|start|run) (?P<t>.+)"),
               ('stop_media', r"(?:don't|do not|dont) (?:stop|pause) (?:the )?(?P<t>music|song|track|video)")),
        'de': (('shutdown', rf'(?:fahre|fahr) {c} nicht herunter|(?:schalte|schalt) {c} nicht aus'),
               ('sleep', rf'versetze {c} nicht in den energiesparmodus|schicke {c} nicht in den energiesparmodus'),
               ('hibernate', rf'versetze {c} nicht in den ruhezustand'),
               ('restart', rf'starte {c} nicht neu'),
               ('close', r'(?:schließe|schliesse|schließ) (?P<t>.+?) nicht'), ('launch', r'(?:starte|öffne|offne) (?P<t>.+?) nicht'),
               ('stop_media', r'(?:stoppe|pausiere) (?:die |das )?(?P<t>musik|lied|video) nicht')),
        'fr': (('shutdown', rf"n'(?:éteins|eteins) pas {c}|n'arrête pas {c}"),
               ('hibernate', rf'ne mets pas {c} en veille prolongée|ne mets pas {c} en hibernation'),
               ('sleep', rf'ne mets pas {c} en veille'),
               ('restart', rf'ne redémarre pas {c}|ne redemarre pas {c}'),
               ('close', r'ne ferme pas (?P<t>.+)'), ('launch', r"ne lance pas (?P<t>.+)|n'ouvre pas (?P<t2>.+)"),
               ('stop_media', r"n'arrête pas (?:la |le )?(?P<t>musique|chanson|vidéo|video)|ne mets pas (?:la |le )?(?P<t2>musique|vidéo|video) en pause")),
        'es': (('shutdown', rf'no apagues {c}|no apague {c}'),
               ('sleep', rf'no suspendas {c}|no pongas {c} en suspensión|no pongas {c} a dormir'),
               ('hibernate', rf'no pongas {c} en hibernación|no hibernes {c}'),
               ('restart', rf'no reinicies {c}|no reinicie {c}'),
               ('close', r'no cierres (?P<t>.+)'), ('launch', r'no (?:abras|inicies|ejecutes|lances) (?P<t>.+)'),
               ('stop_media', r'no (?:detengas|pares|pauses) (?:la |el )?(?P<t>música|musica|canción|vídeo|video)')),
        'it': (('shutdown', rf'non spegnere {c}'),
               ('sleep', rf'non mettere {c} in sospensione|non sospendere {c}'),
               ('hibernate', rf'non ibernare {c}|non mettere {c} in ibernazione'),
               ('restart', rf'non riavviare {c}'),
               ('close', r'non chiudere (?P<t>.+)'), ('launch', r'non (?:avviare|aprire|lanciare) (?P<t>.+)'),
               ('stop_media', r'non (?:fermare|mettere in pausa) (?:la |il )?(?P<t>musica|canzone|video)')),
        'pt': (('shutdown', rf'(?:não|nao) (?:desligues|desligue) {c}'),
               ('sleep', rf'(?:não|nao) (?:coloques|coloque|ponhas) {c} em suspensão'),
               ('hibernate', rf'(?:não|nao) (?:coloques|coloque|ponhas) {c} em hibernação|(?:não|nao) (?:hibernes|hiberne) {c}'),
               ('restart', rf'(?:não|nao) (?:reinicies|reinicie) {c}'),
               ('close', r'(?:não|nao) (?:feches|feche) (?P<t>.+)'), ('launch', r'(?:não|nao) (?:abras|abra|inicies|inicie) (?P<t>.+)'),
               ('stop_media', r'(?:não|nao) (?:pares|pare|pauses) (?:a |o )?(?P<t>música|musica|canção|vídeo|video)')),
    }[code]


_POLITE = r"пожалуйста|please|bitte|s'il te plaît|s'il vous plaît|s'il te plait|s'il vous plait|por favor|per favore"

_TEXT = {
    'none': ('Активных таймеров нет.', 'There are no active timers.', 'Es laufen keine Timer.', 'Aucun minuteur actif.',
             'No hay temporizadores activos.', 'Non ci sono timer attivi.', 'Não há temporizadores ativos.'),
    'which': ('Какой таймер отменить? Сейчас идут: {list}.', 'Which timer should I cancel? Running now: {list}.',
              'Welchen Timer soll ich abbrechen? Aktiv: {list}.', 'Quel minuteur annuler ? En cours : {list}.',
              '¿Qué temporizador cancelo? Activos: {list}.', 'Quale timer annullo? Attivi: {list}.',
              'Que temporizador devo cancelar? Ativos: {list}.'),
    'not_found': ('Таймер «{name}» не нашёл; остальные не трогал.', 'I found no timer “{name}”; the others are untouched.',
                  'Timer „{name}“ nicht gefunden; die anderen bleiben unverändert.', 'Minuteur « {name} » introuvable ; les autres sont intacts.',
                  'No encontré el temporizador «{name}»; los demás siguen igual.', 'Timer «{name}» non trovato; gli altri restano invariati.',
                  'Não encontrei o temporizador «{name}»; os outros ficam iguais.'),
    'all': ('Отменил все таймеры: {n}. Напоминания и расписания не трогал.', 'Cancelled all timers: {n}. Reminders and schedules are untouched.',
            'Alle Timer abgebrochen: {n}. Erinnerungen und Zeitpläne bleiben.', 'Tous les minuteurs sont annulés : {n}. Rappels et plannings intacts.',
            'He cancelado todos los temporizadores: {n}. Recordatorios y horarios siguen igual.',
            'Ho annullato tutti i timer: {n}. Promemoria e programmazioni restano.',
            'Cancelei todos os temporizadores: {n}. Lembretes e agendamentos ficam iguais.'),
    'all_failed': ('Не удалось сохранить отмену таймеров. Проверь список таймеров.', 'Could not save the cancellation. Check the timer list.',
                   'Der Abbruch konnte nicht gespeichert werden. Prüfe die Timerliste.', 'Impossible d’enregistrer l’annulation. Vérifie la liste des minuteurs.',
                   'No se pudo guardar la cancelación. Revisa la lista de temporizadores.', 'Impossibile salvare l’annullamento. Controlla l’elenco dei timer.',
                   'Não foi possível guardar o cancelamento. Verifica a lista de temporizadores.'),
    'keep': ('Хорошо, таймер не трогаю.', 'OK, I’m leaving the timer as it is.', 'Gut, der Timer bleibt, wie er ist.',
             'D’accord, je ne touche pas au minuteur.', 'De acuerdo, no toco el temporizador.', 'Va bene, non tocco il timer.',
             'Está bem, não mexo no temporizador.'),
    'quoted': ('Это команда в кавычках — я её не выполняю, таймеры не изменились.',
               'That is a quoted command, so I am not running it; your timers are unchanged.',
               'Das ist ein zitierter Befehl, ich führe ihn nicht aus; deine Timer bleiben unverändert.',
               'C’est une commande citée : je ne l’exécute pas, tes minuteurs ne changent pas.',
               'Es una orden citada: no la ejecuto y tus temporizadores siguen igual.',
               'È un comando citato: non lo eseguo e i tuoi timer restano invariati.',
               'É um comando citado: não o executo e os teus temporizadores ficam iguais.'),
    'no_action': ('Запланированного действия «{label}» нет; ничего не изменено.', 'There is no scheduled “{label}”; nothing was changed.',
                  'Keine geplante Aktion „{label}“; nichts geändert.', 'Aucune action « {label} » programmée ; rien n’a changé.',
                  'No hay ninguna acción «{label}» programada; no se cambió nada.', 'Nessuna azione «{label}» programmata; non è cambiato nulla.',
                  'Não há nenhuma ação «{label}» agendada; nada foi alterado.'),
    'no_launch': ('Запланированного запуска «{target}» нет: NEO не откладывает запуск приложений. Ничего не изменено.',
                  'There is no scheduled launch of “{target}”: NEO does not schedule app launches. Nothing was changed.',
                  'Kein geplanter Start von „{target}“: NEO plant keine App-Starts. Nichts geändert.',
                  'Aucun lancement programmé de « {target} » : NEO ne programme pas le lancement d’applications. Rien n’a changé.',
                  'No hay un inicio programado de «{target}»: NEO no programa el inicio de aplicaciones. No se cambió nada.',
                  'Nessun avvio programmato di «{target}»: NEO non programma l’avvio delle app. Non è cambiato nulla.',
                  'Não há nenhuma abertura agendada de «{target}»: o NEO não agenda a abertura de aplicações. Nada foi alterado.'),
    'action_which': ('Запланировано несколько подходящих действий. Скажи номер: {list}.',
                     'Several matching actions are scheduled. Say its ID: {list}.',
                     'Mehrere passende Aktionen sind geplant. Nenne die ID: {list}.',
                     'Plusieurs actions correspondantes sont programmées. Indique l’identifiant : {list}.',
                     'Hay varias acciones programadas que coinciden. Di el ID: {list}.',
                     'Ci sono più azioni programmate corrispondenti. Indica l’ID: {list}.',
                     'Há várias ações agendadas correspondentes. Indica o ID: {list}.'),
}


# NEO 188 atlas (NEO-CANCEL-001): «cancel the shutdown timer» names a scheduled system
# action, not a timer called «shutdown». Matched after the timer list, before «not found».
_SYSTEM188 = {
    'shutdown': {'ru': r'выключени[яе]|отключени[яе]|завершения работы', 'en': r'(?:the )?(?:shutdown|shut down|power off)',
                 'de': r'abschalt|herunterfahr(?:en)?|zum herunterfahren', 'fr': r"(?:d'|de l')?(?:arrêt|arret|extinction)",
                 'es': r'(?:de )?apagado', 'it': r'(?:di )?spegnimento', 'pt': r'(?:de )?(?:desligamento|encerramento)'},
    'sleep': {'ru': r'сна|перехода в сон|спящего режима', 'en': r'(?:the )?sleep', 'de': r'(?:für den )?energiespar(?:modus)?',
              'fr': r'(?:de )?mise en veille', 'es': r'(?:de )?suspensión', 'it': r'(?:di )?sospensione', 'pt': r'(?:de )?suspensão'},
    'hibernate': {'ru': r'гибернации', 'en': r'(?:the )?hibernat(?:ion|e)', 'de': r'(?:für den )?ruhezustand(?:s)?',
                  'fr': r'(?:de )?(?:mise en veille prolongée|hibernation)', 'es': r'(?:de )?hibernación', 'it': r'(?:di )?ibernazione',
                  'pt': r'(?:de )?hibernação'},
}
_SYSTEM_LABEL188 = {
    'sleep': ('Переход в сон', 'Sleep', 'Energiesparmodus', 'Mise en veille', 'Suspensión', 'Sospensione', 'Suspensão'),
    'hibernate': ('Гибернация', 'Hibernation', 'Ruhezustand', 'Veille prolongée', 'Hibernación', 'Ibernazione', 'Hibernação'),
}
_LAUNCH188 = {
    'ru': r'(?:отмени|убери|удали) (?:отложенный|запланированный) (?:запуск|старт) (?P<t>.+)',
    'en': r'(?:cancel|remove) the (?:scheduled|delayed|planned) (?:launch|start) of (?P<t>.+)',
    'de': r'(?:brich|breche) den (?:geplanten|verzögerten) start von (?P<t>.+) ab',
    'fr': r"annule le lancement (?:programmé|prévu|différé) de (?P<t>.+)",
    'es': r'cancela el inicio (?:programado|diferido) de (?P<t>.+)',
    'it': r"annulla l'avvio (?:programmato|ritardato) di (?P<t>.+)",
    'pt': r'(?:cancele|cancela) (?:a abertura|o arranque|o início) (?:agendad[ao]|programad[ao]) de (?P<t>.+)',
}


def _system_action188(name: str, code: str) -> str:
    value = norm(name)
    for action, table in _SYSTEM188.items():
        if re.fullmatch(r'(?:' + table[code] + r')', value):
            return action
    return ''


def _system_label188(action: str, code: str) -> str:
    if action == 'shutdown':
        from jarvis.runtime.delayed_actions184 import action_name
        return action_name('shutdown', code)
    return _SYSTEM_LABEL188[action][LANGUAGES.index(code if code in LANGUAGES else 'en')]


def _say(key, code, **values):
    return _TEXT[key][LANGUAGES.index(code if code in LANGUAGES else 'en')].format(**values)


@dataclass(frozen=True)
class CancelRequest:
    kind: str            # quoted | keep | cancel_all | cancel_one | negated
    name: str = ''
    action: str = ''
    target: str = ''


def _distance1(a, b):
    """True when ``a`` and ``b`` differ by one edit or one adjacent swap."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _repair_noun(value, code):
    words = value.split(' ')
    for index, word in enumerate(words):
        for noun in _NOUN_WORDS[code]:
            if len(word) >= 5 and word != noun and _distance1(word, noun):
                words[index] = noun
    return ' '.join(words)


def _without_polite(value):
    return re.sub(r'[,\s]*(?<![\w\'])(?:' + _POLITE + r')(?![\w\'])[,\s]*', ' ', value).strip()


def _surface(text, code):
    value = _without_polite(norm(text))
    value = re.sub(r'\s*,\s*', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def parse(text, code):
    if code not in LANGUAGES:
        return None
    raw = str(text or '').strip()
    if not raw or len(raw) > 240:
        return None
    noun, verb = _NOUN[code], _VERB[code]
    lowered = norm(raw)
    if any(mark in raw for mark in _QUOTES):
        quoted = ' '.join(re.findall(r'[«“„"]([^»”“"]{1,200})[»”“"]', raw))
        body = norm(quoted)
        if body and (re.search(r'(?:^|\s)(?:' + noun + r')(?:\s|$)', body) or re.match(r'(?:' + verb + r')(?:\s|$)', body)):
            return CancelRequest('quoted')
        if re.match(r'^(?:' + _ABOUT[code] + r')', lowered):
            return None
    if re.match(r'^(?:' + _ABOUT[code] + r')\b', lowered) and re.search(r'(?:^|\s)(?:' + noun + r')(?:\s|$)', lowered):
        return CancelRequest('quoted')
    # A compound «не открывай Steam, отмени таймер» is split by the clause
    # guard first; only a single clause (polite commas aside) is owned here.
    polite_free = _without_polite(lowered)
    if re.search(r'[,;:]', polite_free.strip(' ,')):
        return None
    value = _repair_noun(_surface(raw, code), code)
    if re.fullmatch(_KEEP[code], value):
        return CancelRequest('keep')
    launch188 = re.fullmatch(_LAUNCH188[code], value)
    if launch188:
        target188 = launch188.group('t').strip(' «»"“”„')
        at188 = raw.casefold().rfind(target188.casefold())
        return CancelRequest('no_launch', target=raw[at188:at188 + len(target188)] if at188 >= 0 else target188)
    if code == 'de':
        compound188 = re.fullmatch(r'(?:brich|breche) (?:den )?(?P<k>abschalt|herunterfahr|energiespar|ruhezustand)[- ]?timer ab', value)
        if compound188:
            return CancelRequest('cancel_one', name=compound188.group('k'))
    art, every = _ART[code], _ALL[code]
    if code == 'de':
        if re.fullmatch(r'(?:brich|breche) ' + every + r' timer ab|(?:' + verb + r') ' + every + r' timer|alle (?:meine )?timer (?:abbrechen|löschen)', value):
            return CancelRequest('cancel_all')
        one = (re.fullmatch(r'(?:brich|breche) ' + art + r'timer(?: (?P<name>.+?))? ab', value)
               or re.fullmatch(r'(?:' + verb + r') ' + art + r'timer(?: (?P<name>.+))?', value)
               or re.fullmatch(r'timer(?: (?P<name>.+?))? abbrechen', value))
    else:
        if re.fullmatch(r'(?:' + verb + r') ' + every + r' (?:' + noun + r')', value) or (
                code == 'ru' and re.fullmatch(r'(?:все|всё)(?: мои)? таймеры (?:' + verb + r')', value)):
            return CancelRequest('cancel_all')
        one = (re.fullmatch(r'(?:' + verb + r') ' + art + r'(?:' + noun + r')(?: (?P<name>.+))?', value)
               or re.fullmatch(r'(?:' + noun + r') (?:' + verb + r')', value))
        if one is None and code == 'en':
            one = re.fullmatch(r'(?:' + verb + r') ' + art + r'(?P<name>.+?) timer', value)
    if one is not None:
        name = (one.groupdict().get('name') or '').strip(' «»"“”„')
        if re.fullmatch(r'(?:' + noun + r')', name):
            name = ''
        return CancelRequest('cancel_one', name=name)
    for action, pattern in _negations(code):
        found = re.fullmatch(pattern, value)
        if found:
            groups = {k: v for k, v in found.groupdict().items() if v}
            return CancelRequest('negated', action=action, target=next(iter(groups.values()), ''))
    return None


def _timers(manager):
    return [row for row in manager.list() if row.kind == 'timer']


def _match(rows, name):
    wanted = norm(name)
    exact = [row for row in rows if wanted in {row.id.casefold(), norm(row.label)}]
    if exact:
        return exact
    return [row for row in rows if re.search(r'(?:^|\s)' + re.escape(wanted) + r'(?:\s|$)', norm(row.label))]


_BROWSER = r'браузер|окно браузера|browser|browser window|den browser|das browserfenster|le navigateur|el navegador|il browser|o navegador'
_VIDEO = r'видео|ролик|video|vidéo|vídeo'


def _pending_actions(manager, request):
    rows = [row for row in manager.list() if row.kind == 'action']
    wanted = {'shutdown': 'shutdown'}.get(request.action)
    if request.action == 'stop_media' and re.fullmatch(_VIDEO, norm(request.target)):
        wanted = 'stop_video'
    if request.action == 'close' and re.fullmatch(_BROWSER, norm(request.target)):
        wanted = 'close_browser'
    if not wanted:
        return []
    return [row for row in rows if (row.action_spec or {}).get('action') == wanted]


def handle(agent, text, code):
    """Return an AgentTurn when the phrase belongs here, else None."""
    request = parse(text, code)
    if request is None:
        return None
    from jarvis.agent import AgentTurn
    if request.kind == 'quoted':
        return AgentTurn(_say('quoted', code), outcome='clarification', tool_details=['timer_cancel187.quoted'])
    if request.kind == 'keep':
        return AgentTurn(_say('keep', code), success=True, tool_details=['timer_cancel187.keep'])
    if request.kind == 'no_launch':
        return AgentTurn(_say('no_launch', code, target=request.target[:60]), success=False, outcome='clarification',
                         tool_details=['timer_cancel187.no_launch188'])
    from jarvis.runtime.timers import timer_manager
    manager = timer_manager(agent.config.paths.root, code)
    if request.kind == 'negated':
        desktop = getattr(agent, '_conversation_key', 'desktop') == 'desktop'
        matches = _pending_actions(manager, request) if desktop else []
        if not matches:
            return AgentTurn(locale_message('negated', code), success=True, tool_details=['timer_cancel187.negated'])
        from jarvis.runtime.delayed_actions184 import message as delayed_message
        if len(matches) > 1:
            listing = ', '.join(f'{row.id}: {row.label}' for row in matches[:6])
            return AgentTurn(_say('action_which', code, list=listing), success=False, outcome='clarification')
        try:
            removed = manager.cancel(matches[0].id)
        except OSError:
            return AgentTurn(delayed_message('failed', code, label=matches[0].label), success=False)
        if not removed:
            return AgentTurn(locale_message('negated', code), success=True, tool_details=['timer_cancel187.negated'])
        return AgentTurn(delayed_message('cancelled', code, label=matches[0].label), success=True, tool_details=['timer_cancel187.action_cancelled'])
    rows = _timers(manager)
    if request.kind == 'cancel_all':
        if not rows:
            return AgentTurn(_say('none', code), success=False, outcome='clarification', tool_details=['timer_cancel187.none'])
        return _execute(agent, text, {'action': 'cancel_all'}, code)
    if request.name:
        found = _match(rows, request.name)
        system188 = '' if found else _system_action188(request.name, code)
        if system188:
            return _cancel_system188(agent, manager, system188, code)
        if not found:
            if code == 'ru' and not re.match(r'убери', _without_polite(norm(text)).strip()):
                return None          # the reviewed Russian route keeps its own resolution
            # NEO 188 atlas: «убери таймер чай» has no Russian timer route — it used to close
            # an application named «таймер чай»; answer about the timer instead.
            return AgentTurn(_say('not_found', code, name=request.name[:60]), success=False, outcome='clarification',
                             tool_details=['timer_cancel187.not_found'])
    else:
        found = rows
    if not found:
        return AgentTurn(_say('none', code), success=False, outcome='clarification', tool_details=['timer_cancel187.none'])
    if len(found) > 1:
        listing = ', '.join(f'«{row.label}»' for row in found[:6])
        return AgentTurn(_say('which', code, list=listing), success=False, outcome='clarification', tool_details=['timer_cancel187.which'])
    return _execute(agent, text, {'action': 'cancel', 'id': found[0].id}, code)


def _cancel_system188(agent, manager, action, code):
    from jarvis.agent import AgentTurn
    from jarvis.runtime.delayed_actions184 import message as delayed_message
    desktop = getattr(agent, '_conversation_key', 'desktop') == 'desktop'
    rows = [row for row in manager.list() if row.kind == 'action' and (row.action_spec or {}).get('action') == action] if desktop else []
    label = _system_label188(action, code)
    if not rows:
        return AgentTurn(_say('no_action', code, label=label), success=False, outcome='clarification',
                         tool_details=['timer_cancel187.no_action188'])
    if len(rows) > 1:
        listing = ', '.join(f'{row.id}: {row.label}' for row in rows[:6])
        return AgentTurn(_say('action_which', code, list=listing), success=False, outcome='clarification')
    try:
        removed = manager.cancel(rows[0].id)
    except OSError:
        return AgentTurn(delayed_message('failed', code, label=rows[0].label), success=False)
    if not removed:
        return AgentTurn(_say('no_action', code, label=label), success=False, outcome='clarification')
    return AgentTurn(delayed_message('cancelled', code, label=rows[0].label), success=True,
                     tool_details=['timer_cancel187.action_cancelled'])


def _execute(agent, text, args, code):
    from jarvis.routing.ownership_firewall import bind_exact_tool_ownership
    from jarvis.runtime.router import RoutedIntent
    bind_exact_tool_ownership('timer', text=text)
    return agent._execute_routed_intent(RoutedIntent('timer.' + args['action'], 'timer', dict(args), 995), text)
