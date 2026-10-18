"""NEO 188 HREQ-020: LibreHardwareMonitor — download, check, start and remove.

* the release is looked up on the official GitHub repository and downloaded
  only when the user presses «Скачать»; the archive is unpacked into the NEO
  data folder (``tools/LibreHardwareMonitor``) with path, count and size limits;
* starting it is a visible action of the user; Windows may show its own
  elevation prompt (LibreHardwareMonitor reads sensors through a driver) — NEO
  never bypasses or suppresses that prompt;
* «Проверить» reports what is really there: the program file, whether it runs,
  and whether its WMI sensors answer; missing sensors are reported, not invented;
* «Удалить» removes only a copy NEO itself downloaded (marker file) and only
  while the program is not running.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

REPO_API = 'https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest'
EXE = 'LibreHardwareMonitor.exe'
MARKER = 'neo_lhm188.json'
MAX_DOWNLOAD = 80 * 1024 * 1024
MAX_UNPACKED = 200 * 1024 * 1024
MAX_FILES = 2000


class LhmError(Exception):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key, self.values = key, values


def default_folder(config: Any) -> Path:
    custom = str(config.get('monitoring.lhm_folder', '') or '').strip()
    return Path(custom) if custom else Path(config.paths.root) / 'tools' / 'LibreHardwareMonitor'


def choose_asset(release: dict) -> tuple[str, str]:
    """The .NET Framework build of the official release (runs on stock Windows 10/11)."""
    assets = [a for a in (release.get('assets') or []) if isinstance(a, dict) and str(a.get('name', '')).casefold().endswith('.zip')]
    preferred = [a for a in assets if 'net472' in str(a.get('name', '')).casefold()] or \
                [a for a in assets if 'librehardwaremonitor' in str(a.get('name', '')).casefold()]
    if not preferred:
        raise LhmError('no_asset')
    asset = preferred[0]
    url = str(asset.get('browser_download_url', ''))
    if not url.startswith('https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/'):
        raise LhmError('no_asset')
    return url, str(release.get('tag_name', '') or '')


def safe_extract(archive: zipfile.ZipFile, target: Path) -> int:
    infos = archive.infolist()
    if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise LhmError('archive')
    root = target.resolve()
    count = 0
    for info in infos:
        name = info.filename.replace('\\', '/')
        if name.startswith('/') or ':' in name.split('/')[0] or any(part == '..' for part in name.split('/')):
            raise LhmError('archive')
        destination = (target / name).resolve()
        if root != destination and root not in destination.parents:
            raise LhmError('archive')
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, open(destination, 'wb') as out:
            shutil.copyfileobj(source, out, 1024 * 256)
        count += 1
    return count


def download(config: Any, *, progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] = lambda: False,
             session: Any = None) -> Path:
    import requests
    http = session or requests
    folder = default_folder(config)
    try:
        response = http.get(REPO_API, timeout=(5, 20), headers={'Accept': 'application/vnd.github+json'})
        response.raise_for_status()
        url, tag = choose_asset(response.json())
    except LhmError:
        raise
    except Exception as exc:
        raise LhmError('network') from exc
    folder.parent.mkdir(parents=True, exist_ok=True)
    temp = folder.parent / ('.lhm-download-' + str(int(time.time())) + '.zip')
    try:
        with http.get(url, stream=True, timeout=(5, 60)) as stream:
            stream.raise_for_status()
            total = int(stream.headers.get('content-length') or 0)
            if total > MAX_DOWNLOAD:
                raise LhmError('archive')
            done = 0
            with open(temp, 'wb') as out:
                for chunk in stream.iter_content(1024 * 256):
                    if cancelled():
                        raise LhmError('cancelled')
                    done += len(chunk)
                    if done > MAX_DOWNLOAD:
                        raise LhmError('archive')
                    out.write(chunk)
                    if progress:
                        progress(done, total)
        staging = folder.parent / (folder.name + '.new')
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with zipfile.ZipFile(temp) as archive:
                safe_extract(archive, staging)
        except zipfile.BadZipFile as exc:
            raise LhmError('archive') from exc
        exe = next(staging.rglob(EXE), None)
        if exe is None:
            shutil.rmtree(staging, ignore_errors=True)
            raise LhmError('archive')
        (staging / MARKER).write_text(json.dumps({'source': url, 'tag': tag, 'installed': time.time(), 'by': 'NEO'}), encoding='utf-8')
        if folder.exists():
            if running(folder):
                shutil.rmtree(staging, ignore_errors=True)
                raise LhmError('running')
            shutil.rmtree(folder)
        staging.rename(folder)
        return folder
    except LhmError:
        raise
    except Exception as exc:
        raise LhmError('network') from exc
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def executable(folder: Path) -> Path | None:
    try:
        return next(Path(folder).rglob(EXE), None)
    except OSError:
        return None


def running(folder: Path) -> bool:
    try:
        import psutil
        target = str(Path(folder).resolve()).casefold()
        for process in psutil.process_iter(['name', 'exe']):
            exe = str(process.info.get('exe') or '')
            if str(process.info.get('name') or '').casefold() == EXE.casefold() and (not exe or exe.casefold().startswith(target)):
                return True
    except Exception:
        return False
    return False


def check(config: Any) -> dict[str, Any]:
    folder = default_folder(config)
    exe = executable(folder)
    sensors: list = []
    if os.name == 'nt':
        try:
            from jarvis.integrations import system_monitor
            sensors = system_monitor._read_lhm_sensors()
        except Exception:
            sensors = []
    temps = [s for s in sensors if str(s.get('SensorType', '')).casefold() == 'temperature']
    return {'folder': str(folder), 'installed': exe is not None, 'exe': str(exe or ''), 'running': running(folder) if exe else False,
            'sensors': len(sensors), 'temperature_sensors': len(temps), 'by_neo': (folder / MARKER).exists()}


def launch(config: Any) -> Path:
    exe = executable(default_folder(config))
    if exe is None:
        raise LhmError('missing')
    if os.name != 'nt':
        raise LhmError('windows_only')
    os.startfile(str(exe))            # a visible start; Windows shows its own prompt when it requires one
    return exe


def remove(config: Any) -> Path:
    folder = default_folder(config)
    if not folder.exists():
        raise LhmError('missing')
    if not (folder / MARKER).exists():
        raise LhmError('not_ours')
    if running(folder):
        raise LhmError('running')
    shutil.rmtree(folder)
    if folder.exists():
        raise LhmError('remove_failed')
    return folder


_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_T = {
    'status': ('LibreHardwareMonitor: {installed}; {running}; датчиков WMI: {sensors} (температурных: {temps}). Папка: {folder}',
               'LibreHardwareMonitor: {installed}; {running}; WMI sensors: {sensors} (temperature: {temps}). Folder: {folder}',
               'LibreHardwareMonitor: {installed}; {running}; WMI-Sensoren: {sensors} (Temperatur: {temps}). Ordner: {folder}',
               'LibreHardwareMonitor : {installed} ; {running} ; capteurs WMI : {sensors} (température : {temps}). Dossier : {folder}',
               'LibreHardwareMonitor: {installed}; {running}; sensores WMI: {sensors} (temperatura: {temps}). Carpeta: {folder}',
               'LibreHardwareMonitor: {installed}; {running}; sensori WMI: {sensors} (temperatura: {temps}). Cartella: {folder}',
               'LibreHardwareMonitor: {installed}; {running}; sensores WMI: {sensors} (temperatura: {temps}). Pasta: {folder}'),
    'installed': ('установлен', 'installed', 'installiert', 'installé', 'instalado', 'installato', 'instalado'),
    'absent': ('не установлен', 'not installed', 'nicht installiert', 'non installé', 'no instalado', 'non installato', 'não instalado'),
    'running': ('запущен', 'running', 'läuft', 'en cours', 'en ejecución', 'in esecuzione', 'em execução'),
    'stopped': ('не запущен', 'not running', 'läuft nicht', 'arrêté', 'no se ejecuta', 'non in esecuzione', 'não está em execução'),
    'downloaded': ('LibreHardwareMonitor скачан в {folder}. Запустите его кнопкой «Запустить» — Windows может спросить разрешение.',
                   'LibreHardwareMonitor was downloaded to {folder}. Start it with “Start” — Windows may ask for permission.',
                   'LibreHardwareMonitor wurde nach {folder} geladen. Starte es mit „Starten“ – Windows fragt eventuell nach einer Erlaubnis.',
                   'LibreHardwareMonitor a été téléchargé dans {folder}. Lancez-le avec « Démarrer » — Windows peut demander une autorisation.',
                   'LibreHardwareMonitor se descargó en {folder}. Inícialo con «Iniciar»; Windows puede pedir permiso.',
                   'LibreHardwareMonitor è stato scaricato in {folder}. Avvialo con «Avvia»: Windows può chiedere un’autorizzazione.',
                   'O LibreHardwareMonitor foi transferido para {folder}. Inicie-o com «Iniciar» — o Windows pode pedir autorização.'),
    'removed': ('Копия LibreHardwareMonitor, скачанная NEO, удалена.', 'The LibreHardwareMonitor copy downloaded by NEO was removed.',
                'Die von NEO geladene LibreHardwareMonitor-Kopie wurde entfernt.', 'La copie de LibreHardwareMonitor téléchargée par NEO a été supprimée.',
                'Se eliminó la copia de LibreHardwareMonitor descargada por NEO.', 'La copia di LibreHardwareMonitor scaricata da NEO è stata rimossa.',
                'A cópia do LibreHardwareMonitor transferida pelo NEO foi removida.'),
    'launched': ('Запуск LibreHardwareMonitor… Если Windows спросит разрешение — это его собственный запрос.', 'Starting LibreHardwareMonitor… If Windows asks for permission, that is its own prompt.',
                 'LibreHardwareMonitor wird gestartet… Fragt Windows nach einer Erlaubnis, ist das seine eigene Abfrage.', 'Démarrage de LibreHardwareMonitor… Si Windows demande une autorisation, c’est sa propre demande.',
                 'Iniciando LibreHardwareMonitor… Si Windows pide permiso, es su propia solicitud.', 'Avvio di LibreHardwareMonitor… Se Windows chiede un’autorizzazione, è la sua richiesta.',
                 'A iniciar o LibreHardwareMonitor… Se o Windows pedir autorização, é o seu próprio pedido.'),
    'error.network': ('Не удалось скачать LibreHardwareMonitor (нет связи с GitHub).', 'Could not download LibreHardwareMonitor (no connection to GitHub).',
                      'LibreHardwareMonitor konnte nicht geladen werden (keine Verbindung zu GitHub).', 'Impossible de télécharger LibreHardwareMonitor (pas de connexion à GitHub).',
                      'No se pudo descargar LibreHardwareMonitor (sin conexión con GitHub).', 'Impossibile scaricare LibreHardwareMonitor (nessuna connessione a GitHub).',
                      'Não foi possível transferir o LibreHardwareMonitor (sem ligação ao GitHub).'),
    'error.no_asset': ('В последнем релизе LibreHardwareMonitor не найден подходящий архив.', 'No suitable archive was found in the latest LibreHardwareMonitor release.',
                       'Im neuesten LibreHardwareMonitor-Release gibt es kein passendes Archiv.', 'Aucune archive adaptée dans la dernière version de LibreHardwareMonitor.',
                       'No hay un archivo adecuado en la última versión de LibreHardwareMonitor.', 'Nessun archivio adatto nell’ultima versione di LibreHardwareMonitor.',
                       'Não há um arquivo adequado na última versão do LibreHardwareMonitor.'),
    'error.archive': ('Архив LibreHardwareMonitor повреждён или подозрителен — ничего не распаковано.', 'The LibreHardwareMonitor archive is damaged or suspicious — nothing was unpacked.',
                      'Das LibreHardwareMonitor-Archiv ist beschädigt oder verdächtig – nichts entpackt.', 'L’archive LibreHardwareMonitor est endommagée ou suspecte — rien n’a été décompressé.',
                      'El archivo de LibreHardwareMonitor está dañado o es sospechoso: no se descomprimió nada.', 'L’archivio di LibreHardwareMonitor è danneggiato o sospetto: nulla è stato estratto.',
                      'O arquivo do LibreHardwareMonitor está danificado ou é suspeito — nada foi descompactado.'),
    'error.running': ('LibreHardwareMonitor сейчас запущен — закройте его и повторите.', 'LibreHardwareMonitor is running — close it and try again.',
                      'LibreHardwareMonitor läuft gerade – schließe es und versuche es erneut.', 'LibreHardwareMonitor est en cours — fermez-le et réessayez.',
                      'LibreHardwareMonitor está en ejecución: ciérralo e inténtalo de nuevo.', 'LibreHardwareMonitor è in esecuzione: chiudilo e riprova.',
                      'O LibreHardwareMonitor está em execução — feche-o e tente de novo.'),
    'error.missing': ('LibreHardwareMonitor не найден в папке NEO. Сначала нажмите «Скачать».', 'LibreHardwareMonitor was not found in the NEO folder. Press “Download” first.',
                      'LibreHardwareMonitor wurde im NEO-Ordner nicht gefunden. Drücke zuerst „Herunterladen“.', 'LibreHardwareMonitor est introuvable dans le dossier NEO. Cliquez d’abord sur « Télécharger ».',
                      'No se encontró LibreHardwareMonitor en la carpeta de NEO. Pulsa primero «Descargar».', 'LibreHardwareMonitor non trovato nella cartella di NEO. Premi prima «Scarica».',
                      'O LibreHardwareMonitor não foi encontrado na pasta do NEO. Carregue primeiro em «Transferir».'),
    'error.not_ours': ('Эту папку LibreHardwareMonitor создал не NEO — удалять её не буду.', 'This LibreHardwareMonitor folder was not created by NEO — I won’t delete it.',
                       'Diesen LibreHardwareMonitor-Ordner hat nicht NEO angelegt – ich lösche ihn nicht.', 'Ce dossier LibreHardwareMonitor n’a pas été créé par NEO — je ne le supprime pas.',
                       'Esta carpeta de LibreHardwareMonitor no la creó NEO: no la borraré.', 'Questa cartella di LibreHardwareMonitor non l’ha creata NEO: non la eliminerò.',
                       'Esta pasta do LibreHardwareMonitor não foi criada pelo NEO — não a vou apagar.'),
    'error.windows_only': ('LibreHardwareMonitor работает только в Windows.', 'LibreHardwareMonitor works only on Windows.', 'LibreHardwareMonitor läuft nur unter Windows.',
                           'LibreHardwareMonitor ne fonctionne que sous Windows.', 'LibreHardwareMonitor solo funciona en Windows.', 'LibreHardwareMonitor funziona solo su Windows.',
                           'O LibreHardwareMonitor só funciona no Windows.'),
    'error.cancelled': ('Загрузка отменена.', 'Download cancelled.', 'Download abgebrochen.', 'Téléchargement annulé.', 'Descarga cancelada.', 'Download annullato.', 'Transferência cancelada.'),
    'error.remove_failed': ('Удалить папку полностью не удалось.', 'Could not remove the folder completely.', 'Der Ordner konnte nicht vollständig entfernt werden.',
                            'Impossible de supprimer complètement le dossier.', 'No se pudo eliminar la carpeta por completo.', 'Impossibile rimuovere del tutto la cartella.',
                            'Não foi possível remover a pasta por completo.'),
}


def t(key: str, code: str, **values: Any) -> str:
    return _T[key][_CODES.index(code) if code in _CODES else 1].format(**values)


def status_text(info: dict, code: str) -> str:
    return t('status', code, installed=t('installed' if info['installed'] else 'absent', code),
             running=t('running' if info['running'] else 'stopped', code), sensors=info['sensors'],
             temps=info['temperature_sensors'], folder=info['folder'])


def error_text(error: LhmError, code: str) -> str:
    key = 'error.' + error.key
    return t(key if key in _T else 'error.network', code)
