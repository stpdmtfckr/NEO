# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Stable Windows playback-endpoint enumeration/default-device switching.

Native Core Audio/pycaw is preferred. AudioDeviceCmdlets remains a compatibility
fallback for older bundled pycaw versions or unusual driver/PolicyConfig failures.
"""

from dataclasses import dataclass, asdict
import json
import os
import subprocess
from typing import Any

from jarvis.audio.providers.windows_core_audio import WindowsCoreAudioOutputProvider
from jarvis.audio.control_worker import AudioJobOutcomeUnknown, audio_control_executor
from jarvis.runtime.hidden_subprocess import hidden_process_kwargs


@dataclass(frozen=True)
class AudioOutputDevice:
    device_id: str
    name: str
    is_default: bool = False
    provider: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AudioOutputController:
    provider = "powershell_audio_device_cmdlets"
    native = WindowsCoreAudioOutputProvider

    @classmethod
    def _call(cls, fn, *args, timeout: float = 8.0, **kwargs):
        return audio_control_executor().submit(fn, *args, timeout=timeout, **kwargs)

    @staticmethod
    def _run(script: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        if os.name != "nt":
            raise OSError("Устройства вывода Windows доступны только в Windows")
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True, capture_output=True, timeout=timeout, check=False,
            **hidden_process_kwargs(),
        )

    @classmethod
    def _ensure(cls, proc: subprocess.CompletedProcess[str]) -> str:
        if proc.returncode != 0:
            detail = str(proc.stderr or "").strip() or str(proc.stdout or "").strip()
            raise RuntimeError(
                "AudioDeviceCmdlets failed. Run install_audio_device_cmdlets.ps1. " + detail
            )
        return str(proc.stdout or "").strip()

    @staticmethod
    def _rows(raw: str) -> list[dict[str, Any]]:
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    @classmethod
    def _powershell_devices(cls) -> list[AudioOutputDevice]:
        out = cls._ensure(cls._run(
            "Import-Module AudioDeviceCmdlets -ErrorAction Stop; "
            "$current=Get-AudioDevice -Playback; "
            "$items=Get-AudioDevice -List | Where-Object {$_.Type -eq 'Playback'} | "
            "Select-Object ID,Name,@{N='Default';E={$_.ID -eq $current.ID}}; "
            "$items | ConvertTo-Json -Compress"
        ))
        devices: list[AudioOutputDevice] = []
        for item in cls._rows(out):
            ident = str(item.get("ID", "") or item.get("Id", "") or "").strip()
            name = str(item.get("Name", "") or "").strip()
            if ident or name:
                devices.append(AudioOutputDevice(ident, name or ident, bool(item.get("Default", False)), cls.provider))
        return devices

    @classmethod
    def list_devices(cls) -> list[AudioOutputDevice]:
        if cls.native.available():
            try:
                devices = [AudioOutputDevice(d.device_id, d.name, d.is_default, cls.native.name) for d in cls._call(cls.native.list_devices)]
                if devices:
                    return devices
            except Exception:
                pass
        return cls._call(cls._powershell_devices, timeout=12.0)

    @classmethod
    def _powershell_set_default(cls, *, device_id: str = "", device_name: str = "") -> dict[str, Any]:
        if not device_id and not device_name:
            raise ValueError("device_id or device_name is required")
        if device_id:
            escaped = str(device_id).replace("'", "''")
            where = f"$_.Type -eq 'Playback' -and $_.ID -eq '{escaped}'"
        else:
            escaped = str(device_name).replace("'", "''")
            where = f"$_.Type -eq 'Playback' -and $_.Name -like '*{escaped}*'"
        out = cls._ensure(cls._run(
            "Import-Module AudioDeviceCmdlets -ErrorAction Stop; "
            + f"$d=Get-AudioDevice -List | Where-Object {{{where}}} | Select-Object -First 1; "
              "if(-not $d){throw 'Audio device not found'}; Set-AudioDevice -ID $d.ID; "
              "$v=Get-AudioDevice -Playback; "
              "[pscustomobject]@{ID=$d.ID;Name=$d.Name;Default=$true;CurrentID=$v.ID;CurrentName=$v.Name} | ConvertTo-Json -Compress"
        ))
        # HIST-056: the result is verified only by the stable ID read back AFTER the
        # switch (CurrentID) — never by the selected row's own ID, a name match or a
        # malformed/partial answer; a missing readback is reported as such.
        try:
            rows = cls._rows(out)
        except ValueError:
            rows = []
        data = dict(rows[0]) if rows else {}
        selected_id = str(data.get("ID", "") or "").strip()
        current_id = str(data.get("CurrentID", "") or "").strip()
        requested_id = str(device_id or selected_id or "").strip()
        verified = bool(current_id and requested_id and current_id == requested_id)
        name = str((data.get("CurrentName") if verified else data.get("Name")) or device_name or "").strip()
        return {
            "device_id": current_id or requested_id, "name": name, "is_default": verified,
            "provider": cls.provider, "verified": verified,
            "readback": "confirmed" if verified else ("mismatch" if current_id else "missing"),
        }

    @classmethod
    def set_default(cls, *, device_id: str = "", device_name: str = "") -> dict[str, Any]:
        requested_id = str(device_id or "").strip()
        if requested_id and cls.native.available():
            try:
                d = cls._call(cls.native.set_default, requested_id)
                return {"device_id": d.device_id, "name": d.name, "is_default": True, "provider": cls.native.name, "verified": True}
            except NotImplementedError:
                pass
            except AudioJobOutcomeUnknown:
                # HIST-054: the native switch may still land; a PowerShell replay now
                # could race it. Report the unknown outcome instead.
                return {"device_id": requested_id, "name": str(device_name or ""), "is_default": None,
                        "provider": cls.native.name, "verified": False, "outcome_unknown": True}
            except Exception:
                pass
        return cls._call(
            cls._powershell_set_default, device_id=requested_id,
            device_name=str(device_name or "").strip(), timeout=15.0,
        )


__all__ = ["AudioOutputController", "AudioOutputDevice"]
