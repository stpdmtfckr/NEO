"""Windows CPU budget for an owned background child; never the UI or wake ASR.

CpuRate is a fraction of the whole machine, in hundredths of one percent.
Convert a logical-core budget explicitly instead of allowing 25% of every core.
The unnamed job has no kill-on-close or other process/affinity limits.
"""
from __future__ import annotations

import ctypes
import logging
import os

logger = logging.getLogger(__name__)


def cpu_rate(core_budget: float, logical_cpus: int) -> int:
    return max(1, min(10000, int(10000 * max(0.01, min(1.0, float(core_budget))) /
                                  max(1, int(logical_cpus)))))


class BackgroundCpuJob:
    def __init__(self, kernel, handle):
        self._kernel, self._handle = kernel, handle

    def close(self):
        handle, self._handle = self._handle, None
        if handle:
            self._kernel.CloseHandle(handle)


def limit_owned_child(process, core_budget: float = .25) -> BackgroundCpuJob | None:
    """Apply a hard rate budget using Popen's exact child handle, not a PID scan.

    Unsupported/nested-job restrictions leave the existing below-normal worker
    usable and produce a diagnostic. The caller closes the handle at cleanup.
    """
    if os.name != "nt" or core_budget <= 0:
        return None
    handle = getattr(process, "_handle", None)
    if not handle:
        return None
    job = None
    try:
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # DWORD is always 32 bit. Use fixed-width fields for testable ABI size.
        class RateInfo(ctypes.Structure):
            _fields_ = [("ControlFlags", ctypes.c_uint32), ("CpuRate", ctypes.c_uint32)]
        raw_job = kernel.CreateJobObjectW(None, None)
        if not raw_job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
        job = BackgroundCpuJob(kernel, raw_job)
        rate = cpu_rate(core_budget, os.cpu_count() or 1)
        info = RateInfo(0x1 | 0x4, rate)  # ENABLE | HARD_CAP, no MIN/MAX/WEIGHT.
        if not kernel.SetInformationJobObject(raw_job, 15, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject CPU rate")
        if not kernel.AssignProcessToJobObject(raw_job, int(handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        logger.info("Ambient decoder CPU budget applied: %.2f logical cores (%s/10000 machine CPU)", core_budget, rate)
        return job
    except (OSError, AttributeError, TypeError, ValueError):
        if job is not None:
            job.close()
        logger.warning("Ambient decoder CPU budget unavailable; retaining below-normal priority", exc_info=True)
        return None
