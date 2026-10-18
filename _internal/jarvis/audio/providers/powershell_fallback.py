# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""AudioDeviceCmdlets compatibility provider.

This remains available for default-endpoint switching and as a safety net when a
machine/driver does not expose a usable Core Audio endpoint to pycaw.
"""

from dataclasses import dataclass
import json
import os
import re
import subprocess
from typing import Any

from jarvis.runtime.hidden_subprocess import hidden_process_kwargs


@dataclass(frozen=True)
class PowerShellInputDevice:
    device_id: str
    name: str
    is_default: bool = False


class PowerShellAudioInputProvider:
    name = "powershell_audio_device_cmdlets"

    @staticmethod
    def _run(script: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True, capture_output=True, timeout=timeout, check=False,
            **hidden_process_kwargs(),
        )

    @classmethod
    def _ensure(cls, proc: subprocess.CompletedProcess[str]) -> str:
        if proc.returncode != 0:
            raise RuntimeError(
                "AudioDeviceCmdlets failed. Run install_audio_device_cmdlets.ps1. "
                + (str(proc.stderr or "").strip() or str(proc.stdout or "").strip())
            )
        return str(proc.stdout or "").strip()

    @classmethod
    def list_devices(cls) -> list[PowerShellInputDevice]:
        if os.name != "nt":
            raise OSError("Windows only")
        out = cls._ensure(cls._run(
            "Import-Module AudioDeviceCmdlets -ErrorAction Stop; "
            "$current=Get-AudioDevice -Recording; "
            "$items=Get-AudioDevice -List | Where-Object {$_.Type -eq 'Recording'} | "
            "Select-Object ID,Name,@{N='Default';E={$_.ID -eq $current.ID}}; "
            "$items | ConvertTo-Json -Compress"
        ))
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        result: list[PowerShellInputDevice] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("ID", "") or item.get("Id", "") or "").strip()
            name = str(item.get("Name", "") or "").strip()
            if ident or name:
                result.append(PowerShellInputDevice(ident, name or ident, bool(item.get("Default", False))))
        return result

    @classmethod
    def set_mute(cls, muted: bool, device_id: str | None = None) -> bool:
        if os.name != "nt":
            raise OSError("Windows only")
        flag = "1" if muted else "0"
        if device_id:
            escaped = str(device_id).replace("'", "''")
            prefix = f"$d=Get-AudioDevice -List | Where-Object {{$_.ID -eq '{escaped}'}} | Select-Object -First 1; if(-not $d){{throw 'Audio device not found'}}; Set-AudioDevice -ID $d.ID; "
        else:
            prefix = ""
        out = cls._ensure(cls._run(
            "Import-Module AudioDeviceCmdlets -ErrorAction Stop; " + prefix
            + f"Set-AudioDevice -RecordingMute {flag} -ErrorAction Stop | Out-Null; "
              "$v=Get-AudioDevice -RecordingMute; Write-Output $v"
        ))
        return out.casefold() in {"true", "1"}

    @classmethod
    def get_volume(cls, device_id: str | None = None) -> int:
        if device_id:
            # AudioDeviceCmdlets' RecordingVolume targets the default capture
            # endpoint. Avoid silently switching merely for a read of a nondefault
            # endpoint; native Core Audio is required for stable per-id access.
            raise NotImplementedError("non-default input volume requires Core Audio")
        out = cls._ensure(cls._run(
            "Import-Module AudioDeviceCmdlets -ErrorAction Stop; $v=Get-AudioDevice -RecordingVolume; Write-Output $v"
        ))
        match = re.search(r"-?\d+(?:[.,]\d+)?", out)
        return max(0, min(100, int(round(float(match.group(0).replace(',', '.')))))) if match else 0

    @classmethod
    def set_volume(cls, value: int | float, device_id: str | None = None) -> int:
        if device_id:
            raise NotImplementedError("non-default input volume requires Core Audio")
        level = max(0, min(100, int(round(float(value)))))
        out = cls._ensure(cls._run(
            "Import-Module AudioDeviceCmdlets -ErrorAction Stop; "
            + f"Set-AudioDevice -RecordingVolume {level} -ErrorAction Stop | Out-Null; "
              "$v=Get-AudioDevice -RecordingVolume; Write-Output $v"
        ))
        match = re.search(r"-?\d+(?:[.,]\d+)?", out)
        return max(0, min(100, int(round(float(match.group(0).replace(',', '.')))))) if match else level

    @classmethod
    def set_default(cls, *, device_id: str = "", device_name: str = "") -> PowerShellInputDevice:
        if os.name != "nt":
            raise OSError("Windows only")
        if device_id:
            escaped = device_id.replace("'", "''")
            where = f"$_.Type -eq 'Recording' -and $_.ID -eq '{escaped}'"
        elif device_name:
            escaped = device_name.replace("'", "''")
            where = f"$_.Type -eq 'Recording' -and $_.Name -like '*{escaped}*'"
        else:
            raise ValueError("device_id or device_name is required")
        out = cls._ensure(cls._run(
            "Import-Module AudioDeviceCmdlets -ErrorAction Stop; "
            + f"$d=Get-AudioDevice -List | Where-Object {{{where}}} | Select-Object -First 1; "
              "if(-not $d){throw 'Audio device not found'}; Set-AudioDevice -ID $d.ID; "
              "$v=Get-AudioDevice -Recording; Write-Output ($v.ID + '||' + $v.Name)"
        ))
        ident, _, name = out.partition("||")
        return PowerShellInputDevice(ident.strip(), name.strip() or device_name, True)


__all__ = ["PowerShellAudioInputProvider", "PowerShellInputDevice"]
