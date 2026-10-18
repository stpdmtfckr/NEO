"""NEO 184: words *about* a command are never the command.

Checked before every route (deterministic grammar, typo repair, the NEO command model and
the agent model), in 7 languages:

* ``negated``  — «Не выполняй следующую команду: заблокируй компьютер», «Führe den folgenden
  Befehl nicht aus: …», «N’exécute rien, c’est une citation : …». The whole part after the
  colon is under the negation, not just the word next to «не».
* ``quote``    — «Я цитирую: «…»», «Le document contient : « … »», «The email says: …».
* ``meta``     — «Что означает команда «сделай скриншот»?», «What does the command “clear
  clipboard” mean?»: an explanation is asked for, never the action.
* ``switch``   — «Забудь работу, почему небо голубое?»: a change of topic, not deleting memory.

The result only tells the agent *not* to act; it never executes anything itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

_Q = r'[«"“„‚\']'
_SEP = r'\s*[:：—–-]\s*'


@dataclass(frozen=True, slots=True)
class Meta:
    kind: str          # negated | quote | meta | switch
    body: str = ''     # the command / quoted text / the question after a topic switch


def _c(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I | re.S)


_NEGATED = {
    'ru': _c(r'^(?:пожалуйста\s+)?не\s+(?:выполняй|исполняй|запускай|делай|выполняйте)'
             r'(?:\s+(?:следующую|эту|такую|данную|эти|следующие))?(?:\s+(?:команду|команды|инструкцию|просьбу))?'
             r'(?:\s*,\s*(?:это|тут)\s+(?:цитата|пример))?' + _SEP + r'(?P<body>.+)$'),
    'en': _c(r'^(?:please\s+)?(?:do\s+not|don[’\']t|never)\s+(?:run|execute|perform|do|carry\s+out)'
             r'(?:\s+(?:the|this|that|these))?(?:\s+following)?(?:\s+(?:command|commands|instruction|request))?(?:\s+below)?'
             r'(?:\s*,\s*(?:it[’\']s|this\s+is)\s+(?:a\s+quote|an\s+example))?' + _SEP + r'(?P<body>.+)$'),
    'de': _c(r'^(?:bitte\s+)?(?:(?:führe|fuehre)\s+(?:den|diesen|die|das)\s+(?:folgenden?\s+)?(?:befehl|kommando|auftrag|anweisung)\s+nicht\s+aus'
             r'|(?:führe|fuehre)\s+nichts\s+aus|nicht\s+ausführen)'
             r'(?:\s*,\s*(?:das|es)\s+ist\s+(?:ein\s+zitat|ein\s+beispiel))?' + _SEP + r'(?P<body>.+)$'),
    'fr': _c(r'^(?:n[’\']|ne\s+)(?:exécute|execute|lance|fais|exécutez)\s+(?:pas|rien)'
             r'(?:\s+(?:la|cette|ces))?(?:\s+(?:commande|commandes|instruction))?(?:\s+suivantes?)?'
             r'(?:\s*,\s*c[’\']est\s+(?:une\s+citation|un\s+exemple))?' + _SEP + r'(?P<body>.+)$'),
    'es': _c(r'^no\s+(?:ejecutes|ejecute|hagas|lances|realices)(?:\s+(?:el|este|la|esta|los|las))?(?:\s+siguientes?)?'
             r'(?:\s+(?:comando|comandos|orden|instrucción|instruccion))?(?:\s+siguientes?)?(?:\s+nada)?'
             r'(?:\s*,\s*es\s+(?:una\s+cita|un\s+ejemplo))?' + _SEP + r'(?P<body>.+)$'),
    'it': _c(r'^non\s+(?:eseguire|esegui|fare|lanciare|eseguite)(?:\s+(?:il|questo|la|questa|i|niente))?(?:\s+seguent[ei])?'
             r'(?:\s+(?:comando|comandi|istruzione|ordine))?(?:\s+seguent[ei])?'
             r'(?:\s*,\s*è\s+(?:una\s+citazione|un\s+esempio))?' + _SEP + r'(?P<body>.+)$'),
    'pt': _c(r'^não\s+(?:executes|execute|faças|faça|corras|executem)(?:\s+(?:o|este|a|esta|nada))?(?:\s+seguintes?)?'
             r'(?:\s+(?:comando|comandos|ordem|instrução|instrucao))?(?:\s+seguintes?)?'
             r'(?:\s*,\s*é\s+(?:uma\s+citação|um\s+exemplo))?' + _SEP + r'(?P<body>.+)$'),
}

_QUOTE = {
    'ru': _c(r'^(?:(?:я\s+)?цитирую|это\s+цитата|цитата' + _SEP + r'|(?:в|во)\s+(?:документе|тексте|письме|файле|сообщении|чате|статье|инструкции|заметке)\s+'
             r'(?:написано|сказано|говорится|указано)|(?:документ|текст|письмо|файл|сообщение|заметка)\s+(?:содержит|гласит)|на\s+(?:экране|странице)\s+(?:написано|показано))'),
    'en': _c(r'^(?:(?:i\s+am|i[’\']m)\s+quoting|i\s+quote|quote' + _SEP + r'|(?:this\s+is|it[’\']s)\s+a\s+quote'
             r'|the\s+(?:document|text|email|e-mail|file|message|article|note|screen|page)\s+(?:says|reads|contains|shows|states)|it\s+says' + _SEP + r')'),
    'de': _c(r'^(?:ich\s+zitiere|(?:das\s+ist\s+)?ein\s+zitat' + _SEP + r'|zitat' + _SEP + r'|im\s+(?:dokument|text|brief|chat|artikel)\s+steht'
             r'|in\s+der\s+(?:nachricht|e-mail|mail|datei|notiz)\s+steht|(?:das\s+|die\s+)?(?:dokument|text|nachricht|e-mail)\s+(?:enthält|sagt|lautet)'
             r'|auf\s+dem\s+bildschirm\s+steht)'),
    'fr': _c(r'^(?:je\s+cite|(?:c[’\']est\s+une\s+)?citation' + _SEP + r'|le\s+(?:document|texte|message|mail|courriel|fichier)\s+(?:contient|dit|indique|affiche)'
             r'|(?:dans\s+le\s+(?:document|message|texte|mail)\s*,?\s+)?il\s+est\s+écrit|l[’\']écran\s+(?:affiche|indique))'),
    'es': _c(r'^(?:estoy\s+citando|cito\b|(?:es\s+una\s+)?cita' + _SEP + r'|el\s+(?:documento|texto|mensaje|correo|archivo)\s+(?:dice|contiene|pone)'
             r'|en\s+el\s+(?:documento|mensaje|texto|correo)\s+(?:dice|pone|está\s+escrito)|la\s+pantalla\s+(?:dice|muestra))'),
    'it': _c(r'^(?:sto\s+citando|cito\b|(?:è\s+una\s+)?citazione' + _SEP + r'|il\s+(?:documento|testo|messaggio|file)\s+(?:dice|contiene)'
             r'|nel\s+(?:documento|messaggio|testo|file)\s+(?:c[’\']è\s+scritto|dice|si\s+legge)|lo\s+schermo\s+(?:dice|mostra))'),
    'pt': _c(r'^(?:estou\s+a\s+citar|estou\s+citando|cito\b|(?:é\s+uma\s+)?citação' + _SEP + r'|o\s+(?:documento|texto|ficheiro|arquivo|email)\s+(?:diz|contém)'
             r'|a\s+mensagem\s+(?:diz|contém)|na\s+mensagem\s+está\s+escrito|no\s+(?:documento|texto|ecrã)\s+(?:diz|está\s+escrito))'),
}

_META = {
    'ru': _c(r'^(?:что\s+(?:означает|значит|делает)|зачем\s+нужна|для\s+чего\s+(?:нужна\s+)?|почему|как\s+работает)\s+(?:эта\s+|та\s+)?команд[аы]\b'
             r'|^что\s+(?:означает|значит)\s+(?:эта|это|та)\s+(?:фраза|выражение|команда)\b'),
    'en': _c(r'^(?:what\s+does|why\s+does|how\s+does|what\s+is)\s+(?:the|this|that)\s+command\b'
             r'|^what\s+does\s+(?:the|this|that)\s+(?:phrase|sentence)\b|^what\s+(?:does|do)\s+' + _Q + r'.+?' + r'[»"”\']\s+(?:mean|do)\b'),
    'de': _c(r'^(?:was\s+bedeutet|was\s+macht|wozu\s+dient|warum|wie\s+funktioniert)\s+(?:der|dieser|dieses|diese|das)\s+(?:befehl|kommando|satz|ausdruck)\b'),
    'fr': _c(r'^(?:que\s+(?:signifie|veut\s+dire|fait)|pourquoi|comment\s+fonctionne)\s+(?:la|cette|le|ce)\s+(?:commande|phrase|expression)\b'
             r'|^qu[’\']est-ce\s+que\s+(?:signifie|veut\s+dire)\s+(?:la|cette)\s+(?:commande|phrase)\b'),
    'es': _c(r'^¿?\s*(?:qué\s+(?:significa|quiere\s+decir|hace)|por\s+qué|cómo\s+funciona)\s+(?:el|este|la|esta)\s+(?:comando|orden|frase)\b'),
    'it': _c(r'^(?:(?:che\s+)?cosa|che)\s+(?:significa|vuol\s+dire|fa)\s+(?:il|questo|la|questa)\s+(?:comando|frase)\b|^perché\s+(?:il|questo)\s+comando\b'),
    'pt': _c(r'^(?:o\s+que|que)\s+(?:significa|quer\s+dizer|faz)\s+(?:o|este|a|esta)\s+(?:comando|frase)\b|^porque\s+(?:é\s+que\s+)?o\s+comando\b'),
}

# «Забудь работу, почему небо голубое?»: «forget <topic>», then a question.
_FORGET = {
    'ru': r'забудь|забудем|оставим|хватит\s+(?:о|про)', 'en': r'forget(?:\s+about)?|enough\s+about|never\s+mind',
    'de': r'vergiss|vergessen\s+wir|genug\s+von', 'fr': r'oublie|oublions|assez\s+parlé\s+de',
    'es': r'olvida|olvidemos|basta\s+de', 'it': r'dimentica|lasciamo\s+stare|basta\s+con', 'pt': r'esquece|esqueçamos|chega\s+de',
}
_MEMORY_WORDS = re.compile(r'запомн|памят|memor|remember|gedächtnis|merk|mémoire|souviens|recuerd|ricord|lembr|заметк|note|notiz', re.I)


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _NEGATED else 'en'


def _clean(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip()


_POLITE_PREFIX184 = {
    'ru': r'пожалуйста', 'en': r'please', 'de': r'bitte',
    'fr': r"s[’']il\s+(?:te|vous)\s+pla[îi]t", 'es': r'por\s+favor',
    'it': r'per\s+favore', 'pt': r'por\s+favor',
}


def _surface(text: str, code: str) -> str:
    """Remove only courtesy prefixes before classifying the complete scope.

    Commas, semicolons and dashes after 'please' cannot expose an embedded
    forbidden command to downstream fuzzy matching. Payloads stay untouched.
    """
    value = _clean(text)
    pattern = r'^(?:' + _POLITE_PREFIX184[_code(code)] + r')(?:\s*[,;:!—–-]\s*|\s+)'
    for _ in range(4):
        newer = re.sub(pattern, '', value, count=1, flags=re.I)
        if newer == value:
            break
        value = newer
    return value


# «Открой приложение Steam, нет, не открывай его» — the last word is the refusal.
_RETRACT = _c(r'[,;.!]\s*(?:нет|no|nein|non|não|nao)\s*[,!.]?\s*(?:не\s|ничего|don[’\']t|do\s+not|nothing|nicht|nichts|ne\s|n[’\']|rien|no\s|nada|non\s|niente|não\s|nao\s)')
_CHANGED_MIND = _c(r'передумал|changed\s+my\s+mind|anders\s+überlegt|changé\s+d[’\']avis|cambiado\s+de\s+opinión|cambiato\s+idea|mudei\s+de\s+ideias?')
_NOTHING = _c(r'^(?:ничего\s+не|не\s+\w+\s+ничего|don[’\']t\s+\w+\s+anything|do\s+not\s+\w+\s+anything|\w+\s+nichts|n[’\']\w+\s+rien|ne\s+\w+\s+rien|no\s+\w+\s+nada|non\s+\w+\s+niente|não\s+\w+\s+nada)\b')


def _open_quote(text: str) -> bool:
    if text.count('«') > text.count('»') or text.count('„') > text.count('“'):
        return True
    return text.count('"') % 2 == 1 or (text.count('“') > text.count('”') and '„' not in text)


def mixed_rewrite(text: str, code: str) -> str:
    """«Не открывай Steam, открой приложение Calculator» -> «открой приложение Calculator».

    Only when the first clause is a negated action and the rest is a positive one; '' otherwise."""
    value = _surface(text, code)
    parts = re.split(r'\s*[,;]\s*', value, maxsplit=1)
    if len(parts) != 2 or not parts[1]:
        return ''
    from jarvis.runtime.natural_commands import negated_action, looks_like_new_command
    first, rest = parts
    # NEO 187 (REC-CANCEL-SCOPE-01): «Не запускай следующую команду: A, B» —
    # the ban introduced by a colon covers the whole list after it, and a
    # comma inside quotes is part of the quoted command, not a new order.
    if ':' in first or _open_quote(first):
        return ''
    if negated_action(first, code) and not negated_action(rest, code) and looks_like_new_command(rest, code, cancel_orders=True):
        return rest
    return ''


def classify(text: str, code: str) -> Meta | None:
    value = _surface(text, code)
    if not value:
        return None
    code = _code(code)
    # Dictated text is data, even if it contains a withdrawal or quotation.
    from jarvis.runtime.multilingual_commands import dictation_payload184
    if dictation_payload184(value, code) is not None:
        return None
    if code == 'ru':
        from jarvis.runtime.natural_commands import negated_object_first_ru
        if negated_object_first_ru(value):
            return Meta('negated', value)
    lowered = value.casefold()
    if _RETRACT.search(lowered) or (_CHANGED_MIND.search(lowered) and _NOTHING.search(lowered)):
        return Meta('retracted', value)
    found = _NEGATED[code].match(lowered)
    if found:
        return Meta('negated', value[found.start('body'):].strip(' «»"“”„'))
    if _QUOTE[code].match(lowered):
        return Meta('quote', value)
    if _META[code].match(lowered):
        return Meta('meta', value)
    switch = re.match(r'^(?:' + _FORGET[code] + r')\s+(?P<topic>[^,.;:!?]{1,40})\s*[,.;:—-]\s*(?P<rest>.+\?)\s*$', lowered)
    if switch and not _MEMORY_WORDS.search(switch.group('topic')):
        return Meta('switch', value[switch.start('rest'):].strip())
    return None


_TEXT = {
    'negated': ('Хорошо, не выполняю: «{body}».', 'OK, I won’t run it: “{body}”.', 'Gut, ich führe das nicht aus: „{body}“.',
                'D’accord, je ne l’exécute pas : « {body} ».', 'De acuerdo, no lo ejecuto: «{body}».', 'Va bene, non lo eseguo: «{body}».',
                'Está bem, não executo: «{body}».'),
    'retracted': ('Хорошо, ничего не делаю.', 'OK, I won’t do anything.', 'Gut, ich mache nichts.', 'D’accord, je ne fais rien.',
                  'De acuerdo, no hago nada.', 'Va bene, non faccio nulla.', 'Está bem, não faço nada.'),
    'meta': ('Это вопрос о команде, поэтому я её не выполняю. Если нужно её выполнить, скажите команду отдельно.',
             'That is a question about a command, so I am not running it. If you want it done, say the command on its own.',
             'Das ist eine Frage zu einem Befehl, deshalb führe ich ihn nicht aus. Wenn du ihn ausführen willst, sag den Befehl einzeln.',
             'C’est une question sur une commande, je ne l’exécute donc pas. Pour l’exécuter, dites la commande seule.',
             'Es una pregunta sobre un comando, así que no lo ejecuto. Si quieres ejecutarlo, di el comando por separado.',
             'È una domanda su un comando, quindi non lo eseguo. Se vuoi eseguirlo, di’ il comando da solo.',
             'É uma pergunta sobre um comando, por isso não o executo. Se quiseres executá-lo, diz o comando sozinho.'),
}
_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def message(kind: str, code: str, **values: str) -> str:
    return _TEXT[kind][_LANGS.index(_code(code))].format(**values)


# ---- NEO 184: polite requests become the plain command («Я хотел бы, чтобы ты открыл X» -> «открой X») ----

_POLITE = {
    'ru': (_c(r'^(?:я\s+)?(?:хотел(?:а)?\s+бы|хочу|хотелось\s+бы|было\s+бы\s+здорово),?\s+чтобы\s+ты\s+(?P<verb>\w+)\s+(?P<rest>.+?)[.!?]*$'),
           {'открыл': 'открой', 'открыла': 'открой', 'запустил': 'запусти', 'запустила': 'запусти', 'закрыл': 'закрой', 'закрыла': 'закрой',
            'включил': 'включи', 'включила': 'включи', 'выключил': 'выключи', 'выключила': 'выключи', 'нашёл': 'найди', 'нашел': 'найди',
            'нашла': 'найди', 'показал': 'покажи', 'показала': 'покажи', 'поставил': 'поставь', 'поставила': 'поставь', 'сделал': 'сделай',
            'сделала': 'сделай', 'свернул': 'сверни', 'развернул': 'разверни'}, 'front'),
    'de': (_c(r'^ich\s+(?:möchte|moechte|will|hätte\s+gern|haette\s+gern|würde\s+gern),?\s+dass\s+du\s+(?P<rest>.+?)\s+(?P<verb>\w+)[.!?]*$'),
           {'öffnest': 'öffne', 'oeffnest': 'öffne', 'startest': 'starte', 'schließt': 'schließe', 'schliesst': 'schließe', 'suchst': 'suche',
            'findest': 'finde', 'zeigst': 'zeige', 'machst': 'mache', 'einschaltest': 'schalte {rest} ein', 'ausschaltest': 'schalte {rest} aus',
            'aufmachst': 'öffne', 'zumachst': 'schließe'}, 'front'),
    'it': (_c(r'^(?:vorrei|voglio|mi\s+piacerebbe|desidero)\s+che\s+(?:tu\s+)?(?P<verb>\w+)\s+(?P<rest>.+?)[.!?]*$'),
           {'aprissi': 'apri', 'apra': 'apri', 'avviassi': 'avvia', 'avvii': 'avvia', 'chiudessi': 'chiudi', 'chiuda': 'chiudi',
            'cercassi': 'cerca', 'trovassi': 'trova', 'mostrassi': 'mostra', 'accendessi': 'accendi', 'spegnessi': 'spegni'}, 'front'),
    'pt': (_c(r'^(?:poderias|podias|poderá|conseguias|consegues|conseguirias|gostava\s+que|gostaria\s+que|queria\s+que)\s+(?P<verb>\w+)\s+(?P<rest>.+?)[.!?]*$'),
           {'abrir': 'abre', 'abrisses': 'abre', 'abras': 'abre', 'fechar': 'fecha', 'fechasses': 'fecha', 'iniciar': 'inicia', 'iniciasses': 'inicia',
            'procurar': 'procura', 'procurasses': 'procura', 'encontrar': 'encontra', 'mostrar': 'mostra', 'mostrasses': 'mostra'}, 'front'),
    'es': (_c(r'^(?:me\s+gustaría|me\s+gustaria|quisiera|querría|querria)\s+que\s+(?P<verb>\w+)\s+(?P<rest>.+?)[.!?]*$'),
           {'abrieras': 'abre', 'abras': 'abre', 'cerraras': 'cierra', 'cierres': 'cierra', 'buscaras': 'busca', 'busques': 'busca',
            'iniciaras': 'inicia', 'inicies': 'inicia', 'mostraras': 'muestra', 'muestres': 'muestra'}, 'front'),
    'fr': (_c(r'^(?:je\s+voudrais|j[’\']aimerais|je\s+veux|j[’\']aimerais\s+bien)\s+que\s+tu\s+(?P<verb>\w+)\s+(?P<rest>.+?)[.!?]*$'),
           {'ouvres': 'ouvre', 'fermes': 'ferme', 'lances': 'lance', 'cherches': 'cherche', 'trouves': 'trouve', 'montres': 'montre',
            'allumes': 'allume', 'éteignes': 'éteins'}, 'front'),
}


def polite_rewrite(text: str, code: str) -> str:
    """The plain imperative for a polite subordinate-clause request, or ''."""
    value = _clean(text)
    row = _POLITE.get(_code(code))
    if not value or row is None:
        return ''
    pattern, verbs, _order = row
    found = pattern.match(value)
    if not found:
        return ''
    verb = verbs.get(found.group('verb').casefold())
    rest = found.group('rest').strip()
    if not verb or not rest:
        return ''
    return verb.format(rest=rest) if '{rest}' in verb else f'{verb} {rest}'
