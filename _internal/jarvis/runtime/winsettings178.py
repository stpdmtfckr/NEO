"""NEO 178: every page of Windows Settings, and real actions where Windows allows them.

Pages (the user's screenshots, Windows 11): Главная, Система (Дисплей, Звук, Уведомления,
Фокусировка, Питание, Память, Обмен с устройствами поблизости, Многозадачность, Дополнительно,
Активация, Устранение неполадок, Восстановление, Проецирование на этот компьютер, Удалённый
рабочий стол, Буфер обмена, Системные компоненты, Компоненты ИИ, Дополнительные компоненты,
О системе), Bluetooth и устройства, Сеть и Интернет, Персонализация, Приложения, Учётные
записи, Время и язык, Игры, Специальные возможности, Конфиденциальность и защита, Центр
обновления Windows — «открой …» opens the page.

Actions — only for the user's own account, visible and on the user's command, no
administrator rights, nothing that weakens protection:

* settings of the current user in the registry: dark/light theme, transparency, clipboard
  history, game mode, window snapping, Storage Sense, camera / microphone access;
* power: Windows 11 power mode, screen and sleep timeouts (powercfg for the active plan);
* a switch inside the Settings window by UI Automation (as a user would click it): night
  light, «Не беспокоить», airplane mode, notifications;
* information: about this PC, disks, activation status (read only);
* the clipboard history is cleared through the Windows API; the magnifier, Narrator and the
  on-screen keyboard are started or closed; a picture becomes the wallpaper;
* Windows Update: the page opens and «Проверить наличие обновлений» is pressed.

Never: turning on Remote Desktop, resetting or restoring the PC, entering product keys,
renaming the PC, uninstalling programs, turning off security. These pages are only opened;
the user changes them personally.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _n(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').casefold().replace('ё', 'е')).strip(' .,!?:;¿¡«»"\'')


# ---- pages ------------------------------------------------------------------------------------
# Labels and words follow the Windows 11 page names in each of the seven languages.

@dataclass(frozen=True, slots=True)
class Page:
    uri: str
    labels: tuple[str, ...]    # ru en de fr es it pt
    words: str
    title: bool = False        # the Windows title alone is unambiguous: «открой многозадачность»
    protected: bool = False    # only opened: the user changes it personally


def _P(uri: str, labels: str, words: str, title: bool = False, protected: bool = False) -> Page:
    return Page(uri, tuple(labels.split('|')), words, title, protected)


PAGES: dict[str, Page] = {
    'home': _P('ms-settings:home', 'Главная|Home|Startseite|Accueil|Inicio|Home|Página inicial',
               r'(?:главн\w+(?:\s+страниц\w*)?|home|startseite|accueil|inicio|p[aá]gina\s+inicial)'),
    'system': _P('ms-settings:display', 'Система|System|System|Système|Sistema|Sistema|Sistema', r'(?:систем\w*|system|syst[eè]me|sistema)'),
    'display': _P('ms-settings:display', 'Дисплей|Display|Bildschirm|Écran|Pantalla|Schermo|Ecrã',
                  r'(?:диспле\w*|экран\w*|монитор\w*|display|screen|bildschirm|anzeige|[eé]cran|affichage|pantalla|schermo|ecr[aã]|tela)', True),
    'sound': _P('ms-settings:sound', 'Звук|Sound|Sound|Son|Sonido|Audio|Som', r'(?:звук\w*|sound|audio|ton|son|sonido|suono|som)'),
    'notifications': _P('ms-settings:notifications', 'Уведомления|Notifications|Benachrichtigungen|Notifications|Notificaciones|Notifiche|Notificações',
                        r'(?:уведомлени\w*|notifications?|benachrichtigungen|notificaciones|notifiche|notifica[cç][oõ]es)', True),
    'focus': _P('ms-settings:quiethours', 'Фокусировка|Focus|Fokus|Concentration|Concentración|Concentrazione|Foco',
                r'(?:фокусировк\w*|сеанс\w*\s+фокусировк\w*|focus(?:\s+assist|\s+session)?|fokus|concentration|concentraci[oó]n|concentrazione|foco)', True),
    'power': _P('ms-settings:powersleep', 'Питание|Power|Energie|Alimentation|Energía|Alimentazione|Energia',
                r'(?:питани\w*|электропитани\w*|спящ\w+\s+режим\w*|power(?:\s+(?:&|and)\s+sleep)?|sleep|energie|netzbetrieb|alimentation|energ[ií]a|alimentazione)'),
    'battery': _P('ms-settings:batterysaver', 'Батарея|Battery|Akku|Batterie|Batería|Batteria|Bateria',
                  r'(?:батаре\w*|аккумулятор\w*|экономи\w+\s+заряд\w*|battery(?:\s+saver)?|akku|batterie|bater[ií]a|batteria)'),
    'storage': _P('ms-settings:storagesense', 'Память|Storage|Speicher|Stockage|Almacenamiento|Archiviazione|Armazenamento',
                  r'(?:памят\w*(?:\s+устройств\w*)?|хранилищ\w*|контрол\w+\s+памят\w*|дисков\w+\s+пространств\w*|storage(?:\s+sense)?|speicher|stockage|almacenamiento|archiviazione|armazenamento)', True),
    'nearby': _P('ms-settings:crossdevice', 'Обмен с устройствами поблизости|Nearby sharing|Umgebungsfreigabe|Partage de proximité|Uso compartido en proximidad|Condivisione nelle vicinanze|Partilha nas proximidades',
                 r'(?:обмен\w*\s+с\s+устройствами\s+поблизости|обмен\w*\s+поблизости|nearby\s+sharing|umgebungsfreigabe|partage\s+de\s+proximit[eé]|'
                 r'uso\s+compartido\s+en\s+proximidad|condivisione\s+nelle\s+vicinanze|partilha\s+nas\s+proximidades|compartilhamento\s+por\s+proximidade)', True),
    'multitasking': _P('ms-settings:multitasking', 'Многозадачность|Multitasking|Multitasking|Multitâche|Multitarea|Multitasking|Multitarefa',
                       r'(?:многозадачност\w*|multitasking|multit[aâ]che|multitarea|multitarefa)', True),
    'advanced': _P('ms-settings:developers', 'Дополнительно|Advanced|Erweitert|Avancé|Avanzado|Avanzate|Avançado',
                   r'(?:дополнительно|для\s+разработчиков|режим\w*\s+разработчик\w*|advanced\s+system\s+settings|for\s+developers|developer\s+mode|'
                   r'f[uü]r\s+entwickler|espace\s+d[eé]veloppeurs|para\s+programadores|per\s+sviluppatori)'),
    'activation': _P('ms-settings:activation', 'Активация|Activation|Aktivierung|Activation|Activación|Attivazione|Ativação',
                     r'(?:активаци\w*(?:\s+windows)?|activation|aktivierung|activaci[oó]n|attivazione|ativa[cç][aã]o)', True, True),
    'troubleshoot': _P('ms-settings:troubleshoot', 'Устранение неполадок|Troubleshoot|Problembehandlung|Résolution des problèmes|Solucionar problemas|Risoluzione dei problemi|Resolução de problemas',
                       r'(?:устранени\w+\s+неполадок|средств\w+\s+устранения\s+неполадок|troubleshoot(?:ing|er)?|problembehandlung|r[eé]solution\s+des\s+probl[eè]mes|'
                       r'solucionar\s+problemas|risoluzione\s+(?:dei\s+)?problemi|resolu[cç][aã]o\s+de\s+problemas|solu[cç][aã]o\s+de\s+problemas)', True),
    'recovery': _P('ms-settings:recovery', 'Восстановление|Recovery|Wiederherstellung|Récupération|Recuperación|Ripristino|Recuperação',
                   r'(?:восстановлени\w*(?:\s+(?:windows|системы))?|recovery|wiederherstellung|r[eé]cup[eé]ration|recuperaci[oó]n|ripristino|recupera[cç][aã]o)', True, True),
    'project': _P('ms-settings:project', 'Проецирование на этот компьютер|Projecting to this PC|Projizieren auf diesen PC|Projection sur ce PC|Proyectar en este PC|Proiezione in questo PC|Projetar neste PC',
                  r'(?:проецировани\w*(?:\s+на\s+этот\s+компьютер)?|projecting(?:\s+to\s+this\s+pc)?|projizieren(?:\s+auf\s+diesen\s+pc)?|projection\s+sur\s+ce\s+pc|'
                  r'proyectar\s+en\s+este\s+pc|proiezione\s+(?:in|su)\s+questo\s+pc|projetar\s+neste\s+pc)', True),
    'remote_desktop': _P('ms-settings:remotedesktop', 'Удалённый рабочий стол|Remote Desktop|Remotedesktop|Bureau à distance|Escritorio remoto|Desktop remoto|Ambiente de trabalho remoto',
                         r'(?:удал[её]нн\w+\s+рабоч\w+\s+стол\w*|remote\s+desktop|rdp|remotedesktop|bureau\s+[aà]\s+distance|escritorio\s+remoto|desktop\s+remoto|'
                         r'ambiente\s+de\s+trabalho\s+remoto|[aá]rea\s+de\s+trabalho\s+remota)', True, True),
    'clipboard': _P('ms-settings:clipboard', 'Буфер обмена|Clipboard|Zwischenablage|Presse-papiers|Portapapeles|Appunti|Área de transferência',
                    r'(?:буфер\w*\s+обмена|clipboard|zwischenablage|presse-papiers|portapapeles|appunti|[aá]rea\s+de\s+transfer[eê]ncia)'),
    'system_components': _P('ms-settings:appsfeatures-app', 'Системные компоненты|System components|Systemkomponenten|Composants système|Componentes del sistema|Componenti di sistema|Componentes do sistema',
                            r'(?:системн\w+\s+компонент\w*|system\s+components|systemkomponenten|composants\s+syst[eè]me|componentes\s+del?\s+sistema|componenti\s+di\s+sistema)', True),
    'ai_components': _P('ms-settings:aicomponents', 'Компоненты ИИ|AI components|KI-Komponenten|Composants IA|Componentes de IA|Componenti IA|Componentes de IA',
                        r'(?:компонент\w*\s+(?:ии|искусственного\s+интеллекта)|ai\s+components|ki-komponenten|composants\s+(?:d[\'’])?ia|componentes\s+de\s+ia|componenti\s+(?:di\s+)?ia)', True),
    'optional_features': _P('ms-settings:optionalfeatures', 'Дополнительные компоненты|Optional features|Optionale Features|Fonctionnalités facultatives|Características opcionales|Funzionalità facoltative|Funcionalidades opcionais',
                            r'(?:дополнительн\w+\s+компонент\w*|optional\s+features|optionale\s+features|fonctionnalit[eé]s\s+facultatives|caracter[ií]sticas\s+opcionales|'
                            r'funzionalit[aà]\s+facoltative|funcionalidades\s+opcionais|recursos\s+opcionais)', True),
    'about': _P('ms-settings:about', 'О системе|About|Info|Informations système|Información|Informazioni|Acerca de',
                r'(?:о\s+системе|характеристик\w+(?:\s+(?:компьютера|устройства|windows))?|about(?:\s+this\s+pc)?|info|informations\s+syst[eè]me|'
                r'informaci[oó]n(?:\s+del\s+sistema)?|informazioni(?:\s+di\s+sistema)?|acerca\s+de|sobre)', True),
    'bluetooth': _P('ms-settings:bluetooth', 'Bluetooth и устройства|Bluetooth & devices|Bluetooth und Geräte|Bluetooth et appareils|Bluetooth y dispositivos|Bluetooth e dispositivi|Bluetooth e dispositivos',
                    r'(?:bluetooth(?:\s+(?:и|&|and|und|et|y|e)\s+(?:устройств\w*|devices|ger[aä]te|appareils|dispositivos|dispositivi))?|блютуз\w*|устройств\w*|devices|ger[aä]te|appareils|dispositivos|dispositivi)', True),
    'printers': _P('ms-settings:printers', 'Принтеры и сканеры|Printers & scanners|Drucker und Scanner|Imprimantes et scanners|Impresoras y escáneres|Stampanti e scanner|Impressoras e scanners',
                   r'(?:принтер\w*|сканер\w*|printers?|scanners?|drucker|imprimantes?|impresoras?|stampanti|impressoras?)'),
    'mouse': _P('ms-settings:mousetouchpad', 'Мышь|Mouse|Maus|Souris|Mouse|Mouse|Rato',
                r'(?:мыш\w*|тачпад\w*|сенсорн\w+\s+панел\w*|mouse|touchpad|maus|souris|rat[oó]n|rato)'),
    'network': _P('ms-settings:network-status', 'Сеть и Интернет|Network & internet|Netzwerk und Internet|Réseau et Internet|Red e Internet|Rete e Internet|Rede e Internet',
                  r'(?:сет\w*(?:\s+и\s+интернет\w*)?|интернет\w*|network(?:\s+(?:&|and)\s+internet)?|netzwerk(?:\s+und\s+internet)?|r[eé]seau(?:\s+et\s+internet)?|'
                  r'red(?:\s+e\s+internet)?|rete(?:\s+e\s+internet)?|rede(?:\s+e\s+internet)?)', True),
    'wifi': _P('ms-settings:network-wifi', 'Wi-Fi|Wi-Fi|WLAN|Wi-Fi|Wi-Fi|Wi-Fi|Wi-Fi', r'(?:вай[-\s]?фай\w*|wi[-\s]?fi|wlan)'),
    'vpn': _P('ms-settings:network-vpn', 'VPN|VPN|VPN|VPN|VPN|VPN|VPN', r'(?:впн|vpn)'),
    'airplane': _P('ms-settings:network-airplanemode', 'Режим «в самолёте»|Airplane mode|Flugzeugmodus|Mode avion|Modo avión|Modalità aereo|Modo de avião',
                   r'(?:режим\w*\s+(?:в\s+)?самол[её]т\w*|авиарежим\w*|airplane\s+mode|flug(?:zeug)?modus|mode\s+avion|modo\s+avi[oó]n|modalit[aà]\s+aereo|modo\s+de\s+avi[aã]o)'),
    'personalization': _P('ms-settings:personalization', 'Персонализация|Personalization|Personalisierung|Personnalisation|Personalización|Personalizzazione|Personalização',
                          r'(?:персонализаци\w*|personali[sz]ation|personalisierung|personnalisation|personalizaci[oó]n|personalizzazione|personaliza[cç][aã]o)', True),
    'background': _P('ms-settings:personalization-background', 'Фон|Background|Hintergrund|Arrière-plan|Fondo|Sfondo|Fundo',
                     r'(?:фон\w*(?:\s+рабочего\s+стола)?|обо[иея]\w*|background|wallpaper|hintergrund|arri[eè]re-plan|fond\s+d[\'’][eé]cran|fondo(?:\s+de\s+pantalla)?|sfondo|fundo|papel\s+de\s+parede)'),
    'colors': _P('ms-settings:colors', 'Цвета|Colors|Farben|Couleurs|Colores|Colori|Cores', r'(?:цвет\w*|colou?rs?|farben|couleurs|colores|colori|cores)'),
    'themes': _P('ms-settings:themes', 'Темы|Themes|Designs|Thèmes|Temas|Temi|Temas', r'(?:тем[аыу]?|themes?|designs|th[eè]mes|temas|temi)'),
    'lockscreen': _P('ms-settings:lockscreen', 'Экран блокировки|Lock screen|Sperrbildschirm|Écran de verrouillage|Pantalla de bloqueo|Schermata di blocco|Ecrã de bloqueio',
                     r'(?:экран\w*\s+блокировки|lock\s+screen|sperrbildschirm|[eé]cran\s+de\s+verrouillage|pantalla\s+de\s+bloqueo|schermata\s+di\s+blocco|ecr[aã]\s+de\s+bloqueio|tela\s+de\s+bloqueio)', True),
    'taskbar': _P('ms-settings:taskbar', 'Панель задач|Taskbar|Taskleiste|Barre des tâches|Barra de tareas|Barra delle applicazioni|Barra de tarefas',
                  r'(?:панел\w+\s+задач|taskbar|taskleiste|barre\s+des\s+t[aâ]ches|barra\s+de\s+tareas|barra\s+delle\s+applicazioni|barra\s+de\s+tarefas)', True),
    'start': _P('ms-settings:personalization-start', 'Пуск|Start|Start|Démarrer|Inicio|Start|Iniciar', r'(?:меню\s+пуск|пуск|start\s+menu|startmen[uü]|menu\s+d[eé]marrer|men[uú]\s+inicio|menu\s+start|menu\s+iniciar)'),
    'fonts': _P('ms-settings:fonts', 'Шрифты|Fonts|Schriftarten|Polices|Fuentes|Tipi di carattere|Tipos de letra',
                r'(?:шрифт\w*|fonts?|schriftarten|polices|fuentes|tipi\s+di\s+carattere|tipos\s+de\s+letra)'),
    'apps': _P('ms-settings:appsfeatures', 'Приложения|Apps|Apps|Applications|Aplicaciones|App|Aplicações',
               r'(?:приложени\w*|программ\w*|installed\s+apps|apps|applications|aplicaciones|app|aplica[cç][oõ]es|aplicativos)', True),
    'default_apps': _P('ms-settings:defaultapps', 'Приложения по умолчанию|Default apps|Standard-Apps|Applications par défaut|Aplicaciones predeterminadas|App predefinite|Aplicações predefinidas',
                       r'(?:приложени\w+\s+по\s+умолчанию|default\s+apps|standard-?apps|applications\s+par\s+d[eé]faut|aplicaciones\s+predeterminadas|app\s+predefinite|aplica[cç][oõ]es\s+predefinidas)', True),
    'startup': _P('ms-settings:startupapps', 'Автозагрузка|Startup|Autostart|Démarrage|Inicio|Avvio|Arranque',
                  r'(?:автозагрузк\w*|автозапуск\w*|startup(?:\s+apps)?|autostart|d[eé]marrage|aplicaciones\s+de\s+inicio|app\s+di\s+avvio|arranque)', True),
    'accounts': _P('ms-settings:accounts', 'Учётные записи|Accounts|Konten|Comptes|Cuentas|Account|Contas',
                   r'(?:уч[её]тн\w+\s+запис\w*|аккаунт\w*|accounts?|konten|comptes|cuentas|contas)', True),
    'sign_in': _P('ms-settings:signinoptions', 'Варианты входа|Sign-in options|Anmeldeoptionen|Options de connexion|Opciones de inicio de sesión|Opzioni di accesso|Opções de início de sessão',
                  r'(?:вариант\w+\s+входа|пин-?код\w*|sign-?in\s+options|anmeldeoptionen|options\s+de\s+connexion|opciones\s+de\s+inicio\s+de\s+sesi[oó]n|opzioni\s+di\s+accesso|op[cç][oõ]es\s+de\s+in[ií]cio\s+de\s+sess[aã]o)', True, True),
    'time_language': _P('ms-settings:dateandtime', 'Время и язык|Time & language|Zeit und Sprache|Heure et langue|Hora e idioma|Data/ora e lingua|Hora e idioma',
                        r'(?:врем\w+\s+и\s+язык\w*|дат\w+\s+и\s+врем\w+|time(?:\s+(?:&|and)\s+language)|date\s+(?:&|and)\s+time|zeit\s+und\s+sprache|datum\s+und\s+uhrzeit|'
                        r'heure\s+et\s+langue|date\s+et\s+heure|hora\s+e\s+idioma|fecha\s+y\s+hora|data(?:/|\s+e\s+)ora(?:\s+e\s+lingua)?|data\s+e\s+hora)', True),
    'language': _P('ms-settings:regionlanguage', 'Язык и регион|Language & region|Sprache und Region|Langue et région|Idioma y región|Lingua e area geografica|Idioma e região',
                   r'(?:язык\w*(?:\s+и\s+регион\w*)?|регион\w*|language(?:\s+(?:&|and)\s+region)?|sprache(?:\s+und\s+region)?|langue(?:\s+et\s+r[eé]gion)?|idioma(?:\s+y\s+regi[oó]n|\s+e\s+regi[aã]o)?|lingua)'),
    'typing': _P('ms-settings:typing', 'Ввод|Typing|Eingabe|Saisie|Escritura|Digitazione|Escrita',
                 r'(?:клавиатур\w*|ввод\w*|typing|keyboard|tastatur|eingabe|clavier|saisie|teclado|escritura|tastiera|digitazione|escrita)'),
    'gaming': _P('ms-settings:gaming-gamemode', 'Игры|Gaming|Spielen|Jeux|Juegos|Giochi|Jogos',
                 r'(?:игр\w*|gaming|game\s+mode|spielen|spielmodus|jeux|mode\s+jeu|juegos|modo\s+de\s+juego|giochi|modalit[aà]\s+gioco|jogos|modo\s+de\s+jogo)', True),
    'game_bar': _P('ms-settings:gaming-gamebar', 'Xbox Game Bar|Xbox Game Bar|Xbox Game Bar|Xbox Game Bar|Xbox Game Bar|Xbox Game Bar|Xbox Game Bar',
                   r'(?:(?:xbox\s+)?game\s*bar|игров\w+\s+панел\w*)'),
    'accessibility': _P('ms-settings:easeofaccess', 'Специальные возможности|Accessibility|Barrierefreiheit|Accessibilité|Accesibilidad|Accessibilità|Acessibilidade',
                        r'(?:специальн\w+\s+возможност\w*|accessibility|ease\s+of\s+access|barrierefreiheit|accessibilit[eéà]|accesibilidad|acessibilidade)', True),
    'privacy': _P('ms-settings:privacy', 'Конфиденциальность и защита|Privacy & security|Datenschutz und Sicherheit|Confidentialité et sécurité|Privacidad y seguridad|Privacy e sicurezza|Privacidade e segurança',
                  r'(?:конфиденциальност\w*(?:\s+и\s+защит\w*)?|приватност\w*|privacy(?:\s+(?:&|and|e)\s+(?:security|sicurezza))?|datenschutz(?:\s+und\s+sicherheit)?|'
                  r'confidentialit[eé](?:\s+et\s+s[eé]curit[eé])?|privacidad(?:\s+y\s+seguridad)?|privacidade(?:\s+e\s+seguran[cç]a)?)', True),
    'security': _P('windowsdefender:', 'Безопасность Windows|Windows Security|Windows-Sicherheit|Sécurité Windows|Seguridad de Windows|Sicurezza di Windows|Segurança do Windows',
                   r'(?:безопасност\w+\s+windows|защитник\w*(?:\s+windows)?|антивирус\w*|windows\s+security|defender|windows-sicherheit|s[eé]curit[eé]\s+windows|'
                   r'seguridad\s+de\s+windows|sicurezza\s+di\s+windows|seguran[cç]a\s+do\s+windows)', True, True),
    'camera_access': _P('ms-settings:privacy-webcam', 'Доступ к камере|Camera access|Kamerazugriff|Accès à la caméra|Acceso a la cámara|Accesso alla fotocamera|Acesso à câmara',
                        r'(?:(?:доступ\w*\s+к\s+)?камер\w*|веб-?камер\w*|camera(?:\s+access)?|webcam|kamera(?:zugriff)?|cam[eé]ra|c[aá]mara|fotocamera|c[aâ]mara)'),
    'microphone_access': _P('ms-settings:privacy-microphone', 'Доступ к микрофону|Microphone access|Mikrofonzugriff|Accès au micro|Acceso al micrófono|Accesso al microfono|Acesso ao microfone',
                            r'(?:(?:доступ\w*\s+к\s+)?микрофон\w*|microphone(?:\s+access)?|mikrofon(?:zugriff)?|micro|micr[oó]fono|microfono|microfone)'),
    'location': _P('ms-settings:privacy-location', 'Расположение|Location|Standort|Localisation|Ubicación|Posizione|Localização',
                   r'(?:расположени\w*|геолокаци\w*|местоположени\w*|location|standort|localisation|ubicaci[oó]n|posizione|localiza[cç][aã]o)'),
    'updates': _P('ms-settings:windowsupdate', 'Центр обновления Windows|Windows Update|Windows Update|Windows Update|Windows Update|Windows Update|Windows Update',
                  r'(?:центр\w*\s+обновлени\w*(?:\s+windows)?|обновлени\w*(?:\s+windows)?|windows\s+update|updates?|aktualisierung\w*|mises?\s+[aà]\s+jour|'
                  r'actualizaci\w+|aggiornament\w+|atualiza[cç][oõ]es)', True),
}

_SETTINGS = r'(?:настройк\w*|параметр\w*|settings|einstellungen|param[eè]tres|configuraci[oó]n|ajustes|impostazioni|defini[cç][oõ]es|configura[cç][oõ]es)'
_OPEN = (r'(?:открой|откройте|открыть|покажи|зайди\s+в|перейди\s+в|запусти|open|show(?:\s+me)?|go\s+to|take\s+me\s+to|[oö]ffne|zeig(?:\s+mir)?|geh\s+zu|'
         r'ouvre|affiche|va\s+dans|abre|muestra|ve\s+a|apri|mostra|vai\s+(?:a|in)|abre|mostra|vai\s+para)')
_ON = (r'(?:включи|включить|активируй|разреши|turn\s+on|switch\s+on|enable|allow|activate|schalte?\s+(?:\S+\s+){0,3}?ein|aktiviere|erlaube|'
       r'active|activez|allume|autorise|activa|enciende|permite|attiva|accendi|consenti|ativa|ative|liga|permite)')
_OFF = (r'(?:выключи|выключить|отключи|отключить|деактивируй|запрети|turn\s+off|switch\s+off|disable|deny|block|schalte?\s+(?:\S+\s+){0,3}?aus|deaktiviere|'
        r'blockiere|d[eé]sactive|[eé]teins|bloque|desactiva|apaga|bloquea|disattiva|spegni|blocca|desativa|desative|desliga|bloqueia)')
_START = r'(?:запусти|открой|start|launch|open|starte|[oö]ffne|lance|d[eé]marre|ouvre|inicia|abre|avvia|apri|inicia|abre)'
_CLOSE = r'(?:закрой|останови|выключи|close|stop|quit|exit|schlie(?:ß|ss)e|beende|ferme|arr[eê]te|cierra|det[eé]n|chiudi|ferma|fecha|para)'
_IN_WINDOWS = r'(?:\s+(?:в\s+)?(?:windows|виндовс|винде|настройках|параметрах|in\s+windows|unter\s+windows|dans\s+windows|en\s+windows|in\s+windows|no\s+windows))?'


# ---- phrases → actions ---------------------------------------------------------------------------

_TOGGLES: dict[str, str] = {
    # key → words (the same verbs switch it on and off), seven languages
    'dark_mode': (r'(?:т[её]мн\w+\s+(?:тем\w*|режим\w*|оформлени\w*)|ночн\w+\s+тем\w*|dark\s+(?:mode|theme)|dunkl\w+\s+(?:modus|design)|dunkelmodus|'
                  r'mode\s+sombre|th[eè]me\s+sombre|modo\s+oscuro|tema\s+oscuro|modalit[aà]\s+scura|tema\s+scuro|modo\s+escuro|tema\s+escuro)'),
    'transparency': (r'(?:эффект\w*\s+прозрачност\w*|прозрачност\w*(?:\s+окон)?|transparency(?:\s+effects)?|transparenzeffekte|transparenz|'
                     r'effets\s+de\s+transparence|transparence|efectos\s+de\s+transparencia|transparencia|effetti\s+(?:di\s+)?trasparenza|trasparenza|'
                     r'efeitos\s+de\s+transpar[eê]ncia|transpar[eê]ncia)'),
    'clipboard_history': (r'(?:журнал\w*\s+буфера(?:\s+обмена)?|истори\w+\s+буфера(?:\s+обмена)?|clipboard\s+history|zwischenablageverlauf|verlauf\s+der\s+zwischenablage|'
                          r'historique\s+du\s+presse-papiers|historial\s+del\s+portapapeles|cronologia\s+(?:degli\s+)?appunti|hist[oó]rico\s+da\s+[aá]rea\s+de\s+transfer[eê]ncia)'),
    'game_mode': r'(?:игров\w+\s+режим\w*|game\s+mode|spielmodus|mode\s+jeu|modo\s+(?:de\s+)?juego|modalit[aà]\s+gioco|modo\s+de\s+jogo)',
    'snap': (r'(?:прикреплени\w+\s+окон|привязк\w+\s+окон|snap(?:\s+windows)?|fenster\s+andocken|ancrer\s+les\s+fen[eê]tres|ajustar\s+ventanas|'
             r'ancoraggio\s+(?:delle\s+)?finestre|ajustar\s+janelas)'),
    'storage_sense': r'(?:контрол\w+\s+памяти|storage\s+sense|speicheroptimierung|assistant\s+de\s+stockage|sensor\s+de\s+almacenamiento|sensore\s+memoria|sensor\s+de\s+armazenamento)',
    'camera_access': (r'(?:доступ\w*\s+к\s+камер\w*|камер\w+\s+для\s+приложений|camera\s+access|kamerazugriff|acc[eè]s\s+[aà]\s+la\s+cam[eé]ra|acceso\s+a\s+la\s+c[aá]mara|'
                      r'accesso\s+alla\s+(?:fotocamera|webcam)|acesso\s+[aà]\s+c[aâ]mara)'),
    'microphone_access': (r'(?:доступ\w*\s+к\s+микрофон\w*|микрофон\w*\s+для\s+приложений|microphone\s+access|mikrofonzugriff|acc[eè]s\s+au\s+micro(?:phone)?|'
                          r'acceso\s+al\s+micr[oó]fono|accesso\s+al\s+microfono|acesso\s+ao\s+microfone)'),
    'location': (r'(?:геолокаци\w*|определени\w+\s+местоположени\w*|служб\w+\s+(?:определения\s+)?(?:расположени|местоположени)\w*|location(?:\s+services)?|'
                 r'standort(?:dienste)?|localisation|ubicaci[oó]n|posizione|localiza[cç][aã]o)'),
    'night_light': r'(?:ночн\w+\s+свет\w*|night\s+light|nachtmodus|nachtlicht|[eé]clairage\s+nocturne|luz\s+nocturna|luce\s+notturna|luz\s+noturna)',
    'do_not_disturb': (r'(?:режим\w*\s+)?(?:не\s+беспокоить|do\s+not\s+disturb|dnd|nicht\s+st[oö]ren|ne\s+pas\s+d[eé]ranger|no\s+molestar|non\s+disturbare|'
                       r'n[aã]o\s+incomodar|n[aã]o\s+perturbar)'),
    'airplane': (r'(?:режим\w*\s+(?:в\s+)?самол[её]т\w*|авиарежим\w*|airplane\s+mode|flight\s+mode|flug(?:zeug)?modus|mode\s+avion|modo\s+avi[oó]n|'
                 r'modalit[aà]\s+aereo|modo\s+de\s+avi[aã]o)'),
    'notifications': r'(?:все\s+)?уведомлени\w*|(?:all\s+)?notifications|benachrichtigungen|(?:les\s+)?notifications|(?:las\s+)?notificaciones|(?:le\s+)?notifiche|(?:as\s+)?notifica[cç][oõ]es',
    'magnifier': r'(?:экранн\w+\s+луп\w*|луп\w+|magnifier|bildschirmlupe|lupe|loupe|lupa|lente\s+di\s+ingrandimento)',
    'narrator': r'(?:экранн\w+\s+диктор\w*|диктор\w*|narrator|windows-sprachausgabe|narrateur|narrador|assistente\s+vocale)',
    'osk': (r'(?:экранн\w+\s+клавиатур\w*|on-?screen\s+keyboard|bildschirmtastatur|clavier\s+visuel|teclado\s+en\s+pantalla|tastiera\s+(?:su\s+schermo|virtuale)|'
            r'teclado\s+no\s+ecr[aã]|teclado\s+virtual)'),
}
_LIGHT = r'(?:светл\w+\s+(?:тем\w*|режим\w*|оформлени\w*)|light\s+(?:mode|theme)|hell\w+\s+(?:modus|design)|mode\s+clair|th[eè]me\s+clair|modo\s+claro|tema\s+claro|modalit[aà]\s+chiara|tema\s+chiaro)'
_POWER_MODES = {
    'best_performance': (r'(?:максимальн\w+\s+производительност\w*|высок\w+\s+производительност\w*|производительн\w+\s+режим\w*|best\s+performance|high\s+performance|'
                         r'beste\s+leistung|h[oö]chstleistung|meilleures?\s+performances?|mejor\s+rendimiento|massime\s+prestazioni|melhor\s+desempenho|melhor\s+performance)'),
    'balanced': r'(?:сбалансированн\w*|баланс\w*|balanced|ausbalanciert|[eé]quilibr[eé]|equilibrado|bilanciat[oa]|equilibrad[oa])',
    'efficiency': (r'(?:энергосбережени\w*|экономи\w+\s+энерги\w*|максимальн\w+\s+экономи\w+|best\s+power\s+efficiency|power\s+saving|energy\s+saver|'
                   r'beste\s+energieeffizienz|energiesparen|[eé]conomie\s+d[\'’][eé]nergie|meilleure\s+efficacit[eé]|ahorro\s+de\s+energ[ií]a|'
                   r'risparmio\s+energetico|poupan[cç]a\s+de\s+energia|economia\s+de\s+energia)'),
}
_MODE_WORD = r'(?:режим|mode|питани|производительн|энергосбереж|performance|balanced|efficien|energie|leistung|modo|modalit|rendimiento|prestazioni|desempenho|energia|energ[ií]a|[eé]quilibr|equilibr|bilanc)'
_MINUTES = r'(?P<n>\d{1,3})\s*(?:минут\w*|мин\.?|minutes?|mins?|minuten|min\.?|minutos?|minuti)'
_HOURS = r'(?P<h>\d{1,2})\s*(?:час\w*|hours?|stunden?|heures?|horas?|ore|ora)'
_NEVER = r'(?<!\w)(?:никогда|не\s+выключай|не\s+засыпай|never|nie(?:mals)?|jamais|nunca|mai)(?!\w)'
_ONE_HOUR = r'(?:через\s+час|after\s+an?\s+hour|in\s+an?\s+hour|nach\s+einer\s+stunde|apr[eè]s\s+une\s+heure|despu[eé]s\s+de\s+una\s+hora|dopo\s+un[\'’]?\s*ora|depois\s+de\s+uma\s+hora)'
_SCREEN_OFF = (r'(?:(?:выключа\w+|отключа\w+|гас\w+)\s+(?:экран|дисплей|монитор)|экран\w*\s+(?:выключ|отключ|гас)\w*|turn\s+off\s+(?:the\s+)?(?:screen|display)|'
               r'(?:screen|display)\s+(?:off|timeout)|bildschirm\s+(?:aus(?:schalten)?|abschalten)|[eé]teindre\s+l[\'’][eé]cran|[eé]cran\s+[eé]teint|'
               r'apagar\s+(?:la\s+)?pantalla|apaga\s+(?:la\s+)?pantalla|spegni(?:re)?\s+(?:lo\s+)?schermo|desligar\s+(?:o\s+)?ecr[aã]|desliga\s+(?:o\s+)?ecr[aã]|desligar\s+a\s+tela)')
_SLEEP = (r'(?:(?:переходи|уходи|засыпай|отправляй|усыпляй)\s+(?:в\s+)?(?:сон|спящ\w+\s+режим)?|(?:спящ\w+\s+режим|сон)\s+через|никогда\s+не\s+засыпай|не\s+засыпай|'
          r'sleep\s+after|go\s+to\s+sleep\s+after|never\s+sleep|energiesparmodus\s+nach|ruhezustand\s+nach|veille\s+apr[eè]s|mise\s+en\s+veille|suspender\s+despu[eé]s|'
          r'suspensi[oó]n|sospensione\s+dopo|sospendi\s+dopo|suspender\s+depois|suspens[aã]o)')
_ABOUT = (r'(?:(?:расскажи|покажи|скажи)\s+)?(?:о\s+(?:моей\s+)?системе|характеристик\w+\s+(?:моего\s+)?(?:компьютера|пк|системы)|какая\s+(?:у\s+меня\s+)?(?:версия|сборка)\s+windows|'
          r'какой\s+у\s+меня\s+windows|какой\s+у\s+меня\s+(?:процессор|компьютер)|сколько\s+(?:у\s+меня\s+)?оперативн\w+\s+памяти|'
          r'what\s+windows\s+version(?:\s+do\s+i\s+have)?|which\s+windows\s+(?:do\s+i\s+have|is\s+this)|(?:tell\s+me\s+)?about\s+(?:this|my)\s+(?:pc|computer)|(?:my\s+)?pc\s+specs|'
          r'(?:what\s+are\s+)?my\s+(?:pc|computer)\s+specs|welche\s+windows-?version(?:\s+habe\s+ich)?|systeminfo(?:rmationen)?|infos?\s+(?:zu|über)\s+(?:meinem|diesem)\s+pc|'
          r'quelle\s+version\s+de\s+windows(?:\s+ai-je)?|infos?\s+(?:sur\s+)?(?:mon|ce)\s+pc|caract[eé]ristiques\s+(?:de\s+)?(?:mon|ce)\s+pc|'
          r'qu[eé]\s+versi[oó]n\s+de\s+windows\s+tengo|informaci[oó]n\s+(?:de|del|sobre)\s+(?:mi|este)\s+(?:pc|equipo)|caracter[ií]sticas\s+de\s+mi\s+pc|'
          r'che\s+versione\s+di\s+windows\s+(?:ho|uso)|informazioni\s+(?:sul|su\s+questo)\s+pc|caratteristiche\s+del\s+(?:mio\s+)?pc|'
          r'que\s+vers[aã]o\s+do\s+windows\s+tenho|informa[cç][oõ]es\s+(?:do|sobre\s+o)\s+(?:meu\s+)?pc|especifica[cç][oõ]es\s+do\s+(?:meu\s+)?pc)')
_STORAGE = (r'(?:сколько\s+(?:свободного\s+)?места(?:\s+(?:на\s+)?(?:диск\w*|компьютер\w*))?|свободное\s+место(?:\s+на\s+диск\w*)?|сколько\s+осталось\s+места|'
            r'(?:how\s+much\s+)?(?:free\s+)?(?:disk\s+)?space(?:\s+(?:is\s+)?left)?(?:\s+on\s+(?:my\s+)?(?:disks?|drives?|pc))?|'
            r'wie\s+viel\s+(?:freier\s+)?speicher(?:platz)?(?:\s+(?:ist\s+)?(?:noch\s+)?frei)?|freier\s+speicher(?:platz)?|'
            r'combien\s+d[\'’]espace(?:\s+libre)?(?:\s+(?:reste-t-il|sur\s+le\s+disque))?|espace\s+(?:disque\s+)?libre|'
            r'cu[aá]nto\s+espacio(?:\s+libre)?(?:\s+(?:queda|hay))?|espacio\s+libre(?:\s+en\s+(?:el\s+)?disco)?|'
            r'quanto\s+spazio(?:\s+libero)?(?:\s+(?:resta|c[\'’]?[eè]))?|spazio\s+libero(?:\s+su\s+disco)?|'
            r'quanto\s+espa[cç]o(?:\s+livre)?(?:\s+(?:resta|h[aá]))?|espa[cç]o\s+livre(?:\s+no\s+disco)?)')
_ACTIVATION = (r'(?:активирован\w*\s+ли\s+windows|windows\s+активирован\w*\??|проверь\s+активаци\w+(?:\s+windows)?|статус\s+активации(?:\s+windows)?|'
               r'is\s+(?:my\s+)?windows\s+activated|(?:check\s+)?(?:windows\s+)?activation\s+status|ist\s+windows\s+aktiviert|aktivierungsstatus|'
               r'windows\s+est-il\s+activ[eé]|[eé]tat\s+(?:de\s+l[\'’])?activation|est[aá]\s+activado\s+windows|estado\s+de\s+(?:la\s+)?activaci[oó]n|'
               r'windows\s+[eè]\s+attivato|stato\s+(?:di\s+|dell[\'’])?attivazione|o\s+windows\s+est[aá]\s+ativado|estado\s+da\s+ativa[cç][aã]o)')
_UPDATES = (r'(?:(?:проверь|поищи|найди|есть\s+ли)\s+обновлени\w*(?:\s+windows)?|check\s+(?:for\s+)?(?:windows\s+)?updates?|are\s+there\s+(?:any\s+)?updates|'
            r'(?:suche\s+nach|pr[uü]fe(?:\s+auf)?)\s+updates|nach\s+updates\s+suchen|(?:recherche|v[eé]rifie)\s+(?:les\s+)?mises?\s+[aà]\s+jour|'
            r'(?:busca|comprueba)\s+(?:las\s+)?actualizaciones|(?:cerca|controlla)\s+(?:gli\s+)?aggiornamenti|(?:procura|verifica)\s+(?:as\s+)?atualiza[cç][oõ]es)')
_CLEAR_CLIP = (r'(?:(?:очисти|удали|сотри)\s+(?:журнал|истори\w+)\s+буфера(?:\s+обмена)?|clear\s+(?:the\s+)?clipboard\s+history|'
               r'(?:l[oö]sche|leere)\s+(?:den\s+)?zwischenablageverlauf|(?:efface|vide)\s+l[\'’]historique\s+du\s+presse-papiers|'
               r'(?:borra|limpia)\s+el\s+historial\s+del\s+portapapeles|(?:cancella|svuota)\s+(?:la\s+)?cronologia\s+(?:degli\s+)?appunti|'
               r'(?:limpa|apaga)\s+o\s+hist[oó]rico\s+da\s+[aá]rea\s+de\s+transfer[eê]ncia)')
_WALLPAPER = (r'(?:поставь|установи|сделай|set|mach\w*|setze|mets|d[eé]finis|pon|establece|imposta|metti|p[oõ]e|define)\s+(?P<what>.+?)\s+'
              r'(?:на\s+обои|обоями|на\s+рабочий\s+стол|фоном(?:\s+рабочего\s+стола)?|as\s+(?:the\s+|my\s+)?(?:wallpaper|desktop\s+background|background)|'
              r'als\s+(?:hintergrund(?:bild)?|desktophintergrund)|en\s+fond\s+d[\'’][eé]cran|comme\s+fond\s+d[\'’][eé]cran|como\s+(?:fondo\s+de\s+pantalla|fondo)|'
              r'come\s+sfondo|como\s+(?:papel\s+de\s+parede|fundo))')


def _when(text: str) -> int | None:
    """Minutes in «через 10 минут / через час / никогда»; None when no time is said."""
    if re.search(_NEVER, text):
        return 0
    match = re.search(_MINUTES, text)
    if match:
        return int(match.group('n'))
    match = re.search(_HOURS, text)
    if match:
        return int(match.group('h')) * 60
    if re.search(_ONE_HOUR, text):
        return 60
    return None


_POLITE = (r'^(?:(?:пожалуйста|please|bitte|s[\'’]il\s+te\s+pla[iî]t|s[\'’]il\s+vous\s+pla[iî]t|por\s+favor|per\s+favore|можешь|можете|could\s+you|can\s+you|'
           r'kannst\s+du|peux-tu|puedes|puoi|podes|нео|neo)[\s,]+)+')


def parse(text: str, code: str = 'ru') -> tuple[str, dict[str, Any]] | None:
    """('winsettings', arguments) for a Windows Settings request, or None."""
    t = re.sub(_POLITE, '', _n(text))
    t = re.sub(r'(?:[\s,]+(?:пожалуйста|please|bitte|por\s+favor|per\s+favore|s[\'’]il\s+te\s+pla[iî]t))+$', '', t)
    if not t or len(t.split()) > 14:
        return None
    # Word order variants: «schalte den Nachtmodus ein», «turn the night light on», «Soundeinstellungen»
    t = re.sub(r'^schalte?\s+(.+?)\s+(?:ein|an)$', r'aktiviere \1', t)
    t = re.sub(r'^schalte?\s+(.+?)\s+aus$', r'deaktiviere \1', t)
    t = re.sub(r'^(?:turn|switch)\s+(.+?)\s+on$', r'turn on \1', t)
    t = re.sub(r'^(?:turn|switch)\s+(.+?)\s+off$', r'turn off \1', t)
    t = re.sub(r'\b([a-zäöüß]+?)-?einstellungen\b', r'einstellungen \1', t)
    t = re.sub(r'^stelle?\s+(?:den\s+)?energiemodus\s+auf\s+', 'energiemodus auf ', t)
    t = re.sub(r'^(?:переключи(?:сь)?\s+на|поставь|сделай)\s+(?=т[её]мн|светл)', 'включи ', t)
    if re.fullmatch(r'(?:не\s+беспокой(?:те)?\s+меня|режим\s+не\s+беспокоить|do\s+not\s+disturb(?:\s+me)?|bitte\s+nicht\s+st[oö]ren|ne\s+me\s+d[eé]range\s+pas|no\s+me\s+molestes|non\s+disturbarmi|n[aã]o\s+me\s+incomodes)', t):
        return 'winsettings', {'action': 'toggle', 'what': 'do_not_disturb', 'enabled': True}
    if re.fullmatch(_ABOUT, t):
        return 'winsettings', {'action': 'about'}
    if re.fullmatch(_STORAGE, t):
        return None             # «свободное место на диске» is the file tool's disk report (filesystem.disk_usage)
    if re.fullmatch(_ACTIVATION, t):
        return 'winsettings', {'action': 'activation_status'}
    if re.fullmatch(_UPDATES, t):
        return 'winsettings', {'action': 'check_updates'}
    if re.fullmatch(_CLEAR_CLIP, t):
        return 'winsettings', {'action': 'clear_clipboard_history'}
    wall = re.fullmatch(_WALLPAPER, t)
    if wall:
        return 'winsettings', {'action': 'wallpaper', 'what': wall.group('what')}
    # Light theme: «включи светлую тему», «выключи тёмную тему»
    if re.fullmatch(rf'(?:{_ON}\s+)?{_LIGHT}{_IN_WINDOWS}', t):
        return 'winsettings', {'action': 'toggle', 'what': 'dark_mode', 'enabled': False}
    # Power mode: «режим питания максимальная производительность», «включи режим энергосбережения»
    for mode, words in _POWER_MODES.items():
        if re.fullmatch(rf'(?:(?:{_ON}|поставь|переключи(?:сь)?\s+на|сделай|set|switch\s+to|use|stelle?\s+auf|wechsle\s+zu|passe\s+en|mets|pon|cambia\s+a|imposta|passa\s+a|p[oõ]e|muda\s+para)\s+)?'
                        rf'(?:(?:режим\w*\s+(?:питани\w+\s+)?(?:на\s+)?)|(?:the\s+)?power\s+mode\s+(?:to\s+)?|energiemodus\s+(?:auf\s+)?|(?:le\s+)?mode\s+(?:d[\'’]alimentation\s+)?(?:sur\s+)?|'
                        rf'(?:el\s+)?modo\s+(?:de\s+energ[ií]a\s+)?(?:en\s+)?|(?:la\s+)?modalit[aà]\s+(?:di\s+alimentazione\s+)?(?:su\s+)?|(?:o\s+)?modo\s+de\s+energia\s+(?:para\s+)?)?'
                        rf'{words}(?:\s+(?:режим\w*(?:\s+питания)?|(?:power\s+)?mode|(?:energie)?modus|modo(?:\s+de\s+energ[ií]a)?|modalit[aà]))?', t) and re.search(_MODE_WORD, t):
            return 'winsettings', {'action': 'power_mode', 'mode': mode}
    if re.search(_SCREEN_OFF, t):
        minutes = _when(t)
        if minutes is not None:
            return 'winsettings', {'action': 'screen_timeout', 'minutes': minutes}
    if re.search(_SLEEP, t):
        minutes = _when(t)
        if minutes is not None:
            return 'winsettings', {'action': 'sleep_timeout', 'minutes': minutes}
    # Accessibility programs: «запусти экранную лупу», «закрой экранную клавиатуру»
    for key in ('magnifier', 'narrator', 'osk'):
        match = re.fullmatch(rf'(?P<verb>{_START}|{_CLOSE}|{_ON}|{_OFF})\s+(?:the\s+|la\s+|le\s+|el\s+|il\s+|o\s+|a\s+|die\s+|den\s+)?(?:{_TOGGLES[key]})', t)
        if match:
            closing = bool(re.fullmatch(rf'{_CLOSE}|{_OFF}', match.group('verb')))
            return 'winsettings', {'action': 'toggle', 'what': key, 'enabled': not closing}
    # Switches: «включи ночной свет», «выключи режим не беспокоить», «отключи доступ к камере», «enable dark mode»
    on, off = re.match(_ON + r'\b', t), re.match(_OFF + r'\b', t)
    if on or off:
        rest = t[(on or off).end():].strip()
        rest = re.sub(r'^(?:режим\s+(?=не\s+беспокоить)|функци\w+\s+|опци\w+\s+|the\s+|den\s+|die\s+|das\s+|le\s+|la\s+|les\s+|l[\'’]|el\s+|los\s+|las\s+|il\s+|lo\s+|i\s+|gli\s+|o\s+|a\s+|os\s+|as\s+)', '', rest)
        rest = re.sub(r'\s+(?:ein|aus|an)$', '', rest)
        for key, words in _TOGGLES.items():
            if key in {'magnifier', 'narrator', 'osk'}:
                continue
            if re.fullmatch(rf'(?:{words}){_IN_WINDOWS}', rest):
                return 'winsettings', {'action': 'toggle', 'what': key, 'enabled': bool(on)}
        if re.fullmatch(PAGES['remote_desktop'].words, rest):
            return 'winsettings', {'action': 'open', 'page': 'remote_desktop', 'refused': 'remote_desktop'}
    # «тёмная тема» / «dark mode on»
    for key in ('dark_mode', 'night_light', 'do_not_disturb'):
        state = re.fullmatch(rf'(?:{_TOGGLES[key]})\s+(?P<s>вкл\w*|выкл\w*|on|off|an|aus|ein|activ\w*|d[eé]sactiv\w*|attiv\w*|disattiv\w*|ligad\w*|desligad\w*)', t)
        if state:
            enabled = not re.match(r'(?:выкл|off|aus|d[eé]sactiv|disattiv|desligad)', state.group('s'))
            return 'winsettings', {'action': 'toggle', 'what': key, 'enabled': enabled}
    # Pages
    art = r'(?:(?:the|die|den|les|la|le|las|los|el|il|i|as|os|o|a)\s+|l[\'’])?'
    link = r'(?:(?:de|du|des|di|do|da|del|della|dello|dos|das|für|for|of|von|der)\s+|d[\'’])?(?:(?:la|le|el|il|o|a|the|den|der|die)\s+|l[\'’])?'
    for key, page in PAGES.items():
        if key == 'system':
            continue            # «настройки системы» opens Settings itself (system.open_settings), as before
        words = page.words
        if re.fullmatch(rf'(?:{_OPEN}\s+)?{art}(?:{_SETTINGS}\s+(?:windows\s+)?{link}(?:раздел\w*\s+)?{words}|(?:раздел\w*\s+)?{words}\s+(?:в\s+|in\s+|dans\s+|en\s+|nas\s+|nelle\s+)?{art}{_SETTINGS}(?:\s+windows)?)', t):
            return 'winsettings', {'action': 'open', 'page': key}
        if page.title and re.fullmatch(rf'{_OPEN}\s+{art}(?:раздел\w*\s+)?{words}(?:\s+windows)?', t) and _title_only_ok(key, t):
            return 'winsettings', {'action': 'open', 'page': key}
    return None


def _title_only_ok(key: str, t: str) -> bool:
    """Bare «открой X» only for names that cannot mean a program, a file or a folder."""
    if key == 'about':
        return not re.search(r'(?:\binfo|\bsobre)$', t)          # «открой о системе», but not a bare «info»
    if key == 'focus':
        return bool(re.search(r'фокусировк|focus\s+(?:assist|session)', t))   # not a bare «focus»
    if key == 'storage':
        return bool(re.search(r'памят\w*\s+устройств|хранилищ|storage\s+sense|контрол', t))
    if key in {'display', 'apps', 'gaming', 'accounts'}:
        return False
    if key in {'bluetooth', 'network', 'time_language'}:
        return bool(re.search(r'(?:\s+(?:и|&|and|und|et|y|e)\s+)', t))       # the full Windows title
    return True


# ---- replies ---------------------------------------------------------------------------------------

_TEXT: dict[str, tuple[str, ...]] = {
    'opened': ('Открыл параметры Windows: {page}.', 'Opened Windows Settings: {page}.', 'Windows-Einstellungen geöffnet: {page}.',
               'Paramètres Windows ouverts : {page}.', 'Abrí la Configuración de Windows: {page}.', 'Aperte le Impostazioni di Windows: {page}.',
               'Abri as Definições do Windows: {page}.'),
    'protected': ('Открыл «{page}». Здесь меняйте сами: это влияет на защиту и данные компьютера, поэтому NEO такие переключатели не трогает.',
                  'Opened “{page}”. Please change it yourself: it affects the PC’s protection and data, so NEO does not touch these switches.',
                  '„{page}“ geöffnet. Bitte selbst ändern: Es betrifft Schutz und Daten des PCs, deshalb ändert NEO diese Schalter nicht.',
                  '« {page} » ouvert. Modifiez-le vous-même : cela touche la protection et les données du PC, NEO ne change pas ces réglages.',
                  'Abrí «{page}». Cámbialo tú: afecta a la protección y los datos del PC, por eso NEO no toca estos ajustes.',
                  'Aperto «{page}». Modificalo tu: riguarda protezione e dati del PC, perciò NEO non tocca queste impostazioni.',
                  'Abri «{page}». Altera tu: afeta a proteção e os dados do PC, por isso o NEO não mexe nestas opções.'),
    'remote_desktop': ('Удалённый рабочий стол открывает компьютер для входа по сети, поэтому включать его NEO не будет. Открыл страницу — '
                       'если он вам нужен, включите его сами (Windows попросит права администратора).',
                       'Remote Desktop opens the PC to sign-ins over the network, so NEO won’t turn it on. I opened the page — '
                       'turn it on yourself if you need it (Windows asks for administrator rights).',
                       'Remotedesktop öffnet den PC für Anmeldungen übers Netzwerk, daher schaltet NEO ihn nicht ein. Seite geöffnet – schalte ihn selbst ein, falls nötig.',
                       'Le Bureau à distance ouvre le PC aux connexions réseau, NEO ne l’active donc pas. Page ouverte — activez-le vous-même si besoin.',
                       'El Escritorio remoto abre el PC a inicios de sesión por red, así que NEO no lo activará. Abrí la página: actívalo tú si lo necesitas.',
                       'Il Desktop remoto apre il PC agli accessi in rete, quindi NEO non lo attiva. Pagina aperta: attivalo tu se serve.',
                       'O Ambiente de trabalho remoto abre o PC a inícios de sessão pela rede, por isso o NEO não o liga. Abri a página — liga-o tu se precisares.'),
    'on': ('Включил: {what}.', 'Turned on: {what}.', 'Eingeschaltet: {what}.', 'Activé : {what}.', 'Activado: {what}.', 'Attivato: {what}.', 'Ativado: {what}.'),
    'off': ('Выключил: {what}.', 'Turned off: {what}.', 'Ausgeschaltet: {what}.', 'Désactivé : {what}.', 'Desactivado: {what}.', 'Disattivato: {what}.', 'Desativado: {what}.'),
    'already_on': ('«{what}» уже включено.', '“{what}” is already on.', '„{what}“ ist bereits an.', '« {what} » est déjà activé.', '«{what}» ya está activado.',
                   '«{what}» è già attivo.', '«{what}» já está ativado.'),
    'already_off': ('«{what}» уже выключено.', '“{what}” is already off.', '„{what}“ ist bereits aus.', '« {what} » est déjà désactivé.', '«{what}» ya está desactivado.',
                    '«{what}» è già disattivato.', '«{what}» já está desativado.'),
    'toggle_failed': ('Не получилось переключить «{what}» автоматически — открыл нужную страницу параметров, переключите там.',
                      'Couldn’t switch “{what}” automatically — I opened the Settings page; switch it there.',
                      '„{what}“ ließ sich nicht automatisch umschalten – die Einstellungsseite ist offen, schalte es dort um.',
                      'Impossible de basculer « {what} » automatiquement — la page des paramètres est ouverte, basculez-le là.',
                      'No pude cambiar «{what}» automáticamente: abrí la página de Configuración, cámbialo allí.',
                      'Non sono riuscito a cambiare «{what}» automaticamente: ho aperto la pagina delle Impostazioni, cambialo lì.',
                      'Não consegui mudar «{what}» automaticamente — abri a página das Definições, muda lá.'),
    'power_mode': ('Режим питания: {mode}.', 'Power mode: {mode}.', 'Energiemodus: {mode}.', 'Mode d’alimentation : {mode}.', 'Modo de energía: {mode}.',
                   'Modalità di alimentazione: {mode}.', 'Modo de energia: {mode}.'),
    'power_failed': ('Windows не дал сменить это из NEO — открыл «Питание», выберите там.', 'Windows didn’t let NEO change it — I opened Power; choose it there.',
                     'Windows hat die Änderung nicht erlaubt – „Energie“ ist offen, wähle es dort.', 'Windows n’a pas permis la modification — « Alimentation » est ouvert, choisissez-le là.',
                     'Windows no permitió el cambio: abrí «Energía», elígelo allí.', 'Windows non ha consentito la modifica: ho aperto «Alimentazione», sceglilo lì.',
                     'O Windows não permitiu a alteração — abri «Energia», escolhe lá.'),
    'screen_timeout': ('Экран будет выключаться через {when} (от сети и от батареи).', 'The screen will turn off after {when} (plugged in and on battery).',
                       'Der Bildschirm schaltet sich nach {when} aus (Netz und Akku).', 'L’écran s’éteindra après {when} (sur secteur et sur batterie).',
                       'La pantalla se apagará tras {when} (con corriente y con batería).', 'Lo schermo si spegnerà dopo {when} (alimentazione e batteria).',
                       'O ecrã desliga-se após {when} (na corrente e na bateria).'),
    'sleep_timeout': ('Компьютер будет засыпать через {when} (от сети и от батареи).', 'The PC will sleep after {when} (plugged in and on battery).',
                      'Der PC wechselt nach {when} in den Energiesparmodus (Netz und Akku).', 'Le PC se mettra en veille après {when} (secteur et batterie).',
                      'El PC se suspenderá tras {when} (con corriente y con batería).', 'Il PC andrà in sospensione dopo {when} (alimentazione e batteria).',
                      'O PC suspende após {when} (na corrente e na bateria).'),
    'never': ('никогда', 'never', 'nie', 'jamais', 'nunca', 'mai', 'nunca'),
    'minutes': ('{n} мин', '{n} min', '{n} Min.', '{n} min', '{n} min', '{n} min', '{n} min'),
    'clipboard_cleared': ('Журнал буфера обмена очищен (закреплённые элементы Windows оставляет).', 'Clipboard history cleared (Windows keeps pinned items).',
                          'Zwischenablageverlauf gelöscht (angeheftete Elemente behält Windows).', 'Historique du presse-papiers effacé (Windows garde les éléments épinglés).',
                          'Historial del portapapeles borrado (Windows conserva los elementos anclados).', 'Cronologia degli appunti cancellata (Windows mantiene gli elementi aggiunti).',
                          'Histórico da área de transferência limpo (o Windows mantém os itens afixados).'),
    'clipboard_failed': ('Не получилось очистить журнал из NEO — открыл «Буфер обмена», нажмите «Очистить».', 'Couldn’t clear the history from NEO — I opened Clipboard; press “Clear”.',
                         'Verlauf ließ sich nicht löschen – „Zwischenablage“ ist offen, klicke „Löschen“.', 'Impossible d’effacer — « Presse-papiers » est ouvert, cliquez « Effacer ».',
                         'No pude borrar el historial: abrí «Portapapeles», pulsa «Borrar».', 'Non sono riuscito a cancellare: ho aperto «Appunti», premi «Cancella».',
                         'Não consegui limpar — abri «Área de transferência», carrega em «Limpar».'),
    'updates': ('Открыл Центр обновления Windows и нажал «Проверить наличие обновлений».', 'Opened Windows Update and pressed “Check for updates”.',
                'Windows Update geöffnet und „Nach Updates suchen“ gedrückt.', 'Windows Update ouvert et « Rechercher des mises à jour » lancé.',
                'Abrí Windows Update y pulsé «Buscar actualizaciones».', 'Aperto Windows Update e premuto «Controlla aggiornamenti».',
                'Abri o Windows Update e carreguei em «Procurar atualizações».'),
    'updates_open': ('Открыл Центр обновления Windows — нажмите «Проверить наличие обновлений».', 'Opened Windows Update — press “Check for updates”.',
                     'Windows Update geöffnet – klicke „Nach Updates suchen“.', 'Windows Update ouvert — cliquez « Rechercher des mises à jour ».',
                     'Abrí Windows Update: pulsa «Buscar actualizaciones».', 'Aperto Windows Update: premi «Controlla aggiornamenti».',
                     'Abri o Windows Update — carrega em «Procurar atualizações».'),
    'wallpaper': ('Поставил на обои: {name}.', 'Set as wallpaper: {name}.', 'Als Hintergrund gesetzt: {name}.', 'Défini comme fond d’écran : {name}.',
                  'Puesto como fondo: {name}.', 'Impostato come sfondo: {name}.', 'Definido como fundo: {name}.'),
    'wallpaper_which': ('Какую картинку поставить? Выделите её в Проводнике или скажите имя файла.', 'Which picture? Select it in File Explorer or say the file name.',
                        'Welches Bild? Markiere es im Explorer oder nenne den Dateinamen.', 'Quelle image ? Sélectionnez-la dans l’Explorateur ou dites le nom du fichier.',
                        '¿Qué imagen? Selecciónala en el Explorador o di el nombre del archivo.', 'Quale immagine? Selezionala in Esplora file o di’ il nome del file.',
                        'Que imagem? Seleciona-a no Explorador ou diz o nome do ficheiro.'),
    'not_image': ('«{name}» — не картинка (нужен .jpg, .png, .bmp или .webp).', '“{name}” is not a picture (.jpg, .png, .bmp or .webp).',
                  '„{name}“ ist kein Bild (.jpg, .png, .bmp oder .webp).', '« {name} » n’est pas une image (.jpg, .png, .bmp ou .webp).',
                  '«{name}» no es una imagen (.jpg, .png, .bmp o .webp).', '«{name}» non è un’immagine (.jpg, .png, .bmp o .webp).',
                  '«{name}» não é uma imagem (.jpg, .png, .bmp ou .webp).'),
    'started': ('Запустил: {what}.', 'Started: {what}.', 'Gestartet: {what}.', 'Lancé : {what}.', 'Iniciado: {what}.', 'Avviato: {what}.', 'Iniciado: {what}.'),
    'closed': ('Закрыл: {what}.', 'Closed: {what}.', 'Geschlossen: {what}.', 'Fermé : {what}.', 'Cerrado: {what}.', 'Chiuso: {what}.', 'Fechado: {what}.'),
    'mic_warning': (' NEO тоже слушает через микрофон: пока доступ выключен, голосовые команды работать не будут.',
                    ' NEO also listens through the microphone: voice commands won’t work while access is off.',
                    ' NEO hört ebenfalls über das Mikrofon: Solange der Zugriff aus ist, funktionieren Sprachbefehle nicht.',
                    ' NEO écoute aussi par le micro : tant que l’accès est coupé, les commandes vocales ne marchent pas.',
                    ' NEO también escucha por el micrófono: mientras el acceso esté desactivado, las órdenes de voz no funcionarán.',
                    ' Anche NEO ascolta dal microfono: finché l’accesso è disattivato i comandi vocali non funzionano.',
                    ' O NEO também ouve pelo microfone: enquanto o acesso estiver desligado, os comandos de voz não funcionam.'),
    'unsupported': ('Это работает только в Windows.', 'This works only on Windows.', 'Das geht nur unter Windows.', 'Cela ne fonctionne que sous Windows.',
                    'Esto solo funciona en Windows.', 'Funziona solo su Windows.', 'Isto só funciona no Windows.'),
    'activation': ('Windows {state}. Подробности — в «Активация» (открыл).', 'Windows is {state}. Details are in Activation (opened).',
                   'Windows ist {state}. Details unter „Aktivierung“ (geöffnet).', 'Windows est {state}. Détails dans « Activation » (ouvert).',
                   'Windows está {state}. Detalles en «Activación» (abierto).', 'Windows è {state}. Dettagli in «Attivazione» (aperto).',
                   'O Windows está {state}. Detalhes em «Ativação» (aberto).'),
    'activated': ('активирована', 'activated', 'aktiviert', 'activé', 'activado', 'attivato', 'ativado'),
    'not_activated': ('не активирована', 'not activated', 'nicht aktiviert', 'non activé', 'sin activar', 'non attivato', 'não ativado'),
    'activation_unknown': ('Не удалось узнать состояние активации — открыл страницу «Активация».', 'Couldn’t read the activation state — opened the Activation page.',
                           'Aktivierungsstatus nicht lesbar – Seite „Aktivierung“ geöffnet.', 'État d’activation illisible — page « Activation » ouverte.',
                           'No pude leer el estado de activación: abrí la página «Activación».', 'Stato di attivazione non leggibile: aperta la pagina «Attivazione».',
                           'Não consegui ler o estado da ativação — abri a página «Ativação».'),
}
_TOGGLE_LABELS: dict[str, tuple[str, ...]] = {
    'dark_mode': ('Тёмная тема', 'Dark mode', 'Dunkler Modus', 'Mode sombre', 'Modo oscuro', 'Modalità scura', 'Modo escuro'),
    'transparency': ('Эффекты прозрачности', 'Transparency effects', 'Transparenzeffekte', 'Effets de transparence', 'Efectos de transparencia', 'Effetti trasparenza', 'Efeitos de transparência'),
    'clipboard_history': ('Журнал буфера обмена', 'Clipboard history', 'Zwischenablageverlauf', 'Historique du presse-papiers', 'Historial del portapapeles', 'Cronologia appunti', 'Histórico da área de transferência'),
    'game_mode': ('Игровой режим', 'Game mode', 'Spielmodus', 'Mode jeu', 'Modo de juego', 'Modalità gioco', 'Modo de jogo'),
    'snap': ('Прикрепление окон', 'Snap windows', 'Fenster andocken', 'Ancrer les fenêtres', 'Ajustar ventanas', 'Ancoraggio finestre', 'Ajustar janelas'),
    'storage_sense': ('Контроль памяти', 'Storage Sense', 'Speicheroptimierung', 'Assistant de stockage', 'Sensor de almacenamiento', 'Sensore memoria', 'Sensor de armazenamento'),
    'camera_access': ('Доступ к камере', 'Camera access', 'Kamerazugriff', 'Accès à la caméra', 'Acceso a la cámara', 'Accesso alla fotocamera', 'Acesso à câmara'),
    'microphone_access': ('Доступ к микрофону', 'Microphone access', 'Mikrofonzugriff', 'Accès au micro', 'Acceso al micrófono', 'Accesso al microfono', 'Acesso ao microfone'),
    'location': ('Определение местоположения', 'Location', 'Standort', 'Localisation', 'Ubicación', 'Posizione', 'Localização'),
    'night_light': ('Ночной свет', 'Night light', 'Nachtmodus', 'Éclairage nocturne', 'Luz nocturna', 'Luce notturna', 'Luz noturna'),
    'do_not_disturb': ('Не беспокоить', 'Do not disturb', 'Nicht stören', 'Ne pas déranger', 'No molestar', 'Non disturbare', 'Não incomodar'),
    'airplane': ('Режим «в самолёте»', 'Airplane mode', 'Flugzeugmodus', 'Mode avion', 'Modo avión', 'Modalità aereo', 'Modo de avião'),
    'notifications': ('Уведомления', 'Notifications', 'Benachrichtigungen', 'Notifications', 'Notificaciones', 'Notifiche', 'Notificações'),
    'magnifier': ('Экранная лупа', 'Magnifier', 'Bildschirmlupe', 'Loupe', 'Lupa', 'Lente di ingrandimento', 'Lupa'),
    'narrator': ('Экранный диктор', 'Narrator', 'Sprachausgabe', 'Narrateur', 'Narrador', 'Assistente vocale', 'Narrador'),
    'osk': ('Экранная клавиатура', 'On-screen keyboard', 'Bildschirmtastatur', 'Clavier visuel', 'Teclado en pantalla', 'Tastiera su schermo', 'Teclado no ecrã'),
}
_POWER_LABELS: dict[str, tuple[str, ...]] = {
    'best_performance': ('максимальная производительность', 'best performance', 'beste Leistung', 'meilleures performances', 'mejor rendimiento', 'massime prestazioni', 'melhor desempenho'),
    'balanced': ('сбалансированный', 'balanced', 'ausbalanciert', 'équilibré', 'equilibrado', 'bilanciato', 'equilibrado'),
    'efficiency': ('максимальная экономия энергии', 'best power efficiency', 'beste Energieeffizienz', 'meilleure efficacité énergétique', 'mejor eficiencia energética',
                   'massima efficienza energetica', 'melhor eficiência energética'),
}
# Windows 11 power mode overlays (powercfg /overlaysetactive)
POWER_OVERLAYS = {'best_performance': 'ded574b5-45a0-4f42-8737-46345c09c238',
                  'balanced': '00000000-0000-0000-0000-000000000000',
                  'efficiency': '961cc777-2547-4f9d-8174-7d86181b8a7a'}

# Registry switches of the current user: (key, value, kind, on, off). kind: dword | sz
REGISTRY_TOGGLES: dict[str, list[tuple[str, str, str, Any, Any]]] = {
    'dark_mode': [(r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize', 'AppsUseLightTheme', 'dword', 0, 1),
                  (r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize', 'SystemUsesLightTheme', 'dword', 0, 1)],
    'transparency': [(r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize', 'EnableTransparency', 'dword', 1, 0)],
    'clipboard_history': [(r'Software\Microsoft\Clipboard', 'EnableClipboardHistory', 'dword', 1, 0)],
    'game_mode': [(r'Software\Microsoft\GameBar', 'AutoGameModeEnabled', 'dword', 1, 0),
                  (r'Software\Microsoft\GameBar', 'AllowAutoGameMode', 'dword', 1, 0)],
    'snap': [(r'Control Panel\Desktop', 'WindowArrangementActive', 'sz', '1', '0')],
    'storage_sense': [(r'Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy', '01', 'dword', 1, 0)],
    'camera_access': [(r'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam', 'Value', 'sz', 'Allow', 'Deny')],
    'microphone_access': [(r'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone', 'Value', 'sz', 'Allow', 'Deny')],
    'location': [(r'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location', 'Value', 'sz', 'Allow', 'Deny')],
}
# Switches only the Settings window itself can change: (page uri, accessible names of the switch, all languages)
UIA_TOGGLES: dict[str, tuple[str, tuple[str, ...]]] = {
    'night_light': ('ms-settings:display', ('ночной свет', 'night light', 'nachtmodus', 'éclairage nocturne', 'luz nocturna', 'luce notturna', 'luz noturna')),
    'do_not_disturb': ('ms-settings:notifications', ('не беспокоить', 'do not disturb', 'nicht stören', 'ne pas déranger', 'no molestar', 'non disturbare', 'não incomodar')),
    'airplane': ('ms-settings:network-airplanemode', ('режим в самолете', 'в самолете', 'airplane mode', 'flugzeugmodus', 'mode avion', 'modo avión', 'modalità aereo', 'modo de avião')),
    'notifications': ('ms-settings:notifications', ('уведомления', 'notifications', 'benachrichtigungen', 'notificaciones', 'notifiche', 'notificações')),
}
# Programs of «Специальные возможности»: (executable, process name)
ACCESSIBILITY_APPS = {'magnifier': ('magnify.exe', 'magnify.exe'), 'narrator': ('narrator.exe', 'narrator.exe'), 'osk': ('osk.exe', 'osk.exe')}
_IMAGE = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif', '.tif', '.tiff'}
_UPDATE_BUTTONS = ('проверить наличие обновлений', 'check for updates', 'nach updates suchen', 'rechercher des mises à jour',
                   'buscar actualizaciones', 'controlla aggiornamenti', 'cerca aggiornamenti', 'procurar atualizações', 'verificar se há atualizações')
_SETTINGS_TITLES = ('параметры', 'settings', 'einstellungen', 'paramètres', 'configuración', 'impostazioni', 'definições', 'configurações')


def _say(key: str, code: str, **values: Any) -> str:
    return _TEXT[key][_LANGS.index(_code(code))].format(**values)


def _label(table: dict[str, tuple[str, ...]], key: str, code: str) -> str:
    row = table.get(key)
    return row[_LANGS.index(_code(code))] if row else key


def page_label(key: str, code: str) -> str:
    return PAGES[key].labels[_LANGS.index(_code(code))]


@dataclass(slots=True)
class Result:
    ok: bool
    text: str
    data: dict[str, Any]


class Windows:
    """Everything that touches Windows, in one replaceable object (tests use a fake)."""

    def __init__(self) -> None:
        self.nt = os.name == 'nt'

    def open_uri(self, uri: str) -> None:
        os.startfile(uri)  # type: ignore[attr-defined]

    def reg_get(self, key: str, name: str) -> Any:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                return winreg.QueryValueEx(handle, name)[0]
        except OSError:
            return None

    def reg_set(self, key: str, name: str, kind: str, value: Any) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_DWORD if kind == 'dword' else winreg.REG_SZ, value)

    def broadcast(self, area: str) -> None:
        """Tell open windows that a setting changed (WM_SETTINGCHANGE)."""
        try:
            import ctypes
            result = ctypes.c_ulong()
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, area, 0x0002, 3000, ctypes.byref(result))
        except Exception:
            logger.debug('Setting change broadcast failed', exc_info=True)

    def run(self, args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)

    def start(self, exe: str) -> None:
        subprocess.Popen([exe], close_fds=True)

    def close_process(self, name: str) -> int:
        import psutil
        closed = 0
        for proc in psutil.process_iter(['name']):
            if str(proc.info.get('name') or '').casefold() == name:
                try:
                    proc.terminate()
                    closed += 1
                except Exception:
                    continue
        return closed

    def process_running(self, name: str) -> bool:
        import psutil
        return any(str(p.info.get('name') or '').casefold() == name for p in psutil.process_iter(['name']))

    def set_wallpaper(self, path: str) -> bool:
        import ctypes
        return bool(ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path), 0x01 | 0x02))

    def clear_clipboard_history(self) -> bool:
        from winsdk.windows.applicationmodel.datatransfer import Clipboard
        return bool(Clipboard.clear_history())

    def settings_toggle(self, uri: str, names: tuple[str, ...], enabled: bool) -> str:
        """'on' / 'off' / 'already' after switching it in the Settings window, '' when not found."""
        self.open_uri(uri)
        from pywinauto import Desktop
        desktop = Desktop(backend='uia')
        wanted = tuple(name.casefold().replace('ё', 'е').replace('«', '').replace('»', '') for name in names)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            time.sleep(0.3)
            for window in self._settings_windows(desktop):
                for control_type in ('Button', 'CheckBox'):
                    try:
                        controls = window.descendants(control_type=control_type)
                    except Exception:
                        continue
                    for control in controls[:600]:
                        try:
                            name = str(control.element_info.name or '').casefold().replace('ё', 'е').replace('«', '').replace('»', '')
                        except Exception:
                            continue
                        if not name or not any(w == name or name.startswith(w) for w in wanted):
                            continue
                        try:
                            toggle = control.iface_toggle
                            current = int(toggle.CurrentToggleState) != 0
                        except Exception:
                            continue
                        if current == bool(enabled):
                            return 'already'
                        toggle.Toggle()
                        return 'on' if enabled else 'off'
        return ''

    def settings_press(self, uri: str, names: tuple[str, ...]) -> bool:
        self.open_uri(uri)
        from pywinauto import Desktop
        desktop = Desktop(backend='uia')
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            time.sleep(0.3)
            for window in self._settings_windows(desktop):
                try:
                    buttons = window.descendants(control_type='Button')
                except Exception:
                    continue
                for button in buttons[:600]:
                    try:
                        name = str(button.element_info.name or '').casefold()
                    except Exception:
                        continue
                    if any(name.startswith(n) for n in names):
                        try:
                            button.invoke()
                        except Exception:
                            button.click_input()
                        return True
        return False

    @staticmethod
    def _settings_windows(desktop: Any) -> list[Any]:
        found = []
        try:
            windows = desktop.windows(visible_only=True)
        except Exception:
            return found
        for window in windows:
            try:
                title = str(window.window_text() or '').casefold()
            except Exception:
                continue
            if any(title == name or title.startswith(name) for name in _SETTINGS_TITLES):
                found.append(window)
        return found


def _timeout_label(minutes: int, code: str) -> str:
    return _say('never', code) if minutes == 0 else _say('minutes', code, n=minutes)


def run(config: Any, args: dict[str, Any], code: str, *, win: Windows | None = None,
        resolve_image: Callable[[str], str] | None = None) -> Result:
    win = win or Windows()
    action = str(args.get('action', '') or '')
    if not win.nt:
        return Result(False, _say('unsupported', code), {})
    if action == 'open':
        key = str(args.get('page', '') or '')
        page = PAGES.get(key)
        if page is None:
            return Result(False, _say('unsupported', code), {})
        win.open_uri(page.uri)
        if args.get('refused') == 'remote_desktop':
            return Result(True, _say('remote_desktop', code), {'uri': page.uri, 'refused': True})
        key_text = 'protected' if page.protected else 'opened'
        return Result(True, _say(key_text, code, page=page_label(key, code)), {'uri': page.uri})
    if action == 'toggle':
        return _toggle(win, str(args.get('what', '')), bool(args.get('enabled', True)), code)
    if action == 'power_mode':
        mode = str(args.get('mode', 'balanced'))
        proc = win.run(['powercfg', '/overlaysetactive', POWER_OVERLAYS.get(mode, POWER_OVERLAYS['balanced'])])
        if proc.returncode != 0:
            win.open_uri(PAGES['power'].uri)
            return Result(False, _say('power_failed', code), {'stderr': (proc.stderr or '')[:200]})
        return Result(True, _say('power_mode', code, mode=_label(_POWER_LABELS, mode, code)), {'mode': mode})
    if action in {'screen_timeout', 'sleep_timeout'}:
        minutes = max(0, min(600, int(args.get('minutes', 0) or 0)))
        setting = 'monitor-timeout' if action == 'screen_timeout' else 'standby-timeout'
        failed = [side for side in ('ac', 'dc') if win.run(['powercfg', '/change', f'{setting}-{side}', str(minutes)]).returncode != 0]
        if len(failed) == 2:
            win.open_uri(PAGES['power'].uri)
            return Result(False, _say('power_failed', code), {})
        return Result(True, _say(action, code, when=_timeout_label(minutes, code)), {'minutes': minutes})
    if action == 'about':
        return Result(True, about_text(code, win=win), {})
    if action == 'storage_info':
        return Result(True, storage_text(code), {})
    if action == 'activation_status':
        script = ("(Get-CimInstance SoftwareLicensingProduct -Filter \"ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f' "
                  "and PartialProductKey is not null\" | Select-Object -First 1 -ExpandProperty LicenseStatus)")
        proc = win.run(['powershell', '-NoProfile', '-Command', script], timeout=20)
        win.open_uri(PAGES['activation'].uri)
        value = (proc.stdout or '').strip()
        if proc.returncode != 0 or not value.isdigit():
            return Result(True, _say('activation_unknown', code), {})
        state = _say('activated', code) if value == '1' else _say('not_activated', code)
        return Result(True, _say('activation', code, state=state), {'license_status': int(value)})
    if action == 'check_updates':
        pressed = False
        try:
            pressed = win.settings_press(PAGES['updates'].uri, _UPDATE_BUTTONS)
        except Exception:
            logger.debug('Could not press «Check for updates»', exc_info=True)
        return Result(True, _say('updates' if pressed else 'updates_open', code), {'pressed': pressed})
    if action == 'clear_clipboard_history':
        try:
            if win.clear_clipboard_history():
                return Result(True, _say('clipboard_cleared', code), {})
        except Exception:
            logger.debug('Clipboard history clear failed', exc_info=True)
        win.open_uri(PAGES['clipboard'].uri)
        return Result(False, _say('clipboard_failed', code), {})
    if action == 'wallpaper':
        path = (resolve_image or (lambda what: ''))(str(args.get('what', '') or ''))
        if not path:
            return Result(False, _say('wallpaper_which', code), {})
        if Path(path).suffix.casefold() not in _IMAGE:
            return Result(False, _say('not_image', code, name=Path(path).name), {})
        if not win.set_wallpaper(path):
            win.open_uri(PAGES['background'].uri)
            return Result(False, _say('toggle_failed', code, what=Path(path).name), {})
        return Result(True, _say('wallpaper', code, name=Path(path).name), {'path': path})
    return Result(False, _say('unsupported', code), {})


def _toggle(win: Windows, what: str, enabled: bool, code: str) -> Result:
    label = _label(_TOGGLE_LABELS, what, code)
    if what in ACCESSIBILITY_APPS:
        exe, process = ACCESSIBILITY_APPS[what]
        if enabled:
            win.start(exe)
            return Result(True, _say('started', code, what=label), {})
        win.close_process(process)
        return Result(True, _say('closed', code, what=label), {})
    if what in REGISTRY_TOGGLES:
        rows = REGISTRY_TOGGLES[what]
        key, name, _kind, on_value, off_value = rows[0]
        current = win.reg_get(key, name)
        wanted_value = on_value if enabled else off_value
        if current is not None and str(current) == str(wanted_value) and all(
                str(win.reg_get(k, n)) == str(on if enabled else off) for k, n, _k, on, off in rows[1:]):
            return Result(True, _say('already_on' if enabled else 'already_off', code, what=label), {'changed': False})
        for key, name, kind, on_value, off_value in rows:
            win.reg_set(key, name, kind, on_value if enabled else off_value)
        win.broadcast('ImmersiveColorSet' if what in {'dark_mode', 'transparency'} else 'Environment')
        text = _say('on' if enabled else 'off', code, what=label)
        if what == 'microphone_access' and not enabled:
            text += _say('mic_warning', code)
        return Result(True, text, {'changed': True})
    if what in UIA_TOGGLES:
        uri, names = UIA_TOGGLES[what]
        state = ''
        try:
            state = win.settings_toggle(uri, names, enabled)
        except Exception:
            logger.debug('Settings switch %s failed', what, exc_info=True)
        if state == 'already':
            return Result(True, _say('already_on' if enabled else 'already_off', code, what=label), {'changed': False})
        if state:
            return Result(True, _say('on' if enabled else 'off', code, what=label), {'changed': True})
        return Result(False, _say('toggle_failed', code, what=label), {})
    return Result(False, _say('unsupported', code), {})


_ABOUT_LABELS = {
    'name': ('Имя компьютера', 'Device name', 'Gerätename', 'Nom de l’appareil', 'Nombre del dispositivo', 'Nome dispositivo', 'Nome do dispositivo'),
    'build': ('сборка', 'build', 'Build', 'build', 'compilación', 'build', 'compilação'),
    'cpu': ('Процессор', 'Processor', 'Prozessor', 'Processeur', 'Procesador', 'Processore', 'Processador'),
    'board': ('Плата', 'Board', 'Mainboard', 'Carte mère', 'Placa base', 'Scheda madre', 'Placa principal'),
    'memory': ('Память', 'Memory', 'Arbeitsspeicher', 'Mémoire', 'Memoria', 'Memoria', 'Memória'),
    'gb': ('ГБ', 'GB', 'GB', 'Go', 'GB', 'GB', 'GB'),
    'free': ('{free} {gb} свободно из {total} {gb}', '{free} {gb} free of {total} {gb}', '{free} {gb} frei von {total} {gb}',
             '{free} {gb} libres sur {total} {gb}', '{free} {gb} libres de {total} {gb}', '{free} {gb} liberi su {total} {gb}',
             '{free} {gb} livres de {total} {gb}'),
    'no_disks': ('Дисков не найдено.', 'No disks found.', 'Keine Laufwerke gefunden.', 'Aucun disque trouvé.', 'No se encontraron discos.',
                 'Nessun disco trovato.', 'Nenhum disco encontrado.'),
}


def _al(key: str, code: str) -> str:
    return _ABOUT_LABELS[key][_LANGS.index(_code(code))]


def about_text(code: str, *, win: Windows | None = None) -> str:
    """Name, Windows edition/version/build, processor, memory, board — read only."""
    import platform
    rows: list[tuple[str, str]] = [(_al('name', code), platform.node())]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
            def value(name: str) -> str:
                try:
                    return str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    return ''
            build = value('CurrentBuild')
            product = value('ProductName')
            if build.isdigit() and int(build) >= 22000:
                product = product.replace('Windows 10', 'Windows 11')       # Windows 11 still reports «Windows 10» here
            rows.append(('Windows', f'{product} {value("DisplayVersion")} ({_al("build", code)} {build}.{value("UBR")})'))
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
            rows.append((_al('cpu', code), str(winreg.QueryValueEx(key, 'ProcessorNameString')[0]).strip()))
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\BIOS') as key:
                rows.append((_al('board', code), ' '.join(str(winreg.QueryValueEx(key, n)[0]) for n in ('BaseBoardManufacturer', 'BaseBoardProduct')).strip()))
        except OSError:
            pass
    except Exception:
        rows.append(('Windows', platform.platform()))
    try:
        import psutil
        rows.append((_al('memory', code), f'{psutil.virtual_memory().total / 2 ** 30:.0f} {_al("gb", code)}'))
    except Exception:
        pass
    return '\n'.join(f'• {name}: {value}' for name, value in rows if value)


def storage_text(code: str) -> str:
    import psutil
    lines = []
    for part in psutil.disk_partitions(all=False):
        if 'cdrom' in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        lines.append(f'• {part.mountpoint} ' + _al('free', code).format(free=f'{usage.free / 2 ** 30:.0f}', total=f'{usage.total / 2 ** 30:.0f}', gb=_al('gb', code)))
    return '\n'.join(lines) or _al('no_disks', code)


_THIS = (r'(?:это|эту\s+картинку|эту\s+фотку|это\s+фото|этот\s+файл|выделенн\w+(?:\s+картинк\w+)?|this(?:\s+(?:picture|image|photo|file))?|it|'
         r'das(?:\s+bild)?|dieses\s+bild|[cç]a|cette\s+image|cette\s+photo|esto|esta\s+(?:imagen|foto)|questo|questa\s+(?:immagine|foto)|isto|esta\s+(?:imagem|foto))')


def find_image(what: str, config: Any, *, folders: list[Path] | None = None) -> str:
    """The picture meant by «это» (Explorer selection / last file) or by its name in the user's folders."""
    value = _n(what)
    if not value:
        return ''
    if re.fullmatch(_THIS, value):
        try:
            from jarvis.runtime import context173
            found = context173.resolve_file('this', config)
            return found.path if found is not None else ''
        except Exception:
            return ''
    name = re.sub(r'^(?:картинку|фото(?:графию)?|изображение|файл|picture|image|photo|file|bild|datei|imagen|immagine|imagem|ficheiro)\s+', '', value).strip(' "«»')
    if not name:
        return ''
    if folders is None:
        home = Path.home()
        folders = [home / 'Pictures', home / 'Desktop', home / 'Downloads', home / 'Documents']
    rows: list[tuple[float, str]] = []
    for folder in folders:
        try:
            candidates = list(folder.glob('*')) + list(folder.glob('*/*'))
        except OSError:
            continue
        for path in candidates[:4000]:
            if path.suffix.casefold() in _IMAGE and name in path.stem.casefold().replace('ё', 'е'):
                try:
                    rows.append((path.stat().st_mtime, str(path)))
                except OSError:
                    continue
    return max(rows)[1] if rows else ''


def risk(args: dict[str, Any]) -> str:
    """'safe' | 'medium': changing a setting asks first unless it is easy to switch back."""
    action = str(args.get('action', '') or '')
    if action in {'open', 'about', 'storage_info', 'activation_status', 'check_updates'}:
        return 'safe'
    if action == 'toggle' and args.get('what') in {'microphone_access', 'camera_access', 'location'}:
        return 'medium'
    if action in {'clear_clipboard_history', 'sleep_timeout'}:
        return 'medium'
    return 'safe'


__all__ = ['ACCESSIBILITY_APPS', 'PAGES', 'find_image', 'POWER_OVERLAYS', 'REGISTRY_TOGGLES', 'UIA_TOGGLES', 'Result', 'Windows',
           'about_text', 'page_label', 'parse', 'risk', 'run', 'storage_text']
