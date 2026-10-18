# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Language policy shared by UI, speech and routing without Qt dependencies."""

from typing import Any

LANGUAGES: dict[str, str] = {
    "ru": "Русский",
    "en": "English",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "pt": "Português",
}

LANGUAGE_ENGLISH_NAMES = {
    "ru": "Russian", "en": "English", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese",
}

WHISPER_LANGUAGE = {k: k for k in LANGUAGES}

_GAME_GREETINGS = {
    "ru": ("Запущена {game}. Хорошей игры!", "Вижу, ты запустил {game}. Приятной игры!", "{game} уже работает. Удачной игровой сессии!"),
    "en": ("{game} is running. Have a great game!", "I see you started {game}. Enjoy playing!", "{game} is ready. Have fun!"),
    "de": ("{game} läuft. Viel Spaß beim Spielen!", "Du hast {game} gestartet. Viel Spaß!", "{game} ist bereit. Ich wünsche dir eine gute Partie!"),
    "fr": ("{game} est lancé. Bon jeu !", "Je vois que tu as lancé {game}. Amuse-toi bien !", "{game} est prêt. Bonne partie !"),
    "es": ("{game} está en marcha. ¡Que disfrutes del juego!", "Veo que has iniciado {game}. ¡Diviértete!", "{game} está listo. ¡Que tengas una buena partida!"),
    "it": ("{game} è avviato. Buon divertimento!", "Vedo che hai avviato {game}. Buona partita!", "{game} è pronto. Divertiti!"),
    "pt": ("{game} está a correr. Bom jogo!", "Vejo que iniciaste {game}. Diverte-te!", "{game} está pronto. Boa partida!"),
}


def game_directory_label(folder: str, code: str) -> str:
    """Label unverified installation-directory evidence in the selected language."""
    template = {
        "ru": "Папка игры: {folder}", "en": "Game folder: {folder}",
        "de": "Spielordner: {folder}", "fr": "Dossier du jeu : {folder}",
        "es": "Carpeta del juego: {folder}", "it": "Cartella del gioco: {folder}",
        "pt": "Pasta do jogo: {folder}",
    }
    return template.get(code, template["en"]).format(folder=folder)


def game_started_message(game: str, code: str, *, variant: int = 0) -> str:
    """Format only trusted UI framing; preserve the game's proper name exactly."""
    choices = _GAME_GREETINGS.get(code, _GAME_GREETINGS["en"])
    return choices[int(variant) % len(choices)].format(game=str(game))


def language_code(config: Any) -> str:
    try:
        code = str(config.get("app.language", "en") or "en").strip().casefold()
    except Exception:
        code = "en"
    return code if code in LANGUAGES else "en"


def accepted_command_languages(code: str) -> tuple[str, ...]:
    """Strict command-library isolation policy.

    Exactly one command library is active: the language selected in Settings.
    English proper names (songs/apps/files/brands) remain valid payload text, but
    English command verbs are not a fallback in another language mode. Wake
    words are independent user data and are never changed by this policy.
    """
    code = code if code in LANGUAGES else "ru"
    return (code,)


def assistant_language_instruction(code: str) -> str:
    code = code if code in LANGUAGES else "ru"
    return {
        "ru": "Отвечай пользователю только на русском языке. Командный синтаксис принимай только на русском; названия песен, файлов, игр, брендов и приложений могут быть на любом языке и должны сохраняться дословно.",
        "en": "Respond to the user only in English. Accept command intent only from English. Preserve proper names exactly.",
        "de": "Antworte dem Benutzer ausschließlich auf Deutsch. Akzeptiere Befehlssyntax nur auf Deutsch; Titel, Dateien, Spiele, Marken und App-Namen dürfen in jeder Sprache vorkommen und bleiben unverändert.",
        "fr": "Réponds à l'utilisateur uniquement en français. Accepte la syntaxe des commandes uniquement en français ; les titres, fichiers, jeux, marques et noms d’applications peuvent être dans n’importe quelle langue et restent inchangés.",
        "es": "Responde al usuario únicamente en español. Acepta la sintaxis de comandos solo en español; los títulos, archivos, juegos, marcas y nombres de aplicaciones pueden estar en cualquier idioma y se conservan literalmente.",
        "it": "Rispondi all'utente esclusivamente in italiano. Accetta la sintassi dei comandi solo in italiano; titoli, file, giochi, marchi e nomi delle applicazioni possono essere in qualsiasi lingua e vanno conservati invariati.",
        "pt": "Responda ao usuário somente em português. Aceite a sintaxe de comandos apenas em português; títulos, arquivos, jogos, marcas e nomes de aplicativos podem estar em qualquer idioma e devem ser preservados literalmente.",
    }[code]


def command_transcription_prompt(code: str) -> str:
    code = code if code in LANGUAGES else "ru"
    return {
        "ru": "Основная речь русская. Транскрибируй дословно и не переводи обычные русские слова на английский. Английские имена и названия допустимы только когда они действительно произнесены; сохраняй такие названия песен, игр, файлов, каналов и приложений как услышано.",
        "en": "English speech. Transcribe verbatim and do not translate. Preserve song, game, file, channel and application names exactly as spoken.",
        "de": "Deutsche Sprache; englische Namen können in Befehlen vorkommen. Wortgetreu transkribieren, nicht übersetzen. Eigennamen unverändert lassen.",
        "fr": "Parole française; des noms anglais peuvent apparaître dans les commandes. Transcrire fidèlement sans traduire et conserver les noms propres.",
        "es": "Habla en español; pueden aparecer nombres en inglés. Transcribe literalmente, no traduzcas y conserva nombres de canciones, juegos, archivos, canales y aplicaciones.",
        "it": "Parlato italiano; nei comandi possono comparire nomi inglesi. Trascrivi fedelmente, non tradurre e conserva i nomi propri.",
        "pt": "Fala em português; nomes em inglês podem aparecer nos comandos. Transcreva literalmente, não traduza e preserve nomes de músicas, jogos, arquivos, canais e aplicativos.",
    }[code]


def language_rejection_message(code: str) -> str:
    code = code if code in LANGUAGES else "ru"
    return {
        "ru": "Эта команда похожа на команду другого языка. При выбранном русском NEO принимает командный синтаксис только на русском; иностранные названия внутри русской команды допустимы.",
        "en": "That command appears to be in another language. In English mode NEO accepts English commands only.",
        "de": "Dieser Befehl scheint in einer anderen Sprache formuliert zu sein. Im deutschen Modus akzeptiert NEO Befehlssyntax nur auf Deutsch; fremdsprachige Eigennamen innerhalb eines deutschen Befehls sind erlaubt.",
        "fr": "Cette commande semble être formulée dans une autre langue. En mode français, NEO accepte la syntaxe de commande uniquement en français ; les noms propres étrangers dans une commande française sont autorisés.",
        "es": "Este comando parece estar en otro idioma. En modo español, NEO acepta la sintaxis de comandos solo en español; los nombres propios en otros idiomas sí están permitidos.",
        "it": "Questo comando sembra formulato in un’altra lingua. In modalità italiana NEO accetta la sintassi dei comandi solo in italiano; i nomi propri stranieri all’interno di un comando italiano sono consentiti.",
        "pt": "Este comando parece estar formulado em outro idioma. No modo português, o NEO aceita a sintaxe de comandos apenas em português; nomes próprios estrangeiros dentro de um comando em português são permitidos.",
    }[code]
