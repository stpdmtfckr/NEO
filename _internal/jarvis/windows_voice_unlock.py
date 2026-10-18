# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Windows voice-unlock support for NEO JARVIS.

HOTFIX47 uses a native x64 Windows Credential Provider for real Win+L /
Secure Desktop unlock.  The normal desktop process only writes a very short
lived, machine-DPAPI protected one-time request after the configured exact
code phrase is heard.  LogonUI consumes that request through the bundled
credential provider and submits it to Winlogon/LSA.

The older WTSConnectSession/tscon route is retained only as a compatibility
fallback for Remote Desktop/session-reconnect configurations.  It is not the
primary local workstation unlock mechanism.
"""

from dataclasses import dataclass
import csv
import ctypes
from ctypes import wintypes
import getpass
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
from typing import Any, Iterable

from jarvis.secrets import unprotect_secret


_LOGON32_LOGON_INTERACTIVE = 2
_LOGON32_PROVIDER_DEFAULT = 0
_LOGONID_CURRENT = 0xFFFFFFFF
_WTS_CURRENT_SESSION = 0xFFFFFFFF
_WTS_SESSIONSTATE_LOCK = 0
_WTS_SESSIONSTATE_UNLOCK = 1
_WTS_INFO_CLASS_SESSION_INFO_EX = 25
_WINSTATIONNAME_LENGTH = 32
_USERNAME_LENGTH = 20
_DOMAIN_LENGTH = 17

_NATIVE_PROVIDER_CLSID = "{7A0B07ED-99E8-4B79-A61E-E5B4EA1FB347}"
_NATIVE_PROVIDER_NAME = "NEO Voice Unlock"
_REQUEST_MAGIC = b"NJUNLK47"
_REQUEST_VERSION = 47
_REQUEST_TTL_SECONDS = 8
_CRYPTPROTECT_LOCAL_MACHINE = 0x00000004
_REQUEST_HEADER = struct.Struct("<8sIQIII")
_REQUEST_ACL_READY = False


class _WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", ctypes.c_int),
        ("WinStationName", wintypes.WCHAR * (_WINSTATIONNAME_LENGTH + 1)),
        ("UserName", wintypes.WCHAR * (_USERNAME_LENGTH + 1)),
        ("DomainName", wintypes.WCHAR * (_DOMAIN_LENGTH + 1)),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class _WTSINFOEX_UNION(ctypes.Union):
    _fields_ = [("WTSInfoExLevel1", _WTSINFOEX_LEVEL1_W)]


class _WTSINFOEXW(ctypes.Structure):
    _fields_ = [("Level", wintypes.DWORD), ("Data", _WTSINFOEX_UNION)]


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


@dataclass(frozen=True)
class VoiceUnlockResult:
    ok: bool
    status: str
    method: str = ""
    error_code: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "status": str(self.status),
            "method": str(self.method),
            "error_code": int(self.error_code),
        }


def normalize_unlock_phrase(text: str) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def phrase_matches(text: str, phrase: str, aliases: Iterable[str] = ()) -> bool:
    heard = normalize_unlock_phrase(text)
    if not heard:
        return False
    accepted = {normalize_unlock_phrase(phrase)}
    accepted.update(normalize_unlock_phrase(item) for item in aliases)
    accepted.discard("")
    # Deliberately exact after normalization: security-sensitive phrases never
    # use fuzzy recovery.  ASR variants are explicitly configured by the user.
    return heard in accepted


def current_windows_account() -> str:
    if os.name != "nt":
        return str(os.getenv("USER") or getpass.getuser() or "")
    domain = str(os.getenv("USERDOMAIN", "") or "").strip()
    user = str(os.getenv("USERNAME", "") or getpass.getuser() or "").strip()
    return f"{domain}\\{user}" if domain and user else user


def _split_account(account: str) -> tuple[str, str]:
    value = str(account or "").strip()
    if "\\" in value:
        domain, user = value.split("\\", 1)
        return domain.strip(), user.strip()
    if value.lower().startswith("microsoftaccount/"):
        return "MicrosoftAccount", value.split("/", 1)[1].strip()
    if "@" in value:
        return "MicrosoftAccount", value
    domain = str(os.getenv("USERDOMAIN", "") or ".").strip() or "."
    user = value or str(os.getenv("USERNAME", "") or getpass.getuser() or "")
    return domain, user


def validate_windows_password(account: str, password: str) -> VoiceUnlockResult:
    if os.name != "nt":
        return VoiceUnlockResult(False, "Проверка пароля доступна только в Windows.", "unsupported")
    secret = str(password or "")
    if not secret:
        return VoiceUnlockResult(False, "Пароль Windows не задан.", "logon_user")
    domain, user = _split_account(account)
    if not user:
        return VoiceUnlockResult(False, "Не удалось определить имя учётной записи Windows.", "logon_user")
    token = wintypes.HANDLE()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = advapi32.LogonUserW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    fn.restype = wintypes.BOOL
    ok = bool(fn(user, domain, secret, _LOGON32_LOGON_INTERACTIVE, _LOGON32_PROVIDER_DEFAULT, ctypes.byref(token)))
    if ok:
        try:
            return VoiceUnlockResult(True, "Пароль Windows подтверждён.", "logon_user")
        finally:
            if token:
                kernel32.CloseHandle(token)
    err = int(ctypes.get_last_error() or 0)
    return VoiceUnlockResult(False, f"Windows отклонила пароль (код {err}).", "logon_user", err)


def _current_session_id() -> int:
    if os.name != "nt":
        return -1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    session = wintypes.DWORD(0)
    ok = kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session))
    return int(session.value) if ok else -1


def is_current_session_locked() -> bool:
    if os.name != "nt":
        return False
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    pp_buffer = ctypes.c_void_p()
    bytes_returned = wintypes.DWORD(0)
    query = wts.WTSQuerySessionInformationW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    free_memory = wts.WTSFreeMemory
    free_memory.argtypes = [ctypes.c_void_p]
    free_memory.restype = None
    if query(None, _WTS_CURRENT_SESSION, _WTS_INFO_CLASS_SESSION_INFO_EX, ctypes.byref(pp_buffer), ctypes.byref(bytes_returned)):
        try:
            if pp_buffer.value and bytes_returned.value >= ctypes.sizeof(_WTSINFOEXW):
                info = ctypes.cast(pp_buffer, ctypes.POINTER(_WTSINFOEXW)).contents
                if int(info.Level) == 1:
                    flags = int(info.Data.WTSInfoExLevel1.SessionFlags)
                    if flags == _WTS_SESSIONSTATE_LOCK:
                        return True
                    if flags == _WTS_SESSIONSTATE_UNLOCK:
                        return False
        finally:
            if pp_buffer.value:
                free_memory(pp_buffer)
    return False


def _hidden_subprocess_kwargs(*, capture: bool = True) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE if capture else subprocess.DEVNULL,
        "stderr": subprocess.PIPE if capture else subprocess.DEVNULL,
        "text": True,
    }
    if os.name == "nt":
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if flags:
            kwargs["creationflags"] = flags
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
            si.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
            kwargs["startupinfo"] = si
        except Exception:
            pass
    return kwargs


def _app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _bundled_provider_dll() -> Path:
    return _app_root() / "native" / "voice_unlock_provider" / "NeoJarvisVoiceUnlockProvider.dll"


def _program_data() -> Path:
    return Path(os.getenv("ProgramData", r"C:\ProgramData"))


def _installed_provider_dll() -> Path:
    return _program_data() / "NeoJarvis" / "CredentialProvider" / "NeoJarvisVoiceUnlockProvider.dll"


def _request_dir() -> Path:
    return _program_data() / "NeoJarvis" / "voice_unlock"


def _request_path() -> Path:
    return _request_dir() / "request.bin"


def _installer_script_path() -> Path:
    root = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "NeoJarvis" / "voice_unlock"
    root.mkdir(parents=True, exist_ok=True)
    return root / "install_credential_provider.ps1"


def _powershell_exe() -> str:
    root = Path(os.getenv("SystemRoot", r"C:\Windows"))
    # Sysnative bypasses WOW64 redirection when the caller itself is 32-bit.
    sysnative = root / "Sysnative" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    system32 = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if sysnative.is_file():
        return str(sysnative)
    if system32.is_file():
        return str(system32)
    return "powershell.exe"


def native_provider_registered() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        access = winreg.KEY_READ | int(getattr(winreg, "KEY_WOW64_64KEY", 0))
        provider_key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\{_NATIVE_PROVIDER_CLSID}"
        clsid_key = rf"SOFTWARE\Classes\CLSID\{_NATIVE_PROVIDER_CLSID}\InprocServer32"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, provider_key, 0, access):
            pass
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clsid_key, 0, access) as key:
            value, _ = winreg.QueryValueEx(key, None)
        registered_path = Path(os.path.expandvars(str(value or ""))).resolve(strict=False)
        return registered_path == _installed_provider_dll().resolve(strict=False) and _installed_provider_dll().is_file()
    except Exception:
        return False


def _write_installer_script(source: Path, destination: Path) -> Path:
    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    dest_dir = destination.parent
    clsid = _NATIVE_PROVIDER_CLSID
    script = _installer_script_path()
    content = f"""$ErrorActionPreference = 'Stop'
$source = {ps_quote(str(source))}
$destDir = {ps_quote(str(dest_dir))}
$dest = {ps_quote(str(destination))}
if (-not (Test-Path -LiteralPath $source)) {{ throw 'Bundled credential provider DLL is missing.' }}
New-Item -ItemType Directory -Force -Path $destDir | Out-Null
Copy-Item -LiteralPath $source -Destination $dest -Force
$icacls = Join-Path $env:SystemRoot 'System32\\icacls.exe'
& $icacls $destDir '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE -ne 0) {{ throw "icacls failed: $LASTEXITCODE" }}
$providerKey = 'Registry::HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Authentication\\Credential Providers\\{clsid}'
$clsidKey = 'Registry::HKEY_LOCAL_MACHINE\\SOFTWARE\\Classes\\CLSID\\{clsid}'
$inprocKey = $clsidKey + '\\InprocServer32'
New-Item -Force -Path $providerKey | Out-Null
Set-Item -Path $providerKey -Value '{_NATIVE_PROVIDER_NAME}'
New-Item -Force -Path $clsidKey | Out-Null
Set-Item -Path $clsidKey -Value '{_NATIVE_PROVIDER_NAME}'
New-Item -Force -Path $inprocKey | Out-Null
Set-Item -Path $inprocKey -Value $dest
New-ItemProperty -Force -Path $inprocKey -Name 'ThreadingModel' -PropertyType String -Value 'Apartment' | Out-Null
exit 0
"""
    script.write_text(content, encoding="utf-8-sig")
    return script


def ensure_native_provider_registered(*, elevate: bool = True) -> VoiceUnlockResult:
    if os.name != "nt":
        return VoiceUnlockResult(False, "Системный модуль разблокировки доступен только в Windows.", "credential_provider")
    if native_provider_registered():
        return VoiceUnlockResult(True, "Системный модуль голосовой разблокировки зарегистрирован.", "credential_provider")
    source = _bundled_provider_dll()
    if not source.is_file():
        return VoiceUnlockResult(False, f"Не найден системный модуль: {source}", "credential_provider")
    destination = _installed_provider_dll()
    try:
        script = _write_installer_script(source, destination)
    except Exception as exc:
        return VoiceUnlockResult(False, f"Не удалось подготовить установку Credential Provider: {exc}", "credential_provider")
    if not elevate:
        return VoiceUnlockResult(False, "Credential Provider ещё не зарегистрирован. Откройте настройки разблокировки и сохраните их один раз с подтверждением UAC.", "credential_provider")

    ps = _powershell_exe()
    args = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    ps_args = ",".join("'" + part.replace("'", "''") + "'" for part in args)
    quoted_ps = ps.replace("'", "''")
    outer = (
        f"$p=Start-Process -FilePath '{quoted_ps}' -ArgumentList @({ps_args}) "
        "-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"
    )
    try:
        completed = subprocess.run(
            [ps, "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", outer],
            check=False,
            timeout=90,
            **_hidden_subprocess_kwargs(capture=True),
        )
    except Exception as exc:
        return VoiceUnlockResult(False, f"Не удалось запустить регистрацию Credential Provider: {exc}", "credential_provider")
    if int(completed.returncode) != 0:
        detail = (str(completed.stderr or "") + " " + str(completed.stdout or "")).strip().replace("\n", " ")
        if len(detail) > 260:
            detail = detail[:260] + "…"
        return VoiceUnlockResult(False, f"Регистрация Credential Provider не завершилась (код {completed.returncode}). {detail}".strip(), "credential_provider", int(completed.returncode))
    if not native_provider_registered():
        return VoiceUnlockResult(False, "Windows завершила установщик, но регистрация Credential Provider не обнаружена.", "credential_provider")
    return VoiceUnlockResult(True, "Системный модуль голосовой разблокировки установлен и зарегистрирован.", "credential_provider")


def native_provider_status() -> dict[str, Any]:
    return {
        "registered": native_provider_registered(),
        "bundled_dll": str(_bundled_provider_dll()),
        "installed_dll": str(_installed_provider_dll()),
        "clsid": _NATIVE_PROVIDER_CLSID,
    }


def _current_user_sid() -> str:
    if os.name != "nt":
        return ""
    system_root = Path(os.getenv("SystemRoot", r"C:\Windows"))
    whoami = system_root / "System32" / "whoami.exe"
    exe = str(whoami if whoami.is_file() else Path("whoami.exe"))
    try:
        proc = subprocess.run([exe, "/user", "/fo", "csv", "/nh"], timeout=4, check=False, **_hidden_subprocess_kwargs(capture=True))
        if proc.returncode == 0:
            row = next(csv.reader([str(proc.stdout or "").strip()]), [])
            if len(row) >= 2 and row[1].strip().upper().startswith("S-1-"):
                return row[1].strip()
    except Exception:
        pass
    return ""


def _ensure_request_acl() -> VoiceUnlockResult:
    global _REQUEST_ACL_READY
    if _REQUEST_ACL_READY:
        return VoiceUnlockResult(True, "One-time request ACL готов.", "request_acl")
    try:
        directory = _request_dir()
        directory.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return VoiceUnlockResult(False, f"Не удалось создать защищённую папку запроса: {exc}", "request_acl")
    sid = _current_user_sid()
    if not sid:
        return VoiceUnlockResult(False, "Не удалось определить SID текущего пользователя для защищённого one-time запроса.", "request_acl")
    system_root = Path(os.getenv("SystemRoot", r"C:\Windows"))
    icacls = system_root / "System32" / "icacls.exe"
    exe = str(icacls if icacls.is_file() else Path("icacls.exe"))
    grants = [f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]
    try:
        proc = subprocess.run([exe, str(directory), "/inheritance:r", "/grant:r", *grants], timeout=6, check=False, **_hidden_subprocess_kwargs(capture=True))
    except Exception as exc:
        return VoiceUnlockResult(False, f"Не удалось настроить ACL запроса: {exc}", "request_acl")
    if int(proc.returncode) != 0:
        detail = (str(proc.stderr or "") + " " + str(proc.stdout or "")).strip().replace("\n", " ")
        return VoiceUnlockResult(False, f"Windows не применила ACL one-time запроса (код {proc.returncode}): {detail[:220]}", "request_acl", int(proc.returncode))
    _REQUEST_ACL_READY = True
    return VoiceUnlockResult(True, "One-time request ACL готов.", "request_acl")


def _dpapi_protect_local_machine(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI доступен только в Windows")
    payload = bytes(data)
    if not payload:
        raise ValueError("Пустые данные нельзя защищать DPAPI")
    in_array = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    in_blob = _DATA_BLOB(len(payload), ctypes.cast(in_array, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = _DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not fn(ctypes.byref(in_blob), "NEO voice unlock", None, None, None, _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out_blob)):
        err = int(ctypes.get_last_error() or 0)
        raise OSError(err, f"CryptProtectData failed: {err}")
    try:
        return ctypes.string_at(out_blob.pbData, int(out_blob.cbData))
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))


def _write_native_unlock_request(account: str, password: str, *, ttl_seconds: int = _REQUEST_TTL_SECONDS) -> VoiceUnlockResult:
    acl = _ensure_request_acl()
    if not acl.ok:
        return acl
    domain, user = _split_account(account)
    if not domain or not user:
        return VoiceUnlockResult(False, "Не удалось разложить учётную запись на domain/user.", "credential_provider_request")
    domain_raw = domain.encode("utf-16le")
    user_raw = user.encode("utf-16le")
    password_raw = (str(password) + "\0").encode("utf-16le")
    if not password_raw:
        return VoiceUnlockResult(False, "Пароль Windows не задан.", "credential_provider_request")
    try:
        blob = _dpapi_protect_local_machine(password_raw)
    except Exception as exc:
        return VoiceUnlockResult(False, f"Не удалось защитить one-time пароль через DPAPI: {exc}", "credential_provider_request")
    expires = int(time.time()) + max(3, min(20, int(ttl_seconds)))
    header = _REQUEST_HEADER.pack(
        _REQUEST_MAGIC,
        _REQUEST_VERSION,
        expires,
        len(domain_raw) // 2,
        len(user_raw) // 2,
        len(blob),
    )
    payload = header + domain_raw + user_raw + blob
    if len(payload) > 64 * 1024:
        return VoiceUnlockResult(False, "One-time запрос разблокировки слишком большой.", "credential_provider_request")
    target = _request_path()
    temp = target.with_suffix(f".tmp.{os.getpid()}")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except Exception as exc:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
        return VoiceUnlockResult(False, f"Не удалось записать one-time запрос Credential Provider: {exc}", "credential_provider_request")
    return VoiceUnlockResult(True, "One-time запрос передан системному Credential Provider.", "credential_provider_request")


def _delete_unlock_request() -> None:
    try:
        _request_path().unlink(missing_ok=True)
    except Exception:
        pass


def _try_native_credential_provider(account: str, password: str) -> VoiceUnlockResult:
    if not native_provider_registered():
        return VoiceUnlockResult(False, "Системный Credential Provider не зарегистрирован.", "credential_provider")
    request = _write_native_unlock_request(account, password)
    if not request.ok:
        return request
    deadline = time.monotonic() + 3.6
    while time.monotonic() < deadline:
        time.sleep(0.055)
        if not is_current_session_locked():
            _delete_unlock_request()
            return VoiceUnlockResult(True, "Windows разблокирована через NEO Credential Provider.", "credential_provider")
    _delete_unlock_request()
    return VoiceUnlockResult(False, "Credential Provider получил запрос, но Windows осталась заблокирована. Проверьте пароль и журнал Windows/NEO.", "credential_provider")


def _try_wts_connect(password: str) -> VoiceUnlockResult:
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    current = _current_session_id()
    active = int(kernel32.WTSGetActiveConsoleSessionId()) if hasattr(kernel32, "WTSGetActiveConsoleSessionId") else current
    if current < 0:
        return VoiceUnlockResult(False, "Не удалось определить текущую Windows-сессию.", "wts_connect")
    connect = wts.WTSConnectSessionW
    connect.argtypes = [wintypes.ULONG, wintypes.ULONG, wintypes.LPWSTR, wintypes.BOOL]
    connect.restype = wintypes.BOOL
    attempts: list[tuple[int, int]] = []
    if active != 0xFFFFFFFF and active != current:
        attempts.append((current, active))
    attempts.append((current, _LOGONID_CURRENT))
    if active != 0xFFFFFFFF:
        attempts.append((_LOGONID_CURRENT, active))
    seen: set[tuple[int, int]] = set()
    last_error = 0
    for source, target in attempts:
        if (source, target) in seen:
            continue
        seen.add((source, target))
        ctypes.set_last_error(0)
        if bool(connect(source, target, str(password), True)):
            time.sleep(0.12)
            return VoiceUnlockResult(True, "Windows-сессия переподключена.", "wts_connect")
        last_error = int(ctypes.get_last_error() or 0)
    return VoiceUnlockResult(False, f"WTSConnectSessionW не выполнил переподключение (код {last_error}).", "wts_connect", last_error)


def _try_tscon_same_user() -> VoiceUnlockResult:
    if os.name != "nt":
        return VoiceUnlockResult(False, "tscon доступен только в Windows.", "tscon")
    session = _current_session_id()
    if session < 0:
        return VoiceUnlockResult(False, "Не удалось определить текущую Windows-сессию.", "tscon")
    system_root = str(os.getenv("SystemRoot", r"C:\Windows") or r"C:\Windows")
    exe = os.path.join(system_root, "System32", "tscon.exe")
    try:
        proc = subprocess.run([exe, str(session), "/dest:console", "/v"], timeout=4.0, **_hidden_subprocess_kwargs(capture=True))
    except Exception as exc:
        return VoiceUnlockResult(False, f"tscon не запустился: {exc}", "tscon")
    if int(proc.returncode) == 0:
        time.sleep(0.15)
        return VoiceUnlockResult(True, "Текущая пользовательская сессия подключена к консоли.", "tscon")
    detail = (str(proc.stderr or "") + " " + str(proc.stdout or "")).strip().replace("\n", " ")
    if len(detail) > 220:
        detail = detail[:220] + "…"
    return VoiceUnlockResult(False, f"tscon завершился с кодом {proc.returncode}: {detail}", "tscon", int(proc.returncode))


def attempt_voice_unlock(config: Any) -> dict[str, Any]:
    if os.name != "nt":
        return VoiceUnlockResult(False, "Голосовая разблокировка доступна только в Windows.", "unsupported").as_dict()
    if not bool(config.get("voice_unlock.enabled", False)):
        return VoiceUnlockResult(False, "Голосовая разблокировка выключена.", "disabled").as_dict()
    if not is_current_session_locked():
        return VoiceUnlockResult(False, "Windows-сессия уже разблокирована.", "not_locked").as_dict()

    account = str(config.get("voice_unlock.account", "") or "").strip() or current_windows_account()
    encrypted = str(config.get("voice_unlock.password_encrypted", "") or "")
    password = unprotect_secret(encrypted)
    if not password:
        return VoiceUnlockResult(False, "Не удалось получить сохранённый пароль Windows из DPAPI.", "credential_provider").as_dict()

    # HOTFIX47 avoids a second LogonUserW round-trip on the latency-sensitive
    # code-phrase path.  The password is already validated when it is saved;
    # Winlogon/LSA remains the authority that authenticates the one-time request.
    if bool(config.get("voice_unlock.revalidate_password_before_unlock", False)):
        checked = validate_windows_password(account, password)
        if not checked.ok:
            return checked.as_dict()

    if bool(config.get("voice_unlock.native_provider_preferred", True)):
        native = _try_native_credential_provider(account, password)
        if native.ok:
            return native.as_dict()
        if native_provider_registered():
            # Do not waste several more seconds on RDS reconnect APIs when the
            # real local unlock provider was already available but authentication
            # did not complete.
            return native.as_dict()

    # Legacy compatibility for RDP/session reconnect configurations or a provider
    # that has not yet been registered.  This is intentionally not the primary
    # Win+L path anymore.
    wts_result = _try_wts_connect(password)
    if wts_result.ok:
        return wts_result.as_dict()
    if bool(config.get("voice_unlock.tscon_fallback", True)):
        tscon_result = _try_tscon_same_user()
        if tscon_result.ok:
            return tscon_result.as_dict()
        return VoiceUnlockResult(
            False,
            f"Credential Provider не готов; legacy fallback также не сработал. WTS: {wts_result.status} tscon: {tscon_result.status}",
            "credential_provider+wts+tscon",
            tscon_result.error_code or wts_result.error_code,
        ).as_dict()
    return wts_result.as_dict()
