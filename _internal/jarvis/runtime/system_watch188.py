"""NEO 188 HREQ-020 / HREQ-021: low-cost local watchers with configurable thresholds.

* hardware temperatures — an alert after two consecutive readings at or above
  the user's threshold, «back to normal» only after two readings 5 °C below it;
  sensors the OS/device does not expose are reported as unavailable, never
  invented;
* per-process load spikes — CPU (share of the whole machine) and RAM above the
  thresholds for two consecutive samples; one notice per process per cooldown;
* Steam installations — read from the app manifests Steam itself writes
  (``appmanifest_*.acf``): start, and completion only when Steam marks the app
  fully installed with all bytes downloaded. A quiet network is never taken as
  proof of a finished installation.

Everything runs on the existing desktop-events thread, only when the matching
setting is on, at a bounded interval. Nothing here asks for elevation.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

STEAM_FULLY_INSTALLED = 4


def _setting(config: Any, key: str, default: Any) -> Any:
    try:
        value = config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


# ------------------------------------------------------------------ hardware
@dataclass
class HardwareAlerts:
    cooldown: float = 600.0
    _high: dict[str, int] = field(default_factory=dict)
    _low: dict[str, int] = field(default_factory=dict)
    _alerted: dict[str, float] = field(default_factory=dict)

    def observe(self, readings: dict[str, float | None], thresholds: dict[str, float], now: float | None = None) -> list[tuple[str, dict]]:
        now = time.monotonic() if now is None else now
        events: list[tuple[str, dict]] = []
        for component, value in readings.items():
            limit = thresholds.get(component)
            if value is None or limit is None:
                continue
            if value >= limit:
                self._high[component] = self._high.get(component, 0) + 1
                self._low[component] = 0
                last = self._alerted.get(component)
                if self._high[component] >= 2 and (last is None or now - last >= self.cooldown):
                    self._alerted[component] = now
                    events.append(('hardware.temp_high', {'component': component, 'value': round(float(value), 1), 'threshold': float(limit)}))
            else:
                self._high[component] = 0
                if component in self._alerted and value <= limit - 5:
                    self._low[component] = self._low.get(component, 0) + 1
                    if self._low[component] >= 2:
                        self._alerted.pop(component, None)
                        self._low[component] = 0
                        events.append(('hardware.temp_normal', {'component': component, 'value': round(float(value), 1), 'threshold': float(limit)}))
        return events


def hardware_readings() -> dict[str, float | None]:
    """CPU (LibreHardwareMonitor/WMI when available) and NVIDIA GPU temperatures; None = unavailable."""
    readings: dict[str, float | None] = {'cpu': None, 'gpu': None}
    try:
        from jarvis.integrations import system_monitor as monitor
        readings['cpu'] = monitor._cpu_telemetry(monitor._lhm_sensors())[0]
    except Exception:
        readings['cpu'] = None
    try:
        from jarvis.runtime.hardware import _nvidia_smi
        value = (_nvidia_smi() or {}).get('gpu_temp_c')
        readings['gpu'] = float(value) if value is not None else None
    except Exception:
        readings['gpu'] = None
    return readings


# ------------------------------------------------------------------ processes
@dataclass
class ProcessSpikes:
    cooldown: float = 600.0
    _streak: dict[tuple[int, str], int] = field(default_factory=dict)
    _alerted: dict[tuple[str, str], float] = field(default_factory=dict)

    def observe(self, rows: Iterable[dict], *, cpu_percent: float, ram_mb: float, now: float | None = None) -> list[tuple[str, dict]]:
        now = time.monotonic() if now is None else now
        events: list[tuple[str, dict]] = []
        seen: set[tuple[int, str]] = set()
        for row in rows:
            pid, name = int(row.get('pid') or 0), str(row.get('name') or '?')
            for metric, value, limit in (('cpu', row.get('cpu'), cpu_percent), ('ram', row.get('ram_mb'), ram_mb)):
                if value is None or limit <= 0:
                    continue
                key = (pid, metric)
                if float(value) >= limit:
                    seen.add(key)
                    self._streak[key] = self._streak.get(key, 0) + 1
                    alert_key = (name.casefold(), metric)
                    last = self._alerted.get(alert_key)
                    if self._streak[key] >= 2 and (last is None or now - last >= self.cooldown):
                        self._alerted[alert_key] = now
                        events.append(('process.spike', {'process': name, 'pid': pid, 'metric': metric,
                                                         'value': round(float(value), 1), 'threshold': float(limit)}))
        for key in list(self._streak):
            if key not in seen:
                self._streak.pop(key, None)
        return events


def process_rows(previous: dict[int, tuple[float, float]], *, limit: int = 400) -> tuple[list[dict], dict[int, tuple[float, float]]]:
    """CPU share of the whole machine since the previous call, and resident memory, per process."""
    import psutil
    cores = max(1, psutil.cpu_count(logical=True) or 1)
    now = time.monotonic()
    rows, current = [], {}
    for process in list(psutil.process_iter(['pid', 'name', 'cpu_times', 'memory_info']))[:4000]:
        try:
            info = process.info
            pid = int(info['pid'])
            if pid == os.getpid() or not info.get('cpu_times'):
                continue
            busy = float(info['cpu_times'].user + info['cpu_times'].system)
            current[pid] = (busy, now)
            cpu = None
            if pid in previous and now > previous[pid][1]:
                cpu = max(0.0, (busy - previous[pid][0]) / (now - previous[pid][1]) * 100.0 / cores)
            memory = info.get('memory_info')
            rows.append({'pid': pid, 'name': info.get('name') or '?', 'cpu': cpu,
                         'ram_mb': float(memory.rss) / (1024 * 1024) if memory else None})
        except (psutil.Error, OSError, ValueError, AttributeError):
            continue
    rows.sort(key=lambda r: (r['cpu'] or 0, r['ram_mb'] or 0), reverse=True)
    return rows[:limit], current


# ------------------------------------------------------------------ steam
_PAIR = re.compile(r'"([^"]*)"\s+"([^"]*)"')


def parse_acf(text: str) -> dict[str, str]:
    """Flat key/value pairs of a Steam .acf/.vdf file (the keys NEO needs are top-level)."""
    return {key: value for key, value in _PAIR.findall(str(text or ''))}


def steam_libraries(steam_root: Path) -> list[Path]:
    roots = [steam_root / 'steamapps']
    vdf = steam_root / 'steamapps' / 'libraryfolders.vdf'
    try:
        text = vdf.read_text(encoding='utf-8', errors='replace')
    except OSError:
        text = ''
    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        path = Path(match.group(1).replace('\\\\', '\\')) / 'steamapps'
        if path not in roots:
            roots.append(path)
    return [r for r in roots if r.is_dir()]


def steam_root() -> Path | None:
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
                value = winreg.QueryValueEx(key, 'SteamPath')[0]
                if value and Path(value).is_dir():
                    return Path(value)
        except OSError:
            pass
    for candidate in (Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Steam', Path.home() / '.steam' / 'steam'):
        if candidate.is_dir():
            return candidate
    return None


@dataclass
class SteamInstalls:
    _state: dict[str, dict] = field(default_factory=dict)
    _primed: bool = False

    def observe(self, libraries: list[Path]) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        current: dict[str, dict] = {}
        for library in libraries:
            try:
                manifests = list(library.glob('appmanifest_*.acf'))[:2000]
            except OSError:
                continue
            for manifest in manifests:
                try:
                    data = parse_acf(manifest.read_text(encoding='utf-8', errors='replace'))
                except OSError:
                    continue
                appid = data.get('appid') or manifest.stem.rsplit('_', 1)[-1]
                try:
                    flags = int(data.get('StateFlags', '0') or 0)
                    need = int(data.get('BytesToDownload', '0') or 0)
                    got = int(data.get('BytesDownloaded', '0') or 0)
                except ValueError:
                    continue
                complete = flags == STEAM_FULLY_INSTALLED and got >= need
                current[appid] = {'name': data.get('name') or appid, 'path': str(library / 'common' / data.get('installdir', '')),
                                  'complete': complete, 'need': need, 'got': got}
        if not self._primed:
            self._state, self._primed = current, True       # what was there before NEO started is not news
            return events
        for appid, row in current.items():
            before = self._state.get(appid)
            payload = {'name': row['name'], 'path': row['path'], 'source': 'steam'}
            if before is None and not row['complete']:
                events.append(('installation.steam_started', payload))
            elif row['complete'] and (before is None or not before['complete']):
                events.append(('installation.steam_completed', payload))
        self._state = current
        return events


# ------------------------------------------------------------------ texts
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_COMPONENT = {'cpu': ('процессора', 'CPU', 'CPU', 'du processeur', 'de la CPU', 'della CPU', 'do processador'),
              'gpu': ('видеокарты', 'GPU', 'GPU', 'du GPU', 'de la GPU', 'della GPU', 'da GPU')}
_TEXT = {
    'hardware.temp_high': ('🌡 Высокая температура {c}: {v} °C (порог {t} °C).', '🌡 High {c} temperature: {v} °C (threshold {t} °C).',
                           '🌡 Hohe {c}-Temperatur: {v} °C (Schwelle {t} °C).', '🌡 Température {c} élevée : {v} °C (seuil {t} °C).',
                           '🌡 Temperatura alta {c}: {v} °C (umbral {t} °C).', '🌡 Temperatura {c} alta: {v} °C (soglia {t} °C).',
                           '🌡 Temperatura alta {c}: {v} °C (limite {t} °C).'),
    'hardware.temp_normal': ('🌡 Температура {c} снова в норме: {v} °C.', '🌡 {c} temperature is back to normal: {v} °C.',
                             '🌡 {c}-Temperatur wieder normal: {v} °C.', '🌡 Température {c} revenue à la normale : {v} °C.',
                             '🌡 Temperatura {c} de nuevo normal: {v} °C.', '🌡 Temperatura {c} di nuovo normale: {v} °C.',
                             '🌡 Temperatura {c} de novo normal: {v} °C.'),
    'process.spike.cpu': ('📈 Процесс {p} сильно нагружает процессор: {v}% (порог {t}%).', '📈 Process {p} is putting a heavy load on the CPU: {v}% (threshold {t}%).',
                          '📈 Prozess {p} belastet die CPU stark: {v} % (Schwelle {t} %).', '📈 Le processus {p} charge fortement le processeur : {v} % (seuil {t} %).',
                          '📈 El proceso {p} carga mucho la CPU: {v} % (umbral {t} %).', '📈 Il processo {p} carica molto la CPU: {v}% (soglia {t}%).',
                          '📈 O processo {p} está a carregar muito o processador: {v}% (limite {t}%).'),
    'process.spike.ram': ('📈 Процесс {p} занимает много памяти: {v} МБ (порог {t} МБ).', '📈 Process {p} is using a lot of memory: {v} MB (threshold {t} MB).',
                          '📈 Prozess {p} belegt viel Arbeitsspeicher: {v} MB (Schwelle {t} MB).', '📈 Le processus {p} utilise beaucoup de mémoire : {v} Mo (seuil {t} Mo).',
                          '📈 El proceso {p} usa mucha memoria: {v} MB (umbral {t} MB).', '📈 Il processo {p} usa molta memoria: {v} MB (soglia {t} MB).',
                          '📈 O processo {p} está a usar muita memória: {v} MB (limite {t} MB).'),
    'installation.steam_started': ('🎮 Steam начал установку: {n} → {path}', '🎮 Steam started installing: {n} → {path}', '🎮 Steam installiert: {n} → {path}',
                                   '🎮 Steam a commencé l’installation : {n} → {path}', '🎮 Steam empezó a instalar: {n} → {path}', '🎮 Steam ha iniziato a installare: {n} → {path}',
                                   '🎮 O Steam começou a instalar: {n} → {path}'),
    'installation.steam_completed': ('✅ Steam завершил установку: {n} ({path}). Это подтверждено манифестом Steam.', '✅ Steam finished installing: {n} ({path}). Confirmed by the Steam manifest.',
                                     '✅ Steam hat die Installation abgeschlossen: {n} ({path}). Laut Steam-Manifest bestätigt.', '✅ Steam a terminé l’installation : {n} ({path}). Confirmé par le manifeste Steam.',
                                     '✅ Steam terminó de instalar: {n} ({path}). Confirmado por el manifiesto de Steam.', '✅ Steam ha completato l’installazione: {n} ({path}). Confermato dal manifest di Steam.',
                                     '✅ O Steam concluiu a instalação: {n} ({path}). Confirmado pelo manifesto do Steam.'),
}


def event_text(kind: str, data: dict, code: str) -> str:
    index = _CODES.index(code) if code in _CODES else 1
    if kind == 'process.spike':
        key = 'process.spike.' + ('ram' if data.get('metric') == 'ram' else 'cpu')
        value, limit = data.get('value'), data.get('threshold')
        return _TEXT[key][index].format(p=data.get('process', '?'), v=f'{float(value):g}', t=f'{float(limit):g}')
    if kind in {'hardware.temp_high', 'hardware.temp_normal'}:
        component = _COMPONENT.get(str(data.get('component')), (str(data.get('component')),) * 7)[index]
        return _TEXT[kind][index].format(c=component, v=f'{float(data.get("value", 0)):g}', t=f'{float(data.get("threshold", 0)):g}')
    if kind in _TEXT:
        return _TEXT[kind][index].format(n=data.get('name', '?'), path=data.get('path', ''))
    return ''


class Watchers:
    """State of all watchers for one DesktopServices instance."""

    def __init__(self) -> None:
        self.hardware = HardwareAlerts()
        self.processes = ProcessSpikes()
        self.steam = SteamInstalls()
        self._cpu_previous: dict[int, tuple[float, float]] = {}
        self.next_run = 0.0

    def interval(self, config: Any) -> float:
        return max(15.0, min(600.0, float(_setting(config, 'monitoring.interval_seconds', 30) or 30)))

    def enabled(self, config: Any) -> bool:
        return any(bool(_setting(config, key, False)) for key in
                   ('monitoring.hardware_alerts_enabled', 'monitoring.process_alerts_enabled', 'monitoring.steam_installs_enabled'))

    def run(self, config: Any, now: float | None = None) -> list[tuple[str, dict]]:
        now = time.monotonic() if now is None else now
        events: list[tuple[str, dict]] = []
        if bool(_setting(config, 'monitoring.hardware_alerts_enabled', False)):
            thresholds = {'cpu': float(_setting(config, 'monitoring.cpu_temp_c', 90) or 90),
                          'gpu': float(_setting(config, 'monitoring.gpu_temp_c', 85) or 85)}
            events += self.hardware.observe(hardware_readings(), thresholds, now)
        if bool(_setting(config, 'monitoring.process_alerts_enabled', False)):
            rows, self._cpu_previous = process_rows(self._cpu_previous)
            events += self.processes.observe(rows, cpu_percent=float(_setting(config, 'monitoring.process_cpu_percent', 60) or 60),
                                             ram_mb=float(_setting(config, 'monitoring.process_ram_mb', 4096) or 4096), now=now)
        if bool(_setting(config, 'monitoring.steam_installs_enabled', False)):
            root = steam_root()
            if root is not None:
                events += self.steam.observe(steam_libraries(root))
        return events
