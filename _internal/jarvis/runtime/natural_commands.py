"""Small locale-owned command grammar. Slots are copied, never translated.

No network/model calls, device inspection or filesystem access on the hot path.
Unmatched sentences remain available to the mature domain routers.
"""
from __future__ import annotations

from functools import lru_cache
from jarvis.runtime import cached_re as re
import unicodedata

LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")

WRAPPERS = {
    "ru": ("пожалуйста", "можешь", "можете", "не мог бы ты", "не могла бы ты", "не мог бы", "будь добр", "будь добра", "прошу тебя", "мне нужно чтобы ты", "я хочу чтобы ты", "давай", "помоги мне"),
    "en": ("please", "can you", "could you", "would you", "will you", "I need you to", "I want you to", "I'd like you to", "would you kindly", "help me", "let's"),
    "de": ("bitte", "kannst du", "könntest du", "würdest du", "ich möchte dass du", "ich brauche deine Hilfe", "sei so nett und", "hilf mir"),
    "fr": ("s'il te plaît", "s'il vous plaît", "peux-tu", "peux tu", "pourrais-tu", "pourrais tu", "veux-tu bien", "j'aimerais que tu", "je voudrais que tu", "aide-moi à"),
    "es": ("por favor", "puedes", "podrías", "podrias", "quiero que", "necesito que", "me gustaría que", "ayúdame a", "ayudame a"),
    "it": ("per favore", "puoi", "potresti", "vorrei che", "ho bisogno che", "ti chiedo di", "aiutami a", "per piacere"),
    "pt": ("por favor", "podes", "pode", "você pode", "voce pode", "poderia", "quero que", "preciso que", "gostaria que", "ajuda-me a", "me ajude a"),
}

# Natural task framing applies to every registered domain, while the inner
# command still has to pass exact/slot/risk checks. These are not extra intents.
TASK_FRAMES = {
    "ru": ("Следующая задача", "Сделай следующее", "Мне нужна помощь с этим", "Есть просьба", "Теперь вот что", "Давай выполним команду", "Хочу попросить вот о чём", "Для начала"),
    "en": ("Next task", "Do the following", "Help me with this", "I have a request", "Here is what I need", "Please handle this task", "One more thing", "To begin"),
    "de": ("Nächste Aufgabe", "Mach bitte Folgendes", "Hilf mir damit", "Ich habe eine Bitte", "Das brauche ich jetzt", "Erledige bitte diese Aufgabe", "Noch etwas", "Zum Anfang"),
    "fr": ("Prochaine tâche", "Fais ceci", "Aide-moi avec ceci", "J'ai une demande", "Voici ce qu'il me faut", "Occupe-toi de cette tâche", "Encore une chose", "Pour commencer"),
    "es": ("Siguiente tarea", "Haz lo siguiente", "Ayúdame con esto", "Tengo una petición", "Esto es lo que necesito", "Encárgate de esta tarea", "Una cosa más", "Para empezar"),
    "it": ("Prossima attività", "Fai quanto segue", "Aiutami con questo", "Ho una richiesta", "Ecco cosa mi serve", "Occupati di questa attività", "Un'altra cosa", "Per cominciare"),
    "pt": ("Próxima tarefa", "Faz o seguinte", "Ajuda-me com isto", "Tenho um pedido", "É disto que preciso", "Trata desta tarefa", "Mais uma coisa", "Para começar"),
}

# Canonical action and object shells. Native infinitives/subjunctives cover polite
# and indirect constructions without changing the user's title or filename.
VERBS = {
    "ru": ("открой|открыть|открыл|открыла|откройте", "закрой|закрыть|закрыл|закрыла|закройте", "найди|найти|поищи|отыщи|найдите", "создай|создать|создал|создала", "прочитай|прочитать|прочитал", "удали|удалить|удалил"),
    "en": ("open|launch|start|run", "close|quit|exit", "find|locate|look for|search for", "create|make", "read|read out", "delete|remove"),
    "de": ("öffne|oeffne|öffnen|starten|starte|öffnest", "schließe|schliesse|schließen|beende|schließt", "finde|finden|suche|suchen|suchst", "erstelle|erstellen|anlegen|lege an", "lies|lesen|vorlesen", "lösche|loesche|löschen|entferne"),
    "fr": ("ouvre|ouvrir|ouvres|lance|lancer", "ferme|fermer|fermes|quitte", "trouve|trouver|trouves|cherche|chercher", "crée|cree|créer|creer|crées", "lis|lire|lises", "supprime|supprimer|efface|effacer"),
    "es": ("abre|abrir|abras|inicia|iniciar|ejecuta", "cierra|cerrar|cierres", "busca|buscar|busques|encuentra|encontrar|localiza", "crea|crear|crees", "lee|leer|leas", "elimina|eliminar|elimines|borra|borrar"),
    "it": ("apri|aprire|aprissi|avvia|avviare", "chiudi|chiudere|chiudessi", "trova|trovare|trovassi|cerca|cercare", "crea|creare|creassi", "leggi|leggere|leggessi", "elimina|eliminare|cancella|cancellare"),
    "pt": ("abre|abra|abrir|abras|inicia|inicie", "fecha|feche|fechar|feches", "encontra|encontre|encontrar|procura|procure|localiza", "cria|crie|criar", "lê|leia|ler", "apaga|apague|apagar|exclua|elimina"),
}
OBJECTS = {
    "ru": ("приложение|программу|приложения|программы", "файл|документ", "папку|директорию|каталог", "игру"),
    "en": ("application|app|program", "file|document", "folder|directory", "game"),
    "de": ("Anwendung|App|Programm", "Datei|Dokument", "Ordner|Verzeichnis", "Spiel"),
    "fr": ("application|appli|programme", "fichier|document", "dossier|répertoire", "jeu"),
    "es": ("aplicación|aplicacion|programa|app", "archivo|fichero|documento", "carpeta|directorio", "juego"),
    "it": ("applicazione|app|programma", "file|documento", "cartella|directory", "gioco"),
    "pt": ("aplicação|aplicacao|aplicativo|programa|app", "ficheiro|arquivo|documento", "pasta|diretório|diretorio", "jogo"),
}
_ARTICLES = {"ru": "(?:мне|этот|эту|мой|мою)", "en": "(?:the|a|an|my)", "de": "(?:die|das|den|der|eine|ein|einen|meine|mein)", "fr": "(?:le|la|les|un|une|mon|ma|l')", "es": "(?:el|la|un|una|mi)", "it": "(?:il|lo|la|un|una|mio|mia|l')", "pt": "(?:o|a|um|uma|meu|minha)"}
_CANONICAL_VERBS = ("открой", "закрой", "найди", "создай", "прочитай", "удали")
_CANONICAL_OBJECTS = ("приложение", "файл", "папку", "игру")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "е").replace("’", "'")).strip(" \t\r\n!?.,:;")


# NEO 175: spoken lead-ins before a command («слушай, …», «а теперь …», «hey, …»). They broke
# exact matching: «слушай, больше не включай текущую песню» even became «play». Words that
# can begin a real command («слушай музыку», «next track») count only when a comma follows.
DISCOURSE = {
    "ru": ("а теперь", "ну а теперь", "теперь", "ну-ка", "ну ка", "ну давай", "давай-ка", "короче", "итак", "кстати", "ну"),
    "en": ("and now", "now then", "alright then"),
    "de": ("und jetzt", "und nun", "jetzt mal"),
    "fr": ("et maintenant", "maintenant"),
    "es": ("y ahora", "ahora"),
    "it": ("e adesso", "e ora", "adesso"),
    "pt": ("e agora", "agora"),
}
DISCOURSE_COMMA = {
    "ru": ("слушай", "послушай", "слышь", "смотри", "так", "так вот", "окей", "ок", "ладно", "хорошо", "эй", "значит", "вот что"),
    "en": ("hey", "listen", "look", "ok", "okay", "so", "well", "alright", "right", "now", "then"),
    "de": ("hey", "hör mal", "schau mal", "okay", "ok", "also", "na", "jetzt"),
    "fr": ("dis", "écoute", "ecoute", "hé", "eh", "ok", "bon", "alors", "bref"),
    "es": ("oye", "mira", "escucha", "vale", "ok", "bueno", "pues"),
    "it": ("senti", "ascolta", "ehi", "ok", "allora", "dai", "beh"),
    "pt": ("olha", "escuta", "ei", "ok", "bem", "então", "entao"),
}


def _alternation(words) -> str:
    return "|".join(re.escape(x).replace("’", "['’]").replace("\\'", "['’]") if "’" in x else re.escape(x).replace("'", "['’]")
                    for x in sorted(words, key=len, reverse=True))


@lru_cache(maxsize=7)
def _wrapper_regex(code: str) -> re.Pattern[str]:
    words = WRAPPERS.get(code, WRAPPERS["ru"]) + TASK_FRAMES.get(code, TASK_FRAMES["ru"]) + DISCOURSE.get(code, ())
    comma_only = DISCOURSE_COMMA.get(code, ())
    return re.compile(r"^(?:(?:" + _alternation(words) + r")(?:\s*[,;:]\s*|\s+)"
                      + (r"|(?:" + _alternation(comma_only) + r")\s*[,;:!]\s*" if comma_only else "") + ")", re.I)


# NEO 175: «калькулятор открой», «громкость 50 поставь»: the verb said last moves to the front.
_LAST_VERB_RU = re.compile(
    r"^(?P<rest>(?:[^\s«»\"':]+\s+){0,3}[^\s«»\"':]+)\s+(?P<verb>открой|закрой|включи|выключи|запусти|покажи|найди|поставь|"
    r"сделай|убери|вставь|скопируй|сохрани|повтори|возобнови|останови|отключи|подключи|перезапусти|сверни|разверни|обнови|"
    r"проверь|прибавь|убавь|уменьши|увеличь|переключи|включите|откройте|запустите|покажите|верни|выбери|выдели|выполни|"
    r"дообучи|забудь|заверши|закрепи|открепи|исправь|нажми|очисти|отмени|приостанови|переведи|прочитай|переименуй|добавь|переключись|"
    r"создай|восстанови|перейди|прокрути|скачай|установи|продолжи|заблокируй|разблокируй|отправь|удали|запомни|напомни)[.!]?$", re.I)
_NEGATION_RU = re.compile(r"(?:^|\s)(?:не|ни|нет)(?:\s|$)", re.I)


def verb_first_ru(text: str) -> str:
    value = str(text or "").strip()
    match = _LAST_VERB_RU.match(value)
    if not match or _NEGATION_RU.search(value):
        return value
    return f"{match.group('verb')} {match.group('rest')}"


def negated_object_first_ru(text: str) -> bool:
    """Bounded unquoted object-first imperative: «на окно X не переключись»."""
    match = _LAST_VERB_RU.fullmatch(str(text or '').strip())
    return bool(match and re.search(r'(?:^|\s)не$', match.group('rest'), re.I))


def strip_request_shell(text: str, code: str) -> str:
    value = str(text or "").strip()
    # Limit iterations even for adversarial/repeated scaffolding.
    for _ in range(4):
        newer = _wrapper_regex(code).sub("", value, count=1)
        if newer == value:
            break
        value = newer
    # Remove only a punctuation-delimited polite tail outside protected data.
    polite = {"ru":r"пожалуйста", "en":r"please", "de":r"bitte",
              "fr":r"s['’]il (?:te|vous) pla[îi]t", "es":r"por favor",
              "it":r"per favore", "pt":r"por favor"}
    # Dictation after a colon and quotations are opaque; keep their words.
    if not re.search(r'[«»“”"]|:\s', value):
        value = re.sub(r"[,;]\s*(?:" + polite.get(code, polite["ru"]) + r")[.!?]*$", "", value, flags=re.I)
        if code == "ru":
            value = verb_first_ru(value)
    return value.strip()


def negated_action(text: str, code: str) -> bool:
    """Block negated *command shells*, not song titles or factual questions."""
    value = norm(strip_request_shell(text, code))
    if code == 'ru' and negated_object_first_ru(value):
        return True
    if code == 'fr' and re.match(r"^(?:ne\s+|n')", value, re.I):
        # NEO 188 atlas: a whole phrase DECLARED as a command («ne répète pas les chansons
        # automatiquement» = repeat off) is that command, not a refusal of some other action.
        from jarvis.runtime.canonical_commands import _matching_templates
        if _matching_templates(value, code):
            return False
    negative = {
        "ru": r"(?:не\s+(?:надо\s+|нужно\s+|хочу\s+)?|не\s+смей\s+)",
        "en": r"(?:do\s+not\s+|don't\s+|dont\s+|never\s+)",
        "de": r"(?:nicht\s+|bitte\s+nicht\s+)",
        "fr": r"(?:ne\s+|n')", "es": r"(?:no\s+(?:quiero\s+|debes\s+)?)",
        "it": r"(?:non\s+)", "pt": r"(?:não\s+|nao\s+)",
    }[code if code in LANGUAGES else "ru"]
    transport_verbs = {
        "ru": r"(?:открыва|закрыва|удаля|выключа|перезагружа|запуска|отправля|включа|отменя|сбрасыва|останавлива|стира)\w*|выключи|перезагрузи|отправь|включи|поставь|останови|продолжи|скачай|установи|отмени|удали|сбрось",
        "en": "play|pause|stop|resume|send|shutdown|restart|download|install|turn off|cancel|delete|remove|clear",
        "de": "spiele|pausiere|stoppe|sende|starte|lade|installiere|schalte|breche|lösche|entferne",
        "fr": "joue|arrête|envoie|redémarre|télécharge|installe|éteins|annule|supprime",
        "es": "reproduce|pausa|detén|envía|reinicia|descarga|instala|apaga|cancela|canceles|borra|borres|elimina|elimines",
        "it": "riproduci|ferma|invia|riavvia|scarica|installa|spegni|annullare|annulla|cancellare|cancella|eliminare|elimina",
        "pt": "reproduza|reproduz|pare|para|envia|envie|reinicia|instala|desliga|cancele|cancela|apague|apaga|remova|remove",
    }
    verbs = "|".join(VERBS.get(code, VERBS["ru"])) + "|" + transport_verbs.get(code, transport_verbs["ru"])
    if re.match(r"^" + negative + r"(?:" + verbs + r")(?:\s|$)", value, re.I):
        return True
    if code == "ru" and re.match(r"^не\s+(?:надо\s+|нужно\s+)?(?:переводить|перевести|усыплять|гасить|вырубать|отключать|"
                                 r"перезапускать|ставить|сбрасывать|чистить|очищать|отправлять|скачивать|устанавливать)(?:\s|$)", value, re.I):
        # NEO 188 atlas: «не переводить компьютер в гибернацию» — a negated
        # infinitive at the head of a request refuses the action like the imperative.
        return True
    if code == "fr" and re.match(r"^(?:ne\s+|n')(?:me\s+|m'|le\s+|la\s+|les\s+|l')?[^\W\d_]+\s+(?:pas|jamais|plus|point)\b", value, re.I):
        # MEDIA185-U08: the French bipartite negative imperative («n’active pas
        # …», «ne lance jamais …») is a refusal whatever the verb is.
        return True
    denied = re.match(r"^" + negative, value, re.I)
    if denied:
        # Expanded native lexemes (e.g. "не активируй") must get the same
        # fail-closed treatment without maintaining a second verb catalogue.
        # Import only on a negative shell, and query the grammar without binding.
        from jarvis.runtime.canonical_commands import _matching_templates
        if _matching_templates(value[denied.end():], code):
            return True
    # German negation follows the imperative; only inspect its final clause.
    if code == "de" and re.match(r"^(?:" + verbs + r")\b", value, re.I):
        if re.search(r"\s+nicht(?:\s+.*)?$", value, re.I):
            return True
        # NEW185-DE-NEGATION-TYPO: «nihct», «nichtt», «niicht» are still the
        # negation, not part of an application name. Real words such as
        # «Nacht» or «nichts» are not treated as a misspelled «nicht».
        return any(_nicht_typo(token) for token in value.split()[1:])
    return False


def _nicht_typo(token: str) -> bool:
    word = re.sub(r"[^a-zäöüß]", "", str(token or "").casefold())
    if word == "nicht" or not 4 <= len(word) <= 6 or not word.startswith("n"):
        return False
    # German words one edit from «nicht» that are not the negation.
    if word in {"nacht", "nichts", "nichte", "nicht"} or not _damerau1(word, "nicht"):
        return False
    return True


def _damerau1(a: str, b: str) -> bool:
    """True when ``a`` is one edit (incl. a swap) away from ``b``."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


@lru_cache(maxsize=7)
def _patterns(code: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    code = code if code in LANGUAGES else "ru"
    rows = []
    article = _ARTICLES[code] + r"\s*"
    for verbs, verb in zip(VERBS[code], _CANONICAL_VERBS):
        for objects, obj in zip(OBJECTS[code], _CANONICAL_OBJECTS):
            shell = r"(?:" + verbs + r")\s+(?:" + article + r")?(?:" + objects + r")"
            # Require a payload; missing slots belong to DialogueSession.
            rows.append((re.compile(r"^" + shell + r"\s+(?P<value>.+?)\s*$", re.I | re.S), verb + " " + obj))
    return tuple(rows)


def normalize_natural_command(text: str, code: str) -> str | None:
    """Return canonical shell + original payload, or None if not owned here."""
    value = strip_request_shell(text, code)
    if negated_action(value, code):
        return None
    for pattern, shell in _patterns(code):
        match = pattern.fullmatch(value)
        if match:
            return shell + " " + match.group("value")
    return None


def missing_target(text: str, code: str) -> str:
    value = strip_request_shell(text, code).strip(" \t\r\n?!.,")
    code = code if code in LANGUAGES else "ru"
    for verbs, verb in zip(VERBS[code], _CANONICAL_VERBS):
        for objects, obj in zip(OBJECTS[code], _CANONICAL_OBJECTS):
            if re.fullmatch(r"(?:" + verbs + r")\s+(?:" + _ARTICLES[code] + r"\s*)?(?:" + objects + r")", value, re.I):
                return verb + " " + obj
    return ""


def looks_like_new_command(text: str, code: str, *, cancel_orders: bool = False) -> bool:
    value = strip_request_shell(text, code)
    from jarvis.runtime.command_repair160 import WORDS
    commands = "|".join(VERBS.get(code, VERBS["ru"])) + "|" + "|".join(re.escape(w) for w in WORDS.get(code, "").split())
    if cancel_orders:
        # NEO 187: in «Не открывай Steam, отмени таймер чай» the second clause is
        # a real order. An answer to an open question («cancel shutdown» as an
        # application name) keeps the NEO 185 rule and stays the answer.
        commands += "|" + _CANCEL_VERBS187.get(code, _CANCEL_VERBS187["ru"])
    return bool(re.match(r"^(?:" + commands + r")(?:\s|$)", value, re.I))


_CANCEL_VERBS187 = {
    "ru": r"отмени(?:те)?|сбрось(?:те)?", "en": r"cancel", "de": r"brich|breche",
    "fr": r"annule(?:z)?", "es": r"cancela", "it": r"annulla", "pt": r"cancela|cancele",
}


def locale_message(key: str, code: str) -> str:
    messages = {
        "clarify": ("Не совсем понял. Подскажи, что сделать и с чем?", "I’m not sure what you need. What should I do, and with what?", "Ich bin nicht ganz sicher. Was soll ich womit machen?", "Je n’ai pas bien compris. Que dois-je faire, et avec quoi ?", "No lo he entendido del todo. ¿Qué quieres que haga y con qué?", "Non ho capito bene. Cosa devo fare e con cosa?", "Não entendi bem. O que devo fazer e com quê?"),
        "target": ("Уточни название или путь. С каким объектом работаем?", "Which item? Tell me its name or path.", "Welches Objekt? Nenne mir den Namen oder Pfad.", "Quel élément ? Donne-moi son nom ou son chemin.", "¿Qué elemento? Dime su nombre o su ruta.", "Quale elemento? Dimmi il nome o il percorso.", "Qual item? Diz o nome ou o caminho."),
        "negated": ("Хорошо, это действие не выполняю.", "Okay, I won’t do that.", "In Ordnung, ich führe diese Aktion nicht aus.", "D’accord, je n’effectue pas cette action.", "De acuerdo, no haré esa acción.", "Va bene, non eseguo questa azione.", "Certo, não vou executar essa ação."),
        "chat_unavailable": ("Поговорить сейчас не получится: локальная модель NEO (Ollama) не отвечает. Проверьте, что Ollama запущена, или запустите NEO.cmd --install-deps.", "I can’t chat right now: NEO’s local model (Ollama) isn’t responding. Check that Ollama is running, or run NEO.cmd --install-deps.", "Plaudern geht gerade nicht: NEOs lokales Modell (Ollama) antwortet nicht. Prüfe, ob Ollama läuft, oder starte NEO.cmd --install-deps.", "Je ne peux pas discuter pour l’instant : le modèle local de NEO (Ollama) ne répond pas. Vérifie qu’Ollama est lancé ou exécute NEO.cmd --install-deps.", "Ahora no puedo conversar: el modelo local de NEO (Ollama) no responde. Comprueba que Ollama está en marcha o ejecuta NEO.cmd --install-deps.", "Ora non posso chiacchierare: il modello locale di NEO (Ollama) non risponde. Controlla che Ollama sia avviato o esegui NEO.cmd --install-deps.", "Agora não consigo conversar: o modelo local do NEO (Ollama) não responde. Verifica se o Ollama está a correr ou executa NEO.cmd --install-deps."),
        "web_unavailable": ("Не удалось получить ответ через SearchAPI.io. Уточни вопрос или проверь подключение и ключ в настройках.", "I couldn’t get a SearchAPI.io answer. Rephrase the question or check the connection and API key in Settings.", "SearchAPI.io hat keine Antwort geliefert. Formuliere die Frage neu oder prüfe Verbindung und API-Schlüssel.", "Je n’ai pas obtenu de réponse de SearchAPI.io. Reformule la question ou vérifie la connexion et la clé API.", "No pude obtener una respuesta de SearchAPI.io. Reformula la pregunta o comprueba la conexión y la clave API.", "Non ho ottenuto una risposta da SearchAPI.io. Riformula la domanda o controlla la connessione e la chiave API.", "Não consegui uma resposta do SearchAPI.io. Reformula a pergunta ou verifica a ligação e a chave API."),
        "cancelled": ("Хорошо, отменил уточнение.", "Okay, I’ve cancelled that request.", "In Ordnung, die Rückfrage ist aufgehoben.", "D’accord, j’annule cette demande.", "De acuerdo, cancelo esa solicitud.", "Va bene, annullo la richiesta.", "Certo, cancelei esse pedido."),
    }
    return messages[key][LANGUAGES.index(code if code in LANGUAGES else "ru")]
