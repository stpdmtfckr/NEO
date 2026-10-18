"""NEO 188 NEO-UX-002 / TZ-PENDING-FIELD-EXPLANATION-01.

While NEO waits for one field («Укажи полный путь…»), a question about THAT
field («Что такое полный путь?», «что это значит?», «в каком формате?»,
«например?») gets a short explanation with a clearly conditional example,
followed by the same question built from the same pending object.

The explanation answers no slot, confirms nothing, starts nothing, does not
touch the TTL and leaves the pending object untouched. A question about
something else («что такое квантовая физика?») is a change of topic and keeps
the ordinary rules. Text slots (note text, message body) never use this path:
their next sentence is literal data.
"""
from __future__ import annotations

import re
from functools import lru_cache

from jarvis.runtime.natural_commands import LANGUAGES, norm

# A meaning/format question shell. Group «rest» holds what is asked about.
_ASK = {
    'ru': r'(?:а\s+)?(?:что\s+(?:такое|значит|означает|это\s+(?:значит|такое)|ты\s+имеешь\s+в\s+виду(?:\s+под)?|имеется\s+в\s+виду(?:\s+под)?)'
          r'|в\s+каком\s+(?:формате|виде)|как\s+(?:его\s+|это\s+|ее\s+)?(?:указать|написать|записать|назвать|узнать|найти)'
          r'|где\s+(?:его\s+|это\s+)?(?:взять|найти|посмотреть)|(?:приведи|дай|покажи)\s+пример|например|пример|какой\s+пример|это\s+как)',
    'en': r'(?:what\s+(?:does|do)\s+(?:that|this|it|you)\s+mean|what\s+(?:is|does|do\s+you\s+mean(?:\s+by)?|is\s+meant\s+by|format)|what\'s|whats|which\s+format|how\s+do\s+i\s+(?:write|enter|find|give)'
          r'|where\s+do\s+i\s+find|(?:give\s+me\s+an?|an?|for)\s+example|example|meaning\s+of|like\s+what)',
    'de': r'(?:was\s+(?:heißt|heisst|bedeutet)\s+das|was\s+(?:ist|bedeutet|heißt|heisst|meinst\s+du(?:\s+mit)?)|in\s+welchem\s+format|wie\s+(?:gebe\s+ich|schreibe\s+ich|finde\s+ich)'
          r'|wo\s+finde\s+ich|zum\s+beispiel|beispiel|ein\s+beispiel)',
    'fr': r'(?:qu\'est-ce\s+que\s+(?:ça|ca|cela)\s+veut\s+dire|(?:ça|ca)\s+veut\s+dire\s+quoi|qu\'est-ce\s+(?:que|qu\')|c\'est\s+quoi|que\s+(?:veut\s+dire|signifie)|qu\'entends-tu\s+par|quel\s+format|dans\s+quel\s+format'
          r'|comment\s+(?:je\s+)?(?:l\'écris|l\'ecris|le\s+trouver|l\'indiquer)|où\s+(?:le\s+)?trouver|par\s+exemple|un\s+exemple|exemple)',
    'es': r'(?:qué\s+(?:es|significa|quieres\s+decir(?:\s+con)?)|que\s+(?:es|significa)|en\s+qué\s+formato|qué\s+formato|cómo\s+(?:lo\s+)?(?:escribo|indico|encuentro)'
          r'|dónde\s+(?:lo\s+)?encuentro|por\s+ejemplo|un\s+ejemplo|ejemplo)',
    'it': r'(?:che\s+significa|cosa\s+vuol\s+dire|cos\'è|cosa\s+(?:è|significa|intendi(?:\s+per)?)|che\s+cos\'è|in\s+che\s+formato|che\s+formato|come\s+(?:lo\s+)?(?:scrivo|indico|trovo)'
          r'|dove\s+(?:lo\s+)?trovo|per\s+esempio|un\s+esempio|esempio)',
    'pt': r'(?:o\s+que\s+(?:isso|isto)\s+(?:significa|quer\s+dizer)|o\s+que\s+(?:é|significa|queres\s+dizer(?:\s+com)?|quer\s+dizer)|que\s+formato|em\s+que\s+formato|como\s+(?:o\s+)?(?:escrevo|indico|encontro)'
          r'|onde\s+(?:o\s+)?encontro|por\s+exemplo|um\s+exemplo|exemplo)',
}
# Words that name the open field. A question naming none of them is about the
# field only when it is deictic and short («что это значит?», «например?»).
_TERMS = {
    'path': ('путь', 'адрес', 'папк', 'path', 'directory', 'folder', 'pfad', 'ordner', 'chemin', 'dossier', 'ruta', 'carpeta',
             'percorso', 'cartella', 'caminho', 'pasta'),
    'filename': ('имя файла', 'название файла', 'расширени', 'filename', 'file name', 'extension', 'dateiname', 'endung',
                 'nom de fichier', 'nom du fichier', 'nombre de archivo', 'nombre del archivo', 'extensión', 'nome del file',
                 'estensione', 'nome do ficheiro', 'nome do arquivo', 'extensão'),
    'duration': ('длительност', 'время', 'срок', 'duration', 'time', 'dauer', 'zeit', 'durée', 'duree', 'temps', 'duración',
                 'tiempo', 'durata', 'tempo', 'duração'),
    'unit188': ('единиц', 'секунд', 'минут', 'час', 'unit', 'einheit', 'unité', 'unidad', 'unità', 'unidade'),
    'percent': ('громкост', 'процент', 'уровен', 'volume', 'percent', 'level', 'lautstärke', 'prozent', 'niveau', 'pour cent',
                'volumen', 'nivel', 'por ciento', 'livello', 'per cento', 'nível', 'por cento'),
    'url': ('ссылк', 'адрес сайта', 'url', 'link', 'web address', 'adresse', 'lien', 'enlace', 'dirección', 'collegamento',
            'indirizzo', 'ligação', 'endereço'),
    'email': ('почт', 'email', 'e-mail', 'mail', 'courriel', 'correo', 'posta', 'correio'),
    'clock': ('время', 'час', 'time', 'uhrzeit', 'heure', 'hora', 'ora', 'orario'),
    'choice': ('вариант', 'номер', 'option', 'number', 'nummer', 'numéro', 'opción', 'número', 'opzione', 'numero', 'opção'),
    'name': ('название', 'имя', 'приложени', 'программ', 'name', 'app', 'application', 'program', 'anwendung', 'programm', 'nom',
             'logiciel', 'nombre', 'aplicación', 'programa', 'nome', 'applicazione', 'aplicação', 'aplicativo'),
    'query': ('трек', 'песн', 'исполнител', 'запрос', 'track', 'song', 'artist', 'query', 'titel', 'lied', 'künstler', 'chanson',
              'morceau', 'artiste', 'canción', 'pista', 'artista', 'canzone', 'brano', 'música', 'faixa'),
    'number': ('число', 'значени', 'number', 'value', 'zahl', 'wert', 'nombre', 'valeur', 'número', 'valor', 'numero', 'valore'),
    'bool': ('включ', 'выключ', 'on', 'off', 'ein', 'aus', 'activer', 'désactiver', 'activar', 'desactivar', 'attivare', 'ativar'),
}
_ORDER = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_HELP = {
    'path': ('Полный путь — это адрес файла или папки целиком, начиная с диска. Условный пример: C:\\Users\\Имя\\Documents\\отчёт.docx.',
             'A full path is the whole address of a file or folder, starting with the drive. Example only: C:\\Users\\Name\\Documents\\report.docx.',
             'Ein vollständiger Pfad ist die ganze Adresse einer Datei oder eines Ordners, beginnend mit dem Laufwerk. Nur ein Beispiel: C:\\Users\\Name\\Dokumente\\bericht.docx.',
             'Un chemin complet est l’adresse entière d’un fichier ou d’un dossier, à partir du lecteur. Exemple seulement : C:\\Users\\Nom\\Documents\\rapport.docx.',
             'Una ruta completa es la dirección entera de un archivo o carpeta, empezando por la unidad. Solo un ejemplo: C:\\Users\\Nombre\\Documentos\\informe.docx.',
             'Un percorso completo è l’indirizzo intero di un file o di una cartella, a partire dall’unità. Solo un esempio: C:\\Users\\Nome\\Documenti\\relazione.docx.',
             'Um caminho completo é o endereço inteiro de um ficheiro ou pasta, a começar pela unidade. Apenas um exemplo: C:\\Users\\Nome\\Documentos\\relatorio.docx.'),
    'filename': ('Имя файла — только название без папки, вместе с расширением. Условный пример: отчёт.docx. Символы < > : " / \\ | ? * нельзя.',
                 'A file name is just the name without the folder, including the extension. Example only: report.docx. The characters < > : " / \\ | ? * are not allowed.',
                 'Ein Dateiname ist nur der Name ohne Ordner, mit Endung. Nur ein Beispiel: bericht.docx. Die Zeichen < > : " / \\ | ? * sind nicht erlaubt.',
                 'Un nom de fichier est seulement le nom sans le dossier, avec l’extension. Exemple seulement : rapport.docx. Les caractères < > : " / \\ | ? * sont interdits.',
                 'Un nombre de archivo es solo el nombre sin la carpeta, con la extensión. Solo un ejemplo: informe.docx. No se permiten los caracteres < > : " / \\ | ? *.',
                 'Un nome di file è solo il nome senza cartella, con l’estensione. Solo un esempio: relazione.docx. I caratteri < > : " / \\ | ? * non sono ammessi.',
                 'Um nome de ficheiro é só o nome sem a pasta, com a extensão. Apenas um exemplo: relatorio.docx. Os caracteres < > : " / \\ | ? * não são permitidos.'),
    'duration': ('Длительность — сколько ждать: число и единица. Условный пример: «15 минут» или «1 час 20 минут».',
                 'The duration is how long to wait: a number and a unit. Example only: “15 minutes” or “1 hour 20 minutes”.',
                 'Die Dauer ist, wie lange gewartet wird: Zahl und Einheit. Nur ein Beispiel: „15 Minuten“ oder „1 Stunde 20 Minuten“.',
                 'La durée, c’est combien de temps attendre : un nombre et une unité. Exemple seulement : « 15 minutes » ou « 1 heure 20 minutes ».',
                 'La duración es cuánto esperar: un número y una unidad. Solo un ejemplo: «15 minutos» o «1 hora 20 minutos».',
                 'La durata è quanto aspettare: un numero e un’unità. Solo un esempio: «15 minuti» o «1 ora 20 minuti».',
                 'A duração é quanto tempo esperar: um número e uma unidade. Apenas um exemplo: «15 minutos» ou «1 hora 20 minutos».'),
    'unit188': ('Единица — это секунды, минуты или часы: скажите одно слово, например (условно) «минут».',
                'The unit is seconds, minutes or hours: say one word, for example (only an example) “minutes”.',
                'Die Einheit ist Sekunden, Minuten oder Stunden: sag ein Wort, zum Beispiel (nur ein Beispiel) „Minuten“.',
                'L’unité, ce sont des secondes, des minutes ou des heures : dites un mot, par exemple (seulement un exemple) « minutes ».',
                'La unidad son segundos, minutos u horas: di una palabra, por ejemplo (solo un ejemplo) «minutos».',
                'L’unità sono secondi, minuti o ore: di’ una parola, per esempio (solo un esempio) «minuti».',
                'A unidade são segundos, minutos ou horas: diz uma palavra, por exemplo (apenas um exemplo) «minutos».'),
    'percent': ('Это число от 0 до 100: 0 — минимум, 100 — максимум. Условный пример: 40.',
                'It is a number from 0 to 100: 0 is the minimum, 100 the maximum. Example only: 40.',
                'Eine Zahl von 0 bis 100: 0 ist das Minimum, 100 das Maximum. Nur ein Beispiel: 40.',
                'C’est un nombre de 0 à 100 : 0 est le minimum, 100 le maximum. Exemple seulement : 40.',
                'Es un número de 0 a 100: 0 es el mínimo y 100 el máximo. Solo un ejemplo: 40.',
                'È un numero da 0 a 100: 0 è il minimo, 100 il massimo. Solo un esempio: 40.',
                'É um número de 0 a 100: 0 é o mínimo e 100 o máximo. Apenas um exemplo: 40.'),
    'url': ('Ссылка — полный адрес страницы, он начинается с https://. Условный пример: https://example.com.',
            'A link is the full page address and starts with https://. Example only: https://example.com.',
            'Ein Link ist die vollständige Seitenadresse und beginnt mit https://. Nur ein Beispiel: https://example.com.',
            'Un lien est l’adresse complète de la page et commence par https://. Exemple seulement : https://example.com.',
            'Un enlace es la dirección completa de la página y empieza por https://. Solo un ejemplo: https://example.com.',
            'Un link è l’indirizzo completo della pagina e inizia con https://. Solo un esempio: https://example.com.',
            'Uma ligação é o endereço completo da página e começa por https://. Apenas um exemplo: https://example.com.'),
    'email': ('Адрес почты имеет вид имя@домен. Условный пример: name@example.com.',
              'An email address looks like name@domain. Example only: name@example.com.',
              'Eine E-Mail-Adresse sieht aus wie name@domain. Nur ein Beispiel: name@example.com.',
              'Une adresse e-mail a la forme nom@domaine. Exemple seulement : name@example.com.',
              'Una dirección de correo tiene la forma nombre@dominio. Solo un ejemplo: name@example.com.',
              'Un indirizzo email ha la forma nome@dominio. Solo un esempio: name@example.com.',
              'Um endereço de email tem a forma nome@domínio. Apenas um exemplo: name@example.com.'),
    'clock': ('Время в формате ЧЧ:ММ. Условный пример: 07:30.', 'A time as HH:MM. Example only: 07:30.',
              'Eine Uhrzeit im Format HH:MM. Nur ein Beispiel: 07:30.', 'Une heure au format HH:MM. Exemple seulement : 07:30.',
              'Una hora con formato HH:MM. Solo un ejemplo: 07:30.', 'Un orario nel formato HH:MM. Solo un esempio: 07:30.',
              'Uma hora no formato HH:MM. Apenas um exemplo: 07:30.'),
    'choice': ('Нужно выбрать один из вариантов ниже: назовите его номер или точное название.',
               'Pick one of the options below: say its number or its exact name.',
               'Wähle eine der Optionen unten: nenne ihre Nummer oder ihren genauen Namen.',
               'Choisissez une des options ci-dessous : dites son numéro ou son nom exact.',
               'Elige una de las opciones de abajo: di su número o su nombre exacto.',
               'Scegli una delle opzioni qui sotto: di’ il numero o il nome esatto.',
               'Escolhe uma das opções abaixo: diz o número ou o nome exato.'),
    'name': ('Нужно название так, как оно называется на компьютере. Условный пример: «Блокнот».',
             'I need the name as it appears on the computer. Example only: “Notepad”.',
             'Ich brauche den Namen so, wie er auf dem Computer heißt. Nur ein Beispiel: „Editor“.',
             'Il me faut le nom tel qu’il apparaît sur l’ordinateur. Exemple seulement : « Bloc-notes ».',
             'Necesito el nombre tal como aparece en el ordenador. Solo un ejemplo: «Bloc de notas».',
             'Mi serve il nome così come appare sul computer. Solo un esempio: «Blocco note».',
             'Preciso do nome tal como aparece no computador. Apenas um exemplo: «Bloco de notas».'),
    'query': ('Это то, что нужно найти: название и, при желании, исполнитель. Условный пример: «Believer Imagine Dragons».',
              'It is what to look for: a title and, optionally, the artist. Example only: “Believer Imagine Dragons”.',
              'Das ist, wonach gesucht wird: ein Titel und optional der Künstler. Nur ein Beispiel: „Believer Imagine Dragons“.',
              'C’est ce qu’il faut chercher : un titre et, si vous voulez, l’artiste. Exemple seulement : « Believer Imagine Dragons ».',
              'Es lo que hay que buscar: un título y, si quieres, el artista. Solo un ejemplo: «Believer Imagine Dragons».',
              'È ciò che va cercato: un titolo e, se vuoi, l’artista. Solo un esempio: «Believer Imagine Dragons».',
              'É o que procurar: um título e, se quiseres, o artista. Apenas um exemplo: «Believer Imagine Dragons».'),
    'number': ('Нужно одно число в допустимых пределах.', 'I need one number within the allowed range.',
               'Ich brauche eine Zahl im erlaubten Bereich.', 'Il me faut un nombre dans la plage autorisée.',
               'Necesito un número dentro del rango permitido.', 'Mi serve un numero nell’intervallo consentito.',
               'Preciso de um número dentro do intervalo permitido.'),
    'bool': ('Ответьте «включить» или «выключить».', 'Answer “on” or “off”.', 'Antworte „ein“ oder „aus“.',
             'Répondez « activer » ou « désactiver ».', 'Responde «activar» o «desactivar».', 'Rispondi «attivare» o «disattivare».',
             'Responde «ativar» ou «desativar».'),
}


def field_family(kind: str, prompt: str = '') -> str | None:
    kind, prompt = str(kind or ''), str(prompt or '')
    if kind in {'path', 'absolute_source'} or prompt in {'source', 'destination', 'file_path156', 'source_location'}:
        return 'path'
    if kind == 'filename':
        return 'filename'
    if kind == 'duration':
        return 'duration'
    if kind == 'unit188':
        return 'unit188'
    if kind in {'percent156', 'bounded_index', 'zoom160'}:
        return 'percent' if kind == 'percent156' else 'number'
    if kind == 'url':
        return 'url'
    if kind == 'email':
        return 'email'
    if kind == 'clock':
        return 'clock'
    if kind in {'choice', 'open_kind'}:
        return 'choice'
    if kind.startswith(('number157:', 'int157:')) or kind == 'positive_integer':
        return 'number'
    if kind == 'bool157':
        return 'bool'
    if kind == 'name':
        return 'query' if prompt in {'query', 'search_query', 'queue_query'} else 'name'
    return None


@lru_cache(maxsize=7)
def _ask(code: str):
    return re.compile(r'^(?:' + _ASK[code] + r')(?:(?:(?<=\')|\s+)(?P<rest>.*))?$', re.I)


def explain(text: str, code: str, kind: str, prompt: str = '') -> str | None:
    """The short explanation for a question about the open field, else None."""
    if code not in _ASK or any(mark in str(text or '') for mark in '"«»“”'):
        return None
    family = field_family(kind, prompt)
    if family is None:
        return None
    value = norm(str(text or '')).strip(' ?¿!.')
    match = _ask(code).fullmatch(value)
    if not match:
        return None
    rest = (match.group('rest') or '').strip(' ?¿!.')
    words = rest.split()
    deictic = {'это', 'этим', 'то', 'тут', 'здесь', 'под этим', 'that', 'this', 'it', 'by that', 'das', 'damit', 'ça', 'ca', 'cela',
               'eso', 'esto', 'con eso', 'questo', 'quello', 'con questo', 'isso', 'isto', 'com isso'}
    if not rest or rest in deictic:
        return _HELP[family][_ORDER.index(code)]
    if any(term in rest for term in _TERMS.get(family, ())) and len(words) <= 6:
        return _HELP[family][_ORDER.index(code)]
    return None
