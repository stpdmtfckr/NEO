# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Daily briefing personalization and the last visible-delivery receipt.

The daily briefing is assembled from local/integration telemetry by the briefing
tool and uses this module only for localized time-aware greetings, non-repeating
wishes and startup-delivery state.
"""

from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import random
import threading
from typing import Any

from jarvis.language import language_code

logger = logging.getLogger(__name__)
SUPPORTED_LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")
_STATE_LOCK = threading.RLock()

_TEXT = {'today': {'ru': 'Сегодня',
           'en': 'Today is',
           'de': 'Heute ist',
           'fr': 'Nous sommes',
           'es': 'Hoy es',
           'it': 'Oggi è',
           'pt': 'Hoje é'},
 'at': {'ru': 'в', 'en': 'at', 'de': 'um', 'fr': 'à', 'es': 'a las', 'it': 'alle', 'pt': 'às'},
 'time_now': {'ru': 'Сейчас {time}.', 'en': 'The time is {time}.',
              'de': 'Es ist {time} Uhr.', 'fr': 'Il est {time}.',
              'es': 'Hora actual: {time}.', 'it': 'Ora attuale: {time}.',
              'pt': 'Hora atual: {time}.'}}

_WEEKDAYS = {'ru': ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'],
 'en': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
 'de': ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'],
 'fr': ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche'],
 'es': ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo'],
 'it': ['lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica'],
 'pt': ['segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira', 'sexta-feira', 'sábado', 'domingo']}

_MONTHS = {'ru': ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'],
 'en': ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'],
 'de': ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember'],
 'fr': ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre'],
 'es': ['enero',
        'febrero',
        'marzo',
        'abril',
        'mayo',
        'junio',
        'julio',
        'agosto',
        'septiembre',
        'octubre',
        'noviembre',
        'diciembre'],
 'it': ['gennaio',
        'febbraio',
        'marzo',
        'aprile',
        'maggio',
        'giugno',
        'luglio',
        'agosto',
        'settembre',
        'ottobre',
        'novembre',
        'dicembre'],
 'pt': ['janeiro',
        'fevereiro',
        'março',
        'abril',
        'maio',
        'junho',
        'julho',
        'agosto',
        'setembro',
        'outubro',
        'novembro',
        'dezembro']}

_GREETING = {'ru': {'morning': 'Доброе утро', 'day': 'Добрый день', 'evening': 'Добрый вечер', 'night': 'Доброй ночи'},
 'en': {'morning': 'Good morning', 'day': 'Good afternoon', 'evening': 'Good evening', 'night': 'Good evening'},
 'de': {'morning': 'Guten Morgen', 'day': 'Guten Tag', 'evening': 'Guten Abend', 'night': 'Guten Abend'},
 'fr': {'morning': 'Bonjour', 'day': 'Bonjour', 'evening': 'Bonsoir', 'night': 'Bonsoir'},
 'es': {'morning': 'Buenos días', 'day': 'Buenas tardes', 'evening': 'Buenas noches', 'night': 'Buenas noches'},
 'it': {'morning': 'Buongiorno', 'day': 'Buon pomeriggio', 'evening': 'Buonasera', 'night': 'Buonasera'},
 'pt': {'morning': 'Bom dia', 'day': 'Boa tarde', 'evening': 'Boa noite', 'night': 'Boa noite'}}

_WISH_PARTS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {'ru': (('Желаю вам продуктивного дня.',
         'Пусть сегодняшний день пройдёт уверенно.',
         'Желаю вам хорошего темпа на весь день.',
         'Пусть сегодня всё складывается удачно.',
         'Желаю вам спокойного и результативного дня.',
         'Пусть день начнётся с хорошего настроя.',
         'Желаю вам энергии для всего запланированного.',
         'Пусть сегодня будет больше приятных моментов.',
         'Желаю вам ясных мыслей и хороших решений.',
         'Пусть этот день принесёт достойные результаты.'),
        ('Пусть задачи решаются легче обычного.',
         'Надеюсь, планы будут идти по порядку.',
         'Пусть важные дела продвигаются без лишней суеты.',
         'Надеюсь, сегодня найдётся время для главного.',
         'Пусть рабочий ритм будет комфортным.',
         'Надеюсь, день принесёт приятные моменты.',
         'Пусть решения приходят вовремя.',
         'Надеюсь, день приятно удивит.',
         'Пусть всё необходимое окажется под рукой.',
         'Надеюсь, намеченные цели станут ближе.'),
        ('И обязательно оставьте немного времени для отдыха.',
         'И пусть к вечеру останется чувство хорошо сделанного дня.',
         'И пусть рядом будет повод улыбнуться.',
         'И не забывайте делать небольшие паузы.',
         'И пусть хорошее настроение сохранится до вечера.',
         'И пусть день завершится чем-то приятным.',
         'И пусть останутся силы на то, что действительно нравится.',
         'И пусть хотя бы одна вещь сегодня приятно удивит.',
         'И пусть результат окажется лучше ожиданий.',
         'И пусть сегодня будет чем гордиться.')),
 'en': (('I wish you a productive day.',
         'May today move at a confident pace.',
         'I hope you have a strong rhythm throughout the day.',
         'May things go your way today.',
         'I wish you a calm and productive day.',
         'May the day begin on a positive note.',
         'I wish you plenty of energy for everything planned.',
         'May today bring more pleasant moments.',
         'I wish you clear thoughts and good decisions.',
         'May this day bring worthwhile results.'),
        ('May your tasks feel easier than usual.',
         'I hope your plans fall neatly into place.',
         'May the important things move forward without unnecessary rush.',
         'I hope there is time today for what matters most.',
         'May your working rhythm feel comfortable.',
         'I hope the day brings you pleasant moments.',
         'May the right decisions come at the right time.',
         'I hope the day surprises you in a good way.',
         'May everything you need be close at hand.',
         'I hope your goals feel a little closer by evening.'),
        ('And make sure to leave a little time to rest.',
         'And may the evening bring the satisfaction of a day well spent.',
         'And may there be at least one good reason to smile.',
         'And remember to take a few small breaks.',
         'And may your good mood last into the evening.',
         'And may the day end with something pleasant.',
         'And may you still have energy for something you truly enjoy.',
         'And may at least one thing pleasantly surprise you today.',
         'And may the result be better than expected.',
         'And may today give you something to be proud of.')),
 'de': (('Ich wünsche dir einen produktiven Tag.',
         'Möge der heutige Tag sicher und ruhig verlaufen.',
         'Ich wünsche dir einen guten Rhythmus für den ganzen Tag.',
         'Möge heute vieles gut zusammenpassen.',
         'Ich wünsche dir einen ruhigen und erfolgreichen Tag.',
         'Möge der Tag mit guter Energie beginnen.',
         'Ich wünsche dir genug Kraft für alles Geplante.',
         'Möge der Tag viele angenehme Momente bringen.',
         'Ich wünsche dir klare Gedanken und gute Entscheidungen.',
         'Möge dieser Tag gute Ergebnisse bringen.'),
        ('Mögen die Aufgaben heute etwas leichter fallen.',
         'Ich hoffe, deine Pläne fügen sich gut zusammen.',
         'Mögen wichtige Dinge ohne unnötige Hektik vorankommen.',
         'Ich hoffe, heute bleibt Zeit für das Wesentliche.',
         'Möge dein Arbeitsrhythmus angenehm sein.',
         'Ich hoffe, der Tag bringt dir angenehme Momente.',
         'Mögen die richtigen Entscheidungen zur richtigen Zeit kommen.',
         'Ich hoffe, der Tag überrascht dich positiv.',
         'Möge alles Nötige griffbereit sein.',
         'Ich hoffe, deine Ziele rücken heute ein Stück näher.'),
        ('Und nimm dir unbedingt auch etwas Zeit zum Ausruhen.',
         'Und möge der Abend das gute Gefühl eines gelungenen Tages bringen.',
         'Und möge es einen guten Grund zum Lächeln geben.',
         'Und vergiss kleine Pausen nicht.',
         'Und möge die gute Stimmung bis zum Abend bleiben.',
         'Und möge der Tag mit etwas Angenehmem enden.',
         'Und möge noch Energie für etwas bleiben, das dir Freude macht.',
         'Und möge dich heute wenigstens eine Sache angenehm überraschen.',
         'Und möge das Ergebnis besser als erwartet sein.',
         'Und möge es heute etwas geben, auf das du stolz sein kannst.')),
 'fr': (('Je vous souhaite une journée productive.',
         'Que cette journée avance avec assurance.',
         'Je vous souhaite un bon rythme pour toute la journée.',
         'Que les choses se passent au mieux aujourd’hui.',
         'Je vous souhaite une journée calme et efficace.',
         'Que la journée commence avec une bonne énergie.',
         'Je vous souhaite assez d’énergie pour tout ce qui est prévu.',
         'Que cette journée apporte de nombreux moments agréables.',
         'Je vous souhaite des idées claires et de bonnes décisions.',
         'Que cette journée apporte de beaux résultats.'),
        ('Que les tâches soient un peu plus faciles qu’à l’habitude.',
         'J’espère que vos projets se mettront naturellement en place.',
         'Que les choses importantes avancent sans agitation inutile.',
         'J’espère qu’il restera du temps pour l’essentiel.',
         'Que votre rythme de travail reste confortable.',
         'J’espère que la journée apportera de bons moments.',
         'Que les bonnes décisions viennent au bon moment.',
         'J’espère que la journée vous surprendra agréablement.',
         'Que tout ce dont vous avez besoin soit à portée de main.',
         'J’espère que vos objectifs seront un peu plus proches ce soir.'),
        ('Et gardez un peu de temps pour vous reposer.',
         'Et que la soirée apporte la satisfaction d’une journée bien remplie.',
         'Et qu’il y ait au moins une bonne raison de sourire.',
         'Et n’oubliez pas de faire quelques petites pauses.',
         'Et que la bonne humeur vous accompagne jusqu’au soir.',
         'Et que la journée se termine sur quelque chose d’agréable.',
         'Et qu’il reste de l’énergie pour ce que vous aimez vraiment.',
         'Et qu’au moins une chose vous surprenne agréablement aujourd’hui.',
         'Et que le résultat dépasse vos attentes.',
         'Et que cette journée vous donne une raison d’être fier.')),
 'es': (('Le deseo un día productivo.',
         'Que hoy avance con un ritmo seguro.',
         'Le deseo un buen ritmo para toda la jornada.',
         'Que hoy todo encaje de la mejor manera.',
         'Le deseo un día tranquilo y productivo.',
         'Que el día empiece con buena energía.',
         'Le deseo energía para todo lo que tiene previsto.',
         'Que hoy haya muchos momentos agradables.',
         'Le deseo ideas claras y buenas decisiones.',
         'Que este día traiga buenos resultados.'),
        ('Que las tareas resulten un poco más fáciles de lo habitual.',
         'Espero que sus planes encajen con naturalidad.',
         'Que lo importante avance sin prisas innecesarias.',
         'Espero que hoy haya tiempo para lo esencial.',
         'Que el ritmo de trabajo sea cómodo.',
         'Espero que las buenas noticias lleguen pronto.',
         'Que las decisiones correctas lleguen a tiempo.',
         'Espero que el día le sorprenda para bien.',
         'Que todo lo necesario esté a mano.',
         'Espero que sus objetivos estén un poco más cerca al final del día.'),
        ('Y reserve también un poco de tiempo para descansar.',
         'Y que la tarde deje la satisfacción de un día bien aprovechado.',
         'Y que haya al menos un buen motivo para sonreír.',
         'Y no olvide hacer pequeñas pausas.',
         'Y que el buen ánimo dure hasta la noche.',
         'Y que el día termine con algo agradable.',
         'Y que aún quede energía para algo que realmente disfrute.',
         'Y que al menos una cosa le sorprenda gratamente hoy.',
         'Y que el resultado sea mejor de lo esperado.',
         'Y que hoy haya algo de lo que sentirse orgulloso.')),
 'it': (('Le auguro una giornata produttiva.',
         'Che oggi proceda con un ritmo sicuro.',
         'Le auguro un buon ritmo per tutta la giornata.',
         'Che oggi le cose vadano nel modo migliore.',
         'Le auguro una giornata tranquilla e produttiva.',
         'Che la giornata inizi con una buona energia.',
         'Le auguro energia per tutto ciò che ha in programma.',
         'Che oggi ci siano molti momenti piacevoli.',
         'Le auguro pensieri chiari e buone decisioni.',
         'Che questa giornata porti buoni risultati.'),
        ('Che i compiti risultino un po’ più semplici del solito.',
         'Spero che i suoi programmi vadano al loro posto con naturalezza.',
         'Che le cose importanti avanzino senza fretta inutile.',
         'Spero che oggi ci sia tempo per ciò che conta davvero.',
         'Che il ritmo di lavoro sia confortevole.',
         'Spero che le buone notizie arrivino presto.',
         'Che le decisioni giuste arrivino al momento giusto.',
         'Spero che la giornata la sorprenda piacevolmente.',
         'Che tutto ciò che serve sia a portata di mano.',
         'Spero che i suoi obiettivi siano un po’ più vicini questa sera.'),
        ('E si ricordi di lasciare un po’ di tempo al riposo.',
         'E che la sera porti la soddisfazione di una giornata ben spesa.',
         'E che ci sia almeno un buon motivo per sorridere.',
         'E non dimentichi qualche piccola pausa.',
         'E che il buon umore duri fino a sera.',
         'E che la giornata finisca con qualcosa di piacevole.',
         'E che resti energia per qualcosa che le piace davvero.',
         'E che almeno una cosa oggi la sorprenda piacevolmente.',
         'E che il risultato sia migliore del previsto.',
         'E che oggi ci sia qualcosa di cui essere orgogliosi.')),
 'pt': (('Desejo-lhe um dia produtivo.',
         'Que hoje avance com um ritmo seguro.',
         'Desejo-lhe um bom ritmo para todo o dia.',
         'Que hoje tudo se encaixe da melhor forma.',
         'Desejo-lhe um dia tranquilo e produtivo.',
         'Que o dia comece com boa energia.',
         'Desejo-lhe energia para tudo o que está planeado.',
         'Que hoje haja muitos momentos agradáveis.',
         'Desejo-lhe ideias claras e boas decisões.',
         'Que este dia traga bons resultados.'),
        ('Que as tarefas sejam um pouco mais fáceis do que o habitual.',
         'Espero que os seus planos se encaixem naturalmente.',
         'Que o que é importante avance sem pressa desnecessária.',
         'Espero que hoje haja tempo para o essencial.',
         'Que o ritmo de trabalho seja confortável.',
         'Espero que as boas notícias cheguem depressa.',
         'Que as decisões certas cheguem na hora certa.',
         'Espero que o dia o surpreenda pela positiva.',
         'Que tudo o que precisa esteja por perto.',
         'Espero que os seus objetivos estejam um pouco mais próximos ao fim do dia.'),
        ('E reserve também um pouco de tempo para descansar.',
         'E que a noite traga a satisfação de um dia bem aproveitado.',
         'E que haja pelo menos um bom motivo para sorrir.',
         'E não se esqueça de fazer pequenas pausas.',
         'E que o bom humor dure até ao fim do dia.',
         'E que o dia termine com algo agradável.',
         'E que ainda reste energia para algo de que realmente gosta.',
         'E que pelo menos uma coisa o surpreenda agradavelmente hoje.',
         'E que o resultado seja melhor do que o esperado.',
         'E que hoje haja algo de que se orgulhar.'))}


def _code(config: Any) -> str:
    code = language_code(config)
    return code if code in SUPPORTED_LANGUAGES else "en"


class DailyBriefingStore:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.state_path = Path(config.paths.root) / "daily_briefing_state.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.debug("Could not persist daily briefing state", exc_info=True)

    def startup_already_delivered(self, now: datetime | None = None) -> bool:
        if bool(self.config.get("briefing.repeat_on_every_start", False)):
            return False
        today = (now or datetime.now()).date().isoformat()
        return str(self._read(self.state_path).get("last_startup_date", "")) == today

    def mark_startup_delivered(self, now: datetime | None = None) -> None:
        with _STATE_LOCK:
            raw = self._read(self.state_path)
            data = {k: raw[k] for k in ("last_startup_date", "recent_wishes") if k in raw}
            data["last_startup_date"] = (now or datetime.now()).date().isoformat()
            self._write(self.state_path, data)

    def recent_wish_indexes(self) -> list[int]:
        rows = self._read(self.state_path).get("recent_wishes", [])
        return [int(x) for x in rows if isinstance(x, int) or str(x).isdigit()][-30:]

    def remember_wish(self, index: int) -> None:
        with _STATE_LOCK:
            raw = self._read(self.state_path)
            data = {k: raw[k] for k in ("last_startup_date", "recent_wishes") if k in raw}
            rows = [int(x) for x in data.get("recent_wishes", []) if isinstance(x, int) or str(x).isdigit()]
            rows.append(int(index))
            data["recent_wishes"] = rows[-30:]
            self._write(self.state_path, data)


def _time_bucket(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 18:
        return "day"
    if 18 <= h < 24:
        return "evening"
    return "night"


def _date_text(now: datetime, code: str) -> str:
    wd = _WEEKDAYS[code][now.weekday()]
    month = _MONTHS[code][now.month - 1]
    if code == "ru":
        return f"{wd}, {now.day} {month} {now.year} года"
    if code == "en":
        return f"{wd}, {month} {now.day}, {now.year}"
    if code == "de":
        return f"{wd}, {now.day}. {month} {now.year}"
    return f"{wd}, {now.day} {month} {now.year}"


def date_and_time_line(now: datetime, code: str, *, include_time: bool = True) -> str:
    """Format date and time independently; use the supplied local datetime."""
    code = code if code in SUPPORTED_LANGUAGES else "ru"
    line = f"{_TEXT['today'][code]} {_date_text(now, code)}."
    if include_time:
        line += " " + _TEXT['time_now'][code].format(time=f"{now:%H:%M}")
    return line


def greeting_and_wish(config: Any, now: datetime | None = None) -> str:
    now = now or datetime.now()
    code = _code(config)
    store = DailyBriefingStore(config)
    greeting = _GREETING[code][_time_bucket(now)]
    if bool(config.get("user_name_enabled", True)):
        name = " ".join(str(config.get("user_name", "Boss") or "Boss").replace("\x00", " ").split())[:80] or "Boss"
        greeting = f"{greeting}, {name}."
    else:
        greeting = greeting + "."
    seed = int(hashlib.sha256(f"{now.date().isoformat()}|{now.hour}|{Path(config.paths.root)}".encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    recent = set(store.recent_wish_indexes())
    candidates = [x for x in range(1000) if x not in recent] or list(range(1000))
    idx = candidates[rng.randrange(len(candidates))]
    a, b, c = _WISH_PARTS[code]
    ia, ib, ic = idx // 100, (idx // 10) % 10, idx % 10
    wish = f"{a[ia]} {b[ib]} {c[ic]}"
    store.remember_wish(idx)
    date_line = date_and_time_line(now, code, include_time=bool(config.get("briefing.include_time", True)))
    return f"{greeting}\n{date_line}\n{wish}"


def greeting_variant_count(code: str) -> int:
    parts = _WISH_PARTS.get(code)
    return len(parts[0]) * len(parts[1]) * len(parts[2]) if parts else 0
