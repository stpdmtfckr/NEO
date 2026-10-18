"""Conversation/history replies grounded only in saved, scoped observations."""
from __future__ import annotations
from datetime import datetime
import re
from jarvis.runtime.natural_commands import LANGUAGES, norm
from jarvis.runtime.conversation_journal import local_period

_PHRASES = {
    "yesterday": ("что мы делали вчера|чем мы занимались вчера|что я делал вчера", "what did we do yesterday|what was i doing yesterday", "was haben wir gestern gemacht|was habe ich gestern gemacht", "qu'avons-nous fait hier|qu'ai-je fait hier", "qué hicimos ayer|qué hice ayer", "cosa abbiamo fatto ieri|cosa ho fatto ieri", "o que fizemos ontem|o que fiz ontem"),
    "opened": ("что я открывал|что мы открывали|какие файлы я открывал", "what did i open|what have i opened|which files did i open", "was habe ich geöffnet|welche dateien habe ich geöffnet", "qu'ai-je ouvert|quels fichiers ai-je ouverts", "qué he abierto|qué archivos he abierto", "cosa ho aperto|quali file ho aperto", "o que abri|que ficheiros abri"),
    "recent": ("о чём мы говорили|что мы обсуждали|напомни наш разговор", "what were we talking about|what did we discuss|remind me of our conversation", "worüber haben wir gesprochen|was haben wir besprochen", "de quoi parlions-nous|qu'avons-nous discuté", "de qué estábamos hablando|qué hemos hablado", "di cosa stavamo parlando|cosa abbiamo discusso", "de que estávamos a falar|o que discutimos"),
    "plan": ("чем займёмся сегодня|что будем делать сегодня|какие планы на сегодня", "what shall we do today|what should we do today", "was machen wir heute|was sollen wir heute machen", "que fait-on aujourd'hui|qu'allons-nous faire aujourd'hui", "qué hacemos hoy|qué vamos a hacer hoy", "cosa facciamo oggi|cosa faremo oggi", "o que fazemos hoje|o que vamos fazer hoje"),
    "clear": ("забудь историю нашего разговора|очисти память диалога", "forget our conversation history|clear conversation memory", "vergiss unseren gesprächsverlauf|lösche den gesprächsspeicher", "oublie notre historique de conversation|efface la mémoire de conversation", "olvida nuestro historial de conversación|borra la memoria de conversación", "dimentica la cronologia della conversazione|cancella la memoria della conversazione", "esquece o histórico da nossa conversa|apaga a memória da conversa"),
}
_LABELS = {
    "empty": ("В сохранённой истории нет подходящих записей. Это не означает, что ничего не происходило.", "There are no matching saved records. That does not mean nothing happened.", "Es gibt keine passenden gespeicherten Einträge. Das bedeutet nicht, dass nichts passiert ist.", "Aucune entrée enregistrée ne correspond. Cela ne veut pas dire qu'il ne s'est rien passé.", "No hay registros guardados que coincidan. Eso no significa que no haya pasado nada.", "Non ci sono registrazioni corrispondenti. Non significa che non sia successo nulla.", "Não há registos guardados correspondentes. Isso não significa que nada aconteceu."),
    "disabled": ("Память диалога сейчас отключена или запись активности приостановлена.", "Conversation memory is disabled or activity recording is paused.", "Der Gesprächsspeicher ist deaktiviert oder die Aktivitätsaufzeichnung pausiert.", "La mémoire de conversation est désactivée ou l'enregistrement d'activité est en pause.", "La memoria de conversación está desactivada o el registro de actividad está pausado.", "La memoria della conversazione è disattivata o la registrazione delle attività è in pausa.", "A memória da conversa está desativada ou o registo de atividade está em pausa."),
    "history": ("По сохранённым записям этого диалога:", "From this conversation's saved records:", "Aus den gespeicherten Einträgen dieses Gesprächs:", "D'après les entrées enregistrées de cette conversation :", "Según los registros guardados de esta conversación:", "Dalle registrazioni salvate di questa conversazione:", "Segundo os registos guardados desta conversa:"),
    "observed": ("Наблюдалось окно", "Observed window", "Beobachtetes Fenster", "Fenêtre observée", "Ventana observada", "Finestra osservata", "Janela observada"),
    "requested": ("Запрос в диалоге", "Conversation request", "Anfrage im Gespräch", "Demande dans la conversation", "Solicitud en la conversación", "Richiesta nella conversazione", "Pedido na conversa"),
    "opened": ("Открыто", "Opened", "Geöffnet", "Ouvert", "Abierto", "Aperto", "Aberto"),
    "recorded": ("Действие", "Action", "Aktion", "Action", "Acción", "Azione", "Ação"),
    "cleared": ("История этого диалога очищена. Другие диалоги не затронуты.", "This conversation's memory is cleared. Other conversations are unchanged.", "Der Speicher dieses Gesprächs wurde gelöscht. Andere Gespräche bleiben unverändert.", "La mémoire de cette conversation est effacée. Les autres conversations sont conservées.", "Se ha borrado la memoria de esta conversación. Las demás conversaciones se conservan.", "La memoria di questa conversazione è stata cancellata. Le altre conversazioni restano invariate.", "A memória desta conversa foi apagada. As outras conversas foram preservadas."),
    "plan": ("Можем вернуться к последнему открытому объекту: {name}. Или выберем новую задачу — что сейчас важнее?", "We could return to the last item you opened: {name}. Or choose a new task—what matters now?", "Wir könnten zum zuletzt geöffneten Objekt zurückkehren: {name}. Oder eine neue Aufgabe wählen. Was ist jetzt wichtig?", "On pourrait revenir au dernier élément ouvert : {name}. Ou choisir une autre tâche. Quelle est la priorité ?", "Podemos volver al último elemento abierto: {name}. O elegir otra tarea. ¿Qué es más importante ahora?", "Potremmo tornare all'ultimo elemento aperto: {name}. Oppure scegliere un altro compito. Cosa conta adesso?", "Podemos voltar ao último item aberto: {name}. Ou escolher outra tarefa. O que é mais importante agora?"),
    "new_plan": ("Давай выберем одну задачу: найти нужный файл, разобраться с папками или включить музыку. С чего начнём?", "Let's choose one task: find a file, sort out folders, or put on music. Where shall we start?", "Wählen wir eine Aufgabe: eine Datei finden, Ordner ordnen oder Musik hören. Womit beginnen wir?", "Choisissons une tâche : trouver un fichier, organiser des dossiers ou écouter de la musique. Par quoi commencer ?", "Elijamos una tarea: encontrar un archivo, organizar carpetas o poner música. ¿Por dónde empezamos?", "Scegliamo un compito: trovare un file, organizzare cartelle o ascoltare musica. Da dove iniziamo?", "Vamos escolher uma tarefa: encontrar um ficheiro, organizar pastas ou ouvir música. Por onde começamos?"),
}


def grounded_reply(agent, text, code):
    index = LANGUAGES.index(code)
    value = norm(text).lstrip("¿¡")
    intent = next((name for name, rows in _PHRASES.items() if value in {norm(phrase) for phrase in rows[index].split("|")}), "")
    if not intent:
        return None
    label = lambda key: _LABELS[key][index]
    journal = getattr(agent, "conversation_journal", None)
    scope = getattr(agent, "_conversation_key", "desktop")
    if intent == "clear":
        if journal is not None:
            journal.clear(scope)
        agent.history = []
        state = agent.conversations.get(scope)
        state.last_reply, state.last_tool, state.last_arguments = "", "", {}
        state.pending_intent, state.last_attempt = None, {}
        state.pending_shell, state.pending_alias = "", None
        for name in ("_last_file_path", "_last_folder_path", "_last_game_name"):
            setattr(agent, name, "")
        return label("cleared"), "memory_cleared"
    if journal is None or not journal.enabled():
        return (label("new_plan") if intent == "plan" else label("disabled")), "memory_unavailable"
    if intent == "recent":
        rows = [row for row in journal.recent(scope, limit=16) if row["role"] == "user"][-5:]
        return (label("history") + "\n" + "\n".join("• " + row["text"] for row in rows) if rows else label("empty")), "memory_grounded"
    since, until = local_period(agent.config, "yesterday" if intent == "yesterday" else "today")
    rows = journal.objects(scope, since=since if intent != "plan" else 0, until=until, limit=20)
    if intent in {"opened", "plan"}:
        rows = [row for row in rows if row["action"] in {"open", "launch", "run", "start"}]
    if intent == "plan":
        if rows:
            data = rows[0]["data"]
            name = next((str(data[key]) for key in ("name", "game_name", "path", "source", "query") if data.get(key)), "")
            if name:
                return label("plan").format(name=name), "memory_grounded"
        return label("new_plan"), "conversation"
    lines = []
    if intent == "yesterday":
        requests = [row for row in journal.recent(scope, limit=200) if row["role"] == "user" and since <= row["created"] < until][-5:]
        for row in requests:
            lines.append(datetime.fromtimestamp(row["created"]).strftime("%H:%M") + " · " + label("requested") + ": " + row["text"])
    for row in reversed(rows):
        data = row["data"]
        name = next((str(data[key]) for key in ("name", "game_name", "path", "source", "query", "process_name") if data.get(key)), "")
        if name:
            action = label("opened") if row["action"] in {"open", "launch", "run", "start"} else label("recorded")
            lines.append(datetime.fromtimestamp(row["created"]).strftime("%H:%M") + " · " + action + ": " + name)
    if scope == "desktop" and intent == "yesterday":
        from jarvis.runtime.event_store import read_observed_history
        observed = read_observed_history(agent.config, scope=scope, period="yesterday", limit=30)
        for row in observed.get("rows", []):
            data = row.get("data", {})
            if row.get("kind") == "activity.window":
                name = str(data.get("process_name", "")) + (" — " + str(data["title"]) if data.get("title") else "")
                lines.append(datetime.fromtimestamp(row["created"]).strftime("%H:%M") + " · " + label("observed") + ": " + name)
    lines = list(dict.fromkeys(lines))[:20]
    return (label("history") + "\n" + "\n".join("• " + line for line in lines) if lines else label("empty")), "memory_grounded"
