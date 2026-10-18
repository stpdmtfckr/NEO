# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Runtime localization for NEO.

Design rules:
- User-authored text is never translated or rewritten by this module.
- Widget captions/help/placeholder text are translated from a stored Russian source.
- The assistant's language setting is independent from wake-word text.
- Exactly one selected UI/command language is active; no English/Russian fallback corpus is loaded.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QDoubleSpinBox, QGroupBox, QLabel, QLineEdit, QMainWindow,
    QMenu, QPlainTextEdit, QSpinBox, QTabWidget, QWidget,
)

from jarvis.language import (
    LANGUAGES, LANGUAGE_ENGLISH_NAMES, WHISPER_LANGUAGE, language_code,
    accepted_command_languages, assistant_language_instruction,
    command_transcription_prompt, language_rejection_message,
)

# Core UI strings are deliberately exact: these are the most visible controls.
# Longer help text is handled by the phrase glossary below and falls back to a
# localized generic help sentence rather than leaking Russian into a foreign UI.
EXACT: dict[str, dict[str, str]] = {
    "Да": {"en":"Yes","de":"Ja","fr":"Oui","es":"Sí","it":"Sì","pt":"Sim"},
    "Нет": {"en":"No","de":"Nein","fr":"Non","es":"No","it":"No","pt":"Não"},
    "Вы": {"en":"You","de":"Sie","fr":"Vous","es":"Tú","it":"Tu","pt":"Você"},
    "Размышление": {"en":"Reasoning","de":"Überlegung","fr":"Réflexion","es":"Razonamiento","it":"Ragionamento","pt":"Raciocínio"},
    "Думает…": {"en":"Thinking…","de":"Denkt nach…","fr":"Réfléchit…","es":"Pensando…","it":"Sta pensando…","pt":"Pensando…"},
    "Новый диалог начат.": {"en":"New conversation started.","de":"Neue Unterhaltung gestartet.","fr":"Nouvelle conversation commencée.","es":"Nueva conversación iniciada.","it":"Nuova conversazione avviata.","pt":"Nova conversa iniciada."},
    "Настройки сохранены": {"en":"Settings saved","de":"Einstellungen gespeichert","fr":"Paramètres enregistrés","es":"Configuración guardada","it":"Impostazioni salvate","pt":"Configurações salvas"},
    "ИИ": {"en":"AI","de":"KI","fr":"IA","es":"IA","it":"IA","pt":"IA"},
    "Поведение": {"en":"Behavior","de":"Verhalten","fr":"Comportement","es":"Comportamiento","it":"Comportamento","pt":"Comportamento"},
    "Микрофон и wake word": {"en":"Microphone and wake word","de":"Mikrofon und Wake Word","fr":"Microphone et mot d'activation","es":"Micrófono y palabra de activación","it":"Microfono e parola di attivazione","pt":"Microfone e palavra de ativação"},
    "Медиа": {"en":"Media","de":"Medien","fr":"Médias","es":"Multimedia","it":"Media","pt":"Mídia"},
    "Окно": {"en":"Window","de":"Fenster","fr":"Fenêtre","es":"Ventana","it":"Finestra","pt":"Janela"},
    "Робот": {"en":"Robot","de":"Roboter","fr":"Robot","es":"Robot","it":"Robot","pt":"Robô"},
    "Безопасность": {"en":"Security","de":"Sicherheit","fr":"Sécurité","es":"Seguridad","it":"Sicurezza","pt":"Segurança"},
    "Дополнительно": {"en":"Advanced","de":"Erweitert","fr":"Avancé","es":"Avanzado","it":"Avanzate","pt":"Avançado"},
    "Настройки NEO": {"en":"NEO Settings","de":"NEO Einstellungen","fr":"Paramètres NEO","es":"Configuración de NEO","it":"Impostazioni NEO","pt":"Configurações do NEO"},
    "Язык интерфейса и ассистента": {"en":"Interface and assistant language","de":"Sprache von Oberfläche und Assistent","fr":"Langue de l'interface et de l'assistant","es":"Idioma de la interfaz y del asistente","it":"Lingua dell'interfaccia e dell'assistente","pt":"Idioma da interface e do assistente"},
    "Язык": {"en":"Language","de":"Sprache","fr":"Langue","es":"Idioma","it":"Lingua","pt":"Idioma"},
    "API и внешние сервисы": {"en":"APIs and external services","de":"APIs und externe Dienste","fr":"API et services externes","es":"API y servicios externos","it":"API e servizi esterni","pt":"APIs e serviços externos"},
    "Включить OpenWeather": {"en":"Enable OpenWeather","de":"OpenWeather aktivieren","fr":"Activer OpenWeather","es":"Activar OpenWeather","it":"Abilita OpenWeather","pt":"Ativar OpenWeather"},
    "Город погоды": {"en":"Weather city","de":"Wetterstadt","fr":"Ville météo","es":"Ciudad del clima","it":"Città meteo","pt":"Cidade do clima"},
    "Код страны": {"en":"Country code","de":"Ländercode","fr":"Code pays","es":"Código de país","it":"Codice paese","pt":"Código do país"},
    "Единицы погоды": {"en":"Weather units","de":"Wettereinheiten","fr":"Unités météo","es":"Unidades meteorológicas","it":"Unità meteo","pt":"Unidades meteorológicas"},
    "Таймеры и напоминания на сцене робота": {"en":"Timers and reminders on robot stage","de":"Timer und Erinnerungen auf der Roboterfläche","fr":"Minuteurs et rappels sur la scène du robot","es":"Temporizadores y recordatorios en la escena del robot","it":"Timer e promemoria sulla scena del robot","pt":"Temporizadores e lembretes na cena do robô"},
    "Показывать активные таймеры и напоминания": {"en":"Show active timers and reminders","de":"Aktive Timer und Erinnerungen anzeigen","fr":"Afficher les minuteurs et rappels actifs","es":"Mostrar temporizadores y recordatorios activos","it":"Mostra timer e promemoria attivi","pt":"Mostrar temporizadores e lembretes ativos"},
    "Слева внизу": {"en":"Bottom left","de":"Unten links","fr":"En bas à gauche","es":"Abajo a la izquierda","it":"In basso a sinistra","pt":"Inferior esquerdo"},
    "Справа внизу": {"en":"Bottom right","de":"Unten rechts","fr":"En bas à droite","es":"Abajo a la derecha","it":"In basso a destra","pt":"Inferior direito"},
    "Положение": {"en":"Position","de":"Position","fr":"Position","es":"Posición","it":"Posizione","pt":"Posição"},
    "Размер текста": {"en":"Text size","de":"Textgröße","fr":"Taille du texte","es":"Tamaño del texto","it":"Dimensione testo","pt":"Tamanho do texto"},
    "Показывать утреннюю/дневную сводку при запуске": {"en":"Show morning/daily briefing at startup","de":"Morgen-/Tagesübersicht beim Start anzeigen","fr":"Afficher le résumé matinal/quotidien au démarrage","es":"Mostrar resumen matutino/diario al iniciar","it":"Mostra riepilogo mattutino/giornaliero all'avvio","pt":"Mostrar resumo matinal/diário na inicialização"},
    "Один вариант распознавания на строку": {"en":"One recognition variant per line","de":"Eine Erkennungsvariante pro Zeile","fr":"Une variante de reconnaissance par ligne","es":"Una variante de reconocimiento por línea","it":"Una variante di riconoscimento per riga","pt":"Uma variante de reconhecimento por linha"},
    "По одному ASR-варианту на строку": {"en":"One speech-recognition variant per line","de":"Eine Spracherkennungsvariante pro Zeile","fr":"Une variante de reconnaissance vocale par ligne","es":"Una variante de reconocimiento de voz por línea","it":"Una variante di riconoscimento vocale per riga","pt":"Uma variante de reconhecimento de voz por linha"},
    "Подключение к Ollama": {"en":"Ollama connection","de":"Ollama-Verbindung","fr":"Connexion Ollama","es":"Conexión con Ollama","it":"Connessione a Ollama","pt":"Conexão com Ollama"},
    "Качество и нагрузка": {"en":"Quality and performance","de":"Qualität und Leistung","fr":"Qualité et performances","es":"Calidad y rendimiento","it":"Qualità e prestazioni","pt":"Qualidade e desempenho"},
    "Локальная разговорная модель, память и обучение на этом компьютере": {"en":"Local conversation model, memory and learning on this computer","de":"Lokales Gesprächsmodell, Gedächtnis und Lernen auf diesem Computer","fr":"Modèle de conversation local, mémoire et apprentissage sur cet ordinateur","es":"Modelo de conversación local, memoria y aprendizaje en este equipo","it":"Modello di conversazione locale, memoria e apprendimento su questo computer","pt":"Modelo de conversa local, memória e aprendizagem neste computador"},
    "NEO удаляет известные сообщения этого личного чата — ваши команды и ответы NEO. Telegram разрешает боту удалять только сообщения младше 48 часов; более старые и сообщения без известного ID нужно удалить вручную.": {"en":"NEO deletes the known messages of this private chat — your commands and NEO’s replies. Telegram lets a bot delete only messages younger than 48 hours; older messages and messages without a known ID have to be deleted by hand.","de":"NEO löscht die bekannten Nachrichten dieses privaten Chats – deine Befehle und die Antworten von NEO. Telegram erlaubt einem Bot nur das Löschen von Nachrichten, die jünger als 48 Stunden sind; ältere und solche ohne bekannte ID musst du manuell löschen.","fr":"NEO supprime les messages connus de ce chat privé : vos commandes et les réponses de NEO. Telegram ne permet à un bot de supprimer que les messages de moins de 48 heures ; les plus anciens et ceux sans ID connu doivent être supprimés à la main.","es":"NEO elimina los mensajes conocidos de este chat privado: tus órdenes y las respuestas de NEO. Telegram solo permite a un bot borrar mensajes de menos de 48 horas; los más antiguos y los que no tienen un ID conocido hay que borrarlos a mano.","it":"NEO elimina i messaggi noti di questa chat privata: i tuoi comandi e le risposte di NEO. Telegram consente a un bot di eliminare solo i messaggi più recenti di 48 ore; quelli più vecchi e quelli senza ID noto vanno eliminati a mano.","pt":"O NEO apaga as mensagens conhecidas deste chat privado: os teus comandos e as respostas do NEO. O Telegram só deixa um bot apagar mensagens com menos de 48 horas; as mais antigas e as sem ID conhecido têm de ser apagadas manualmente."},
    "Использовать мои исправления команд для обучения моделей NEO (обезличенно)": {"en":"Use my command corrections to train NEO models (anonymized)","de":"Meine Befehlskorrekturen zum Training der NEO-Modelle verwenden (anonymisiert)","fr":"Utiliser mes corrections de commandes pour entraîner les modèles NEO (anonymisées)","es":"Usar mis correcciones de órdenes para entrenar los modelos de NEO (anonimizadas)","it":"Usa le mie correzioni dei comandi per addestrare i modelli NEO (anonimizzate)","pt":"Usar as minhas correções de comandos para treinar os modelos do NEO (anonimizadas)"},
    "Исправления команд работают сразу и без обучения. Для обучения моделей NEO используются только разрешённые исправления, и только в обезличенном виде. Уже обученная модель может сохранять влияние прежних данных: запрет действует на следующие обучения.": {"en":"Command corrections work at once, without training. Only allowed corrections are used to train NEO models, and only anonymized. A model that is already trained may keep the influence of earlier data: turning this off applies to future training.","de":"Befehlskorrekturen wirken sofort, ohne Training. Zum Training der NEO-Modelle werden nur erlaubte Korrekturen verwendet, und nur anonymisiert. Ein bereits trainiertes Modell kann den Einfluss früherer Daten behalten: Das Abschalten gilt für künftige Trainings.","fr":"Les corrections de commandes fonctionnent immédiatement, sans entraînement. Seules les corrections autorisées servent à entraîner les modèles NEO, et uniquement anonymisées. Un modèle déjà entraîné peut garder l’influence des données passées : la désactivation vaut pour les entraînements futurs.","es":"Las correcciones de órdenes funcionan al instante, sin entrenamiento. Para entrenar los modelos de NEO solo se usan las correcciones permitidas, y solo anonimizadas. Un modelo ya entrenado puede conservar la influencia de datos anteriores: desactivarlo afecta a los entrenamientos futuros.","it":"Le correzioni dei comandi funzionano subito, senza addestramento. Per addestrare i modelli NEO si usano solo le correzioni consentite, e solo anonimizzate. Un modello già addestrato può conservare l’influenza dei dati precedenti: la disattivazione vale per gli addestramenti futuri.","pt":"As correções de comandos funcionam logo, sem treino. Para treinar os modelos do NEO só se usam as correções permitidas, e apenas anonimizadas. Um modelo já treinado pode manter a influência de dados anteriores: desligar aplica-se aos treinos futuros."},
    "Имена и обращение": {"en":"Names and address","de":"Namen und Anrede","fr":"Noms et façon de s’adresser","es":"Nombres y trato","it":"Nomi e modo di rivolgersi","pt":"Nomes e tratamento"},
    "Характер и стиль общения": {"en":"Personality and communication style","de":"Persönlichkeit und Kommunikationsstil","fr":"Personnalité et style de communication","es":"Personalidad y estilo de comunicación","it":"Personalità e stile di comunicazione","pt":"Personalidade e estilo de comunicação"},
    "Поведение голосового диалога": {"en":"Voice conversation behavior","de":"Verhalten des Sprachdialogs","fr":"Comportement du dialogue vocal","es":"Comportamiento del diálogo por voz","it":"Comportamento del dialogo vocale","pt":"Comportamento do diálogo por voz"},
    "Распознавание речи": {"en":"Speech recognition","de":"Spracherkennung","fr":"Reconnaissance vocale","es":"Reconocimiento de voz","it":"Riconoscimento vocale","pt":"Reconhecimento de voz"},
    "Фраза активации": {"en":"Wake phrase","de":"Aktivierungsphrase","fr":"Phrase d'activation","es":"Frase de activación","it":"Frase di attivazione","pt":"Frase de ativação"},
    "Основной wake word": {"en":"Primary wake word","de":"Primäres Wake Word","fr":"Mot d'activation principal","es":"Palabra de activación principal","it":"Parola di attivazione principale","pt":"Palavra de ativação principal"},
    "Варианты wake word": {"en":"Wake-word variants","de":"Wake-Word-Varianten","fr":"Variantes du mot d'activation","es":"Variantes de la palabra de activación","it":"Varianti della parola di attivazione","pt":"Variações da palavra de ativação"},
    "Разрешить голосовой ввод": {"en":"Enable voice input","de":"Spracheingabe aktivieren","fr":"Activer la saisie vocale","es":"Activar entrada de voz","it":"Abilita input vocale","pt":"Ativar entrada de voz"},
    "Включить быстрые команды без wake word (дальше / скип / стоп / пауза / назад / лайк / дизлайк)": {"en":"Enable quick commands without wake word (next / skip / stop / pause / back / like / dislike)","de":"Schnellbefehle ohne Wake Word aktivieren (weiter / skip / stopp / pause / zurück / like / dislike)","fr":"Activer les commandes rapides sans mot d'activation (suivant / skip / stop / pause / retour / like / dislike)","es":"Activar comandos rápidos sin palabra de activación (siguiente / skip / stop / pausa / atrás / like / dislike)","it":"Abilita comandi rapidi senza parola di attivazione (avanti / skip / stop / pausa / indietro / like / dislike)","pt":"Ativar comandos rápidos sem palavra de ativação (próxima / skip / stop / pausa / voltar / like / dislike)"},
    "Папки данных NEO": {"en":"NEO data folders","de":"NEO-Datenordner","fr":"Dossiers de données NEO","es":"Carpetas de datos de NEO","it":"Cartelle dati NEO","pt":"Pastas de dados do NEO"},
    "Основные данные": {"en":"Main data","de":"Hauptdaten","fr":"Données principales","es":"Datos principales","it":"Dati principali","pt":"Dados principais"},
    "Логи": {"en":"Logs","de":"Protokolle","fr":"Journaux","es":"Registros","it":"Log","pt":"Logs"},
    "Скриншоты": {"en":"Screenshots","de":"Screenshots","fr":"Captures d’écran","es":"Capturas de pantalla","it":"Screenshot","pt":"Capturas de tela"},
    "Резервные копии": {"en":"Backups","de":"Sicherungen","fr":"Sauvegardes","es":"Copias de seguridad","it":"Backup","pt":"Backups"},
    "Кэш": {"en":"Cache","de":"Cache","fr":"Cache","es":"Caché","it":"Cache","pt":"Cache"},
    "Выбрать": {"en":"Browse","de":"Auswählen","fr":"Parcourir","es":"Elegir","it":"Scegli","pt":"Escolher"},
    "Открыть": {"en":"Open","de":"Öffnen","fr":"Ouvrir","es":"Abrir","it":"Apri","pt":"Abrir"},
    "Путь к папке": {"en":"Folder path","de":"Ordnerpfad","fr":"Chemin du dossier","es":"Ruta de carpeta","it":"Percorso cartella","pt":"Caminho da pasta"},
    "Папка": {"en":"Folder","de":"Ordner","fr":"Dossier","es":"Carpeta","it":"Cartella","pt":"Pasta"},
    "Горячие клавиши": {"en":"Hotkeys","de":"Hotkeys","fr":"Raccourcis clavier","es":"Atajos de teclado","it":"Scorciatoie da tastiera","pt":"Atalhos de teclado"},
    "Вкл./выкл. микрофон NEO": {"en":"Toggle NEO microphone","de":"NEO-Mikrofon ein/aus","fr":"Activer/désactiver le microphone NEO","es":"Activar/desactivar el micrófono NEO","it":"Attiva/disattiva microfono NEO","pt":"Ativar/desativar microfone NEO"},
    "Выключить микрофон для NEO": {"en":"Mute microphone for NEO","de":"Mikrofon für NEO stummschalten","fr":"Couper le microphone pour NEO","es":"Silenciar el micrófono para NEO","it":"Disattiva microfono per NEO","pt":"Silenciar microfone para NEO"},
    "Включить микрофон для NEO": {"en":"Unmute microphone for NEO","de":"Mikrofon für NEO einschalten","fr":"Réactiver le microphone pour NEO","es":"Reactivar el micrófono para NEO","it":"Riattiva microfono per NEO","pt":"Reativar microfone para NEO"},
    "Микрофон NEO выключен": {"en":"NEO microphone is muted","de":"NEO-Mikrofon ist stummgeschaltet","fr":"Le microphone NEO est coupé","es":"El micrófono de NEO está silenciado","it":"Il microfono NEO è disattivato","pt":"O microfone do NEO está silenciado"},
    "Яндекс Музыка": {"en":"Yandex Music","de":"Yandex Music","fr":"Yandex Music","es":"Yandex Music","it":"Yandex Music","pt":"Yandex Music"},
    "Серверы Discord": {"en":"Discord servers","de":"Discord-Server","fr":"Serveurs Discord","es":"Servidores de Discord","it":"Server Discord","pt":"Servidores Discord"},
    "Браузер для YouTube": {"en":"Browser for YouTube","de":"Browser für YouTube","fr":"Navigateur pour YouTube","es":"Navegador para YouTube","it":"Browser per YouTube","pt":"Navegador para YouTube"},
    "Управление компьютером": {"en":"Computer control","de":"Computersteuerung","fr":"Contrôle de l'ordinateur","es":"Control del ordenador","it":"Controllo del computer","pt":"Controle do computador"},
    "Словарь и сценарии": {"en":"Dictionary and scenarios","de":"Wörterbuch und Szenarien","fr":"Dictionnaire et scénarios","es":"Diccionario y escenarios","it":"Dizionario e scenari","pt":"Dicionário e cenários"},
    "Информационные окна / Overlay": {"en":"Information panels / Overlay","de":"Informationsfenster / Overlay","fr":"Fenêtres d'information / Overlay","es":"Paneles de información / Overlay","it":"Pannelli informativi / Overlay","pt":"Painéis de informação / Overlay"},
    "Имя": {"en":"Name","de":"Name","fr":"Nom","es":"Nombre","it":"Nome","pt":"Nome"},
    "Имя пользователя": {"en":"User name","de":"Benutzername","fr":"Nom de l’utilisateur","es":"Nombre del usuario","it":"Nome utente","pt":"Nome do usuário"},
    "Использовать имя в каждом ответе": {"en":"Use name in every reply","de":"Name in jeder Antwort verwenden","fr":"Utiliser le nom dans chaque réponse","es":"Usar el nombre en cada respuesta","it":"Usa il nome in ogni risposta","pt":"Usar o nome em cada resposta"},
    "Имя для персонального обращения. По умолчанию — Boss. Галочка справа включает обращение по имени в каждом ответе; при выключении имя не используется. Само имя сохраняется как введено и не переводится.": {"en":"Name used for personal address. Default: Boss. The checkbox on the right adds the name to every reply; when disabled, the name is not used. The entered name is preserved and is not translated.","de":"Name für die persönliche Anrede. Standard: Boss. Das Kontrollkästchen rechts fügt den Namen jeder Antwort hinzu; wenn es deaktiviert ist, wird der Name nicht verwendet. Der eingegebene Name bleibt unverändert und wird nicht übersetzt.","fr":"Nom utilisé pour s’adresser personnellement à l’utilisateur. Par défaut : Boss. La case à droite ajoute le nom à chaque réponse ; lorsqu’elle est désactivée, le nom n’est pas utilisé. Le nom saisi est conservé tel quel et n’est pas traduit.","es":"Nombre utilizado para dirigirse personalmente al usuario. Predeterminado: Boss. La casilla de la derecha añade el nombre a cada respuesta; si está desactivada, el nombre no se usa. El nombre introducido se conserva y no se traduce.","it":"Nome usato per rivolgersi personalmente all’utente. Predefinito: Boss. La casella a destra aggiunge il nome a ogni risposta; se è disattivata, il nome non viene usato. Il nome inserito viene mantenuto invariato e non tradotto.","pt":"Nome usado para se dirigir pessoalmente ao usuário. Padrão: Boss. A caixa à direita adiciona o nome a cada resposta; quando desativada, o nome não é usado. O nome digitado é preservado e não é traduzido."},
    "Промпт": {"en":"Prompt","de":"Prompt","fr":"Prompt","es":"Prompt","it":"Prompt","pt":"Prompt"},
    "Адрес": {"en":"Address","de":"Adresse","fr":"Adresse","es":"Dirección","it":"Indirizzo","pt":"Endereço"},
    "Модель": {"en":"Model","de":"Modell","fr":"Modèle","es":"Modelo","it":"Modello","pt":"Modelo"},
    "Модель диалога": {"en":"Chat model","de":"Dialogmodell","fr":"Modèle de dialogue","es":"Modelo de chat","it":"Modello di dialogo","pt":"Modelo de diálogo"},
    "Модель изображения": {"en":"Vision model","de":"Bildmodell","fr":"Modèle vision","es":"Modelo de visión","it":"Modello visivo","pt":"Modelo de visão"},
    "Модель распознавания": {"en":"Recognition model","de":"Erkennungsmodell","fr":"Modèle de reconnaissance","es":"Modelo de reconocimiento","it":"Modello di riconoscimento","pt":"Modelo de reconhecimento"},
    "Устройство ввода": {"en":"Input device","de":"Eingabegerät","fr":"Périphérique d'entrée","es":"Dispositivo de entrada","it":"Dispositivo di input","pt":"Dispositivo de entrada"},
    "Чувствительность микрофона": {"en":"Microphone sensitivity","de":"Mikrofonempfindlichkeit","fr":"Sensibilité du microphone","es":"Sensibilidad del micrófono","it":"Sensibilità del microfono","pt":"Sensibilidade do microfone"},
    "Отсечение фонового шума": {"en":"Background-noise rejection","de":"Unterdrückung von Hintergrundgeräuschen","fr":"Réduction du bruit de fond","es":"Reducción del ruido de fondo","it":"Riduzione del rumore di fondo","pt":"Redução de ruído de fundo"},
    # UI185-REMOTE-COMMAND-LENGTH-I18N-01: a number of characters, not a duration.
    "Максимальная длина команды": {"en":"Maximum command length","de":"Maximale Befehlslänge","fr":"Longueur maximale de la commande","es":"Longitud máxima del comando","it":"Lunghezza massima del comando","pt":"Comprimento máximo do comando"},
    "Пауза завершает команду": {"en":"Silence ends a command","de":"Stille beendet einen Befehl","fr":"Le silence termine une commande","es":"El silencio finaliza un comando","it":"Il silenzio termina un comando","pt":"O silêncio encerra um comando"},
    "Ожидание начала речи": {"en":"Speech-start timeout","de":"Wartezeit auf Sprachbeginn","fr":"Délai de début de parole","es":"Tiempo de espera para empezar a hablar","it":"Timeout inizio parlato","pt":"Tempo para início da fala"},
    "Режим после wake word": {"en":"Mode after wake word","de":"Modus nach Wake Word","fr":"Mode après mot d'activation","es":"Modo tras palabra de activación","it":"Modalità dopo la parola di attivazione","pt":"Modo após palavra de ativação"},
    "Стоп-фразы непрерывного режима": {"en":"Continuous-mode stop phrases","de":"Stopp-Phrasen im Dauermodus","fr":"Phrases d'arrêt du mode continu","es":"Frases de parada del modo continuo","it":"Frasi di stop della modalità continua","pt":"Frases de parada do modo contínuo"},
    "Показать установленные модели": {"en":"Show installed models","de":"Installierte Modelle anzeigen","fr":"Afficher les modèles installés","es":"Mostrar modelos instalados","it":"Mostra modelli installati","pt":"Mostrar modelos instalados"},
    "Проверить Ollama": {"en":"Test Ollama","de":"Ollama testen","fr":"Tester Ollama","es":"Probar Ollama","it":"Testa Ollama","pt":"Testar Ollama"},
    "Добавить": {"en":"Add","de":"Hinzufügen","fr":"Ajouter","es":"Añadir","it":"Aggiungi","pt":"Adicionar"},
    "Удалить": {"en":"Delete","de":"Löschen","fr":"Supprimer","es":"Eliminar","it":"Elimina","pt":"Excluir"},
    "Сохранить": {"en":"Save","de":"Speichern","fr":"Enregistrer","es":"Guardar","it":"Salva","pt":"Salvar"},
    "Применить": {"en":"Apply","de":"Übernehmen","fr":"Appliquer","es":"Aplicar","it":"Applica","pt":"Aplicar"},
    "Отмена": {"en":"Cancel","de":"Abbrechen","fr":"Annuler","es":"Cancelar","it":"Annulla","pt":"Cancelar"},
    "Выбрать": {"en":"Choose","de":"Auswählen","fr":"Choisir","es":"Elegir","it":"Scegli","pt":"Escolher"},
    "Название": {"en":"Name","de":"Name","fr":"Nom","es":"Nombre","it":"Nome","pt":"Nome"},
    "Сервер": {"en":"Server","de":"Server","fr":"Serveur","es":"Servidor","it":"Server","pt":"Servidor"},
    "Путь": {"en":"Path","de":"Pfad","fr":"Chemin","es":"Ruta","it":"Percorso","pt":"Caminho"},
    "Тип": {"en":"Type","de":"Typ","fr":"Type","es":"Tipo","it":"Tipo","pt":"Tipo"},
    "Режим": {"en":"Mode","de":"Modus","fr":"Mode","es":"Modo","it":"Modalità","pt":"Modo"},
    "Основной": {"en":"Primary","de":"Primär","fr":"Principal","es":"Principal","it":"Principale","pt":"Principal"},
    "Пользовательский": {"en":"Custom","de":"Benutzerdefiniert","fr":"Personnalisé","es":"Personalizado","it":"Personalizzato","pt":"Personalizado"},
    "Выключено": {"en":"Disabled","de":"Deaktiviert","fr":"Désactivé","es":"Desactivado","it":"Disattivato","pt":"Desativado"},
    "Напишите команду…": {"en":"Type a command…","de":"Befehl eingeben…","fr":"Saisissez une commande…","es":"Escribe un comando…","it":"Scrivi un comando…","pt":"Digite um comando…"},
    "Ожидание": {"en":"Idle","de":"Bereit","fr":"En attente","es":"En espera","it":"In attesa","pt":"Aguardando"},
    "Думаю…": {"en":"Thinking…","de":"Denke nach…","fr":"Réflexion…","es":"Pensando…","it":"Sto pensando…","pt":"Pensando…"},
    "Размышляю…": {"en":"Thinking…","de":"Denke nach…","fr":"Réflexion…","es":"Pensando…","it":"Sto riflettendo…","pt":"Pensando…"},
    "Отвечаю…": {"en":"Responding…","de":"Antworte…","fr":"Réponse…","es":"Respondiendo…","it":"Rispondo…","pt":"Respondendo…"},
    "Слушаю.": {"en":"Listening.","de":"Ich höre zu.","fr":"J'écoute.","es":"Escuchando.","it":"Ti ascolto.","pt":"Estou ouvindo."},
    "Слушаю…": {"en":"Listening…","de":"Höre zu…","fr":"Écoute…","es":"Escuchando…","it":"In ascolto…","pt":"Ouvindo…"},
    "Распознаю вашу речь…": {"en":"Recognizing your speech…","de":"Sprache wird erkannt…","fr":"Reconnaissance de votre voix…","es":"Reconociendo tu voz…","it":"Riconoscimento della voce…","pt":"Reconhecendo sua fala…"},
    "Ошибка": {"en":"Error","de":"Fehler","fr":"Erreur","es":"Error","it":"Errore","pt":"Erro"},
    "Речь не обнаружена": {"en":"No speech detected","de":"Keine Sprache erkannt","fr":"Aucune parole détectée","es":"No se detectó voz","it":"Nessuna voce rilevata","pt":"Nenhuma fala detectada"},
    "Речь не распознана": {"en":"Speech not recognized","de":"Sprache nicht erkannt","fr":"Parole non reconnue","es":"Voz no reconocida","it":"Voce non riconosciuta","pt":"Fala não reconhecida"},
    "Команда не распознана": {"en":"Command not recognized","de":"Befehl nicht erkannt","fr":"Commande non reconnue","es":"Comando no reconocido","it":"Comando non riconosciuto","pt":"Comando não reconhecido"},
    "Настройки сохранены": {"en":"Settings saved","de":"Einstellungen gespeichert","fr":"Paramètres enregistrés","es":"Configuración guardada","it":"Impostazioni salvate","pt":"Configurações salvas"},
    "Новый диалог начат.": {"en":"New conversation started.","de":"Neue Unterhaltung gestartet.","fr":"Nouvelle conversation démarrée.","es":"Nueva conversación iniciada.","it":"Nuova conversazione avviata.","pt":"Nova conversa iniciada."},
    "Полностью завершить": {"en":"Quit completely","de":"Vollständig beenden","fr":"Quitter complètement","es":"Salir por completo","it":"Chiudi completamente","pt":"Encerrar completamente"},
    "Настройки": {"en":"Settings","de":"Einstellungen","fr":"Paramètres","es":"Configuración","it":"Impostazioni","pt":"Configurações"},
    "Скрыть": {"en":"Hide","de":"Ausblenden","fr":"Masquer","es":"Ocultar","it":"Nascondi","pt":"Ocultar"},
    "Показать NEO": {"en":"Show NEO","de":"NEO anzeigen","fr":"Afficher NEO","es":"Mostrar NEO","it":"Mostra NEO","pt":"Mostrar NEO"},
    "Новый диалог": {"en":"New conversation","de":"Neue Unterhaltung","fr":"Nouvelle conversation","es":"Nueva conversación","it":"Nuova conversazione","pt":"Nova conversa"},
}

# Phrase-level glossary used for labels/tooltips not explicitly listed above.
GLOSSARY: dict[str, dict[str, str]] = {
    "en": {
        "Настройки":"Settings","Микрофон":"Microphone","Монитор":"Monitor","Мониторы":"Monitors","Сообщения":"Messages","Панели":"Panels","Рамки":"Borders","Шрифт":"Font","Высота":"Height","Ширина":"Width","Прозрачность":"Opacity","Цвет":"Color","фон":"background","Фон":"Background","текст":"text","Текст":"Text","модель":"model","Модель":"Model","плеер":"player","голос":"voice","голосовой":"voice","браузер":"browser","приложение":"application","управление":"control","Ожидание":"Timeout","Скорость":"Speed","Размер":"Size","Основной":"Primary","Дополнительный":"Secondary","Разрешить":"Enable","Показывать":"Show","Использовать":"Use","Автоматически":"Automatically","Обновить":"Refresh","Добавить":"Add","Удалить":"Delete","Сохранить":"Save","Проверка":"Check","Проверить":"Check","выбранный":"selected","файлов":"files","файлами":"files","папки":"folders","папок":"folders","сценарии":"scenarios","Сценарии":"Scenarios","словарь":"dictionary","Словарь":"Dictionary","игровой":"game","сессии":"sessions","сессия":"session","Оформление":"Appearance","интерфейса":"interface","интерфейс":"interface","окна":"window","Окно":"Window","робота":"robot","Робот":"Robot","экрана":"screen","Экран":"Screen","выбран":"selected","команды":"commands","команда":"command","речь":"speech","распознавание":"recognition","активации":"activation","Подключение":"Connection","Качество":"Quality","нагрузка":"performance","Безопасность":"Security","Дополнительно":"Advanced","Поведение":"Behavior","Пользовательский":"Custom","Выключено":"Disabled","секунд":"seconds","сек":"sec","токенов":"tokens","минут":"minutes","Введите":"Enter","Обычно":"Usually","Рекомендуется":"Recommended","Нужен":"Required","Нужна":"Required","необяз.":"optional","необязательный":"optional","по умолчанию":"by default","в фоне":"in background","без":"without","мышь":"mouse","клавиатуру":"keyboard","окно":"window","язык":"language","ассистента":"assistant","интеграцию":"integration","сервер":"server","канал":"channel","устройство":"device","громкость":"volume","размер":"size","позиция":"position","порог":"threshold","чувствительность":"sensitivity","включить":"enable","выключить":"disable"},
    "de": {
        "Настройки":"Einstellungen","Микрофон":"Mikrofon","Монитор":"Monitor","Мониторы":"Monitore","Сообщения":"Nachrichten","Панели":"Panels","Рамки":"Rahmen","Шрифт":"Schrift","Высота":"Höhe","Ширина":"Breite","Прозрачность":"Transparenz","Цвет":"Farbe","фон":"Hintergrund","Фон":"Hintergrund","текст":"Text","Текст":"Text","модель":"Modell","Модель":"Modell","плеер":"Player","голос":"Stimme","голосовой":"Sprach-","браузер":"Browser","приложение":"Anwendung","управление":"Steuerung","Ожидание":"Wartezeit","Скорость":"Geschwindigkeit","Размер":"Größe","Основной":"Primär","Дополнительный":"Zusätzlich","Разрешить":"Aktivieren","Показывать":"Anzeigen","Использовать":"Verwenden","Автоматически":"Automatisch","Обновить":"Aktualisieren","Добавить":"Hinzufügen","Удалить":"Löschen","Сохранить":"Speichern","Проверка":"Prüfung","Проверить":"Prüfen","файлов":"Dateien","папки":"Ordner","папок":"Ordner","сценарии":"Szenarien","Сценарии":"Szenarien","словарь":"Wörterbuch","Словарь":"Wörterbuch","игровой":"Spiel-","сессии":"Sitzungen","сессия":"Sitzung","Оформление":"Darstellung","интерфейса":"Oberfläche","интерфейс":"Oberfläche","окна":"Fenster","Окно":"Fenster","робота":"Roboters","Робот":"Roboter","экрана":"Bildschirm","Экран":"Bildschirm","команды":"Befehle","команда":"Befehl","речь":"Sprache","распознавание":"Erkennung","активации":"Aktivierung","Подключение":"Verbindung","Качество":"Qualität","нагрузка":"Leistung","Безопасность":"Sicherheit","Дополнительно":"Erweitert","Пользовательский":"Benutzerdefiniert","Выключено":"Deaktiviert","секунд":"Sekunden","сек":"Sek","токенов":"Token","минут":"Minuten","Введите":"Geben Sie ein","Обычно":"Normalerweise","Рекомендуется":"Empfohlen","Нужен":"Erforderlich","Нужна":"Erforderlich","по умолчанию":"standardmäßig","в фоне":"im Hintergrund","без":"ohne","мышь":"Maus","клавиатуру":"Tastatur","язык":"Sprache","ассистента":"Assistenten","интеграцию":"Integration","сервер":"Server","канал":"Kanal","устройство":"Gerät","громкость":"Lautstärke","размер":"Größe","позиция":"Position","порог":"Schwelle","чувствительность":"Empfindlichkeit","включить":"aktivieren","выключить":"deaktivieren"},
    "fr": {
        "Настройки":"Paramètres","Микрофон":"Microphone","Монитор":"Moniteur","Мониторы":"Moniteurs","Сообщения":"Messages","Панели":"Panneaux","Рамки":"Bordures","Шрифт":"Police","Высота":"Hauteur","Ширина":"Largeur","Прозрачность":"Transparence","Цвет":"Couleur","фон":"arrière-plan","Фон":"Arrière-plan","текст":"texte","Текст":"Texte","модель":"modèle","Модель":"Modèle","плеер":"lecteur","голос":"voix","голосовой":"vocal","браузер":"navigateur","приложение":"application","управление":"contrôle","Ожидание":"Délai","Скорость":"Vitesse","Размер":"Taille","Основной":"Principal","Дополнительный":"Supplémentaire","Разрешить":"Activer","Показывать":"Afficher","Использовать":"Utiliser","Автоматически":"Automatiquement","Обновить":"Actualiser","Добавить":"Ajouter","Удалить":"Supprimer","Сохранить":"Enregistrer","Проверка":"Vérification","Проверить":"Vérifier","файлов":"fichiers","папки":"dossiers","папок":"dossiers","сценарии":"scénarios","Сценарии":"Scénarios","словарь":"dictionnaire","Словарь":"Dictionnaire","игровой":"jeu","сессии":"sessions","сессия":"session","Оформление":"Apparence","интерфейса":"interface","интерфейс":"interface","окна":"fenêtre","Окно":"Fenêtre","робота":"robot","Робот":"Robot","экрана":"écran","Экран":"Écran","команды":"commandes","команда":"commande","речь":"parole","распознавание":"reconnaissance","активации":"activation","Подключение":"Connexion","Качество":"Qualité","нагрузка":"performances","Безопасность":"Sécurité","Дополнительно":"Avancé","Пользовательский":"Personnalisé","Выключено":"Désactivé","секунд":"secondes","сек":"s","токенов":"jetons","минут":"minutes","Введите":"Entrez","Обычно":"En général","Рекомендуется":"Recommandé","Нужен":"Requis","Нужна":"Requise","по умолчанию":"par défaut","в фоне":"en arrière-plan","без":"sans","мышь":"souris","клавиатуру":"clavier","язык":"langue","ассистента":"assistant","интеграцию":"intégration","сервер":"serveur","канал":"canal","устройство":"périphérique","громкость":"volume","размер":"taille","позиция":"position","порог":"seuil","чувствительность":"sensibilité","включить":"activer","выключить":"désactiver"},
    "es": {
        "Настройки":"Configuración","Микрофон":"Micrófono","Монитор":"Monitor","Мониторы":"Monitores","Сообщения":"Mensajes","Панели":"Paneles","Рамки":"Bordes","Шрифт":"Fuente","Высота":"Altura","Ширина":"Anchura","Прозрачность":"Transparencia","Цвет":"Color","фон":"fondo","Фон":"Fondo","текст":"texto","Текст":"Texto","модель":"modelo","Модель":"Modelo","плеер":"reproductor","голос":"voz","голосовой":"de voz","браузер":"navegador","приложение":"aplicación","управление":"control","Ожидание":"Espera","Скорость":"Velocidad","Размер":"Tamaño","Основной":"Principal","Дополнительный":"Adicional","Разрешить":"Activar","Показывать":"Mostrar","Использовать":"Usar","Автоматически":"Automáticamente","Обновить":"Actualizar","Добавить":"Añadir","Удалить":"Eliminar","Сохранить":"Guardar","Проверка":"Comprobación","Проверить":"Comprobar","файлов":"archivos","папки":"carpetas","папок":"carpetas","сценарии":"escenarios","Сценарии":"Escenarios","словарь":"diccionario","Словарь":"Diccionario","игровой":"de juego","сессии":"sesiones","сессия":"sesión","Оформление":"Apariencia","интерфейса":"interfaz","интерфейс":"interfaz","окна":"ventana","Окно":"Ventana","робота":"robot","Робот":"Robot","экрана":"pantalla","Экран":"Pantalla","команды":"comandos","команда":"comando","речь":"voz","распознавание":"reconocimiento","активации":"activación","Подключение":"Conexión","Качество":"Calidad","нагрузка":"rendimiento","Безопасность":"Seguridad","Дополнительно":"Avanzado","Пользовательский":"Personalizado","Выключено":"Desactivado","секунд":"segundos","сек":"s","токенов":"tokens","минут":"minutos","Введите":"Introduce","Обычно":"Normalmente","Рекомендуется":"Recomendado","Нужен":"Necesario","Нужна":"Necesaria","по умолчанию":"por defecto","в фоне":"en segundo plano","без":"sin","мышь":"ratón","клавиатуру":"teclado","язык":"idioma","ассистента":"asistente","интеграцию":"integración","сервер":"servidor","канал":"canal","устройство":"dispositivo","громкость":"volumen","размер":"tamaño","позиция":"posición","порог":"umbral","чувствительность":"sensibilidad","включить":"activar","выключить":"desactivar"},
    "it": {
        "Настройки":"Impostazioni","Микрофон":"Microfono","Монитор":"Monitor","Мониторы":"Monitor","Сообщения":"Messaggi","Панели":"Pannelli","Рамки":"Bordi","Шрифт":"Font","Высота":"Altezza","Ширина":"Larghezza","Прозрачность":"Trasparenza","Цвет":"Colore","фон":"sfondo","Фон":"Sfondo","текст":"testo","Текст":"Testo","модель":"modello","Модель":"Modello","плеер":"player","голос":"voce","голосовой":"vocale","браузер":"browser","приложение":"applicazione","управление":"controllo","Ожидание":"Attesa","Скорость":"Velocità","Размер":"Dimensione","Основной":"Principale","Дополнительный":"Aggiuntivo","Разрешить":"Abilita","Показывать":"Mostra","Использовать":"Usa","Автоматически":"Automaticamente","Обновить":"Aggiorna","Добавить":"Aggiungi","Удалить":"Elimina","Сохранить":"Salva","Проверка":"Controllo","Проверить":"Controlla","файлов":"file","папки":"cartelle","папок":"cartelle","сценарии":"scenari","Сценарии":"Scenari","словарь":"dizionario","Словарь":"Dizionario","игровой":"di gioco","сессии":"sessioni","сессия":"sessione","Оформление":"Aspetto","интерфейса":"interfaccia","интерфейс":"interfaccia","окна":"finestra","Окно":"Finestra","робота":"robot","Робот":"Robot","экрана":"schermo","Экран":"Schermo","команды":"comandi","команда":"comando","речь":"voce","распознавание":"riconoscimento","активации":"attivazione","Подключение":"Connessione","Качество":"Qualità","нагрузка":"prestazioni","Безопасность":"Sicurezza","Дополнительно":"Avanzate","Пользовательский":"Personalizzato","Выключено":"Disattivato","секунд":"secondi","сек":"s","токенов":"token","минут":"minuti","Введите":"Inserisci","Обычно":"Di solito","Рекомендуется":"Consigliato","Нужен":"Richiesto","Нужна":"Richiesta","по умолчанию":"predefinito","в фоне":"in background","без":"senza","мышь":"mouse","клавиатуру":"tastiera","язык":"lingua","ассистента":"assistente","интеграцию":"integrazione","сервер":"server","канал":"canale","устройство":"dispositivo","громкость":"volume","размер":"dimensione","позиция":"posizione","порог":"soglia","чувствительность":"sensibilità","включить":"abilitare","выключить":"disabilitare"},
    "pt": {
        "Настройки":"Configurações","Микрофон":"Microfone","Монитор":"Monitor","Мониторы":"Monitores","Сообщения":"Mensagens","Панели":"Painéis","Рамки":"Bordas","Шрифт":"Fonte","Высота":"Altura","Ширина":"Largura","Прозрачность":"Transparência","Цвет":"Cor","фон":"fundo","Фон":"Fundo","текст":"texto","Текст":"Texto","модель":"modelo","Модель":"Modelo","плеер":"player","голос":"voz","голосовой":"de voz","браузер":"navegador","приложение":"aplicativo","управление":"controle","Ожидание":"Espera","Скорость":"Velocidade","Размер":"Tamanho","Основной":"Principal","Дополнительный":"Adicional","Разрешить":"Ativar","Показывать":"Mostrar","Использовать":"Usar","Автоматически":"Automaticamente","Обновить":"Atualizar","Добавить":"Adicionar","Удалить":"Excluir","Сохранить":"Salvar","Проверка":"Verificação","Проверить":"Verificar","файлов":"arquivos","папки":"pastas","папок":"pastas","сценарии":"cenários","Сценарии":"Cenários","словарь":"dicionário","Словарь":"Dicionário","игровой":"de jogo","сессии":"sessões","сессия":"sessão","Оформление":"Aparência","интерфейса":"interface","интерфейс":"interface","окна":"janela","Окно":"Janela","робота":"robô","Робот":"Robô","экрана":"tela","Экран":"Tela","команды":"comandos","команда":"comando","речь":"fala","распознавание":"reconhecimento","активации":"ativação","Подключение":"Conexão","Качество":"Qualidade","нагрузка":"desempenho","Безопасность":"Segurança","Дополнительно":"Avançado","Пользовательский":"Personalizado","Выключено":"Desativado","секунд":"segundos","сек":"s","токенов":"tokens","минут":"minutos","Введите":"Digite","Обычно":"Normalmente","Рекомендуется":"Recomendado","Нужен":"Necessário","Нужна":"Necessária","по умолчанию":"por padrão","в фоне":"em segundo plano","без":"sem","мышь":"mouse","клавиатуру":"teclado","язык":"idioma","ассистента":"assistente","интеграцию":"integração","сервер":"servidor","канал":"canal","устройство":"dispositivo","громкость":"volume","размер":"tamanho","позиция":"posição","порог":"limiar","чувствительность":"sensibilidade","включить":"ativar","выключить":"desativar"},
}


# Additional high-frequency UI/runtime chrome. User-entered values are never passed
# through this table; it is only used for application-owned captions and statuses.
EXACT.update({
    "Система готова. Напишите команду или нажмите микрофон.": {
        "en":"System ready. Type a command or press the microphone.",
        "de":"System bereit. Geben Sie einen Befehl ein oder drücken Sie das Mikrofon.",
        "fr":"Système prêt. Saisissez une commande ou appuyez sur le microphone.",
        "es":"Sistema listo. Escriba un comando o pulse el micrófono.",
        "it":"Sistema pronto. Digita un comando o premi il microfono.",
        "pt":"Sistema pronto. Digite um comando ou pressione o microfone."
    },
    "Речь не обнаружена. Увеличьте чувствительность или выберите другой микрофон.": {
        "en":"No speech detected. Increase sensitivity or choose another microphone.",
        "de":"Keine Sprache erkannt. Erhöhen Sie die Empfindlichkeit oder wählen Sie ein anderes Mikrofon.",
        "fr":"Aucune parole détectée. Augmentez la sensibilité ou choisissez un autre microphone.",
        "es":"No se detectó voz. Aumente la sensibilidad o elija otro micrófono.",
        "it":"Nessuna voce rilevata. Aumenta la sensibilità o scegli un altro microfono.",
        "pt":"Nenhuma fala detectada. Aumente a sensibilidade ou escolha outro microfone."
    },
    "Чек-лист пуст": {
        "en":"Checklist is empty",
        "de":"Checkliste ist leer",
        "fr":"La liste de contrôle est vide",
        "es":"La lista de comprobación está vacía",
        "it":"La checklist è vuota",
        "pt":"A lista de verificação está vazia"
    },
    "Отправить": {"en":"Send","de":"Senden","fr":"Envoyer","es":"Enviar","it":"Invia","pt":"Enviar"},
    "Голосовой ввод / остановить запись": {"en":"Voice input / stop recording","de":"Spracheingabe / Aufnahme stoppen","fr":"Saisie vocale / arrêter l'enregistrement","es":"Entrada de voz / detener grabación","it":"Input vocale / ferma registrazione","pt":"Entrada de voz / parar gravação"},
    "Показать логи запуска": {"en":"Show launcher logs","de":"Startprotokoll anzeigen","fr":"Afficher les journaux de démarrage","es":"Mostrar registros de inicio","it":"Mostra log di avvio","pt":"Mostrar logs de inicialização"},
    "Скрыть логи запуска": {"en":"Hide launcher logs","de":"Startprotokoll ausblenden","fr":"Masquer les journaux de démarrage","es":"Ocultar registros de inicio","it":"Nascondi log di avvio","pt":"Ocultar logs de inicialização"},
    "Консоль запуска недоступна": {"en":"Launcher console is unavailable","de":"Startkonsole ist nicht verfügbar","fr":"La console de démarrage est indisponible","es":"La consola de inicio no está disponible","it":"La console di avvio non è disponibile","pt":"O console de inicialização não está disponível"},
    "Свернуть окно": {"en":"Minimize window","de":"Fenster minimieren","fr":"Réduire la fenêtre","es":"Minimizar ventana","it":"Riduci finestra","pt":"Minimizar janela"},
    "Закрыть окно (NEO останется в трее)": {"en":"Close window (NEO stays in tray)","de":"Fenster schließen (NEO bleibt im Infobereich)","fr":"Fermer la fenêtre (NEO reste dans la zone de notification)","es":"Cerrar ventana (NEO permanece en la bandeja)","it":"Chiudi finestra (NEO resta nell'area di notifica)","pt":"Fechar janela (NEO permanece na bandeja)"},
    "Состояние / активный микрофон": {"en":"Status / active microphone","de":"Status / aktives Mikrofon","fr":"État / microphone actif","es":"Estado / micrófono activo","it":"Stato / microfono attivo","pt":"Status / microfone ativo"},
    "Изменить размер окна": {"en":"Resize window","de":"Fenstergröße ändern","fr":"Redimensionner la fenêtre","es":"Cambiar tamaño de ventana","it":"Ridimensiona finestra","pt":"Redimensionar janela"},
    "Думает…": {"en":"Thinking…","de":"Denkt nach…","fr":"Réflexion…","es":"Pensando…","it":"Sto pensando…","pt":"Pensando…"},
    "Слушаю… Говорите. Скажите «стоп», чтобы завершить диалог.": {"en":"Listening… Speak now. Say “stop” to end the conversation.","de":"Ich höre zu… Sprechen Sie. Sagen Sie „Stopp“, um den Dialog zu beenden.","fr":"J'écoute… Parlez. Dites « stop » pour terminer le dialogue.","es":"Escuchando… Hable. Diga «stop» para terminar el diálogo.","it":"In ascolto… Parla. Di' “stop” per terminare il dialogo.","pt":"Ouvindo… Fale. Diga “stop” para encerrar o diálogo."},
    "Операция прервана.": {"en":"Operation interrupted.","de":"Vorgang unterbrochen.","fr":"Opération interrompue.","es":"Operación interrumpida.","it":"Operazione interrotta.","pt":"Operação interrompida."},
    "Операция прервана — слушаю новую команду…": {"en":"Operation interrupted - listening for a new command…","de":"Vorgang unterbrochen - ich höre auf einen neuen Befehl…","fr":"Opération interrompue - j'écoute une nouvelle commande…","es":"Operación interrumpida - escuchando un nuevo comando…","it":"Operazione interrotta - ascolto un nuovo comando…","pt":"Operação interrompida - ouvindo um novo comando…"},
    "Ищу информацию…": {"en":"Searching…","de":"Suche…","fr":"Recherche…","es":"Buscando…","it":"Ricerca…","pt":"Pesquisando…"},
    "Работаю с браузером…": {"en":"Working with the browser…","de":"Arbeite mit dem Browser…","fr":"Utilisation du navigateur…","es":"Trabajando con el navegador…","it":"Lavoro con il browser…","pt":"Trabalhando com o navegador…"},
    "Читаю содержимое…": {"en":"Reading content…","de":"Lese Inhalte…","fr":"Lecture du contenu…","es":"Leyendo contenido…","it":"Lettura del contenuto…","pt":"Lendo conteúdo…"},
    "Продумываю и создаю код…": {"en":"Planning and creating code…","de":"Plane und erstelle Code…","fr":"Planification et création du code…","es":"Planificando y creando código…","it":"Pianificazione e creazione del codice…","pt":"Planejando e criando código…"},
    "Выполняю действие на компьютере…": {"en":"Performing the computer action…","de":"Führe die Computeraktion aus…","fr":"Exécution de l'action sur l'ordinateur…","es":"Ejecutando la acción en el ordenador…","it":"Esecuzione dell'azione sul computer…","pt":"Executando a ação no computador…"},
    "Ожидаю подтверждения…": {"en":"Waiting for confirmation…","de":"Warte auf Bestätigung…","fr":"En attente de confirmation…","es":"Esperando confirmación…","it":"In attesa di conferma…","pt":"Aguardando confirmação…"},
    "Выполняю подтверждённое действие…": {"en":"Performing confirmed action…","de":"Führe bestätigte Aktion aus…","fr":"Exécution de l'action confirmée…","es":"Ejecutando la acción confirmada…","it":"Esecuzione dell'azione confermata…","pt":"Executando a ação confirmada…"},
    "Подтверждение действия": {"en":"Action confirmation","de":"Aktion bestätigen","fr":"Confirmation de l'action","es":"Confirmación de acción","it":"Conferma azione","pt":"Confirmação da ação"},
    "Подготавливаю модель для быстрого ответа…": {"en":"Preparing the model for a fast response…","de":"Modell wird für eine schnelle Antwort vorbereitet…","fr":"Préparation du modèle pour une réponse rapide…","es":"Preparando el modelo para una respuesta rápida…","it":"Preparazione del modello per una risposta rapida…","pt":"Preparando o modelo para uma resposta rápida…"},
    "Диалог активен — слушаю…": {"en":"Conversation active - listening…","de":"Dialog aktiv - ich höre zu…","fr":"Dialogue actif - j'écoute…","es":"Diálogo activo - escuchando…","it":"Dialogo attivo - in ascolto…","pt":"Diálogo ativo - ouvindo…"},
    "Снова жду wake word.": {"en":"Waiting for the wake word again.","de":"Ich warte wieder auf das Wake Word.","fr":"J'attends de nouveau le mot d'activation.","es":"Vuelvo a esperar la palabra de activación.","it":"Attendo di nuovo la parola di attivazione.","pt":"Aguardando novamente a palavra de ativação."},
    "Микрофон": {"en":"Microphone","de":"Mikrofon","fr":"Microphone","es":"Micrófono","it":"Microfono","pt":"Microfone"},
    "Пауза": {"en":"Pause","de":"Pause","fr":"Pause","es":"Pausa","it":"Pausa","pt":"Pausa"},
    "Продолжить": {"en":"Resume","de":"Fortsetzen","fr":"Reprendre","es":"Reanudar","it":"Riprendi","pt":"Retomar"},
    "Следующий": {"en":"Next","de":"Weiter","fr":"Suivant","es":"Siguiente","it":"Successivo","pt":"Próxima"},
    "Предыдущий": {"en":"Previous","de":"Zurück","fr":"Précédent","es":"Anterior","it":"Precedente","pt":"Anterior"},
    "Включено": {"en":"Enabled","de":"Aktiviert","fr":"Activé","es":"Activado","it":"Attivato","pt":"Ativado"},
    "Авто": {"en":"Auto","de":"Auto","fr":"Auto","es":"Auto","it":"Auto","pt":"Auto"},
    "Низкая": {"en":"Low","de":"Niedrig","fr":"Faible","es":"Baja","it":"Bassa","pt":"Baixa"},
    "Средняя": {"en":"Medium","de":"Mittel","fr":"Moyenne","es":"Media","it":"Media","pt":"Média"},
    "Высокая": {"en":"High","de":"Hoch","fr":"Élevée","es":"Alta","it":"Alta","pt":"Alta"},
})

EXACT.update({
    "Меняет интерфейс, язык ответов и Phrase Atlas. Активен только словарь выбранного языка; словари остальных языков не подключаются до переключения языка. Имена треков, файлов, сайтов, приложений и другой пользовательский текст остаются без изменений.": {
        "en": "Changes the interface, response language and Phrase Atlas. Only the selected language corpus is active; other language corpora are not loaded until you switch languages. Track names, file names, websites, applications and other user-provided text remain unchanged.",
        "de": "Ändert Oberfläche, Antwortsprache und Phrase Atlas. Nur der Korpus der ausgewählten Sprache ist aktiv; andere Sprachkorpora werden erst beim Sprachwechsel geladen. Titel, Dateinamen, Websites, Anwendungen und anderer Benutzereingabetext bleiben unverändert.",
        "fr": "Modifie l’interface, la langue des réponses et Phrase Atlas. Seul le corpus de la langue sélectionnée est actif ; les autres corpus ne sont chargés qu’après un changement de langue. Les titres, noms de fichiers, sites, applications et autres textes fournis par l’utilisateur restent inchangés.",
        "es": "Cambia la interfaz, el idioma de las respuestas y Phrase Atlas. Solo está activo el corpus del idioma seleccionado; los demás corpus no se cargan hasta cambiar de idioma. Los nombres de pistas, archivos, sitios, aplicaciones y demás texto del usuario permanecen sin cambios.",
        "it": "Cambia l’interfaccia, la lingua delle risposte e Phrase Atlas. È attivo solo il corpus della lingua selezionata; gli altri corpus non vengono caricati finché non si cambia lingua. Titoli dei brani, nomi dei file, siti, applicazioni e altro testo dell’utente restano invariati.",
        "pt": "Altera a interface, o idioma das respostas e o Phrase Atlas. Somente o corpus do idioma selecionado fica ativo; os demais não são carregados até a troca de idioma. Nomes de faixas, arquivos, sites, aplicativos e outros textos fornecidos pelo usuário permanecem inalterados."
    },
    "По умолчанию NEO автоматически выбирает локальную модель и безопасные параметры. Режим разработчика возвращает скрытые настройки Ollama, моделей, контекста, длины ответа, таймаутов и диагностического pipeline inspector.": {
        "en": "By default, NEO selects the local model and safe parameters automatically. Developer mode exposes advanced Ollama, model, context, response-length, timeout and pipeline-inspector settings.",
        "de": "Standardmäßig wählt NEO das lokale Modell und sichere Parameter automatisch. Der Entwicklermodus zeigt erweiterte Einstellungen für Ollama, Modelle, Kontext, Antwortlänge, Zeitlimits und den Pipeline-Inspector an.",
        "fr": "Par défaut, NEO choisit automatiquement le modèle local et des paramètres sûrs. Le mode développeur affiche les réglages avancés d’Ollama, des modèles, du contexte, de la longueur des réponses, des délais et de l’inspecteur de pipeline.",
        "es": "De forma predeterminada, NEO selecciona automáticamente el modelo local y parámetros seguros. El modo desarrollador muestra ajustes avanzados de Ollama, modelos, contexto, longitud de respuesta, tiempos de espera e inspector de pipeline.",
        "it": "Per impostazione predefinita NEO sceglie automaticamente il modello locale e parametri sicuri. La modalità sviluppatore mostra le impostazioni avanzate di Ollama, modelli, contesto, lunghezza delle risposte, timeout e pipeline inspector.",
        "pt": "Por padrão, o NEO escolhe automaticamente o modelo local e parâmetros seguros. O modo de desenvolvedor exibe configurações avançadas do Ollama, modelos, contexto, tamanho da resposta, tempos limite e inspetor do pipeline."
    },
    "Static/Regex остаётся самым быстрым и приоритетным уровнем. Semantic использует компактные локальные предрасчитанные векторы NumPy без FAISS/USearch и без отдельной модели. LazyVision не делает постоянных скриншотов.": {
        "en": "Static/Regex remains the fastest and highest-priority layer. Semantic uses compact precomputed local NumPy vectors without FAISS/USearch or a separate model. LazyVision does not take continuous screenshots.",
        "de": "Static/Regex bleibt die schnellste und höchstpriorisierte Ebene. Semantic verwendet kompakte lokal vorab berechnete NumPy-Vektoren ohne FAISS/USearch und ohne separates Modell. LazyVision erstellt keine dauernden Screenshots.",
        "fr": "Static/Regex reste la couche la plus rapide et prioritaire. Semantic utilise des vecteurs NumPy locaux compacts précalculés, sans FAISS/USearch ni modèle séparé. LazyVision ne réalise pas de captures d’écran en continu.",
        "es": "Static/Regex sigue siendo la capa más rápida y prioritaria. Semantic usa vectores NumPy locales compactos precalculados, sin FAISS/USearch ni un modelo separado. LazyVision no realiza capturas de pantalla continuas.",
        "it": "Static/Regex resta il livello più rapido e prioritario. Semantic usa vettori NumPy locali compatti precalcolati, senza FAISS/USearch né un modello separato. LazyVision non acquisisce screenshot continui.",
        "pt": "Static/Regex continua sendo a camada mais rápida e prioritária. Semantic usa vetores NumPy locais compactos pré-calculados, sem FAISS/USearch nem modelo separado. O LazyVision não faz capturas de tela contínuas."
    },
})

EXACT.update({
    "Дополнительные возможности": {"en":"Additional features","de":"Zusätzliche Funktionen","fr":"Fonctions supplémentaires","es":"Funciones adicionales","it":"Funzioni aggiuntive","pt":"Recursos adicionais"},
    "Запуск и техническое отображение": {"en":"Startup and technical display","de":"Start und technische Anzeige","fr":"Démarrage et affichage technique","es":"Inicio y visualización técnica","it":"Avvio e visualizzazione tecnica","pt":"Inicialização e exibição técnica"},
    "Папки данных NEO": {"en":"NEO data folders","de":"NEO-Datenordner","fr":"Dossiers de données NEO","es":"Carpetas de datos de NEO","it":"Cartelle dati NEO","pt":"Pastas de dados do NEO"},
    "Горячие клавиши": {"en":"Hotkeys","de":"Hotkeys","fr":"Raccourcis clavier","es":"Atajos de teclado","it":"Tasti rapidi","pt":"Atalhos"},
    "Автокликер": {"en":"Autoclicker","de":"Autoklicker","fr":"Clic automatique","es":"Autoclicker","it":"Autoclicker","pt":"Autoclicker"},
    "Голосовое управление окном": {"en":"Voice window control","de":"Sprachsteuerung des Fensters","fr":"Contrôle vocal de la fenêtre","es":"Control de ventana por voz","it":"Controllo vocale finestra","pt":"Controle de janela por voz"},
})

EXACT.update({  # NEO 188 HREQ-020/021: monitoring settings
    "Мониторинг оборудования и процессов": {"en":"Hardware and process monitoring","de":"Hardware- und Prozessüberwachung","fr":"Surveillance du matériel et des processus","es":"Supervisión de hardware y procesos","it":"Monitoraggio di hardware e processi","pt":"Monitorização de hardware e processos"},
    "Предупреждать о высокой температуре процессора и видеокарты": {"en":"Warn about high CPU and GPU temperature","de":"Vor hoher CPU- und GPU-Temperatur warnen","fr":"Avertir en cas de température élevée du processeur et du GPU","es":"Avisar de temperatura alta de CPU y GPU","it":"Avvisa in caso di temperatura alta di CPU e GPU","pt":"Avisar sobre temperatura alta do processador e da GPU"},
    "Порог процессора": {"en":"CPU threshold","de":"CPU-Schwelle","fr":"Seuil du processeur","es":"Umbral de CPU","it":"Soglia CPU","pt":"Limite do processador"},
    "Порог видеокарты": {"en":"GPU threshold","de":"GPU-Schwelle","fr":"Seuil du GPU","es":"Umbral de GPU","it":"Soglia GPU","pt":"Limite da GPU"},
    "Предупреждать о резком росте нагрузки отдельных процессов": {"en":"Warn about sudden load spikes of individual processes","de":"Vor plötzlichen Lastspitzen einzelner Prozesse warnen","fr":"Avertir des pics de charge soudains de processus","es":"Avisar de picos bruscos de carga de procesos concretos","it":"Avvisa dei picchi improvvisi di carico dei singoli processi","pt":"Avisar sobre picos súbitos de carga de processos"},
    "Порог процессора для процесса": {"en":"CPU threshold per process","de":"CPU-Schwelle pro Prozess","fr":"Seuil processeur par processus","es":"Umbral de CPU por proceso","it":"Soglia CPU per processo","pt":"Limite de processador por processo"},
    "Порог памяти для процесса": {"en":"Memory threshold per process","de":"Speicherschwelle pro Prozess","fr":"Seuil mémoire par processus","es":"Umbral de memoria por proceso","it":"Soglia memoria per processo","pt":"Limite de memória por processo"},
    "Сообщать о начале и подтверждённом завершении установок Steam": {"en":"Report the start and confirmed completion of Steam installations","de":"Beginn und bestätigten Abschluss von Steam-Installationen melden","fr":"Signaler le début et la fin confirmée des installations Steam","es":"Avisar del inicio y del final confirmado de instalaciones de Steam","it":"Segnala l’inizio e la fine confermata delle installazioni di Steam","pt":"Avisar do início e da conclusão confirmada de instalações do Steam"},
    "Как часто проверять": {"en":"Check interval","de":"Prüfintervall","fr":"Fréquence de vérification","es":"Frecuencia de comprobación","it":"Frequenza di controllo","pt":"Frequência de verificação"},
    "Проверки идут только при включённых флажках. Нагрузка процесса — доля всего процессора; видеопамять отдельных процессов Windows здесь не показывает. Установка Steam считается завершённой только по отметке Steam в манифесте игры, а не по прекращению загрузки.": {"en":"Checks run only when the boxes are on. Process load is a share of the whole CPU; per-process video memory is not shown here. A Steam installation counts as finished only by Steam’s mark in the game manifest, not because the download stopped.","de":"Prüfungen laufen nur bei aktivierten Kästchen. Prozesslast ist ein Anteil der gesamten CPU; Videospeicher pro Prozess wird hier nicht angezeigt. Eine Steam-Installation gilt nur durch die Markierung von Steam im Spielmanifest als abgeschlossen, nicht weil der Download stoppt.","fr":"Les vérifications n’ont lieu que si les cases sont cochées. La charge d’un processus est une part du processeur entier ; la mémoire vidéo par processus n’est pas affichée ici. Une installation Steam n’est considérée comme terminée que par la marque de Steam dans le manifeste du jeu, pas parce que le téléchargement s’arrête.","es":"Las comprobaciones solo se hacen con las casillas activadas. La carga de un proceso es una parte de toda la CPU; aquí no se muestra la memoria de vídeo por proceso. Una instalación de Steam se considera terminada solo por la marca de Steam en el manifiesto del juego, no porque se detenga la descarga.","it":"I controlli avvengono solo con le caselle attive. Il carico di un processo è una quota dell’intera CPU; la memoria video per processo qui non è mostrata. Un’installazione di Steam è considerata finita solo dal segno di Steam nel manifest del gioco, non perché il download si ferma.","pt":"As verificações só correm com as caixas ligadas. A carga de um processo é uma parte de todo o processador; a memória de vídeo por processo não é mostrada aqui. Uma instalação do Steam só conta como concluída pela marca do Steam no manifesto do jogo, não porque a transferência parou."},
    "Папка LibreHardwareMonitor": {"en":"LibreHardwareMonitor folder","de":"LibreHardwareMonitor-Ordner","fr":"Dossier LibreHardwareMonitor","es":"Carpeta de LibreHardwareMonitor","it":"Cartella di LibreHardwareMonitor","pt":"Pasta do LibreHardwareMonitor"},
    "Скачать": {"en":"Download","de":"Herunterladen","fr":"Télécharger","es":"Descargar","it":"Scarica","pt":"Transferir"},
    "Запустить": {"en":"Start","de":"Starten","fr":"Démarrer","es":"Iniciar","it":"Avvia","pt":"Iniciar"},
    "LibreHardwareMonitor даёт температуры процессора через WMI. Он скачивается с официального GitHub только по кнопке; при запуске Windows может запросить разрешение — NEO этот запрос не обходит. Датчики, которых нет у устройства, не показываются.": {"en":"LibreHardwareMonitor provides CPU temperatures through WMI. It is downloaded from the official GitHub only when you press the button; when it starts, Windows may ask for permission — NEO does not bypass that prompt. Sensors the device does not have are not shown.","de":"LibreHardwareMonitor liefert CPU-Temperaturen über WMI. Es wird nur per Schaltfläche vom offiziellen GitHub geladen; beim Start fragt Windows eventuell nach einer Erlaubnis – NEO umgeht diese Abfrage nicht. Sensoren, die das Gerät nicht hat, werden nicht angezeigt.","fr":"LibreHardwareMonitor fournit les températures du processeur via WMI. Il n’est téléchargé depuis le GitHub officiel que sur clic ; au démarrage, Windows peut demander une autorisation — NEO ne contourne pas cette demande. Les capteurs absents de l’appareil ne sont pas affichés.","es":"LibreHardwareMonitor ofrece las temperaturas de la CPU mediante WMI. Solo se descarga del GitHub oficial al pulsar el botón; al iniciarlo, Windows puede pedir permiso y NEO no evita esa solicitud. No se muestran sensores que el equipo no tiene.","it":"LibreHardwareMonitor fornisce le temperature della CPU tramite WMI. Si scarica dal GitHub ufficiale solo con il pulsante; all’avvio Windows può chiedere un’autorizzazione e NEO non la aggira. I sensori che il dispositivo non ha non vengono mostrati.","pt":"O LibreHardwareMonitor dá as temperaturas do processador por WMI. Só é transferido do GitHub oficial com o botão; ao iniciar, o Windows pode pedir autorização — o NEO não contorna esse pedido. Sensores que o dispositivo não tem não são mostrados."},
})

EXACT.update({  # NEO 188 HREQ-006: cloud density
    "Кучность облаков": {"en":"Cloud density","de":"Wolkendichte","fr":"Densité des nuages","es":"Densidad de nubes","it":"Densità delle nuvole","pt":"Densidade das nuvens"},
    "Мало": {"en":"Few","de":"Wenig","fr":"Peu","es":"Pocas","it":"Poche","pt":"Poucas"},
    "Средне": {"en":"Medium","de":"Mittel","fr":"Moyen","es":"Medio","it":"Medio","pt":"Médio"},
    "Много": {"en":"Many","de":"Viele","fr":"Beaucoup","es":"Muchas","it":"Molte","pt":"Muitas"},
    "Сколько облаков плывёт по сцене, когда выбран эффект «Облака».": {"en":"How many clouds drift across the stage when the “Clouds” effect is chosen.","de":"Wie viele Wolken über die Bühne ziehen, wenn der Effekt „Wolken“ gewählt ist.","fr":"Nombre de nuages qui traversent la scène avec l’effet « Nuages ».","es":"Cuántas nubes cruzan el escenario con el efecto «Nubes».","it":"Quante nuvole attraversano la scena con l’effetto «Nuvole».","pt":"Quantas nuvens atravessam o palco com o efeito «Nuvens»."},
})

EXACT.update({  # NEO 174: settings sidebar, search and page descriptions
    "Найти настройку…": {"en":"Find a setting…","de":"Einstellung suchen…","fr":"Rechercher un réglage…","es":"Buscar un ajuste…","it":"Cerca un'impostazione…","pt":"Procurar uma definição…"},
    "Ничего не найдено": {"en":"Nothing found","de":"Nichts gefunden","fr":"Aucun résultat","es":"No se encontró nada","it":"Nessun risultato","pt":"Nada encontrado"},
    "Распознавание речи, микрофон и слово-активация": {"en":"Speech recognition, microphone and wake word","de":"Spracherkennung, Mikrofon und Aktivierungswort","fr":"Reconnaissance vocale, micro et mot d’activation","es":"Reconocimiento de voz, micrófono y palabra de activación","it":"Riconoscimento vocale, microfono e parola di attivazione","pt":"Reconhecimento de voz, microfone e palavra de ativação"},
    "Музыка, видео и управление воспроизведением": {"en":"Music, video and playback control","de":"Musik, Video und Wiedergabesteuerung","fr":"Musique, vidéo et contrôle de la lecture","es":"Música, vídeo y control de reproducción","it":"Musica, video e controllo della riproduzione","pt":"Música, vídeo e controlo da reprodução"},
    "Поведение NEO во время игр": {"en":"How NEO behaves while you play","de":"Verhalten von NEO beim Spielen","fr":"Comportement de NEO pendant les jeux","es":"Comportamiento de NEO durante los juegos","it":"Comportamento di NEO durante i giochi","pt":"Comportamento do NEO durante os jogos"},
    "Цвета, шрифты и размер интерфейса": {"en":"Colours, fonts and interface size","de":"Farben, Schriften und Größe der Oberfläche","fr":"Couleurs, polices et taille de l’interface","es":"Colores, fuentes y tamaño de la interfaz","it":"Colori, caratteri e dimensione dell'interfaccia","pt":"Cores, tipos de letra e tamanho da interface"},
    "Внешний вид и анимация робота": {"en":"Robot look and animation","de":"Aussehen und Animation des Roboters","fr":"Apparence et animation du robot","es":"Aspecto y animación del robot","it":"Aspetto e animazione del robot","pt":"Aspeto e animação do robô"},
    "Ключи и подключения интернет-сервисов": {"en":"Keys and connections of online services","de":"Schlüssel und Verbindungen der Online-Dienste","fr":"Clés et connexions des services en ligne","es":"Claves y conexiones de los servicios en línea","it":"Chiavi e connessioni dei servizi online","pt":"Chaves e ligações dos serviços online"},
    "Подтверждения, приватность и журналы": {"en":"Confirmations, privacy and logs","de":"Bestätigungen, Datenschutz und Protokolle","fr":"Confirmations, confidentialité et journaux","es":"Confirmaciones, privacidad y registros","it":"Conferme, privacy e registri","pt":"Confirmações, privacidade e registos"},
    "Запуск, производительность и служебные функции": {"en":"Startup, performance and maintenance","de":"Start, Leistung und Wartung","fr":"Démarrage, performances et maintenance","es":"Inicio, rendimiento y mantenimiento","it":"Avvio, prestazioni e manutenzione","pt":"Arranque, desempenho e manutenção"},
    "Виджеты на рабочем столе": {"en":"Desktop widgets","de":"Desktop-Widgets","fr":"Widgets du bureau","es":"Widgets del escritorio","it":"Widget del desktop","pt":"Widgets do ambiente de trabalho"},
})

GENERIC_HELP = {
    "en": "This option configures the corresponding NEO feature.",
    "de": "Diese Option konfiguriert die entsprechende NEO-Funktion.",
    "fr": "Cette option configure la fonction NEO correspondante.",
    "es": "Esta opción configura la función correspondiente de NEO.",
    "it": "Questa opzione configura la relativa funzione di NEO.",
    "pt": "Esta opção configura o recurso correspondente do NEO.",
}

RUNTIME_PATTERNS: dict[str, list[tuple[re.Pattern[str], str]]] = {}

_UI_CATALOG_DIR = Path(__file__).resolve().parent / "locales" / "ui"

@lru_cache(maxsize=2)
def _ui_catalog(code: str) -> dict[str, str]:
    """Load only the selected UI locale (plus at most one recently-used locale)."""
    key = str(code or "ru").strip().casefold()
    if key not in LANGUAGES:
        return {}
    try:
        data = json.loads((_UI_CATALOG_DIR / f"{key}.json").read_text(encoding="utf-8"))
        rows = data.get("strings", {}) if isinstance(data, dict) else {}
        return {str(k): str(v) for k, v in rows.items() if str(k) and str(v)} if isinstance(rows, dict) else {}
    except Exception:
        return {}


@lru_cache(maxsize=2)
def _catalog_templates(code: str) -> tuple[tuple[str, "re.Pattern[str]", str, tuple[str, ...]], ...]:
    """NEO 184: catalog rows with {placeholders}, matchable against an already formatted text."""
    import string
    rows = []
    for key, value in _ui_catalog(code).items():
        if "{" not in key or key == value:
            continue
        try:
            parts = list(string.Formatter().parse(key))
        except ValueError:
            continue
        pattern, names, prefix = "", [], None
        for literal, name, _spec, _conv in parts:
            pattern += re.escape(literal)
            if prefix is None:
                prefix = literal
            if name is None:
                continue
            if not name.isidentifier():
                pattern = ""
                break
            pattern += f"(?P={name})" if name in names else f"(?P<{name}>.+?)"
            if name not in names:
                names.append(name)
        if pattern and names and prefix:
            rows.append((prefix, re.compile(pattern, re.S), value, tuple(names)))
    return tuple(rows)


def _translate_template(source: str, code: str) -> str:
    for prefix, rx, target, names in _catalog_templates(code):
        if not source.startswith(prefix):
            continue
        match = rx.fullmatch(source)
        if match is None:
            continue
        out = target
        for name in names:
            out = out.replace("{" + name + "}", match.group(name))
        return out
    return ""


@lru_cache(maxsize=14)
def _ordered_glossary(entries: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """Reuse phrase order while retaining changes to the public glossary mappings."""
    return tuple(sorted(entries, key=lambda item: len(item[0]), reverse=True))


def tr(text: str, code: str) -> str:
    source = str(text or "")
    if not source or code not in LANGUAGES:
        return source
    catalog_value = _ui_catalog(code).get(source)
    if catalog_value:
        return catalog_value
    if code != "ru" and "{" not in source:
        templated = _translate_template(source, code)
        if templated:
            return templated
    # Russian also has explicit overrides for inherited English UI captions.
    # Unlisted text (including filenames, provider messages and user input)
    # remains unchanged; no glossary is applied to the source locale.
    if code == "ru":
        return source
    exact = EXACT.get(source)
    if exact and code in exact:
        return exact[code]
    # Dynamic prefixes/suffixes first.
    dynamic = _translate_dynamic(source, code)
    if dynamic != source:
        return dynamic
    # Word/phrase glossary. Longest entries first.
    out = source
    glossary = GLOSSARY.get(code, {})
    for ru, dest in _ordered_glossary(tuple(glossary.items())):
        out = out.replace(ru, dest)
    # Never leave a foreign-language UI full of Russian help prose. Short labels may
    # keep a translated fragment; long help strings get a localized generic fallback.
    if re.search(r"[А-Яа-яЁё]", out):
        if len(source) > 70:
            return GENERIC_HELP[code]
        # Strip isolated untranslated Russian words from short captions only after
        # useful translated tokens have been produced.
        if out != source:
            # UI185-I18N-COMPOSITIONAL-HELP-LOSS-01: a caption may lose ONE unknown
            # qualifier word; a help sentence is never reduced to «text/ .».
            removed = len(re.findall(r"\b[А-Яа-яЁё][А-Яа-яЁё-]*\b", out))
            stripped = re.sub(r"\b[А-Яа-яЁё][А-Яа-яЁё-]*\b", "", out)
            stripped = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s{2,}", " ", stripped)).strip(" -—:;/.,")
            words = re.findall(r"[^\W\d_]{2,}", stripped)
            if stripped and removed <= 1 and len(words) >= 1 and not source.rstrip().endswith((".", "!", "?")):
                return stripped
        # HOTFIX101: never leak Russian application chrome in a foreign UI.
        # The phrase glossary handles known captions; truly unknown captions get
        # a localized neutral label rather than silently switching language.
        return GENERIC_HELP[code]
    return out


def _translate_dynamic(source: str, code: str) -> str:
    # The most common status/action messages have entity payloads that must not be
    # translated. Regex keeps those payloads byte-for-byte intact.
    templates = {
        "en": {
            "now": "Now playing ({src}): {track}.", "err": "Error: {x}", "heard": "Heard command “{x}”.", "mic":"Microphone: {x}",
        },
        "de": {"now":"Jetzt läuft ({src}): {track}.","err":"Fehler: {x}","heard":"Befehl „{x}“ erkannt.","mic":"Mikrofon: {x}"},
        "fr": {"now":"Lecture en cours ({src}) : {track}.","err":"Erreur : {x}","heard":"Commande « {x} » reconnue.","mic":"Microphone : {x}"},
        "es": {"now":"Reproduciendo ahora ({src}): {track}.","err":"Error: {x}","heard":"Comando «{x}» reconocido.","mic":"Micrófono: {x}"},
        "it": {"now":"In riproduzione ({src}): {track}.","err":"Errore: {x}","heard":"Comando “{x}” riconosciuto.","mic":"Microfono: {x}"},
        "pt": {"now":"Tocando agora ({src}): {track}.","err":"Erro: {x}","heard":"Comando “{x}” reconhecido.","mic":"Microfone: {x}"},
    }[code]
    m = re.fullmatch(r"Сейчас играет \((.+?)\): (.+)\.", source)
    if m: return templates["now"].format(src=m.group(1), track=m.group(2))
    m = re.fullmatch(r"Ошибка:\s*(.+)", source, flags=re.S)
    if m: return templates["err"].format(x=m.group(1))
    m = re.fullmatch(r"Микрофон:\s*(.+)", source, flags=re.S)
    if m: return templates["mic"].format(x=m.group(1))
    # HIST-082: an app-owned Ollama error with a literal model name (the name stays as is).
    m = re.fullmatch(r"Модель для разговора «(.+?)» не установлена\. Откройте настройки модели и нажмите «Скачать»\.", source)
    if m:
        row = _ui_catalog(code).get("Модель для разговора «{wanted}» не установлена. Откройте настройки модели и нажмите «Скачать».")
        if row:
            return row.replace("{wanted}", m.group(1))
    return source


def localize_runtime_text(text: str, code: str) -> str:
    """Translate assistant-owned status/tool text while preserving user payloads."""
    source = str(text or "")
    if code == "ru" or not source:
        return source
    exact = EXACT.get(source)
    if exact and code in exact:
        return exact[code]
    dynamic = _translate_dynamic(source, code)
    if dynamic != source:
        return dynamic
    # Entity-preserving result patterns. Never run arbitrary tool output through
    # the long-help fallback in tr(), because that could replace a real path/title
    # with generic help text.
    patterns = {
        "en": [
            (r"^Открыл(?: приложение)?:?\s*(.+)$", "Opened: {0}"),
            (r"^Запустил(?: приложение)?:?\s*(.+)$", "Started: {0}"),
            (r"^Закрыл(?: приложение| окно)?:?\s*(.+)$", "Closed: {0}"),
            (r"^Переключил трек:?\s*(.+)$", "Switched track: {0}"),
            (r"^Поставил музыку на паузу:?\s*(.*)$", "Paused music{0}"),
            (r"^Продолжил музыку:?\s*(.*)$", "Resumed music{0}"),
        ],
        "de": [(r"^Открыл(?: приложение)?:?\s*(.+)$","Geöffnet: {0}"),(r"^Запустил(?: приложение)?:?\s*(.+)$","Gestartet: {0}"),(r"^Закрыл(?: приложение| окно)?:?\s*(.+)$","Geschlossen: {0}"),(r"^Переключил трек:?\s*(.+)$","Titel gewechselt: {0}")],
        "fr": [(r"^Открыл(?: приложение)?:?\s*(.+)$","Ouvert : {0}"),(r"^Запустил(?: приложение)?:?\s*(.+)$","Lancé : {0}"),(r"^Закрыл(?: приложение| окно)?:?\s*(.+)$","Fermé : {0}"),(r"^Переключил трек:?\s*(.+)$","Piste changée : {0}")],
        "es": [(r"^Открыл(?: приложение)?:?\s*(.+)$","Abierto: {0}"),(r"^Запустил(?: приложение)?:?\s*(.+)$","Iniciado: {0}"),(r"^Закрыл(?: приложение| окно)?:?\s*(.+)$","Cerrado: {0}"),(r"^Переключил трек:?\s*(.+)$","Pista cambiada: {0}")],
        "it": [(r"^Открыл(?: приложение)?:?\s*(.+)$","Aperto: {0}"),(r"^Запустил(?: приложение)?:?\s*(.+)$","Avviato: {0}"),(r"^Закрыл(?: приложение| окно)?:?\s*(.+)$","Chiuso: {0}"),(r"^Переключил трек:?\s*(.+)$","Traccia cambiata: {0}")],
        "pt": [(r"^Открыл(?: приложение)?:?\s*(.+)$","Aberto: {0}"),(r"^Запустил(?: приложение)?:?\s*(.+)$","Iniciado: {0}"),(r"^Закрыл(?: приложение| окно)?:?\s*(.+)$","Fechado: {0}"),(r"^Переключил трек:?\s*(.+)$","Faixa alterada: {0}")],
    }
    for pat, repl in patterns.get(code, []):
        m = re.match(pat, source, flags=re.I|re.S)
        if m:
            return repl.format(*m.groups())
    # Keep one deterministic localization policy across desktop and Telegram.
    # The headless helper never leaks an untranslated Russian clause into a
    # foreign locale and preserves user entities/paths/URLs.
    from jarvis.runtime.localized_text import localize_runtime_text as _headless_localize_runtime_text
    return _headless_localize_runtime_text(source, code)



def localized_source_name(source: str, code: str) -> str:
    key = str(source or "").casefold()
    labels = {
        "yandex_music": "Яндекс Музыка",
        "discord": "Discord",
        "youtube": "YouTube",
        "media": "Медиа",
    }
    return tr(labels.get(key, source or "Медиа"), code)

def translate_widget_tree(root: QWidget, code: str, *, exclude: tuple = ()) -> None:
    """Retranslate application-owned UI chrome without changing user data.

    Editable text (wake words, prompts, paths, tokens, dictionaries, chat input)
    is intentionally left untouched. Only labels, placeholders, tabs, menu actions,
    tooltips and non-editable option captions are translated.
    """
    if code not in LANGUAGES:
        code = "ru"
    widgets = [root, *root.findChildren(QWidget)]
    if exclude:
        # NEO 180: windows that translate themselves (an open Settings dialog) are skipped.
        skipped = set()
        for other in exclude:
            if other is not None:
                skipped.add(id(other))
                skipped.update(id(child) for child in other.findChildren(QWidget))
        widgets = [widget for widget in widgets if id(widget) not in skipped]
    for widget in widgets:
        try:
            if isinstance(widget, QGroupBox):
                _translate_property_text(widget, "title", widget.title, widget.setTitle, code)
            if isinstance(widget, QAbstractButton):
                _translate_property_text(widget, "text", widget.text, widget.setText, code)
            elif isinstance(widget, QLabel):
                _translate_property_text(widget, "text", widget.text, widget.setText, code)
            # NEO 183: screen-reader name/description follow the interface language like the labels
            a11y_name = widget.property("i18n_a11y_name_source")
            if a11y_name:
                widget.setAccessibleName(tr(str(a11y_name), code))
            a11y_desc = widget.property("i18n_a11y_desc_source")
            if a11y_desc:
                widget.setAccessibleDescription(tr(str(a11y_desc), code))
            if isinstance(widget, (QLineEdit, QPlainTextEdit)):
                # Only the grey hint; what the user typed is never translated.
                _translate_property_text(widget, "placeholder", widget.placeholderText, widget.setPlaceholderText, code)
            # User-authored contents of line/plain-text edits are never translated.
            if isinstance(widget, QComboBox) and not widget.isEditable() and not bool(widget.property("i18n_skip_combo")):
                for i in range(widget.count()):
                    role = int(Qt.UserRole) + 77
                    # UI185-DEVICE-LABEL-I18N-01: an item marked literal (a device name as
                    # Windows reports it) is user data; a template item keeps its payload.
                    if widget.itemData(i, int(Qt.UserRole) + 78):
                        continue
                    src = widget.itemData(i, role)
                    if src is None:
                        src = widget.itemText(i)
                        widget.setItemData(i, src, role)
                    rendered = tr(str(src), code)
                    payload = widget.itemData(i, int(Qt.UserRole) + 79)
                    if isinstance(payload, dict):
                        for key, value in payload.items():
                            rendered = rendered.replace("{" + str(key) + "}", str(value))
                    widget.setItemText(i, rendered)
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                for kind, getter, setter in (
                    ("prefix", widget.prefix, widget.setPrefix),
                    ("suffix", widget.suffix, widget.setSuffix),
                    ("special_value", widget.specialValueText, widget.setSpecialValueText),
                ):
                    prop = f"i18n_{kind}_source"
                    src = widget.property(prop)
                    if src is None:
                        src = getter(); widget.setProperty(prop, src)
                    if src:
                        setter(tr(str(src), code))
            if isinstance(widget, QTabWidget):
                for i in range(widget.count()):
                    role_key = f"i18n_tab_{i}_source"
                    src = widget.property(role_key)
                    if src is None:
                        src = widget.tabText(i); widget.setProperty(role_key, src)
                    widget.setTabText(i, tr(str(src), code))
            if isinstance(widget, QMenu):
                _translate_property_text(widget, "title", widget.title, widget.setTitle, code)
            tip = widget.toolTip()
            if tip:
                key = "i18n_tooltip_source"
                src = widget.property(key)
                if src is None:
                    src = tip; widget.setProperty(key, src)
                value = tr(str(src), code)
                fields = widget.property("i18n_tooltip_values")
                if isinstance(fields, dict):
                    value = value.format(**fields)
                widget.setToolTip(value)
        except Exception:
            continue
    # QAction objects are not QWidget descendants, so translate them separately.
    try:
        for action in root.findChildren(QAction):
            src = action.property("i18n_text_source")
            if src is None:
                src = action.text(); action.setProperty("i18n_text_source", src)
            if src:
                action.setText(tr(str(src), code))
            tip = action.toolTip()
            if tip:
                src_tip = action.property("i18n_tooltip_source")
                if src_tip is None:
                    src_tip = tip; action.setProperty("i18n_tooltip_source", src_tip)
                action.setToolTip(tr(str(src_tip), code))
    except Exception:
        pass
    try:
        key = "i18n_window_title_source"
        src = root.property(key)
        if src is None:
            src = root.windowTitle(); root.setProperty(key, src)
        if src:
            root.setWindowTitle(tr(str(src), code))
    except Exception:
        pass

def _translate_property_text(widget: QWidget, name: str, getter, setter, code: str) -> None:
    if name == "text" and bool(widget.property("i18n_skip_text")):
        return
    src_key = f"i18n_{name}_source"
    src = widget.property(src_key)
    if src is None:
        src = getter()
        widget.setProperty(src_key, src)
    if src:
        value = tr(str(src), code)
        fields = widget.property(f"i18n_{name}_values")
        if isinstance(fields, dict):
            value = value.format(**fields)
        setter(value)
