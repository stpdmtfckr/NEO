# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Native Windows capture-endpoint control through Core Audio/pycaw.

No PowerShell process is spawned for mute/volume reads or writes.  On current
pycaw builds this provider also switches the Windows default endpoint through
PolicyConfig/SetDefaultDevice and verifies the selected endpoint.  The controller
keeps AudioDeviceCmdlets only as a compatibility fallback for drivers or runtimes
where native default-device switching is unavailable.
"""

from dataclasses import dataclass
import os
import threading
from typing import Any

_COM_THREAD_STATE = threading.local()


@dataclass(frozen=True)
class NativeInputDevice:
    device_id: str
    name: str
    is_default: bool = False


class WindowsCoreAudioInputProvider:
    name = "windows_core_audio"

    @staticmethod
    def available() -> bool:
        if os.name != "nt":
            return False
        try:
            import comtypes  # noqa: F401
            import pycaw  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _imports():
        if os.name != "nt":
            raise OSError("Core Audio is available only on Windows")
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL, CoInitialize
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, EDataFlow, ERole
        try:
            from pycaw.constants import DEVICE_STATE
        except Exception:
            DEVICE_STATE = None
        # COM initialization is per-thread and reference-counted.  Calling
        # CoInitialize on every endpoint operation leaked the COM init count on
        # the long-lived Telegram/audio worker.  Initialize once per thread.
        if not bool(getattr(_COM_THREAD_STATE, "initialized", False)):
            CoInitialize()
            _COM_THREAD_STATE.initialized = True
        return POINTER, cast, CLSCTX_ALL, AudioUtilities, IAudioEndpointVolume, EDataFlow, ERole, DEVICE_STATE

    @classmethod
    def _enumerator(cls):
        _, _, _, AudioUtilities, _, _, _, _ = cls._imports()
        getter = getattr(AudioUtilities, "GetDeviceEnumerator", None)
        if callable(getter):
            return getter()
        # Older pycaw compatibility.
        import comtypes
        from pycaw.pycaw import CLSID_MMDeviceEnumerator, IMMDeviceEnumerator
        return comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
        )

    @classmethod
    def _default_raw_device(cls):
        _, _, _, _, _, EDataFlow, ERole, _ = cls._imports()
        return cls._enumerator().GetDefaultAudioEndpoint(
            EDataFlow.eCapture.value, ERole.eMultimedia.value
        )

    @classmethod
    def _raw_device(cls, device_id: str | None = None):
        if device_id:
            return cls._enumerator().GetDevice(str(device_id))
        return cls._default_raw_device()

    @classmethod
    def _endpoint_volume(cls, device_id: str | None = None):
        POINTER, cast, CLSCTX_ALL, _, IAudioEndpointVolume, _, _, _ = cls._imports()
        dev = cls._raw_device(device_id)
        endpoint = getattr(dev, "EndpointVolume", None)
        if endpoint is not None:
            return endpoint
        raw = getattr(dev, "_dev", None) or getattr(dev, "device", None) or dev
        if not hasattr(raw, "Activate"):
            raise RuntimeError("Core Audio endpoint does not expose IAudioEndpointVolume")
        interface = raw.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))

    @classmethod
    def _device_id(cls, dev: Any) -> str:
        for candidate in (dev, getattr(dev, "_dev", None), getattr(dev, "device", None)):
            if candidate is None:
                continue
            value = getattr(candidate, "id", None)
            if value:
                return str(value)
            getter = getattr(candidate, "GetId", None)
            if callable(getter):
                try:
                    return str(getter())
                except Exception:
                    pass
        return ""

    @classmethod
    def _wrapper_devices_for_flow(cls, flow_value: int) -> list[Any]:
        _, _, _, AudioUtilities, _, _, _, DEVICE_STATE = cls._imports()
        getter = getattr(AudioUtilities, "GetAllDevices", None)
        active = getattr(DEVICE_STATE, "ACTIVE", None) if DEVICE_STATE is not None else None
        active_value = getattr(active, "value", active) if active is not None else 1
        if callable(getter):
            # pycaw >= 20251023 uses data_flow/device_state.  Do not fall back to
            # unfiltered GetAllDevices when those keywords are unsupported: that
            # was the V4.7 bug that labelled render endpoints as microphones.
            try:
                return list(getter(data_flow=flow_value, device_state=active_value))
            except TypeError:
                pass
            except Exception:
                pass
        # Version-independent Core Audio fallback.  EnumAudioEndpoints exists in
        # older pycaw builds and gives the OS-native capture/render separation.
        try:
            enumerator = cls._enumerator()
            collection = enumerator.EnumAudioEndpoints(flow_value, int(active_value))
            count = int(collection.GetCount())
            out: list[Any] = []
            create = getattr(AudioUtilities, "CreateDevice", None)
            for i in range(count):
                raw = collection.Item(i)
                if raw is None:
                    continue
                if callable(create):
                    try:
                        out.append(create(raw))
                        continue
                    except Exception:
                        pass
                out.append(raw)
            return out
        except Exception:
            return []

    @classmethod
    def _wrapper_devices(cls) -> list[Any]:
        _, _, _, _, _, EDataFlow, _, _ = cls._imports()
        return cls._wrapper_devices_for_flow(EDataFlow.eCapture.value)

    @classmethod
    def _friendly_name_for_id(cls, device_id: str) -> str:
        for dev in cls._wrapper_devices():
            if cls._device_id(dev) != device_id:
                continue
            for attr in ("FriendlyName", "friendly_name", "name"):
                value = getattr(dev, attr, None)
                if value:
                    return str(value)
        return device_id

    @classmethod
    def default_device(cls) -> NativeInputDevice:
        dev = cls._default_raw_device()
        device_id = cls._device_id(dev)
        return NativeInputDevice(device_id, cls._friendly_name_for_id(device_id), True)

    @classmethod
    def list_devices(cls) -> list[NativeInputDevice]:
        default_id = ""
        try:
            default_id = cls._device_id(cls._default_raw_device())
        except Exception:
            pass
        out: list[NativeInputDevice] = []
        seen: set[str] = set()
        for dev in cls._wrapper_devices():
            device_id = cls._device_id(dev)
            if not device_id or device_id in seen:
                continue
            # Some pycaw versions return render+capture wrappers from GetAllDevices.
            # Only keep devices whose endpoint-volume interface can be opened as a
            # capture candidate when flow metadata is unavailable.
            flow = getattr(dev, "data_flow", getattr(dev, "DataFlow", None))
            if flow is not None:
                flow_text = str(flow).casefold()
                if "capture" not in flow_text and str(flow) not in {"1", "EDataFlow.eCapture"}:
                    continue
            seen.add(device_id)
            name = ""
            for attr in ("FriendlyName", "friendly_name", "name"):
                value = getattr(dev, attr, None)
                if value:
                    name = str(value)
                    break
            out.append(NativeInputDevice(device_id, name or device_id, device_id == default_id))
        # Ensure the default capture endpoint is always represented.
        if default_id and default_id not in seen:
            out.insert(0, NativeInputDevice(default_id, cls._friendly_name_for_id(default_id), True))
        return out

    @classmethod
    def get_mute(cls, device_id: str | None = None) -> bool:
        return bool(cls._endpoint_volume(device_id).GetMute())

    @classmethod
    def set_mute(cls, muted: bool, device_id: str | None = None) -> bool:
        endpoint = cls._endpoint_volume(device_id)
        endpoint.SetMute(1 if muted else 0, None)
        return bool(endpoint.GetMute())

    @classmethod
    def get_volume(cls, device_id: str | None = None) -> int:
        scalar = float(cls._endpoint_volume(device_id).GetMasterVolumeLevelScalar())
        return max(0, min(100, int(round(scalar * 100.0))))

    @classmethod
    def set_volume(cls, value: int | float, device_id: str | None = None) -> int:
        level = max(0, min(100, int(round(float(value)))))
        endpoint = cls._endpoint_volume(device_id)
        endpoint.SetMasterVolumeLevelScalar(level / 100.0, None)
        # HIST-055: read back from the SAME endpoint; re-resolving «default» here
        # could read another device the user switched to meanwhile.
        return max(0, min(100, int(round(float(endpoint.GetMasterVolumeLevelScalar()) * 100.0))))

    @classmethod
    def change_volume(cls, delta: int | float, device_id: str | None = None) -> dict:
        """Relative change on ONE resolved device: read, write and read back the same endpoint."""
        dev = cls._raw_device(device_id)
        resolved_id = cls._device_id(dev) if not device_id else str(device_id)
        endpoint = cls._endpoint_volume(resolved_id or None) if resolved_id else cls._endpoint_volume(device_id)
        current = int(round(float(endpoint.GetMasterVolumeLevelScalar()) * 100.0))
        requested = max(0, min(100, int(round(current + float(delta)))))
        endpoint.SetMasterVolumeLevelScalar(requested / 100.0, None)
        actual = max(0, min(100, int(round(float(endpoint.GetMasterVolumeLevelScalar()) * 100.0))))
        return {"before": current, "value": actual, "device_id": resolved_id}

    @classmethod
    def set_default(cls, device_id: str) -> NativeInputDevice:
        if not device_id:
            raise ValueError("device_id is required")
        _, _, _, AudioUtilities, _, _, ERole, _ = cls._imports()
        setter = getattr(AudioUtilities, "SetDefaultDevice", None)
        if not callable(setter):
            raise NotImplementedError("Installed pycaw does not provide SetDefaultDevice")
        roles = [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications]
        setter(str(device_id), roles=roles)
        actual = cls.default_device()
        if actual.device_id != str(device_id):
            raise RuntimeError("Windows Core Audio did not confirm requested default microphone")
        return actual


class WindowsCoreAudioOutputProvider(WindowsCoreAudioInputProvider):
    """Native render-endpoint enumeration/default switching."""

    name = "windows_core_audio"

    @classmethod
    def _default_raw_device(cls):
        _, _, _, _, _, EDataFlow, ERole, _ = cls._imports()
        return cls._enumerator().GetDefaultAudioEndpoint(
            EDataFlow.eRender.value, ERole.eMultimedia.value
        )

    @classmethod
    def _wrapper_devices(cls) -> list[Any]:
        _, _, _, _, _, EDataFlow, _, _ = cls._imports()
        return cls._wrapper_devices_for_flow(EDataFlow.eRender.value)

    @classmethod
    def default_device(cls) -> NativeInputDevice:
        dev = cls._default_raw_device()
        device_id = cls._device_id(dev)
        return NativeInputDevice(device_id, cls._friendly_name_for_id(device_id), True)

    @classmethod
    def set_default(cls, device_id: str) -> NativeInputDevice:
        if not device_id:
            raise ValueError("device_id is required")
        _, _, _, AudioUtilities, _, _, ERole, _ = cls._imports()
        setter = getattr(AudioUtilities, "SetDefaultDevice", None)
        if not callable(setter):
            raise NotImplementedError("Installed pycaw does not provide SetDefaultDevice")
        roles = [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications]
        setter(str(device_id), roles=roles)
        actual = cls.default_device()
        if actual.device_id != str(device_id):
            raise RuntimeError("Windows Core Audio did not confirm requested default output")
        return actual


__all__ = ["WindowsCoreAudioInputProvider", "WindowsCoreAudioOutputProvider", "NativeInputDevice"]
