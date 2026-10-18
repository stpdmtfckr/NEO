# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from copy import deepcopy
from typing import Any

import psutil


class _ProbeCache:
    """Coalesce native probes, including absent/failed optional providers.

    A missing sensor provider is the usual case on consumer PCs. Repeatedly
    launching PowerShell to rediscover that absence caused avoidable CPU peaks.
    No sampler thread is created; cached data is used only when requested.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._values = {}

    def read(self, key, probe, *, ttl=2.0, missing_ttl=30.0):
        with self._lock:
            cached = self._values.get(key)
            if cached is not None and time.monotonic() < cached[0]:
                return deepcopy(cached[1])
            value = probe()
            self._values[key] = (time.monotonic() + (ttl if value else missing_ttl), deepcopy(value))
            return value


_PROBES = _ProbeCache()
_SNAPSHOTS = _ProbeCache()


def _gib(value: int | float) -> float:
    return round(float(value or 0) / (1024 ** 3), 2)


def _hidden_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    kwargs: dict[str, Any] = {}
    try:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
    except Exception:
        pass
    return kwargs


def _run_hidden(command: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, **_hidden_kwargs())
    except Exception:
        return None


def _lhm_sensors() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    return _PROBES.read("lhm", _read_lhm_sensors)


def _read_lhm_sensors() -> list[dict[str, Any]]:
    script = (
        "$x=Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction SilentlyContinue | Select-Object Name,SensorType,Value,Parent; "
        "if(-not $x){$x=Get-CimInstance -Namespace root/OpenHardwareMonitor -ClassName Sensor -ErrorAction SilentlyContinue | Select-Object Name,SensorType,Value,Parent}; "
        "if($x){$x|ConvertTo-Json -Compress}"
    )
    proc = _run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script], 2.5)
    if not proc or proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        raw = json.loads(proc.stdout.strip())
        rows = raw if isinstance(raw, list) else [raw]
        parsed = [x for x in rows if isinstance(x, dict)]
        return parsed
    except Exception:
        return []


def _cpu_telemetry(lhm: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    temperatures: list[float] = []
    powers: list[float] = []
    for row in lhm:
        name = str(row.get("Name") or "").casefold()
        typ = str(row.get("SensorType") or "").casefold()
        try:
            value = float(row.get("Value"))
        except Exception:
            continue
        if typ == "temperature" and ("cpu" in name or "package" in name or "core max" in name) and 5 <= value <= 130:
            temperatures.append(value)
        if typ == "power" and ("cpu" in name or "package" in name) and 0 <= value <= 1000:
            powers.append(value)
    temp = max(temperatures) if temperatures else None
    power = max(powers) if powers else None

    if temp is None:
        try:
            temps = getattr(psutil, "sensors_temperatures", lambda: {})() or {}
            values=[]
            for rows in temps.values():
                for row in rows:
                    current=float(getattr(row,"current",0) or 0)
                    if 5 <= current <= 130: values.append(current)
            if values: temp=max(values)
        except Exception:
            pass
    # Do not reinterpret generic ACPI thermal-zone readings as CPU package
    # temperature. On Windows these zones may represent motherboard/ambient
    # sensors; if no CPU-labelled source is available, report unavailable.
    return (round(temp,1) if temp is not None else None, round(power,1) if power is not None else None)


def _read_nvidia_gpus() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    proc = _run_hidden([
        "nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.current.graphics",
        "--format=csv,noheader,nounits",
    ], 4)
    if proc and proc.returncode == 0:
        for line in proc.stdout.splitlines():
            parts=[x.strip() for x in line.split(",")]
            if len(parts)>=7:
                def num(x: str) -> float | None:
                    try: return float(x)
                    except Exception: return None
                rows.append({"name":parts[0],"utilization_percent":num(parts[1]),"memory_used_mb":num(parts[2]),"memory_total_mb":num(parts[3]),"temperature_c":num(parts[4]),"power_w":num(parts[5]),"clock_mhz":num(parts[6])})
    return rows


def _read_windows_gpu_identity() -> list[dict[str, Any]]:
    rows = []
    script="Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
    proc=_run_hidden(["powershell","-NoProfile","-NonInteractive","-WindowStyle","Hidden","-Command",script],3.5)
    if proc and proc.stdout.strip():
        try:
            raw=json.loads(proc.stdout.strip()); items=raw if isinstance(raw,list) else [raw]
            for item in items:
                rows.append({"name":str(item.get("Name") or "GPU"),"memory_total_mb":round(float(item.get("AdapterRAM") or 0)/1024/1024,1) if item.get("AdapterRAM") else None,"memory_used_mb":None,"utilization_percent":None,"temperature_c":None,"power_w":None,"clock_mhz":None})
        except Exception: pass
    return rows


def _gpu_snapshot(lhm: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _PROBES.read("nvidia", _read_nvidia_gpus)
    if rows:
        return rows

    # If nvidia-smi is not applicable, inspect available LHM GPU sensors.
    gpu_values: dict[str, dict[str, Any]] = {}
    for row in lhm:
        name=str(row.get("Name") or ""); typ=str(row.get("SensorType") or "").casefold()
        parent=str(row.get("Parent") or "GPU")
        if "gpu" not in parent.casefold() and "gpu" not in name.casefold():
            continue
        try: val=float(row.get("Value"))
        except Exception: continue
        g=gpu_values.setdefault(parent,{"name":parent,"utilization_percent":None,"temperature_c":None,"power_w":None,"clock_mhz":None,"memory_used_mb":None,"memory_total_mb":None})
        lname=name.casefold()
        if typ=="temperature" and ("core" in lname or "gpu" in lname): g["temperature_c"]=val
        elif typ=="load" and ("core" in lname or "gpu" in lname): g["utilization_percent"]=val
        elif typ=="power" and ("package" in lname or "gpu" in lname or "board" in lname): g["power_w"]=val
        elif typ=="clock" and ("core" in lname or "gpu" in lname): g["clock_mhz"]=val
    rows=list(gpu_values.values())
    if rows or os.name != "nt": return rows

    # Static adapter identity does not need the dynamic sensor refresh cadence.
    return _PROBES.read("gpu_identity", _read_windows_gpu_identity, ttl=300.0)


def _network_snapshot(before: Any, after: Any, elapsed: float) -> dict[str, Any]:
    elapsed=max(0.05,float(elapsed or 0.05))
    down=max(0,int(after.bytes_recv)-int(before.bytes_recv))*8.0/elapsed/1_000_000
    up=max(0,int(after.bytes_sent)-int(before.bytes_sent))*8.0/elapsed/1_000_000
    links=[]
    try:
        stats=psutil.net_if_stats()
        for name,st in stats.items():
            if st.isup and int(getattr(st,"speed",0) or 0)>0:
                links.append({"name":name,"speed_mbps":int(st.speed)})
    except Exception: pass
    return {"download_mbps":round(down,2),"upload_mbps":round(up,2),"sent_gb":_gib(after.bytes_sent),"received_gb":_gib(after.bytes_recv),"links":links}


def snapshot() -> dict[str, Any]:
    # GUI, voice and remote status commands share one measurement instead of
    # starting overlapping native probes and CPU sample windows.
    return _SNAPSHOTS.read("system", _collect_snapshot, ttl=1.0)


def _collect_snapshot() -> dict[str, Any]:
    lhm=_lhm_sensors()
    net_before=psutil.net_io_counters(); started=time.perf_counter()
    cpu_percent=psutil.cpu_percent(interval=0.22)
    net_after=psutil.net_io_counters(); elapsed=time.perf_counter()-started
    freq=psutil.cpu_freq(); vm=psutil.virtual_memory(); swap=psutil.swap_memory(); cpu_temp,cpu_power=_cpu_telemetry(lhm)
    disks=[]; seen=set()
    for part in psutil.disk_partitions(all=False):
        mount=part.mountpoint
        if mount in seen: continue
        seen.add(mount)
        try: use=psutil.disk_usage(mount)
        except Exception: continue
        disks.append({"device":part.device,"mount":mount,"fstype":part.fstype,"total_gb":_gib(use.total),"used_gb":_gib(use.used),"free_gb":_gib(use.free),"percent":use.percent})
    return {
        "cpu":{"percent":cpu_percent,"physical_cores":psutil.cpu_count(logical=False),"logical_cores":psutil.cpu_count(logical=True),"frequency_mhz":round(freq.current,0) if freq else None,"max_frequency_mhz":round(freq.max,0) if freq and freq.max else None,"temperature_c":cpu_temp,"power_w":cpu_power},
        "memory":{"total_gb":_gib(vm.total),"used_gb":_gib(vm.used),"available_gb":_gib(vm.available),"percent":vm.percent,"swap_used_gb":_gib(swap.used),"swap_percent":swap.percent},
        "disks":disks,"gpus":_gpu_snapshot(lhm),"network":_network_snapshot(net_before,net_after,elapsed),"uptime_seconds":max(0,int(time.time()-psutil.boot_time())),
    }


def _v(value: Any, suffix: str = "", digits: int = 0) -> str:
    if value is None: return "недоступно"
    try: return f"{float(value):.{digits}f}{suffix}"
    except Exception: return str(value)


def format_snapshot(data: dict[str, Any], action: str = "summary", lang: str = "ru") -> str:
    """Format telemetry as compact sections that remain readable in proportional UI fonts."""
    action = str(action or "summary").casefold()
    lang = lang if lang in {"ru","en","de","fr","es","it","pt"} else "en"
    L = {
        "ru": {"title":"Состояние компьютера","load":"Загрузка","clock":"Частота","temp":"Температура","power":"Потребление","cores":"Ядра / потоки","telemetry":"Телеметрия","ram":"RAM","used":"Используется","available":"Доступно","disks":"Диски","free":"свободно","disk_used":"занято","network":"Интернет / сеть","traffic":"Трафик сейчас","link":"Линк адаптера","unavailable":"недоступно"},
        "en": {"title":"Computer status","load":"Load","clock":"Clock","temp":"Temperature","power":"Power","cores":"Cores / threads","telemetry":"Telemetry","ram":"RAM","used":"Used","available":"Available","disks":"Disks","free":"free","disk_used":"used","network":"Network","traffic":"Current traffic","link":"Adapter link","unavailable":"unavailable"},
        "de": {"title":"Computerstatus","load":"Auslastung","clock":"Takt","temp":"Temperatur","power":"Leistung","cores":"Kerne / Threads","telemetry":"Telemetrie","ram":"RAM","used":"Belegt","available":"Verfügbar","disks":"Laufwerke","free":"frei","disk_used":"belegt","network":"Netzwerk","traffic":"Aktueller Datenverkehr","link":"Adapter-Link","unavailable":"nicht verfügbar"},
        "fr": {"title":"État de l’ordinateur","load":"Charge","clock":"Fréquence","temp":"Température","power":"Puissance","cores":"Cœurs / threads","telemetry":"Télémétrie","ram":"RAM","used":"Utilisée","available":"Disponible","disks":"Disques","free":"libre","disk_used":"utilisé","network":"Réseau","traffic":"Trafic actuel","link":"Lien adaptateur","unavailable":"indisponible"},
        "es": {"title":"Estado del ordenador","load":"Carga","clock":"Frecuencia","temp":"Temperatura","power":"Potencia","cores":"Núcleos / hilos","telemetry":"Telemetría","ram":"RAM","used":"Usada","available":"Disponible","disks":"Discos","free":"libre","disk_used":"usado","network":"Red","traffic":"Tráfico actual","link":"Enlace del adaptador","unavailable":"no disponible"},
        "it": {"title":"Stato del computer","load":"Carico","clock":"Frequenza","temp":"Temperatura","power":"Potenza","cores":"Core / thread","telemetry":"Telemetria","ram":"RAM","used":"Usata","available":"Disponibile","disks":"Dischi","free":"libero","disk_used":"usato","network":"Rete","traffic":"Traffico attuale","link":"Link adattatore","unavailable":"non disponibile"},
        "pt": {"title":"Estado do computador","load":"Carga","clock":"Frequência","temp":"Temperatura","power":"Potência","cores":"Núcleos / threads","telemetry":"Telemetria","ram":"RAM","used":"Usada","available":"Disponível","disks":"Discos","free":"livre","disk_used":"usado","network":"Rede","traffic":"Tráfego atual","link":"Ligação do adaptador","unavailable":"indisponível"},
    }[lang]
    cpu=data["cpu"]; mem=data["memory"]; disks=data["disks"]; gpus=data["gpus"]; net=data.get("network") or {}

    def val(value: Any, suffix: str = "", digits: int = 0) -> str:
        if value is None: return L["unavailable"]
        try: return f"{float(value):.{digits}f}{suffix}"
        except Exception: return str(value)
    def pct(v: Any) -> str: return val(v, "%", 0)
    def watts(v: Any) -> str: return val(v, " W", 1)
    def temp(v: Any) -> str: return val(v, "°C", 1)
    def mhz(v: Any) -> str:
        if v is None: return L["unavailable"]
        try:
            x=float(v)
            return f"{x/1000:.2f} GHz" if x>=1000 else f"{x:.0f} MHz"
        except Exception: return str(v)

    cpu_lines=[
        "CPU",
        f"• {L['load']}: {pct(cpu.get('percent'))}",
        f"• {L['clock']}: {mhz(cpu.get('frequency_mhz'))}",
        f"• {L['temp']}: {temp(cpu.get('temperature_c'))}",
        f"• {L['power']}: {watts(cpu.get('power_w'))}",
        f"• {L['cores']}: {cpu.get('physical_cores') or '—'} / {cpu.get('logical_cores') or '—'}",
    ]

    gpu_blocks=[]
    if gpus:
        for idx,g in enumerate(gpus[:3]):
            header=f"GPU{(' '+str(idx+1)) if len(gpus)>1 else ''} · {g.get('name') or 'GPU'}"
            block=[
                header,
                f"• {L['load']}: {pct(g.get('utilization_percent'))}",
                f"• {L['temp']}: {temp(g.get('temperature_c'))}",
                f"• {L['clock']}: {mhz(g.get('clock_mhz'))}",
                f"• {L['power']}: {watts(g.get('power_w'))}",
            ]
            total=g.get('memory_total_mb'); used=g.get('memory_used_mb')
            if total is not None:
                try:
                    total_g=float(total)/1024.0
                    used_g=float(used)/1024.0 if used is not None else None
                    free_g=max(0.0, total_g-used_g) if used_g is not None else None
                    block.append(f"• VRAM свободно: {free_g:.1f}/{total_g:.1f} GB" if free_g is not None else f"• VRAM свободно: —/{total_g:.1f} GB")
                    if used_g is not None:
                        block.append(f"• VRAM занято: {used_g:.1f} GB")
                except Exception:
                    block.append(f"• VRAM: {used if used is not None else '—'}/{total} MB")
            gpu_blocks.append("\n".join(block))
    else:
        gpu_blocks=["GPU\n"+f"• {L['telemetry']}: {L['unavailable']}"]

    mem_lines=[
        L["ram"],
        f"• {L['available']}: {mem['available_gb']:.1f}/{mem['total_gb']:.1f} GB",
        f"• {L['used']}: {mem['used_gb']:.1f} GB · {mem['percent']:.0f}%",
    ]
    disk_lines=[L["disks"]]
    for d in disks[:8]:
        mount=str(d.get('mount') or d.get('device') or '?')
        disk_lines.append(f"• {mount}: {L['free']} {d['free_gb']:.1f}/{d['total_gb']:.1f} GB · {L['disk_used']} {d['percent']:.0f}%")

    link=max([int(x.get('speed_mbps') or 0) for x in net.get('links',[])]+[0])
    net_lines=[
        L["network"],
        f"• {L['traffic']}: ↓ {val(net.get('download_mbps'),' Mbps',2)} · ↑ {val(net.get('upload_mbps'),' Mbps',2)}",
    ]
    if link:
        net_lines.append(f"• {L['link']}: {link} Mbps")

    if action=="cpu": return "\n".join(cpu_lines)
    if action=="gpu": return "\n\n".join(gpu_blocks)
    if action=="memory": return "\n".join(mem_lines)
    if action=="disks": return "\n".join(disk_lines)
    if action=="network": return "\n".join(net_lines)
    # V7.9: one readable order everywhere: CPU -> GPU -> RAM -> disks -> network.
    # The daily briefing reuses this exact formatter instead of maintaining a
    # second cramped CPU/RAM-only representation.
    return L["title"]+"\n\n"+"\n\n".join(["\n".join(cpu_lines), *gpu_blocks, "\n".join(mem_lines), "\n".join(disk_lines), "\n".join(net_lines)])
