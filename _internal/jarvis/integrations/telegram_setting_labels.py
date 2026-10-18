"""Native labels for the public Telegram settings registry; no service imports.

LABELS values: en, de, fr, es, it, pt. CHOICES values: ru, en, de, fr, es, it, pt.
The Russian source strings remain stable lookup keys, including legacy wording.
"""

_ROWS = """
Язык|Language|Sprache|Langue|Idioma|Lingua|Idioma
Показывать технические детали выполненных действий|Show action details|Details ausgeführter Aktionen anzeigen|Afficher les détails des actions exécutées|Mostrar detalles de las acciones realizadas|Mostra i dettagli delle azioni eseguite|Mostrar detalhes das ações executadas
Разрешить команды «покажи окно» / «скройся»|Control window visibility by voice|Fenstersichtbarkeit per Sprache steuern|Contrôler la visibilité de la fenêtre par la voix|Controlar la visibilidad de la ventana por voz|Controlla la visibilità della finestra con la voce|Controlar a visibilidade da janela por voz
Включить Atlas 2.0 (Static → Semantic → Context → AI)|Phrase Atlas|Befehlssammlung|Répertoire de commandes|Repertorio de comandos|Repertorio di comandi|Repertório de comandos
Семантический matcher для гибких формулировок|Flexible phrase matching|Flexible Formulierungen erkennen|Reconnaître des formulations variées|Reconocer formulaciones variadas|Riconosci formulazioni diverse|Reconhecer formulações variadas
LazyVision — анализировать экран только по необходимости|Analyze the screen only on request|Bildschirm nur auf Anfrage analysieren|Analyser l’écran uniquement sur demande|Analizar la pantalla solo cuando se solicite|Analizza lo schermo solo su richiesta|Analisar o ecrã apenas quando solicitado
Разрешить автокликер и голосовые команды|Allow autoclicker voice commands|Sprachbefehle für automatische Klicks erlauben|Autoriser les commandes vocales du clic automatique|Permitir comandos de voz para clics automáticos|Consenti comandi vocali per i clic automatici|Permitir comandos de voz para cliques automáticos
Показывать отдельное окно Developer|Show developer window|Entwicklerfenster anzeigen|Afficher la fenêtre de développement|Mostrar la ventana de desarrollo|Mostra la finestra di sviluppo|Mostrar a janela de desenvolvimento
Порог semantic matcher|Phrase matching threshold|Schwellenwert der Befehlserkennung|Seuil de correspondance des commandes|Umbral de coincidencia de comandos|Soglia di corrispondenza dei comandi|Limiar de correspondência dos comandos
Скорость|Clicks per second|Klicks pro Sekunde|Clics par seconde|Clics por segundo|Clic al secondo|Cliques por segundo
Кнопка мыши|Mouse button|Maustaste|Bouton de la souris|Botón del ratón|Pulsante del mouse|Botão do rato
Вкл./выкл. микрофон NEO|Toggle NEO microphone|NEO-Mikrofon ein-/ausschalten|Activer ou désactiver le microphone NEO|Activar o desactivar el micrófono de NEO|Attiva o disattiva il microfono NEO|Ativar ou desativar o microfone do NEO
Вкл./выкл. автокликер|Toggle automatic clicking|Automatische Klicks ein-/ausschalten|Activer ou désactiver les clics automatiques|Activar o desactivar los clics automáticos|Attiva o disattiva i clic automatici|Ativar ou desativar os cliques automáticos
Прокрутка чата вверх|Scroll chat up|Chat nach oben scrollen|Faire défiler la conversation vers le haut|Desplazar el chat hacia arriba|Scorri la chat verso l’alto|Deslocar a conversa para cima
Прокрутка чата вниз|Scroll chat down|Chat nach unten scrollen|Faire défiler la conversation vers le bas|Desplazar el chat hacia abajo|Scorri la chat verso il basso|Deslocar a conversa para baixo
Окно поверх остальных|Toggle window on top|Fenster im Vordergrund umschalten|Afficher ou masquer la fenêtre au premier plan|Mostrar u ocultar la ventana en primer plano|Mostra o nascondi la finestra in primo piano|Mostrar ou ocultar a janela em primeiro plano
Ширина|Width|Breite|Largeur|Anchura|Larghezza|Largura
Высота|Height|Höhe|Hauteur|Altura|Altezza|Altura
Размер текста диалога|Chat font size|Schriftgröße im Chat|Taille du texte de la conversation|Tamaño del texto del chat|Dimensione del testo della chat|Tamanho do texto da conversa
Скругление углов|Corner radius|Eckenradius|Arrondi des angles|Radio de las esquinas|Raggio degli angoli|Raio dos cantos
Высота чата|Chat height|Chathöhe|Hauteur de la conversation|Altura del chat|Altezza della chat|Altura da conversa
Разрешить перетаскивание окна за верхнюю область|Allow dragging by the top of the window|Fenster am oberen Bereich verschieben|Déplacer la fenêtre par sa partie supérieure|Permitir arrastrar la ventana por la parte superior|Consenti di trascinare la finestra dalla parte superiore|Permitir arrastar a janela pela parte superior
Режим «только робот»|Robot-only mode|Nur Roboter anzeigen|Mode robot seul|Modo solo robot|Modalità solo robot|Modo apenas robô
Шрифт диалогов|Chat font|Schriftart im Chat|Police de la conversation|Fuente del chat|Carattere della chat|Tipo de letra da conversa
Частота анимаций|Animation frame rate|Bildrate der Animationen|Fréquence des animations|Fotogramas por segundo de las animaciones|Fotogrammi al secondo delle animazioni|Fotogramas por segundo das animações
Цветовая схема|Colour theme|Farbschema|Thème de couleurs|Tema de colores|Tema di colori|Tema de cores
Фон всего окна|Window opacity|Deckkraft des Fensters|Opacité de la fenêtre|Opacidad de la ventana|Opacità della finestra|Opacidade da janela
Панели|Panel opacity|Deckkraft der Bereiche|Opacité des panneaux|Opacidad de los paneles|Opacità dei pannelli|Opacidade dos painéis
Кнопки и поля|Button and field opacity|Deckkraft von Tasten und Feldern|Opacité des boutons et champs|Opacidad de botones y campos|Opacità di pulsanti e campi|Opacidade dos botões e campos
Диалоговая область|Chat opacity|Deckkraft des Chatbereichs|Opacité de la conversation|Opacidad del chat|Opacità della chat|Opacidade da conversa
Сообщения|Message opacity|Deckkraft der Nachrichten|Opacité des messages|Opacidad de los mensajes|Opacità dei messaggi|Opacidade das mensagens
Рамки|Border opacity|Deckkraft der Rahmen|Opacité des bordures|Opacidad de los bordes|Opacità dei bordi|Opacidade dos contornos
Акцент и выделение|Accent and highlight colour|Akzent- und Hervorhebungsfarbe|Couleur d’accentuation et de sélection|Color de acento y resaltado|Colore di accento ed evidenziazione|Cor de destaque e seleção
Основной фон окна|Window background colour|Hintergrundfarbe des Fensters|Couleur de fond de la fenêtre|Color de fondo de la ventana|Colore di sfondo della finestra|Cor de fundo da janela
Наполнение панелей|Panel colour|Farbe der Bereiche|Couleur des panneaux|Color de los paneles|Colore dei pannelli|Cor dos painéis
Рамка окна|Window border colour|Rahmenfarbe des Fensters|Couleur de bordure de la fenêtre|Color del borde de la ventana|Colore del bordo della finestra|Cor do contorno da janela
Основной текст|Main text colour|Haupttextfarbe|Couleur du texte principal|Color del texto principal|Colore del testo principale|Cor do texto principal
Дополнительный текст|Secondary text colour|Farbe des Zusatztexts|Couleur du texte secondaire|Color del texto secundario|Colore del testo secondario|Cor do texto secundário
Кнопки и поле ввода|Button and input colour|Farbe von Tasten und Eingabefeld|Couleur des boutons et de la saisie|Color de botones y entrada de texto|Colore di pulsanti e campo di testo|Cor dos botões e do campo de texto
Элементы при наведении|Hover colour|Farbe beim Darüberfahren|Couleur au survol|Color al pasar el cursor|Colore al passaggio del puntatore|Cor ao passar o cursor
Фон диалоговой области|Chat background colour|Hintergrundfarbe des Chats|Couleur de fond de la conversation|Color de fondo del chat|Colore di sfondo della chat|Cor de fundo da conversa
Сообщения пользователя|User message colour|Farbe der Benutzernachrichten|Couleur des messages utilisateur|Color de los mensajes del usuario|Colore dei messaggi dell’utente|Cor das mensagens do utilizador
Сообщения робота|Assistant message colour|Farbe der Assistentennachrichten|Couleur des messages de l’assistant|Color de los mensajes del asistente|Colore dei messaggi dell’assistente|Cor das mensagens do assistente
Показывать сводку на день при запуске|Show daily briefing at startup|Tagesübersicht beim Start anzeigen|Afficher le résumé quotidien au démarrage|Mostrar el resumen diario al iniciar|Mostra il riepilogo giornaliero all’avvio|Mostrar o resumo diário ao iniciar
Время запуска ассистента|Briefing: startup time|Tagesübersicht: Startzeit|Résumé : heure de démarrage|Resumen: hora de inicio|Riepilogo: ora di avvio|Resumo: hora de início
Погода и температура|Briefing: weather and temperature|Tagesübersicht: Wetter und Temperatur|Résumé : météo et température|Resumen: tiempo y temperatura|Riepilogo: meteo e temperatura|Resumo: tempo e temperatura
Полные сведения о компьютере — CPU / GPU / RAM / диски|Briefing: processor, graphics, memory and disks|Tagesübersicht: Prozessor, Grafik, Speicher und Laufwerke|Résumé : processeur, carte graphique, mémoire et disques|Resumen: procesador, gráfica, memoria y discos|Riepilogo: processore, grafica, memoria e dischi|Resumo: processador, gráfica, memória e discos
Краткие сведения о процессах|Briefing: processes|Tagesübersicht: Prozesse|Résumé : processus|Resumen: procesos|Riepilogo: processi|Resumo: processos
Подключённые устройства компьютера|Briefing: connected devices|Tagesübersicht: angeschlossene Geräte|Résumé : appareils connectés|Resumen: dispositivos conectados|Riepilogo: dispositivi collegati|Resumo: dispositivos ligados
Подключённые Bluetooth-устройства|Briefing: Bluetooth devices|Tagesübersicht: Bluetooth-Geräte|Résumé : appareils Bluetooth|Resumen: dispositivos Bluetooth|Riepilogo: dispositivi Bluetooth|Resumo: dispositivos Bluetooth
Проверять подключённые API-сервисы|Check connected API services|Verbundene API-Dienste prüfen|Vérifier les services API connectés|Comprobar los servicios API conectados|Verificare i servizi API collegati|Verificar os serviços API ligados
Отображать робота|Show robot|Roboter anzeigen|Afficher le robot|Mostrar el robot|Mostra il robot|Mostrar o robô
Анимации в ожидании|Idle animations|Animationen im Ruhezustand|Animations au repos|Animaciones en reposo|Animazioni a riposo|Animações em repouso
Наушники|Headphones|Kopfhörer|Casque audio|Auriculares|Cuffie|Auscultadores
GIF-анимации|GIF animations|GIF-Animationen|Animations GIF|Animaciones GIF|Animazioni GIF|Animações GIF
Чашка кофе|Coffee cup|Kaffeetasse|Tasse de café|Taza de café|Tazza di caffè|Chávena de café
Сигарета|Cigarette|Zigarette|Cigarette|Cigarrillo|Sigaretta|Cigarro
Разрешить перетаскивание и изменение размеров информационных элементов|Move and resize information items|Infoelemente verschieben und skalieren|Déplacer et redimensionner les éléments d’information|Mover y redimensionar los elementos informativos|Sposta e ridimensiona gli elementi informativi|Mover e redimensionar os elementos informativos
Показывать текущее время|Show current time|Aktuelle Uhrzeit anzeigen|Afficher l’heure actuelle|Mostrar la hora actual|Mostra l’ora attuale|Mostrar a hora atual
Показывать текущую песню и исполнителя|Show current track and artist|Aktuellen Titel und Interpreten anzeigen|Afficher le morceau et l’artiste actuels|Mostrar la canción y el artista actuales|Mostra brano e artista attuali|Mostrar a faixa e o intérprete atuais
Показывать температуру города по умолчанию|Show default-city temperature|Temperatur der Standardstadt anzeigen|Afficher la température de la ville par défaut|Mostrar la temperatura de la ciudad predeterminada|Mostra la temperatura della città predefinita|Mostrar a temperatura da cidade predefinida
Показывать название города рядом с погодой|Show city name beside weather|Stadtnamen neben dem Wetter anzeigen|Afficher la ville à côté de la météo|Mostrar la ciudad junto al tiempo|Mostra la città accanto al meteo|Mostrar a cidade junto ao tempo
Показывать активные таймеры и напоминания|Show active timers and reminders|Aktive Timer und Erinnerungen anzeigen|Afficher les minuteurs et rappels actifs|Mostrar temporizadores y recordatorios activos|Mostra timer e promemoria attivi|Mostrar temporizadores e lembretes ativos
Размер робота|Robot size|Robotergröße|Taille du robot|Tamaño del robot|Dimensione del robot|Tamanho do robô
Масштаб GIF|GIF scale|GIF-Skalierung|Échelle du GIF|Escala del GIF|Scala della GIF|Escala do GIF
Размер текста|Information font size|Schriftgröße der Informationen|Taille du texte informatif|Tamaño del texto informativo|Dimensione del testo informativo|Tamanho do texto informativo
Размер текста таймера|Timer font size|Schriftgröße des Timers|Taille du texte du minuteur|Tamaño del texto del temporizador|Dimensione del testo del timer|Tamanho do texto do temporizador
Прозрачность таймера|Timer opacity|Deckkraft des Timers|Opacité du minuteur|Opacidad del temporizador|Opacità del timer|Opacidade do temporizador
Засыпать через|Idle sleep delay|Wartezeit bis zum Schlafmodus|Délai avant la mise en veille|Espera antes del reposo|Attesa prima del riposo|Espera antes do repouso
Активная анимация|Ambient animation|Hintergrundanimation|Animation d’ambiance|Animación ambiental|Animazione ambientale|Animação ambiente
Стиль таймера|Timer style|Timerstil|Style du minuteur|Estilo del temporizador|Stile del timer|Estilo do temporizador
Положение часов|Clock position|Position der Uhr|Position de l’horloge|Posición del reloj|Posizione dell’orologio|Posição do relógio
Положение трека|Track position|Position der Titelanzeige|Position du morceau|Posición de la canción|Posizione del brano|Posição da faixa
Положение температуры|Temperature position|Position der Temperaturanzeige|Position de la température|Posición de la temperatura|Posizione della temperatura|Posição da temperatura
Положение таймера|Timer position|Position des Timers|Position du minuteur|Posición del temporizador|Posizione del timer|Posição do temporizador
Корпус|Body colour|Körperfarbe|Couleur du corps|Color del cuerpo|Colore del corpo|Cor do corpo
Светлая оболочка головы|Head shell colour|Farbe der Kopfverkleidung|Couleur de la coque de tête|Color de la carcasa de la cabeza|Colore del guscio della testa|Cor da cobertura da cabeça
Экран лица|Face screen colour|Farbe des Gesichtsbildschirms|Couleur de l’écran du visage|Color de la pantalla facial|Colore dello schermo del volto|Cor do ecrã do rosto
Глаза и эмоции|Eye and expression colour|Farbe der Augen und Mimik|Couleur des yeux et expressions|Color de los ojos y expresiones|Colore degli occhi e delle espressioni|Cor dos olhos e expressões
Контуры и детали|Outline and detail colour|Farbe der Konturen und Details|Couleur des contours et détails|Color de contornos y detalles|Colore di contorni e dettagli|Cor dos contornos e detalhes
Свечение вокруг робота|Robot glow colour|Farbe des Roboterleuchtens|Couleur du halo du robot|Color del resplandor del robot|Colore del bagliore del robot|Cor do brilho do robô
Цвет наушников|Headphone colour|Kopfhörerfarbe|Couleur du casque|Color de los auriculares|Colore delle cuffie|Cor dos auscultadores
Цвет кружки|Cup colour|Tassenfarbe|Couleur de la tasse|Color de la taza|Colore della tazza|Cor da chávena
Цвет рук|Hand colour|Handfarbe|Couleur des mains|Color de las manos|Colore delle mani|Cor das mãos
Луна|Moon colour|Mondfarbe|Couleur de la lune|Color de la luna|Colore della luna|Cor da lua
Облака|Cloud colour|Wolkenfarbe|Couleur des nuages|Color de las nubes|Colore delle nuvole|Cor das nuvens
Дождь|Rain colour|Regenfarbe|Couleur de la pluie|Color de la lluvia|Colore della pioggia|Cor da chuva
Звездопад / звёздное небо|Shooting-star and starry-sky colour|Farbe von Sternschnuppen und Sternenhimmel|Couleur des étoiles filantes et du ciel étoilé|Color de las estrellas fugaces y el cielo estrellado|Colore delle stelle cadenti e del cielo stellato|Cor das estrelas cadentes e do céu estrelado
Снег|Snow colour|Schneefarbe|Couleur de la neige|Color de la nieve|Colore della neve|Cor da neve
Цвет текста|Timer text colour|Textfarbe des Timers|Couleur du texte du minuteur|Color del texto del temporizador|Colore del testo del timer|Cor do texto do temporizador
Цвет фона|Timer background colour|Hintergrundfarbe des Timers|Couleur de fond du minuteur|Color de fondo del temporizador|Colore di sfondo del timer|Cor de fundo do temporizador
Цвет рамки|Timer border colour|Rahmenfarbe des Timers|Couleur de bordure du minuteur|Color del borde del temporizador|Colore del bordo del timer|Cor do contorno do temporizador
Smart Downloads|Smart Downloads|Intelligente Downloads|Téléchargements intelligents|Descargas inteligentes|Download intelligenti|Transferências inteligentes
Уведомления VPN|VPN notifications|VPN-Benachrichtigungen|Notifications VPN|Notificaciones de VPN|Notifiche VPN|Notificações VPN
Уведомления о задержке сети|Network latency notifications|Hinweise zur Netzwerklatenz|Notifications de latence réseau|Notificaciones de latencia de red|Notifiche sulla latenza di rete|Notificações de latência da rede
История действий|Activity history|Aktivitätsverlauf|Historique d’activité|Historial de actividad|Cronologia attività|Histórico de atividade
Индекс файлов|File index|Dateiindex|Index des fichiers|Índice de archivos|Indice dei file|Índice de ficheiros
Анализ изображений|Image analysis|Bildanalyse|Analyse d’images|Análisis de imágenes|Analisi delle immagini|Análise de imagens
Порог задержки (мс)|Latency threshold (ms)|Latenzschwelle (ms)|Seuil de latence (ms)|Umbral de latencia (ms)|Soglia di latenza (ms)|Limiar de latência (ms)
Хранить историю (дней)|History retention (days)|Verlauf aufbewahren (Tage)|Conserver l’historique (jours)|Conservar el historial (días)|Conserva la cronologia (giorni)|Conservar o histórico (dias)
Папки загрузок|Download folders|Downloadordner|Dossiers de téléchargement|Carpetas de descargas|Cartelle dei download|Pastas de transferências
Исключённые приложения|Excluded applications|Ausgeschlossene Anwendungen|Applications exclues|Aplicaciones excluidas|Applicazioni escluse|Aplicações excluídas
Исключённые папки|Excluded folders|Ausgeschlossene Ordner|Dossiers exclus|Carpetas excluidas|Cartelle escluse|Pastas excluídas
Книжная полка|Bookshelf|Regal|Étagère|Estantería|Scaffale|Estante
Копировать файлы на полку|Copy imported files to shelf|Importierte Dateien ins Regal kopieren|Copier les fichiers importés sur l’étagère|Copiar archivos importados a la estantería|Copia i file importati nello scaffale|Copiar ficheiros importados para a estante
Восстанавливать закреплённые окна|Restore pinned windows|Angeheftete Fenster wiederherstellen|Restaurer les fenêtres épinglées|Restaurar las ventanas fijadas|Ripristina le finestre fissate|Restaurar as janelas fixadas
Размер шрифта|Font size|Schriftgröße|Taille de police|Tamaño de fuente|Dimensione del carattere|Tamanho do tipo de letra
Непрозрачность|Opacity|Deckkraft|Opacité|Opacidad|Opacità|Opacidade
Горячая клавиша полки|Bookshelf keyboard shortcut|Tastenkombination für das Regal|Raccourci clavier de l’étagère|Atajo de teclado de la estantería|Scorciatoia dello scaffale|Atalho de teclado da estante
""".strip()

LABELS = {fields[0]: tuple(fields[1:]) for line in _ROWS.splitlines() if (fields := line.split("|"))}
LABELS["Стиль общения"] = ("Conversation style", "Gesprächsstil", "Style de conversation", "Estilo de conversación", "Stile di conversazione", "Estilo de conversa")

RU_OVERRIDES = {
    "Включить Atlas 2.0 (Static → Semantic → Context → AI)": "Фразеатлас",
    "Семантический matcher для гибких формулировок": "Распознавать гибкие формулировки",
    "LazyVision — анализировать экран только по необходимости": "Анализ экрана только по запросу",
    "Порог semantic matcher": "Порог сопоставления фраз",
    "Прозрачность таймера": "Непрозрачность таймера",
}

CHOICES = {
    "friendly": ("Дружелюбный", "Friendly", "Freundlich", "Amical", "Amistoso", "Amichevole", "Amigável"),
    "business": ("Деловой", "Professional", "Sachlich", "Professionnel", "Profesional", "Professionale", "Profissional"),
    "military": ("Военный", "Military", "Militärisch", "Militaire", "Militar", "Militare", "Militar"),
    "street": ("Разговорный", "Casual", "Locker", "Familier", "Informal", "Informale", "Informal"),
    "off": ("Выключено", "Off", "Aus", "Désactivé", "Desactivado", "Disattivo", "Desativado"),
    "rain": ("Дождь", "Rain", "Regen", "Pluie", "Lluvia", "Pioggia", "Chuva"),
    "snow": ("Снег", "Snow", "Schnee", "Neige", "Nieve", "Neve", "Neve"),
    "starfall": ("Звездопад", "Shooting stars", "Sternschnuppen", "Étoiles filantes", "Estrellas fugaces", "Stelle cadenti", "Estrelas cadentes"),
    "cloudy": ("Облака", "Clouds", "Wolken", "Nuages", "Nubes", "Nuvole", "Nuvens"),
    "starry_sky": ("Звёздное небо", "Starry sky", "Sternenhimmel", "Ciel étoilé", "Cielo estrellado", "Cielo stellato", "Céu estrelado"),
    "fireflies": ('Светлячки', 'Fireflies', 'Glühwürmchen', 'Lucioles', 'Luciérnagas', 'Lucciole', 'Pirilampos'),
    "left": ("Левая", "Left", "Links", "Gauche", "Izquierdo", "Sinistro", "Esquerdo"),
    "right": ("Правая", "Right", "Rechts", "Droite", "Derecho", "Destro", "Direito"),
    "middle": ("Средняя", "Middle", "Mitte", "Milieu", "Central", "Centrale", "Central"),
    "glass": ("Стекло", "Glass", "Glas", "Verre", "Cristal", "Vetro", "Vidro"),
    "minimal": ("Минимальный", "Minimal", "Minimal", "Minimaliste", "Minimalista", "Minimale", "Minimalista"),
    "pill": ("Капсула", "Capsule", "Kapsel", "Capsule", "Cápsula", "Capsula", "Cápsula"),
    "top_left": ("Сверху слева", "Top left", "Oben links", "En haut à gauche", "Arriba a la izquierda", "In alto a sinistra", "Em cima à esquerda"),
    "top_center": ("Сверху по центру", "Top centre", "Oben mittig", "En haut au centre", "Arriba en el centro", "In alto al centro", "Em cima ao centro"),
    "top_right": ("Сверху справа", "Top right", "Oben rechts", "En haut à droite", "Arriba a la derecha", "In alto a destra", "Em cima à direita"),
    "center": ("По центру", "Centre", "Mitte", "Au centre", "En el centro", "Al centro", "Ao centro"),
    "left_center": ("Слева по центру", "Middle left", "Mittig links", "Au milieu à gauche", "En el centro a la izquierda", "Al centro a sinistra", "Ao centro à esquerda"),
    "right_center": ("Справа по центру", "Middle right", "Mittig rechts", "Au milieu à droite", "En el centro a la derecha", "Al centro a destra", "Ao centro à direita"),
    "bottom_left": ("Снизу слева", "Bottom left", "Unten links", "En bas à gauche", "Abajo a la izquierda", "In basso a sinistra", "Em baixo à esquerda"),
    "bottom_center": ("Снизу по центру", "Bottom centre", "Unten mittig", "En bas au centre", "Abajo en el centro", "In basso al centro", "Em baixo ao centro"),
    "bottom_right": ("Снизу справа", "Bottom right", "Unten rechts", "En bas à droite", "Abajo a la derecha", "In basso a destra", "Em baixo à direita"),
    "ru": ("Русский", "Russian", "Russisch", "Russe", "Ruso", "Russo", "Russo"),
    "en": ("Английский", "English", "Englisch", "Anglais", "Inglés", "Inglese", "Inglês"),
    "de": ("Немецкий", "German", "Deutsch", "Allemand", "Alemán", "Tedesco", "Alemão"),
    "fr": ("Французский", "French", "Französisch", "Français", "Francés", "Francese", "Francês"),
    "es": ("Испанский", "Spanish", "Spanisch", "Espagnol", "Español", "Spagnolo", "Espanhol"),
    "it": ("Итальянский", "Italian", "Italienisch", "Italien", "Italiano", "Italiano", "Italiano"),
    "pt": ("Португальский", "Portuguese", "Portugiesisch", "Portugais", "Portugués", "Portoghese", "Português"),
    "Custom": ("Своя схема", "Custom", "Benutzerdefiniert", "Personnalisé", "Personalizado", "Personalizzato", "Personalizado"),
}
CHOICES["center_left"] = CHOICES["left_center"]
CHOICES["center_right"] = CHOICES["right_center"]
