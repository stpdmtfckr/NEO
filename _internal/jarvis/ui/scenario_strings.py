"""Scenario editor and execution status vocabulary, in all seven UI languages."""
LANGS = ("ru", "en", "de", "fr", "es", "it", "pt")
_ROWS = {
"confirm_title": ("Подтверждение сценария", "Confirm scenario", "Szenario bestätigen", "Confirmer le scénario", "Confirmar escenario", "Conferma scenario", "Confirmar cenário"),
"confirm_steps": ("Проверьте точные шаги и параметры. Запуск выполнит включённые шаги. Отмена останавливает оставшиеся шаги, но не отменяет уже выполненные действия.", "Review the exact steps and parameters. Running executes enabled steps. Cancellation stops remaining steps; it does not undo completed actions.", "Prüfen Sie die genauen Schritte und Parameter. Nur aktivierte Schritte werden ausgeführt. Abbrechen stoppt verbleibende Schritte, macht fertige Aktionen aber nicht rückgängig.", "Vérifiez les étapes et paramètres exacts. Seules les étapes activées seront exécutées. Annuler arrête les étapes restantes sans annuler les actions terminées.", "Revise los pasos y parámetros exactos. Solo se ejecutarán los pasos habilitados. Cancelar detiene los pasos restantes, pero no deshace acciones completadas.", "Verifica i passaggi e i parametri esatti. Verranno eseguiti solo i passaggi attivi. Annullare ferma quelli rimanenti, ma non annulla le azioni già completate.", "Reveja os passos e parâmetros exatos. Só serão executados os passos ativos. Cancelar para os restantes, mas não desfaz ações já concluídas."),
"menu_hint": ("Выберите сценарий. Опасные шаги требуют подтверждения.", "Choose a scenario. Risky steps require confirmation.", "Wählen Sie ein Szenario. Riskante Schritte erfordern eine Bestätigung.", "Choisissez un scénario. Les étapes risquées demandent confirmation.", "Elija un escenario. Los pasos de riesgo requieren confirmación.", "Scegli uno scenario. I passaggi rischiosi richiedono conferma.", "Escolha um cenário. Passos de risco exigem confirmação."),
"identifier": ("Идентификатор", "Identifier", "Kennung", "Identifiant", "Identificador", "Identificatore", "Identificador"),
"required": ("Обязательный шаг", "Required step", "Erforderlicher Schritt", "Étape obligatoire", "Paso obligatorio", "Passaggio obbligatorio", "Etapa obrigatória"),
"busy": ("Сценарий уже выполняется", "Scenario is already running", "Szenario wird bereits ausgeführt", "Le scénario est déjà en cours", "El escenario ya se está ejecutando", "Lo scenario è già in esecuzione", "O cenário já está em execução"),
"inputs": ("Параметры запуска не заданы или недопустимы — ничего не выполнено", "Run inputs are missing or not allowed — nothing was run", "Laufparameter fehlen oder sind unzulässig – nichts ausgeführt", "Paramètres d’exécution manquants ou non autorisés — rien n’a été exécuté", "Faltan parámetros de ejecución o no son válidos: no se ejecutó nada", "Parametri di esecuzione mancanti o non consentiti: nulla è stato eseguito", "Parâmetros de execução em falta ou inválidos — nada foi executado"),
"changed": ("Сценарий изменился. Подтвердите обновлённые шаги", "Scenario changed. Confirm the updated steps", "Szenario geändert. Bestätigen Sie die aktualisierten Schritte", "Scénario modifié. Confirmez les étapes actualisées", "El escenario cambió. Confirme los pasos actualizados", "Scenario modificato. Conferma i passaggi aggiornati", "O cenário mudou. Confirme as etapas atualizadas"),
"size": ("Слишком большой сценарий: не более {value} символов в параметрах", "Scenario too large: at most {value} characters in parameters", "Szenario zu groß: höchstens {value} Zeichen in Parametern", "Scénario trop volumineux : au plus {value} caractères dans les paramètres", "Escenario demasiado grande: máximo {value} caracteres en parámetros", "Scenario troppo grande: massimo {value} caratteri nei parametri", "Cenário muito grande: no máximo {value} caracteres nos parâmetros"),
"run": ("Запустить", "Run", "Starten", "Exécuter", "Ejecutar", "Avvia", "Executar"),
"enable_first": ("Сценарий выключен: включите его и сохраните, чтобы запустить", "The scenario is disabled: enable it and save to run it", "Das Szenario ist deaktiviert: aktivieren und speichern, um es zu starten", "Le scénario est désactivé : activez-le et enregistrez pour l’exécuter", "El escenario está desactivado: actívelo y guarde para ejecutarlo", "Lo scenario è disattivato: attivalo e salva per avviarlo", "O cenário está desativado: ative-o e guarde para o executar"),
"fix_invalid": ("В сценарии есть ошибки: исправьте шаги перед запуском", "The scenario has errors: fix its steps before running", "Das Szenario enthält Fehler: Schritte vor dem Start korrigieren", "Le scénario contient des erreurs : corrigez les étapes avant l’exécution", "El escenario tiene errores: corrija los pasos antes de ejecutarlo", "Lo scenario contiene errori: correggi i passaggi prima di avviarlo", "O cenário tem erros: corrija os passos antes de executar"),
"save_first": ("Сначала сохраните изменения настроек", "Save Settings changes before running", "Einstellungsänderungen vor dem Start speichern", "Enregistrez les paramètres avant d’exécuter", "Guarde los cambios antes de ejecutar", "Salva le impostazioni prima di avviare", "Salve as alterações antes de executar"),
"run_safely": ("Запуск через обычную проверку безопасности", "Run through the normal safety checks", "Mit den üblichen Sicherheitsprüfungen starten", "Exécuter avec les contrôles de sécurité habituels", "Ejecutar con los controles de seguridad habituales", "Avvia con i normali controlli di sicurezza", "Executar com as verificações de segurança normais"),
"title": ("Сценарии", "Scenarios", "Szenarien", "Scénarios", "Escenarios", "Scenari", "Cenários"),
"intro": ("Соберите шаги по порядку. Сохраните настройки, затем запускайте сценарий с полки или кодовой фразой.", "Arrange the steps in order. Save Settings, then run from the shelf or with your trigger phrase.", "Ordnen Sie die Schritte. Speichern Sie die Einstellungen und starten Sie über die Ablage oder den Auslösesatz.", "Ordonnez les étapes. Enregistrez les paramètres, puis lancez depuis l’étagère ou avec la phrase de déclenchement.", "Ordene los pasos. Guarde la configuración y ejecute desde la estantería o con la frase de activación.", "Ordina i passaggi. Salva le impostazioni, poi avvia dallo scaffale o con la frase di attivazione.", "Organize as etapas. Salve as configurações e execute pela estante ou pela frase de ativação."),
"safety": ("Опасные шаги всегда проходят проверку безопасности. Проверка ниже ничего не запускает и не проверяет доступ к устройствам.", "Dangerous steps always use the safety checks. Validation below runs nothing and does not probe devices.", "Gefährliche Schritte durchlaufen immer die Sicherheitsprüfung. Die Prüfung unten führt nichts aus und fragt keine Geräte ab.", "Les étapes dangereuses passent toujours les contrôles de sécurité. La validation ci-dessous ne lance rien et n’interroge aucun appareil.", "Los pasos peligrosos siempre pasan los controles de seguridad. La validación no ejecuta nada ni consulta dispositivos.", "I passaggi pericolosi passano sempre i controlli di sicurezza. La verifica non avvia nulla e non interroga dispositivi.", "Etapas perigosas sempre passam pelas verificações de segurança. A validação não executa nada nem consulta dispositivos."),
"new": ("Новый", "New", "Neu", "Nouveau", "Nuevo", "Nuovo", "Novo"),
"new_name": ("Новый сценарий", "New scenario", "Neues Szenario", "Nouveau scénario", "Nuevo escenario", "Nuovo scenario", "Novo cenário"),
"duplicate": ("Копировать", "Duplicate", "Duplizieren", "Dupliquer", "Duplicar", "Duplica", "Duplicar"),
"copy_name": ("{name} — копия", "{name} — copy", "{name} — Kopie", "{name} — copie", "{name} — copia", "{name} — copia", "{name} — cópia"),
"delete": ("Удалить", "Delete", "Löschen", "Supprimer", "Eliminar", "Elimina", "Excluir"),
"delete_confirm": ("Удалить сценарий «{name}»? Изменение вступит в силу после сохранения настроек.", "Delete scenario “{name}”? This takes effect after saving Settings.", "Szenario „{name}“ löschen? Die Änderung gilt nach dem Speichern der Einstellungen.", "Supprimer le scénario « {name} » ? Cela prendra effet après l’enregistrement des paramètres.", "¿Eliminar el escenario «{name}»? Se aplicará al guardar la configuración.", "Eliminare lo scenario «{name}»? La modifica sarà applicata salvando le impostazioni.", "Excluir o cenário “{name}”? Isso será aplicado ao salvar as configurações."),
"delete_step": ("Удалить выбранный шаг?", "Delete the selected step?", "Ausgewählten Schritt löschen?", "Supprimer l’étape sélectionnée ?", "¿Eliminar el paso seleccionado?", "Eliminare il passaggio selezionato?", "Excluir a etapa selecionada?"),
"yes": ("Да", "Yes", "Ja", "Oui", "Sí", "Sì", "Sim"),
"cancel": ("Отмена", "Cancel", "Abbrechen", "Annuler", "Cancelar", "Annulla", "Cancelar"),
"save": ("Готово", "Done", "Fertig", "Terminé", "Listo", "Fatto", "Concluído"),
"name": ("Название", "Name", "Name", "Nom", "Nombre", "Nome", "Nome"),
"phrase": ("Кодовая фраза", "Trigger phrase", "Auslösesatz", "Phrase de déclenchement", "Frase de activación", "Frase di attivazione", "Frase de ativação"),
"aliases": ("Другие фразы, по одной на строку", "Other phrases, one per line", "Weitere Sätze, einer pro Zeile", "Autres phrases, une par ligne", "Otras frases, una por línea", "Altre frasi, una per riga", "Outras frases, uma por linha"),
"enabled": ("Включён", "Enabled", "Aktiviert", "Activé", "Activado", "Attivo", "Ativado"),
"disabled": ("Отключён", "Disabled", "Deaktiviert", "Désactivé", "Desactivado", "Disattivato", "Desativado"),
"policy": ("При ошибке", "On failure", "Bei Fehler", "En cas d’erreur", "Si hay un error", "In caso di errore", "Em caso de erro"),
"stop": ("Остановить сценарий", "Stop scenario", "Szenario stoppen", "Arrêter le scénario", "Detener escenario", "Ferma scenario", "Parar cenário"),
"continue": ("Продолжить следующие шаги", "Continue next steps", "Nächste Schritte fortsetzen", "Continuer les étapes suivantes", "Continuar los pasos siguientes", "Continua i passaggi successivi", "Continuar as próximas etapas"),
"inherit": ("Как в сценарии", "Use scenario policy", "Szenarioregel verwenden", "Utiliser la règle du scénario", "Usar la regla del escenario", "Usa la regola dello scenario", "Usar a regra do cenário"),
"max_seconds": ("Лимит времени, секунд", "Time limit, seconds", "Zeitlimit, Sekunden", "Limite de temps, secondes", "Límite de tiempo, segundos", "Limite di tempo, secondi", "Limite de tempo, segundos"),
"steps": ("Шаги", "Steps", "Schritte", "Étapes", "Pasos", "Passaggi", "Etapas"),
"step": ("Шаг", "Step", "Schritt", "Étape", "Paso", "Passaggio", "Etapa"),
"add_step": ("Добавить шаг", "Add step", "Schritt hinzufügen", "Ajouter une étape", "Añadir paso", "Aggiungi passaggio", "Adicionar etapa"),
"edit": ("Изменить", "Edit", "Bearbeiten", "Modifier", "Editar", "Modifica", "Editar"),
"up": ("Выше", "Move up", "Nach oben", "Monter", "Subir", "Sposta su", "Subir"),
"down": ("Ниже", "Move down", "Nach unten", "Descendre", "Bajar", "Sposta giù", "Descer"),
"type": ("Тип шага", "Step type", "Schritttyp", "Type d’étape", "Tipo de paso", "Tipo di passaggio", "Tipo de etapa"),
"open": ("Открыть файл или приложение", "Open file or application", "Datei oder Anwendung öffnen", "Ouvrir un fichier ou une application", "Abrir archivo o aplicación", "Apri file o applicazione", "Abrir arquivo ou aplicativo"),
"wait": ("Подождать", "Wait", "Warten", "Attendre", "Esperar", "Attendi", "Esperar"),
"command": ("Команда (расширенный шаг)", "Command (advanced)", "Befehl (erweitert)", "Commande (avancé)", "Comando (avanzado)", "Comando (avanzato)", "Comando (avançado)"),
"summary": ("Параметры", "Details", "Details", "Détails", "Detalles", "Dettagli", "Detalhes"),
"path": ("Путь к файлу или приложению", "File or application path", "Datei- oder Anwendungspfad", "Chemin du fichier ou de l’application", "Ruta del archivo o aplicación", "Percorso del file o applicazione", "Caminho do arquivo ou aplicativo"),
"browse": ("Выбрать файл…", "Choose file…", "Datei wählen…", "Choisir un fichier…", "Elegir archivo…", "Scegli file…", "Escolher arquivo…"),
"monitor": ("Монитор (0 = не перемещать)", "Monitor (0 = do not move)", "Monitor (0 = nicht verschieben)", "Écran (0 = ne pas déplacer)", "Monitor (0 = no mover)", "Monitor (0 = non spostare)", "Monitor (0 = não mover)"),
"window_title": ("Часть заголовка окна (необязательно)", "Window title fragment (optional)", "Teil des Fenstertitels (optional)", "Fragment du titre de fenêtre (facultatif)", "Parte del título de ventana (opcional)", "Parte del titolo finestra (facoltativo)", "Parte do título da janela (opcional)"),
"launch_wait": ("Пауза перед переносом, секунд", "Delay before placement, seconds", "Pause vor Platzierung, Sekunden", "Délai avant placement, secondes", "Espera antes de mover, segundos", "Attesa prima di spostare, secondi", "Espera antes de mover, segundos"),
"seconds": ("Пауза, секунд (0–30)", "Wait, seconds (0–30)", "Wartezeit, Sekunden (0–30)", "Attente, secondes (0–30)", "Espera, segundos (0–30)", "Attesa, secondi (0–30)", "Espera, segundos (0–30)"),
"timeout": ("Тайм-аут команды, секунд", "Command timeout, seconds", "Befehlszeitlimit, Sekunden", "Délai de commande, secondes", "Tiempo máximo del comando, segundos", "Timeout del comando, secondi", "Tempo limite do comando, segundos"),
"argv": ("Аргументы: JSON-массив строк", "Arguments: JSON array of strings", "Argumente: JSON-Liste von Zeichenketten", "Arguments : tableau JSON de chaînes", "Argumentos: matriz JSON de cadenas", "Argomenti: array JSON di stringhe", "Argumentos: matriz JSON de strings"),
"argv_help": ("Первый элемент — исполняемый файл, остальные — отдельные аргументы. Оболочка не запускается автоматически. Команды требуют подтверждения безопасности.", "First item is the executable; remaining items are individual arguments. A shell is never added automatically. Commands require safety confirmation.", "Der erste Eintrag ist das Programm, die übrigen sind einzelne Argumente. Eine Shell wird nie automatisch hinzugefügt. Befehle erfordern Sicherheitsbestätigung.", "Le premier élément est l’exécutable, les autres sont des arguments distincts. Aucun shell n’est ajouté automatiquement. Les commandes exigent une confirmation de sécurité.", "El primer elemento es el ejecutable; los demás son argumentos separados. Nunca se añade un intérprete automáticamente. Los comandos requieren confirmación de seguridad.", "Il primo elemento è l’eseguibile; gli altri sono argomenti separati. Non viene aggiunta automaticamente una shell. I comandi richiedono conferma di sicurezza.", "O primeiro item é o executável; os demais são argumentos separados. Nenhum shell é adicionado automaticamente. Os comandos exigem confirmação de segurança."),
"validate": ("Проверить без запуска", "Validate without running", "Prüfen ohne Ausführung", "Valider sans exécuter", "Validar sin ejecutar", "Verifica senza eseguire", "Validar sem executar"),
"valid": ("Проверка пройдена: шагов {count}. Ничего не запускалось. Доступность путей и устройств проверяется только при запуске.", "Validation passed: {count} steps. Nothing was run. Paths and devices are checked only during execution.", "Prüfung bestanden: {count} Schritte. Nichts wurde ausgeführt. Pfade und Geräte werden erst bei der Ausführung geprüft.", "Validation réussie : {count} étapes. Rien n’a été exécuté. Chemins et appareils seront vérifiés à l’exécution.", "Validación correcta: {count} pasos. No se ejecutó nada. Las rutas y dispositivos se comprobarán al ejecutar.", "Verifica riuscita: {count} passaggi. Nulla è stato eseguito. Percorsi e dispositivi saranno verificati durante l’esecuzione.", "Validação concluída: {count} etapas. Nada foi executado. Caminhos e dispositivos serão verificados na execução."),
"empty_list": ("Создайте сценарий и добавьте первый шаг", "Create a scenario and add your first step", "Erstellen Sie ein Szenario und fügen Sie den ersten Schritt hinzu", "Créez un scénario et ajoutez une première étape", "Cree un escenario y añada el primer paso", "Crea uno scenario e aggiungi il primo passaggio", "Crie um cenário e adicione a primeira etapa"),
"log": ("Ход выполнения", "Execution progress", "Ausführungsverlauf", "Progression de l’exécution", "Progreso de ejecución", "Avanzamento esecuzione", "Progresso da execução"),
"cancel_run": ("Остановить выполнение", "Cancel run", "Ausführung abbrechen", "Annuler l’exécution", "Cancelar ejecución", "Annulla esecuzione", "Cancelar execução"),
"cancel_requested": ("Остановка запрошена…", "Cancellation requested…", "Abbruch angefordert…", "Annulation demandée…", "Cancelación solicitada…", "Annullamento richiesto…", "Cancelamento solicitado…"),
"idle": ("Нет активного запуска", "No active run", "Keine aktive Ausführung", "Aucune exécution active", "Ninguna ejecución activa", "Nessuna esecuzione attiva", "Nenhuma execução ativa"),
"started": ("Запущен", "Started", "Gestartet", "Démarré", "Iniciado", "Avviato", "Iniciado"),
"step_started": ("Выполняется шаг", "Step running", "Schritt läuft", "Étape en cours", "Paso en curso", "Passaggio in corso", "Etapa em execução"),
"step_finished": ("Шаг завершён", "Step finished", "Schritt beendet", "Étape terminée", "Paso terminado", "Passaggio terminato", "Etapa concluída"),
"step_skipped": ("Шаг отключён, пропущен", "Disabled step skipped", "Deaktivierter Schritt übersprungen", "Étape désactivée ignorée", "Paso desactivado omitido", "Passaggio disattivato saltato", "Etapa desativada ignorada"),
"completed": ("Выполнен", "Completed", "Abgeschlossen", "Terminé", "Completado", "Completato", "Concluído"),
"failed": ("Завершён с ошибками", "Finished with errors", "Mit Fehlern beendet", "Terminé avec des erreurs", "Finalizado con errores", "Terminato con errori", "Concluído com erros"),
"cancelled": ("Остановлен", "Cancelled", "Abgebrochen", "Annulé", "Cancelado", "Annullato", "Cancelado"),
"unsupported": ("Неизвестный шаг сохранён без изменений", "Unknown step preserved unchanged", "Unbekannter Schritt unverändert erhalten", "Étape inconnue conservée sans modification", "Paso desconocido conservado sin cambios", "Passaggio sconosciuto conservato invariato", "Etapa desconhecida preservada sem alterações"),
"duplicate_id": ("Идентификаторы сценариев должны быть уникальными", "Scenario IDs must be unique", "Szenario-IDs müssen eindeutig sein", "Les identifiants de scénarios doivent être uniques", "Los identificadores deben ser únicos", "Gli identificatori devono essere univoci", "Os identificadores devem ser únicos"),
"ambiguous": ("Фраза или название совпадает с другим сценарием: {name}", "A phrase or name matches another scenario: {name}", "Satz oder Name stimmt mit einem anderen Szenario überein: {name}", "Une phrase ou un nom correspond à un autre scénario : {name}", "Una frase o nombre coincide con otro escenario: {name}", "Una frase o un nome coincide con un altro scenario: {name}", "Uma frase ou nome coincide com outro cenário: {name}"),
"invalid": ("Параметры сценария требуют исправления", "Scenario parameters need correction", "Szenarioparameter müssen korrigiert werden", "Les paramètres du scénario doivent être corrigés", "Los parámetros del escenario necesitan corrección", "I parametri dello scenario devono essere corretti", "Os parâmetros do cenário precisam de correção"),
"number": ("Введите число в диапазоне {value}", "Enter a number in the range {value}", "Zahl im Bereich {value} eingeben", "Saisissez un nombre entre {value}", "Introduzca un número en el rango {value}", "Inserisci un numero nell’intervallo {value}", "Insira um número no intervalo {value}"),
"text": ("Нужен допустимый текст (до {value} символов)", "Valid text required (up to {value} characters)", "Gültiger Text erforderlich (bis {value} Zeichen)", "Texte valide requis (jusqu’à {value} caractères)", "Se requiere texto válido (hasta {value} caracteres)", "Serve testo valido (fino a {value} caratteri)", "Texto válido obrigatório (até {value} caracteres)"),
"object": ("Некорректная структура данных", "Invalid data structure", "Ungültige Datenstruktur", "Structure de données incorrecte", "Estructura de datos incorrecta", "Struttura dati non valida", "Estrutura de dados inválida"),
"boolean": ("Нужно значение true или false", "Must be true or false", "Muss true oder false sein", "Doit être true ou false", "Debe ser true o false", "Deve essere true o false", "Deve ser true ou false"),
"policy_error": ("Выберите остановку или продолжение", "Choose stop or continue", "Stopp oder Fortsetzen wählen", "Choisissez arrêt ou poursuite", "Elija detener o continuar", "Scegli arresto o continuazione", "Escolha parar ou continuar"),
"aliases_error": ("Не более 64 дополнительных фраз", "At most 64 additional phrases", "Höchstens 64 zusätzliche Sätze", "64 phrases supplémentaires au maximum", "Máximo 64 frases adicionales", "Massimo 64 frasi aggiuntive", "No máximo 64 frases adicionais"),
"steps_error": ("Нужно от 1 до 100 шагов", "Between 1 and 100 steps required", "1 bis 100 Schritte erforderlich", "Entre 1 et 100 étapes requises", "Se requieren entre 1 y 100 pasos", "Servono da 1 a 100 passaggi", "São necessárias de 1 a 100 etapas"),
"argv_error": ("Нужен JSON-массив из 1–128 строк", "A JSON array of 1–128 strings is required", "JSON-Liste mit 1–128 Zeichenketten erforderlich", "Un tableau JSON de 1 à 128 chaînes est requis", "Se requiere una matriz JSON de 1–128 cadenas", "Serve un array JSON di 1–128 stringhe", "É necessária uma matriz JSON com 1–128 strings"),
"kind": ("Неподдерживаемый тип шага: {value}", "Unsupported step type: {value}", "Nicht unterstützter Schritttyp: {value}", "Type d’étape non pris en charge : {value}", "Tipo de paso no admitido: {value}", "Tipo di passaggio non supportato: {value}", "Tipo de etapa não suportado: {value}"),
"empty": ("Включите хотя бы один шаг", "Enable at least one step", "Mindestens einen Schritt aktivieren", "Activez au moins une étape", "Active al menos un paso", "Attiva almeno un passaggio", "Ative pelo menos uma etapa"),
"duration": ("Сумма пауз превышает лимит сценария", "Total wait exceeds the scenario time limit", "Gesamtwartezeit überschreitet das Szenariozeitlimit", "Le total des attentes dépasse la limite du scénario", "La espera total supera el límite del escenario", "L’attesa totale supera il limite dello scenario", "A espera total ultrapassa o limite do cenário"),
}


_ROWS.update({
    "window_ambiguous": ("Найдено несколько окон. Укажите уникальное название окна.", "Multiple matching windows; specify a unique window title", "Mehrere passende Fenster gefunden. Geben Sie einen eindeutigen Fenstertitel an.", "Plusieurs fenêtres correspondent. Indiquez un titre de fenêtre unique.", "Hay varias ventanas coincidentes. Indique un título de ventana único.", "Più finestre corrispondono. Specifica un titolo univoco.", "Há várias janelas correspondentes. Indique um título de janela único."),
    "window_not_found": ("Окно не найдено", "Window not found", "Fenster nicht gefunden", "Fenêtre introuvable", "Ventana no encontrada", "Finestra non trovata", "Janela não encontrada"),
    "placement_unsupported": ("Размещение окон доступно только в Windows", "Window placement is only available on Windows", "Fensterplatzierung ist nur unter Windows verfügbar", "Le placement des fenêtres est disponible uniquement sous Windows", "La colocación de ventanas solo está disponible en Windows", "Il posizionamento delle finestre è disponibile solo su Windows", "O posicionamento de janelas só está disponível no Windows"),
    "placement_failed": ("Не удалось разместить окно", "Window placement failed", "Fenster konnte nicht platziert werden", "Impossible de placer la fenêtre", "No se pudo colocar la ventana", "Impossibile posizionare la finestra", "Não foi possível posicionar a janela"),
    "monitor_unavailable": ("Монитор {number} недоступен", "Monitor {number} is unavailable", "Monitor {number} ist nicht verfügbar", "L’écran {number} est indisponible", "El monitor {number} no está disponible", "Il monitor {number} non è disponibile", "O monitor {number} não está disponível"),
})


from jarvis.ui.scenario_feature_strings184 import ROWS as _FEATURE_ROWS
_ROWS.update(_FEATURE_ROWS)

def execution_error_text(message, language="en"):
    """Localize known runtime placement errors without rewriting user/OS text."""
    import re
    text = str(message or "")
    known = {
        "Multiple matching windows; specify a unique window title": "window_ambiguous",
        "window not found": "window_not_found",
        "Window placement is only available on Windows": "placement_unsupported",
        "Window placement failed": "placement_failed",
    }
    if text in _FEATURE_ROWS:
        return st(text, language)
    if text in known:
        return st(known[text], language)
    monitor = re.fullmatch(r"monitor (\d+) unavailable", text)
    return st("monitor_unavailable", language, number=monitor.group(1)) if monitor else text


def st(key: str, language: str = "en", **values) -> str:
    row = _ROWS.get(key)
    text = row[LANGS.index(language) if language in LANGS else 1] if row else key
    return text.format(**values) if values else text


def issue_text(issue, language="en") -> str:
    key = {"policy": "policy_error", "aliases": "aliases_error", "steps": "steps_error", "argv": "argv_error"}.get(issue.code, issue.code)
    prefix = f"{st('step', language)} {issue.step_index} · " if issue.step_index else ""
    field = {"on_failure": "policy", "scenario": "title", "id": "identifier"}.get(issue.field, issue.field)
    return prefix + st(field, language) + ": " + st(key, language, value=issue.value)


def install_catalogue():
    from jarvis.i18n import EXACT
    for row in _ROWS.values():
        EXACT[row[0]] = dict(zip(LANGS[1:], row[1:]))
