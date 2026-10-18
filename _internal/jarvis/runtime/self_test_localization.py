"""Native diagnostic framing with untouched technical evidence."""
import re
from .diagnostics import redact

LANGUAGES=('ru','en','de','fr','es','it','pt')
ROWS={
 'PASS':('Пройдено','Passed','Bestanden','Réussi','Correcto','Superato','Concluído'),
 'FAIL':('Ошибка','Failed','Fehlgeschlagen','Échec','Error','Errore','Falha'),
 'SKIP':('Не проверялось','Not tested','Nicht geprüft','Non testé','Sin comprobar','Non verificato','Não verificado'),
 'DISABLED':('Отключено','Disabled','Deaktiviert','Désactivé','Desactivado','Disattivato','Desativado'),
 'UNAVAILABLE':('Недоступно','Unavailable','Nicht verfügbar','Indisponible','No disponible','Non disponibile','Indisponível'),
 'complete':('Локальная проверка завершена.','Local checks completed.','Lokale Prüfung abgeschlossen.','Vérification locale terminée.','Comprobación local terminada.','Verifica locale completata.','Verificação local concluída.'),
 'issues':('Локальная проверка обнаружила проблемы.','Local checks found issues.','Die lokale Prüfung hat Probleme gefunden.','La vérification locale a détecté des problèmes.','La comprobación local detectó problemas.','La verifica locale ha rilevato problemi.','A verificação local encontrou problemas.'),
 'optional':('Основные проверки завершены. Некоторые дополнительные возможности недоступны.','Core checks completed. Some optional features are unavailable.','Kernprüfung abgeschlossen. Einige optionale Funktionen sind nicht verfügbar.','Vérifications principales terminées. Certaines fonctions facultatives sont indisponibles.','Comprobaciones principales terminadas. Algunas funciones opcionales no están disponibles.','Verifiche principali completate. Alcune funzioni facoltative non sono disponibili.','Verificações principais concluídas. Algumas funções opcionais estão indisponíveis.'),
 'ollama':('Локальная служба Ollama','Local Ollama service','Lokaler Ollama-Dienst','Service Ollama local','Servicio Ollama local','Servizio Ollama locale','Serviço Ollama local'),
 'ollama_chat':('Локальный разговор','Local conversation','Lokales Gespräch','Conversation locale','Conversación local','Conversazione locale','Conversa local'),
 'ollama_chat_model':('Модель разговора','Conversation model','Gesprächsmodell','Modèle de conversation','Modelo de conversación','Modello di conversazione','Modelo de conversa'),
 'ollama_model':('Модель изображения','Image model','Bildmodell','Modèle d’image','Modelo de imagen','Modello visivo','Modelo de imagem'),
 'microphone':('Микрофон','Microphone','Mikrofon','Microphone','Micrófono','Microfono','Microfone'),
 'settings_write':('Запись настроек','Settings storage','Speicherung der Einstellungen','Enregistrement des paramètres','Almacenamiento de ajustes','Salvataggio delle impostazioni','Gravação das definições'),
 'browser':('Браузер','Browser','Browser','Navigateur','Navegador','Browser','Navegador'),
 'windows_api':('Функции Windows','Windows functions','Windows-Funktionen','Fonctions Windows','Funciones de Windows','Funzioni Windows','Funções do Windows'),
 'windows_only':('Для этой проверки нужен Windows.','This check requires Windows.','Diese Prüfung benötigt Windows.','Cette vérification nécessite Windows.','Esta comprobación requiere Windows.','Questa verifica richiede Windows.','Esta verificação requer Windows.'),
 'api_separate':('Авторизация проверяется отдельно: NEO.cmd --integration-test.','Check authentication separately: NEO.cmd --integration-test.','Anmeldung separat prüfen: NEO.cmd --integration-test.','Vérifier l’authentification séparément : NEO.cmd --integration-test.','Comprueba la autorización por separado: NEO.cmd --integration-test.','Verifica l’autorizzazione separatamente: NEO.cmd --integration-test.','Verifica a autorização separadamente: NEO.cmd --integration-test.'),
 'gpu_skipped':('Нагрузка на GPU не запускалась.','No GPU workload was started.','Keine GPU-Last gestartet.','Aucune charge GPU lancée.','No se inició ninguna carga GPU.','Nessun carico GPU avviato.','Nenhuma carga GPU foi iniciada.'),
 'model_missing':('Нужная модель не установлена','Required model is not installed','Erforderliches Modell ist nicht installiert','Le modèle requis n’est pas installé','El modelo necesario no está instalado','Il modello richiesto non è installato','O modelo necessário não está instalado'),
 'api_ready':('Сервер доступен; модель не загружалась.','Server available; no model loaded.','Server erreichbar; kein Modell geladen.','Serveur disponible ; aucun modèle chargé.','Servidor disponible; ningún modelo cargado.','Server disponibile; nessun modello caricato.','Servidor disponível; nenhum modelo carregado.'),
 'api_missing':('Сервер анализа изображения недоступен. Проверьте настройки Ollama.','Image analysis server unavailable. Check Ollama settings.','Bildanalyseserver nicht erreichbar. Prüfe die Ollama-Einstellungen.','Serveur d’analyse d’image indisponible. Vérifie les paramètres Ollama.','Servidor de análisis de imagen no disponible. Revisa los ajustes de Ollama.','Server di analisi delle immagini non disponibile. Verifica le impostazioni Ollama.','Servidor de análise de imagem indisponível. Verifica as definições do Ollama.'),
 'write_ok':('Временный файл записан, прочитан и удалён; настройки сохранены.','Temporary file written, read and removed; existing settings preserved.','Temporäre Datei geschrieben, gelesen und entfernt; Einstellungen unverändert.','Fichier temporaire écrit, lu et supprimé ; paramètres conservés.','Archivo temporal escrito, leído y eliminado; ajustes conservados.','File temporaneo scritto, letto e rimosso; impostazioni conservate.','Ficheiro temporário gravado, lido e removido; definições preservadas.'),
}
ROWS.update({
 'python':('Python','Python','Python','Python','Python','Python','Python'),
 'psutil':('Системные ресурсы','System resources','Systemressourcen','Ressources système','Recursos del sistema','Risorse di sistema','Recursos do sistema'),
 'discord_install':('Discord','Discord','Discord','Discord','Discord','Discord','Discord'),
 'nvidia_smi':('Диагностика GPU','GPU diagnostics','GPU-Diagnose','Diagnostic GPU','Diagnóstico de GPU','Diagnostica GPU','Diagnóstico da GPU'),
 'not_configured':('не настроено','not configured','nicht eingerichtet','non configuré','sin configurar','non configurato','não configurado'),
 'system_details':('Логических процессоров: {cpu}; память: {ram} ГБ.','Logical processors: {cpu}; memory: {ram} GB.','Logische Prozessoren: {cpu}; Arbeitsspeicher: {ram} GB.','Processeurs logiques : {cpu} ; mémoire : {ram} Go.','Procesadores lógicos: {cpu}; memoria: {ram} GB.','Processori logici: {cpu}; memoria: {ram} GB.','Processadores lógicos: {cpu}; memória: {ram} GB.'),
 'microphone_presence':('Аудиовходов: {count}. Проверено наличие через WinMM; распознавание речи не проверялось.','Audio inputs: {count}. WinMM presence checked; speech recognition not tested.','Audioeingänge: {count}. Verfügbarkeit über WinMM geprüft; Spracherkennung nicht getestet.','Entrées audio : {count}. Présence vérifiée via WinMM ; reconnaissance vocale non testée.','Entradas de audio: {count}. Presencia comprobada mediante WinMM; reconocimiento de voz sin probar.','Ingressi audio: {count}. Presenza verificata tramite WinMM; riconoscimento vocale non testato.','Entradas de áudio: {count}. Presença verificada através do WinMM; reconhecimento de voz não testado.'),
 'microphone_missing':('Микрофон не обнаружен. Проверьте устройство и разрешение доступа к микрофону в Windows.','No microphone detected. Check the device and microphone permission in Windows.','Kein Mikrofon erkannt. Prüfe das Gerät und die Mikrofonberechtigung in Windows.','Aucun microphone détecté. Vérifie le périphérique et l’autorisation du microphone dans Windows.','No se detectó ningún micrófono. Revisa el dispositivo y el permiso del micrófono en Windows.','Nessun microfono rilevato. Controlla il dispositivo e l’autorizzazione del microfono in Windows.','Nenhum microfone detetado. Verifica o dispositivo e a permissão do microfone no Windows.'),
 'foreground_details':('Активное окно: HWND={hwnd}.','Active window: HWND={hwnd}.','Aktives Fenster: HWND={hwnd}.','Fenêtre active : HWND={hwnd}.','Ventana activa: HWND={hwnd}.','Finestra attiva: HWND={hwnd}.','Janela ativa: HWND={hwnd}.'),
})

ROWS.update({
 'model_unknown':('Состояние модели не проверено: сначала запустите Ollama.','Model state was not checked; start Ollama first.','Modellstatus nicht geprüft; starte zuerst Ollama.','État du modèle non vérifié ; lancez Ollama d’abord.','Estado del modelo sin comprobar; inicia Ollama primero.','Stato del modello non verificato; avvia prima Ollama.','Estado do modelo não verificado; inicia primeiro o Ollama.'),
 'chat_install':('В настройках разговорной модели нажмите «Скачать».','Click Download in conversation model settings.','Klicke in den Gesprächsmodell-Einstellungen auf Herunterladen.','Cliquez sur Télécharger dans les réglages du modèle de conversation.','Pulsa Descargar en los ajustes del modelo de conversación.','Premi Scarica nelle impostazioni del modello di conversazione.','Clica em Transferir nas definições do modelo de conversa.'),
 'vision_install':('Установите именно эту мультимодальную модель в Ollama или отключите анализ изображений. Разговорная модель 0.5B её не заменяет.','Install this exact multimodal model in Ollama or disable image analysis. The 0.5B conversation model cannot replace it.','Installiere genau dieses multimodale Modell in Ollama oder deaktiviere die Bildanalyse. Das 0.5B-Gesprächsmodell ersetzt es nicht.','Installez ce modèle multimodal précis dans Ollama ou désactivez l’analyse d’image. Le modèle de conversation 0.5B ne le remplace pas.','Instala este modelo multimodal exacto en Ollama o desactiva el análisis de imágenes. El modelo de conversación 0.5B no lo sustituye.','Installa questo preciso modello multimodale in Ollama o disattiva l’analisi delle immagini. Il modello di conversazione 0.5B non lo sostituisce.','Instala este modelo multimodal exato no Ollama ou desativa a análise de imagens. O modelo de conversa 0.5B não o substitui.'),
 'api_missing':('Локальная служба Ollama недоступна. Запустите Ollama и повторите проверку; установка: https://ollama.com/download.','Local Ollama service is unavailable. Start Ollama and check again; installation: https://ollama.com/download.','Der lokale Ollama-Dienst ist nicht verfügbar. Starte Ollama und prüfe erneut; Installation: https://ollama.com/download.','Le service Ollama local est indisponible. Lancez Ollama et revérifiez ; installation : https://ollama.com/download.','El servicio local Ollama no está disponible. Inicia Ollama y comprueba de nuevo; instalación: https://ollama.com/download.','Il servizio Ollama locale non è disponibile. Avvia Ollama e ricontrolla; installazione: https://ollama.com/download.','O serviço Ollama local está indisponível. Inicia o Ollama e verifica novamente; instalação: https://ollama.com/download.'),
})

def word(key,language):
    row=ROWS.get(key)
    return row[LANGUAGES.index(language) if language in LANGUAGES else 1] if row else key

def render_check(row,language='en'):
    name=str(row.get('name',''))
    status=str(row.get('status') or ('PASS' if row.get('ok') else 'FAIL'))
    raw=str(row.get('detail',row.get('message','')))
    detail=raw
    if status=='DISABLED':detail=word('DISABLED',language)
    elif 'Requires a Windows host' in raw:detail=word('windows_only',language)
    elif name in {'ollama_model','ollama_chat_model'} and status=='UNAVAILABLE':
        model=raw.split(': ',1)[1].strip() if ': ' in raw else raw
        detail=word('model_missing',language)+': '+(word('not_configured',language) if model=='(not configured)' else model)
        detail+='. '+word('vision_install' if name=='ollama_model' else 'chat_install',language)
    elif name in {'ollama_model','ollama_chat_model'} and status=='SKIP':
        detail=word('model_unknown',language)
    elif name in {'ollama','ollama_chat'} and status=='UNAVAILABLE':
        detail=word('api_missing',language)
    elif name in {'ollama','ollama_chat'} and status=='PASS':detail=word('api_ready',language)
    elif name=='settings_write' and status=='PASS':detail=word('write_ok',language)
    elif name=='discord_install' and status=='SKIP':
        name='Discord';detail=word('api_separate',language)
    elif name=='nvidia_smi' and status=='SKIP':
        detail=word('gpu_skipped',language)
    elif name=='psutil' and status=='PASS' and (match:=re.fullmatch(r'cpu=(\d+) ram=([\d.]+)GB',raw)):
        detail=word('system_details',language).format(cpu=match[1],ram=match[2])
    elif name=='microphone' and status=='PASS' and (match:=re.fullmatch(r'inputs=(\d+); WinMM presence only, audio recognition not tested',raw)):
        detail=word('microphone_presence',language).format(count=match[1])
    elif name=='microphone' and raw=='RuntimeError: No microphone detected; check Windows privacy/device settings':
        detail=word('microphone_missing',language)
    elif name=='windows_api' and status=='PASS' and (match:=re.fullmatch(r'foreground HWND=(\d+)',raw)):
        detail=word('foreground_details',language).format(hwnd=match[1])
    return f"{word(name,language)} — {word(status,language)}: {redact(detail)}"

def render_report(report,language='en',*,issues_only=False):
    checks=[r for r in report.get('checks',[]) if isinstance(r,dict)]
    issues=[r for r in checks if not r.get('ok',True) or r.get('status') in {'FAIL','UNAVAILABLE'}]
    key='issues' if not report.get('ok') else 'optional' if issues else 'complete'
    rows=issues if issues_only else checks
    return word(key,language)+'\n'+'\n'.join('• '+render_check(r,language) for r in rows[:30])
