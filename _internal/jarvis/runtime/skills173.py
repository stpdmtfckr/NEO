"""NEO 173: phrases → exact app skills, «это / туда / тот файл», and verified chains.

Returns what to run — a tool and its arguments — so every action still goes through the
agent's usual confirmation, ownership and undo rules. «Перемести это туда» becomes the
ordinary filesystem move with full paths; «скачай … и распакуй» becomes the checked chain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


def _n(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').casefold().replace('ё', 'е')).strip(' .,!?:;¿¡«»"\'')


@dataclass(slots=True)
class Plan:
    tool: str = ''
    args: dict[str, Any] = field(default_factory=dict)
    text: str = ''             # an answer instead of an action (e.g. «какой файл?»)
    detail: str = ''


_URL = re.compile(r'https?://[^\s"\'<>«»]+', re.I)
_DOWNLOAD = r'(?:скачай|загрузи|download|lade(?:\s+herunter)?|t[eé]l[eé]charge|descarga|scarica|transfere|baixa)'
_UNPACK = r'(?:распакуй|разархивируй|извлеки|unpack|unzip|extract|entpacke|d[eé]compresse|extrais|descomprime|extrae|estrai|decomprimi|extrai|descompacta)'
_OPEN_IT = r'(?:открой(?:\s+(?:его|е[её]|папку|результат))?|покажи(?:\s+(?:его|е[её]))?|open(?:\s+it|\s+the\s+folder)?|show\s+it|[oö]ffne(?:\s+ihn|\s+es)?|ouvre(?:-le)?|abre(?:lo)?|apri(?:lo)?|abre-o)'
_AND = r'(?:\s*,\s*|\s+(?:и|затем|потом|and|then|und|dann|et|puis|y|luego|e|poi|depois)\s+|\s+)'
_TO = {
    'there': r'(?:туда|в\s+(?:эту|ту|ту\s+же)\s+папку|сюда|there|here|into\s+(?:this|that)\s+folder|dorthin|hierher|l[aà]-bas|ici|all[ií]|aqu[ií]|l[iì]|qui|a[ií]|aqui)',
    'desktop': r'(?:на\s+рабочий\s+стол|to\s+the\s+desktop|auf\s+den\s+desktop|sur\s+le\s+bureau|al\s+escritorio|sulla\s+scrivania|para\s+o\s+ambiente\s+de\s+trabalho)',
    'downloads': r'(?:в\s+загрузки|to\s+downloads|in\s+downloads|dans\s+t[eé]l[eé]chargements|a\s+descargas|in\s+download|para\s+transfer[eê]ncias)',
    'documents': r'(?:в\s+документы|to\s+documents|in\s+dokumente|dans\s+documents|a\s+documentos|in\s+documenti|para\s+documentos)',
}
_ITEM = (r'(?:это|этот\s+файл|эту\s+папку|его|е[её]|тот\s+файл|последний\s+файл|скачанн\w+\s+файл|выделенн\w*|this(?:\s+file)?|it|that\s+file|the\s+downloaded\s+file|'
         r'das|diese\s+datei|die\s+heruntergeladene\s+datei|[cç]a|ce\s+fichier|le\s+fichier\s+t[eé]l[eé]charg[eé]|esto|este\s+archivo|el\s+archivo\s+descargado|'
         r'questo(?:\s+file)?|il\s+file\s+scaricato|isto|este\s+ficheiro|o\s+ficheiro\s+transferido)')
_ARCHIVE = r'(?:(?:этот|тот|скачанн\w+|последн\w+)\s+архив|архив|the\s+archive|this\s+archive|das\s+archiv|l[\'’]archive|el\s+archivo\s+comprimido|l[\'’]archivio|o\s+arquivo)'
_EXECUTABLE = {'.exe', '.msi', '.bat', '.cmd', '.ps1', '.vbs', '.scr', '.com', '.jar', '.lnk'}

_TEXT = {
    'which_file': ('Не понял, какой файл имеется в виду: выделите его в Проводнике или назовите.', 'I’m not sure which file you mean: select it in File Explorer or name it.',
                   'Welche Datei ist gemeint? Markiere sie im Explorer oder nenne sie.', 'Quel fichier ? Sélectionnez-le dans l’Explorateur ou nommez-le.',
                   '¿Qué archivo? Selecciónalo en el Explorador o dime su nombre.', 'Quale file? Selezionalo in Esplora file o dimmi il nome.',
                   'Que ficheiro? Seleciona-o no Explorador ou diz o nome.'),
    'which_folder': ('Не понял, куда: откройте нужную папку в Проводнике или назовите её («на рабочий стол», «в документы»).',
                     'I’m not sure where to: open the folder in File Explorer or name it (“to the desktop”, “to documents”).',
                     'Wohin? Öffne den Ordner im Explorer oder nenne ihn („auf den Desktop“).', 'Où ? Ouvrez le dossier dans l’Explorateur ou nommez-le (« sur le bureau »).',
                     '¿A dónde? Abre la carpeta en el Explorador o nómbrala («al escritorio»).', 'Dove? Apri la cartella in Esplora file o nominala («sulla scrivania»).',
                     'Para onde? Abre a pasta no Explorador ou diz o nome («para o ambiente de trabalho»).'),
    'program': ('«{name}» — это программа. По словам «это»/«тот файл» я программы не запускаю: показал её в Проводнике.',
                '“{name}” is a program. I don’t run programs from “this”/“that file”: I showed it in File Explorer.',
                '„{name}“ ist ein Programm. Über „das“/„diese Datei“ starte ich keine Programme: im Explorer gezeigt.',
                '« {name} » est un programme. Je ne lance pas de programme sur « ça » : affiché dans l’Explorateur.',
                '«{name}» es un programa. No ejecuto programas con «esto»: lo mostré en el Explorador.',
                '«{name}» è un programma. Non avvio programmi con «questo»: mostrato in Esplora file.',
                '«{name}» é um programa. Não executo programas com «isto»: mostrei-o no Explorador.'),
    'no_url': ('Что скачать? Скажите адрес (https://…) или скопируйте ссылку и скажите «скачай ссылку из буфера».',
               'What should I download? Say the address (https://…) or copy the link and say “download the link from the clipboard”.',
               'Was soll ich laden? Sag die Adresse (https://…) oder kopiere den Link.', 'Que télécharger ? Dites l’adresse (https://…) ou copiez le lien.',
               '¿Qué descargo? Di la dirección (https://…) o copia el enlace.', 'Cosa scarico? Di’ l’indirizzo (https://…) o copia il link.',
               'O que transfiro? Diz o endereço (https://…) ou copia a ligação.'),
}


def t(key: str, code: str, /, **values: Any) -> str:
    return _pick(_TEXT[key], code).format(**values)


def _clipboard_url() -> str:
    try:
        from jarvis.runtime import clipboard168
        content = clipboard168.read()
        match = _URL.search(content.text) if content.kind == 'text' else None
        return match.group(0) if match else ''
    except Exception:
        return ''


_SKILL_ONLY_FOLDERS = frozenset({'this_pc', 'recycle'})


def plan(text: str, code: str, config: Any, *, clipboard_url=_clipboard_url, context=None) -> Plan | None:
    from jarvis.runtime import context173
    from jarvis.tools import apps_tool173
    context = context or context173
    raw = ' '.join(str(text or '').split())
    t_ = _n(raw)
    if not t_:
        return None

    # 1. Chains: «скачай <url>, распакуй и открой», «распакуй это и открой»
    url_match = _URL.search(raw)
    if re.match(_DOWNLOAD + r'\b', t_):
        rest = t_
        steps = ['download']
        if re.search(_UNPACK, rest):
            steps.append('unpack')
        if re.search(_AND + _OPEN_IT + r'$', rest):
            steps.append('open')
        url = url_match.group(0).rstrip('.,;)') if url_match else ''
        if not url and re.search(r'(?:из\s+буфера|ссылк\w*|from\s+the\s+clipboard|the\s+link|link)', t_):
            url = clipboard_url()
        if url_match or url or len(steps) > 1:
            if not url:
                return Plan(text=t('no_url', code), detail='chain.no_url')
            return Plan('apps', {'action': 'chain', 'steps': ','.join(steps), 'url': url}, detail='chain.' + '_'.join(steps))
    unpack = re.fullmatch(rf'{_UNPACK}\s+(?P<what>{_ITEM}|{_ARCHIVE})(?P<open>{_AND}{_OPEN_IT})?', t_)
    if unpack:
        found = context.resolve_file('downloaded' if re.search(r'скачанн|download|t[eé]l[eé]charg|descarg|scaricat|transferid', unpack.group('what'))
                                     else context.which_file(unpack.group('what')), config)
        if found is None:
            return Plan(text=t('which_file', code), detail='context.which_file')
        steps = ['unpack'] + (['open'] if unpack.group('open') else [])
        return Plan('apps', {'action': 'chain', 'steps': ','.join(steps), 'source': found.path}, detail='chain.' + '_'.join(steps))

    # 2. «перемести/скопируй это туда», «открой тот файл», «покажи его в проводнике»
    move = re.fullmatch(rf'(?P<verb>перемести|переложи|скопируй|move|copy|verschiebe|kopiere|d[eé]place|copie|mueve|copia|sposta|copia|move|copia)\s+'
                        rf'(?P<what>{_ITEM})\s+(?P<to>{"|".join(_TO.values())})', t_)
    if move:
        found = context.resolve_file(context.which_file(move.group('what')), config)
        if found is None:
            return Plan(text=t('which_file', code), detail='context.which_file')
        where = move.group('to')
        if re.fullmatch(_TO['there'], where):
            folder = context.resolve_folder(config)
            if folder is None or Path(folder.path) == Path(found.path).parent:
                return Plan(text=t('which_folder', code), detail='context.which_folder')
            destination = folder.path
        else:
            key = next(k for k, pattern in _TO.items() if k != 'there' and re.fullmatch(pattern, where))
            destination = str(apps_tool173.known_folder(key))
        action = 'copy' if re.match(r'(?:скопируй|copy|kopiere|copie|copia)', move.group('verb')) else 'move'
        return Plan('filesystem', {'action': action, 'source': found.path, 'destination': destination}, detail=f'context.{action}')
    show = re.fullmatch(rf'(?:покажи|где|show|where\s+is|zeig|wo\s+ist|montre|o[uù]\s+est|muestra|d[oó]nde\s+est[aá]|mostra|dov[\'’]?[eè]|onde\s+est[aá])\s+(?P<what>{_ITEM})(?:\s+(?:в\s+проводнике|in\s+(?:file\s+)?explorer|im\s+explorer|dans\s+l[\'’]explorateur|en\s+el\s+explorador|in\s+esplora\s+file|no\s+explorador))?', t_)
    if show and (re.search(r'(?:где|where|wo|o[uù]|d[oó]nde|dov|onde)', t_) or 'проводник' in t_ or 'explor' in t_):
        found = context.resolve_file(context.which_file(show.group('what')), config)
        if found is None:
            return Plan(text=t('which_file', code), detail='context.which_file')
        return Plan('filesystem', {'action': 'reveal', 'source': found.path}, detail='context.reveal')
    open_it = re.fullmatch(rf'(?:открой|запусти|open|[oö]ffne|ouvre|abre|apri|abre)\s+(?P<what>тот\s+файл|последний\s+файл|скачанн\w+\s+файл|that\s+file|the\s+last\s+file|the\s+downloaded\s+file|'
                           rf'die\s+letzte\s+datei|die\s+heruntergeladene\s+datei|le\s+dernier\s+fichier|le\s+fichier\s+t[eé]l[eé]charg[eé]|el\s+[uú]ltimo\s+archivo|el\s+archivo\s+descargado|'
                           rf'l[\'’]ultimo\s+file|il\s+file\s+scaricato|o\s+[uú]ltimo\s+ficheiro|o\s+ficheiro\s+transferido)', t_)
    if open_it:
        found = context.resolve_file(context.which_file(open_it.group('what')), config)
        if found is None:
            return Plan(text=t('which_file', code), detail='context.which_file')
        if Path(found.path).suffix.casefold() in _EXECUTABLE:
            return Plan('filesystem', {'action': 'reveal', 'source': found.path}, text=t('program', code, name=Path(found.path).name),
                        detail='context.program_revealed')
        return Plan('filesystem', {'action': 'open', 'source': found.path}, detail='context.open')

    # NEO 178: every Windows Settings page and its switches; Word / Excel / PowerPoint documents
    from jarvis.runtime import office178, winsettings178
    setting = winsettings178.parse(raw, code)
    if setting is not None:
        return Plan(setting[0], setting[1], detail=f"winsettings.{setting[1].get('action')}")
    document = office178.parse(raw, code)
    if (document is not None and document[1].get('action') == 'read'
            and not document[1].get('name') and not document[1].get('kind')):
        # "Read this file" refers to the selected/recent file, which need not
        # be an Office document. Resolve it before Office's own document fallback.
        found = context.resolve_file(context.which_file(raw), config)
        if found is not None:
            if Path(found.path).suffix.casefold() not in office178.SUFFIX_KIND:
                return Plan('filesystem', {'action': 'read_text', 'source': found.path}, detail='context.read_text')
            document = (document[0], {**document[1], 'name': found.path})
    if document is not None and office178.claims(document[1]):
        arguments = {k: v for k, v in document[1].items() if k != 'soft'}
        return Plan(document[0], arguments, detail=f"office.{arguments.get('action')}")

    # 3. Exact app skills
    skill = apps_tool173.parse(raw, code)
    if skill is not None:
        # Standard user folders already have a native route (desktop.open) with its
        # own contracts; the skill only adds folders that route does not know.
        if skill[1].get('action') == 'folder' and skill[1].get('folder') not in _SKILL_ONLY_FOLDERS:
            return None
        return Plan(skill[0], skill[1], detail=f"skill.{skill[1].get('action')}")
    return None


__all__ = ['Plan', 'plan', 't']
