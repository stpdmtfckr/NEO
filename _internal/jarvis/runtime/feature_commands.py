"""Explicit selected-locale routes for optional desktop features."""
from __future__ import annotations
import re
from functools import lru_cache
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell, negated_action

# action ; native phrases in RU/EN/DE/FR/ES/IT/PT. No payload guessing.
ROWS = """
shelf.show;покажи книжную полку|открой полку|покажи полку;show shelf|open shelf|show bookshelf;zeige das regal|öffne das regal;montre l'étagère|ouvre l'étagère;muestra la estantería|abre la estantería;mostra lo scaffale|apri lo scaffale;mostra a estante|abre a estante
shelf.hide;скрой книжную полку|скрой полку|закрой полку;hide shelf|close shelf;verberge das regal|schließe das regal;masque l'étagère|ferme l'étagère;oculta la estantería|cierra la estantería;nascondi lo scaffale|chiudi lo scaffale;oculta a estante|fecha a estante
shelf.list;что на полке|покажи содержимое полки;list shelf items|what is on my shelf;liste die regalinhalte auf|was ist im regal;liste le contenu de l'étagère;lista el contenido de la estantería;elenca il contenuto dello scaffale;lista o conteúdo da estante
downloads.list;покажи загрузки|история загрузок|что скачивается;show downloads|download history|what is downloading;zeige downloads|downloadverlauf;montre les téléchargements|historique des téléchargements;muestra las descargas|historial de descargas;mostra i download|cronologia download;mostra os downloads|histórico de downloads
downloads.status;статус загрузок|проверь загрузки;download status|check downloads;downloadstatus|prüfe downloads;état des téléchargements;estado de las descargas;stato dei download;estado dos downloads
activity.list;покажи историю активности|что я делал;show activity history|what was i doing;zeige den aktivitätsverlauf;montre l'historique d'activité;muestra el historial de actividad;mostra la cronologia attività;mostra o histórico de atividade
activity.pause;приостанови историю активности|приостанови журнал активности;pause activity history|pause activity recording;pausiere den aktivitätsverlauf;mets l'historique d'activité en pause;pausa el historial de actividad;metti in pausa la cronologia attività;pausa o histórico de atividade
activity.resume;продолжи историю активности|возобнови журнал активности;resume activity history|resume activity recording;setze den aktivitätsverlauf fort;reprends l'historique d'activité;reanuda el historial de actividad;riprendi la cronologia attività;retoma o histórico de atividade
activity.clear;очисти историю активности;clear activity history;lösche den aktivitätsverlauf;efface l'historique d'activité;borra el historial de actividad;cancella la cronologia attività;apaga o histórico de atividade
screen.analyze;что на экране|что сейчас на экране|что у меня на экране;what is on the screen|what's on the screen|what is on my screen|what's on my screen|what is on screen;was ist auf dem bildschirm|was ist auf meinem bildschirm;qu'est-ce qu'il y a à l'écran|qu'y a-t-il à l'écran|qu'est-ce qui est à l'écran;qué hay en la pantalla|qué hay en mi pantalla;cosa c'è sullo schermo|che cosa c'è sullo schermo|cosa c'è sul mio schermo;o que está no ecrã|o que há no ecrã|o que está na tela|o que há na tela
mini_browser.close;закрой мини браузер|скрой мини браузер;close mini browser|hide mini browser;schließe den mini browser;ferme le mini navigateur;cierra el mini navegador;chiudi il mini browser;fecha o mini navegador
""".strip()


@lru_cache(maxsize=7)
def exact_phrases(code: str) -> dict[str, tuple[str, str]]:
    index = LANGUAGES.index(code if code in LANGUAGES else "ru")
    result = {}
    for line in ROWS.splitlines():
        columns = line.split(";")
        tool, action = columns[0].split(".", 1)
        for phrase in columns[index + 1].split("|"):
            result[norm(phrase)] = (tool, action)
    return result


_SLOTS = {
    "shelf.create": (
        r"(?:добавь|сохрани) (?:заметку|карточку) на полку (?P<title>[^:]+):\s*(?P<text>.+)",
        r"(?:add|save) (?:a )?(?:note|card) to (?:the )?shelf (?P<title>[^:]+):\s*(?P<text>.+)",
        r"(?:speichere|füge) (?:eine )?notiz (?:im|zum) regal (?P<title>[^:]+):\s*(?P<text>.+)",
        r"(?:ajoute|enregistre) une note sur l'étagère (?P<title>[^:]+):\s*(?P<text>.+)",
        r"(?:añade|guarda) una nota en la estantería (?P<title>[^:]+):\s*(?P<text>.+)",
        r"(?:aggiungi|salva) una nota sullo scaffale (?P<title>[^:]+):\s*(?P<text>.+)",
        r"(?:adiciona|guarda) uma nota na estante (?P<title>[^:]+):\s*(?P<text>.+)",
    ),
    "shelf.import_file": (
        r"(?:добавь|положи|импортируй) файл на полку (?P<path>.+)", r"(?:add|import) (?:a )?file to (?:the )?shelf (?P<path>.+)",
        r"importiere (?:die )?datei ins regal (?P<path>.+)", r"importe (?:le )?fichier sur l'étagère (?P<path>.+)",
        r"importa (?:el )?archivo a la estantería (?P<path>.+)", r"importa (?:il )?file sullo scaffale (?P<path>.+)",
        r"importa (?:o )?(?:ficheiro|arquivo) para a estante (?P<path>.+)",
    ),
    "activity.search": (
        r"найди в истории активности (?P<query>.+)", r"search activity history for (?P<query>.+)",
        r"suche im aktivitätsverlauf nach (?P<query>.+)", r"cherche dans l'historique d'activité (?P<query>.+)",
        r"busca en el historial de actividad (?P<query>.+)", r"cerca nella cronologia attività (?P<query>.+)",
        r"procura no histórico de atividade (?P<query>.+)",
    ),
    "mini_browser.open": (
        r"открой (?:в )?мини[ -]браузер(?:е)? (?P<url>https?://\S+)", r"open (?:in )?(?:the )?mini browser (?P<url>https?://\S+)",
        r"öffne (?:im )?mini browser (?P<url>https?://\S+)", r"ouvre (?:dans )?le mini navigateur (?P<url>https?://\S+)",
        r"abre (?:en )?el mini navegador (?P<url>https?://\S+)", r"apri (?:nel )?mini browser (?P<url>https?://\S+)",
        r"abre (?:no )?mini navegador (?P<url>https?://\S+)",
    ),
    "game_locations.query": (
        r"где (?:лежат|хранятся|находятся) (?P<kind>сохранения|скриншоты|логи|настройки) (?:игры )?(?P<name>.+)",
        r"where are (?:the )?(?P<kind>saves|screenshots|logs|settings) (?:for |of )?(?P<name>.+)",
        r"wo sind (?:die )?(?P<kind>spielstände|screenshots|protokolle|einstellungen) (?:von )?(?P<name>.+)",
        r"où sont les (?P<kind>sauvegardes|captures|journaux|paramètres) (?:de )?(?P<name>.+)",
        r"dónde están (?:los |las )?(?P<kind>partidas guardadas|partidas|capturas|registros|ajustes) (?:de )?(?P<name>.+)",
        r"dove sono (?:i |le )?(?P<kind>salvataggi|schermate|registri|impostazioni) (?:di )?(?P<name>.+)",
        r"onde estão (?:os |as )?(?P<kind>jogos guardados|jogos salvos|partidas salvas|partidas guardadas|capturas|registos|definições) (?:de )?(?P<name>.+)",
    ),
}

_NOTE_NOUNS = (
    r"(?:заметку|заметки|заметка|записку)", r"(?:the |a )?note", r"(?:die |eine )?notiz",
    r"(?:la |une )?note", r"(?:la |una )?nota", r"(?:la |una )?nota", r"(?:a |uma )?nota",
)
_CHECKLIST_NOUNS = (
    r"(?:чек[ -]?лист|список задач)", r"(?:the |a )?checklist", r"(?:die |eine )?checkliste",
    r"(?:la |une )?(?:liste de tâches|checklist)", r"(?:la |una )?lista de tareas",
    r"(?:la |una )?lista di attività", r"(?:a |uma )?lista de tarefas",
)
_OBJECT_VERBS = {
    "read": ("прочитай|покажи", "read|show", "lies|zeige", "lis|montre", "lee|muestra", "leggi|mostra", "lê|leia|mostra|mostre"),
    "create": ("создай|добавь", "create|add", "erstelle", "crée|cree", "crea|añade", "crea|aggiungi", "cria|crie|adiciona"),
    "update": ("измени|перепиши|обнови", "edit|rewrite|update", "ändere|bearbeite", "modifie|réécris", "modifica|reescribe", "modifica|riscrivi", "altera|reescreve|edita"),
    "delete": ("удали", "delete|remove", "lösche|entferne", "supprime|efface", "elimina|borra", "elimina|cancella", "apaga|exclui|exclua"),
    "pin": ("закрепи", "pin", "hefte an|fixiere", "épingle", "fija", "fissa", "fixa|fixe"),
    "copy": ("скопируй", "copy", "kopiere", "copie", "copia", "copia", "copia|copie"),
}


@lru_cache(maxsize=7)
def _object_patterns(code: str):
    index = LANGUAGES.index(code if code in LANGUAGES else "ru")
    # Native separable verbs / placement suffixes are syntax, not part of a
    # note's title. Quoted literal titles still use the generic rule below.
    specific = {
        'en': [('pin','note',r'pin (?:the |a )?note (?P<title>.+?) to the desktop')],
        'de': [('read','note',r'lies (?:die |eine )?notiz (?P<title>.+?) vor'),
               ('pin','note',r'fixiere (?:die |eine )?notiz (?P<title>.+?) auf dem desktop')],
        'es': [('create','note',r'añade una nota nueva (?P<title>[^:]+?) a la estantería\s*:\s*(?P<content>.*)')],
        'it': [('pin','note',r'fissa (?:la |una )?nota (?P<title>.+?) sul desktop')],
    }
    specific.setdefault('fr',[]).append(('update','note',r'(?:réécris|modifie) (?:la |une )?note (?P<title>.+?) ainsi\s*:\s*(?P<content>.+)'))
    specific.setdefault('es',[]).append(('update','note',r'(?:reescribe|modifica) (?:la |una )?nota (?P<title>.+?) así:\s*(?P<content>.+)'))
    specific.setdefault('it',[]).append(('update','note',r'(?:riscrivi|modifica) (?:la |una )?nota (?P<title>.+?) così:\s*(?P<content>.+)'))
    specific.setdefault('en',[]).append(('update','note',r'(?:rewrite|update|edit) (?:the |a )?note (?P<title>.+?) as:\s*(?P<content>.+)'))
    specific.setdefault('ru',[]).append(('update','note',r'(?:перепиши|обнови|измени) заметку (?P<title>.+?) следующим текстом:\s*(?P<content>.+)'))
    shelf_delete = {
        'en':r'(?:delete|remove) (?:the |a )?note (?P<title>.+?) from the shelf',
        'de':r'(?:lösche|entferne) (?:die |eine )?notiz (?P<title>.+?) aus (?:dem regal|der ablage)',
        'fr':r"(?:supprime|efface) (?:la |une )?note (?P<title>.+?) de l['’]étagère",
        'es':r'(?:elimina|borra) (?:la |una )?nota (?P<title>.+?) de la estantería',
        'it':r'(?:elimina|cancella) (?:la |una )?nota (?P<title>.+?) dallo scaffale',
        'pt':r'(?:apaga|exclua|exclui) (?:a |uma )?nota (?P<title>.+?) da estante',
    }
    if code in shelf_delete:specific.setdefault(code,[]).append(('delete','note',shelf_delete[code]))
    result = [(action,kind,re.compile(pattern,re.I|re.S)) for action,kind,pattern in specific.get(code,[])]
    for action, verbs in _OBJECT_VERBS.items():
        for kind, nouns in (("note", _NOTE_NOUNS), ("checklist", _CHECKLIST_NOUNS)):
            content = r"(?:\s*:\s*(?P<content>.*))?" if action == "create" else r"\s*:\s*(?P<content>.+)" if action == "update" else ""
            pattern = re.compile(r"(?:" + verbs[index] + r")\s+" + nouns[index] + r"\s+(?P<title>[^:\r\n]+?)" + content, re.I | re.S)
            result.append((action, kind, pattern))
    return tuple(result)


_CHECK_PATTERNS = (
    r"(?P<verb>отметь|сними отметку) пункт (?P<index>\d+) (?:в )?(?:чек[ -]?листе|списке задач) (?P<title>.+)",
    r"(?P<verb>check|uncheck) item (?P<index>\d+) in (?:the )?checklist (?P<title>.+)",
    r"(?P<verb>markiere|demarkiere) punkt (?P<index>\d+) in (?:der )?checkliste (?P<title>.+)",
    r"(?P<verb>coche|décoche) (?:l')?élément (?P<index>\d+) (?:de|dans) la liste de tâches (?P<title>.+)",
    r"(?P<verb>marca|desmarca) (?:el )?elemento (?P<index>\d+) (?:de|en) la lista de tareas (?P<title>.+)",
    r"(?P<verb>seleziona|deseleziona) (?:l')?elemento (?P<index>\d+) nella lista di attività (?P<title>.+)",
    r"(?P<verb>marca|desmarca) (?:o )?item (?P<index>\d+) na lista de tarefas (?P<title>.+)",
)

_GAME_KIND_WORDS = {
    "saves": ("сохранения|сохранений", "saves|save folder", "Spielstände|Speicherstände", "sauvegardes", "partidas guardadas|partidas|guardados", "salvataggi", "jogos guardados|jogos salvos|partidas salvas|gravações"),
    "config": ("настройки|настроек|конфиги", "settings|configuration", "Einstellungen|Konfiguration", "paramètres|configuration", "ajustes|configuración", "impostazioni|configurazione", "definições|configuração"),
    "screenshots": ("скриншоты|скриншотов", "screenshots", "Screenshots|Bildschirmfotos", "captures", "capturas", "schermate", "capturas"),
    "videos": ("видеозаписи|видеозаписей|записи игры", "recordings|videos", "Aufnahmen|Videos", "enregistrements|vidéos", "grabaciones|vídeos", "registrazioni|video", "gravações de vídeo|vídeos"),
    "install": ("папку установки|папка установки|каталог установки", "installation folder|install folder", "Installationsordner", "dossier d'installation", "carpeta de instalación", "cartella di installazione", "pasta de instalação"),
    "exe": ("исполняемый файл|exe файл", "executable|exe file", "ausführbare Datei|EXE Datei", "exécutable|fichier exécutable", "ejecutable|archivo ejecutable", "eseguibile|file eseguibile", "executável|ficheiro executável"),
    "mods": ("моды|модов|папку модов", "mods|mod folder", "Mods|Modordner", "mods|dossier des mods", "mods|carpeta de mods", "mod|cartella delle mod", "mods|pasta dos mods"),
    "logs": ("логи|логов|журнал игры", "logs|log folder", "Protokolle|Logs", "journaux|logs", "registros|logs", "registri|log", "registos|logs"),
}
_GAME_PREFIXES = {
    "find": (r"где (?:лежат|хранятся|находятся|находится)", r"where (?:is|are)(?: the)?", r"wo (?:ist|sind)(?: (?:die|der|das))?", r"où (?:est|sont)(?: (?:le|la|les))?", r"dónde (?:está|están)(?: (?:el|la|los|las))?", r"dove (?:è|sono)(?: (?:il|la|i|le))?", r"onde (?:está|estão)(?: (?:o|a|os|as))?"),
    "open": (r"открой", r"open(?: the)?", r"öffne(?: (?:die|der|das|den))?", r"ouvre(?: (?:le|la|les))?", r"abre(?: (?:el|la|los|las))?", r"apri(?: (?:il|la|i|le))?", r"abre(?: (?:o|a|os|as))?"),
}
_GAME_JOIN = (r"(?:игры\s+)?", r"(?:(?:for|of)\s+)?", r"(?:von\s+)?", r"(?:de\s+)?", r"(?:de\s+)?", r"(?:di\s+)?", r"(?:de\s+)?")


@lru_cache(maxsize=7)
def _game_patterns(code: str):
    index = LANGUAGES.index(code if code in LANGUAGES else "ru")
    return tuple((action, kind, re.compile(prefixes[index] + r"\s+(?:" + words[index] + r")\s+" + _GAME_JOIN[index] + r"(?P<name>.+)", re.I)) for action, prefixes in _GAME_PREFIXES.items() for kind, words in _GAME_KIND_WORDS.items())


_MINI_FILE_PATTERNS = (
    r"открой (?:файл|изображение|картинку) в мини[ -]браузере (?P<path>.+)",
    r"open (?:file|image) in (?:the )?mini browser (?P<path>.+)",
    r"öffne (?:die )?(?:Datei|das Bild) im mini browser (?P<path>.+)",
    r"ouvre (?:le fichier|l'image) dans le mini navigateur (?P<path>.+)",
    r"abre (?:el archivo|la imagen) en el mini navegador (?P<path>.+)",
    r"apri (?:il file|l'immagine) nel mini browser (?P<path>.+)",
    r"abre (?:o ficheiro|o arquivo|a imagem) no mini navegador (?P<path>.+)",
)

_IMAGE_CONTROLS = {
    "close": ("закрой изображение|закрой картинку", "close (?:the )?image", "schließe das bild", "ferme l['’]image", "cierra la imagen", "chiudi l['’]immagine", "fecha a imagem"),
    "pin": ("закрепи изображение|закрепи картинку", "pin (?:the )?image", "fixiere das bild", "épingle l['’]image", "fija la imagen", "fissa l['’]immagine", "fixa a imagem"),
    "unpin": ("открепи изображение|открепи картинку", "unpin (?:the )?image", "löse die fixierung des bildes", "désépingle l['’]image", "desfija la imagen", "sblocca l['’]immagine", "desfixa a imagem"),
    "zoom": (r"(?:установи )?масштаб изображения (?P<zoom>\d+(?:[.,]\d+)?) (?:процентов|процента|%)", r"set (?:the )?image zoom to (?P<zoom>\d+(?:[.,]\d+)?) (?:percent|%)", r"setze die bildvergrößerung auf (?P<zoom>\d+(?:[.,]\d+)?) (?:prozent|%)", r"règle le zoom de l['’]image à (?P<zoom>\d+(?:[.,]\d+)?) (?:pour cent|%)", r"ajusta el zoom de la imagen al (?P<zoom>\d+(?:[.,]\d+)?) (?:por ciento|%)", r"imposta lo zoom dell['’]immagine al (?P<zoom>\d+(?:[.,]\d+)?) (?:per cento|%)", r"define o zoom da imagem para (?P<zoom>\d+(?:[.,]\d+)?) (?:por cento|%)"),
}

_SHELF_NOUNS = (
    r"(?:(?:мою|мой|моя)\s+)?(?:книжн(?:ую|ая)\s+полк[ау]|книжный\s+шкаф|полк[ау])",
    r"(?:(?:the|my)\s+)?(?:bookshelf|book\s+shelf|bookcase|book\s+cabinet|shelf)",
    r"(?:(?:das|den|mein|meinen)\s+)?(?:Bücherregal|Bücherschrank|Regal)",
    r"(?:(?:la|ma)\s+|l['’])?(?:bibliothèque|étagère(?:\s+à\s+livres)?)",
    r"(?:(?:la|el|mi)\s+)?(?:estantería|librería|estante)",
    r"(?:(?:la|lo|il|mia|mio)\s+)?(?:libreria|scaffale)",
    r"(?:(?:a|minha)\s+)?estante(?:\s+de\s+livros)?",
)
_SHELF_VERBS = {
    "show": (r"открой|открыть|покажи|показать|разверни|развернуть", r"open|show|display", r"öffne|öffnen|zeige|zeigen", r"ouvre|ouvrir|montre|montrer|affiche|afficher", r"abre|abrir|muestra|mostrar", r"apri|aprire|mostra|mostrare", r"abre|abra|abrir|mostra|mostre|mostrar"),
    "hide": (r"закрой|закрыть|скрой|скрыть|сверни|свернуть|убери|убрать", r"close|hide|dismiss", r"schließe|schließen|verberge|verbergen", r"ferme|fermer|masque|masquer|cache|cacher", r"cierra|cerrar|oculta|ocultar", r"chiudi|chiudere|nascondi|nascondere", r"fecha|feche|fechar|oculta|oculte|ocultar"),
}

@lru_cache(maxsize=7)
def _slot_patterns(code: str):
    index = LANGUAGES.index(code if code in LANGUAGES else "ru")
    return tuple((name.split(".", 1), re.compile(rows[index], re.I | re.S)) for name, rows in _SLOTS.items())


def parse_feature_command(text: str, code: str) -> tuple[str, dict] | None:
    from jarvis.runtime.streaming_commands184 import parse_obs_command
    obs = parse_obs_command(text, code)
    if obs is not None:
        return obs
    from jarvis.runtime.utterance_purpose import strip_assistant_address
    value = strip_request_shell(strip_assistant_address(text), code)
    # NEO 188 NEO-PRIVACY-001: «больше не ищи в папке X» is a revocation, checked before negation.
    from jarvis.runtime.knowledge_phrases188 import parse as parse_revoke188
    revoke188 = parse_revoke188(value, code)
    if revoke188 is not None:
        return revoke188
    from jarvis.runtime.document_scope188 import parse as parse_document188
    document188 = parse_document188(text, code)
    if document188 is not None:
        return document188
    from jarvis.runtime.office_formula188 import parse as parse_formula188
    formula188 = parse_formula188(text, code)
    if formula188 is not None:
        return formula188
    from jarvis.runtime.office_chart188 import parse as parse_chart188
    chart188 = parse_chart188(text, code)
    if chart188 is not None:
        return chart188
    from jarvis.runtime.source_inspect188 import parse as parse_source188
    source188 = parse_source188(text, code)
    if source188 is not None:
        return source188
    if negated_action(value, code):
        return None
    # Resolve the complete native briefing request before generic imperative
    # normalization can interpret "show ..." as opening an application.
    from jarvis.runtime.integration_phrases import _briefing_request, _n
    if _briefing_request(_n(value), code):
        return 'briefing', {'action': 'summary'}
    from jarvis.runtime.location_requests158 import parse_location_request
    location = parse_location_request(value, code)
    if location is not None:
        return location
    from jarvis.runtime.file_requests162 import parse_file_request
    file_request = parse_file_request(value, code)
    if file_request is not None:
        return file_request
    if code == 'fr':
        site = re.fullmatch(r'affiche le site (?P<url>https?://\S+) dans le mini[ -]navigateur',value,re.I)
        if site:return 'mini_browser',{'action':'open','url':site['url']}
    # A note-to-clipboard instruction owns a shelf object, not arbitrary dictation.
    if code == 'en':
        note_copy = re.fullmatch(r'put (?:the |a )?note (?P<title>.+?) on the clipboard', value, re.I)
        if note_copy:
            return 'shelf', {'action':'copy','kind':'note','title':note_copy['title'].strip(' "«»')}
    from jarvis.runtime.command_contracts150 import complete
    reviewed = complete(value, code)
    if reviewed is not None:
        return reviewed
    index = LANGUAGES.index(code if code in LANGUAGES else "ru")
    weather_chat = (
        r"(?:как дела[,? ]+)?(?:а )?(?:какая погода|как там погода|что с погодой)(?: сегодня)?",
        r"(?:how are you[,? ]+)?(?:and )?(?:what is the weather like|how is the weather)(?: today)?",
        r"(?:wie geht es dir[,? ]+)?(?:und )?wie ist das wetter(?: heute)?",
        r"(?:comment ça va[,? ]+)?(?:et )?quel temps fait-il(?: aujourd'hui)?",
        r"(?:cómo estás[,? ]+)?(?:y )?qué tiempo hace(?: hoy)?",
        r"(?:come stai[,? ]+)?(?:e )?che tempo fa(?: oggi)?",
        r"(?:como estás[,? ]+)?(?:e )?(?:como está o tempo|que tempo (?:faz|está|vai fazer))(?: hoje)?",
    )
    if re.fullmatch(weather_chat[index], value.strip(" .!?¿¡"), re.I):
        return "weather", {"action": "summary"}
    from jarvis.runtime.duration import parse_timer_command
    timer = parse_timer_command(value, code)
    if timer is not None:
        return timer
    from jarvis.runtime.volume188 import parse as parse_volume188
    volume188 = parse_volume188(value, code)
    if volume188 is not None:
        return volume188
    from jarvis.runtime.command_contracts149 import match_readonly, parse_music_request, match_literal_transport
    state_request = match_readonly(value, code)
    if state_request is not None:
        return state_request
    transport = match_literal_transport(value, code)
    if transport is not None:
        return transport
    musical = parse_music_request(value, code)
    if musical is not None and musical[1].get("query"):
        from jarvis.runtime.canonical_commands import _MODE_WORD186
        if not _MODE_WORD186.fullmatch(str(musical[1].get("query", "")).strip()):
            return musical
    from jarvis.routing.intent_core import match_assistive_group
    group = match_assistive_group(value, code)
    if group is not None:
        return group
    for action, verbs in _SHELF_VERBS.items():
        if re.fullmatch(r"(?:" + verbs[index] + r")\s+" + _SHELF_NOUNS[index], value.strip(" .!?"), re.I):
            return "shelf", {"action": action}
        if code == "de" and re.fullmatch(_SHELF_NOUNS[index] + r"\s+(?:" + verbs[index] + r")", value.strip(" .!?"), re.I):
            return "shelf", {"action": action}
    exact = exact_phrases(code).get(norm(value))
    if exact:
        return exact[0], {"action": exact[1]}
    index = LANGUAGES.index(code if code in LANGUAGES else "ru")
    for action, patterns in _IMAGE_CONTROLS.items():
        match = re.fullmatch(patterns[index], value, re.I)
        if match:
            args = {"action": "pin" if action == "unpin" else action, "id": "image"}
            if action in {"pin", "unpin"}:
                args["pinned"] = action == "pin"
            elif action == "zoom":
                zoom = float(match.group("zoom").replace(",", "."))
                if zoom <= 0:
                    return None
                args["zoom"] = zoom
            return "mini_browser", args
    local_file = re.fullmatch(_MINI_FILE_PATTERNS[index], value, re.I)
    if local_file:
        return "mini_browser", {"action": "open", "path": local_file.group("path").strip(' "«»')}
    for action, kind, pattern in _game_patterns(code):
        match = pattern.fullmatch(value)
        if match:
            return "game_locations", {"action": action, "name": match.group("name").strip(' "«»'), "kind": kind}
    check_value = value
    if code == 'de' and re.search(r'\s+als erledigt$', check_value, re.I):
        check_value = re.sub(r'\s+als erledigt$', '', check_value, flags=re.I)
    check = re.fullmatch(_CHECK_PATTERNS[index], check_value, re.I)
    if check:
        if int(check.group("index")) < 1:
            return None
        checked = norm(check.group("verb")) not in {"сними отметку", "uncheck", "demarkiere", "décoche", "desmarca", "deseleziona"}
        return "shelf", {"action": "check", "title": check.group("title").strip(' "«»'), "index": int(check.group("index")) - 1, "checked": checked}
    for (tool, action), pattern in _slot_patterns(code):
        match = pattern.fullmatch(value.strip())
        if match:
            args = {name: val.strip(' "«»') for name, val in match.groupdict().items()}
            if tool == "game_locations":
                kind = norm(args["kind"])
                kinds = {
                    "saves": "сохранения saves spielstände sauvegardes partidas guardadas salvataggi jogos guardados jogos salvos partidas salvas",
                    "screenshots": "скриншоты screenshots captures capturas schermate",
                    "logs": "логи logs protokolle journaux registros registri registos",
                    "settings": "настройки settings einstellungen paramètres ajustes impostazioni definições",
                }
                args["kind"] = next((key for key, words in kinds.items() if kind in words), "saves")
            return tool, {"action": action, **args}
    for action, kind, pattern in _object_patterns(code):
        match = pattern.fullmatch(value)
        if match:
            args = {"action": action, "kind": kind, "title": match.group("title").strip(' "«»')}
            content = match.groupdict().get("content")
            if content is not None:
                if kind == "checklist":
                    args["items"] = [part.strip() for part in content.split(";") if part.strip()]
                else:
                    args["content"] = content
            return "shelf", args
    return None
