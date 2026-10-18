"""Seven-language passive capture-startup notices."""
LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")
MESSAGES = {
    "missing": (
        "{game}: оверлей NVIDIA не запущен. Откройте NVIDIA App и включите оверлей. Автозапуск приложения можно включить в настройках игр.",
        "{game}: the NVIDIA overlay is not running. Open NVIDIA App and enable the overlay. You can enable app autostart in Gaming settings.",
        "{game}: Das NVIDIA-Overlay läuft nicht. Öffne NVIDIA App und aktiviere das Overlay. Den App-Autostart kannst du in den Spieleinstellungen aktivieren.",
        "{game} : l’overlay NVIDIA n’est pas lancé. Ouvrez NVIDIA App et activez l’overlay. Le démarrage automatique se règle dans les paramètres de jeu.",
        "{game}: la superposición NVIDIA no está activa. Abre NVIDIA App y actívala. Puedes activar el inicio automático en los ajustes de juegos.",
        "{game}: l’overlay NVIDIA non è attivo. Apri NVIDIA App e attivalo. Puoi abilitare l’avvio automatico nelle impostazioni di gioco.",
        "{game}: a sobreposição NVIDIA não está ativa. Abre NVIDIA App e ativa-a. Podes ativar o arranque automático nas definições de jogos."),
    "launch_requested": (
        "{game}: запросил запуск {app} свёрнутым, без активации окна. Жду оверлей; включение повтора ещё не подтверждено.",
        "{game}: requested a minimized launch of {app} without activating its window. Waiting for the overlay; Instant Replay is not yet confirmed on.",
        "{game}: Minimierten Start von {app} ohne Fensteraktivierung angefordert. Warte auf das Overlay; Instant Replay ist noch nicht als eingeschaltet bestätigt.",
        "{game} : démarrage réduit de {app} demandé sans activer sa fenêtre. En attente de l’overlay ; l’activation de la relecture n’est pas encore confirmée.",
        "{game}: solicitado el inicio minimizado de {app} sin activar su ventana. Esperando la superposición; aún no se confirma que la repetición esté activa.",
        "{game}: richiesto l’avvio ridotto a icona di {app} senza attivare la finestra. Attendo l’overlay; il replay non è ancora confermato attivo.",
        "{game}: pedido o arranque minimizado de {app} sem ativar a janela. A aguardar a sobreposição; o replay ainda não foi confirmado como ligado."),
    "already_running": (
        "{game}: приложение NVIDIA уже работает, но оверлей ещё не обнаружен. Проверьте настройку оверлея в приложении; жду его готовности.",
        "{game}: the NVIDIA app is already running, but its overlay has not been detected. Check the app’s overlay setting; waiting for it to become ready.",
        "{game}: Die NVIDIA-Anwendung läuft bereits, aber das Overlay wurde noch nicht erkannt. Prüfe die Overlay-Einstellung; warte auf Bereitschaft.",
        "{game} : l’application NVIDIA est déjà lancée, mais son overlay n’est pas détecté. Vérifiez son réglage ; en attente de disponibilité.",
        "{game}: la aplicación NVIDIA ya está abierta, pero no se detecta su superposición. Revisa ese ajuste; esperando a que esté lista.",
        "{game}: l’applicazione NVIDIA è già avviata, ma l’overlay non è rilevato. Controlla l’impostazione; attendo che sia pronto.",
        "{game}: a aplicação NVIDIA já está aberta, mas a sobreposição não foi detetada. Verifica essa definição; a aguardar que fique pronta."),
    "not_installed": (
        "{game}: NVIDIA App не найдена в стандартных папках установки. Откройте установленное приложение вручную; NEO ничего не устанавливает.",
        "{game}: NVIDIA App was not found in standard install folders. Open your installed app manually; NEO does not install anything.",
        "{game}: NVIDIA App wurde in den Standardordnern nicht gefunden. Öffne die installierte Anwendung manuell; NEO installiert nichts.",
        "{game} : NVIDIA App est introuvable dans les dossiers habituels. Ouvrez l’application installée manuellement ; NEO n’installe rien.",
        "{game}: no se encontró NVIDIA App en las carpetas habituales. Abre tu aplicación manualmente; NEO no instala nada.",
        "{game}: NVIDIA App non trovata nelle cartelle standard. Apri manualmente l’app installata; NEO non installa nulla.",
        "{game}: NVIDIA App não foi encontrada nas pastas habituais. Abre a aplicação instalada manualmente; o NEO não instala nada."),
    "launch_failed": (
        "{game}: запустить приложение NVIDIA не удалось. Откройте его вручную и проверьте оверлей (Alt+Z). Мгновенный повтор не включён автоматически.",
        "{game}: the NVIDIA app could not be launched. Open it manually and check the overlay (Alt+Z). Instant Replay was not enabled automatically.",
        "{game}: Die NVIDIA-Anwendung konnte nicht gestartet werden. Öffne sie manuell und prüfe das Overlay (Alt+Z). Instant Replay wurde nicht automatisch eingeschaltet.",
        "{game} : impossible de lancer l’application NVIDIA. Ouvrez-la manuellement et vérifiez l’overlay (Alt+Z). La relecture n’a pas été activée automatiquement.",
        "{game}: no se pudo iniciar la aplicación NVIDIA. Ábrela manualmente y revisa la superposición (Alt+Z). La repetición no se activó automáticamente.",
        "{game}: impossibile avviare l’applicazione NVIDIA. Aprila manualmente e controlla l’overlay (Alt+Z). Il replay non è stato attivato automaticamente.",
        "{game}: não foi possível iniciar a aplicação NVIDIA. Abre-a manualmente e verifica a sobreposição (Alt+Z). O replay não foi ligado automaticamente."),
    "probe_unknown": (
        "{game}: не удалось проверить, работает ли приложение NVIDIA. Автозапуск пропущен; проверьте приложение и оверлей (Alt+Z).",
        "{game}: could not check whether the NVIDIA app is running. Autostart was skipped; check the app and overlay (Alt+Z).",
        "{game}: Es ließ sich nicht prüfen, ob die NVIDIA-Anwendung läuft. Autostart übersprungen; prüfe Anwendung und Overlay (Alt+Z).",
        "{game} : impossible de vérifier si l’application NVIDIA est lancée. Démarrage automatique ignoré ; vérifiez l’application et l’overlay (Alt+Z).",
        "{game}: no se pudo comprobar si la aplicación NVIDIA está abierta. Se omitió el inicio automático; revisa la aplicación y la superposición (Alt+Z).",
        "{game}: impossibile verificare se l’applicazione NVIDIA è avviata. Avvio automatico saltato; controlla l’app e l’overlay (Alt+Z).",
        "{game}: não foi possível verificar se a aplicação NVIDIA está aberta. Arranque automático ignorado; verifica a aplicação e a sobreposição (Alt+Z)."),
    "timeout": (
        "{game}: оверлей NVIDIA не стал доступен за 60 секунд. Проверьте настройку оверлея и Alt+Z. Мгновенный повтор не включён автоматически.",
        "{game}: the NVIDIA overlay did not become available within 60 seconds. Check the overlay setting and Alt+Z. Instant Replay was not enabled automatically.",
        "{game}: Das NVIDIA-Overlay wurde nicht innerhalb von 60 Sekunden verfügbar. Prüfe die Overlay-Einstellung und Alt+Z. Instant Replay wurde nicht automatisch eingeschaltet.",
        "{game} : l’overlay NVIDIA n’est pas devenu disponible sous 60 secondes. Vérifiez son réglage et Alt+Z. La relecture n’a pas été activée automatiquement.",
        "{game}: la superposición NVIDIA no estuvo disponible en 60 segundos. Revisa su ajuste y Alt+Z. La repetición no se activó automáticamente.",
        "{game}: l’overlay NVIDIA non è diventato disponibile entro 60 secondi. Controlla l’impostazione e Alt+Z. Il replay non è stato attivato automaticamente.",
        "{game}: a sobreposição NVIDIA não ficou disponível em 60 segundos. Verifica a definição e Alt+Z. O replay não foi ligado automaticamente."),
}


def message(key: str, code: str, **values) -> str:
    return MESSAGES[key][LANGUAGES.index(code) if code in LANGUAGES else 1].format(**values)
