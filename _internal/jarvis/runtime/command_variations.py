# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Conservative ASR vocabulary repair for spoken desktop commands.

This module is deliberately *not* a general spell checker.  It repairs only words
that belong to JARVIS' action/domain vocabulary and keeps user payloads (song names,
file names, messages, game names, etc.) untouched.  The first command word may be
repaired more aggressively; later words require an exact generated alias or a high
fuzzy score.
"""

import difflib
import re
from functools import lru_cache

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+", re.UNICODE)
_PROTECTED_INTERROGATIVES = {"какой", "какая", "какое", "какую", "какие", "какого", "какому", "каким", "какой-то"}

# Canonical surface forms are the words the deterministic routers already know.
# Aliases include common Russian inflections, transliterations and observed Whisper
# slips.  Generated variants below multiply coverage without maintaining thousands
# of hand-written entries in production code.
COMMAND_VARIATION_GROUPS: dict[str, tuple[str, ...]] = {
    # Core actions / filesystem / applications.
    "открой": ("открой", "открыть", "аткрой", "открои", "ткрой", "otkroy", "otkroj", "atkroy", "open"),
    "закрой": ("закрой", "закрыть", "зкрой", "закрои", "zakroy", "zakroj", "close"),
    "запусти": ("запусти", "запустить", "зопусти", "запусти", "zapusti", "zapusty", "run", "launch"),
    "покажи": ("покажи", "показать", "пакажи", "покожи", "pokazhi", "pokaji", "show"),
    "скрой": ("скрой", "скрыть", "спрячь", "спряч", "skroy", "hide"),
    "найди": ("найди", "найти", "наиди", "найд", "najdi", "naydi", "find", "search"),
    "поищи": ("поищи", "поиши", "паищи", "poishi", "search"),
    "создай": ("создай", "создать", "саздай", "создой", "sozdai", "sozday", "create"),
    "скопируй": ("скопируй", "копируй", "скопируи", "скапируй", "skopiruy", "copy"),
    "перемести": ("перемести", "переместить", "перемисти", "перемесити", "peremesti", "move"),
    "перенеси": ("перенеси", "перенести", "перенисе", "перенис", "perenesi", "move"),
    "переименуй": ("переименуй", "переименовать", "переименуи", "пирименуй", "rename"),
    "удали": ("удали", "удалить", "удолить", "удолей", "удоли", "udali", "delete", "remove"),
    "прочитай": ("прочитай", "прочесть", "прочитай", "прачитай", "read"),
    "запиши": ("запиши", "записать", "запеши", "зопиши", "write"),
    "напиши": ("напиши", "написать", "напеши", "напешы", "write", "type"),
    "впиши": ("впиши", "вписать", "фпиши", "впеши", "enter", "type"),
    "замени": ("замени", "заменить", "зомени", "замини", "replace"),
    "добавь": ("добавь", "добавить", "добафь", "добовь", "добав", "add"),
    "вставь": ("вставь", "вставить", "фставь", "встать", "paste", "insert"),
    "очисти": ("очисти", "очистить", "ачисти", "очести", "clear", "flush"),
    "распакуй": ("распакуй", "распаковать", "распокуи", "unzip", "extract"),
    "заархивируй": ("заархивируй", "архивируй", "заархивируи", "zip", "archive"),
    "отмени": ("отмени", "отменить", "атмени", "отмини", "undo", "cancel"),
    "верни": ("верни", "вернуть", "вирни", "return", "restore"),
    "проверь": ("проверь", "проверить", "праверь", "провирь", "check"),
    "сохрани": ("сохрани", "сохранить", "сахрани", "сохронить", "save"),
    "отправь": ("отправь", "отправить", "атправь", "отправ", "send"),
    "скачай": ("скачай", "скачать", "скочай", "download"),
    "установи": ("установи", "установить", "устонови", "install"),
    "подключись": ("подключись", "подключиться", "падключись", "join", "connect"),
    "отключись": ("отключись", "отключиться", "атключись", "leave", "disconnect"),
    "сделай": ("сделай", "сделать", "зделай", "sdelai", "do"),
    "выведи": ("выведи", "вывести", "вывиди", "show", "display"),
    "отобрази": ("отобрази", "отобразить", "атобрази", "display"),
    "загугли": ("загугли", "загуглить", "зогугли", "google"),

    # Windows/window/browser/UI.
    "сверни": ("сверни", "свернуть", "свирни", "minimize"),
    "разверни": ("разверни", "развернуть", "развирни", "maximize"),
    "восстанови": ("восстанови", "восстановить", "вастанови", "restore"),
    "сфокусируй": ("сфокусируй", "фокус", "сфакусируй", "focus"),
    "переключи": ("переключи", "переключить", "переключь", "переключе", "switch"),
    "перейди": ("перейди", "перейти", "прейди", "периди", "go", "navigate"),
    "нажми": ("нажми", "нажать", "нажми", "нажмы", "click", "press"),
    "выбери": ("выбери", "выбрать", "выбири", "select", "choose"),
    "введи": ("введи", "ввести", "ввиды", "enter", "type"),
    "напечатай": ("напечатай", "напечатать", "напичатай", "type"),

    # Media / Discord / Yandex / YouTube.
    "включи": ("включи", "включить", "вклучи", "включы", "включе", "vklyuchi", "play", "start"),
    "выключи": ("выключи", "выключить", "выклучи", "выключы", "turnoff", "disable"),
    "поставь": ("поставь", "поставить", "паставь", "постафь", "постав", "set", "put"),
    "пауза": ("пауза", "паузу", "пауз", "пауса", "pause", "pauza", "pausa"),
    "приостанови": ("приостанови", "приостановить", "преостанови", "приостонави"),
    "стоп": ("стоп", "стап", "стоб", "стопп", "stop", "stap", "stopp", "stob"),
    "останови": ("останови", "остановить", "астанови", "останов"),
    "продолжи": ("продолжи", "продолжить", "прадолжи", "продалжи", "продолжен", "prodolzhi", "resume", "continue"),
    "возобнови": ("возобнови", "возобновить", "вазобнови", "возабнови", "resume"),
    "пропусти": ("пропусти", "пропустить", "прапусти", "пропусте", "скипни"),
    "дальше": ("дальше", "далше", "дальш", "далши", "далие", "далее", "dalshe", "dalsze", "dalsza", "dalze", "dalej", "دلشة", "next"),
    "скип": ("скип", "скипни", "скып", "скиб", "скипп", "skip", "skib", "skeep", "skyp"),
    "назад": ("назад", "назат", "назадд", "back", "previous", "prev", "previus", "prew"),
    "предыдущий": ("предыдущий", "предыдущая", "предыдущее", "предидущий", "предыдущ", "предущ", "предыд"),
    "перемотай": ("перемотай", "перемотать", "перемотои", "seek", "rewind"),
    "перемешай": ("перемешай", "перемешать", "перемишай", "shuffle"),
    "повтор": ("повтор", "повтори", "повторяй", "репит", "repeat"),
    "лайк": ("лайк", "лайкни", "поставь лайк", "like", "a like", "alike", "laik"),
    "дизлайк": ("дизлайк", "дизлайкни", "дислайк", "дизлаик", "дизлек", "dislike", "dislaik", "dislayk", "thumbs down"),

    # Audio/system/hardware/network.
    "громче": ("громче", "громчи", "громче", "louder", "volumeup"),
    "тише": ("тише", "тиши", "tishe", "quieter", "volumedown"),
    "приглуши": ("приглуши", "приглушить", "приглушы", "duck", "mute"),
    "заблокируй": ("заблокируй", "заблокировать", "зоблокируй", "lock"),
    "перезагрузи": ("перезагрузи", "перезапусти", "перезагрузить", "restart", "reboot"),
    "усыпи": ("усыпи", "сон", "sleep"),
    "запомни": ("запомни", "запомнить", "зопомни", "remember"),
    "забудь": ("забудь", "забыть", "забуть", "forget"),
    "пинг": ("пинг", "пингани", "проверить пинг", "ping"),
    "переведи": ("переведи", "перевести", "пириведи", "translate"),
    "проанализируй": ("проанализируй", "проанализировать", "анализируй", "analyze"),
    "сними": ("сними", "снять", "снеми", "capture"),
    "отметь": ("отметь", "отметить", "атметь", "check"),

    # Conversation / information commands also benefit from stable first words.
    "расскажи": ("расскажи", "расказать", "раскажи", "расскажы", "tell"),
    "объясни": ("объясни", "обьясни", "обесни", "explain"),
    "помоги": ("помоги", "памаги", "help"),

    # Domain/object vocabulary. These are repaired only with stricter rules after
    # the first token so song/file/message payloads are not rewritten.
    "файл": ("файл", "фаил", "fajl", "file"),
    "папка": ("папка", "папку", "папки", "папке", "papka", "papku", "folder"),
    "проводник": ("проводник", "проводнек", "провадник", "explorer"),
    "окно": ("окно", "акно", "window"),
    "вкладка": ("вкладка", "вкладку", "вкладке", "фкладка", "tab"),
    "браузер": ("браузер", "броузер", "браузир", "brauzer", "browser"),
    "дискорд": ("дискорд", "дискорт", "дискор", "дскорд", "дескорд", "дескорт", "детскорд", "детскорде", "discord", "discort", "diskord", "diskort"),
    "яндекс": ("яндекс", "андекс", "ендекс", "иандекс", "яндикс", "яндэкс", "yandex"),
    "ютуб": ("ютуб", "ютьюб", "ютюб", "ютубе", "ютьюбе", "youtube"),
    "музыка": ("музыка", "музыку", "музика", "музыке", "music", "muzyka", "muzika"),
    "видео": ("видео", "видево", "видиа", "видио", "video"),
    "трек": ("трек", "трэк", "трак", "трап", "track"),
    "песня": ("песня", "песню", "песни", "песне", "song"),
    "плейлист": ("плейлист", "плэйлист", "плелист", "плей лист", "playlist"),
    "сервер": ("сервер", "сервир", "server"),
    "канал": ("канал", "канол", "channel"),
    "микрофон": ("микрофон", "микрафон", "microphone", "mic"),
    "громкость": ("громкость", "громкасть", "volume"),
    "блютуз": ("блютуз", "блютус", "bluetooth"),
    "вайфай": ("вайфай", "вай фай", "wifi", "wi-fi"),
    "яркость": ("яркость", "яркасть", "brightness"),
    "экран": ("экран", "икран", "screen", "display"),
    "монитор": ("монитор", "манитор", "monitor"),
    "скриншот": ("скриншот", "скрин", "скрин шот", "screenshot"),
    "буфер": ("буфер", "буффер", "clipboard"),
    "процесс": ("процесс", "процес", "process"),
    "настройки": ("настройки", "настроики", "settings"),
    "чеклист": ("чеклист", "чек лист", "чек-лист", "checklist"),
    "панель": ("панель", "панел", "panel", "overlay"),
    "заметка": ("заметка", "заметку", "заметки", "note"),
    "игра": ("игра", "игру", "игры", "game"),
    "сессия": ("сессия", "сесия", "session"),
    "устройство": ("устройство", "устройства", "девайс", "device"),
    "диск": ("диск", "диска", "drive"),
    "архив": ("архив", "архиф", "archive", "zip"),
    "сеть": ("сеть", "сети", "network"),
    "память": ("память", "памети", "memory"),
    "приложение": ("приложение", "приложения", "программу", "программа", "app", "application"),
    "документ": ("документ", "дакумент", "document"),
    "корзина": ("корзина", "корзину", "карзина", "recyclebin"),
    "сайт": ("сайт", "саит", "website", "site"),
    "ссылка": ("ссылка", "ссылку", "сылка", "link", "url"),
    "интернет": ("интернет", "интирнет", "internet", "web"),
    "сообщение": ("сообщение", "сообщения", "саобщение", "message"),
    "почта": ("почта", "почту", "почте", "email", "mail"),

    # HOTFIX23: API/productivity vocabulary. The existing generated mutation
    # engine automatically adds missing-letter, transliterated and doubled-final
    # variants, so these compact families cover thousands of ASR spellings without
    # bloating the runtime prompt.
    "погода": ("погода", "погоде", "погоду", "пагода", "погада", "weather", "wether"),
    "температура": ("температура", "температуре", "температуру", "темпиратура", "температруа", "temperature", "temperatur"),
    "прогноз": ("прогноз", "прогнос", "прогноз погоды", "forecast", "forcast"),
    "дождь": ("дождь", "дожь", "дошдь", "дождик", "rain", "raining"),
    "ветер": ("ветер", "ветра", "ветир", "wind"),
    "влажность": ("влажность", "влажнасть", "влажности", "humidity", "humiditi"),
    "осадки": ("осадки", "осадков", "асадки", "precipitation", "precip"),
    "восход": ("восход", "восхот", "sunrise"),
    "закат": ("закат", "закатт", "sunset"),
    "стим": ("стим", "стеам", "steam", "stiem"),
    "друг": ("друг", "друга", "другу", "friend", "freind"),
    "друзья": ("друзья", "друзей", "друья", "friends", "frends"),
    "достижения": ("достижения", "достижений", "достиженя", "ачивки", "achievements", "achivements"),
    "календарь": ("календарь", "календаре", "календаря", "калиндарь", "calendar", "calender"),
    "событие": ("событие", "события", "событий", "сабытие", "event", "events"),
    "встреча": ("встреча", "встречу", "встречи", "фстреча", "meeting", "meetings"),
    "непрочитанные": ("непрочитанные", "непрочитанных", "непрочитаные", "unread", "unred"),
    "письмо": ("письмо", "письма", "писем", "писмо", "email", "mail"),
    "ответь": ("ответь", "ответить", "атветь", "reply", "replay"),
    "таймер": ("таймер", "таймир", "таймерр", "timer", "taimer"),
    "напоминание": ("напоминание", "напомни", "напоменание", "reminder", "remind"),
    "будильник": ("будильник", "будилник", "будильнек", "alarm", "alaram"),
    "процессор": ("процессор", "процесор", "процесорр", "cpu", "processor"),
    "видеокарта": ("видеокарта", "видиокарта", "видеокарты", "gpu", "graphicscard"),
    "оперативная": ("оперативная", "оперативка", "оперативной", "ram", "memory"),
    "сводка": ("сводка", "сводку", "сфодка", "summary", "briefing", "status"),
    "праздник": ("праздник", "праздники", "празник", "holiday", "holidays"),
}

# Only these can be fuzzy-repaired away from the first words.  They are target/
# domain markers whose accidental correction is low-risk and materially helps routing.
STRICT_DOMAIN_TERMS = {
    "файл", "папка", "проводник", "окно", "вкладка", "браузер", "дискорд", "яндекс",
    "ютуб", "музыка", "видео", "трек", "песня", "плейлист", "сервер", "канал",
    "микрофон", "громкость", "блютуз", "вайфай", "яркость", "экран", "монитор",
    "скриншот", "буфер", "процесс", "настройки", "чеклист", "панель", "заметка",
    "игра", "сессия", "устройство", "диск", "архив", "сеть", "память",
    "приложение", "документ", "корзина", "сайт", "ссылка", "интернет", "сообщение", "почта",
    "погода", "температура", "прогноз", "дождь", "ветер", "влажность", "осадки", "восход", "закат",
    "стим", "друг", "друзья", "достижения", "календарь", "событие", "встреча",
    "непрочитанные", "письмо", "таймер", "напоминание", "будильник", "процессор", "видеокарта",
    "оперативная", "сводка", "праздник",
}

_RU_LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _norm(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", str(value or "").casefold().replace("ё", "е"))


def _translit_ru(value: str) -> str:
    out: list[str] = []
    for ch in str(value or "").casefold():
        if "а" <= ch <= "я" or ch == "ё":
            out.append(ch.translate(_RU_LAT))
        elif ch.isalnum():
            out.append(ch)
    return "".join(out)


def _generated_word_variants(value: str) -> set[str]:
    """Small, collision-filtered ASR mutation family for one lexical form."""
    base = _norm(value)
    if not base:
        return set()
    out = {base}
    translit = _translit_ru(value)
    if translit and translit != base:
        out.add(_norm(translit))
        # Whisper sometimes renders Russian sh/ch/zh in Polish-like spelling.
        out.add(_norm(translit.replace("sh", "sz").replace("ch", "cz").replace("zh", "z")))
    # One missing character is a very common short-command error. Avoid generating
    # it for tiny words where collisions would be too frequent.
    if len(base) >= 5:
        for i in range(1, len(base) - 1):
            out.add(base[:i] + base[i + 1 :])
    # Adjacent duplicate collapse/expansion covers stop/стопп and similar endings.
    for i in range(1, len(base)):
        if base[i] == base[i - 1]:
            out.add(base[:i] + base[i + 1 :])
    if len(base) >= 4:
        out.add(base + base[-1])
    return {x for x in out if len(x) >= 2}


@lru_cache(maxsize=1)
def _indexes() -> tuple[dict[str, str], dict[str, tuple[str, ...]], tuple[str, ...]]:
    owners: dict[str, set[str]] = {}
    explicit_owners: dict[str, set[str]] = {}
    canonical_aliases: dict[str, set[str]] = {}
    for canonical, aliases in COMMAND_VARIATION_GROUPS.items():
        bucket = canonical_aliases.setdefault(canonical, set())
        for raw in (canonical, *aliases):
            raw_key = _norm(raw)
            if raw_key:
                explicit_owners.setdefault(raw_key, set()).add(canonical)
            for alias in _generated_word_variants(raw):
                bucket.add(alias)
                owners.setdefault(alias, set()).add(canonical)
    # Generated one-letter variants are accepted only when unambiguous. Explicit
    # hand-observed aliases get a separate ownership map so a generated mutation
    # from another command cannot accidentally suppress a real field alias such
    # as «вклучи».
    exact: dict[str, str] = {
        alias: next(iter(canonicals))
        for alias, canonicals in owners.items()
        if len(canonicals) == 1
    }
    for canonical, aliases in COMMAND_VARIATION_GROUPS.items():
        exact[_norm(canonical)] = canonical
        for raw in aliases:
            key = _norm(raw)
            if key and len(explicit_owners.get(key, {canonical})) == 1:
                exact[key] = canonical
    fuzzy = {k: tuple(sorted(v)) for k, v in canonical_aliases.items()}
    command_starts = tuple(
        c for c in COMMAND_VARIATION_GROUPS
        if c not in STRICT_DOMAIN_TERMS
    )
    return exact, fuzzy, command_starts


def command_vocabulary_score(text: str) -> float:
    """Return 0..1 lexical evidence that a transcript contains a JARVIS command."""
    exact, _fuzzy, _starts = _indexes()
    tokens = [_norm(m.group(0)) for m in _TOKEN_RE.finditer(str(text or ""))]
    tokens = [x for x in tokens if x]
    if not tokens:
        return 0.0
    hits = sum(1 for token in tokens[:8] if token in exact)
    first_bonus = 1.25 if tokens[0] in exact and exact[tokens[0]] not in STRICT_DOMAIN_TERMS else 0.0
    return min(1.0, (hits + first_bonus) / max(2.25, min(len(tokens), 6)))


def _best_fuzzy(token: str, *, first: bool, domain_only: bool) -> str | None:
    key = _norm(token)
    if len(key) < 3:
        return None
    _exact, fuzzy, command_starts = _indexes()
    canonicals = STRICT_DOMAIN_TERMS if domain_only else command_starts
    # Short command words need stricter scoring; long acoustic slips are safer.
    threshold = 0.76 if first and len(key) >= 6 else (0.80 if first else 0.88)
    best_name = ""
    best_score = threshold
    second = 0.0
    for canonical in canonicals:
        for alias in fuzzy.get(canonical, ()):
            if abs(len(alias) - len(key)) > max(2, len(key) // 3):
                continue
            score = difflib.SequenceMatcher(None, key, alias).ratio()
            if score > best_score:
                second = best_score
                best_name, best_score = canonical, score
            elif score > second:
                second = score
    if not best_name:
        return None
    # Ambiguous fuzzy guesses are worse than leaving the payload untouched.
    if best_score - second < (0.055 if first else 0.075):
        return None
    return best_name


def repair_command_vocabulary(text: str) -> str:
    """Repair command vocabulary while preserving arbitrary user entities.

    The first word receives the strongest repair because it is normally the action.
    Words 2..6 are repaired only when they are exact generated aliases or very close
    to a known domain term. Later words are exact-only. This makes phrases such as
    ``вклучи Rammstein Sonne в детскорде`` route correctly without touching
    ``Rammstein Sonne``.
    """
    original = str(text or "")
    spans = list(_TOKEN_RE.finditer(original))
    if not spans:
        return original
    exact, _fuzzy, _starts = _indexes()
    replacements: dict[int, str] = {}

    first_key = _norm(spans[0].group(0))
    # Never fuzzy-rewrite Russian question pronouns into action verbs.  This is
    # a second line of defence after the ASR hotword balancing above.
    if first_key in _PROTECTED_INTERROGATIVES:
        first_canonical = None
        guessed = None
        command_like = False
    else:
        first_canonical = exact.get(first_key)
        guessed = None
    if first_key in _PROTECTED_INTERROGATIVES:
        command_like = False
    elif first_canonical and first_canonical not in STRICT_DOMAIN_TERMS:
        replacements[0] = first_canonical
        command_like = True
    else:
        guessed = _best_fuzzy(spans[0].group(0), first=True, domain_only=False)
        command_like = guessed is not None
        if guessed:
            replacements[0] = guessed

    # If the first token is not a command, only repair unmistakable service/domain
    # aliases. This keeps ordinary friend-style conversation natural.
    for index, match in enumerate(spans[1:], start=1):
        key = _norm(match.group(0))
        canonical = exact.get(key)
        if canonical in STRICT_DOMAIN_TERMS:
            replacements[index] = canonical
            continue
        if command_like and index <= 5:
            guessed = _best_fuzzy(match.group(0), first=False, domain_only=True)
            if guessed:
                replacements[index] = guessed

    if not replacements:
        return original
    pieces: list[str] = []
    cursor = 0
    for index, match in enumerate(spans):
        pieces.append(original[cursor:match.start()])
        repl = replacements.get(index)
        if repl:
            # Keep an initial capital when Whisper emitted one; everything else is
            # normalized to the router-friendly canonical form.
            if match.group(0)[:1].isupper() and index == 0:
                repl = repl[:1].upper() + repl[1:]
            pieces.append(repl)
        else:
            pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(original[cursor:])
    return "".join(pieces)



def naturalize_command_language(text: str) -> str:
    """Translate only known *command vocabulary* from Latin ASR output to Russian.

    This is intentionally not a general translator: user payloads such as song names,
    file names and game titles remain byte-for-byte unchanged. It exists so acoustic
    decodes like ``stop music`` are shown/routed as ``стоп музыка`` instead of looking
    like the assistant misunderstood Russian speech.
    """
    return repair_command_vocabulary(text)

def priority_hotwords(limit: int = 90) -> list[str]:
    """Compact high-value vocabulary for Whisper hinting.

    The full generated corpus is intentionally *not* placed into Whisper hotwords:
    its decoder has a finite prompt context. Runtime fuzzy repair handles the long
    tail; these canonical terms only bias the acoustic decode toward action words.
    """
    preferred = [
        "открой", "закрой", "запусти", "включи", "выключи", "поставь", "найди",
        "создай", "скопируй", "перемести", "перенеси", "переименуй", "удали",
        "прочитай", "запиши", "напиши", "добавь", "вставь", "замени", "очисти",
        "покажи", "скрой", "сверни", "разверни", "переключи", "перейди", "нажми",
        "приостанови", "останови", "продолжи", "возобнови", "пропусти", "дальше",
        "скип", "назад", "предыдущий", "перемотай", "перемешай", "лайк", "дизлайк",
        "дискорд", "яндекс", "ютуб", "музыка", "видео", "трек", "песня",
        "плейлист", "браузер", "вкладка", "окно", "файл", "папка", "проводник",
        "экран", "монитор", "громкость", "микрофон", "чеклист", "панель",
        "настройки", "блютуз", "вайфай", "яркость", "скриншот", "буфер",
    ]
    return preferred[: max(1, int(limit))]
