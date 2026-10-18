"""NEO 172: «разбери загрузки», «найди большие файлы» — a plan first, then only after «да», always undoable.

* ``plan`` sorts the loose files of one folder (Downloads, Desktop, Documents) into subfolders
  by kind: Изображения, Документы, Архивы, Установщики, Видео, Музыка, Прочее. Nothing
  is moved yet; the plan is shown with counts and examples.
* ``apply`` moves the files (never overwriting: a name clash gets « (2)») and writes a
  journal next to NEO's data, so «верни как было» works even after a restart.
* ``undo`` moves every file back and removes subfolders that became empty.
* ``big_files`` lists the largest files in the user's folders. It never deletes anything.

Folders that are not loose files (subfolders, shortcuts in use, partial downloads,
hidden and system files) are left where they are.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any

_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
PENDING_TTL = 180.0


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


KINDS: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    'images': (('Изображения', 'Images', 'Bilder', 'Images', 'Imágenes', 'Immagini', 'Imagens'),
               frozenset({'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic', '.tif', '.tiff', '.svg', '.ico', '.raw', '.psd'})),
    'documents': (('Документы', 'Documents', 'Dokumente', 'Documents', 'Documentos', 'Documenti', 'Documentos'),
                  frozenset({'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp', '.txt', '.rtf', '.csv',
                             '.md', '.epub', '.fb2', '.djvu', '.xml', '.json'})),
    'archives': (('Архивы', 'Archives', 'Archive', 'Archives', 'Archivos comprimidos', 'Archivi', 'Arquivos compactados'),
                 frozenset({'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.iso'})),
    'installers': (('Установщики', 'Installers', 'Installationsprogramme', 'Installateurs', 'Instaladores', 'Programmi di installazione', 'Instaladores'),
                   frozenset({'.exe', '.msi', '.msix', '.appx', '.appxbundle', '.bat', '.cmd'})),
    'video': (('Видео', 'Videos', 'Videos', 'Vidéos', 'Vídeos', 'Video', 'Vídeos'),
              frozenset({'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.webm', '.flv', '.m4v'})),
    'music': (('Музыка', 'Music', 'Musik', 'Musique', 'Música', 'Musica', 'Música'),
              frozenset({'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.opus'})),
    'other': (('Прочее', 'Other', 'Sonstiges', 'Divers', 'Otros', 'Altro', 'Outros'), frozenset()),
}
_ORDER = ('images', 'documents', 'archives', 'installers', 'video', 'music', 'other')
_PARTIAL = frozenset({'.crdownload', '.part', '.partial', '.download', '.tmp', '.opdownload'})
_SKIP_NAMES = frozenset({'desktop.ini', 'thumbs.db', '.ds_store'})
_KEEP_ON_DESKTOP = frozenset({'.lnk', '.url'})


def kind_of(path: Path) -> str:
    suffix = path.suffix.casefold()
    for name in _ORDER:
        if suffix in KINDS[name][1]:
            return name
    return 'other'


def folder_names(code: str) -> dict[str, str]:
    return {name: _pick(KINDS[name][0], code) for name in _ORDER}


# ---- which folder -----------------------------------------------------------------------------

_FOLDER_WORDS = {
    'downloads': re.compile(r'(?:загрузк\w*|скачанн\w*|downloads?|download-ordner|téléchargements|descargas|scaricati|download|transferências)', re.I),
    'desktop': re.compile(r'(?:рабоч\w+\s+стол\w*|desktop|schreibtisch|bureau|escritorio|scrivania|área\s+de\s+trabalho|ambiente\s+de\s+trabalho)', re.I),
    'documents': re.compile(r'(?:документ\w*|documents?|dokumente|documentos|documenti)', re.I),
}


def known_folder(key: str) -> Path:
    """The user's Downloads / Desktop / Documents (Windows known folders, also when moved to another drive)."""
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            import uuid
            ids = {'downloads': '{374DE290-123F-4565-9164-39C4925E467B}', 'desktop': '{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}',
                   'documents': '{FDD39AD0-238F-46AF-ADB4-6C85480369C7}'}
            guid = uuid.UUID(ids[key])

            class GUID(ctypes.Structure):
                _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]
            value = GUID(guid.fields[0], guid.fields[1], guid.fields[2], (ctypes.c_ubyte * 8)(*guid.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(value), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
    return Path.home() / {'downloads': 'Downloads', 'desktop': 'Desktop', 'documents': 'Documents'}[key]


def which_folder(text: str) -> str:
    low = str(text or '').casefold().replace('ё', 'е')
    for key, pattern in _FOLDER_WORDS.items():
        if pattern.search(low):
            return key
    return 'downloads'


# ---- plan / apply / undo ------------------------------------------------------------------------

@dataclass(slots=True)
class Move:
    source: str
    target: str


@dataclass(slots=True)
class Plan:
    folder: str
    moves: list[Move] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)
    skipped: int = 0


def _loose_files(folder: Path, desktop: bool) -> tuple[list[Path], int]:
    files, skipped = [], 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return [], 0
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        path = Path(entry.path)
        name = path.name.casefold()
        if name in _SKIP_NAMES or name.startswith(('.', '~$')) or path.suffix.casefold() in _PARTIAL:
            skipped += 1
            continue
        if desktop and path.suffix.casefold() in _KEEP_ON_DESKTOP:
            skipped += 1                   # shortcuts stay on the desktop
            continue
        if os.name == 'nt':
            try:
                attributes = entry.stat(follow_symlinks=False).st_file_attributes   # type: ignore[attr-defined]
                if attributes & 0x2 or attributes & 0x4:                           # hidden / system
                    skipped += 1
                    continue
            except (OSError, AttributeError):
                pass
        files.append(path)
    return sorted(files, key=lambda p: p.name.casefold()), skipped


def _free_name(target: Path, taken: set[str]) -> Path:
    candidate, n = target, 2
    while candidate.exists() or str(candidate).casefold() in taken:
        candidate = target.with_name(f'{target.stem} ({n}){target.suffix}')
        n += 1
    return candidate


def plan(folder: Path, code: str, *, desktop: bool = False) -> Plan:
    folder = Path(folder)
    names = folder_names(code)
    result = Plan(str(folder))
    files, result.skipped = _loose_files(folder, desktop)
    taken: set[str] = set()
    for path in files:
        kind = kind_of(path)
        target = _free_name(folder / names[kind] / path.name, taken)
        taken.add(str(target).casefold())
        result.moves.append(Move(str(path), str(target)))
        result.counts[kind] = result.counts.get(kind, 0) + 1
        examples = result.examples.setdefault(kind, [])
        if len(examples) < 2:
            examples.append(path.name)
    return result


def journal_path(data_root: Path) -> Path:
    return Path(data_root) / 'tidy' / 'journal.json'


_JOURNAL_LOCK = threading.RLock()
_MISSING = {'ru': 'нет на месте', 'en': 'not in place', 'de': 'nicht vorhanden', 'fr': 'introuvable', 'es': 'no está',
            'it': 'non presente', 'pt': 'não está no lugar'}
UNDO_ATTEMPTS = 3          # HIST-015: a row that cannot be put back is retried a bounded number of times


def _write_journal(path: Path, history: list[dict[str, Any]]) -> bool:
    """Atomically replace the journal (own temp file, one retry on a sharing violation).

    A failed replace leaves its temp file behind on purpose: _read_journal()
    adopts the newest intact temp, so the newest intended state is never lost."""
    import uuid
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.stem}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp')
    try:
        temporary.write_text(json.dumps(history[-10:], ensure_ascii=False, indent=1), encoding='utf-8')
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False
    for attempt in range(2):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt:
                return False
            time.sleep(0.05)
        except OSError:
            return False
    for stale in path.parent.glob(f'{path.stem}*.tmp'):
        try:
            stale.unlink()                       # older intended states are superseded now
        except OSError:
            pass
    return True


def _parse_journal(path: Path) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _read_journal(path: Path) -> list[dict[str, Any]]:
    """The journal, or a newer intact temp left by a failed replace (HIST-014)."""
    main = _parse_journal(path)
    try:
        main_time = path.stat().st_mtime if main is not None else -1.0
    except OSError:
        main_time = -1.0
    best, best_time = main, main_time
    for candidate in path.parent.glob(f'{path.stem}*.tmp') if path.parent.is_dir() else ():
        data = _parse_journal(candidate)
        try:
            stamp = candidate.stat().st_mtime
        except OSError:
            continue
        if data is not None and stamp >= best_time:
            best, best_time = data, stamp
    if best is not main and best is not None:
        _write_journal(path, best)               # promote the recovered state
    return best or []


def apply(p: Plan, data_root: Path, code: str = 'ru') -> tuple[int, list[str]]:
    """Move the files of a plan. Returns (moved, errors). The journal allows «верни как было».

    HIST-014: the journal entry is written BEFORE any file moves (status «moving»,
    with the resolved targets) and completed afterwards, so a failed final write
    still leaves undo a record of every move that may have happened."""
    import uuid
    errors: list[str] = []
    rows: list[dict[str, str]] = []
    taken: set[str] = set()
    for move in p.moves:
        source, target = Path(move.source), Path(move.target)
        if not source.is_file():
            continue
        if target.exists() or str(target).casefold() in taken:
            target = _free_name(target, taken)
        taken.add(str(target).casefold())
        rows.append({'source': str(source), 'target': str(target)})
    if not rows:
        return 0, errors
    path = journal_path(data_root)
    entry = {'id': uuid.uuid4().hex, 'time': datetime.now().isoformat(timespec='seconds'), 'folder': p.folder,
             'status': 'moving', 'moved': rows}
    with _JOURNAL_LOCK:
        history = _read_journal(path)
        history.append(entry)
        if not _write_journal(path, history):
            # Without a recoverable record nothing is moved at all.
            return 0, [t('journal_failed', code)]
        moved: list[dict[str, str]] = []
        for row in rows:
            source, target = Path(row['source']), Path(row['target'])
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    errors.append(f'{source.name}: {target.name} уже существует')
                    continue
                shutil.move(str(source), str(target))
                moved.append(row)
            except OSError as exc:
                errors.append(f'{source.name}: {exc.strerror or exc}')
        entry['status'], entry['moved'] = 'done', moved
        if not moved:
            history.remove(entry)
        if not _write_journal(path, history) and moved:
            errors.append(t('journal_failed', code))   # the «moving» record still covers these files
    return len(moved), errors


def undo(data_root: Path, code: str = 'ru') -> tuple[int, list[str], str]:
    """Move back the files of the last tidy-up. Returns (restored, errors, folder).

    HIST-015: a row is dropped from the journal only after its file is proven back
    in place; rows that failed stay with their reason and are retried by the next
    «верни как было» (at most UNDO_ATTEMPTS times)."""
    path = journal_path(data_root)
    with _JOURNAL_LOCK:
        history = _read_journal(path)
        open_entries = [row for row in history if row.get('status') != 'abandoned']
        if not open_entries:
            return 0, [], ''
        last = open_entries[-1]
        restored, errors, parents = 0, [], set()
        remaining: list[dict[str, Any]] = []
        for row in reversed(last.get('moved') or []):
            source, target = Path(row['source']), Path(row['target'])
            if not target.is_file():
                if source.is_file() and last.get('status') == 'moving':
                    parents.add(target.parent)   # this move never happened; only its empty folder goes
                    continue
                errors.append(f'{target.name}: ' + _MISSING.get(_code(code), _MISSING['ru']))
                remaining.append({**row, 'reason': 'missing'})
                continue
            try:
                back = source if not source.exists() else _free_name(source, set())
                shutil.move(str(target), str(back))
                restored += 1
                parents.add(target.parent)
            except OSError as exc:
                errors.append(f'{target.name}: {exc.strerror or exc}')
                remaining.append({**row, 'reason': str(exc.strerror or exc)})
        for folder in parents:
            try:
                folder.rmdir()           # only when it became empty
            except OSError:
                pass
        if remaining:
            last['moved'] = list(reversed(remaining))
            last['attempts'] = int(last.get('attempts', 0) or 0) + 1
            last['status'] = 'abandoned' if last['attempts'] >= UNDO_ATTEMPTS else 'partial'
        else:
            history.remove(last)
        _write_journal(path, history)
    return restored, errors, str(last.get('folder') or '')


def has_pending_undo(data_root: Path) -> bool:
    """True while the last tidy-up still has rows that «верни как было» can retry."""
    with _JOURNAL_LOCK:
        return any(row.get('status') != 'abandoned' for row in _read_journal(journal_path(data_root)))


# ---- big files ---------------------------------------------------------------------------------

_SKIP_DIRS = frozenset({'appdata', '$recycle.bin', 'node_modules', '.git', '.cache', 'windows', 'program files', 'program files (x86)'})


def big_files(folders: list[Path], *, top: int = 10, min_size: int = 50 * 1024 * 1024, budget: int = 200_000,
              deadline: float = 8.0) -> list[tuple[int, str]]:
    """The largest files (size, path) in these folders, walking at most ``budget`` entries or ``deadline`` seconds."""
    found: list[tuple[int, str]] = []
    seen = 0
    started = time.monotonic()
    for root in folders:
        stack = [Path(root)]
        while stack and seen < budget and time.monotonic() - started < deadline:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                seen += 1
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() not in _SKIP_DIRS and not entry.name.startswith('.'):
                            stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat(follow_symlinks=False).st_size
                        if size >= min_size:
                            found.append((size, entry.path))
                except OSError:
                    continue
    found.sort(reverse=True)
    return found[:top]


def size_text(size: int, code: str) -> str:
    units = {'ru': ('КБ', 'МБ', 'ГБ'), 'de': ('KB', 'MB', 'GB'), 'fr': ('Ko', 'Mo', 'Go'), 'pt': ('KB', 'MB', 'GB')}.get(_code(code), ('KB', 'MB', 'GB'))
    value, unit = size / 1024, units[0]
    for next_unit in units[1:]:
        if value < 1024:
            break
        value, unit = value / 1024, next_unit
    text = f'{value:.1f}' if value < 100 else f'{value:.0f}'
    return (text.replace('.', ',') if _code(code) in {'ru', 'de', 'fr', 'es', 'it', 'pt'} else text) + ' ' + unit


# ---- texts and pending -------------------------------------------------------------------------

_TEXT = {
    'plan': ('Вот план для папки «{folder}» ({total}). Ничего ещё не перемещено:\n{rows}\n{skipped}Разложить так? Ответьте «да». Потом можно сказать «верни как было».',
             'Here is the plan for “{folder}” ({total}). Nothing has been moved yet:\n{rows}\n{skipped}Sort it like this? Say “yes”. You can say “put it back” later.',
             'So würde ich „{folder}“ ordnen ({total}). Noch ist nichts verschoben:\n{rows}\n{skipped}So sortieren? Sag „ja“. Später geht „mach es rückgängig“.',
             'Voici le plan pour « {folder} » ({total}). Rien n’est encore déplacé :\n{rows}\n{skipped}Je range ainsi ? Dites « oui ». Ensuite, « remets comme avant » annule.',
             'Este es el plan para «{folder}» ({total}). Aún no se ha movido nada:\n{rows}\n{skipped}¿Lo ordeno así? Di «sí». Luego puedes decir «déjalo como estaba».',
             'Ecco il piano per «{folder}» ({total}). Non ho ancora spostato nulla:\n{rows}\n{skipped}Riordino così? Di’ «sì». Poi puoi dire «rimetti com’era».',
             'Este é o plano para «{folder}» ({total}). Ainda nada foi movido:\n{rows}\n{skipped}Organizo assim? Diz «sim». Depois podes dizer «volta como estava».'),
    'row': ('• {name}: {n} — {examples}', '• {name}: {n} — {examples}', '• {name}: {n} — {examples}', '• {name} : {n} — {examples}',
            '• {name}: {n} — {examples}', '• {name}: {n} — {examples}', '• {name}: {n} — {examples}'),
    'skipped': ('Не трогаю: {n} (ярлыки, недокачанные, скрытые и системные файлы).\n', 'Left alone: {n} (shortcuts, partial downloads, hidden and system files).\n',
                'Bleibt liegen: {n} (Verknüpfungen, unfertige Downloads, versteckte und Systemdateien).\n',
                'Non touchés : {n} (raccourcis, téléchargements partiels, fichiers cachés et système).\n',
                'No toco: {n} (accesos directos, descargas parciales, archivos ocultos y de sistema).\n',
                'Non tocco: {n} (collegamenti, download parziali, file nascosti e di sistema).\n',
                'Não mexo: {n} (atalhos, transferências parciais, ficheiros ocultos e de sistema).\n'),
    'empty': ('В папке «{folder}» нет файлов, которые нужно разложить.', 'There are no loose files to sort in “{folder}”.',
              'In „{folder}“ gibt es keine losen Dateien zum Ordnen.', 'Il n’y a pas de fichiers à ranger dans « {folder} ».',
              'No hay archivos sueltos que ordenar en «{folder}».', 'Non ci sono file sparsi da riordinare in «{folder}».',
              'Não há ficheiros soltos para organizar em «{folder}».'),
    'done': ('Готово: разложил {n} файлов по папкам в «{folder}». Сказать «верни как было» — и всё вернётся.',
             'Done: sorted {n} files into folders in “{folder}”. Say “put it back” to undo.',
             'Fertig: {n} Dateien in „{folder}“ in Ordner sortiert. „Mach es rückgängig“ stellt alles wieder her.',
             'C’est fait : {n} fichiers rangés dans des dossiers de « {folder} ». « Remets comme avant » annule.',
             'Hecho: ordené {n} archivos en carpetas dentro de «{folder}». Di «déjalo como estaba» para deshacer.',
             'Fatto: ho riordinato {n} file in cartelle in «{folder}». Di’ «rimetti com’era» per annullare.',
             'Feito: organizei {n} ficheiros em pastas em «{folder}». Diz «volta como estava» para desfazer.'),
    'errors': (' Не получилось с {n}: {items}.', ' Failed for {n}: {items}.', ' Fehlgeschlagen bei {n}: {items}.', ' Échec pour {n} : {items}.',
               ' Falló con {n}: {items}.', ' Non riuscito per {n}: {items}.', ' Falhou em {n}: {items}.'),
    'undone': ('Вернул {n} файлов на прежние места в «{folder}».', 'Put {n} files back where they were in “{folder}”.',
               '{n} Dateien in „{folder}“ zurückgelegt.', '{n} fichiers remis à leur place dans « {folder} ».',
               'Devolví {n} archivos a su sitio en «{folder}».', 'Ho rimesso {n} file al loro posto in «{folder}».',
               'Voltei a pôr {n} ficheiros no lugar em «{folder}».'),
    'journal_failed': ('журнал «верни как было» не удалось сохранить на диск', 'the “put it back” journal could not be saved to disk',
                       'das Rückgängig-Journal konnte nicht gespeichert werden', 'le journal « remets comme avant » n’a pas pu être enregistré',
                       'no se pudo guardar el registro de «déjalo como estaba»', 'non è stato possibile salvare il registro «rimetti com’era»',
                       'não foi possível guardar o registo «volta como estava»'),
    'undo_kept': (' Эти файлы я запомнил: повторите «верни как было», когда они снова будут доступны.',
                  ' I kept a record of those files: say “put it back” again once they are available.',
                  ' Diese Dateien habe ich mir gemerkt: Sagen Sie erneut „mach es rückgängig“, sobald sie verfügbar sind.',
                  ' J’ai gardé ces fichiers en mémoire : redites « remets comme avant » quand ils seront disponibles.',
                  ' Guardé esos archivos: repite «déjalo como estaba» cuando vuelvan a estar disponibles.',
                  ' Ho tenuto traccia di quei file: ripeti «rimetti com’era» quando saranno di nuovo disponibili.',
                  ' Guardei esses ficheiros: repete «volta como estava» quando estiverem disponíveis.'),
    'nothing_to_undo': ('Нечего возвращать: я ещё ничего не раскладывал.', 'Nothing to put back: I haven’t sorted anything yet.',
                        'Nichts zurückzulegen: Ich habe noch nichts sortiert.', 'Rien à remettre : je n’ai encore rien rangé.',
                        'Nada que devolver: todavía no he ordenado nada.', 'Niente da rimettere: non ho ancora riordinato nulla.',
                        'Nada para voltar: ainda não organizei nada.'),
    'big': ('Самые большие файлы ({where}):\n{rows}\nЧтобы открыть папку с файлом, скажите «открой папку номер 2». Удаляю только по отдельной команде.',
            'The largest files ({where}):\n{rows}\nSay “open folder number 2” to see one in Explorer. I delete only on a separate command.',
            'Die größten Dateien ({where}):\n{rows}\nMit „öffne Ordner Nummer 2“ zeige ich eine im Explorer. Gelöscht wird nur auf eigenen Befehl.',
            'Les plus gros fichiers ({where}) :\n{rows}\nDites « ouvre le dossier numéro 2 » pour l’afficher. Je ne supprime que sur commande séparée.',
            'Los archivos más grandes ({where}):\n{rows}\nDi «abre la carpeta número 2» para verlo. Solo borro con una orden aparte.',
            'I file più grandi ({where}):\n{rows}\nDi’ «apri la cartella numero 2» per vederlo. Elimino solo con un comando a parte.',
            'Os maiores ficheiros ({where}):\n{rows}\nDiz «abre a pasta número 2» para o ver. Só apago com um comando à parte.'),
    'no_big': ('Файлов больше {size} в {where} не нашёл.', 'No files over {size} in {where}.', 'Keine Dateien über {size} in {where}.',
               'Aucun fichier de plus de {size} dans {where}.', 'No hay archivos de más de {size} en {where}.',
               'Nessun file oltre {size} in {where}.', 'Nenhum ficheiro acima de {size} em {where}.'),
    'opened': ('Открыл папку с файлом «{name}».', 'Opened the folder of “{name}”.', 'Ordner von „{name}“ geöffnet.', 'Dossier de « {name} » ouvert.',
               'Abrí la carpeta de «{name}».', 'Aperta la cartella di «{name}».', 'Abri a pasta de «{name}».'),
    'cancelled': ('Хорошо, ничего не трогаю.', 'OK, I won’t touch anything.', 'Okay, ich fasse nichts an.', 'D’accord, je ne touche à rien.',
                  'De acuerdo, no toco nada.', 'Va bene, non tocco nulla.', 'Está bem, não mexo em nada.'),
    'files': ('файлов: {n}', 'files: {n}', 'Dateien: {n}', 'fichiers : {n}', 'archivos: {n}', 'file: {n}', 'ficheiros: {n}'),
}


def t(key: str, code: str, /, **values: Any) -> str:
    return _pick(_TEXT[key], code).format(**values)


def plan_text(p: Plan, code: str) -> str:
    names = folder_names(code)
    rows = []
    for kind in _ORDER:
        n = p.counts.get(kind, 0)
        if n:
            rows.append(t('row', code, name=names[kind], n=n, examples=', '.join(p.examples.get(kind, []))))
    skipped = t('skipped', code, n=p.skipped) if p.skipped else ''
    return t('plan', code, folder=p.folder, total=t('files', code, n=len(p.moves)), rows='\n'.join(rows), skipped=skipped)


def big_text(rows: list[tuple[int, str]], code: str, where: str) -> str:
    lines = [f'{i}. {Path(path).name} — {size_text(size, code)} ({Path(path).parent})' for i, (size, path) in enumerate(rows, 1)]
    return t('big', code, where=where, rows='\n'.join(lines))


@dataclass(slots=True)
class _State:
    plan: Plan | None = None
    big: list[tuple[int, str]] = field(default_factory=list)
    at: float = 0.0
    owner_scope: str = ''
    question_token: object | None = None


_lock = threading.Lock()
_state = _State()


def remember_plan(p: Plan) -> None:
    global _state
    from jarvis.runtime.conversation_journal import current_scope
    with _lock:
        _state = _State(p, list(_state.big), time.monotonic(), current_scope(), object())


def remember_big(rows: list[tuple[int, str]]) -> None:
    with _lock:
        _state.big = list(rows)
        _state.at = time.monotonic()


def pending_plan() -> Plan | None:
    with _lock:
        if _state.plan is not None and time.monotonic() - _state.at <= PENDING_TTL:
            return _state.plan
    return None


def forget_plan() -> None:
    with _lock:
        _state.plan = None


def owned_question_token() -> object | None:
    """Snapshot a live question created in this conversation; never infer its owner."""
    from jarvis.runtime.conversation_journal import current_scope
    with _lock:
        if (_state.owner_scope == current_scope() and _state.plan is not None
                and time.monotonic() - _state.at <= PENDING_TTL):
            return _state.question_token
    return None


def forget_if_owned(token: object | None) -> bool:
    """Clear only the same owned generation, preserving any replacement question."""
    # The existing lock protects both identity and mutation.
    if token is None:
        return False
    from jarvis.runtime.conversation_journal import current_scope
    with _lock:
        if (_state.owner_scope != current_scope() or _state.question_token is not token
                or not (_state.plan is not None)):
            return False
        _state.plan = None
        return True


def big_row(index: int) -> tuple[int, str] | None:
    with _lock:
        return _state.big[index - 1] if 1 <= index <= len(_state.big) else None


__all__ = ['KINDS', 'Move', 'Plan', 'apply', 'big_files', 'big_row', 'big_text', 'folder_names', 'forget_plan', 'forget_if_owned', 'owned_question_token', 'journal_path', 'has_pending_undo',
           'kind_of', 'known_folder', 'pending_plan', 'plan', 'plan_text', 'remember_big', 'remember_plan', 'size_text', 't',
           'undo', 'which_folder']
