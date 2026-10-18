"""Native framing for transport acknowledgements; payloads stay verbatim."""
from __future__ import annotations

LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")
ROWS = {
    "heard": ("Услышал «{phrase}». {task}", "I heard “{phrase}”. {task}", "Ich habe „{phrase}“ gehört. {task}", "J’ai entendu « {phrase} ». {task}", "He oído «{phrase}». {task}", "Ho sentito «{phrase}». {task}", "Ouvi «{phrase}». {task}"),
    "working": ("Выполняю команду.", "I’m carrying out the command.", "Ich führe den Befehl aus.", "J’exécute la commande.", "Estoy ejecutando la orden.", "Sto eseguendo il comando.", "Estou a executar o comando."),
    "working_pause": ("Ставлю музыку на паузу.", "I’m pausing the music.", "Ich pausiere die Musik.", "Je mets la musique en pause.", "Estoy poniendo la música en pausa.", "Sto mettendo in pausa la musica.", "Estou a colocar a música em pausa."),
    "working_resume": ("Продолжаю воспроизведение.", "I’m resuming playback.", "Ich setze die Wiedergabe fort.", "Je reprends la lecture.", "Estoy reanudando la reproducción.", "Sto riprendendo la riproduzione.", "Estou a retomar a reprodução."),
    "working_next": ("Переключаю на следующий трек.", "I’m switching to the next track.", "Ich wechsle zum nächsten Titel.", "Je passe au titre suivant.", "Estoy pasando a la siguiente canción.", "Sto passando al brano successivo.", "Estou a passar à faixa seguinte."),
    "working_previous": ("Возвращаю предыдущий трек.", "I’m switching to the previous track.", "Ich wechsle zum vorherigen Titel.", "Je reviens au titre précédent.", "Estoy volviendo a la canción anterior.", "Sto tornando al brano precedente.", "Estou a voltar à faixa anterior."),
    "starting": ("Выбрал {track}; плеер ещё загружает аудио. Начало воспроизведения пока не подтверждено.", "I selected {track}; the player is still loading the audio. Playback has not been confirmed yet.", "Ich habe {track} ausgewählt; der Player lädt noch. Der Wiedergabestart ist noch nicht bestätigt.", "J’ai sélectionné {track} ; le lecteur charge encore l’audio. Le démarrage n’est pas encore confirmé.", "He seleccionado {track}; el reproductor aún está cargando el audio. La reproducción todavía no está confirmada.", "Ho selezionato {track}; il lettore sta ancora caricando l’audio. L’avvio non è ancora confermato.", "Selecionei {track}; o leitor ainda está a carregar o áudio. O início da reprodução ainda não foi confirmado."),
    "accepted": ("Команда принята: {source}.", "Command accepted: {source}.", "Befehl angenommen: {source}.", "Commande acceptée : {source}.", "Orden aceptada: {source}.", "Comando accettato: {source}.", "Comando aceite: {source}."),
    "next": ("Переключил трек: {track}.", "Switched track: {track}.", "Titel gewechselt: {track}.", "Titre suivant : {track}.", "He cambiado de canción: {track}.", "Brano cambiato: {track}.", "Mudei de faixa: {track}."),
    "previous": ("Вернул предыдущий трек: {track}.", "I switched back to the previous track: {track}.", "Ich habe zum vorherigen Titel gewechselt: {track}.", "Je suis revenu au titre précédent : {track}.", "He vuelto a la canción anterior: {track}.", "Sono tornato al brano precedente: {track}.", "Voltei à faixa anterior: {track}."),
    "pause": ("Готово, поставил музыку на паузу ({source}).", "Done — I paused the music in {source}.", "Erledigt — ich habe die Musik in {source} pausiert.", "C’est fait, j’ai mis la musique en pause dans {source}.", "Listo, he puesto la música en pausa en {source}.", "Fatto, ho messo in pausa la musica in {source}.", "Feito, coloquei a música em pausa em {source}."),
    "resume": ("Готово, продолжил воспроизведение музыки ({source}).", "Done — I resumed the music in {source}.", "Erledigt — ich habe die Musik in {source} fortgesetzt.", "C’est fait, j’ai repris la musique dans {source}.", "Listo, he reanudado la música en {source}.", "Fatto, ho ripreso la musica in {source}.", "Feito, retomei a música em {source}."),
    "like": ("Поставил лайк{track}.", "Liked{track}.", "Mit Gefällt mir markiert{track}.", "Ajouté aux favoris{track}.", "Marcado como favorito{track}.", "Aggiunto ai preferiti{track}.", "Adicionado aos favoritos{track}."),
    "dislike": ("Поставил дизлайк.", "Marked as disliked.", "Mit Gefällt mir nicht markiert.", "Marqué comme non apprécié.", "Marcado como no me gusta.", "Contrassegnato con Non mi piace.", "Marcado como Não gosto."),
    "volume": ("Громкость {source}: {value}%.", "{source} volume: {value}%.", "Lautstärke von {source}: {value}%.", "Volume de {source} : {value} %.", "Volumen de {source}: {value}%.", "Volume di {source}: {value}%.", "Volume de {source}: {value}%."),
    "shuffle_on": ("Перемешивание включено: {source}.", "Shuffle on: {source}.", "Zufallswiedergabe ein: {source}.", "Lecture aléatoire activée : {source}.", "Reproducción aleatoria activada: {source}.", "Riproduzione casuale attivata: {source}.", "Reprodução aleatória ativada: {source}."),
    "shuffle_off": ("Перемешивание выключено: {source}.", "Shuffle off: {source}.", "Zufallswiedergabe aus: {source}.", "Lecture aléatoire désactivée : {source}.", "Reproducción aleatoria desactivada: {source}.", "Riproduzione casuale disattivata: {source}.", "Reprodução aleatória desativada: {source}."),
    "repeat_on": ("Повтор текущего трека включён: {source}.", "Track repeat on: {source}.", "Titelwiederholung ein: {source}.", "Répétition du titre activée : {source}.", "Repetición de la canción activada: {source}.", "Ripetizione del brano attivata: {source}.", "Repetição da faixa ativada: {source}."),
    "repeat_off": ("Повтор выключен: {source}.", "Repeat off: {source}.", "Wiederholung aus: {source}.", "Répétition désactivée : {source}.", "Repetición desactivada: {source}.", "Ripetizione disattivata: {source}.", "Repetição desativada: {source}."),
    # VIDEO185-QUICK-REPLY-DOMAIN-01: an explicit browser-video command keeps
    # its subject; «музыка» is reserved for the music transport.
    "working_pause_video": ("Ставлю видео на паузу.", "I’m pausing the video.", "Ich pausiere das Video.", "Je mets la vidéo en pause.", "Estoy poniendo el vídeo en pausa.", "Sto mettendo in pausa il video.", "Estou a colocar o vídeo em pausa."),
    "pause_video": ("Готово, поставил видео на паузу ({source}).", "Done — I paused the video in {source}.", "Erledigt — ich habe das Video in {source} pausiert.", "C’est fait, j’ai mis la vidéo en pause dans {source}.", "Listo, he puesto el vídeo en pausa en {source}.", "Fatto, ho messo in pausa il video in {source}.", "Feito, coloquei o vídeo em pausa em {source}."),
    "resume_video": ("Готово, продолжил воспроизведение видео ({source}).", "Done — I resumed the video in {source}.", "Erledigt — ich habe das Video in {source} fortgesetzt.", "C’est fait, j’ai repris la vidéo dans {source}.", "Listo, he reanudado el vídeo en {source}.", "Fatto, ho ripreso il video in {source}.", "Feito, retomei o vídeo em {source}."),
    "failed_video": ("Не удалось выполнить команду для видео.", "The video command could not be completed.", "Der Videobefehl konnte nicht ausgeführt werden.", "La commande vidéo n’a pas pu être exécutée.", "No se ha podido ejecutar la orden de vídeo.", "Impossibile eseguire il comando video.", "Não foi possível executar o comando de vídeo."),
    "queue": ("Дальше:", "Up next:", "Als Nächstes:", "À suivre :", "A continuación:", "In coda:", "A seguir:"),
    "unavailable": ("Не удалось определить активный медиаплеер.", "I could not identify an active media player.", "Ich konnte keinen aktiven Mediaplayer ermitteln.", "Je n’ai pas trouvé de lecteur multimédia actif.", "No he encontrado un reproductor activo.", "Non ho trovato un lettore multimediale attivo.", "Não encontrei um leitor multimédia ativo."),
    "failed": ("Не удалось выполнить музыкальную команду.", "The music command could not be completed.", "Der Musikbefehl konnte nicht ausgeführt werden.", "La commande musicale n’a pas pu être exécutée.", "No se ha podido ejecutar la orden musical.", "Impossibile eseguire il comando musicale.", "Não foi possível executar o comando de música."),
    "not_discord": ("Эта быстрая команда не управляет Discord.", "This quick command does not control Discord.", "Dieser Kurzbefehl steuert Discord nicht.", "Cette commande rapide ne contrôle pas Discord.", "Esta orden rápida no controla Discord.", "Questo comando rapido non controlla Discord.", "Este comando rápido não controla o Discord."),
}


def message(key: str, code: str, **values: object) -> str:
    return ROWS[key][LANGUAGES.index(code) if code in LANGUAGES else 1].format(**values)


def content_identity(data: dict, source: str, fallback_label: str) -> tuple[str, str]:
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    explicit = data.get("content_provider") or data.get("active_content_provider") or current.get("content_provider") or current.get("active_content_provider")
    provider = str(explicit or source).casefold()
    label = str(data.get("media_source") or current.get("media_source") or ("YouTube" if provider == "youtube" else fallback_label))
    if not explicit and label.casefold() == "youtube":
        provider = "youtube"
    return provider, label


def domain_key(key: str, domain: str = "") -> str:
    """The video variant of a reply key when the request named video."""
    return key + "_video" if domain == "video" and key + "_video" in ROWS else key


def acknowledgement(action: str, phrase: str, code: str, domain: str = "") -> str:
    key = domain_key("working_" + action, domain)
    task = message(key if key in ROWS else "working", code)
    return message("heard", code, phrase=str(phrase).strip()[:160], task=task)
