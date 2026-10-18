# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import faulthandler
import ctypes
import logging
import os
import signal
import shutil
import sys
import time
from pathlib import Path

# Speech and audio subprocesses inherit these values. Set them before importing the UI.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Capture/remove the per-run secret before UI workers can inherit it.
from jarvis.runtime.startup_handshake import capture_launch_secret, publish_progress
from jarvis.runtime.speed174 import enlarge_regex_cache

enlarge_regex_cache()          # NEO 174: keep NEO's command patterns compiled
capture_launch_secret()
publish_progress("imports_begin")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from jarvis.config import ConfigManager
from jarvis.integrations.yandex_music import YandexMusicAdapter
from jarvis.integrations.telegram_remote import TelegramRemoteController
from jarvis.logging_setup import configure_logging
from jarvis.ui.main_window import MainWindow
from jarvis.runtime.system_timeline import SystemContextTracker
from jarvis.adaptation.ambient import AmbientLearningController
from jarvis.gaming.memory import GamingContextTracker
from jarvis.runtime.crash_recovery import SessionGuard
from jarvis.runtime.instance_coordinator import InstanceCoordinator
from jarvis.windows_taskbar import apply_native_window_icon, configure_taskbar_relaunch, ensure_start_menu_shortcut
from jarvis.windows_startup import refresh_priority_launcher


def _configure_windows_shell_identity() -> None:
    """Give the Python-hosted Qt app its own Windows taskbar identity."""
    if os.name != "nt":
        return
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        fn = shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [ctypes.c_wchar_p]
        fn.restype = ctypes.c_long
        fn("NeoJarvis.NEO_JARVIS")
    except Exception:
        pass



# Release lineage compatibility marker retained for older diagnostics.

def main() -> int:
    root = Path(__file__).resolve().parent
    from jarvis.runtime.distribution164 import project_root
    package_root = project_root()
    from bootstrap import check_release_files
    if not check_release_files(package_root):
        return 5
    publish_progress("instance_arbitration")
    instance = InstanceCoordinator()
    if not instance.acquire_primary():
        # First try the normal tray-restore path. If the old instance has just
        # entered shutdown, its tray may already be gone while network cleanup is
        # still unwinding. HOTFIX57 waits for the OS lock to be released and then
        # becomes primary instead of silently exiting forever.
        if instance.request("show_main_window", wait_ack=True, timeout=2.5):
            from jarvis.runtime.startup_handshake import acknowledge_existing_instance
            from jarvis.runtime.distribution164 import native_host
            return 0 if native_host() or acknowledge_existing_instance() else 4
        if instance.wait_for_release(timeout=10.0) and instance.acquire_primary():
            pass
        else:
            # A genuinely live but unresponsive primary still owns the lock. Do
            # not spawn a competing audio/UI stack. The launcher log records this
            # condition and a later launch can recover once that process exits.
            return 3
    # Normalize the process working directory so child processes and helper scripts
    # behave the same whether NEO is launched from Explorer, CMD or an IDE.
    try:
        os.chdir(root)
    except OSError:
        pass
    config = ConfigManager(root / "config" / "default_settings.json")
    configure_logging(config.paths.logs)
    publish_progress("configuration_ready")
    # VERSION.txt is the single release marker source. Keeping a second hard-
    # coded string previously made V3 binaries announce themselves as V2.
    try:
        release_version = (root / "VERSION.txt").read_text(encoding="utf-8").strip()
    except Exception:
        release_version = "NEO v4.0.20 ALPHA-BETA"
    build_marker = "NEO build: " + release_version
    if sys.stdout is not None:
        print(build_marker)
    logging.getLogger(__name__).info(build_marker)
    # The release label can stay the same across source snapshots. Report the
    # declared archive identity and a freshly checked, explicitly partial subset.
    from jarvis.runtime.build_identity import startup_identity_receipt
    source_identity, source_marker = startup_identity_receipt(root)
    if sys.stdout is not None:
        print(source_marker)
    logging.getLogger(__name__).info(source_marker)
    from jarvis.runtime.process_diagnostics184 import record_gui_start
    record_gui_start(config, source_identity)
    # Discord/Yandex audio files are transient handoff data, not a music
    # library. Clear leftovers immediately on every NEO process start so a
    # previous crash or forced shutdown cannot accumulate cached songs.
    try:
        YandexMusicAdapter.clear_transient_cache_on_startup_once()
    except Exception:
        pass
    session_guard = SessionGuard(config.paths.logs)
    previous_crash = session_guard.start() if bool(config.get("crash_recovery.enabled", True)) else None
    original_excepthook = sys.excepthook
    def _guarded_excepthook(exc_type, exc, tb):
        try:
            if isinstance(exc, BaseException): session_guard.mark_exception(exc)
        except Exception:
            pass
        original_excepthook(exc_type, exc, tb)
    sys.excepthook = _guarded_excepthook

    # Keep a low-level crash trace for native faults (Qt/DirectComposition/etc.).
    # On Windows an access violation otherwise terminates Python with 0xC0000005
    # and leaves no traceback at all. Keep the file object alive for app lifetime.
    crash_file = None
    try:
        config.paths.logs.mkdir(parents=True, exist_ok=True)
        crash_path = config.paths.logs / "native_crash.log"
        if crash_path.exists() and crash_path.stat().st_size > 10 * 1024 * 1024:
            crash_path.replace(crash_path.with_suffix(".log.1"))
        crash_file = open(crash_path, "a", encoding="utf-8")
        crash_file.write(f"\n=== NEO process start pid={os.getpid()} time={time.strftime('%Y-%m-%d %H:%M:%S')} build={release_version} ===\n")
        crash_file.flush()
        faulthandler.enable(file=crash_file, all_threads=True)
    except Exception:
        crash_file = None

    # Must happen before QApplication creates any native top-level windows.
    _configure_windows_shell_identity()
    app = QApplication(sys.argv)

    def _request_quit(_signum, _frame) -> None:
        QTimer.singleShot(0, window._quit_application)

    try:
        signal.signal(signal.SIGINT, _request_quit)
    except Exception:
        pass
    app.setApplicationName("NEO")
    app.setOrganizationName("NeoJarvis")
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    icon = root / "assets" / "neo_jarvis.ico"
    if not icon.exists():
        icon = root / "assets" / "robot_taskbar.svg"
    daily_launcher = package_root / "NEO.exe"
    # Prepare a real Start Menu identity before the user pins the running window.
    # The matching AppUserModelID prevents Windows from pinning pythonw.exe.
    ensure_start_menu_shortcut(daily_launcher, root / "assets" / "neo_jarvis.ico")
    app_icon = QIcon(str(icon)) if icon.exists() else QIcon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    publish_progress("ui_constructing")
    window = MainWindow(config)
    publish_progress("ui_constructed")
    from jarvis.runtime.desktop_services import DesktopServices
    desktop_services = DesktopServices(config, window.desktop_service_event_signal.emit)
    window.desktop_services = desktop_services
    desktop_services.start()
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)

    # HOTFIX123: lifelong ambient intelligence. The controller is intentionally
    # outside Agent execution: it can learn context, but it cannot call tools or
    # grant itself permissions. Microphone learning reuses rejected wake ASR.
    ambient_learning = AmbientLearningController(
        config,
        context_memory=getattr(window.agent, "context_memory", None),
        busy_provider=lambda: bool(getattr(window, "_busy", False) or getattr(window, "_quitting", False)),
    )
    window.ambient_learning = ambient_learning
    ambient_learning.start()

    # Passive low-frequency system timeline feeds both durable context and the
    # ambient learner without adding a second foreground-window poll.
    system_timeline = SystemContextTracker(
        config, getattr(window.agent, "context_memory", None),
        observation_callback=lambda payload: (ambient_learning.observe_system(payload), desktop_services.observe_activity(payload)),
    )
    system_timeline.start()
    gaming_memory = GamingContextTracker(
        config, getattr(window.agent, "context_memory", None),
        notice_callback=window.game_event_signal.emit,
    )
    gaming_memory.start()
    # NEO 167: "match found" notices (taskbar flash / League client) and accepting.
    from jarvis.gaming.match_ready import match_ready_watcher
    match_watch = match_ready_watcher(config)
    match_watch.set_notice_callback(window.match_notice_signal.emit)
    match_watch.start()

    # Telegram is a separate untrusted transport with its own Agent instance and
    # confirmation state.  It deliberately does not share the local voice
    # Agent._pending_context, preventing a Telegram callback from confirming a
    # voice-side action (or vice versa).
    telegram_remote = TelegramRemoteController(config, event_callback=window.telegram_event_signal.emit)
    # Keep Telegram's own Agent/security confirmation state isolated, while media
    # buttons use the desktop ToolRegistry so current track/queue/like state is shared.
    telegram_remote.set_desktop_tools(window.agent.tools)
    window.telegram_remote = telegram_remote
    match_watch.attach_telegram(telegram_remote)
    # No unsolicited startup chat/Telegram message. A clean first launch stays
    # visually idle; status appears only when the user asks for it or a real
    # diagnostic failure needs attention.

    # Local interactive resources are torn down first. Release the single-instance
    # lock immediately afterwards, before any bounded Telegram network cleanup, so
    # a user can relaunch NEO as soon as the tray exit completes.
    if bool(config.get("crash_recovery.enabled", True)):
        app.aboutToQuit.connect(session_guard.mark_shutting_down)
    app.aboutToQuit.connect(gaming_memory.stop)
    app.aboutToQuit.connect(match_watch.stop)
    app.aboutToQuit.connect(desktop_services.stop)
    app.aboutToQuit.connect(system_timeline.stop)
    app.aboutToQuit.connect(ambient_learning.stop)
    app.aboutToQuit.connect(window.shutdown_resources)
    # Remove/disable the active Telegram inline panel while Bot API access is
    # still fully alive.  This makes a normal tray exit or Settings -> disable
    # visibly take the remote controls offline instead of leaving stale buttons.
    app.aboutToQuit.connect(telegram_remote.prepare_shutdown_ui)
    app.aboutToQuit.connect(instance.mark_shutting_down)
    app.aboutToQuit.connect(instance.close)
    app.aboutToQuit.connect(telegram_remote.stop)
    # Create the frameless HWND and attach its icon/relaunch identity *before* it
    # is shown. Windows creates the taskbar button on first show, so assigning the
    # native icon and explicit AppUserModelID here prevents a generic blank-file
    # glyph from being cached for the running window.
    apply_native_window_icon(window, root / "assets" / "neo_jarvis.ico")
    configure_taskbar_relaunch(window, daily_launcher, root / "assets" / "neo_jarvis.ico")
    window.show()
    from jarvis.runtime.startup_handshake import publish_ready
    from jarvis.runtime.vision_startup import start_vision_backend
    publish_progress("ui_shown_waiting_for_event_loop")
    startup_ready_timer = QTimer(window)
    startup_ready_timer.setSingleShot(True)
    window._startup_ready_timer = startup_ready_timer
    ready_attempts = 0
    def _publish_ui_ready() -> None:
        nonlocal ready_attempts
        ready_attempts += 1
        if not publish_ready() and ready_attempts < 3:
            startup_ready_timer.start(250 * ready_attempts)
    startup_ready_timer.timeout.connect(_publish_ui_ready)
    startup_ready_timer.start(0)
    # Only the loopback server starts here. Probing/installation run outside Qt,
    # without loading a model or blocking the UI event loop.
    QTimer.singleShot(1200, lambda: start_vision_backend(config))

    # Compatibility flag for old launchers. Current NEO.exe uses the per-run/pid
    # JSON handshake above and reports startup failures in its GUI.
    try:
        bootstrap_root = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "NeoJarvis" / "bootstrap"
        bootstrap_root.mkdir(parents=True, exist_ok=True)
        ready_path = bootstrap_root / "gui_ready.flag"
        ready_path.write_text(f"pid={os.getpid()}\nbuild={build_marker}\ntime={time.time()}\n", encoding="utf-8")
    except Exception:
        logging.getLogger(__name__).debug("Could not write GUI startup handshake", exc_info=True)

    external_command_timer = QTimer(window)
    external_command_timer.setInterval(140)
    def _drain_external_instance_commands() -> None:
        for command in instance.drain_commands():
            action = str(command.get("action", "") or "").casefold()
            if action in {"show", "show_main_window", "restore"}:
                window._show_from_tray()
                instance.acknowledge(command)
                continue
            if action in {"quit", "quit_app", "shutdown_app", "exit_app"}:
                # ACK before scheduling app.quit(), otherwise aboutToQuit closes
                # the coordinator and a launcher can incorrectly assume the old
                # process is wedged. The normal Qt shutdown path still runs all
                # local cleanup hooks.
                instance.acknowledge(command)
                QTimer.singleShot(0, window._quit_application)
                continue
            # ping/unknown benign commands are acknowledged without UI effects.
            instance.acknowledge(command)
    external_command_timer.timeout.connect(_drain_external_instance_commands)
    external_command_timer.start()
    # Consume a launch request that may have arrived while this primary instance
    # was still constructing the UI.
    QTimer.singleShot(0, _drain_external_instance_commands)

    # Re-assert once after the compositor sees the window. Some Qt/Windows builds
    # recreate the native backing surface during the first translucent show.
    QTimer.singleShot(80, lambda: apply_native_window_icon(window, root / "assets" / "neo_jarvis.ico"))
    QTimer.singleShot(140, lambda: configure_taskbar_relaunch(window, daily_launcher, root / "assets" / "neo_jarvis.ico"))
    # NEO.cmd hands normal launches to the source-built NEO.exe GUI shim.
    # It owns managed-runtime setup and waits for the per-run readiness proof.
    # HOTFIX107: remote-start companion is independent from heavy NEO
    # autostart and remains alive after a normal NEO shutdown.
    QTimer.singleShot(220, config.refresh_wake_service_target)
    if bool(config.get("ui.start_with_windows", False)):
        # V5.7 refreshes both standard Startup and existing priority launchers to
        # this extracted release. This path never requests elevation; if a priority
        # task is missing it keeps a standard silent fallback instead of losing
        # autostart after an update.
        QTimer.singleShot(300, config.refresh_autostart_target)
    if previous_crash is not None:
        try:
            session_guard.enrich_previous(window.agent.database.recent_actions(int(config.get("crash_recovery.recent_action_limit", 100) or 100)))
        except Exception:
            pass
        QTimer.singleShot(500, lambda: window.notify_previous_crash(session_guard.recovery))
    # Stage network/model startup instead of creating a single native-resource
    # burst. Fresh V4 Windows crash dumps showed Discord, Telegram, audio and
    # legacy SQLite enrichment overlapping in the first seconds. Game-data
    # maintenance is separately delayed even further by its startup grace.
    # Telegram remains a no-op until Settings → Remote connection enables it.
    QTimer.singleShot(2200, telegram_remote.start)
    if bool(config.get("startup_self_test.enabled", True)):
        QTimer.singleShot(3200, window.run_startup_self_test)
    try:
        return app.exec()
    except KeyboardInterrupt:
        return 0
    finally:
        window.shutdown_resources()
        # Core Audio/comtypes owns a dedicated COM apartment in V5.3. Release it
        # explicitly while Python/Qt are still fully alive; never defer COM
        # finalizers to interpreter garbage collection. Telegram's aboutToQuit
        # hook has already stopped accepting remote commands at this point.
        try:
            from jarvis.audio.control_worker import shutdown_audio_control_executor
            if not shutdown_audio_control_executor(timeout=2.0):
                window._shutdown_incomplete = True
        except Exception:
            window._shutdown_incomplete = True
        window.mark_shutdown_complete()
        if bool(config.get("crash_recovery.enabled", True)) and not bool(getattr(window, "_shutdown_incomplete", False)):
            try:
                session_guard.clean_shutdown()
            except Exception:
                pass
        if crash_file is not None:
            try:
                crash_file.flush()
                crash_file.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
