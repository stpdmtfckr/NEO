"""NEO 171: talk locally or look it up; short, focused web answers that end with an offer.

* ``classify`` tells conversation (opinions, jokes, questions to NEO itself) from requests
  that need current or exact outside facts (specifications, prices, reviews, news, people,
  release dates, figures). Conversation stays with the local model; facts go to SearchAPI.io.
* ``structure`` turns the provider's long answer into a short lead plus the key points that
  match the question. It only selects and trims the provider's own sentences: nothing is
  rephrased or invented, and «подробнее» shows the full answer.
* ``offers`` ends the answer with one or two next searches that fit the question (reviews,
  the same model of another year, price...). The next short reply — «да», «отзывы»,
  «второе», «2018» — runs that search (``resolve``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import threading
import time
from typing import Any

_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
PENDING_TTL = 180.0


def _n(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').casefold().replace('ё', 'е')).strip(' .,!?:;¿¡—-\t\r\n«»"\'')


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


# ---- conversation or web ---------------------------------------------------------------

_B = r'(?<![\w-])'
_E = r'(?![\w-])'

# Questions to NEO itself, opinions, jokes, feelings, creative requests: the local model.
_CHAT = re.compile(
    _B + r'(?:'
    r'как\s+(?:у\s+тебя\s+)?дела|кто\s+ты|как\s+тебя\s+зовут|что\s+ты\s+(?:умеешь|можешь|любишь|думаешь|считаешь|чувствуешь)|'
    r'(?:ты|тебе|тебя)\s+(?:нравится|нравятся|грустно|скучно|любишь|думаешь|считаешь)|как\s+(?:ты\s+)?(?:думаешь|считаешь)|по-?твоему|'
    r'твое\s+мнение|твои\s+мысли|давай\s+(?:поболтаем|поговорим)|поболтаем|анекдот\w*|пошути\w*|шутк\w*|'
    r'сочини\w*|придумай\w*|напиши\s+(?:стих|рассказ|сказку|поздравлени)\w*|стих\w*\s+про|'
    r'мне\s+(?:грустно|скучно|плохо|одиноко|хорошо|весело|страшно)|я\s+(?:устал\w*|грущу|скучаю|рад\w*)|'
    r'how\s+are\s+you|who\s+are\s+you|what(?:\s+do)?\s+you\s+(?:think|feel|like|can\s+do)|your\s+(?:opinion|name)|'
    r'tell\s+(?:me\s+)?a\s+(?:joke|story)|a\s+joke|make\s+up|write\s+(?:me\s+)?a\s+(?:poem|story)|i\s*(?:\'m|\s+am)\s+(?:bored|sad|tired)|'
    r'wie\s+geht\s*(?:es|\'s)?\s*dir|wer\s+bist\s+du|was\s+(?:denkst|meinst|h[aä]ltst)\s+du|deine\s+meinung|witz\w*|'
    r'comment\s+(?:vas-tu|ça\s+va|tu\s+vas)|qui\s+es-tu|qu[\'’]en\s+penses-tu|tu\s+penses|ton\s+avis|blague\w*|'
    r'c[oó]mo\s+est[aá]s|qui[eé]n\s+eres|qu[eé]\s+(?:piensas|opinas)|tu\s+opini[oó]n|chiste\w*|'
    r'come\s+stai|chi\s+sei|cosa\s+(?:ne\s+)?pensi|la\s+tua\s+opinione|barzellett\w*|'
    r'como\s+(?:est[aá]s|vais)|quem\s+[eé]s|o\s+que\s+(?:achas|pensas)|a\s+tua\s+opini[aã]o|piada\w*'
    r')' + _E, re.I)

_YOU = re.compile(_B + r'(?:ты|тебе|тебя|тобой|твой|твоя|твое|твои|you|your|du|dich|dir|dein\w*|toi|t[\'’]|tu|ton|tes|t[uú]|ti|tuo|tua|voc[eê]|teu)' + _E, re.I)

# What the question is about: each facet also picks the follow-up offers.
_FACETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('specs', re.compile(_B + r'(?:характеристик\w*|техническ\w+\s+(?:данные|характеристик\w*)|мощност\w*|расход\w*|разгон\w*|'
                         r'двигател\w*|комплектаци\w*|габарит\w*|технически\w*\s+параметр\w*|specs?|specifications?|horsepower|fuel\s+(?:consumption|economy)|'
                         r'technische\s+daten|leistung|verbrauch|caract[eé]ristiques|fiche\s+technique|consommation|'
                         r'caracter[ií]sticas|especificaciones|ficha\s+t[eé]cnica|consumo|caratteristiche|specifiche|scheda\s+tecnica|'
                         r'especifica[cç][oõ]es|ficha\s+t[eé]cnica)' + _E, re.I)),
    ('reviews', re.compile(_B + r'(?:отзыв\w*|обзор\w*|мнени\w+\s+владельц\w*|reviews?|owners?\s+opinions?|bewertung\w*|erfahrung\w*|testbericht\w*|'
                           r'avis|rese[nñ]as?|opiniones|recensioni|opinioni|avalia[cç][oõ]es|opini[oõ]es)' + _E, re.I)),
    ('price', re.compile(_B + r'(?:цен[аыуе]|ценник\w*|сколько\s+(?:стоит|стоят)|стоимост\w*|почем|price\w*|cost\w*|how\s+much|preis\w*|kostet|prix|co[uû]te|precio\w*|cuesta|'
                         r'prezz\w*|costa|pre[cç]o\w*|custa)' + _E, re.I)),
    ('news', re.compile(_B + r'(?:новост\w*|последн\w+\s+(?:событи|новост)\w*|что\s+нового|news|latest|nachrichten|neuigkeiten|actualit[eé]s?|'
                        r'noticias|notizie|not[ií]cias)' + _E, re.I)),
    ('release', re.compile(_B + r'(?:когда\s+(?:выйдет|вышел|вышла|выходит)|дат\w+\s+выхода|релиз\w*|release\s+date|when\s+(?:does|did|will)\s+\w+\s+(?:come\s+out|release)|'
                           r'erscheinungsdatum|date\s+de\s+sortie|fecha\s+de\s+lanzamiento|data\s+di\s+uscita|data\s+de\s+lan[cç]amento)' + _E, re.I)),
    ('person', re.compile(_B + r'(?:кто\s+(?:такой|такая|такие)|who\s+is|wer\s+ist|qui\s+est|qui[eé]n\s+es|chi\s+[eè]|quem\s+[eé])' + _E, re.I)),
    # Only topics that are current by nature: «сегодня» alone («на сегодня хватит») is not a search.
    ('current', re.compile(_B + r'(?:погод\w*|курс\w*\s+(?:доллар|евро|валют|рубл|биткоин)\w*|расписани\w*|сч[её]т\s+матча|результат\w*\s+матча|'
                           r'weather|exchange\s+rate|match\s+score|wetter|wechselkurs|m[eé]t[eé]o|taux\s+de\s+change|clima|tiempo\s+en|tipo\s+de\s+cambio|'
                           r'meteo|tasso\s+di\s+cambio|tempo\s+em|previs[aã]o\s+do\s+tempo|c[aâ]mbio)' + _E, re.I)),
    # NEO 172: «как установить драйвер», «как пройти 5 уровень» — the answer is a list of steps.
    ('howto', re.compile(_B + r'(?:как\s+(?:мне\s+)?(?:установить|переустановить|настроить|исправить|починить|сделать|включить|отключить|выключить|удалить|'
                         r'обновить|пройти|победить|убрать|поменять|изменить|подключить|перенести|очистить|ускорить|восстановить|скачать|создать|'
                         r'сбросить|откатить|вернуть|добавить|записать|сохранить|активировать|разблокировать|получить)|'
                         r'how\s+(?:do\s+i|to|can\s+i)\s+(?:install|reinstall|set\s+up|setup|configure|fix|repair|make|enable|disable|turn\s+(?:on|off)|remove|'
                         r'uninstall|update|beat|pass|get|change|connect|move|clean|speed\s+up|restore|download|create|reset|add|record|save|activate|unlock)|'
                         r'wie\s+(?:kann\s+ich\s+)?(?:installiere|installieren|richte|einrichten|behebe|beheben|repariere|aktiviere|deaktiviere|entferne|'
                         r'aktualisiere|schaffe|verbinde|lösche|stelle|erstelle|setze)|'
                         r'comment\s+(?:je\s+)?(?:installer|installe|configurer|configure|corriger|corrige|réparer|activer|active|désactiver|supprimer|'
                         r'mettre\s+à\s+jour|réussir|connecter|créer|réinitialiser|récupérer)|'
                         r'c[oó]mo\s+(?:puedo\s+)?(?:instalo|instalar|configuro|configurar|arreglo|arreglar|soluciono|solucionar|activo|activar|desactivo|'
                         r'desactivar|elimino|eliminar|actualizo|actualizar|paso|pasar|conecto|conectar|creo|crear|restablezco|restablecer)|'
                         r'come\s+(?:posso\s+)?(?:installo|installare|configuro|configurare|risolvo|risolvere|riparo|attivo|attivare|disattivo|elimino|'
                         r'aggiorno|supero|superare|collego|creo|ripristino)|'
                         r'como\s+(?:posso\s+)?(?:instalo|instalar|configuro|configurar|corrijo|corrigir|resolvo|resolver|ativo|ativar|desativo|desativar|'
                         r'apago|apagar|atualizo|atualizar|passo|passar|ligo|conecto|crio|criar|restauro|restaurar))' + _E, re.I)),
    ('figures', re.compile(_B + r'(?:население|столиц\w*|высот\w*|длин\w*|площад\w*|сколько\s+лет|год\s+основания|рост\s+у|population|capital\s+of|'
                           r'how\s+(?:tall|old|long|high|big)|einwohner|hauptstadt|habitants|capitale|poblaci[oó]n|abitanti|popula[cç][aã]o)' + _E, re.I)),
)

# «Look it up» verbs without «in the browser» (the browser has its own lane).
_FIND = re.compile(_B + r'(?:найди|найдите|поищи|узнай|посмотри|проверь|find|look\s+up|search\s+for|check|finde|such\w*|trouve|cherche|'
                   r'encuentra|busca|trova|cerca|encontra|procura)' + _E, re.I)
# Things on this computer: never the web.
_LOCAL = re.compile(_B + r'(?:(?:мо\w+|этого|этом|свое\w*)\s+(?:компьютер\w*|пк|ноутбук\w*|систем\w*|видеокарт\w*|процессор\w*)|'
                    r'файл\w*|папк\w*|документ\w*|на\s+(?:компьютере|диске|рабочем\s+столе)|в\s+проводник\w*|'
                    r'my\s+(?:computer|pc|laptop|system)|this\s+(?:computer|pc)|files?|folders?|meinem\s+(?:pc|computer)|'
                    r'mon\s+(?:ordinateur|pc)|mi\s+(?:ordenador|pc)|mio\s+(?:computer|pc)|meu\s+(?:computador|pc))' + _E, re.I)
_YEAR = re.compile(r'(?<!\d)((?:19[5-9]|20[0-4])\d)(?!\d)')
# A model code: letters and digits together (m5, f90, rtx4090, a6, s23) or a Latin name in Cyrillic text.
_MODEL = re.compile(r'(?<![\w-])(?:[a-zа-я]{1,6}-?\d{1,5}[a-zа-я]{0,3}|\d{1,4}[a-z]{1,3})(?![\w-])', re.I)
_LATIN_IN_CYRILLIC = re.compile(r'[a-z]{3,}', re.I)


def facets(text: str) -> list[str]:
    t = _n(text)
    return [name for name, pattern in _FACETS if pattern.search(t)]


# NEO 175: everyday small talk in the 7 languages: always the chat, never the web or an action.
SMALL_TALK = re.compile(
    r"^(?:what'?s\s+up|sup|wassup|how'?s\s+it\s+going|how\s+is\s+it\s+going|how\s+r\s+u|how\s+are\s+(?:you|u)(?:\s+doing)?|"
    r"you'?re\s+(?:awesome|great|the\s+best|cool|amazing|funny|smart)|you\s+are\s+(?:awesome|great|the\s+best|cool|amazing|funny|smart)|"
    r"(?:good|great|nice)\s+(?:job|work)|well\s+done|i\s+love\s+you|nice\s+to\s+meet\s+you|i'?m\s+(?:bored|tired|sad|fine|ok|okay|good|happy)|"
    r"как\s+жизнь|как\s+сам|как\s+поживаешь|что\s+делаешь|чем\s+занимаешься|чем\s+занят|ты\s+(?:лучший|лучшая|классный|молодец|умница|супер|крутой)|"
    r"молодец|умница|я\s+тебя\s+люблю|рад\s+(?:тебя\s+)?(?:слышать|видеть)|приятно\s+познакомиться|"
    r"wie\s+geht'?s|wie\s+geht\s+es\s+dir|was\s+machst\s+du|du\s+bist\s+(?:toll|super|der\s+beste)|"
    r"ça\s+va|ca\s+va|comment\s+ça\s+va|comment\s+vas-tu|tu\s+es\s+(?:génial|super)|"
    r"qu[eé]\s+tal|c[oó]mo\s+est[aá]s|qu[eé]\s+haces|eres\s+(?:genial|el\s+mejor)|"
    r"come\s+stai|come\s+va|che\s+fai|sei\s+(?:fantastico|il\s+migliore)|"
    r"tudo\s+bem|como\s+est[aá]s|como\s+vai|o\s+que\s+fazes|[eé]s\s+(?:o\s+melhor|incr[ií]vel))\b", re.I)


def classify(text: str) -> str:
    """'chat' (the local model), 'web' (SearchAPI.io) or 'either' (local first, the web if it can't)."""
    t = _n(text)
    if not t:
        return 'either'
    if SMALL_TALK.match(t) or SMALL_TALK.match(str(text or '').strip().casefold()):
        return 'chat'
    if _LOCAL.search(t):
        return 'either'
    found = facets(t)
    explicit_find = bool(_FIND.match(t))
    if _CHAT.search(t) and not (explicit_find and found):
        return 'chat'
    if found:
        return 'web'
    if _YOU.search(t) and not explicit_find:
        return 'chat'          # «сколько тебе лет», «а ты любишь музыку?»: a question to NEO itself
    cyrillic = bool(re.search(r'[а-я]', t))
    if explicit_find and (_YEAR.search(t) or _MODEL.search(t) or (cyrillic and _LATIN_IN_CYRILLIC.search(t))):
        return 'web'
    return 'either'


# ---- what the question is about -----------------------------------------------------------

_CAR = re.compile(_B + r'(?:машин\w*|авто\w*|автомобил\w*|car|auto|wagen|voiture|coche|macchina|carro|bmw|бмв|audi|ауди|mercedes|мерседес|'
                  r'toyota|тойот\w*|kia|киа|hyundai|хендай|хундай|lada|лад\w|honda|хонд\w|nissan|ниссан\w*|volkswagen|фольксваген\w*|vw|ford|форд\w*|'
                  r'tesla|тесл\w|skoda|шкод\w|mazda|мазд\w|porsche|порше|lexus|лексус\w*|chevrolet|шевроле|renault|рено|geely|джили|chery|чери|'
                  r'haval|хавейл|хавал|mitsubishi|митсубиси|subaru|субару|volvo|вольво|jeep|джип|land\s+rover|ленд\s+ровер)' + _E, re.I)
_SCAFFOLD = re.compile(
    _B + r'(?:пожалуйста|please|bitte|s[\'’]il\s+te\s+pla[iî]t|por\s+favor|per\s+favore|'
    r'найди|найдите|поищи|узнай|посмотри|проверь|покажи|расскажи|скажи|подскажи|мне|нам|'
    r'в\s+интернете|в\s+сети|в\s+google|в\s+гугле|через\s+google|информаци\w*|инфу|'
    r'какие|какой|какая|какое|каковы|какова|каков|сколько|что|кто|где|когда|у|про|об?|для|на|'
    r'find|look\s+up|search\s+for|check|tell\s+me|show\s+me|what|which|are|is|the|of|for|about|on|me|does|do|a|an|'
    r'finde|zeig|sag|was|welche|sind|ist|die|der|das|von|f[uü]r|über|wie\s+viel|ein|eine|einen|'
    r'trouve|cherche|dis-moi|quelles?|quels?|sont|est|les?|la|des?|du|pour|sur|combien|un|une|'
    r'encuentra|busca|dime|cu[aá]les|son|es|los|las|el|del|para|sobre|cu[aá]nto|un|una|'
    r'trova|cerca|dimmi|quali|sono|il|lo|gli|delle?|degli|della|per|su|quanto|uno?|una|'
    r'encontra|procura|diz-me|quais|s[aã]o|os|as|o|a|do|da|dos|das|em|quanto|um|uma)' + _E, re.I)
_FACET_WORDS = re.compile(
    _B + r'(?:технические|характеристик\w*|параметр\w*|отзыв\w*|обзор\w*|владельц\w*|цен\w*|стоимост\w*|стоит|новост\w*|последние|'
    r'машин\w*|автомобил\w*|модел(?:и|ью|ей)|года|год|г\.?|'
    r'specs?|specifications?|reviews?|owners?|price|cost|news|latest|car|year|'
    r'technische|daten|bewertung\w*|preis|nachrichten|jahr|caract[eé]ristiques|avis|prix|actualit[eé]s|voiture|ann[eé]e|'
    r'caracter[ií]sticas|especificaciones|rese[nñ]as|opiniones|precio|noticias|coche|a[nñ]o|'
    r'caratteristiche|specifiche|recensioni|opinioni|prezzo|notizie|macchina|anno|especifica[cç][oõ]es|avalia[cç][oõ]es|pre[cç]o|not[ií]cias|carro|ano)' + _E, re.I)


def subject(text: str) -> tuple[str, str]:
    """(the thing asked about, its year) — «найди характеристики bmw m5 2020 года» → ('bmw m5', '2020')."""
    raw = re.sub(r'\s+', ' ', str(text or '')).strip(' .,!?:;¿¡')
    year_match = _YEAR.search(raw)
    year = year_match.group(1) if year_match else ''
    rest = _YEAR.sub(' ', raw)
    for _name, pattern in _FACETS:
        rest = pattern.sub(' ', rest)
    rest = _FACET_WORDS.sub(' ', rest)
    for _ in range(3):
        rest = _SCAFFOLD.sub(' ', rest)
    rest = re.sub(r'\s+', ' ', rest).strip(' .,!?:;-—')
    return rest, year


# ---- short, focused answer ------------------------------------------------------------------

# SQ-05: only conversational tails are «вода». Caveats about price, dates,
# uncertainty or applicability («обратите внимание, что информация может
# отличаться», «please note that…») carry meaning and survive shortening.
_FLUFF = re.compile(
    r'(?:^|\b)(?:хотите|если\s+(?:вам|вы|хотите)|обратитесь|надеюсь|в\s+заключени\w*|в\s+итоге,?\s+выбор|'
    r'would\s+you\s+like|let\s+me\s+know|i\s+hope|in\s+conclusion|feel\s+free|'
    r'möchten\s+sie|ich\s+hoffe|voulez-vous|j[\'’]esp[eè]re|¿?quieres|espero\s+que|vuoi|spero\s+che|quer\s+que|espero\s+que)', re.I)
_CAVEAT = re.compile(
    r'(?:обратите\s+внимание|может\s+(?:отличаться|меняться|измениться)|уточняйте|не\s+является\s+офертой|по\s+состоянию\s+на|'
    r'please\s+note|may\s+(?:vary|differ|change)|subject\s+to\s+change|as\s+of\b|kann\s+(?:abweichen|variieren)|'
    r'peut\s+(?:varier|changer)|puede\s+(?:variar|cambiar)|può\s+(?:variare|cambiare)|pode\s+(?:variar|mudar))', re.I)
# Markdown emphasis is removed only as a paired marker around words; operators,
# identifiers and file names (2**3, __init__, data__2026.csv) are literal text.
_BOLD = re.compile(r'(?<![\w*])\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*(?![\w*])')
_UNDER_BOLD = re.compile(r'(?<![\w_])__(?=\S)([^_\n]*\s[^_\n]*?)(?<=\S)__(?![\w_])')
_CODE = re.compile(r'`([^`\n]+)`')


def _demark(text: str) -> str:
    text = _CODE.sub(r'\1', str(text or ''))
    text = _BOLD.sub(r'\1', text)
    return _UNDER_BOLD.sub(r'\1', text)
_STOP = frozenset(('какие', 'какой', 'какая', 'каковы', 'найди', 'расскажи', 'пожалуйста', 'about', 'which', 'what', 'where', 'there',
                   'these', 'those', 'есть', 'этой', 'этот', 'этого', 'чтобы', 'мне', 'with', 'from', 'that', 'this', 'года', 'year'))
_UNITS = re.compile(r'\d[\d\s.,]*\s*(?:л\.?\s*с|лс|hp|кв?т|kw|нм|nm|л(?:/100\s*км)?|l(?:/100\s*km)?|км/ч|km/h|mph|с(?:ек)?\b|s\b|мм|mm|кг|kg|'
                    r'гб|gb|тб|tb|мп|mp|мач|mah|ггц|ghz|вт|w\b|дюйм\w*|"|₽|руб\w*|\$|€|usd|eur|%)', re.I)
_PRICE_MARK = re.compile(r'(?:₽|руб\w*|\$|€|usd|eur|£|цен|стоит|price|cost|preis|prix|precio|prezzo|pre[cç]o)', re.I)


@dataclass(slots=True)
class _Unit:
    text: str
    order: int
    heading: str = ''
    bullet: bool = False


def _clean(line: str) -> str:
    line = _demark(line)
    line = re.sub(r'^\s*(?:[-*•·]|\d{1,2}[.)])\s+', '', line)
    return re.sub(r'\s+', ' ', line).strip()


def _sentences(paragraph: str) -> list[str]:
    parts = re.split(r'(?<=[.!?…])\s+(?=[A-ZА-ЯЁ0-9«"(])', paragraph)
    return [p.strip() for p in parts if p.strip()]


def _units(answer: str) -> list[_Unit]:
    out: list[_Unit] = []
    heading = ''
    for raw in str(answer or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        head = re.match(r'^#{1,6}\s*(.+)$', line) or re.match(r'^\*\*([^*]{2,60})\*\*:?\s*$', line)
        if head or (len(line) <= 60 and line.endswith(':') and not re.match(r'^\s*(?:[-*•]|\d+[.)])', line)):
            heading = _clean(head.group(1) if head else line).rstrip(':')
            continue
        bullet = bool(re.match(r'^\s*(?:[-*•·]|\d{1,2}[.)])\s+', line))
        text = _clean(line)
        if bullet:
            out.append(_Unit(text, len(out), heading, True))
        else:
            for sentence in _sentences(text):
                out.append(_Unit(sentence, len(out), heading, False))
    return out


def _stems(text: str) -> set[str]:
    words = re.findall(r'[\w-]{3,}', _n(text))
    return {w[:5] for w in words if w not in _STOP}


def _score(unit: _Unit, stems: set[str], wanted: list[str]) -> float:
    text = unit.text + ' ' + unit.heading
    score = 2.0 * len(stems & _stems(text))
    if 'specs' in wanted and _UNITS.search(unit.text):
        score += 3.0
    if 'price' in wanted and _PRICE_MARK.search(unit.text):
        score += 3.0
    if 'reviews' in wanted and re.search(r'(?:плюс|минус|недостат|достоинств|надежн|pros?|cons?|reliab|vorteil|nachteil|avantage|inconv|ventaj|desventaj|vantagg|svantagg|vantage|desvantage)', text, re.I):
        score += 3.0
    if unit.bullet:
        score += 0.5
    if len(unit.text) > 260:
        score -= 1.0
    return score


_LABELS = {
    'key': ('Главное', 'Key facts', 'Das Wichtigste', 'L’essentiel', 'Lo principal', 'In breve', 'O principal'),
    'steps': ('Что сделать', 'What to do', 'So geht’s', 'Étapes', 'Qué hacer', 'Cosa fare', 'O que fazer'),
    'more': ('Это главное из ответа. Полностью — скажите «подробнее».', 'That’s the gist. Say “more details” for the full answer.',
             'Das ist das Wichtigste. Die ganze Antwort: „mehr Details“.', 'C’est l’essentiel. Réponse complète : « plus de détails ».',
             'Eso es lo principal. Respuesta completa: «más detalles».', 'Questo è il succo. Risposta completa: «più dettagli».',
             'Isto é o principal. Resposta completa: «mais detalhes».'),
}


def _trim(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit - 3].rsplit(' ', 1)[0] + '…', True


def structure(question: str, answer: str, code: str, *, max_points: int = 6) -> tuple[str, bool]:
    """(short focused text, whether something was left out). Only the provider's sentences are used.

    SQ-02: the lead is the unit that answers the question best, not merely the
    first long one, and «Главное» never pads itself with unrelated fragments.
    SQ-07: any trimming (lead, point, step) reports ``shortened`` so the full
    text stays available via «подробнее».
    """
    units = [u for u in _units(answer) if u.text and (not _FLUFF.search(u.text) or _CAVEAT.search(u.text))]
    if not units:
        return str(answer or '').strip(), False
    stems = _stems(question)
    wanted = facets(question)
    relevant = {id(u) for u in units if not stems or _score(u, stems, wanted) > (0.5 if u.bullet else 0.0) or _CAVEAT.search(u.text)}
    noise = re.compile(r'(?:купить|скидк|доставк|в\s+наличии|форум|подскажите|\bbuy\b|\bsale\b|discount|forum)', re.I)
    direct = set(relevant)
    for previous, current in zip(units, units[1:]):
        # The sentence right after one that names the topic usually continues it
        # («Берёза — род… Распространена в…»); one step only, never noise.
        if id(previous) in direct and not current.bullet and not noise.search(current.text):
            relevant.add(id(current))
    total = sum(len(u.text) for u in units)
    if total <= 420 and len(units) <= 4:
        kept = [u for u in units if id(u) in relevant or not noise.search(u.text)]
        kept = kept or units
        return '\n'.join(u.text for u in kept), len(kept) < len(units)
    steps = [u for u in units if u.bullet]
    if 'howto' in wanted and len(steps) >= 2:
        # A how-to answer keeps the provider's steps in their order: numbered, trimmed, at most 8.
        lead = next((u for u in units if not u.bullet and len(u.text) >= 20 and u.order < steps[0].order), None)
        shown = steps[:8]
        trimmed = False
        lines = []
        for i, u in enumerate(shown, 1):
            text, cut = _trim(u.text, 240)
            trimmed = trimmed or cut
            lines.append(f'{i}. ' + text)
        head = (lead.text + '\n\n') if lead is not None else ''
        rest = [u for u in units if u is not lead and u not in shown]
        return head + _pick(_LABELS['steps'], code) + ':\n' + '\n'.join(lines), bool(rest) or trimmed
    candidates = [u for u in units if not u.bullet and len(u.text) >= 30]
    if stems and candidates:
        lead = max(candidates, key=lambda u: (_score(u, stems, wanted) - (2.0 if noise.search(u.text) else 0.0), -u.order))
    else:
        lead = candidates[0] if candidates else units[0]
    rest = [u for u in units if u is not lead]
    ranked = sorted(rest, key=lambda u: (-_score(u, stems, wanted), u.order))
    chosen = [u for u in ranked if id(u) in relevant and not (noise.search(u.text) and _score(u, stems, wanted) <= 2.0)][:max_points]
    chosen = sorted(chosen, key=lambda u: u.order)
    points = []
    trimmed = False
    for u in chosen:
        text, cut = _trim(u.text, 240)
        trimmed = trimmed or cut
        if u.heading and len(text) <= 70 and _n(u.heading) not in _n(text) and not re.match(r'^[^:]{1,40}:\s', text):
            text = f'{u.heading}: {text}'
        points.append('• ' + text)
    lead_text, lead_cut = _trim(lead.text, 300)
    body = lead_text + ('\n\n' + _pick(_LABELS['key'], code) + ':\n' + '\n'.join(points) if points else '')
    return body, len(rest) > len(chosen) or lead_cut or trimmed


# ---- follow-up offers ----------------------------------------------------------------------

@dataclass(slots=True)
class Offer:
    key: str                     # reviews | specs | other_year | price | news
    label: str
    query: str                   # '' for other_year until the year is known
    words: re.Pattern[str]


@dataclass(slots=True)
class _Pending:
    offers: list[Offer] = field(default_factory=list)
    full: str = ''
    thing: str = ''
    car: bool = False
    code: str = 'ru'
    waiting_year: bool = False
    at: float = 0.0
    owner_scope: str = ''
    question_token: object | None = None


_WORDS = {
    'reviews': re.compile(_B + r'(?:отзыв\w*|мнени\w*|обзор\w*|reviews?|opinions?|bewertung\w*|avis|rese[nñ]as?|opiniones|recensioni|opinioni|avalia[cç][oõ]es|opini[oõ]es)' + _E, re.I),
    'specs': re.compile(_B + r'(?:характеристик\w*|параметр\w*|specs?|specifications?|daten|caract[eé]ristiques|caracter[ií]sticas|caratteristiche|especifica[cç][oõ]es)' + _E, re.I),
    'other_year': re.compile(_B + r'(?:друг\w+\s+год\w*|год\w*|другого|another\s+year|other\s+year|year|anderen?\s+jahr\w*|jahr\w*|autre\s+ann[eé]e|ann[eé]e|otro\s+a[nñ]o|a[nñ]o|altro\s+anno|anno|outro\s+ano|ano)' + _E, re.I),
    'price': re.compile(_B + r'(?:цен\w*|стоимост\w*|стоит|price|cost|preis|prix|precio|prezzo|pre[cç]o)' + _E, re.I),
    'news': re.compile(_B + r'(?:новост\w*|news|nachrichten|actualit[eé]s|noticias|notizie|not[ií]cias)' + _E, re.I),
}
_QUERIES = {
    'reviews_car': ('отзывы владельцев {x}', '{x} owner reviews', '{x} Erfahrungen Besitzer', 'avis propriétaires {x}', 'opiniones de propietarios {x}', 'recensioni proprietari {x}', 'opiniões de proprietários {x}'),
    'reviews': ('отзывы о {x}', '{x} reviews', '{x} Bewertungen', 'avis {x}', 'reseñas {x}', 'recensioni {x}', 'avaliações {x}'),
    'specs': ('характеристики {x}', '{x} specifications', '{x} technische Daten', 'caractéristiques {x}', 'características {x}', 'caratteristiche {x}', 'especificações {x}'),
    'other_year': ('характеристики {x} {y} года', '{x} {y} specifications', '{x} {y} technische Daten', 'caractéristiques {x} {y}', 'características {x} {y}', 'caratteristiche {x} {y}', 'especificações {x} {y}'),
    'price': ('цена {x}', '{x} price', '{x} Preis', 'prix {x}', 'precio {x}', 'prezzo {x}', 'preço {x}'),
    'news': ('последние новости {x}', '{x} latest news', '{x} aktuelle Nachrichten', 'actualités {x}', 'noticias {x}', 'notizie {x}', 'notícias {x}'),
}
_OFFER_LABELS = {
    'reviews_car': ('отзывы владельцев об этой модели', 'owner reviews of this model', 'Erfahrungen von Besitzern dieses Modells', 'des avis de propriétaires de ce modèle',
                    'opiniones de propietarios de este modelo', 'recensioni dei proprietari di questo modello', 'opiniões de proprietários deste modelo'),
    'reviews': ('отзывы о нём', 'reviews of it', 'Bewertungen dazu', 'des avis', 'reseñas', 'recensioni', 'avaliações'),
    'specs': ('его характеристики', 'its specifications', 'die technischen Daten', 'ses caractéristiques', 'sus características', 'le caratteristiche', 'as especificações'),
    'other_year': ('характеристики этой модели другого года', 'the specs of this model from another year', 'die Daten dieses Modells aus einem anderen Jahr',
                   'les caractéristiques de ce modèle d’une autre année', 'las características de este modelo de otro año', 'le caratteristiche di questo modello di un altro anno',
                   'as especificações deste modelo de outro ano'),
    'price': ('сколько он стоит', 'its price', 'den Preis', 'son prix', 'su precio', 'il prezzo', 'o preço'),
    'news': ('последние новости о нём', 'the latest news about it', 'aktuelle Nachrichten dazu', 'les dernières actualités', 'las últimas noticias', 'le ultime notizie', 'as últimas notícias'),
}
_TEXT = {
    'offer': ('Хотите, чтобы я нашёл {a}?', 'Want me to find {a}?', 'Soll ich {a} suchen?', 'Voulez-vous que je cherche {a} ?',
              '¿Quieres que busque {a}?', 'Vuoi che cerchi {a}?', 'Queres que procure {a}?'),
    'or': (' или ', ' or ', ' oder ', ' ou ', ' o ', ' o ', ' ou '),
    'which': ('Что найти: {a}?', 'What should I look up: {a}?', 'Was soll ich suchen: {a}?', 'Que dois-je chercher : {a} ?', '¿Qué busco: {a}?', 'Cosa cerco: {a}?', 'O que procuro: {a}?'),
    'year': ('Какого года?', 'Which year?', 'Welches Jahr?', 'Quelle année ?', '¿De qué año?', 'Di che anno?', 'De que ano?'),
    'related': ('Могу ещё узнать: «{q}?» Найти?', 'I can also look up: “{q}?” Shall I?', 'Ich kann auch nachsehen: „{q}?“ Soll ich?',
                'Je peux aussi chercher : « {q} ? » Je cherche ?', 'También puedo buscar: «{q}?» ¿Lo busco?', 'Posso anche cercare: «{q}?» Cerco?',
                'Também posso procurar: «{q}?» Procuro?'),
    'ok': ('Хорошо.', 'OK.', 'Okay.', 'D’accord.', 'De acuerdo.', 'Va bene.', 'Está bem.'),
}
_YES = re.compile(r'^(?:да|ага|угу|давай(?:те)?|конечно|хочу|ок(?:ей)?|найди|yes|yeah|yep|sure|ok(?:ay)?|please|go\s+ahead|ja|gerne|oui|volontiers|s[ií]|claro|vale|certo|sim)'
                  r'(?:\s+(?:давай|найди|пожалуйста|please|bitte|por\s+favor|per\s+favore))?$', re.I)
_NO = re.compile(r'^(?:нет|неа|не\s+надо|не\s+нужно|не\s+стоит|no|nope|no\s+thanks|nein|non|n[aã]o)(?:\s+(?:спасибо|thanks|danke|merci|gracias|grazie|obrigad[oa]))?$', re.I)
# NEO 187 HREQ-014: «да да», «да, верно», «это то, что я имел в виду» agree;
# «да нет», «не знаю» and negations never become consent.
from jarvis.runtime.confirm_words187 import guard_no as _guard_no187, guard_yes as _guard_yes187
_YES, _NO = _guard_yes187(_YES), _guard_no187(_NO)
_MORE = re.compile(r'^(?:подробнее|подробно|полностью|весь\s+ответ|полный\s+ответ|расскажи\s+подробнее|more(?:\s+details)?|full\s+answer|mehr(?:\s+details)?|'
                   r'plus\s+de\s+d[eé]tails|m[aá]s\s+detalles|pi[uù]\s+dettagli|mais\s+detalhes)$', re.I)
_ORDINAL = (re.compile(r'^(?:1|перв\w*|first|erste\w*|premi\w*|primer\w*|prim\w*)(?:\s+\w+)?$', re.I),
            re.compile(r'^(?:2|втор\w*|second\w*|zweite\w*|deuxi[eè]me|segund\w*|second\w*)(?:\s+\w+)?$', re.I))

_lock = threading.Lock()
_pending = _Pending()


def offers(question: str, code: str, *, related: list[str] | None = None) -> list[Offer]:
    """Up to two next searches that fit the question."""
    thing, year = subject(question)
    wanted = facets(question)
    code = _code(code)
    if not thing or len(thing) > 60:
        return []
    car = bool(_CAR.search(_n(question)))
    keys: list[str]
    if 'specs' in wanted:
        keys = ['reviews', 'other_year' if (car or year) else 'price']
    elif 'reviews' in wanted:
        keys = ['specs', 'price']
    elif 'price' in wanted:
        keys = ['reviews', 'specs']
    elif 'person' in wanted:
        keys = ['news']
    else:
        return []
    out = []
    for key in keys:
        name = 'reviews_car' if key == 'reviews' and car else key
        query = '' if key == 'other_year' else _pick(_QUERIES[name], code).format(x=thing)
        out.append(Offer(key, _pick(_OFFER_LABELS[name], code), query, _WORDS[key]))
    return out


def related_offer(question: str) -> Offer:
    """A next question the search provider itself suggested."""
    q = re.sub(r'\s+', ' ', str(question or '')).strip().rstrip('?')
    return Offer('related', q, q, re.compile(r'(?!x)x'))


def offer_text(items: list[Offer], code: str) -> str:
    if not items:
        return ''
    if items[0].key == 'related':
        return _pick(_TEXT['related'], code).format(q=items[0].label)
    joined = _pick(_TEXT['or'], code).join(o.label for o in items)
    return _pick(_TEXT['offer'], code).format(a=joined)


def readable(answer: str) -> str:
    """The provider's full answer without markdown marks (headings, bold, list stars)."""
    out = []
    for raw in str(answer or '').splitlines():
        line = raw.rstrip()
        head = re.match(r'^\s*#{1,6}\s*(.+)$', line)
        if head:
            line = _clean(head.group(1)).rstrip(':') + ':'
        elif re.match(r'^\s*[-*•·]\s+', line):
            line = '• ' + _clean(line)
        else:
            line = _demark(line)
        out.append(line)
    return '\n'.join(out).strip()


def remember(question: str, items: list[Offer], code: str, *, full: str = '') -> None:
    """Keep the offers (and the full answer) for the next short reply."""
    global _pending
    from jarvis.runtime.conversation_journal import current_scope
    thing, _year = subject(question)
    with _lock:
        _pending = _Pending(list(items), readable(full), thing, bool(_CAR.search(_n(question))), _code(code), False,
                            time.monotonic(), current_scope(), object())


def pending() -> bool:
    with _lock:
        return bool(_pending.offers or _pending.full) and time.monotonic() - _pending.at <= PENDING_TTL


def forget() -> None:
    global _pending
    with _lock:
        _pending = _Pending()


def owned_question_token() -> object | None:
    """Snapshot a live question created in this conversation; never infer its owner."""
    from jarvis.runtime.conversation_journal import current_scope
    with _lock:
        if (_pending.owner_scope == current_scope() and bool(_pending.offers or _pending.full)
                and time.monotonic() - _pending.at <= PENDING_TTL):
            return _pending.question_token
    return None


def forget_if_owned(token: object | None) -> bool:
    """Clear only the same owned generation, preserving any replacement question."""
    global _pending
    if token is None:
        return False
    from jarvis.runtime.conversation_journal import current_scope
    with _lock:
        if (_pending.owner_scope != current_scope() or _pending.question_token is not token
                or not (bool(_pending.offers or _pending.full))):
            return False
        _pending = _Pending()
        return True


def _year_query(p: _Pending, year: str) -> str:
    return _pick(_QUERIES['other_year'], p.code).format(x=p.thing, y=year)


def resolve(text: str) -> dict[str, Any] | None:
    """The next reply to the offer: {'query': ...} to search, {'reply': ...} to say, or None (a new request)."""
    if not pending():
        return None
    t = _n(text)
    t = re.sub(r'^(?:ну|а|и|тогда|well|then)\s+', '', t)
    if not t or len(t.split()) > 8:
        return None
    with _lock:
        p = _pending
        code = p.code
        if _MORE.match(t) and p.full:
            full, p.full = p.full, ''
            return {'reply': full, 'kind': 'more'}
        if _NO.match(t):
            p.offers, p.full = [], ''
            return {'reply': _pick(_TEXT['ok'], code), 'kind': 'dismiss'}
        year = _YEAR.search(t)
        by_year = next((o for o in p.offers if o.key == 'other_year'), None)
        if year and by_year is not None and (p.waiting_year or len(t.split()) <= 4 or by_year.words.search(t) or _WORDS['specs'].search(t)):
            p.offers, p.waiting_year = [], False
            return {'query': _year_query(p, year.group(1)), 'kind': 'other_year'}
        if p.waiting_year:
            return None
        chosen: Offer | None = None
        for index, pattern in enumerate(_ORDINAL):
            if pattern.match(t) and index < len(p.offers):
                chosen = p.offers[index]
        if chosen is None:
            matches = [o for o in p.offers if o.words.search(t)]
            if len(matches) > 1 and by_year in matches and _WORDS['other_year'].search(t):
                matches = [by_year]
            elif len(matches) > 1:
                matches = [o for o in matches if o.key != 'other_year'] or matches
            if len(matches) == 1:
                chosen = matches[0]
        if chosen is None and _YES.match(t):
            if len(p.offers) == 1:
                chosen = p.offers[0]
            elif p.offers:
                from jarvis.runtime.conversation_journal import current_scope
                p.owner_scope, p.question_token = current_scope(), object()
                return {'reply': _pick(_TEXT['which'], code).format(a=_pick(_TEXT['or'], code).join(o.label for o in p.offers)), 'kind': 'which'}
        if chosen is None:
            return None
        if chosen.key == 'other_year':
            from jarvis.runtime.conversation_journal import current_scope
            p.owner_scope, p.question_token = current_scope(), object()
            p.waiting_year = True
            p.at = time.monotonic()
            return {'reply': _pick(_TEXT['year'], code), 'kind': 'year'}
        p.offers = []
        return {'query': chosen.query, 'kind': chosen.key}


def more_hint(code: str) -> str:
    return _pick(_LABELS['more'], code)


__all__ = ['Offer', 'classify', 'facets', 'forget', 'forget_if_owned', 'owned_question_token', 'more_hint', 'offer_text', 'offers', 'pending', 'readable', 'related_offer', 'remember', 'resolve', 'structure', 'subject']
