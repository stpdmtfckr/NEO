# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any


_BT_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])
_BT_PAIR_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])
_BT_PNP_CACHE: tuple[float, str] = (0.0, "")
_BT_LOCK = threading.RLock()
_WINRT_INVENTORY_TIMEOUT = 4.0


class _BluetoothRows(list):
    """An empty successful inventory is different from a missing WinRT driver."""
    def __init__(self, rows=(), *, available=False):
        super().__init__(rows)
        self.available = available


@dataclass(slots=True)
class BluetoothDeviceInfo:
    name: str
    instance_id: str = ""
    connected: bool = False
    battery: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hidden_flags() -> int:
    return (int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) |
            int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000))) if os.name == "nt" else 0


def _decode_powershell_output(raw: bytes) -> str:
    """Decode PowerShell pipes without producing replacement-character soup."""
    payload = bytes(raw or b"")
    if not payload:
        return ""
    encodings = (
        ("utf-16", "utf-8-sig", "utf-8", "cp866", "cp1251")
        if payload.count(b"\x00") > len(payload) // 8
        else ("utf-8-sig", "utf-8", "cp866", "cp1251", "utf-16")
    )
    for encoding in encodings:
        try:
            return payload.decode(encoding).strip()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return payload.decode("utf-8", errors="ignore").strip()


def _powershell(script: str, timeout: float = 8.0) -> str:
    if os.name != "nt":
        return ""
    # Windows PowerShell 5.1 otherwise writes redirected output using the
    # console/OEM code page. Force UTF-8 before ConvertTo-Json so Cyrillic and
    # vendor device names survive the subprocess pipe intact.
    utf8_prefix = (
        "$OutputEncoding=[Console]::OutputEncoding="
        "[System.Text.UTF8Encoding]::new($false);"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", utf8_prefix + script],
        capture_output=True, text=False,
        timeout=timeout, creationflags=_hidden_flags(),
    )
    if proc.returncode != 0:
        return ""
    return _decode_powershell_output(proc.stdout)




def _friendly_bluetooth_name(value: Any) -> str:
    """Return a user-facing Bluetooth device name, filtering Windows service nodes."""
    name = " ".join(str(value or "").replace("\x00", " ").replace("\ufffd", " ").split()).strip(" ,;|-")
    if not name or len(name) < 2:
        return ""
    low = name.casefold()
    # Get-PnpDevice exposes profile/service children next to the actual paired
    # device. They are not connectable products and made the Telegram list noisy.
    noisy_exact = {
        "bluetooth le device", "generic bluetooth adapter", "microsoft bluetooth enumerator",
        "microsoft bluetooth le enumerator", "bluetooth device (rfcomm protocol tdi)",
    }
    noisy_tokens = (
        "device identification service", "служба идентификации", "generic attribute service",
        "generic access profile", "gatt service", "rfcomm", "avrcp transport",
        "audio sink", "audio source", "hands-free service", "handsfree service",
        "bluetooth peripheral device", "bluetooth enumerator", "bluetooth radio",
        "microsoft bluetooth",
    )
    if low in noisy_exact or any(token in low for token in noisy_tokens):
        return ""
    # Service-shaped names are almost always PnP children. Keep real products
    # such as "Surface ..." by only rejecting an explicit service suffix.
    if low.endswith(" service") or low.endswith(" служба"):
        return ""
    return name


def _winrt_bluetooth_devices() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    async def collect() -> list[dict[str, Any]]:
        from winsdk.windows.devices.bluetooth import BluetoothConnectionStatus, BluetoothDevice, BluetoothLEDevice  # type: ignore
        from winsdk.windows.devices.enumeration import DeviceInformation  # type: ignore
        rows: dict[str, dict[str, Any]] = {}
        successful = 0
        for cls in (BluetoothDevice, BluetoothLEDevice):
            try:
                all_selector = cls.get_device_selector_from_pairing_state(True)
                connected_selector = cls.get_device_selector_from_connection_status(BluetoothConnectionStatus.CONNECTED)
                all_infos = await DeviceInformation.find_all_async(all_selector)
                conn_infos = await DeviceInformation.find_all_async(connected_selector)
                successful += 1
                connected_ids = {str(getattr(x, "id", "") or "") for x in conn_infos}
                for info in all_infos:
                    name = _friendly_bluetooth_name(getattr(info, "name", ""))
                    iid = str(getattr(info, "id", "") or "")
                    if not name:
                        continue
                    key = name.casefold()
                    row = rows.setdefault(key, {"name": name, "instance_id": iid, "connected": False, "battery": None})
                    row["connected"] = bool(row.get("connected")) or iid in connected_ids
            except Exception:
                continue
        return _BluetoothRows(rows.values(), available=successful == 2)
    async def bounded_collect():
        # Some Windows radio drivers never complete a WinRT enumeration. Keep
        # time for the independent PnP fallback within the briefing's budget.
        return await asyncio.wait_for(collect(), timeout=_WINRT_INVENTORY_TIMEOUT)
    try:
        return asyncio.run(bounded_collect())
    except Exception:
        return []


def list_bluetooth_devices(*, connected_only: bool = False, force: bool = False, background: bool = False) -> list[dict[str, Any]]:
    """Share concurrent inventory requests; background connections stay live.

    The 30-second notification monitor refreshes WinRT connection state, while
    battery/PnP enrichment is reused for five minutes. Explicit device/briefing
    requests retain their 12-second freshness and force bypasses every cache.
    When WinRT is unavailable, the PnP fallback retries once per minute.
    """
    with _BT_LOCK:
        return _list_bluetooth_devices(connected_only=connected_only, force=force, background=background)


def _list_bluetooth_devices(*, connected_only: bool, force: bool, background: bool) -> list[dict[str, Any]]:
    global _BT_CACHE, _BT_PNP_CACHE
    if os.name != "nt":
        return []
    cached_at, cached_rows = _BT_CACHE
    if not force and cached_at > 0 and time.monotonic() - cached_at < 12.0:
        rows = [dict(x) for x in cached_rows]
        return [x for x in rows if bool(x.get("connected"))] if connected_only else rows
    winrt_rows = _winrt_bluetooth_devices()
    authoritative = bool(getattr(winrt_rows, "available", bool(winrt_rows)))
    script = r'''
$ErrorActionPreference='SilentlyContinue'
$skip='Enumerator|Radio|Adapter|RFCOMM|Microsoft Bluetooth|Generic Bluetooth|Bluetooth Device \(RFCOMM'
$out=@(Get-PnpDevice -Class Bluetooth -PresentOnly | Where-Object { $_.FriendlyName -and $_.FriendlyName -notmatch $skip } | ForEach-Object {
  $d=$_; $connected=$false; $battery=$null
  $props=Get-PnpDeviceProperty -InstanceId $d.InstanceId
  foreach($p in $props) {
    if($p.KeyName -match 'IsConnected$') { try { $connected=[bool]$p.Data } catch {} }
    if($p.KeyName -match 'Battery|PowerLevel') {
      try { $n=[int]$p.Data; if($n -ge 0 -and $n -le 100) { $battery=$n } } catch {}
    }
  }
  [pscustomobject]@{name=[string]$d.FriendlyName;instance_id=[string]$d.InstanceId;connected=$connected;battery=$battery}
})
$out | ConvertTo-Json -Compress
'''
    pnp_at, raw = _BT_PNP_CACHE
    ttl = 300.0 if authoritative else 60.0
    if force or not background or pnp_at <= 0 or time.monotonic() - pnp_at >= ttl:
        try:
            raw = _powershell(script)
        except (OSError, subprocess.SubprocessError):
            raw = ""
        _BT_PNP_CACHE = (time.monotonic(), raw)
    if not raw:
        values = list(winrt_rows)
        if not background:
            _BT_CACHE = (time.monotonic(), [dict(x) for x in values])
        return [x for x in values if bool(x.get("connected"))] if connected_only else values
    try:
        data = json.loads(raw)
    except Exception:
        values = list(winrt_rows)
        if not background:
            _BT_CACHE = (time.monotonic(), [dict(x) for x in values])
        return [x for x in values if bool(x.get("connected"))] if connected_only else values
    if isinstance(data, dict):
        data = [data]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    # WinRT's pairing-state selector is authoritative for the phone UI. PnP can
    # also expose nearby/unpaired association endpoints; when WinRT is available
    # use PowerShell only to enrich already-paired product rows.
    paired_names = {str(x.get("name", "") or "").casefold() for x in winrt_rows if str(x.get("name", "") or "").strip()}
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        name = _friendly_bluetooth_name(row.get("name", ""))
        if not name or name.casefold() in seen:
            continue
        if authoritative and name.casefold() not in paired_names:
            continue
        seen.add(name.casefold())
        battery = row.get("battery")
        try:
            battery = int(battery) if battery is not None else None
        except Exception:
            battery = None
        item = BluetoothDeviceInfo(
            name=name,
            instance_id=str(row.get("instance_id", "") or ""),
            connected=bool(row.get("connected", False)),
            battery=battery if isinstance(battery, int) and 0 <= battery <= 100 else None,
        ).to_dict()
        # Cache the full paired inventory. The briefing's connected-only read
        # must not hide disconnected products from the next normal query.
        result.append(item)
    merged = {str(x.get("name", "") or "").casefold(): dict(x) for x in result if str(x.get("name", "") or "").strip()}
    for item in winrt_rows:
        key = str(item.get("name", "") or "").casefold()
        if not key:
            continue
        row = merged.setdefault(key, dict(item))
        row["connected"] = bool(item.get("connected")) if authoritative else bool(row.get("connected")) or bool(item.get("connected"))
        if row.get("battery") is None and item.get("battery") is not None:
            row["battery"] = item.get("battery")
    values = list(merged.values())
    if not background:
        _BT_CACHE = (time.monotonic(), [dict(x) for x in values])
    return [x for x in values if bool(x.get("connected"))] if connected_only else values



def list_pairable_bluetooth_devices(*, force: bool = False) -> list[dict[str, Any]]:
    """Discover nearby *unpaired* Bluetooth products via WinRT, without opening Settings."""
    global _BT_PAIR_CACHE
    if os.name != "nt":
        return []
    cached_at, cached_rows = _BT_PAIR_CACHE
    if not force and cached_rows and time.monotonic() - cached_at < 10.0:
        return [dict(x) for x in cached_rows]

    async def collect() -> list[dict[str, Any]]:
        from winsdk.windows.devices.bluetooth import BluetoothDevice, BluetoothLEDevice  # type: ignore
        from winsdk.windows.devices.enumeration import DeviceInformation  # type: ignore

        rows: dict[str, dict[str, Any]] = {}
        for cls, kind in ((BluetoothDevice, "classic"), (BluetoothLEDevice, "le")):
            try:
                selector = cls.get_device_selector_from_pairing_state(False)
                infos = await DeviceInformation.find_all_async(selector)
            except Exception:
                continue
            for info in infos:
                name = _friendly_bluetooth_name(getattr(info, "name", ""))
                device_id = str(getattr(info, "id", "") or "").strip()
                if not name or not device_id:
                    continue
                key = name.casefold()
                rows.setdefault(key, {
                    "name": name,
                    "device_id": device_id,
                    "kind": kind,
                    "paired": False,
                })
        return sorted(rows.values(), key=lambda x: str(x.get("name", "")).casefold())

    try:
        values = asyncio.run(collect())
    except Exception:
        values = []
    _BT_PAIR_CACHE = (time.monotonic(), [dict(x) for x in values])
    return values


def pair_bluetooth_device(device_id: str, name: str = "") -> tuple[bool, str]:
    """Pair one discovered Bluetooth device using the native WinRT pairing API."""
    global _BT_CACHE, _BT_PAIR_CACHE, _BT_PNP_CACHE
    target_id = str(device_id or "").strip()
    target_name = _friendly_bluetooth_name(name) or "Bluetooth-устройство"
    if os.name != "nt" or not target_id:
        return False, "Bluetooth-сопряжение доступно только в Windows."

    async def pair() -> tuple[bool, str]:
        from winsdk.windows.devices.enumeration import DeviceInformation, DevicePairingResultStatus  # type: ignore

        info = await DeviceInformation.create_from_id_async(target_id)
        if info is None:
            return False, f"Устройство «{target_name}» больше не найдено. Обнови список."
        pairing = getattr(info, "pairing", None)
        if pairing is None:
            return False, f"Устройство «{target_name}» не поддерживает стандартное сопряжение Windows."
        if bool(getattr(pairing, "is_paired", False)):
            return True, f"«{target_name}» уже сопряжено."
        if not bool(getattr(pairing, "can_pair", True)):
            return False, f"«{target_name}» сейчас не разрешает сопряжение. Переведи устройство в режим pairing и обнови список."

        result = await pairing.pair_async()
        status = getattr(result, "status", None)
        success_values = {
            getattr(DevicePairingResultStatus, "PAIRED", object()),
            getattr(DevicePairingResultStatus, "ALREADY_PAIRED", object()),
        }
        if status in success_values:
            return True, f"Bluetooth-устройство «{target_name}» сопряжено."
        status_name = str(getattr(status, "name", "") or status or "unknown").replace("_", " ").lower()
        return False, f"Не удалось сопрячь «{target_name}»: {status_name}. Если Windows требует PIN/подтверждение, подтверди его на устройстве и повтори."

    try:
        ok, text = asyncio.run(pair())
    except Exception as exc:
        return False, f"Не удалось сопрячь «{target_name}»: {exc}"
    if ok:
        with _BT_LOCK:
            _BT_CACHE = (0.0, [])
            _BT_PAIR_CACHE = (0.0, [])
            _BT_PNP_CACHE = (0.0, "")
    return ok, text

def _clean_present_device_name(value: Any) -> str:
    name = str(value or "").replace("\ufffd", " ")
    name = " ".join(name.replace("\x00", " ").split()).strip(" ,;|-\t")
    if not name or len(name) < 2:
        return ""
    low = name.casefold()
    noisy = (
        "hid-compliant", "hid compliant", "usb input device", "generic",
        "enumerator", "rfcomm", "bluetooth radio", "microsoft bluetooth",
        "system device", "usb composite device", "nvidia high definition audio",
    )
    if any(token in low for token in noisy):
        return ""
    if "vid_" in low or "pid_" in low or low.startswith("hid\\") or low.startswith("usb\\"):
        return ""
    return name


def list_present_devices(limit: int = 16) -> list[str]:
    """Return a bounded list of user-facing present Windows devices.

    Do not dump every PnP/HID child node into the daily briefing: those are
    implementation details and are often duplicated.  Only friendly endpoint
    classes are kept, with category labels and strict de-duplication.
    """
    if os.name != "nt":
        return []
    limit = max(1, min(30, int(limit or 16)))
    script = rf"""
$ErrorActionPreference='SilentlyContinue'
$classes=@('AudioEndpoint','Bluetooth','Camera','Keyboard','Mouse')
$out=@(Get-PnpDevice -PresentOnly | Where-Object {{
  $_.Status -eq 'OK' -and $_.FriendlyName -and $classes -contains $_.Class
}} | Select-Object Class,FriendlyName -Unique)
$out | ConvertTo-Json -Compress
"""
    raw = _powershell(script)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    labels = {
        "audioendpoint": "Аудио", "bluetooth": "Bluetooth",
        "camera": "Камера", "keyboard": "Клавиатура", "mouse": "Мышь",
    }
    result: list[str] = []
    seen: set[str] = set()
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        name = _clean_present_device_name(row.get("FriendlyName"))
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        category = labels.get(str(row.get("Class", "") or "").casefold(), "Устройство")
        result.append(f"{category}: {name}")
        if len(result) >= limit:
            break
    return result


def set_device_connection(name: str, connected: bool) -> tuple[bool, str]:
    """Use Windows Settings UIA for generic BT profile connect/disconnect.

    Windows has no universal public Win32/WinRT connect/disconnect verb that works
    for every paired audio/HID profile. UI Automation mirrors the same supported
    Settings action the user would click and therefore avoids destructive PnP
    disable/enable tricks.
    """
    target = str(name or "").strip()
    if os.name != "nt" or not target:
        return False, "Bluetooth-управление доступно только в Windows."
    try:
        os.startfile("ms-settings:bluetooth")  # type: ignore[attr-defined]
        time.sleep(1.1)
        from pywinauto import Desktop  # type: ignore
        desktop = Desktop(backend="uia")
        windows = [w for w in desktop.windows() if "Settings" in str(w.window_text()) or "Парамет" in str(w.window_text())]
        wanted = ("Connect", "Подключить") if connected else ("Disconnect", "Отключить")
        for win in windows:
            try:
                matches = [c for c in win.descendants() if target.casefold() in str(c.window_text() or "").casefold()]
            except Exception:
                continue
            for match in matches[:4]:
                node = match
                for _ in range(5):
                    try:
                        buttons = [c for c in node.descendants(control_type="Button") if any(x.casefold() in str(c.window_text() or "").casefold() for x in wanted)]
                    except Exception:
                        buttons = []
                    if buttons:
                        buttons[0].click_input()
                        return True, f"{'Подключаю' if connected else 'Отключаю'} Bluetooth-устройство «{target}»."
                    try:
                        node = node.parent()
                    except Exception:
                        break
        return False, f"Не нашёл кнопку {'подключения' if connected else 'отключения'} для «{target}». Открыл параметры Bluetooth."
    except Exception as exc:
        return False, f"Не удалось управлять «{target}»: {exc}"
