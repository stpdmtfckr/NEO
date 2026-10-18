# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Turn-scoped ownership firewall for NEO JARVIS routing.

A user turn may be *about* many nouns but it has one execution owner.  This
module classifies the command shell before Atlas/GameWeb/LLM routing and keeps
that ownership in a ContextVar until the turn finishes.  ToolRegistry performs
the final check, so legacy direct actions, Atlas routes and LLM tool calls all
share the same hard boundary.

The classifier is intentionally lexical and conservative.  Payload words never
win over an explicit shell, e.g. ``найди в браузере как создать заметку`` is a
browser request and ``создай файл cpu.txt`` is a filesystem request.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
from typing import Any, Iterable

from jarvis.runtime.integration_phrases import _weather_signal


def _norm(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[\t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,!?:;—-\"'«»")


@dataclass(frozen=True, slots=True)
class OwnershipClaim:
    owner: str = "conversation"
    confidence: float = 0.0
    reason: str = ""
    allowed_tools: tuple[str, ...] = ()
    schema_tools: tuple[str, ...] = ()
    enforced: bool = False
    text: str = ""

    def allows(self, tool: str) -> bool:
        if not self.enforced:
            return True
        return _norm(tool) in {_norm(x) for x in self.allowed_tools}


_CURRENT: ContextVar[OwnershipClaim | None] = ContextVar("neo_turn_ownership", default=None)


# Tools are deliberately grouped by *execution ownership*, not by broad runtime
# domains.  This is finer than runtime.domains and therefore can distinguish a
# file operation from a system-status query even though both are locally hosted.
_TOOLSETS: dict[str, tuple[str, ...]] = {
    "shelf": ("shelf",),
    "downloads": ("downloads",),
    "activity": ("activity",),
    "mini_browser": ("mini_browser",),
    "game_locations": ("game_locations",),
    "browser": ("browser", "browser_advanced", "application"),
    "filesystem_file": ("filesystem", "desktop", "search"),
    "filesystem_folder": ("filesystem", "desktop", "search", "application"),
    "notes": ("overlay",),
    "overlay": ("overlay",),
    "screen": ("screen",),
    "application": ("application", "desktop", "window", "window_advanced"),
    "window": ("window", "window_advanced"),
    "process": ("system", "application", "diagnostics"),
    "system_status": ("system_monitor", "hardware", "diagnostics", "system"),
    "system_control": ("system",),
    "audio": ("audio",),
    "mouse": ("system",),
    "media_yandex": ("yandex_music",),
    "media_spotify": ("spotify",),
    "media_discord": ("discord",),
    "media_video": ("media", "browser"),
    # Owner is resolved from actual playback state before LLM use.  Keeping both
    # here permits that deterministic resolver, while cross-media semantics still
    # reject an explicitly named wrong destination.
    "media_generic": ("music", "yandex_music", "spotify", "discord"),
    "discord": ("discord", "transfer"),
    "email": ("email",),
    "telegram_messages": ("telegram_messages",),
    "calendar": ("google_calendar",),
    "translation": ("translate", "screen"),
    "weather": ("weather",),
    "date": ("date_info",),
    "timer": ("timer",),
    "memory": ("memory",),
    "input": ("input_control", "autoclicker"),
    "automation": ("automation", "scenario"),
    "developer": ("developer", "powershell"),
    "clipboard": ("clipboard", "transfer"),
    "network": ("network",),
    "diagnostics": ("diagnostics",),
    "history": ("history",),
    "text_edit": ("text_edit",),
    "briefing": ("briefing",),
    "game_session": ("game", "screen"),
    # Factual game/web turns are answered by GameWeb/SearchAPI, not ToolRegistry.
    "game_information": (),
    "web_information": (),
    # Explicit cross-domain transfer is intentionally a union rather than an
    # unlocked turn; unrelated system/note/media tools remain impossible.
    "transfer": ("transfer", "filesystem", "desktop", "search", "clipboard", "discord", "email", "browser"),
}

_SCHEMA_PRIORITY: dict[str, tuple[str, ...]] = {
    "browser": ("browser", "browser_advanced"),
    "filesystem_file": ("filesystem",),
    "filesystem_folder": ("filesystem",),
    "notes": ("overlay",),
    "overlay": ("overlay",),
    "screen": ("screen",),
    "application": ("application",),
    "window": ("window", "window_advanced"),
    "process": ("system", "application"),
    "system_status": ("system_monitor", "hardware"),
    "system_control": ("system",),
    "audio": ("audio",),
    "mouse": ("system",),
    "media_yandex": ("yandex_music",),
    "media_spotify": ("spotify",),
    "media_discord": ("discord",),
    "media_video": ("media", "browser"),
    "media_generic": ("music", "yandex_music", "spotify", "discord"),
    "discord": ("discord",),
    "email": ("email",),
    "telegram_messages": ("telegram_messages",),
    "calendar": ("google_calendar",),
    "translation": ("translate", "screen"),
    "weather": ("weather",),
    "date": ("date_info",),
    "timer": ("timer",),
    "memory": ("memory",),
    "input": ("input_control", "autoclicker"),
    "automation": ("automation", "scenario"),
    "developer": ("developer",),
    "clipboard": ("clipboard",),
    "network": ("network",),
    "diagnostics": ("diagnostics",),
    "history": ("history",),
    "text_edit": ("text_edit",),
    "briefing": ("briefing",),
    "game_session": ("game",),
    "game_information": (),
    "web_information": (),
    "transfer": ("transfer", "filesystem"),
}


def _claim(owner: str, confidence: float, reason: str, text: str) -> OwnershipClaim:
    return OwnershipClaim(
        owner=owner,
        confidence=float(confidence),
        reason=reason,
        allowed_tools=_TOOLSETS.get(owner, ()),
        schema_tools=_SCHEMA_PRIORITY.get(owner, ()),
        enforced=owner in _TOOLSETS,
        text=str(text or ""),
    )


def _has_action(t: str, words: str) -> bool:
    return bool(re.search(rf"\b(?:{words})\w*\b", t, re.I))


def classify_turn_ownership(text: str) -> OwnershipClaim:
    """Classify one utterance into a hard execution owner when evidence is strong."""
    raw = str(text or "")
    t = _norm(raw)
    if not t:
        return OwnershipClaim(text=raw)
    # Quoted/payload text must never redefine the shell.  We do not delete it
    # because entity words outside quotes are still useful; instead shell rules
    # below have strict precedence over topic rules.
    imperative = r"(?:открой|открыть|запусти|запустить|закрой|закрыть|покажи|показать|выведи|вывести|найди|найти|поищи|создай|создать|удали|удалить|перемести|перенеси|переименуй|скопируй|прочитай|запиши|добавь|включи|выключи|поставь|сделай|измени|установи|отправь|напиши|нажми|кликни|перейди|переключи|подключи|отключи|перезапусти)"

    # Translation is a shell: translated payload may freely contain words such
    # as browser/file/game without gaining ownership of those domains.
    if re.match(r"^(?:переведи|перевести|как\s+переводится|что\s+означает|translate\b|how\s+do\s+you\s+say\b)", t, re.I):
        return _claim("translation", 1.0, "explicit translation shell", raw)

    # Explicit cross-domain transfers are legitimate, but remain tightly scoped.
    if re.search(r"\btransfer\b", t, re.I) and re.search(r"\b(?:clipboard|буфер|file|файл|discord|дискорд|email|почт)\w*\b", t, re.I):
        return _claim("transfer", 0.995, "explicit transfer namespace", raw)

    if (re.search(r"\b(?:отправ|перешл|прикреп|перенес|transfer)\w*\b", t, re.I) and re.search(
        r"\b(?:файл\w*|file\w*|документ\w*|скриншот\w*|ссылк\w*|discord\w*|дискорд\w*|почт\w*|письм\w*|писем|email\w*)\b", t, re.I
    )) or (re.search(r"\b(?:file|файл)\w*\b", t, re.I) and re.search(r"\b(?:to|в|на)\s+(?:discord|дискорд|email|почт)\w*\b", t, re.I)):
        return _claim("transfer", 0.99, "explicit cross-domain transfer shell", raw)

    if re.search(r"\bbrowser\s+advanced\b", t, re.I):
        return _claim("browser", 0.995, "explicit browser-advanced namespace", raw)

    # Explicit web/browser shells come before payload nouns.  In
    # «найди в браузере как создать заметку» the words after the browser shell
    # are opaque search payload and must not become an overlay command.
    if re.match(r"^(?:загугли|погугли|гугл|google)\b", t, re.I) or (
        re.search(r"\b(?:найди|поищи|проверь)\w*\b", t, re.I)
        and re.search(r"\b(?:в\s+интернет\w*|в\s+сети|через\s+google|через\s+гугл)\b", t, re.I)
    ):
        return _claim("web_information", 1.0, "explicit web-knowledge shell", raw)
    if re.search(r"\b(?:в|через)\s+(?:браузер\w*|поисковик\w*)\b", t, re.I) or re.match(
        r"^(?:открой|запусти|покажи|закрой|перезапусти)\w*.*?\b(?:браузер\w*|browser\w*|chrome\w*|firefox\w*|opera(?:\s+gx)?|microsoft\s+edge|edge)\b", t, re.I
    ) or (re.search(r"\bвклад\w*\b", t, re.I) and (
        re.search(imperative, t, re.I) or re.search(r"\b(?:сколько|какие|какая|что|список|активн\w*)\b", t, re.I)
    )) or (re.search(r"\b(?:браузер\w*|browser\w*)\b", t, re.I) and re.search(r"\b(?:какой|какая|что|где|активн\w*|открыт(?!ь)\w*)\b", t, re.I)):
        return _claim("browser", 1.0, "explicit browser UI shell", raw)

    # HOTFIX116: a YouTube URL with an explicit Discord destination is media for
    # Discord, not generic browser navigation. This rule is language-neutral and
    # intentionally precedes the broad URL owner below.
    if re.search(r"\b(?:discord\w*|discort\w*|diskord\w*|diskort\w*|дискорд\w*|дискорт\w*|дискор\w*)\b", t, re.I) and re.search(
        r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/", raw, re.I
    ):
        return _claim("media_discord", 1.0, "explicit Discord YouTube URL", raw)

    # Explicit URLs are browser navigation, while Windows/POSIX-looking paths
    # are filesystem targets even when the basename contains words like CPU/game.
    # Accept bare hosts and postfix speech grammar too ("github.com открой").
    try:
        from jarvis.runtime.semantics import extract_explicit_browser_url
        explicit_url = extract_explicit_browser_url(raw)
    except Exception:
        explicit_url = ""
    if explicit_url:
        return _claim("browser", 1.0, "explicit URL navigation", raw)
    if re.match(imperative, t, re.I) and (
        re.search(r"(?:^|\s)[a-z]:[\\/]", raw, re.I)
        or re.search(r"(?:^|\s)/(?:home|users?|mnt|tmp|var)/", raw, re.I)
        or re.search(r"\.(?:txt|json|ya?ml|ini|log|pdf|docx?|xlsx?|pptx?|zip|rar|7z|py|js|ts|html?|css|md)\b", t, re.I)
    ):
        return _claim("filesystem_file", 0.995, "explicit path/file-extension shell", raw)

    # A current-game panel and "where did we stop" refer to the live game
    # session, not a generic JARVIS overlay and not factual GameWeb knowledge.
    if (re.search(r"\bпанел\w*\b", t, re.I) and re.search(r"\b(?:текущ\w*|активн\w*)\s+игр\w*\b", t, re.I)) or re.search(
        r"\bгде\s+(?:я|мы)\s+остановил\w*.*\b(?:игр\w*|game\w*)\b", t, re.I
    ):
        return _claim("game_session", 1.0, "explicit current-game session shell", raw)

    # JARVIS panels/notes are not filesystem documents unless the user says file.
    if re.search(r"\b(?:заметк\w*|чек[ -]?лист\w*|оверле\w*|панел\w*)\b", t, re.I) and not re.search(r"\b(?:файл\w*|документ\w*)\b", t, re.I):
        return _claim("notes" if "замет" in t else "overlay", 1.0, "explicit JARVIS panel object", raw)

    # Canonical Windows shell folders are filesystem objects even when the user
    # omits the word «папка».  Legacy routing used to interpret «открой Загрузки»
    # or «открой Рабочий стол» as application names, which violated domain
    # ownership before the filesystem resolver had a chance to act.  Keep the
    # list deliberately limited to unambiguous shell-folder names; generic
    # «музыка»/«видео» are excluded because they can denote media playback.
    known_shell_folder = re.fullmatch(
        r"(?:открой|открыть|запусти|запустить|покажи|показать|перейди\s+(?:в|на))\s+"
        r"(?:рабочий\s+стол|desktop|загрузк(?:и|у)|downloads?|документ(?:ы|ам)|documents?|"
        r"картинк(?:и|ам)|изображени(?:я|ям)|pictures?|домашн(?:яя|ю)\s+папк(?:а|у)|home|"
        r"профил(?:ь|я)|корзин(?:а|у)|recycle\s*bin|этот\s+компьютер|мой\s+компьютер|this\s+pc)",
        t, re.I,
    )
    if known_shell_folder:
        return _claim("filesystem_folder", 1.0, "canonical Windows shell-folder target", raw)

    # When both a file object and a containing folder are named, the grammatical
    # object immediately following the command owns the operation.  E.g.
    # «создай файл x.txt в папке Документы» is a file mutation with a folder
    # location, not a folder mutation.  This prevents the broad word «папка»
    # later in the sentence from stealing file ownership.
    if re.match(
        r"^(?:открой|открыть|создай|создать|удали|удалить|перемести|перенеси|переименуй|скопируй|прочитай|"
        r"запиши|добавь|измени|редактируй|распакуй|заархивируй)\w*\s+(?:этот\s+|эту\s+|новый\s+)?"
        r"(?:файл\w*|документ\w*|архив\w*)\b", t, re.I
    ):
        return _claim("filesystem_file", 1.0, "primary file/document object shell", raw)

    # Mailbox folders are mail objects, not filesystem directories.  This rule
    # deliberately precedes the broad ``папка`` shell below so phrases such as
    # «покажи почтовые папки» cannot be stolen by Files.
    if re.search(r"\b(?:почтов\w*|email\w*|gmail|mailbox\w*)\b", t, re.I) and re.search(
        r"\b(?:папк\w*|folder\w*|каталог\w*)\b", t, re.I
    ):
        return _claim("email", 1.0, "explicit mailbox-folder namespace", raw)

    # File/folder shells precede application/system topic words.  Thus cpu.txt is
    # a filename and «как создать заметку» can be a browser query payload.
    if re.search(r"\b(?:проводник\w*|explorer(?:\.exe)?|file\s+explorer)\b", t, re.I):
        return _claim("filesystem_folder", 1.0, "explicit file explorer object", raw)
    if re.search(r"\b(?:папк\w*|каталог\w*|директор\w*|folder\w*|directory\w*)\b", t, re.I) and (
        re.search(imperative, t, re.I) or re.search(r"\b(?:где|путь|содержим\w*|список|что\s+(?:в|находится)|какие|перечисли)\b", t, re.I)
    ):
        return _claim("filesystem_folder", 1.0, "explicit folder shell", raw)
    if re.search(r"\b(?:файл\w*|документ\w*|архив\w*|\.txt\b|\.json\b|\.pdf\b|\.docx\b|\.zip\b|\.rar\b)\b", t, re.I) and (
        re.search(imperative, t, re.I) or re.search(r"\b(?:где|путь|содержим\w*|прочитать|записать|редактир\w*|что\s+в|какие|прочитай)\b", t, re.I)
    ):
        return _claim("filesystem_file", 1.0, "explicit file/document shell", raw)

    # Screen/vision questions must not become game/system status just because the
    # pixels happen to show those subjects.
    if re.search(r"\b(?:на\s+экране|скриншот\w*|что\s+видишь|посмотри\s+на\s+экран|прочитай\s+экран)\b", t, re.I):
        return _claim("screen", 0.99, "explicit screen/vision shell", raw)

    # Input/autoclicker is a local control namespace.
    if re.search(r"\b(?:автокликер\w*|нажми\w*|нажать\s+клавиш\w*|горяч\w*\s+клавиш\w*|сочетан\w*\s+клавиш\w*|кликни\w*)\b", t, re.I):
        return _claim("input", 0.99, "explicit input-control shell", raw)

    # Media and communication precede active-game context.  This fixes the
    # observed Tarkov -> «покажи очередь треков» contamination.
    if re.search(r"\b(?:discord\w*|дискорд\w*|дискорт\w*)\b", t, re.I) and (re.search(
        r"\b(?:сервер\w*|канал\w*|войс\w*|сообщен\w*|отправ\w*|напиш\w*|музык\w*|трек\w*|очеред\w*|бот\w*|подключ\w*|отключ\w*|звук\w*|аудио\w*|громк\w*|mute|волн\w*|плейлист\w*)\b", t, re.I
    ) or re.search(r"\b(?:мои\s+(?:треки|песни)|(?:neo|нео)\s+(?:треки|песни)|мою\s+музыку)\b", t, re.I)
      or re.match(r"^(?:открой|запусти|покажи|перейди|закрой|перезапусти)\w*", t, re.I)):
        # HOTFIX97: an explicit Discord destination owns personalized Yandex
        # sources too.  "Моя волна" is the source payload; "в Discord" is the
        # execution destination.  Classifying by the payload used to lock the
        # turn to media_yandex and the registry then (correctly) blocked the
        # already-resolved discord.music_my_wave route.
        discord_media = bool(re.search(
            r"\b(?:музык\w*|трек\w*|очеред\w*|play|pause|звук\w*|аудио\w*|громк\w*|mute|волн\w*|плейлист\w*)\b",
            t, re.I,
        ) or re.search(r"\b(?:мои\s+(?:треки|песни)|(?:neo|нео)\s+(?:треки|песни)|мою\s+музыку)\b", t, re.I))
        return _claim("media_discord" if discord_media else "discord", 1.0, "explicit Discord namespace", raw)
    if re.search(r"\b(?:яндекс\s*музык\w*|yandex\s*music|моя\s+волна|мою\s+волну)\b", t, re.I):
        return _claim("media_yandex", 1.0, "explicit Yandex Music namespace", raw)
    if re.search(r"\b(?:spotify|спотифай\w*|спотифи\w*|спотиф\w*)\b", t, re.I):
        return _claim("media_spotify", 1.0, "explicit Spotify namespace", raw)
    # Provider-neutral music commands belong to unified Music, not the generic
    # Windows application/audio namespaces.  Explicit Yandex/Spotify/Discord
    # destinations were already claimed above.
    if re.search(r"\b(?:трек\w*|песн\w*|музык\w*)\b", t, re.I) and re.search(
        r"\b(?:включ|запуст|постав|начни|продолж|останов|пауза|следующ|предыдущ|найди|добав|очист|перемот)\w*\b", t, re.I
    ):
        return _claim("media_generic", 0.995, "explicit provider-neutral music shell", raw)
    if (re.search(r"\bгромкост\w*\b", t, re.I) and re.search(r"\bмузык\w*\b", t, re.I)) or re.search(
        r"\bмузык\w*\s+(?:громче|тише)\b", t, re.I
    ):
        return _claim("media_generic", 0.995, "music-specific volume shell", raw)
    if re.search(r"\b(?:youtube|ютуб\w*|ютьюб\w*|видео|видеоролик\w*|ролик\w*)\b", t, re.I) and re.search(
        r"\b(?:включ|постав|пауза|перемот|следующ|предыдущ|найди|открой|покажи|play|pause|seek)\w*\b", t, re.I
    ):
        return _claim("media_video", 0.99, "explicit video/YouTube media shell", raw)
    if re.search(r"\b(?:очеред\w*\s+(?:трек\w*|песен|песн\w*)|трек\w*\s+в\s+очеред|что\s+(?:сейчас\s+)?играет|следующ\w*\s+трек\w*|предыдущ\w*\s+трек\w*)\b", t, re.I):
        return _claim("media_generic", 0.99, "generic active-media transport/queue shell", raw)

    # Personal Telegram messages (Secretary Mode) are a dedicated namespace;
    # Telegram remote-control commands are handled by the phone transport itself.
    if re.search(r"\b(?:telegram|телеграм\w*|телег\w*)\b", t, re.I) and re.search(
        r"\b(?:сообщен\w*|написал\w*|написал[аи]?|отправител\w*|кто\s+написал|сколько|ответь|ответить|прочитай|последн\w*)\b", t, re.I
    ):
        return _claim("telegram_messages", 1.0, "explicit Telegram personal-message shell", raw)

    # Google Calendar/schedule is isolated from local timers/reminders. Explicit
    # words calendar/schedule/event/meeting own the turn; bare «напомни» remains timer.
    if re.search(r"\b(?:google\s*calendar|гугл\s*календар\w*|календар\w*|расписан\w*|событи\w*|встреч\w*)\b", t, re.I) and (
        re.search(imperative, t, re.I) or re.search(r"\b(?:сегодня|завтра|следующ\w*|какие|какое|что\s+у\s+меня|на\s+недел\w*)\b", t, re.I)
    ):
        return _claim("calendar", 1.0, "explicit calendar/schedule shell", raw)

    # Mail and explicit message namespaces should never route to files/notes.
    if re.search(r"\b(?:gmail|mail\.ru|мейл\.ру|email\w*|inbox\w*|почт\w*|письм\w*|писем|входящ\w*)\b", t, re.I) and (
        re.search(imperative, t, re.I) or re.search(r"\b(?:непрочитан\w*|входящ\w*|от\s+кого|тема\w*|последн\w*|сколько|какие|какое|что\s+в|кто\s+написал)\b", t, re.I)
    ):
        return _claim("email", 0.98, "explicit email shell", raw)

    # An explicit game title/context also owns hardware *requirements* questions.
    # «какой процессор нужен для игры Warframe» is external game knowledge, while
    # «какая температура процессора в Warframe сейчас» remains local telemetry.
    pre_game_ref = bool(re.search(
        r"\b(?:в\s+игр\w*|для\s+игр\w*|у\s+игр\w*|по\s+игр\w*|про\s+игр\w*|warframe\w*|warfr\w*|darktide\w*|tarkov\w*|elden\s+ring|rainbow\s+six|minecraft\w*|world\s+of\s+warcraft|\bwow\b|game\w*)\b",
        t, re.I,
    ))
    pre_game_question = bool(re.search(
        r"\b(?:как|где|что|кто|какой|какая|какое|какие|зачем|почему|сколько|расскажи|объясни|сравни|нужен|нужна|нужно|нужны|требован\w*)\b",
        t, re.I,
    ))
    local_telemetry = bool(re.search(
        r"\b(?:температур\w*|загрузк\w*|нагрузк\w*|частот\w*|потреблен\w*|использу\w*\s+сейчас|свободн\w*\s+мест|у\s+меня\s+(?:сейчас\s+)?)\b",
        t, re.I,
    ))
    if pre_game_ref and pre_game_question and not local_telemetry:
        return _claim("game_information", 0.995, "explicit game knowledge/requirements shape", raw)

    # Disk inventory/storage can be a filesystem request.  Bare «покажи диски»
    # or an explicit «файловая система» must not be consumed by PC telemetry;
    # temperature/load/health/space-status requests still belong to System.
    disk_obj = bool(re.search(r"\b(?:диск\w*|накопител\w*|drive\w*)\b", t, re.I))
    filesystem_storage = bool(re.search(r"\bфайлов\w*\s+систем\w*\b", t, re.I))
    disk_inventory = bool(disk_obj and re.search(r"\b(?:доступн\w*|перечисли|список|какие|покажи\s+(?:все\s+)?диск)\b", t, re.I))
    if filesystem_storage or disk_inventory:
        return _claim("filesystem_file", 0.995, "explicit filesystem drive/storage shell", raw)

    # Hardware/system status has a much stricter object requirement than generic
    # words such as «компоненты». Gameplay components therefore cannot become PC
    # components/statistics.
    hardware_obj = re.search(r"\b(?:cpu|gpu|ram|vram|процессор\w*|видеокарт\w*|оперативн\w*\s+памят\w*|видеопамят\w*|диск\w*|накопител\w*)\b", t, re.I)
    status_shell = re.search(r"\b(?:статус|состояни\w*|загрузк\w*|нагрузк\w*|температур\w*|частот\w*|потреблен\w*|сколько\s+памят|свободн\w*\s+мест)\b", t, re.I)
    if hardware_obj and (status_shell or re.search(r"\b(?:сколько|какой|какая|какое|какие|покажи|назови|что\s+за)\b", t, re.I)):
        return _claim("system_status", 1.0, "explicit PC hardware status shell", raw)
    if re.search(r"\b(?:статус|состояни\w*|сводк\w*)\s+(?:компьютер\w*|систем\w*)\b", t, re.I):
        return _claim("system_status", 1.0, "explicit computer-status shell", raw)

    # Audio/device controls. Bluetooth remains system-control because pairing and
    # enable/disable are OS operations, not media playback.
    if re.search(r"\b(?:(?:микрофон|микро)\w*|наушник\w*|динамик\w*|колонк\w*|устройств\w*\s+(?:ввода|вывода)|аудиовход\w*|аудиовыход\w*)\b", t, re.I) and re.search(
        r"\b(?:какие|какой|какая|покажи|перечисли|список|выбран\w*|доступн\w*)\b", t, re.I
    ):
        return _claim("audio", 0.995, "explicit audio device/status namespace", raw)
    if re.search(r"\b(?:микрофон\w*|наушник\w*|динамик\w*|звук\w*|аудио\w*|громкост\w*|volume|mute|мьют\w*)\b", t, re.I) and re.search(
        r"\b(?:включ|выключ|громк|тише|громче|mute|мьют|установ|постав|выбер|переключ)\w*\b", t, re.I
    ):
        return _claim("audio", 0.99, "explicit audio-control shell", raw)
    if re.match(r"^(?:открой|покажи|запусти)\w*\s+(?:систем\w*|system)\b", t, re.I):
        return _claim("system_control", 0.99, "explicit OS system shell", raw)
    if re.search(r"\b(?:bluetooth|блютуз\w*|wi[ -]?fi|вайфай\w*|параметр\w*|настройк\w*(?:\s+windows)?|settings|диспетчер\s+задач|task\s+manager)\b", t, re.I) and re.search(imperative, t, re.I):
        return _claim("system_control", 0.99, "explicit OS-control/settings shell", raw)
    if re.search(r"^отмени\s+выключение\b", t, re.I) or (
        re.match(r"^(?:выключи|перезагрузи|перезапусти|заблокируй|усыпи)\b", t, re.I)
        and re.search(r"\b(?:пк|компьютер\w*|систем\w*|windows|виндовс\w*|машин\w*)\b", t, re.I)
    ):
        return _claim("system_control", 1.0, "explicit OS power/control shell", raw)

    if re.search(r"\b(?:мыш\w*|mouse)\b", t, re.I) and re.search(r"\b(?:чувствительност\w*|скорост\w*)\b", t, re.I):
        return _claim("mouse", 0.995, "explicit mouse-sensitivity namespace", raw)

    # Other local namespaces that otherwise look like ordinary factual questions.
    if re.search(r"\b(?:ping|пинг\w*|ip(?:-?адрес)?|dns|скорост\w*\s+интернет|статус\s+сет\w*|сетев\w*\s+подключ)\b", t, re.I):
        return _claim("network", 0.99, "explicit network diagnostics namespace", raw)
    if re.search(r"\b(?:self[ -]?test|селф[ -]?тест|диагностик\w*|latency|задержк\w*)\b", t, re.I) and re.search(r"\b(?:запуст|провед|покажи|проверь|измер)\w*\b", t, re.I):
        return _claim("diagnostics", 0.98, "explicit diagnostics namespace", raw)
    if re.search(r"\b(?:истори\w*\s+действ|что\s+ты\s+(?:сейчас|только\s+что)\s+сделал|отмени\s+последн|верни\s+.*обратно)\b", t, re.I):
        return _claim("history", 0.98, "explicit action-history namespace", raw)
    if re.search(r"\b(?:исправь\s+орфограф\w*|замени\s+последн\w*\s+предложен|выдели\s+предыдущ\w*\s+абзац)\b", t, re.I):
        return _claim("text_edit", 0.98, "explicit text-edit namespace", raw)
    if re.search(r"\b(?:сводк\w*\s+на\s+день|утренн\w*\s+сводк\w*|daily\s+briefing|morning\s+briefing)\b", t, re.I):
        return _claim("briefing", 0.99, "explicit daily-briefing namespace", raw)
    if re.search(r"\b(?:какие|какая|сколько|покажи|список)\b", t, re.I) and re.search(r"\b(?:процесс\w*|запущен\w*\s+(?:приложен\w*|программ\w*))\b", t, re.I):
        return _claim("process", 0.98, "explicit running-process inventory", raw)
    if re.search(r"\b(?:какие|сколько|покажи|список)\b", t, re.I) and re.search(r"\b(?:окн\w*|window\w*)\b", t, re.I):
        return _claim("window", 0.98, "explicit window inventory", raw)

    # Window and application shells precede game knowledge. «запусти Warframe»
    # launches an app; it never asks Google about Warframe.
    if re.search(r"\b(?:окн\w*|window\w*)\b", t, re.I) and re.search(
        r"\b(?:перемест|перенес|сверн|разверн|закро|фокус|активир|покажи|выбери)\w*\b", t, re.I
    ):
        return _claim("window", 0.99, "explicit existing-window shell", raw)
    if re.match(r"^(?:открой|запусти|закрой|перезапусти|перейди\s+в)\b", t, re.I) and not re.search(r"\b(?:файл\w*|папк\w*|вкладк\w*|настройк\w*)\b", t, re.I):
        return _claim("application", 0.96, "explicit application lifecycle shell", raw)
    if re.search(r"\b(?:процесс\w*|process\w*)\b", t, re.I) and re.search(r"\b(?:заверш|убей|останов|список|покажи|найди)\w*\b", t, re.I):
        return _claim("process", 0.98, "explicit OS process shell", raw)

    # Game session/player-state is distinct from factual game knowledge.
    if re.search(r"\b(?:сесси\w*|session\w*)\b", t, re.I) and re.search(r"\b(?:игр\w*|game\w*|моя|мой|я)\b", t, re.I):
        return _claim("game_session", 1.0, "explicit game-session shell", raw)
    if re.search(r"\b(?:мой|моя|мои|у\s+меня)\b", t, re.I) and re.search(r"\b(?:инвентар\w*|экипиров\w*|прогресс\w*|билд\w*|сохранен\w*|сохранён\w*)\b", t, re.I):
        return _claim("game_session", 0.99, "explicit player-state shell", raw)

    # Factual gameplay knowledge.  This does not require an exact supported game
    # title: ASR typos such as warfrme still stay in the web/game namespace.
    game_ref = bool(re.search(
        r"\b(?:в\s+игр\w*|по\s+игр\w*|про\s+игр\w*|из\s+игр\w*|warframe\w*|warfr\w*|darktide\w*|tarkov\w*|elden\s+ring|rainbow\s+six|minecraft\w*|world\s+of\s+warcraft|\bwow\b|game\w*)\b",
        t, re.I,
    ))
    gameplay_fact = bool(re.search(
        r"\b(?:как|где|что|кто|какой|какая|какое|какие|зачем|почему|сколько|расскажи|объясни|сравни|"
        r"выбить|получить|добыть|найти|фарм\w*|крафт\w*|скрафт\w*|пройти|выполнить|"
        r"компонент\w*|оруж\w*|оперативник\w*|персонаж\w*|квест\w*|босс\w*|ресурс\w*|перк\w*|"
        r"талант\w*|способност\w*|урон\w*|билд\w*|предмет\w*|дроп\w*|лут\w*)\b",
        t, re.I,
    ))
    if game_ref and gameplay_fact:
        return _claim("game_information", 0.995, "explicit gameplay-information shape", raw)

    # Use the shared lexical-collision guard.  A raw ``ветер\w*`` pattern
    # also matches ``ветеран`` and historically routed Darktide ability
    # questions into Weather.  Keep one canonical weather classifier across
    # ownership, direct integration parsing and LLM tool selection.
    if _weather_signal(t):
        return _claim("weather", 0.98, "explicit weather namespace", raw)
    if re.search(r"\b(?:таймер\w*|напомни\w*|напоминан\w*|будильник\w*)\b", t, re.I):
        return _claim("timer", 0.98, "explicit timer/reminder namespace", raw)
    if re.search(r"\b(?:какая\s+дата|какое\s+число|день\s+недели|праздник\w*)\b", t, re.I):
        return _claim("date", 0.98, "explicit date namespace", raw)
    if re.match(r"^(?:запомни|забудь|ты\s+помнишь|что\s+ты\s+знаешь\s+обо?\s+мне)\b", t, re.I):
        return _claim("memory", 0.98, "explicit memory namespace", raw)
    if re.search(r"\b(?:буфер\w*|clipboard\w*)\b", t, re.I):
        return _claim("clipboard", 0.98, "explicit clipboard namespace", raw)
    if re.search(r"\b(?:создай\s+приложение|напиши\s+код|исправь\s+код|собери\s+exe|powershell\b)\b", t, re.I):
        return _claim("developer", 0.96, "explicit developer namespace", raw)

    return OwnershipClaim(owner="conversation", confidence=0.0, reason="no exclusive domain shell", text=raw)


def claim_for_owner(owner: str, *, text: str = "", reason: str = "explicit deterministic owner") -> OwnershipClaim:
    """Build a validated claim for a deterministic parser that already owns the shell."""
    return _claim(str(owner or "conversation"), 1.0, reason, text)


def current_turn_claim() -> OwnershipClaim:
    return _CURRENT.get() or OwnershipClaim()


def override_turn_ownership(owner: str, *, reason: str = "explicit runtime contract", text: str = "") -> OwnershipClaim:
    """Authoritatively replace the current claim for an exact runtime contract.

    Used for user-defined scenario phrases whose literal text may intentionally
    look like another domain (e.g. a scenario named «Погода»).
    """
    claim = _claim(str(owner or "conversation"), 1.0, reason, text or current_turn_claim().text)
    _CURRENT.set(claim)
    return claim


def bind_exact_tool_ownership(tool: str, *, text: str = "") -> OwnershipClaim:
    """Bind a fully matched, schema-checked grammar to exactly one tool.

    Unlike broad lexical domains, explicit native templates need no permissive
    fallback for unfamiliar tool namespaces. The final registry still enforces
    ownership, permissions, target guards and risk confirmation.
    """
    claim = OwnershipClaim(
        owner="native:" + tool, confidence=1.0, reason="reviewed native command template",
        allowed_tools=(tool,), schema_tools=(tool,), enforced=True, text=text,
    )
    _CURRENT.set(claim)
    return claim


def refine_turn_ownership(text: str) -> OwnershipClaim:
    """Replace the current claim after multilingual/ASR normalization."""
    fresh = classify_turn_ownership(text)
    current = current_turn_claim()
    # Never downgrade an explicit raw-text claim to conversation just because a
    # later normalizer removed a token.  A stronger normalized claim may replace it.
    if not fresh.enforced and current.enforced:
        return current
    if fresh.enforced and (not current.enforced or fresh.confidence >= current.confidence - 0.02):
        _CURRENT.set(fresh)
        return fresh
    return current




@contextmanager
def ownership_claim_scope(claim: OwnershipClaim | None):
    """Temporarily restore a previously validated ownership claim."""
    token = _CURRENT.set(claim if isinstance(claim, OwnershipClaim) else OwnershipClaim())
    try:
        yield current_turn_claim()
    finally:
        _CURRENT.reset(token)


@contextmanager
def execution_union_scope(claims: Iterable[OwnershipClaim], *, extra_tools: Iterable[str] = ("automation",), text: str = ""):
    """Allow only a preflighted union for an explicit compound command.

    This is not a generic unlocked owner. Each child call must be validated
    against its own clause claim *before* entering this scope; the union only
    permits the deterministic automation executor to replay that approved plan.
    """
    rows = [c for c in claims if isinstance(c, OwnershipClaim) and c.enforced]
    allowed: list[str] = []
    for c in rows:
        for name in c.allowed_tools:
            if name not in allowed:
                allowed.append(name)
    for name in extra_tools:
        if str(name) not in allowed:
            allowed.append(str(name))
    compound = OwnershipClaim(
        owner="compound", confidence=1.0, reason="explicit preflighted multi-domain command",
        allowed_tools=tuple(allowed), schema_tools=("automation",), enforced=True, text=str(text or ""),
    )
    token = _CURRENT.set(compound)
    try:
        yield compound
    finally:
        _CURRENT.reset(token)


@contextmanager
def turn_ownership_scope(text: str):
    claim = classify_turn_ownership(text)
    token = _CURRENT.set(claim)
    try:
        yield claim
    finally:
        _CURRENT.reset(token)


# NEO 169: a tool that already passed this check may use another tool as part of
# its own implementation (for example «нажми Продолжить» in the ui tool clicks the
# on-screen button through input_control; input_control falls back to the screen
# tool when UI Automation does not see the element). Only these reviewed
# parent → child pairs are delegations, only while the parent's handler runs, and
# the child still goes through validation, risk, confirmation and target guards.
INTERNAL_DELEGATIONS: frozenset[tuple[str, str]] = frozenset({
    ("ui", "input_control"), ("ui", "screen"),
    ("input_control", "ui"), ("input_control", "screen"),
    ("screen", "input_control"), ("screen", "ui"),
    ("copy", "clipboard"), ("copy", "screen"), ("copy", "window"), ("copy", "input_control"),
    ("desktop", "browser"), ("desktop", "application"),
    ("history", "filesystem"),
    ("transfer", "discord"), ("transfer", "browser_advanced"), ("transfer", "clipboard"),
})
_ACTIVE_TOOL: ContextVar[str] = ContextVar("neo_active_tool", default="")


@contextmanager
def tool_execution_scope(tool: str):
    """Mark the handler of ``tool`` as running (used by ToolRegistry around handlers)."""
    token = _ACTIVE_TOOL.set(_norm(tool))
    try:
        yield
    finally:
        _ACTIVE_TOOL.reset(token)


def delegated_call(tool: str) -> bool:
    parent = _ACTIVE_TOOL.get()
    return bool(parent) and (parent, _norm(tool)) in INTERNAL_DELEGATIONS


def tool_allowed(tool: str, arguments: dict[str, Any] | None = None, claim: OwnershipClaim | None = None) -> tuple[bool, str]:
    c = claim or current_turn_claim()
    if _norm(tool) in {"obs_control", "capture"}:
        from jarvis.runtime.streaming_commands184 import obs_tool_allowed
        allowed, reason = obs_tool_allowed(_norm(tool), dict(arguments or {}), c.text)
        if not allowed:
            return False, reason
    if not c.enforced:
        return True, ""
    name = _norm(tool)
    if delegated_call(name):
        return True, ""
    if name not in {_norm(x) for x in c.allowed_tools}:
        return False, f"turn ownership '{c.owner}' forbids tool '{name}' ({c.reason})"

    args = dict(arguments or {})
    action = _norm(args.get("action", ""))
    # Browser ownership may use application only to open a browser itself.  This
    # closes the remaining loophole where an LLM could see an app schema and open
    # an unrelated program while handling a browser request.
    if c.owner == "browser" and name == "application":
        target = _norm(args.get("name", "") or args.get("query", "") or args.get("target", ""))
        browserish = bool(re.search(r"\b(?:browser|браузер|chrome|firefox|opera|edge)\b", target, re.I))
        if action not in {"open", "launch", "run"} or not browserish:
            return False, "browser owner permits application only for opening a browser"
    if c.owner == "filesystem_file" and name == "filesystem":
        file_actions = {
            "open", "find", "copy", "move", "rename", "delete", "delete_permanent",
            "read_text", "write_text", "replace_text", "create_text_file", "create_file",
            "append_text", "insert_text", "search_text", "exists", "info", "zip", "unzip",
            "hash", "undo_last_change", "reveal",
        }
        if action and action not in file_actions:
            return False, f"file owner forbids folder/disk filesystem action '{action}'"
    if c.owner == "filesystem_folder" and name == "filesystem":
        folder_actions = {
            "open", "list", "find", "create_folder", "copy", "move", "rename", "delete",
            "delete_permanent", "exists", "info", "zip", "unzip", "list_drives",
            "disk_usage", "tree", "reveal",
        }
        if action and action not in folder_actions:
            return False, f"folder owner forbids file-content filesystem action '{action}'"
    if c.owner == "filesystem_file" and name == "desktop":
        hint = _norm(args.get("hint", ""))
        query = str(args.get("query", "") or "")
        literal_file = bool(
            hint == "file"
            or re.search(r"^[a-zA-Z]:[\\/].+", query)
            or re.search(r"[\\/]", query)
            or re.search(r"\.(?:txt|json|ya?ml|ini|log|pdf|docx?|xlsx?|pptx?|zip|rar|7z|py|js|ts|html?|css|md)\b", query, re.I)
        )
        if action not in {"open", "resolve"} or not literal_file:
            return False, "file owner permits desktop only for a literal file target"
    if c.owner == "filesystem_folder" and name == "desktop":
        hint = _norm(args.get("hint", ""))
        if action not in {"open", "resolve"} or hint not in {"folder", "directory"}:
            return False, "folder owner permits desktop only for a folder-locked target"
    if c.owner == "filesystem_folder" and name == "application":
        target = _norm(args.get("name", "") or args.get("query", "") or args.get("target", ""))
        if action not in {"open", "launch", "run"} or not re.search(r"\b(?:explorer|проводник|file\s+explorer)\b", target, re.I):
            return False, "folder owner permits application only for File Explorer"
    if c.owner == "application" and name == "desktop":
        hint = _norm(args.get("hint", ""))
        if action not in {"open", "resolve"} or hint not in {"app", "game"}:
            return False, "application owner permits desktop only for app/game-locked targets"
    if c.owner == "application" and name in {"window", "window_advanced"}:
        allowed = {"active", "list", "focus", "restore", "list_instances", "focus_instance"}
        if action not in allowed:
            return False, "application owner permits window tools only for non-destructive focus/inspection"
    if c.owner == "system_status" and name == "system":
        if action not in {"status", "top_processes", "list_power_plans", "get_power_plan", "get_mouse_sensitivity"}:
            return False, "system-status owner permits only read-only system actions"
    if c.owner == "process" and name == "system":
        if action not in {"top_processes", "terminate_process", "open_task_manager"}:
            return False, "process owner permits only process-management system actions"
    if c.owner == "process" and name == "application":
        if action not in {"close", "kill", "list", "find", "info"}:
            return False, "process owner cannot launch applications"
    if c.owner == "notes" and name == "overlay":
        if action not in {"show", "hide", "close", "delete", "rename", "list", "set_text", "append_text", "remove_text", "clear_text", "move"}:
            return False, "notes owner cannot mutate checklist/music overlay semantics"
    if c.owner == "translation" and name == "screen":
        if action not in {"capture", "capture_window", "analyze", "analyze_window", "translate"}:
            return False, "translation owner may read/translate screen content but never click it"
    if c.owner == "game_session" and name == "screen":
        if action not in {"capture", "capture_window", "analyze", "analyze_window"}:
            return False, "game-session owner may inspect screen state but never click it"
    if c.owner == "mouse" and name == "system":
        if action not in {"get_mouse_sensitivity", "set_mouse_sensitivity", "change_mouse_sensitivity"}:
            return False, "mouse owner permits only mouse-sensitivity actions"
    return True, ""


def routed_conflict(tool: str, arguments: dict[str, Any] | None = None, claim: OwnershipClaim | None = None) -> str:
    ok, reason = tool_allowed(tool, arguments, claim)
    return "" if ok else reason


__all__ = [
    "OwnershipClaim", "classify_turn_ownership", "claim_for_owner", "current_turn_claim",
    "refine_turn_ownership", "override_turn_ownership", "turn_ownership_scope", "ownership_claim_scope",
    "execution_union_scope", "tool_allowed", "routed_conflict",
]
