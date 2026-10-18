"""NEO 173: «скачай …, распакуй и открой» — every step is checked, a failure names the step.

Steps:
1. download — only http(s); into Downloads; name from the server or the address; never
   overwrites; the file must exist, be non-empty and have the announced size;
2. unpack — ZIP (built in), or 7z/RAR with 7-Zip if it is installed; into a folder next to the
   archive; paths that would escape that folder are refused; the result must contain files;
3. open — the unpacked folder (or the file) in File Explorer. NEO never runs a downloaded
   program: that stays the user's own click.

Downloaded and unpacked files get the usual Windows «from the internet» mark
(Zone.Identifier), exactly like a browser download, so SmartScreen and Office still warn.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable
from urllib.parse import unquote, urlparse
import zipfile

MAX_BYTES = 4 * 1024 ** 3
_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


class StepFailed(Exception):
    def __init__(self, reason_key: str, **values: Any) -> None:
        super().__init__(reason_key)
        self.reason_key, self.values = reason_key, values


@dataclass(slots=True)
class StepResult:
    step: str
    ok: bool
    text: str = ''
    path: str = ''


@dataclass(slots=True)
class ChainResult:
    steps: list[StepResult] = field(default_factory=list)
    failed_step: str = ''

    @property
    def ok(self) -> bool:
        return not self.failed_step


def mark_from_internet(path: Path, url: str = '') -> None:
    """The Windows «downloaded from the internet» mark, as browsers write it (no-op elsewhere)."""
    if os.name != 'nt':
        return
    try:
        with open(str(path) + ':Zone.Identifier', 'w', encoding='utf-8') as stream:
            stream.write('[ZoneTransfer]\r\nZoneId=3\r\n' + (f'HostUrl={url}\r\n' if url else ''))
    except OSError:
        pass


def _free(path: Path) -> Path:
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f'{path.stem} ({n}){path.suffix}')
        n += 1
    return candidate


def _file_name(url: str, response: Any) -> str:
    disposition = str(getattr(response, 'headers', {}).get('Content-Disposition', '') or '')
    match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I) or re.search(r'filename="?([^";]+)"?', disposition, re.I)
    name = unquote(match.group(1)).strip() if match else unquote(Path(urlparse(url).path).name)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' .')
    return name[:150] or 'download'


def download(url: str, folder: Path, *, session: Any = None, progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] = lambda: False) -> Path:
    if urlparse(url).scheme not in {'http', 'https'}:
        raise StepFailed('bad_url', url=url)
    if session is None:
        from jarvis.runtime import http_pool173
        session = http_pool173.session('downloads')
    folder.mkdir(parents=True, exist_ok=True)
    try:
        response = session.get(url, stream=True, timeout=(10, 60), allow_redirects=True)
    except Exception as exc:
        raise StepFailed('network', error=type(exc).__name__) from exc
    with response:
        if int(getattr(response, 'status_code', 0) or 0) >= 400:
            raise StepFailed('http', status=response.status_code)
        total = int(response.headers.get('Content-Length', 0) or 0)
        if total > MAX_BYTES:
            raise StepFailed('too_big', size=total)
        target = _free(folder / _file_name(url, response))
        partial = target.with_name(target.name + '.part')
        written = 0
        try:
            with open(partial, 'wb') as out:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if cancelled():
                        raise StepFailed('cancelled')
                    if not chunk:
                        continue
                    out.write(chunk)
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise StepFailed('too_big', size=written)
                    if progress:
                        progress(written, total)
        except StepFailed:
            partial.unlink(missing_ok=True)
            raise
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise StepFailed('disk', error=exc.strerror or str(exc)) from exc
        if written == 0 or (total and written != total):
            partial.unlink(missing_ok=True)
            raise StepFailed('incomplete', got=written, total=total)
        partial.replace(target)
    mark_from_internet(target, url)
    return target


def _seven_zip() -> str:
    for candidate in (shutil.which('7z'), r'C:\Program Files\7-Zip\7z.exe', r'C:\Program Files (x86)\7-Zip\7z.exe'):
        if candidate and Path(candidate).is_file():
            return candidate
    return ''


def unpack(archive: Path, *, seven_zip: Callable[[], str] = _seven_zip) -> Path:
    archive = Path(archive)
    if not archive.is_file():
        raise StepFailed('no_archive', name=archive.name)
    target = _free(archive.with_suffix('') if archive.suffix else archive.with_name(archive.name + ' (files)'))
    suffix = archive.suffix.casefold()
    if suffix == '.zip' or zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as z:
                bad = z.testzip()
                if bad is not None:
                    raise StepFailed('corrupt', name=archive.name)
                root = target.resolve()
                for info in z.infolist():
                    destination = (target / info.filename).resolve()
                    if root not in destination.parents and destination != root:
                        raise StepFailed('unsafe_paths', name=archive.name)
                target.mkdir(parents=True)
                z.extractall(target)
        except zipfile.BadZipFile as exc:
            raise StepFailed('corrupt', name=archive.name) from exc
    elif suffix in {'.7z', '.rar', '.tar', '.gz', '.xz', '.bz2'}:
        tool = seven_zip()
        if not tool:
            raise StepFailed('no_7zip', kind=suffix)
        target.mkdir(parents=True)
        # «x» keeps folders; «-o» the output folder; «-y» answers yes to prompts about the target only.
        done = subprocess.run([tool, 'x', str(archive), f'-o{target}', '-y'], capture_output=True, timeout=600,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if done.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            raise StepFailed('corrupt', name=archive.name)
    else:
        raise StepFailed('not_archive', name=archive.name)
    files = [p for p in target.rglob('*') if p.is_file()]
    if not files:
        shutil.rmtree(target, ignore_errors=True)
        raise StepFailed('empty', name=archive.name)
    for path in files:
        mark_from_internet(path)
    return target


def open_in_explorer(path: Path) -> None:
    if os.name != 'nt':
        raise StepFailed('unsupported')
    if Path(path).is_dir():
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(['explorer', '/select,', os.path.normpath(str(path))])


_STEP = {
    'download': ('скачивание', 'download', 'Herunterladen', 'téléchargement', 'descarga', 'download', 'transferência'),
    'unpack': ('распаковка', 'unpacking', 'Entpacken', 'extraction', 'extracción', 'estrazione', 'extração'),
    'open': ('открытие', 'opening', 'Öffnen', 'ouverture', 'apertura', 'apertura', 'abertura'),
}
_REASON = {
    'bad_url': ('это не адрес http(s): «{url}»', 'not an http(s) address: “{url}”', 'keine http(s)-Adresse: „{url}“', 'pas une adresse http(s) : « {url} »',
                'no es una dirección http(s): «{url}»', 'non è un indirizzo http(s): «{url}»', 'não é um endereço http(s): «{url}»'),
    'network': ('сайт не ответил ({error})', 'the site did not answer ({error})', 'die Seite antwortete nicht ({error})', 'le site n’a pas répondu ({error})',
                'el sitio no respondió ({error})', 'il sito non ha risposto ({error})', 'o site não respondeu ({error})'),
    'http': ('сайт ответил ошибкой {status}', 'the site answered with error {status}', 'die Seite meldete Fehler {status}', 'le site a répondu par l’erreur {status}',
             'el sitio respondió con el error {status}', 'il sito ha risposto con l’errore {status}', 'o site respondeu com o erro {status}'),
    'too_big': ('файл больше 4 ГБ', 'the file is over 4 GB', 'die Datei ist größer als 4 GB', 'le fichier dépasse 4 Go', 'el archivo supera 4 GB',
                'il file supera 4 GB', 'o ficheiro tem mais de 4 GB'),
    'incomplete': ('файл скачался не полностью ({got} из {total} байт)', 'the file was not downloaded completely ({got} of {total} bytes)',
                   'die Datei ist unvollständig ({got} von {total} Bytes)', 'le fichier est incomplet ({got} sur {total} octets)',
                   'el archivo está incompleto ({got} de {total} bytes)', 'il file è incompleto ({got} di {total} byte)',
                   'o ficheiro está incompleto ({got} de {total} bytes)'),
    'disk': ('не удалось записать файл: {error}', 'could not write the file: {error}', 'Datei konnte nicht geschrieben werden: {error}',
             'impossible d’écrire le fichier : {error}', 'no se pudo escribir el archivo: {error}', 'impossibile scrivere il file: {error}',
             'não foi possível gravar o ficheiro: {error}'),
    'cancelled': ('остановлено по вашей команде', 'stopped on your command', 'auf deinen Befehl gestoppt', 'arrêté sur votre demande',
                  'detenido por tu orden', 'interrotto su tuo comando', 'parado a teu pedido'),
    'no_archive': ('архива «{name}» нет на месте', 'the archive “{name}” is gone', 'das Archiv „{name}“ fehlt', 'l’archive « {name} » a disparu',
                   'el archivo «{name}» no está', 'l’archivio «{name}» non c’è', 'o arquivo «{name}» não está'),
    'corrupt': ('архив «{name}» повреждён или защищён паролем', 'the archive “{name}” is damaged or password-protected',
                'das Archiv „{name}“ ist beschädigt oder passwortgeschützt', 'l’archive « {name} » est endommagée ou protégée par mot de passe',
                'el archivo «{name}» está dañado o protegido con contraseña', 'l’archivio «{name}» è danneggiato o protetto da password',
                'o arquivo «{name}» está danificado ou protegido por palavra-passe'),
    'unsafe_paths': ('архив «{name}» пытается записать файлы за пределы своей папки — не распаковываю',
                     'the archive “{name}” tries to write outside its folder — not unpacking',
                     'das Archiv „{name}“ will außerhalb seines Ordners schreiben – ich entpacke nicht',
                     'l’archive « {name} » veut écrire hors de son dossier — je ne l’extrais pas',
                     'el archivo «{name}» intenta escribir fuera de su carpeta: no lo extraigo',
                     'l’archivio «{name}» cerca di scrivere fuori dalla sua cartella: non lo estraggo',
                     'o arquivo «{name}» tenta escrever fora da sua pasta — não extraio'),
    'no_7zip': ('для {kind} нужен 7-Zip (7-zip.org) — ZIP распаковываю сам', '{kind} needs 7-Zip (7-zip.org) — ZIP I unpack myself',
                'für {kind} wird 7-Zip benötigt (7-zip.org) – ZIP entpacke ich selbst', '{kind} nécessite 7-Zip (7-zip.org) — le ZIP, je l’extrais moi-même',
                '{kind} necesita 7-Zip (7-zip.org); los ZIP los extraigo yo', '{kind} richiede 7-Zip (7-zip.org); i ZIP li estraggo io',
                '{kind} precisa do 7-Zip (7-zip.org) — ZIP extraio eu'),
    'not_archive': ('«{name}» — не архив', '“{name}” is not an archive', '„{name}“ ist kein Archiv', '« {name} » n’est pas une archive',
                    '«{name}» no es un archivo comprimido', '«{name}» non è un archivio', '«{name}» não é um arquivo compactado'),
    'empty': ('архив «{name}» пустой', 'the archive “{name}” is empty', 'das Archiv „{name}“ ist leer', 'l’archive « {name} » est vide',
              'el archivo «{name}» está vacío', 'l’archivio «{name}» è vuoto', 'o arquivo «{name}» está vazio'),
    'unsupported': ('открыть Проводник можно только в Windows', 'File Explorer is available only on Windows', 'Der Explorer gibt es nur unter Windows',
                    'l’Explorateur n’existe que sous Windows', 'el Explorador solo existe en Windows', 'Esplora file c’è solo su Windows',
                    'o Explorador só existe no Windows'),
}
_TEXT = {
    'downloaded': ('✓ Шаг {n}: скачал «{name}» ({size}) в Загрузки.', '✓ Step {n}: downloaded “{name}” ({size}) to Downloads.',
                   '✓ Schritt {n}: „{name}“ ({size}) in Downloads geladen.', '✓ Étape {n} : « {name} » ({size}) téléchargé dans Téléchargements.',
                   '✓ Paso {n}: descargué «{name}» ({size}) en Descargas.', '✓ Passo {n}: scaricato «{name}» ({size}) in Download.',
                   '✓ Passo {n}: transferi «{name}» ({size}) para Transferências.'),
    'unpacked': ('✓ Шаг {n}: распаковал в папку «{name}» ({count} файлов).', '✓ Step {n}: unpacked into “{name}” ({count} files).',
                 '✓ Schritt {n}: nach „{name}“ entpackt ({count} Dateien).', '✓ Étape {n} : extrait dans « {name} » ({count} fichiers).',
                 '✓ Paso {n}: extraído en «{name}» ({count} archivos).', '✓ Passo {n}: estratto in «{name}» ({count} file).',
                 '✓ Passo {n}: extraído para «{name}» ({count} ficheiros).'),
    'opened': ('✓ Шаг {n}: открыл «{name}» в Проводнике.', '✓ Step {n}: opened “{name}” in File Explorer.', '✓ Schritt {n}: „{name}“ im Explorer geöffnet.',
               '✓ Étape {n} : « {name} » ouvert dans l’Explorateur.', '✓ Paso {n}: abrí «{name}» en el Explorador.', '✓ Passo {n}: aperto «{name}» in Esplora file.',
               '✓ Passo {n}: abri «{name}» no Explorador.'),
    'failed': ('✗ Не получилось на шаге {n} ({step}): {reason}.', '✗ Failed at step {n} ({step}): {reason}.', '✗ Fehlgeschlagen bei Schritt {n} ({step}): {reason}.',
               '✗ Échec à l’étape {n} ({step}) : {reason}.', '✗ Falló en el paso {n} ({step}): {reason}.', '✗ Non riuscito al passo {n} ({step}): {reason}.',
               '✗ Falhou no passo {n} ({step}): {reason}.'),
    'skipped': ('Дальше не продолжал: {rest}.', 'Did not continue with: {rest}.', 'Nicht fortgesetzt: {rest}.', 'Pas continué : {rest}.',
                'No continué con: {rest}.', 'Non ho proseguito con: {rest}.', 'Não continuei com: {rest}.'),
    'not_run': ('Программы из архива я не запускаю — откройте их сами, если доверяете источнику.',
                'I don’t run programs from the archive — open them yourself if you trust the source.',
                'Programme aus dem Archiv starte ich nicht – öffne sie selbst, wenn du der Quelle vertraust.',
                'Je ne lance pas les programmes de l’archive : ouvrez-les vous-même si vous faites confiance à la source.',
                'No ejecuto programas del archivo: ábrelos tú si confías en la fuente.',
                'Non avvio programmi dall’archivio: aprili tu se ti fidi della fonte.',
                'Não executo programas do arquivo: abre-os tu se confiares na fonte.'),
}


def t(key: str, code: str, /, **values: Any) -> str:
    return _pick(_TEXT[key], code).format(**values)


def reason_text(error: StepFailed, code: str) -> str:
    return _pick(_REASON[error.reason_key], code).format(**error.values)


def run(steps: list[str], code: str, *, url: str = '', source: str = '', downloads: Path | None = None,
        downloader: Callable[..., Path] = download, unpacker: Callable[[Path], Path] = unpack,
        opener: Callable[[Path], None] = open_in_explorer, size_text: Callable[[int, str], str] | None = None,
        cancelled: Callable[[], bool] = lambda: False) -> tuple[ChainResult, str]:
    """Run ``steps`` (download / unpack / open) in order; stop at the first failure and say where."""
    from jarvis.runtime import context173, tidy172
    size_text = size_text or tidy172.size_text
    result = ChainResult()
    lines: list[str] = []
    current = Path(source) if source else None
    for number, step in enumerate(steps, 1):
        try:
            if step == 'download':
                current = downloader(url, Path(downloads) if downloads else tidy172.known_folder('downloads'), cancelled=cancelled)
                context173.note('file', current)
                lines.append(t('downloaded', code, n=number, name=current.name, size=size_text(current.stat().st_size, code)))
            elif step == 'unpack':
                if current is None:
                    raise StepFailed('no_archive', name='?')
                current = unpacker(current)
                context173.note('folder', current)
                count = sum(1 for p in Path(current).rglob('*') if p.is_file())
                lines.append(t('unpacked', code, n=number, name=Path(current).name, count=count))
            elif step == 'open':
                if current is None:
                    raise StepFailed('no_archive', name='?')
                opener(current)
                lines.append(t('opened', code, n=number, name=Path(current).name))
            result.steps.append(StepResult(step, True, lines[-1], str(current or '')))
        except StepFailed as error:
            result.failed_step = step
            lines.append(t('failed', code, n=number, step=_pick(_STEP[step], code), reason=reason_text(error, code)))
            rest = [_pick(_STEP[s], code) for s in steps[number:]]
            if rest:
                lines.append(t('skipped', code, rest=', '.join(rest)))
            result.steps.append(StepResult(step, False, lines[-1]))
            break
    if result.ok and 'unpack' in steps and current is not None and any(
            p.suffix.casefold() in {'.exe', '.msi', '.bat', '.cmd'} for p in Path(current).rglob('*') if p.is_file()):
        lines.append(t('not_run', code))
    return result, '\n'.join(lines)


__all__ = ['ChainResult', 'StepFailed', 'StepResult', 'download', 'mark_from_internet', 'open_in_explorer', 'reason_text', 'run', 't', 'unpack']
