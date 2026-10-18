# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Disposable Windows CoreAudio helper for NEO JARVIS.

If a buggy COM/audio driver crashes this process, the desktop assistant stays
alive and simply falls back to shared Discord spectrum metadata.
"""

import os
import sys
import time
from typing import Any


def _is_discord(session: Any) -> bool:
    name = ""
    try:
        proc = getattr(session, "Process", None)
        name = str(proc.name() if proc is not None else "").casefold()
    except Exception:
        name = ""
    # pycaw can expose Process=None for Electron renderer sessions even though
    # ProcessId is valid. Resolve the PID as a second path.
    if not name:
        try:
            pid = int(getattr(session, "ProcessId", 0) or 0)
            if pid:
                import psutil
                name = str(psutil.Process(pid).name() or "").casefold()
        except Exception:
            pass
    label = " ".join((
        name,
        str(getattr(session, "DisplayName", "") or "").casefold(),
        str(getattr(session, "Identifier", "") or "").casefold(),
    ))
    return "discord" in label or "squirrel\\discord" in label


def _meter_for_session(session: Any) -> Any | None:
    try:
        from pycaw.pycaw import IAudioMeterInformation
        ctl = getattr(session, "_ctl", None)
        if ctl is None:
            return None
        return ctl.QueryInterface(IAudioMeterInformation)
    except Exception:
        return None




def _default_endpoint_meter() -> Any | None:
    try:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        speakers = AudioUtilities.GetSpeakers()
        ctl = speakers.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
        try:
            return ctl.QueryInterface(IAudioMeterInformation)
        except Exception:
            return ctl
    except Exception:
        return None


def main() -> int:
    if os.name != "nt":
        return 0
    try:
        from comtypes import CoInitialize, CoUninitialize
        from pycaw.pycaw import AudioUtilities
        CoInitialize()
    except Exception:
        return 2
    meters: list[Any] = []
    endpoint_meter: Any | None = None
    empty_scans = 0
    next_scan = 0.0
    try:
        while True:
            now = time.monotonic()
            if now >= next_scan:
                next_scan = now + 4.0
                fresh: list[Any] = []
                try:
                    for session in AudioUtilities.GetAllSessions():
                        if _is_discord(session):
                            meter = _meter_for_session(session)
                            if meter is not None:
                                fresh.append(meter)
                except Exception:
                    fresh = []
                meters = fresh
                if meters:
                    empty_scans = 0
                else:
                    empty_scans += 1
                    # Parent starts this helper only while Discord playback is
                    # positively observed. If Windows hides the Electron session
                    # meter, fall back to the default render endpoint rather than
                    # leaving the robot waveform permanently flat.
                    if empty_scans >= 1 and endpoint_meter is None:
                        endpoint_meter = _default_endpoint_meter()
            peak = 0.0
            alive: list[Any] = []
            for meter in meters:
                try:
                    value = float(meter.GetPeakValue())
                    peak = max(peak, value)
                    alive.append(meter)
                except Exception:
                    continue
            meters = alive
            if not meters and endpoint_meter is not None:
                try:
                    peak = max(peak, float(endpoint_meter.GetPeakValue()))
                except Exception:
                    endpoint_meter = None
            try:
                sys.stdout.write(f"{max(0.0, min(1.0, peak)):.6f}\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                return 0
            time.sleep(0.080)
    finally:
        try:
            CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
