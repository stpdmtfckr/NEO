# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jarvis.integrations.local_translation import looks_like_translation_request


@dataclass(frozen=True)
class IntegrationIntent:
    intent: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    confidence: float = 0.99


def _n(text: str) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    value = re.sub(r"[^a-zà-öø-ÿа-я0-9@._+\-:]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _has(t: str, *parts: str) -> bool:
    return any(x in t for x in parts)

def _briefing_request(t: str, code: str | None = None) -> bool:
    # Match the command, not arbitrary filenames or text payloads containing
    # "daily briefing". Russian accusatives were missing from the old atlas.
    patterns = (
        r'(?:пожалуйста )?(?:(?:покажи|дай|расскажи|прочитай|составь|подготовь|выведи|сделай|можешь показать)(?: мне)? )?(?:сводк[ау] (?:дня|на сегодня|на день)|(?:утренняя|утреннюю|дневная|дневную|ежедневная|ежедневную) сводк[ау])(?: пожалуйста)?',
        r'(?:please )?(?:(?:show|give|read|prepare)(?: me)? )?(?:the |my )?(?:daily|morning) briefing(?: please)?',
        r'(?:bitte )?(?:(?:zeige|lies|gib)(?: mir)? )?(?:die |meine )?(?:tagesübersicht|tageszusammenfassung)(?: bitte)?',
        r'(?:(?:montre|donne|lis)(?: moi)? )?(?:le |mon )?résumé du jour(?: s il te plaît)?',
        r'(?:por favor )?(?:(?:muestra|muéstrame|dame|lee) )?(?:el |mi )?resumen del día(?: por favor)?',
        r'(?:per favore )?(?:(?:mostra|mostrami|dammi|leggi) )?(?:il |mio )?riepilogo del giorno(?: per favore)?',
        r'(?:por favor )?(?:(?:mostra|mostre|dá me|leia) )?(?:o |meu )?resumo do dia(?: por favor)?',
    )
    languages = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
    selected = (patterns[languages.index(code)],) if code in languages else patterns
    return any(re.fullmatch(pattern, t) for pattern in selected)

def _weather_signal(t: str) -> bool:
    """Return True only for a real weather request, never lexical collisions.

    Weather used to be an eager top-level domain and accepted short substring
    matches (notably ``ветер`` inside ``ветеран``). Temperature is also ambiguous:
    it may describe CPU/GPU/game mechanics. Treat it as weather only in an
    explicitly atmospheric construction or a compact standalone weather question.
    """
    strong_patterns = (
        r"\bпогод\w*\b", r"\bпрогноз\w*\b", r"\bдожд\w*\b",
        r"\b(?:ветер|ветра|ветру|ветром|ветре|ветры|ветров|ветрам|ветрами|ветрах|ветрен\w*)\b",
        r"\bвлажност\w*\b", r"\bосад\w*\b", r"\bвосход\w*\b", r"\bзакат\w*\b", r"\bзонт\w*\b",
        r"\bweather\b", r"\bforecast\w*\b", r"\brain(?:y|fall)?\b",
        r"\bwind(?:y|speed)?\b", r"\bhumidity\b", r"\bprecipitation\w*\b", r"\bsunrise\b", r"\bsunset\b", r"\bumbrella\b",
        r"\bwetter\b", r"\bm[ée]t[ée]o\b", r"\bclima\b", r"\blluvia\b", r"\bpioggia\b", r"\bchuva\b",
    )
    if any(re.search(pattern, t, re.I) for pattern in strong_patterns):
        return True

    # Temperature gets a dedicated ambiguity guard.  Compact requests such as
    # “какая температура сейчас?” / “temperature today?” remain weather, while
    # “температура процессора” and “урон при температуре перегрева” do not.
    if re.search(r"\b(?:температур\w*|temperature\w*|temperatur\w*|température\w*|temperatura\w*)\b", t, re.I):
        atmospheric = re.search(
            r"\b(?:на\s+улиц\w*|за\s+окн\w*|воздух\w*|градус\w*|сейчас|сегодня|завтра|утром|вечером|ночью|"
            r"outside|outdoors|air|degrees?|now|today|tomorrow|tonight|morning|evening|"
            r"draußen|heute|morgen|dehors|aujourd'hui|demain|hoy|mañana|oggi|domani|hoje|amanhã)\b",
            t, re.I,
        )
        technical = re.search(
            r"\b(?:cpu|gpu|процессор\w*|видеокарт\w*|желез\w*|ядр\w*|диск\w*|"
            r"урон\w*|оруж\w*|пушк\w*|ствол\w*|перегрев\w*|плазм\w*|класс\w*|враг\w*|мисси\w*|предмет\w*|"
            r"damage|weapon|gun|overheat|plasma|class|enemy|mission|item)\b", t, re.I,
        )
        compact = bool(re.fullmatch(
            r"(?:какая|какой|сколько)?\s*(?:сейчас\s*)?(?:температур\w*|temperature|temperatur|température|temperatura)\s*[?.!]*",
            t, re.I,
        ))
        return bool((atmospheric or compact) and not technical)
    return False


_WEATHER_STOP = {
    "сегодня", "завтра", "послезавтра", "утром", "утро", "днем", "днём", "вечером", "вечер", "ночью", "ночь",
    "сейчас", "будет", "была", "погода", "погоду", "погоде", "температура", "температуру", "прогноз", "дождь", "ветер",
    "today", "tomorrow", "tonight", "morning", "afternoon", "evening", "weather", "forecast", "temperature", "rain", "wind",
    # NEO 175: never a city («погода на завтра» gave the city «на»)
    "неделю", "неделе", "неделя", "выходные", "выходных", "этой", "эту", "следующей", "следующую", "пожалуйста", "там",
    "week", "weekend", "next", "this", "the", "please", "now", "here",
}
# NEO 175: a word after «погода» that starts with a preposition is a time or a phrase, not a city.
_NOT_A_CITY_START = {"на", "по", "к", "ко", "с", "со", "у", "о", "об", "за", "до", "от", "при", "через", "и", "а", "мне",
                     "on", "for", "at", "to", "and", "me", "this", "next", "the",
                     # NEO 182: verbs of «que tempo faz / quel temps fait-il / qué tiempo hace» are never cities
                     "faz", "fará", "fara", "fait", "fera", "hace", "hará", "hara", "fa", "farà", "macht", "ist", "is",
                     "está", "esta", "estará", "será", "sera", "è", "est", "wird", "il", "it", "es"}


def _clean_weather_city(value: str) -> str:
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’.-]+", str(value or ""))
    if words and words[0].casefold() in _NOT_A_CITY_START:
        return ""
    kept: list[str] = []
    for word in words[:5]:
        if word.casefold().replace("ё", "е") in _WEATHER_STOP or (kept and word.casefold() in _NOT_A_CITY_START):
            break
        kept.append(word.strip(" .,-"))
    return " ".join(x for x in kept if x).strip()


def extract_weather_location(text: str) -> tuple[str, str]:
    """Extract an explicitly requested city without replacing it by the default.

    Natural weather questions are allowed to be noisy; the extractor intentionally
    prefers an explicit prepositional location and otherwise leaves city empty so
    OpenWeather can use the configured default.
    """
    raw = str(text or "").strip()
    country = ""
    # Strong quoted city names are useful for multi-word cities.
    quoted = re.findall(r"[«\"']([^»\"']{2,80})[»\"']", raw)
    if quoted:
        city = _clean_weather_city(quoted[-1])
        if city:
            return city, country
    patterns = [
        r"(?:погод\w*|температур\w*|прогноз\w*|дожд\w*|(?:ветер|ветра|ветру|ветром|ветре|ветры|ветров|ветрам|ветрами|ветрах|ветрен\w*)|влажност\w*)[^,.!?]{0,45}?\b(?:в|во|для)\s+([A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’.-]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’.-]+){0,3})",
        r"\b(?:в|во)\s+([A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’.-]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’.-]+){0,3})[^,.!?]{0,35}?(?:погод\w*|температур\w*|прогноз\w*|дожд\w*|(?:ветер|ветра|ветру|ветром|ветре|ветры|ветров|ветрам|ветрами|ветрах|ветрен\w*))",
        r"(?:weather|forecast|temperature|rain|wind|humidity)[^,.!?]{0,35}?\b(?:in|for|at)\s+([A-Za-zÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ'’.-]+){0,3})",
    ]
    for pattern in patterns:
        m = re.search(pattern, raw, re.I)
        if m:
            city = _clean_weather_city(m.group(1))
            if city:
                return city, country
    # Compact forms: "погода Москва завтра" / "weather Berlin tomorrow".
    m = re.search(r"\b(?:погод\w*|weather|forecast)\s+(?:в\s+)?([A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’.-]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’.-]+){0,2})", raw, re.I)
    if m:
        city = _clean_weather_city(m.group(1))
        if city and city.casefold().replace("ё", "е") not in _WEATHER_STOP:
            return city, country
    return "", country


# NEO 188 atlas (NEG-*-05): «ну, я думаю, сегодня дождь» is talk about the weather, not a weather request.
_OPINION188 = re.compile(
    r"^(?:(?:ну|well|nun|naja|eh\s+bien|bon|bueno|beh|bem|bom)[,\s]+)?"
    r"(?:я\s+)?(?:думаю|считаю|полагаю|кажется|по-моему|наверное|i\s+(?:think|guess|believe|suppose|reckon)|"
    r"ich\s+(?:glaube|denke|vermute|schätze)|je\s+(?:pense|crois|suppose)|creo|pienso|supongo|credo|penso|suppongo|"
    r"acho|creio|suponho)\b", re.I)
_QUESTION188 = re.compile(
    r"[?¿]|\b(?:ли|какая|какой|какие|будет\s+ли|what|will\s+it|is\s+it|wird\s+es|wie\s+wird|est-ce|va-t-il|"
    r"qual|quale|cuál|quel)\b", re.I)


def _opinion_statement188(text: str) -> bool:
    value = " ".join(str(text or "").split()).strip()
    return bool(_OPINION188.match(value)) and not _QUESTION188.search(value)


def parse_integration_phrase(text: str) -> IntegrationIntent | None:
    """High-confidence API/productivity grammar.

    The multilingual layer rewrites supported languages into a canonical Russian
    command surface before this function runs. English spellings are also accepted
    directly so the parser remains useful in isolated tests and typed commands.
    """
    t = _n(text)
    if not t:
        return None

    # --- Natural text translation ---
    # Translation has high priority because its payload may itself contain
    # words that look like another domain, and Whisper can render «переведи» as
    # «перейди». The dedicated detector accepts that ASR error only inside an
    # unmistakable translation construction.
    translation_domain = looks_like_translation_request(text)
    # HOTFIX82: any reference to a screen/monitor/screenshot belongs to vision
    # translation. The old narrow exclusion only caught exact «на экране», so
    # «переведи текст С экрана» was incorrectly sent to text translation and the
    # assistant translated the command itself.
    visual_translation = bool(re.search(r"\b(?:экран|монитор|скриншот)\w*\b|\b(?:screen|monitor|screenshot)\b", t, re.I))
    if translation_domain and not visual_translation:
        return IntegrationIntent("translate_text", "translate", {"text": text}, 995, 0.9995)

    # --- Timers / reminders / alarms ---
    # Domain ownership requires timer semantics, not merely a token named
    # "timer". This matters when game-specific contexts introduce terms such as
    # "Timer Knight" or "Alarm Blade". The integration layer should claim
    # those words only when the utterance also looks like timer management.
    timer_token = bool(re.search(
        r"\b(?:таймер(?:а|е|у|ом|ы|ов|ах|ами)?|timer|timers|"
        r"будильник(?:а|е|у|ом|и|ов)?|alarm|alarms)\b", t, re.I
    ))
    reminder_token = bool(re.search(
        r"\b(?:напомни(?:ть|те)?|напомин(?:ание|ания|аний|анию|анием|ать|и)?|remind|reminder|reminders)\b",
        t, re.I,
    ))
    timer_create_cue = bool(re.search(
        r"\b(?:постав\w*|запуст\w*|созда\w*|включи\w*|set|start|create|begin)\b", t, re.I
    ))
    timer_cancel_cue = bool(re.search(
        r"\b(?:отмен\w*|удал\w*|выключ\w*|останов\w*|cancel|remove|delete|stop)\b", t, re.I
    ))
    timer_list_cue = bool(re.search(
        r"\b(?:список|какие|сколько\s+остал\w*|покажи\s+(?:мои\s+)?таймер\w*|list|remaining)\b", t, re.I
    ))
    # Compact natural forms like "таймер на пять минут" / "timer for 5 min"
    # have no explicit create verb but do carry a temporal quantity.
    duration_cue = bool(re.search(
        r"\b(?:на|через|for|in)\s+(?:(?:\d+(?:[.,]\d+)?)|(?:один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|"
        r"one|two|three|four|five|six|seven|eight|nine|ten))\s*(?:секунд\w*|минут\w*|час\w*|second\w*|minute\w*|hour\w*)\b",
        t, re.I,
    ))
    timer_domain = reminder_token or (timer_token and (timer_create_cue or timer_cancel_cue or timer_list_cue or duration_cue))
    if timer_domain:
        if timer_cancel_cue:
            return IntegrationIntent("timer_cancel", "timer", {"action": "cancel", "text": text}, 972, 0.998)
        if timer_list_cue and not timer_create_cue and not reminder_token:
            return IntegrationIntent("timer_list", "timer", {"action": "list"}, 970, 0.995)
        action = "reminder" if reminder_token else ("alarm" if re.search(r"\b(?:будильник\w*|alarm|alarms)\b", t, re.I) else "timer")
        return IntegrationIntent("timer_create", "timer", {"action": action, "text": text}, 972, 0.998)


    # --- Weather / OpenWeather ---
    weather_domain = _weather_signal(t) and not _opinion_statement188(text)
    if weather_domain:
        action = "summary"
        if _has(t, "дожд", "rain", "regen", "pluie", "pleuvoir", "lluvia", "llover", "pioggia", "piovere", "chuva", "chover", "осад", "precipitation", "niederschlag", "précipitation", "precipitación", "precipitazioni", "precipitação", "зонт", "umbrella", "regenschirm", "parapluie", "paraguas", "ombrello", "guarda-chuva", "снег", "snow"):
            action = "rain"
        elif _has(t, "влажност", "humidity", "luftfeuchtigkeit", "humidité", "humedad", "umidità", "umidade"):
            action = "humidity"
        elif re.search(r"\b(?:ветер|ветра|ветру|ветром|ветре|ветры|ветров|ветрам|ветрами|ветрах|ветрен\w*|wind|vent|viento|vento)\b", t, re.I):
            action = "wind"
        elif _has(t, "восход", "sunrise", "sonnenaufgang", "lever du soleil", "amanecer", "alba", "nascer do sol"):
            action = "sunrise"
        elif _has(t, "закат", "sunset", "sonnenuntergang", "coucher du soleil", "atardecer", "tramonto", "pôr do sol", "por do sol"):
            action = "sunset"
        elif _has(t, "температур", "temperature", "temperatur", "température", "temperatura", "сколько градусов", "how many degrees"):
            action = "temperature"
        elif _has(t, "завтра", "вечер", "утр", "ноч", "днем", "днём", "tomorrow", "evening", "morning", "tonight", "afternoon", "morgen", "demain", "mañana", "domani", "amanhã", "forecast", "прогноз", "vorhersage", "prévision", "prevision", "pronóstico", "pronostico", "previsioni", "previsão", "previsao"):
            action = "forecast"
        city, country = extract_weather_location(text)
        return IntegrationIntent("weather_" + action, "weather", {"action": action, "period": text, "city": city, "country": country}, 975, 0.998)

    # --- Offline date arithmetic / holidays ---
    date_question = _has(t, "день недели", "какой день", "какая дата", "следующ пятниц", "через сколько дней", "новый год", "праздник", "какой сегодня праздник", "day of week", "what date", "next friday", "how many days", "new year", "holiday")
    if date_question:
        return IntegrationIntent("date_info", "date_info", {"action": "date_info", "text": text}, 978, 0.999)

    # --- PC resource monitoring ---
    summary_phrase = (
        _has(t, "состояние компьют", "статус компьют", "информация по компьют", "информация о компьют",
             "информация моего компьют", "информация про компьют", "сводка по компьют", "все по компьют",
             "ресурсы компьют", "ресурсы системы", "состояние системы", "статус системы", "информация о системе",
             "информация по системе", "полная диагностика", "диагностика компьют", "computer status", "system status",
             "computer overview", "system overview", "computer health", "system health")
        or bool(re.search(r"(?:как\s+(?:чувству|пожива)\w*|что\s+с)\s+(?:себя\s+)?(?:(?:мой|моя|мое|моё|моим|моего|моему)\s+)?компьют", t, re.I))
        or bool(re.search(r"(?:состояни|статус|здоровь|ресурс)\w*\s+(?:(?:моего|моему|моем|моём|моим|мой)\s+)?(?:компьют|систем)", t, re.I))
        or bool(re.search(r"(?:информац|сведени|данн|сводк)\w*\s+(?:(?:по|о|об|про)\s+)?(?:(?:моего|моему|моем|моём|моим|мой)\s+)?(?:компьют|систем)", t, re.I))
        or bool(re.search(r"(?:покажи|дай|выведи|расскажи)\w*[^.!?]{0,35}(?:всю|полную)?[^.!?]{0,20}(?:информац|сведени|сводк|данн|диагност)[^.!?]{0,35}(?:компьют|систем)", t, re.I))
    )
    # Resource abbreviations are lexical tokens, never arbitrary substrings.
    # Substring matching made generated/game names such as "Frame" look like
    # RAM and allowed the external-domain arbiter to steal valid game queries.
    has_cpu=bool(re.search(r"\b(?:процессор\w*|cpu)\b", t, re.I))
    has_gpu=bool(re.search(r"\b(?:видеокарт\w*|gpu|vram)\b", t, re.I))
    has_mem=bool(re.search(r"\b(?:оператив\w*|памят\w*|ram|memory)\b", t, re.I))
    has_disk=bool(re.search(r"\bдиск(?:и|а|е|ов|у|ом|ами|ах)?\b",t,re.I)) or _has(t,"storage","drive","свободн мест")
    has_net=_has(t,"интернет","сеть","network","скорост интернета","скорость сети","download","upload")
    monitor_domain=summary_phrase or has_cpu or has_gpu or has_mem or has_disk or has_net or _has(t,"температур компьют","ресурсы компьют")
    if monitor_domain:
        count=sum(bool(x) for x in (has_cpu,has_gpu,has_mem,has_disk,has_net))
        if summary_phrase or count>1:
            action="summary"
        elif has_gpu: action="gpu"
        elif has_cpu: action="cpu"
        elif has_mem: action="memory"
        elif has_disk: action="disks"
        elif has_net: action="network"
        else: action="summary"
        return IntegrationIntent("system_monitor_"+action,"system_monitor",{"action":action},982,0.999)

    # --- Morning briefing on request ---
    if _briefing_request(t) or _has(t, "утренняя сводка", "сводка на день", "morning briefing", "daily briefing", "что сегодня важного"):
        return IntegrationIntent("morning_briefing", "briefing", {"action": "summary"}, 965, 0.995)


    return None
