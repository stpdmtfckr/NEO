"""Local application/game paths own location requests, never web search."""
from __future__ import annotations
import re

_PATTERNS = {
 'ru': (
  r'(?:где (?:находятся|лежат|хранятся)|найди|покажи|ищи) (?:все )?(?:файлы|папки|каталоги|расположение) (?:игры|приложения|программы) (?P<name>.+)',
  r'где установлен[ао]? (?:игра |приложение |программа )?(?P<name>.+)',
  r'где находится (?:игра |приложение |программа )(?P<name>.+)',
  r'(?:покажи|найди) (?:все )?папки[,]? связанные с (?:игрой|приложением|программой) (?P<name>.+)',
  r'продолжи (?:поиск|искать) (?:файлов|файлы|папок|папки) (?:игры|приложения|программы) (?P<name>.+)',
 ),
 'en': (r'(?:where (?:are|is)|find|show)(?: me)? (?:all )?(?:the )?(?:files|folders|location) (?:for|of) (?:the )?(?:game |application |app |program )(?P<name>.+)',
        r'where is (?:the )?(?:game |application |app |program )?(?P<name>.+?) installed',
        r'continue (?:searching|the search) for (?:files|folders) (?:for|of) (?:game |app |application |program )(?P<name>.+)'),
 'de': (r'(?:wo sind|finde|zeige)(?: mir)? (?:alle )?(?:die )?(?:Dateien|Ordner) (?:von|für) (?:dem Spiel |der Anwendung |dem Programm )(?P<name>.+)',
        r'wo ist (?:das Spiel |die Anwendung |das Programm )?(?P<name>.+?) installiert'),
 'fr': (r'(?:où sont|trouve|affiche)(?: tous)? (?:les )?(?:fichiers|dossiers) (?:de|du) (?:jeu |l[’\x27]application |programme )(?P<name>.+)',
        r'où est installé(?:e)? (?:le jeu |l[’\x27]application |le programme )?(?P<name>.+)'),
 'es': (r'(?:dónde están|busca|muestra)(?: todos)? (?:los |las )?(?:archivos|carpetas) (?:de|del) (?:juego |la aplicación |programa )(?P<name>.+)',
        r'dónde está instalad[oa] (?:el juego |la aplicación |el programa )?(?P<name>.+)'),
 'it': (r'(?:dove sono|trova|mostra)(?: tutti)? (?:i |le )?(?:file|cartelle) (?:di|del) (?:gioco |programma )(?P<name>.+)',
        r'dove è installat[oa] (?:il gioco |il programma |l[’\x27]applicazione )?(?P<name>.+)'),
 'pt': (r'(?:onde estão|encontra|mostra)(?: todos)? (?:os |as )?(?:arquivos|ficheiros|pastas) (?:de|do) (?:jogo |programa )(?P<name>.+)',
        r'onde está instalad[oa] (?:o jogo |o programa |a aplicação )?(?P<name>.+)'),
}


# Explicit local location questions are resolved before generic search/open
# commands. Keep the captured product name intact (including spelling/case).
_FOLDER_PATTERNS = {
 'ru': (
  r'в (?:какой|которой) (?:папке|директории|каталоге) (?:лежит|находится|установлен[ао]?) (?:программа |приложение |игра )(?P<name>.+)',
  r'где (?:лежит|находится) (?:программа |приложение |игра )(?P<name>.+)',
  r'(?:найди|покажи) (?:папку установки|установочную папку|путь к файлам|расположение) (?:игры |программы |приложения )(?P<name>.+)',
  r'(?:где (?:лежит|находится)|найди|покажи) (?:exe[ -]?файл|экзешник|исполняемый файл|exe)(?: (?:игры|программы|приложения))? (?P<name>.+)',
  r'(?:(?:покажи|найди) )?путь к (?:программе |приложению |игре )(?P<name>.+)',
  r'(?:где )?(?:папка|каталог|расположение) (?:программы |приложения |игры )(?P<name>.+)',
  r'где (?:экзешник|exe)(?: (?:программы|приложения|игры))? (?P<name>.+)',
  r'где (?:программа |приложение |игра )(?P<name>.+)',
 ),
 'en': (
  r'(?:in )?(?:which|what) (?:folder|directory) is (?:the )?(?:program |application |app |game )(?P<name>.+?)(?: installed| located| stored)?',
  r'where is (?:the )?(?:program |application |app |game )(?P<name>.+?)(?: located| stored)?',
  r'(?:find|show)(?: me)? (?:the )?(?:install(?:ation)? folder|install(?:ation)? directory|executable|exe(?: file)?)(?: (?:of|for))?(?: the)? (?:program |application |app |game )?(?P<name>.+)',
 ),
 'de': (
  r'in welchem (?:Ordner|Verzeichnis) (?:ist|liegt|befindet sich) (?:das Programm |die Anwendung |das Spiel )(?P<name>.+?)(?: installiert)?',
  r'wo (?:ist|liegt|befindet sich) (?:das Programm |die Anwendung |das Spiel )(?P<name>.+)',
  # 'zeige den Installationsordner von ...' already owns an explicit open
  # contract. Search additions must not change that action or its clarification.
  r'finde(?: mir)? den Installationsordner(?: (?:von|für))? (?:das Programm |die Anwendung |das Spiel |dem Programm |der Anwendung |dem Spiel )?(?P<name>.+)',
  r'(?:finde|zeige)(?: mir)? (?:die ausführbare Datei|die Exe-Datei)(?: (?:von|für))? (?:das Programm |die Anwendung |das Spiel |dem Programm |der Anwendung |dem Spiel )?(?P<name>.+)',
 ),
 'fr': (
  r'dans quel (?:dossier|répertoire) (?:est|se trouve) (?:le programme |l[’\x27]application |le jeu )(?P<name>.+?)(?: installé(?:e)?)?',
  r'où (?:est|se trouve) (?:le programme |l[’\x27]application |le jeu )(?P<name>.+)',
  r'(?:trouve|affiche)(?:-moi)? (?:le dossier d[’\x27]installation|l[’\x27]exécutable)(?: (?:de|du))? (?:programme |jeu |l[’\x27]application )?(?P<name>.+)',
 ),
 'es': (
  r'en qué (?:carpeta|directorio) (?:está|se encuentra)(?: instalad[oa])? (?:el programa |la aplicación |el juego )(?P<name>.+)',
  r'dónde (?:está|se encuentra) (?:el programa |la aplicación |el juego )(?P<name>.+)',
  r'(?:busca|muestra) (?:la carpeta de instalación|el ejecutable)(?: (?:de|del))? (?:programa |juego |la aplicación )?(?P<name>.+)',
 ),
 'it': (
  r'in quale (?:cartella|directory) (?:si trova|è)(?: installat[oa])? (?:il programma |l[’\x27]applicazione |il gioco )(?P<name>.+)',
  r'dove (?:è|si trova) (?:il programma |l[’\x27]applicazione |il gioco )(?P<name>.+)',
  r'(?:trova|mostra) (?:la cartella di installazione|l[’\x27]eseguibile)(?: (?:di|del))? (?:programma |gioco |l[’\x27]applicazione )?(?P<name>.+)',
 ),
 'pt': (
  r'em (?:que|qual) (?:pasta|diretório) (?:está|fica|se encontra)(?: instalad[oa])? (?:o programa |a aplicação |o aplicativo |o jogo )(?P<name>.+)',
  r'onde (?:está|fica|se encontra) (?:o programa |a aplicação |o aplicativo |o jogo )(?P<name>.+)',
  r'(?:encontra|mostra) (?:a pasta de instalação|o executável)(?: (?:de|do))? (?:programa |jogo |da aplicação |do aplicativo )?(?P<name>.+)',
 ),
}
for _language, _patterns in _FOLDER_PATTERNS.items():
    _PATTERNS[_language] += _patterns

_BARE_INSTALLED = {
 'ru': r'где установлен[ао]? (?:игра|приложение|программа)',
 'en': r'where is (?:the )?(?:game|application|app|program) installed',
 'de': r'wo ist (?:das Spiel|die Anwendung|das Programm) installiert',
 'fr': r'où est installé(?:e)? (?:le jeu|l[’\x27]application|le programme)',
 'es': r'dónde está instalad[oa] (?:el juego|la aplicación|el programa)',
 'it': r'dove è installat[oa] (?:il gioco|il programma|l[’\x27]applicazione)',
 'pt': r'onde está instalad[oa] (?:o jogo|o programa|a aplicação)',
}


def missing_location_request(text, language='ru'):
    value = str(text).strip().rstrip('?! .')
    if re.fullmatch(_BARE_INSTALLED.get(language, r'(?!)'), value, re.I):
        return True
    # A trailing predicate (e.g. 'installed') must not become the product name.
    for pattern in _PATTERNS.get(language, ()):
        match = re.fullmatch(pattern, value + ' __neo_missing_target__', re.I)
        if match and match['name'] == '__neo_missing_target__':
            return True
    return False


def parse_location_request(text, language='ru'):
    value = str(text).strip().rstrip('?! .')
    if missing_location_request(value, language):
        return None
    for pattern in _PATTERNS.get(language, ()):
        match = re.fullmatch(pattern, value, re.I)
        if match:
            name = match['name'].strip(' "«»')
            if len(name) >= 2:
                return 'game_locations', {'action': 'find', 'name': name, 'kind': 'all'}
    return None
