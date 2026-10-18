"""NEO 188 atlas: status questions bound to the handlers that already exist.

* proposed.installations.status — «что сейчас устанавливается»: read-only view of
  the Steam manifests the HREQ-021 watcher already reads. Other installers are
  not tracked, and the answer says so.
* proposed.monitor.spike_status — «какой процесс нагружает компьютер»: the
  existing ``system.top_processes`` tool.
* proposed.document.show_evidence — «покажи фрагмент, на котором основан ответ»:
  the first source excerpt of the last answer from the user's files.

Whole phrases only: these questions used to open an application named
«installation progress for Steam» or «excerpt supporting the answer».
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_SPIKES = {
    'ru': r"(?:какой|что за) процесс (?:нагружает|грузит|тормозит) (?:компьютер|комп|пк|систему)|покажи (?:скачки|пики|всплески) нагрузки(?: процессов)?",
    'en': r"which process is (?:overloading|slowing down|loading) the (?:computer|pc|system)|show (?:the )?(?:process )?(?:resource|load) spikes",
    'de': r"welcher prozess belastet den (?:computer|pc|rechner)|zeige (?:die )?lastspitzen(?: der prozesse)?",
    'fr': r"quel processus surcharge l['’]ordinateur|montre les pics de (?:consommation|charge)(?: des processus)?",
    'es': r"qué proceso está sobrecargando el (?:ordenador|equipo|pc)|muestra los picos de (?:consumo|carga)(?: de los procesos)?",
    'it': r"quale processo sovraccarica il (?:computer|pc)|mostra i picchi di (?:consumo|carico)(?: dei processi)?",
    'pt': r"que processo está a sobrecarregar o (?:computador|pc)|mostre? os picos de (?:consumo|carga)(?: dos processos)?",
}
_INSTALLS = {
    'ru': r"что (?:сейчас )?устанавливается|покажи (?:состояние|прогресс|ход) установки(?: (?P<app>.+))?",
    'en': r"what is being installed(?: now)?|show (?:the )?installation progress(?: (?:for|of) (?P<app>.+))?",
    'de': r"was wird gerade installiert|zeige den installationsfortschritt(?: von (?P<app>.+))?",
    'fr': r"qu['’]est-ce qui est en cours d['’]installation|montre la progression de l['’]installation(?: de (?P<app>.+))?",
    'es': r"qué se está instalando|muestra el progreso de (?:la )?instalación(?: de (?P<app>.+))?",
    'it': r"(?:che )?cosa si sta installando|mostra l['’]avanzamento dell['’]installazione(?: di (?P<app>.+))?",
    'pt': r"o que está (?:a ser|sendo) instalado|mostre? o progresso da instalação(?: de (?P<app>.+))?",
}
_EVIDENCE = {
    'ru': r"покажи (?:фрагмент|цитату|место),? (?:на котором|на которой|откуда) (?:основан|основана|взят) (?:этот )?ответ",
    'en': r"show (?:me )?the (?:excerpt|passage|fragment) (?:supporting|behind|backing) (?:the|this|that) answer",
    'de': r"zeige (?:mir )?den ausschnitt,? auf dem die antwort (?:basiert|beruht)",
    'fr': r"montre (?:le passage|l['’]extrait) qui justifie (?:la|cette) réponse",
    'es': r"muestra el fragmento que respalda (?:la|esta) respuesta",
    'it': r"mostra il (?:passaggio|frammento) su cui si basa (?:la|questa) risposta",
    'pt': r"mostra o (?:trecho|excerto|fragmento) que fundamenta a resposta",
}
# proposed.discord.play_now: Discord «play» already starts the track at once (unlike «add to queue»).
_DISCORD_NOW = {
    'ru': r"(?:включи|поставь) сейчас (?P<q>.+?) (?:в|на) (?:дискорде|дискорд|discord)|замени текущую (?:песню|музыку|трек) (?:в|на) (?:дискорде|discord) на (?P<q2>.+)",
    'en': r"play (?P<q>.+?) now (?:in|on) discord|replace the current discord (?:song|track) with (?P<q2>.+)",
    'de': r"spiele jetzt (?P<q>.+?) (?:in|auf) discord|ersetze den aktuellen discord-(?:song|titel) durch (?P<q2>.+)",
    'fr': r"joue (?P<q>.+?) maintenant (?:dans|sur) discord|remplace la chanson discord actuelle par (?P<q2>.+)",
    'es': r"reproduce (?P<q>.+?) ahora en discord|reemplaza la canción actual de discord por (?P<q2>.+)",
    'it': r"riproduci ora (?P<q>.+?) su discord|sostituisci il brano corrente di discord con (?P<q2>.+)",
    'pt': r"toca (?P<q>.+?) agora no discord|substitua a música atual do discord por (?P<q2>.+)",
}
_TEXT = {
    'none': ('Steam сейчас ничего не устанавливает. Установки вне Steam NEO не отслеживает.',
             'Steam is not installing anything right now. NEO does not track installs outside Steam.',
             'Steam installiert gerade nichts. Installationen außerhalb von Steam verfolgt NEO nicht.',
             'Steam n’installe rien pour le moment. NEO ne suit pas les installations hors de Steam.',
             'Steam no está instalando nada ahora. NEO no sigue las instalaciones fuera de Steam.',
             'Steam non sta installando nulla al momento. NEO non segue le installazioni fuori da Steam.',
             'O Steam não está a instalar nada agora. O NEO não acompanha instalações fora do Steam.'),
    'list': ('Steam сейчас устанавливает: {rows}. Установки вне Steam NEO не отслеживает.',
             'Steam is installing: {rows}. NEO does not track installs outside Steam.',
             'Steam installiert gerade: {rows}. Installationen außerhalb von Steam verfolgt NEO nicht.',
             'Steam installe : {rows}. NEO ne suit pas les installations hors de Steam.',
             'Steam está instalando: {rows}. NEO no sigue las instalaciones fuera de Steam.',
             'Steam sta installando: {rows}. NEO non segue le installazioni fuori da Steam.',
             'O Steam está a instalar: {rows}. O NEO não acompanha instalações fora do Steam.'),
    'app_none': ('Steam сейчас не устанавливает «{app}».', 'Steam is not installing “{app}” right now.', 'Steam installiert „{app}“ gerade nicht.',
                 'Steam n’installe pas « {app} » pour le moment.', 'Steam no está instalando «{app}» ahora.', 'Steam non sta installando «{app}» al momento.',
                 'O Steam não está a instalar «{app}» agora.'),
    'no_steam': ('Steam на этом компьютере не найден; установки вне Steam NEO не отслеживает.',
                 'Steam was not found on this computer; NEO does not track installs outside Steam.',
                 'Steam wurde auf diesem Computer nicht gefunden; Installationen außerhalb von Steam verfolgt NEO nicht.',
                 'Steam est introuvable sur cet ordinateur ; NEO ne suit pas les installations hors de Steam.',
                 'No se encontró Steam en este ordenador; NEO no sigue las instalaciones fuera de Steam.',
                 'Steam non è stato trovato su questo computer; NEO non segue le installazioni fuori da Steam.',
                 'O Steam não foi encontrado neste computador; o NEO não acompanha instalações fora do Steam.'),
}


def text(key: str, code: str, **values) -> str:
    return _TEXT[key][_CODES.index(code) if code in _CODES else 1].format(**values)


@lru_cache(maxsize=7)
def _patterns(code: str):
    whole = lambda table: re.compile(r'^(?:' + table[code] + r')$', re.I)
    return whole(_SPIKES), whole(_INSTALLS), whole(_EVIDENCE)


def parse(raw: str, code: str) -> tuple[str, str] | None:
    """('spikes'|'installs'|'evidence', app) for a whole status question, else None."""
    from jarvis.runtime.natural_commands import strip_request_shell
    if code not in _CODES:
        return None
    value = re.sub(r'\s+', ' ', strip_request_shell(str(raw or ''), code)).strip(' .!?¿¡').replace('ё', 'е')
    if not value or len(value) > 120:
        return None
    spikes, installs, evidence = _patterns(code)
    if spikes.fullmatch(value):
        return 'spikes', ''
    now = re.fullmatch(r'(?:' + _DISCORD_NOW[code] + r')', value, re.I)
    if now:
        query = (now.group('q') or now.group('q2') or '').strip(' «»"“”')
        original = re.sub(r'\s+', ' ', str(raw or ''))
        at = original.casefold().find(query.casefold())
        return 'discord_now', original[at:at + len(query)] if at >= 0 else query
    if evidence.fullmatch(value):
        return 'evidence', ''
    found = installs.fullmatch(value)
    if found:
        app = (found.groupdict().get('app') or '').strip(' «»"“”')
        return 'installs', '' if re.fullmatch(r'(?:в\s+|in\s+)?(?:steam|стим|стиме)', app, re.I) else app
    return None


def steam_installing(libraries: list[Path]) -> list[dict]:
    """Incomplete Steam manifests right now (read-only, same fields as the HREQ-021 watcher)."""
    from jarvis.runtime.system_watch188 import STEAM_FULLY_INSTALLED, parse_acf
    rows: list[dict] = []
    for library in libraries:
        try:
            manifests = list(library.glob('appmanifest_*.acf'))[:2000]
        except OSError:
            continue
        for manifest in manifests:
            try:
                data = parse_acf(manifest.read_text(encoding='utf-8', errors='replace'))
                flags = int(data.get('StateFlags', '0') or 0)
                need = int(data.get('BytesToDownload', '0') or 0)
                got = int(data.get('BytesDownloaded', '0') or 0)
            except (OSError, ValueError):
                continue
            if flags == STEAM_FULLY_INSTALLED and got >= need:
                continue
            percent = int(min(100, max(0, got * 100 // need))) if need > 0 else None
            rows.append({'name': data.get('name') or manifest.stem, 'percent': percent})
    return rows


def installs_answer(code: str, app: str = '', *, libraries: list[Path] | None = None) -> str:
    if libraries is None:
        from jarvis.runtime.system_watch188 import steam_libraries, steam_root
        root = steam_root()
        if root is None:
            return text('no_steam', code)
        libraries = steam_libraries(root)
    rows = steam_installing(libraries)
    if app:
        wanted = app.casefold()
        rows = [row for row in rows if wanted in str(row['name']).casefold()]
        if not rows:
            return text('app_none', code, app=app[:60])
    if not rows:
        return text('none', code)
    listed = ', '.join(f"{row['name']} — {row['percent']}%" if row['percent'] is not None else str(row['name']) for row in rows[:8])
    return text('list', code, rows=listed)
