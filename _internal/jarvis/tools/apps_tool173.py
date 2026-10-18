"""NEO 173: exact skills for everyday programs — through the programs' own mechanisms.

Instead of looking for words on the screen, NEO uses what each program offers itself:

* File Explorer — ``shell:`` folders (Downloads, Documents, Desktop, Pictures, Videos, Music,
  This PC, Recycle Bin); new folders are created on disk directly and shown in Explorer.
* Windows Settings — ``ms-settings:`` pages (sound, display, Bluetooth, Wi-Fi, updates, …).
* Steam — ``steam://`` links (store, library, downloads, friends, settings, store search).
* Discord — ``discord://`` links; its own shortcut Ctrl+, for settings.
* Telegram — ``tg://`` links (settings, a chat by @username); a message is typed, never sent.
* Browsers — their own shortcuts: history Ctrl+H, downloads Ctrl+J, bookmarks, private window,
  reopen a closed tab.

Everything is local, visible and on the user's command. Programs are only opened, never
closed or changed; nothing is sent in the user's name.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from urllib.parse import quote

_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


def _n(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').casefold().replace('ё', 'е')).strip(' .,!?:;¿¡«»"\'')


_B, _E = r'(?<![\w-])', r'(?![\w-])'
_OPEN = r'(?:открой|откройте|покажи|зайди\s+в|перейди\s+в|open|show(?:\s+me)?|go\s+to|[oö]ffne|zeig(?:\s+mir)?|ouvre|affiche|abre|muestra|mu[eé]strame|apri|mostra|abre|mostra)'

# ---- File Explorer ---------------------------------------------------------------------------

FOLDERS: dict[str, tuple[str, str]] = {        # key → (shell: name, words)
    'downloads': ('shell:Downloads', r'(?:загрузк\w*|скачанн\w*|downloads?|download-ordner|t[eé]l[eé]chargements|descargas|scaricati|transfer[eê]ncias)'),
    'documents': ('shell:Personal', r'(?:документ\w*|(?:my\s+)?documents|dokumente|mes\s+documents|documentos|documenti)'),
    'desktop': ('shell:Desktop', r'(?:рабоч\w+\s+стол\w*|desktop|schreibtisch|bureau|escritorio|scrivania|[aá]rea\s+de\s+trabalho)'),
    'pictures': ('shell:My Pictures', r'(?:изображени\w*|картинк\w*|фотографи\w*|pictures|photos|bilder|images|im[aá]genes|immagini|imagens)'),
    'videos': ('shell:My Video', r'(?:видео|videos?|vid[eé]os)'),
    'music': ('shell:My Music', r'(?:музык\w*|music|musik|musique|m[uú]sica|musica)'),
    'this_pc': ('shell:MyComputerFolder', r'(?:этот\s+компьютер|мой\s+компьютер|this\s+pc|my\s+computer|dieser\s+pc|ce\s+pc|este\s+equipo|questo\s+pc|este\s+computador)'),
    'recycle': ('shell:RecycleBinFolder', r'(?:корзин\w*|recycle\s+bin|papierkorb|corbeille|papelera(?:\s+de\s+reciclaje)?|cestino|reciclagem)'),
}
_FOLDER_PREFIX = r'(?:(?:папку|папка|folder|den\s+ordner|ordner|le\s+dossier|dossier|la\s+carpeta|carpeta|la\s+cartella|cartella|a\s+pasta|pasta)\s+)?'


def known_folder(key: str) -> Path:
    if key in {'downloads', 'documents', 'desktop'}:
        from jarvis.runtime import tidy172
        return tidy172.known_folder(key)
    names = {'pictures': 'Pictures', 'videos': 'Videos', 'music': 'Music'}
    return Path.home() / names.get(key, '')


# ---- Windows Settings --------------------------------------------------------------------------

SETTINGS: dict[str, tuple[str, str]] = {
    'sound': ('ms-settings:sound', r'(?:звук\w*|sound|audio|ton|son|sonido|suono|som)'),
    'display': ('ms-settings:display', r'(?:экран\w*|дисплей\w*|display|bildschirm|anzeige|affichage|[eé]cran|pantalla|schermo|ecr[aã]|tela)'),
    'bluetooth': ('ms-settings:bluetooth', r'(?:блютуз\w*|bluetooth)'),
    'wifi': ('ms-settings:network-wifi', r'(?:вай[-\s]?фай\w*|wi[-\s]?fi|wlan)'),
    'network': ('ms-settings:network-status', r'(?:сет\w*|интернет\w*|network|netzwerk|r[eé]seau|red|rete|rede)'),
    'vpn': ('ms-settings:network-vpn', r'(?:впн|vpn)'),
    'hotspot': ('ms-settings:network-mobilehotspot', r'(?:точк\w+\s+доступа|мобильн\w+\s+хот-?спот\w*|hotspot|point\s+d[\'’]acc[eè]s)'),
    'airplane': ('ms-settings:network-airplanemode', r'(?:режим\w*\s+(?:в\s+)?самол[её]т\w*|airplane\s+mode|flugmodus|mode\s+avion|modo\s+avi[oó]n|modalit[aà]\s+aereo|modo\s+de\s+avi[aã]o)'),
    'updates': ('ms-settings:windowsupdate', r'(?:центр\w*\s+обновлени\w*|обновлени\w*(?:\s+windows)?|windows\s+update|updates?|aktualisierung\w*|mises?\s+à\s+jour|actualizaci\w+|aggiornament\w+|atualiza[cç][oõ]es)'),
    'apps': ('ms-settings:appsfeatures', r'(?:приложени\w*|программ\w*|installed\s+apps|apps|apps\s+&\s+features|programme|applications|aplicaciones|app|aplica[cç][oõ]es)'),
    'default_apps': ('ms-settings:defaultapps', r'(?:приложени\w+\s+по\s+умолчанию|default\s+apps|standard-?apps|applications\s+par\s+d[eé]faut|aplicaciones\s+predeterminadas|app\s+predefinite|aplica[cç][oõ]es\s+predefinidas)'),
    'startup': ('ms-settings:startupapps', r'(?:автозагрузк\w*|автозапуск\w*|startup(?:\s+apps)?|autostart|d[eé]marrage|inicio|avvio|arranque)'),
    'power': ('ms-settings:powersleep', r'(?:питани\w*|электропитани\w*|сон|power(?:\s+&\s+sleep)?|sleep|energie|alimentation|energ[ií]a|alimentazione|energia)'),
    'battery': ('ms-settings:batterysaver', r'(?:батаре\w*|аккумулятор\w*|battery|akku|batterie|bater[ií]a|batteria)'),
    'storage': ('ms-settings:storagesense', r'(?:хранилищ\w*|пам[яи]ть\s+устройства|диск\w*|storage|speicher|stockage|almacenamiento|archiviazione|armazenamento)'),
    'about': ('ms-settings:about', r'(?:о\s+системе|об\s+устройстве|характеристик\w+\s+компьютера|about(?:\s+this\s+pc)?|info|[àa]\s+propos|acerca\s+de|informazioni|sobre)'),
    'mouse': ('ms-settings:mousetouchpad', r'(?:мыш\w*|тачпад\w*|сенсорн\w+\s+панел\w*|mouse|touchpad|maus|souris|rat[oó]n|rato)'),
    'keyboard': ('ms-settings:typing', r'(?:клавиатур\w*|ввод\w*|keyboard|typing|tastatur|clavier|teclado|tastiera)'),
    'language': ('ms-settings:regionlanguage', r'(?:язык\w*|регион\w*|language|sprache|langue|idioma|lingua)'),
    'time': ('ms-settings:dateandtime', r'(?:дат\w+\s+и\s+врем\w+|врем\w+|date\s+(?:and|&)\s+time|time|datum|uhrzeit|date\s+et\s+heure|fecha\s+y\s+hora|data\s+e\s+ora|data\s+e\s+hora)'),
    'notifications': ('ms-settings:notifications', r'(?:уведомлени\w*|notifications?|benachrichtigungen|notificaciones|notifiche|notifica[cç][oõ]es)'),
    'focus': ('ms-settings:quiethours', r'(?:фокусировк\w*|не\s+беспокоить|focus(?:\s+assist)?|do\s+not\s+disturb|nicht\s+st[oö]ren|ne\s+pas\s+d[eé]ranger|no\s+molestar|non\s+disturbare|n[aã]o\s+incomodar)'),
    'background': ('ms-settings:personalization-background', r'(?:фон\w*|обо[иев]\w*|персонализаци\w*|background|wallpaper|personali[sz]ation|hintergrund|fond\s+d[\'’][eé]cran|fondo|sfondo|plano\s+de\s+fundo)'),
    'themes': ('ms-settings:themes', r'(?:тем[аыу]|themes?|designs|th[eè]mes|temas|temi)'),
    'colors': ('ms-settings:colors', r'(?:цвет\w*|тёмн\w+\s+тем\w*|темн\w+\s+тем\w*|colou?rs?|dark\s+mode|farben|couleurs|colores|colori|cores)'),
    'night_light': ('ms-settings:nightlight', r'(?:ночн\w+\s+свет\w*|night\s+light|nachtmodus|[eé]clairage\s+nocturne|luz\s+nocturna|luce\s+notturna|luz\s+noturna)'),
    'printers': ('ms-settings:printers', r'(?:принтер\w*|печат\w*|printers?|drucker|imprimantes?|impresoras?|stampanti|impressoras?)'),
    'devices': ('ms-settings:connecteddevices', r'(?:устройств\w*|devices?|ger[aä]te|p[eé]riph[eé]riques|dispositivos|dispositivi)'),
    'camera': ('ms-settings:privacy-webcam', r'(?:камер\w*|веб-?камер\w*|camera|webcam|kamera|cam[eé]ra|c[aá]mara)'),
    'microphone': ('ms-settings:privacy-microphone', r'(?:микрофон\w*|microphone|mikrofon|micr[oó]fono|microfono|microfone)'),
    'privacy': ('ms-settings:privacy', r'(?:конфиденциальност\w*|приватност\w*|privacy|datenschutz|confidentialit[eé]|privacidad|privacidade)'),
    'gaming': ('ms-settings:gaming-gamemode', r'(?:игров\w+\s+режим\w*|game\s+mode|spielmodus|mode\s+jeu|modo\s+de\s+juego|modalit[aà]\s+gioco|modo\s+de\s+jogo)'),
    'accounts': ('ms-settings:yourinfo', r'(?:уч[её]тн\w+\s+запис\w*|аккаунт\w*|accounts?|konten|comptes|cuentas|account|contas)'),
    'accessibility': ('ms-settings:easeofaccess', r'(?:специальн\w+\s+возможност\w*|accessibility|barrierefreiheit|accessibilit[eé]|accesibilidad|accessibilit[aà]|acessibilidade)'),
    'clipboard': ('ms-settings:clipboard', r'(?:буфер\w*\s+обмена|clipboard|zwischenablage|presse-papiers|portapapeles|appunti|[aá]rea\s+de\s+transfer[eê]ncia)'),
}
_SETTINGS_WORD = r'(?:настройк\w*|параметр\w*|settings|einstellungen|param[eè]tres|configuraci[oó]n|ajustes|impostazioni|defini[cç][oõ]es|configura[cç][oõ]es)'

# ---- Steam / Discord / Telegram / browsers ------------------------------------------------------

_STEAM = r'(?:steam\w*|стим\w*)'
STEAM_PAGES = {
    'store': ('steam://store', r'(?:магазин\w*|store|shop|laden|boutique|tienda|negozio|loja)'),
    'library': ('steam://nav/games', r'(?:библиотек\w*|игр\w*|library|games|bibliothek|biblioth[eè]que|biblioteca|giochi|jogos|juegos)'),
    'downloads': ('steam://open/downloads', r'(?:загрузк\w*|скачивани\w*|downloads?|t[eé]l[eé]chargements|descargas|download|transfer[eê]ncias)'),
    'friends': ('steam://open/friends', r'(?:друз\w*|friends?|freunde|amis|amigos|amici)'),
    'settings': ('steam://open/settings', _SETTINGS_WORD),
}
_DISCORD = r'(?:discord\w*|дискорд\w*|дискорт\w*)'
_TELEGRAM = r'(?:telegram\w*|телеграм\w*|телега\w*|телеге)'
_BROWSER = r'(?:браузер\w*|browser\w*|navigateur|navegador|chrome|хром\w*|edge|firefox|opera|опер\w*|яндекс\s+браузер\w*)'
BROWSER_KEYS = {
    'history': (('ctrl', 'h'), r'(?:истори\w*|history|verlauf|historique|historial|cronologia|hist[oó]rico)'),
    'downloads': (('ctrl', 'j'), r'(?:загрузк\w*|скачанн\w*|downloads?|t[eé]l[eé]chargements|descargas|download|transfer[eê]ncias)'),
    'bookmarks': (('ctrl', 'shift', 'o'), r'(?:закладк\w*|избранн\w*|bookmarks?|favorites|lesezeichen|favoris|marcadores|segnalibri|favoritos)'),
    'private': (('ctrl', 'shift', 'n'), r'(?:инкогнито|приватн\w+\s+(?:окно|вкладк\w*|режим\w*)|incognito(?:\s+window)?|private\s+window|inprivate|privates\s+fenster|navigation\s+priv[eé]e|ventana\s+privada|incógnito|finestra\s+(?:privata|anonima)|janela\s+privada|an[oô]nima)'),
    'reopen_tab': (('ctrl', 'shift', 't'), r'(?:верни\s+закрыт\w+\s+вкладк\w*|открой\s+закрыт\w+\s+вкладк\w*|восстанови\s+(?:закрыт\w+\s+)?вкладк\w*|reopen\s+(?:the\s+)?closed\s+tab|geschlossenen\s+tab|rouvre\s+l[\'’]onglet|reabre\s+la\s+pesta[nñ]a|riapri\s+la\s+scheda|reabre\s+o\s+separador)'),
}
BROWSER_PROCESSES = frozenset({'chrome.exe', 'msedge.exe', 'opera.exe', 'opera_gx.exe', 'firefox.exe', 'browser.exe', 'brave.exe', 'vivaldi.exe', 'yandex.exe'})


# ---- phrases → intents ---------------------------------------------------------------------------

def parse(text: str, code: str) -> tuple[str, dict[str, Any]] | None:
    """An exact skill for this phrase: (tool, arguments), or None."""
    from jarvis.runtime.natural_commands import strip_request_shell
    text = strip_request_shell(text, code)
    t = _n(text)
    if not t or len(t.split()) > 12:
        return None
    # Browser: history, downloads of the browser, bookmarks, private window, reopen a closed tab.
    for key, (_keys, words) in BROWSER_KEYS.items():
        if key == 'reopen_tab':
            if re.fullmatch(words, t):
                return 'apps', {'action': 'browser', 'key': key}
            continue
        if key == 'private' and re.fullmatch(rf'(?:{_OPEN}\s+)?(?:(?:новое|new|neues|nouvelle|nueva|nuova|nova)\s+)?(?:окно\s+|вкладку\s+|window\s+)?{words}(?:\s+(?:в\s+)?{_BROWSER})?', t):
            return 'apps', {'action': 'browser', 'key': key}
        if re.fullmatch(rf'{_OPEN}\s+{words}\s+(?:в\s+|of\s+the\s+|des\s+|del\s+|do\s+)?{_BROWSER}|{_OPEN}\s+{words}' + ('' if key == 'downloads' else r'(?:\s+браузера)?'), t) \
                and (key != 'downloads' or re.search(_BROWSER, t)):
            return 'apps', {'action': 'browser', 'key': key}
    # Steam
    if re.search(_STEAM, t):
        search = re.fullmatch(rf'(?:найди|поищи|find|search\s+for|suche|cherche|busca|cerca|procura)\s+(.+?)\s+(?:в|in|dans|en|su|na|no)\s+(?:магазине\s+)?{_STEAM}', t)
        if search:
            return 'apps', {'action': 'steam', 'page': 'search', 'query': search.group(1)}
        for page, (_uri, words) in STEAM_PAGES.items():
            if re.fullmatch(rf'{_OPEN}\s+(?:{words}\s+(?:в\s+|in\s+|de\s+|di\s+|do\s+|im\s+)?{_STEAM}|{_STEAM}\s+{words})', t):
                return 'apps', {'action': 'steam', 'page': page}
    # Discord
    if re.search(_DISCORD, t):
        if re.fullmatch(rf'{_OPEN}\s+(?:{_SETTINGS_WORD}\s+(?:в\s+)?{_DISCORD}|{_DISCORD}\s+{_SETTINGS_WORD})', t):
            return 'apps', {'action': 'discord', 'page': 'settings'}
        if re.fullmatch(rf'{_OPEN}\s+(?:(?:личные\s+сообщения|личку|лс|друзей|direct\s+messages|dms|friends|direktnachrichten|messages\s+priv[eé]s|mensajes\s+directos|messaggi\s+diretti|mensagens\s+diretas)\s+(?:в\s+)?{_DISCORD}|{_DISCORD}\s+(?:dms|friends))', t):
            return 'apps', {'action': 'discord', 'page': 'dms'}
    # Telegram
    if re.search(_TELEGRAM, t):
        if re.fullmatch(rf'{_OPEN}\s+(?:{_SETTINGS_WORD}\s+(?:в\s+)?{_TELEGRAM}|{_TELEGRAM}\s+{_SETTINGS_WORD})', t):
            return 'apps', {'action': 'telegram', 'page': 'settings'}
        raw = ' '.join(str(text).split())
        # Message-center commands own sender filtering and actual outbound sends.
        # Desktop Telegram skills only open a chat or prepare text for review.
        if re.search(r'\b(?:сообщения|messages|mensajes|messaggi|mensagens|nachrichten)\b', t):
            return None
        if re.match(r'(?:напиши|отправь)\s+в\s+' + _TELEGRAM + r'\s+для(?:\s|$)', t):
            return None
        chat = re.search(r'(?<![\w.@+-])@([A-Za-z][A-Za-z0-9_]{3,31})(?![\w.]|@)', raw)
        write = re.match(rf'(?i)(?:напиши|отправь|write|send|schreib\w*|[eé]cris|escribe|scrivi|escreve)\b.*?@[A-Za-z][A-Za-z0-9_]{{3,31}}[\s,:]+(.+)$', raw)
        if chat and write:
            return 'apps', {'action': 'telegram', 'page': 'chat', 'username': chat.group(1), 'text': write.group(1).strip()}
        if chat and re.match(rf'{_OPEN}\b', t):
            return 'apps', {'action': 'telegram', 'page': 'chat', 'username': chat.group(1)}
        if re.match(r'(?:напиши|отправь)\s+в\s+' + _TELEGRAM, t) and not chat:
            return 'apps', {'action': 'telegram', 'page': 'need_username'}
    # Pages with their own well-known names: «открой центр обновления windows», «открой windows update».
    if re.fullmatch(rf'{_OPEN}\s+(?:центр\w*\s+обновлени\w*(?:\s+windows)?|windows\s+update)', t):
        return 'apps', {'action': 'settings', 'page': 'updates'}
    # Windows Settings: «открой настройки звука», «открой звук в параметрах», «параметры bluetooth»
    for page, (_uri, words) in SETTINGS.items():
        art = r'(?:(?:die|les|la|le|las|los|el|il|i|as|os|the)\s+)?'
        link = r'(?:(?:de|du|des|di|do|da|del|della|dello|dos|das|für|for|of|von)\s+)?(?:(?:la|le|el|il|o|a|the|den|der)\s+|l[\'’])?'
        if re.fullmatch(rf'(?:{_OPEN}\s+)?(?:{art}{_SETTINGS_WORD}\s+(?:windows\s+)?{link}{words}|{words}\s+(?:в\s+)?{art}{_SETTINGS_WORD})', t):
            if page in {'apps'} and re.search(_STEAM + '|' + _DISCORD + '|' + _TELEGRAM, t):
                continue
            return 'apps', {'action': 'settings', 'page': page}
    # File Explorer folders
    for key, (_shell, words) in FOLDERS.items():
        if re.fullmatch(rf'{_OPEN}\s+{_FOLDER_PREFIX}(?:мои\s+|my\s+|meine\s+|mes\s+|mis\s+|i\s+miei\s+|os\s+meus\s+|the\s+|den\s+|die\s+|das\s+|le\s+|la\s+|les\s+|el\s+|il\s+|lo\s+|a\s+|o\s+|os\s+|as\s+)?{words}', t):
            return 'apps', {'action': 'folder', 'folder': key}
    # New folder
    raw_text = ' '.join(str(text or '').split()).strip(' .!?')
    new = re.fullmatch(r'(?:создай|сделай|create|make|erstelle|cr[eé]e|crea|cria)\s+(?:новую\s+|a\s+new\s+|a\s+|an\s+|new\s+|einen\s+neuen\s+|einen\s+|un\s+nouveau\s+|un\s+|una\s+nueva\s+|una\s+nuova\s+|una\s+|uma\s+nova\s+|uma\s+)?'
                       r'(?:папку|folder|ordner|dossier|carpeta|cartella|pasta)\s+(?P<name>.+?)'
                       r'(?:\s+(?:на|в|on|on\s+the|in|in\s+the|auf\s+dem|im|sur\s+le|dans|dans\s+les|en|en\s+el|sul|sulla|nella|na|no|em)\s+(?P<where>.+))?', raw_text, re.I)
    if new:
        # A postnominal adjective is a missing name, not a literal folder name.
        # Quoted or explicitly named folders retain this valid user data.
        if code == 'es' and new.group('name').casefold() == 'nueva':
            return None
        where = re.sub(r'^(?:the|my|le|la|les|mes|el|mis|il|i|o|os|a|as|den|dem|die|meinen|meine)\s+', '', _n(new.group('where') or ''))
        folder = next((key for key, (_s, words) in FOLDERS.items() if key in {'downloads', 'documents', 'desktop', 'pictures', 'videos', 'music'}
                       and re.fullmatch(words, where)), '')
        if where and not folder and not re.fullmatch(r'(?:туда|сюда|there|here|этой\s+папке|this\s+folder)', where):
            return None                     # an unknown place: the general file commands handle full paths
        name = re.sub(r'^(?:с\s+названием|под\s+названием|called|named|namens|nomm[eé]e?|llamada|chiamata|chamada)\s+', '', new.group('name')).strip(' "«»')
        if not name or len(name) > 80:
            return None
        if re.search(r'[\\/]|^[A-Za-z]:', name):
            return None                     # NEO 188 atlas: a path is for the file commands, not a bare folder name
        return 'apps', {'action': 'create_folder', 'name': name, 'folder': folder or 'context'}
    return None


# ---- the tool ---------------------------------------------------------------------------------

_TEXT = {
    'folder': ('Открыл «{name}» в Проводнике.', 'Opened “{name}” in File Explorer.', '„{name}“ im Explorer geöffnet.', '« {name} » ouvert dans l’Explorateur.',
               'Abrí «{name}» en el Explorador.', 'Aperto «{name}» in Esplora file.', 'Abri «{name}» no Explorador.'),
    'created': ('Создал папку «{name}» в «{where}» и показал её в Проводнике.', 'Created the folder “{name}” in “{where}” and showed it in File Explorer.',
                'Ordner „{name}“ in „{where}“ erstellt und im Explorer gezeigt.', 'Dossier « {name} » créé dans « {where} » et affiché dans l’Explorateur.',
                'Creé la carpeta «{name}» en «{where}» y la mostré en el Explorador.', 'Creata la cartella «{name}» in «{where}» e mostrata in Esplora file.',
                'Criei a pasta «{name}» em «{where}» e mostrei-a no Explorador.'),
    'exists': ('Папка «{name}» в «{where}» уже есть — показал её.', 'The folder “{name}” already exists in “{where}” — showed it.',
               'Der Ordner „{name}“ gibt es in „{where}“ schon – gezeigt.', 'Le dossier « {name} » existe déjà dans « {where} » — affiché.',
               'La carpeta «{name}» ya existe en «{where}»; la mostré.', 'La cartella «{name}» esiste già in «{where}»: mostrata.',
               'A pasta «{name}» já existe em «{where}» — mostrei-a.'),
    'bad_name': ('В имени папки не может быть символов \\ / : * ? " < > |.', 'A folder name cannot contain \\ / : * ? " < > |.',
                 'Ein Ordnername darf \\ / : * ? " < > | nicht enthalten.', 'Un nom de dossier ne peut pas contenir \\ / : * ? " < > |.',
                 'Un nombre de carpeta no puede contener \\ / : * ? " < > |.', 'Il nome della cartella non può contenere \\ / : * ? " < > |.',
                 'O nome da pasta não pode conter \\ / : * ? " < > |.'),
    'settings': ('Открыл параметры Windows: {page}.', 'Opened Windows Settings: {page}.', 'Windows-Einstellungen geöffnet: {page}.',
                 'Paramètres Windows ouverts : {page}.', 'Abrí la Configuración de Windows: {page}.', 'Aperte le Impostazioni di Windows: {page}.',
                 'Abri as Definições do Windows: {page}.'),
    'steam': ('Открыл в Steam: {page}.', 'Opened in Steam: {page}.', 'In Steam geöffnet: {page}.', 'Ouvert dans Steam : {page}.',
              'Abrí en Steam: {page}.', 'Aperto in Steam: {page}.', 'Abri no Steam: {page}.'),
    'discord': ('Открыл Discord: {page}.', 'Opened Discord: {page}.', 'Discord geöffnet: {page}.', 'Discord ouvert : {page}.',
                'Abrí Discord: {page}.', 'Aperto Discord: {page}.', 'Abri o Discord: {page}.'),
    'telegram': ('Открыл Telegram: {page}.', 'Opened Telegram: {page}.', 'Telegram geöffnet: {page}.', 'Telegram ouvert : {page}.',
                 'Abrí Telegram: {page}.', 'Aperto Telegram: {page}.', 'Abri o Telegram: {page}.'),
    'typed': ('Открыл чат @{user} и напечатал сообщение. Отправьте его сами — Enter.', 'Opened the chat with @{user} and typed the message. Send it yourself with Enter.',
              'Chat mit @{user} geöffnet und die Nachricht getippt. Sende sie selbst mit Enter.', 'Discussion avec @{user} ouverte et message saisi. Envoyez-le vous-même avec Entrée.',
              'Abrí el chat con @{user} y escribí el mensaje. Envíalo tú con Enter.', 'Aperta la chat con @{user} e scritto il messaggio. Invialo tu con Invio.',
              'Abri o chat com @{user} e escrevi a mensagem. Envia-a tu com Enter.'),
    'need_username': ('Чтобы открыть чат, назовите имя пользователя Telegram с @ (например, «напиши в телеграм @ivan привет»): '
                      'по имени из контактов Telegram открыть чат снаружи не позволяет.',
                      'To open a chat, say the Telegram username with @ (e.g. “write in telegram @ivan hi”): Telegram does not let other programs open a chat by a contact name.',
                      'Nenne den Telegram-Benutzernamen mit @ (z. B. „schreib in Telegram @ivan hallo“): Telegram erlaubt kein Öffnen nach Kontaktname.',
                      'Dites le nom d’utilisateur Telegram avec @ (ex. « écris sur telegram @ivan salut ») : Telegram ne permet pas d’ouvrir une discussion par le nom du contact.',
                      'Di el nombre de usuario de Telegram con @ (p. ej. «escribe en telegram @ivan hola»): Telegram no deja abrir un chat por el nombre del contacto.',
                      'Di’ il nome utente Telegram con @ (es. «scrivi su telegram @ivan ciao»): Telegram non consente di aprire una chat dal nome del contatto.',
                      'Diz o nome de utilizador do Telegram com @ (ex.: «escreve no telegram @ivan olá»): o Telegram não permite abrir um chat pelo nome do contacto.'),
    'browser': ('Нажал в браузере «{what}» ({keys}).', 'Pressed “{what}” in the browser ({keys}).', '„{what}“ im Browser gedrückt ({keys}).',
                '« {what} » envoyé au navigateur ({keys}).', 'Pulsé «{what}» en el navegador ({keys}).', 'Premuto «{what}» nel browser ({keys}).',
                'Carreguei em «{what}» no navegador ({keys}).'),
    'no_browser': ('Браузер не открыт. Скажите «открой браузер», а затем повторите.', 'No browser is open. Say “open the browser” and try again.',
                   'Kein Browser offen. Sag „öffne den Browser“ und versuch es erneut.', 'Aucun navigateur ouvert. Dites « ouvre le navigateur » puis réessayez.',
                   'No hay navegador abierto. Di «abre el navegador» y repite.', 'Nessun browser aperto. Di’ «apri il browser» e riprova.',
                   'Nenhum navegador aberto. Diz «abre o navegador» e repete.'),
    'not_installed': ('Похоже, {app} не установлен: Windows не знает ссылки {scheme}.', '{app} seems not to be installed: Windows does not know {scheme} links.',
                      '{app} scheint nicht installiert zu sein: Windows kennt {scheme}-Links nicht.', '{app} ne semble pas installé : Windows ne connaît pas les liens {scheme}.',
                      '{app} parece no estar instalado: Windows no conoce los enlaces {scheme}.', '{app} sembra non installato: Windows non conosce i link {scheme}.',
                      '{app} parece não estar instalado: o Windows não conhece ligações {scheme}.'),
    'unsupported': ('Это работает только в Windows.', 'This works only on Windows.', 'Das geht nur unter Windows.', 'Cela ne fonctionne que sous Windows.',
                    'Esto solo funciona en Windows.', 'Funziona solo su Windows.', 'Isto só funciona no Windows.'),
}
_LABELS = {
    'history': ('история', 'history', 'Verlauf', 'historique', 'historial', 'cronologia', 'histórico'),
    'downloads': ('загрузки', 'downloads', 'Downloads', 'téléchargements', 'descargas', 'download', 'transferências'),
    'bookmarks': ('закладки', 'bookmarks', 'Lesezeichen', 'favoris', 'marcadores', 'segnalibri', 'marcadores'),
    'private': ('приватное окно', 'private window', 'privates Fenster', 'fenêtre privée', 'ventana privada', 'finestra privata', 'janela privada'),
    'reopen_tab': ('вернуть закрытую вкладку', 'reopen closed tab', 'geschlossenen Tab wiederherstellen', 'rouvrir l’onglet fermé',
                   'reabrir la pestaña cerrada', 'riaprire la scheda chiusa', 'reabrir o separador fechado'),
    'store': ('магазин', 'store', 'Shop', 'boutique', 'tienda', 'negozio', 'loja'), 'library': ('библиотека', 'library', 'Bibliothek', 'bibliothèque', 'biblioteca', 'libreria', 'biblioteca'),
    'friends': ('друзья', 'friends', 'Freunde', 'amis', 'amigos', 'amici', 'amigos'), 'settings': ('настройки', 'settings', 'Einstellungen', 'paramètres', 'ajustes', 'impostazioni', 'definições'),
    'search': ('поиск «{q}»', 'search “{q}”', 'Suche „{q}“', 'recherche « {q} »', 'búsqueda «{q}»', 'ricerca «{q}»', 'pesquisa «{q}»'),
    'dms': ('личные сообщения', 'direct messages', 'Direktnachrichten', 'messages privés', 'mensajes directos', 'messaggi diretti', 'mensagens diretas'),
    'chat': ('чат @{user}', 'chat @{user}', 'Chat @{user}', 'discussion @{user}', 'chat @{user}', 'chat @{user}', 'chat @{user}'),
}
_FOLDER_LABELS = {
    'downloads': ('Загрузки', 'Downloads', 'Downloads', 'Téléchargements', 'Descargas', 'Download', 'Transferências'),
    'documents': ('Документы', 'Documents', 'Dokumente', 'Documents', 'Documentos', 'Documenti', 'Documentos'),
    'desktop': ('Рабочий стол', 'Desktop', 'Desktop', 'Bureau', 'Escritorio', 'Desktop', 'Ambiente de trabalho'),
    'pictures': ('Изображения', 'Pictures', 'Bilder', 'Images', 'Imágenes', 'Immagini', 'Imagens'),
    'videos': ('Видео', 'Videos', 'Videos', 'Vidéos', 'Vídeos', 'Video', 'Vídeos'),
    'music': ('Музыка', 'Music', 'Musik', 'Musique', 'Música', 'Musica', 'Música'),
    'this_pc': ('Этот компьютер', 'This PC', 'Dieser PC', 'Ce PC', 'Este equipo', 'Questo PC', 'Este computador'),
    'recycle': ('Корзина', 'Recycle Bin', 'Papierkorb', 'Corbeille', 'Papelera', 'Cestino', 'Reciclagem'),
}


_SETTINGS_RU = {
    'sound': 'звук', 'display': 'экран', 'bluetooth': 'Bluetooth', 'wifi': 'Wi-Fi', 'network': 'сеть', 'vpn': 'VPN',
    'hotspot': 'мобильный хот-спот', 'airplane': 'режим «в самолёте»', 'updates': 'Центр обновления Windows', 'apps': 'приложения',
    'default_apps': 'приложения по умолчанию', 'startup': 'автозагрузка', 'power': 'питание и спящий режим', 'battery': 'батарея',
    'storage': 'память устройства', 'about': 'о системе', 'mouse': 'мышь и сенсорная панель', 'keyboard': 'ввод', 'language': 'язык и регион',
    'time': 'дата и время', 'notifications': 'уведомления', 'focus': 'фокусировка внимания', 'background': 'фон рабочего стола',
    'themes': 'темы', 'colors': 'цвета', 'night_light': 'ночной свет', 'printers': 'принтеры', 'devices': 'устройства',
    'camera': 'доступ к камере', 'microphone': 'доступ к микрофону', 'privacy': 'конфиденциальность', 'gaming': 'игровой режим',
    'accounts': 'учётная запись', 'accessibility': 'специальные возможности', 'clipboard': 'буфер обмена',
}


def t(key: str, code: str, /, **values: Any) -> str:
    return _pick(_TEXT[key], code).format(**values)


def _start(uri: str) -> None:
    os.startfile(uri)  # type: ignore[attr-defined]


def _browser_window():
    from jarvis.runtime import screen_targets171 as targets
    for window in targets.windows(limit=14):
        if window.process in BROWSER_PROCESSES:
            return window
    return None


def run(config: Any, args: dict[str, Any], *, start=None, press=None, find_browser=None, type_text=None, sleep=time.sleep):
    from jarvis.tools.registry import ToolExecution
    from jarvis.language import language_code
    code = language_code(config)
    start = start or _start
    action = str(args.get('action', '') or '')
    windows = os.name == 'nt' or start is not _start
    if not windows:
        return ToolExecution(False, t('unsupported', code), {'unsupported': True})
    try:
        if action == 'folder':
            key = str(args.get('folder', ''))
            start(FOLDERS[key][0])
            path = known_folder(key) if key not in {'this_pc', 'recycle'} else None
            if path is not None:
                from jarvis.runtime import context173
                context173.note('folder', path)
            return ToolExecution(True, t('folder', code, name=_pick(_FOLDER_LABELS[key], code)), {'folder': key, 'uri': FOLDERS[key][0]})
        if action == 'create_folder':
            return _create_folder(config, args, code, start)
        if action == 'settings':
            page = str(args.get('page', ''))
            uri = SETTINGS[page][0]
            start(uri)
            label = _SETTINGS_RU.get(page, page.replace('_', ' ')) if code == 'ru' else page.replace('_', ' ')
            return ToolExecution(True, t('settings', code, page=label), {'uri': uri})
        if action == 'steam':
            page = str(args.get('page', ''))
            if page == 'search':
                query = str(args.get('query', '')).strip()
                uri = 'steam://openurl/https://store.steampowered.com/search/?term=' + quote(query)
                label = _pick(_LABELS['search'], code).format(q=query)
            else:
                uri, label = STEAM_PAGES[page][0], _pick(_LABELS[page], code)
            start(uri)
            return ToolExecution(True, t('steam', code, page=label), {'uri': uri})
        if action == 'discord':
            page = str(args.get('page', ''))
            start('discord://-/channels/@me')
            if page == 'settings':
                # Discord's own shortcut for User Settings, once its window is in front.
                sleep(1.5)
                (press or _press)(('ctrl', ','))
            return ToolExecution(True, t('discord', code, page=_pick(_LABELS[page], code)), {'uri': 'discord://-/channels/@me'})
        if action == 'telegram':
            page = str(args.get('page', ''))
            if page == 'need_username':
                return ToolExecution(False, t('need_username', code), {'requires_clarification': True})
            if page == 'settings':
                start('tg://settings')
                return ToolExecution(True, t('telegram', code, page=_pick(_LABELS['settings'], code)), {'uri': 'tg://settings'})
            user = str(args.get('username', '')).lstrip('@')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', user):
                return ToolExecution(False, t('need_username', code), {'requires_clarification': True})
            uri = f'tg://resolve?domain={user}'
            start(uri)
            text = str(args.get('text', '') or '').strip()
            if text:
                sleep(1.5)
                (type_text or _type)(text)          # typed into the message field, not sent
                return ToolExecution(True, t('typed', code, user=user), {'uri': uri, 'typed': True, 'sent': False})
            return ToolExecution(True, t('telegram', code, page=_pick(_LABELS['chat'], code).format(user=user)), {'uri': uri})
        if action == 'browser':
            key = str(args.get('key', ''))
            window = (find_browser or _browser_window)()
            if window is None:
                return ToolExecution(False, t('no_browser', code), {'requires_clarification': True})
            keys = BROWSER_KEYS[key][0]
            if key == 'private' and window.process == 'firefox.exe':
                keys = ('ctrl', 'shift', 'p')
            if key == 'bookmarks' and window.process == 'firefox.exe':
                keys = ('ctrl', 'shift', 'o')
            from jarvis.runtime import screen_targets171 as targets
            targets.bring_to_front(window)
            (press or _press)(keys)
            return ToolExecution(True, t('browser', code, what=_pick(_LABELS[key], code), keys='+'.join(k.capitalize() for k in keys)),
                                 {'keys': list(keys), 'browser': window.process})
    except OSError as exc:
        scheme = {'steam': 'steam://', 'discord': 'discord://', 'telegram': 'tg://'}.get(action, '')
        if scheme:
            return ToolExecution(False, t('not_installed', code, app=action.capitalize(), scheme=scheme), {'not_installed': True, 'error': str(exc)})
        raise
    raise ValueError(f'Unknown apps action: {action}')


def _press(keys: tuple[str, ...]) -> None:
    from jarvis.runtime import input168
    input168.press(input168.KeySpec(tuple(keys)))


def _type(text: str) -> None:
    from jarvis.runtime import input168
    input168.type_text(text, submit=False)


def _create_folder(config: Any, args: dict[str, Any], code: str, start) -> Any:
    from jarvis.tools.registry import ToolExecution
    from jarvis.runtime import context173
    name = str(args.get('name', '')).strip()
    if not name or re.search(r'[\\/:*?"<>|]', name) or name in {'.', '..'}:
        return ToolExecution(False, t('bad_name', code), {'requires_clarification': True})
    key = str(args.get('folder', '') or 'context')
    if key == 'context':
        found = context173.resolve_folder(config)
        base = Path(found.path) if found else known_folder('desktop')
    else:
        base = known_folder(key)
    target = base / name
    existed = target.is_dir()
    target.mkdir(parents=True, exist_ok=True)
    context173.note('folder', target)
    if os.name == 'nt':
        subprocess.Popen(['explorer', '/select,', os.path.normpath(str(target))])
    return ToolExecution(True, t('exists' if existed else 'created', code, name=name, where=str(base)), {'path': str(target), 'created': not existed})


__all__ = ['BROWSER_KEYS', 'FOLDERS', 'SETTINGS', 'STEAM_PAGES', 'known_folder', 'parse', 'run', 't']
