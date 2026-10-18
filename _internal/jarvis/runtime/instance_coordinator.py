# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from jarvis.runtime.hidden_subprocess import hidden_process_kwargs


class InstanceCoordinator:
    """Small per-user single-instance coordinator.

    The first JARVIS process owns a one-byte advisory lock under LOCALAPPDATA.
    Later launches do not start another Qt/voice stack: they write a tiny command
    file that the primary process polls and then exit. This is deliberately file-
    based so the launcher does not depend on a TCP port, firewall rule or QtNetwork.
    """

    def __init__(self) -> None:
        base = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "NeoJarvis" / "instance"
        base.mkdir(parents=True, exist_ok=True)
        self.root = base
        self.lock_path = base / "neo_jarvis.lock"
        self.command_path = base / "command.json"
        self.ack_path = base / "ack.json"
        self.state_path = base / "primary.json"
        self._handle = None
        self._owned = False
        self._guard = threading.RLock()
        # V5.8: the byte-file lock remains a cross-platform compatibility guard,
        # but Windows now also owns a kernel named mutex. Unlike a path/PID file,
        # the mutex is acquired atomically by the OS and is released automatically
        # if Python crashes or Windows terminates the process. This closes the
        # logon race where Startup.vbs and the priority scheduled task could both
        # reach Qt/Discord before either noticed the other. Keep the name stable
        # across releases so a new archive cannot start beside an older NEO.
        self._mutex_name = r"Local\NeoJarvis.NEO_JARVIS.Instance"
        self._mutex_handle = None


    def _acquire_windows_mutex(self) -> bool:
        if os.name != "nt":
            return True
        if self._mutex_handle:
            return True
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(None, False, self._mutex_name)
            if not handle:
                return False
            error = ctypes.get_last_error()
            if error == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self._mutex_handle = handle
            return True
        except Exception:
            # The file lock below remains the conservative fallback on unusual
            # Windows runtimes where kernel32 binding failed.
            return True

    def _release_windows_mutex(self) -> None:
        handle = self._mutex_handle
        self._mutex_handle = None
        if not handle or os.name != "nt":
            return
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        except Exception:
            pass

    def _windows_mutex_exists(self) -> bool:
        if os.name != "nt":
            return False
        if self._mutex_handle:
            return True
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
            kernel32.OpenMutexW.restype = wintypes.HANDLE
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenMutexW(SYNCHRONIZE, False, self._mutex_name)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False

    @staticmethod
    def _lock(handle, *, blocking: bool) -> bool:
        if os.name == "nt":
            import msvcrt

            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), mode, 1)
                return True
            except OSError:
                return False
        try:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), flags)
            return True
        except (OSError, BlockingIOError):
            return False

    @staticmethod
    def _unlock(handle) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

    def _open_lock(self):
        handle = open(self.lock_path, "a+b", buffering=0)
        try:
            if self.lock_path.stat().st_size < 1:
                handle.write(b"\0")
                handle.flush()
        except Exception:
            pass
        handle.seek(0)
        return handle

    def acquire_primary(self) -> bool:
        with self._guard:
            if self._owned:
                return True
            # Windows kernel arbitration happens before any file/PID metadata.
            # Exactly one process can cross this point during simultaneous logon.
            if not self._acquire_windows_mutex():
                return False
            handle = self._open_lock()
            if not self._lock(handle, blocking=False):
                handle.close()
                self._release_windows_mutex()
                return False
            self._handle = handle
            self._owned = True
            try:
                self.state_path.write_text(json.dumps({
                    "pid": os.getpid(), "started_at": time.time(), "shutting_down": False
                }), encoding="utf-8")
            except Exception:
                pass
            # A command left by a process that raced with startup is still useful,
            # so do not delete it here. drain_commands() will consume it once UI is ready.
            return True


    def read_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def primary_pid(self) -> int:
        try:
            return int(self.read_state().get("pid", 0) or 0)
        except Exception:
            return 0

    @staticmethod
    def pid_alive(pid: int) -> bool:
        pid = int(pid or 0)
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not handle:
                    return False
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def clear_stale_metadata(self) -> None:
        """Remove metadata only when no process owns the OS-level lock.

        The lock file itself may remain on disk forever; the advisory byte lock is
        what defines ownership. This makes a crashed/terminated process recoverable
        without trusting a stale PID file.
        """
        if self.has_primary():
            return
        for path in (self.state_path, self.command_path, self.ack_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    def force_terminate_primary(self, *, timeout: float = 8.0) -> bool:
        """Force-stop only the PID recorded by the current lock owner.

        This is intentionally a launcher recovery path, not a general process-kill
        API. It refuses to act unless the instance lock is currently owned and a
        live PID is present in primary.json.
        """
        if not self.has_primary():
            self.clear_stale_metadata()
            return True
        pid = self.primary_pid()
        if pid <= 0 or not self.pid_alive(pid):
            return False
        try:
            if os.name == "nt":
                import subprocess
                completed = subprocess.run(
                    ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, check=False, timeout=10,
                    **hidden_process_kwargs(),
                )
                if completed.returncode not in (0, 128):
                    # Priority startup may have launched JARVIS elevated. After
                    # the user explicitly approved force-stop in the launcher
                    # dialog, allow one hidden UAC-assisted taskkill fallback.
                    ps = (
                        "$p=Start-Process -FilePath 'taskkill.exe' "
                        f"-ArgumentList @('/PID','{pid}','/T','/F') "
                        "-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"
                    )
                    elevated = subprocess.run(
                        ["powershell.exe", "-NoLogo", "-NoProfile", "-WindowStyle", "Hidden",
                         "-ExecutionPolicy", "Bypass", "-Command", ps],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL, check=False, timeout=45,
                        **hidden_process_kwargs(),
                    )
                    if elevated.returncode not in (0, 128):
                        return False
            else:
                import signal
                os.kill(pid, signal.SIGKILL)
        except Exception:
            return False
        released = self.wait_for_release(timeout=max(1.0, float(timeout)))
        if released:
            self.clear_stale_metadata()
        return released

    def mark_shutting_down(self) -> None:
        if not self._owned:
            return
        try:
            self.state_path.write_text(json.dumps({
                "pid": os.getpid(), "started_at": time.time(), "shutting_down": True
            }), encoding="utf-8")
        except Exception:
            pass

    def close(self) -> None:
        with self._guard:
            handle = self._handle
            self._handle = None
            self._owned = False
            if handle is not None:
                self._unlock(handle)
                try:
                    handle.close()
                except Exception:
                    pass
            self._release_windows_mutex()
            for path in (self.state_path, self.command_path, self.ack_path):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    def wait_for_release(self, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + max(0.2, float(timeout))
        while time.monotonic() < deadline:
            if not self.has_primary():
                return True
            time.sleep(0.10)
        return not self.has_primary()

    def has_primary(self) -> bool:
        # Named mutex detects a V5.8+ primary without touching disk and cannot be
        # left stale after a crash. Fall through to the byte lock so V5.8 still
        # detects older NEO releases that do not create the mutex.
        if self._windows_mutex_exists():
            return True
        handle = self._open_lock()
        try:
            if self._lock(handle, blocking=False):
                self._unlock(handle)
                return False
            return True
        finally:
            handle.close()

    def request(
        self,
        action: str = "show_main_window",
        *,
        wait_ack: bool = False,
        timeout: float = 2.5,
    ) -> bool:
        """Send a command to the live primary process.

        ``wait_ack`` is used by START_JARVIS.bat before it decides that a tray
        instance handled the launch. Merely owning the lock is not enough: a
        wedged process could otherwise make every later EXE/BAT invocation exit
        silently forever. The primary writes a tiny acknowledgement only after
        its Qt event loop actually consumed the request.
        """
        if not self.has_primary():
            return False
        request_id = f"{os.getpid()}-{time.time_ns()}"
        payload = {
            "action": str(action or "show_main_window"),
            "pid": os.getpid(),
            "created_at": time.time(),
            "request_id": request_id,
        }
        tmp = self.command_path.with_suffix(f".tmp.{os.getpid()}")
        try:
            # Never accept an acknowledgement left by an older request.
            try:
                self.ack_path.unlink(missing_ok=True)
            except Exception:
                pass
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.command_path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False
        if not wait_ack:
            return True
        deadline = time.monotonic() + max(0.2, float(timeout))
        while time.monotonic() < deadline:
            try:
                ack = json.loads(self.ack_path.read_text(encoding="utf-8"))
            except Exception:
                ack = None
            if isinstance(ack, dict) and str(ack.get("request_id", "")) == request_id:
                try:
                    self.ack_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return True
            time.sleep(0.05)
        return False

    def acknowledge(self, command: dict[str, Any]) -> None:
        """Acknowledge a command after the primary UI actually handled it."""
        request_id = str(command.get("request_id", "") or "")
        if not request_id:
            return
        payload = {"request_id": request_id, "pid": os.getpid(), "handled_at": time.time()}
        tmp = self.ack_path.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.ack_path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def drain_commands(self, *, max_age_seconds: float = 15.0) -> list[dict[str, Any]]:
        if not self._owned or not self.command_path.exists():
            return []
        try:
            raw = json.loads(self.command_path.read_text(encoding="utf-8"))
        except Exception:
            raw = None
        try:
            self.command_path.unlink(missing_ok=True)
        except Exception:
            pass
        if not isinstance(raw, dict):
            return []
        created = float(raw.get("created_at", 0.0) or 0.0)
        if created > 0 and time.time() - created > max(1.0, float(max_age_seconds)):
            return []
        return [raw]
