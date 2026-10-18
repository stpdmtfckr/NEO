# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Stable microphone control API used by tools/voice routing.

V5.3 serializes Windows endpoint operations onto one dedicated audio-control
thread.  Telegram, GUI and agent workers may all request microphone changes, but
Core Audio COM objects are never created concurrently on arbitrary caller
threads.  Mute also remembers the endpoint level and restores it on unmute.
"""

from dataclasses import dataclass, asdict
import threading
from typing import Any

from jarvis.audio.control_worker import audio_control_executor
from jarvis.audio.providers.windows_core_audio import WindowsCoreAudioInputProvider
from jarvis.audio.providers.powershell_fallback import PowerShellAudioInputProvider


@dataclass(frozen=True)
class AudioInputDevice:
    device_id: str
    name: str
    is_default: bool = False
    provider: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AudioInputController:
    def __init__(self) -> None:
        self.native = WindowsCoreAudioInputProvider
        self.fallback = PowerShellAudioInputProvider
        # Lazy: do not create a background COM thread merely because the audio
        # tool is registered at NEO startup. It is spawned on the first real
        # microphone/device operation.
        self._executor = None
        self._state_lock = threading.RLock()
        # Last non-muted volume by concrete endpoint ID.  ``__default__`` is a
        # compatibility slot for fallback providers that cannot expose an ID.
        self._pre_mute_volume: dict[str, int] = {}

    def _call(self, fn, *args, timeout: float = 8.0, **kwargs):
        executor = self._executor
        if executor is None:
            executor = audio_control_executor()
            self._executor = executor
        return executor.submit(fn, *args, timeout=timeout, **kwargs)

    def _current_device_id(self, requested: str = "") -> str:
        requested = str(requested or "").strip()
        if requested:
            return requested
        if self.native.available():
            try:
                dev = self._call(self.native.default_device)
                return str(getattr(dev, "device_id", "") or "").strip()
            except Exception:
                pass
        return ""

    def list_devices(self) -> list[AudioInputDevice]:
        if self.native.available():
            try:
                devices = [
                    AudioInputDevice(d.device_id, d.name, d.is_default, self.native.name)
                    for d in self._call(self.native.list_devices)
                ]
                if devices:
                    return devices
            except Exception:
                pass
        # AudioDeviceCmdlets is slower than the native provider but still returns
        # stable Windows endpoint IDs, which is important for Telegram selection.
        try:
            return [
                AudioInputDevice(d.device_id, d.name, d.is_default, self.fallback.name)
                for d in self._call(self.fallback.list_devices, timeout=12.0)
            ]
        except Exception:
            pass
        # Last-resort display-only enumeration.
        try:
            import sounddevice as sd
            return [
                AudioInputDevice("", str(info.get("name", "")), False, "sounddevice")
                for info in sd.query_devices()
                if int(info.get("max_input_channels", 0)) > 0
            ]
        except Exception:
            return []

    def _get_volume_native_or_fallback(self, device_id: str = "") -> tuple[int, str, str]:
        if self.native.available():
            try:
                value = int(self._call(self.native.get_volume, device_id or None))
                return value, self.native.name, device_id
            except Exception as native_exc:
                if device_id:
                    raise RuntimeError(f"Core Audio could not read requested microphone id: {native_exc}") from native_exc
        value = int(self._call(self.fallback.get_volume, None, timeout=12.0))
        return value, self.fallback.name, ""

    def _require_addressable187(self, device_id: str) -> None:
        """HIST-052: an explicitly named microphone is never replaced by the default one."""
        if str(device_id or "").strip() and not self.native.available():
            raise RuntimeError("The requested microphone cannot be addressed: Core Audio is unavailable, "
                               "and the fallback controls only the default device.")

    def set_mute(self, muted: bool, *, device_id: str = "") -> dict[str, Any]:
        muted = bool(muted)
        self._require_addressable187(device_id)
        concrete_id = self._current_device_id(device_id)
        state_key = concrete_id or "__default__"

        # Save the level immediately before muting.  Some USB/Bluetooth drivers
        # expose mute as a zero-level transition; explicit restoration makes the
        # user-visible contract deterministic across devices.
        if muted:
            try:
                level, _, read_id = self._get_volume_native_or_fallback(concrete_id)
                # HIST-058: the saved level belongs to the endpoint it was READ from.
                # A default/fallback reading is never filed under an explicit ID.
                owner = read_id if read_id else ("__default__" if not concrete_id else "")
                if owner:
                    with self._state_lock:
                        self._pre_mute_volume[owner] = max(0, min(100, int(level)))
            except Exception:
                pass

        provider = ""
        actual = None
        if self.native.available():
            try:
                actual = bool(self._call(self.native.set_mute, muted, concrete_id or None))
                provider = self.native.name
            except Exception as native_exc:
                if concrete_id:
                    raise RuntimeError(f"Core Audio could not control requested microphone id: {native_exc}") from native_exc
        if actual is None:
            actual = bool(self._call(self.fallback.set_mute, muted, None, timeout=12.0))
            provider = self.fallback.name
            concrete_id = ""
            state_key = "__default__"

        restored_volume: int | None = None
        if not muted and actual is False:
            with self._state_lock:
                # HIST-058: no cross-device fallback — a level saved for «the default
                # device» (maybe another endpoint then) is never applied to this one.
                saved = self._pre_mute_volume.get(state_key)
            if saved is not None:
                try:
                    if provider == self.native.name and self.native.available():
                        restored_volume = int(self._call(self.native.set_volume, int(saved), concrete_id or None))
                    else:
                        restored_volume = int(self._call(self.fallback.set_volume, int(saved), None, timeout=12.0))
                except Exception:
                    restored_volume = None

        result = {
            "muted": bool(actual),
            "verified": bool(actual) == muted,
            "provider": provider,
            "device_id": concrete_id,
        }
        if muted:
            with self._state_lock:
                if state_key in self._pre_mute_volume:
                    result["saved_volume"] = self._pre_mute_volume[state_key]
        elif restored_volume is not None:
            result["restored_volume"] = restored_volume
        return result

    def get_volume(self, *, device_id: str = "") -> dict[str, Any]:
        self._require_addressable187(device_id)
        concrete_id = self._current_device_id(device_id)
        value, provider, provider_id = self._get_volume_native_or_fallback(concrete_id)
        return {"value": value, "verified": True, "provider": provider, "device_id": provider_id or concrete_id}

    def set_volume(self, value: int | float, *, device_id: str = "") -> dict[str, Any]:
        requested = max(0, min(100, int(round(float(value)))))
        self._require_addressable187(device_id)
        concrete_id = self._current_device_id(device_id)
        if self.native.available():
            try:
                actual = int(self._call(self.native.set_volume, requested, concrete_id or None))
                return {
                    "value": actual, "requested": requested,
                    "verified": abs(actual-requested) <= 3,
                    "provider": self.native.name, "device_id": concrete_id,
                }
            except Exception as native_exc:
                if concrete_id:
                    raise RuntimeError(f"Core Audio could not control requested microphone id: {native_exc}") from native_exc
        actual = int(self._call(self.fallback.set_volume, requested, None, timeout=12.0))
        return {
            "value": actual, "requested": requested,
            "verified": abs(actual-requested) <= 3,
            "provider": self.fallback.name, "device_id": "",
        }

    def set_default(self, *, device_id: str = "", device_name: str = "") -> dict[str, Any]:
        requested_id = str(device_id or "").strip()
        requested_name = str(device_name or "").strip()
        if requested_id and self.native.available():
            try:
                device = self._call(self.native.set_default, requested_id)
                return AudioInputDevice(device.device_id, device.name, True, self.native.name).as_dict() | {"verified": True}
            except NotImplementedError:
                pass
            except Exception:
                # Fall through to the compatibility provider. Some drivers expose
                # the endpoint but reject PolicyConfig; AudioDeviceCmdlets may still work.
                pass
        device = self._call(
            self.fallback.set_default,
            device_id=requested_id,
            device_name=requested_name,
            timeout=15.0,
        )
        actual_id = str(device.device_id or "").strip()
        actual_name = str(device.name or "").strip()
        verified = bool(actual_id and actual_id == requested_id) if requested_id else bool(
            requested_name and actual_name and requested_name.casefold() in actual_name.casefold()
        )
        return {
            **AudioInputDevice(actual_id, actual_name, True, self.fallback.name).as_dict(),
            "verified": verified,
        }


__all__ = ["AudioInputController", "AudioInputDevice"]
