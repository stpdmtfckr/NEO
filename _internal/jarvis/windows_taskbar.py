# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
APP_USER_MODEL_ID = "NeoJarvis.NEO_JARVIS"


def apply_native_window_icon(window: object, icon_path: Path) -> bool:
    """Force WM_SETICON for frameless Qt windows on Windows.

    QApplication/QWidget icons are normally enough, but on some Windows 11
    shell/Qt combinations a frameless HWND keeps the generic host icon in the
    taskbar. Setting both small and big native icons makes the running window and
    a newly pinned item use the packaged robot icon consistently.
    """
    if os.name != "nt":
        return False
    icon = Path(icon_path).resolve()
    if not icon.exists():
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        load_image = user32.LoadImageW
        load_image.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        load_image.restype = wintypes.HANDLE
        send_message = user32.SendMessageW
        send_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        send_message.restype = wintypes.LPARAM
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        SM_CXICON, SM_CYICON = 11, 12
        SM_CXSMICON, SM_CYSMICON = 49, 50
        get_metric = user32.GetSystemMetrics
        get_metric.argtypes = [ctypes.c_int]
        get_metric.restype = ctypes.c_int
        handles = []
        hwnd = wintypes.HWND(int(window.winId()))
        sizes = (
            (ICON_SMALL, max(16, get_metric(SM_CXSMICON)), max(16, get_metric(SM_CYSMICON))),
            (ICON_BIG, max(32, get_metric(SM_CXICON)), max(32, get_metric(SM_CYICON))),
        )
        for icon_type, width, height in sizes:
            hicon = load_image(None, str(icon), IMAGE_ICON, width, height, LR_LOADFROMFILE)
            if hicon:
                send_message(hwnd, WM_SETICON, wintypes.WPARAM(icon_type), wintypes.LPARAM(int(hicon)))
                handles.append(int(hicon))
        # Qt's frameless/translucent HWND can sometimes leave the registered
        # window-class icon unset even after WM_SETICON. Set the class fallbacks
        # too; Explorer may consult them while creating/refreshing a taskbar button.
        try:
            set_class_icon = user32.SetClassLongPtrW
            set_class_icon.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            set_class_icon.restype = ctypes.c_void_p
            if handles:
                big = handles[-1]
                small = handles[0]
                set_class_icon(hwnd, -14, ctypes.c_void_p(big))      # GCLP_HICON
                set_class_icon(hwnd, -34, ctypes.c_void_p(small))    # GCLP_HICONSM
        except Exception:
            pass
        # Do not destroy handles while the HWND is alive. Keep them on the Qt
        # object so Python lifetime is explicit; Windows releases them at exit.
        setattr(window, "_neo_taskbar_icon_handles", handles)
        return bool(handles)
    except Exception as exc:
        logger.warning("Could not apply native Windows window icon: %s", exc)
        return False


def _icon_location180(launcher: Path, icon: Path) -> tuple[str, int]:
    """NEO 180: the shortcut/taskbar icon comes from NEO.exe's own icon resource.

    Windows caches icons per file path; builds before 180 shipped an .ico whose small
    frames were PNG, which some shell paths showed as the blank-file glyph and which
    stayed cached for that path. The exe (rebuilt with classic bitmap frames) is a
    different path, so the taskbar picks up the real robot icon."""
    if launcher.suffix.casefold() == ".exe" and launcher.exists():
        return str(launcher), 0
    return str(icon if icon.exists() else launcher), 0


def ensure_start_menu_shortcut(launcher_path: Path, icon_path: Path | None = None) -> Path | None:
    """Create/update a Start Menu shortcut matching the taskbar AppUserModelID.

    The target is always the dedicated GUI launcher, never python/pythonw.  The
    shortcut points directly at the packaged multi-size robot .ico so Windows
    does not depend on the launcher's embedded resource cache. A hidden WScript.Shell fallback still creates a correctly
    targeted/iconed shortcut if pywin32 is temporarily unavailable.
    """
    if os.name != "nt":
        return None
    launcher = Path(launcher_path).resolve()
    icon = Path(icon_path).resolve() if icon_path is not None else launcher
    if not launcher.exists():
        return None
    programs = Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programs.mkdir(parents=True, exist_ok=True)
    shortcut = programs / "NEO.lnk"
    old_shortcut = programs / "NEO JARVIS.lnk"
    try:
        if old_shortcut.exists() and old_shortcut != shortcut:
            old_shortcut.unlink()
    except OSError:
        pass
    created = False
    try:
        import pythoncom
        from win32com.shell import shell
        from win32com.propsys import propsys, pscon

        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.SetPath(str(launcher))
        link.SetWorkingDirectory(str(launcher.parent))
        link.SetDescription("NEO desktop assistant")
        link.SetIconLocation(*_icon_location180(launcher, icon))
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        variant = lambda value: propsys.PROPVARIANTType(value, pythoncom.VT_ILLEGAL)
        store.SetValue(pscon.PKEY_AppUserModel_ID, variant(APP_USER_MODEL_ID))
        store.Commit()
        persist = link.QueryInterface(pythoncom.IID_IPersistFile)
        persist.Save(str(shortcut), 0)
        created = shortcut.exists()
    except Exception as exc:
        logger.warning("Could not create AppUserModelID shortcut through pywin32: %s", exc)
        # This fallback is intentionally hidden and does not take foreground focus.
        # It still gives Windows a real NEO_JARVIS.exe target and packaged robot icon.
        try:
            import subprocess
            def psq(value: str) -> str:
                return "'" + value.replace("'", "''") + "'"
            script = (
                "$w=New-Object -ComObject WScript.Shell;"
                f"$s=$w.CreateShortcut({psq(str(shortcut))});"
                f"$s.TargetPath={psq(str(launcher))};"
                f"$s.WorkingDirectory={psq(str(launcher.parent))};"
                f"$s.IconLocation={psq('%s,%d' % _icon_location180(launcher, icon))};"
                "$s.Description='NEO desktop assistant';$s.Save()"
            )
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            startupinfo = None
            if hasattr(subprocess, "STARTUPINFO"):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
                startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                check=False, timeout=8, creationflags=flags, startupinfo=startupinfo,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            created = shortcut.exists()
        except Exception as fallback_exc:
            logger.warning("Could not create fallback Start Menu shortcut: %s", fallback_exc)
    if created:
        try:
            import ctypes
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            # SHCNE_ASSOCCHANGED forces Explorer to refresh shortcut/icon associations.
            shell32.SHChangeNotify(0x08000000, 0, None, None)
        except Exception:
            pass
        logger.info("Windows Start Menu shortcut prepared for taskbar pinning: %s", shortcut)
        return shortcut
    return None


def configure_taskbar_relaunch(window: object, launcher_path: Path, icon_path: Path | None = None) -> bool:
    """Attach stable Windows taskbar identity/relaunch metadata to the Qt HWND.

    NEO is hosted by pythonw.exe, so without explicit relaunch metadata
    Windows can pin the Python host instead of the application.  The window
    property store is told to relaunch the dedicated NEO_JARVIS.exe and use the packaged
    multi-resolution robot icon.
    """
    if os.name != "nt":
        return False
    launcher = Path(launcher_path).resolve()
    icon = Path(icon_path).resolve() if icon_path is not None else launcher.parent / "assets" / "neo_jarvis.ico"
    if not launcher.exists():
        logger.warning("Taskbar launcher is missing: %s", launcher)
        return False
    try:
        apply_native_window_icon(window, icon)
        import pythoncom
        from win32com.propsys import propsys, pscon

        hwnd = int(window.winId())
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        variant = lambda value: propsys.PROPVARIANTType(value, pythoncom.VT_ILLEGAL)
        # Microsoft recommends setting relaunch properties before the explicit
        # AppUserModelID because setting the ID asks the taskbar to refresh.
        # RelaunchCommand uses CreateProcess semantics, which cannot execute a
        # CMD script directly. Resolve the shell explicitly for the source build.
        command = f'"{launcher}"'
        if launcher.suffix.casefold() in {".cmd", ".bat"}:
            shell = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            command = f'"{shell}" /d /s /c ""{launcher}""'
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchCommand, variant(command))
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchDisplayNameResource, variant("NEO"))
        # Point the taskbar at the real multi-resolution robot .ico. The source
        # launcher used by SLIM builds is not guaranteed to carry an icon resource,
        # which is why older builds could show Windows' blank-file glyph after pinning.
        icon_resource = "%s,%d" % _icon_location180(launcher, icon)
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchIconResource, variant(icon_resource))
        try:
            store.SetValue(pscon.PKEY_AppUserModel_PreventPinning, variant(False))
        except Exception:
            pass
        store.SetValue(pscon.PKEY_AppUserModel_ID, variant(APP_USER_MODEL_ID))
        store.Commit()
        logger.info("Windows taskbar relaunch target configured: %s icon=%s", launcher, icon if icon.exists() else launcher)
        return True
    except Exception as exc:
        logger.warning("Could not configure Windows taskbar relaunch metadata: %s", exc)
        return False
