# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import os
import sys
import time

from jarvis.runtime.instance_coordinator import InstanceCoordinator

GATE_PROCEED = 0
GATE_HANDLED = 10
GATE_ERROR = 20


def _message_box(text: str, title: str, flags: int) -> int:
    # Test hook keeps regression tests deterministic on non-Windows hosts.
    scripted = str(os.getenv("NEO_JARVIS_TEST_DIALOG", "") or "").strip().casefold()
    if scripted:
        mapping = {"yes": 6, "no": 7, "cancel": 2, "ok": 1}
        return mapping.get(scripted, 2)
    if os.name != "nt":
        return 2
    try:
        import ctypes
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:
        return 2


def _choice_dialog(state: dict) -> int:
    pid = int(state.get("pid", 0) or 0)
    shutting = bool(state.get("shutting_down", False))
    status = "завершается" if shutting else "уже запущен"
    text = (
        f"NEO {status}." + (f"\nPID: {pid}" if pid else "") +
        "\n\nДа — ПЕРЕЗАПУСТИТЬ NEO"
        "\nНет — ПОЛНОСТЬЮ ЗАВЕРШИТЬ NEO"
        "\nОтмена — ничего не делать"
        "\n\nЕсли окно или трей зависли, перезапуск сможет предложить принудительное завершение."
    )
    # MB_YESNOCANCEL | MB_ICONQUESTION | MB_SETFOREGROUND
    return _message_box(text, "NEO — экземпляр уже работает", 0x00000003 | 0x00000020 | 0x00010000)


def _force_dialog(pid: int) -> bool:
    text = (
        "Запущенный экземпляр NEO не ответил на команду завершения."
        + (f"\nPID: {pid}" if pid else "") +
        "\n\nПринудительно завершить зависший процесс?"
        "\nНесохранённое состояние текущей сессии может быть потеряно."
    )
    # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND
    return _message_box(text, "NEO — процесс не отвечает", 0x00000004 | 0x00000030 | 0x00010000) == 6


def _graceful_stop(instance: InstanceCoordinator) -> bool:
    # If the primary is in the middle of shutdown, let it release the OS lock
    # before deciding it is unresponsive.
    state = instance.read_state()
    if bool(state.get("shutting_down", False)):
        if instance.wait_for_release(timeout=6.0):
            instance.clear_stale_metadata()
            return True
    if instance.request("quit_app", wait_ack=True, timeout=3.5):
        if instance.wait_for_release(timeout=10.0):
            instance.clear_stale_metadata()
            return True
    else:
        if instance.wait_for_release(timeout=3.0):
            instance.clear_stale_metadata()
            return True
    return not instance.has_primary()


def startup_gate() -> int:
    instance = InstanceCoordinator()
    if not instance.has_primary():
        instance.clear_stale_metadata()
        return GATE_PROCEED

    # Autostart must never stop at an interactive dialog. If a duplicate startup
    # race occurs, simply ask the existing instance to stay alive and exit.
    if str(os.getenv("NEO_JARVIS_AUTOSTART", "") or "").strip() == "1":
        instance.request("ping", wait_ack=True, timeout=2.0)
        return GATE_HANDLED

    choice = _choice_dialog(instance.read_state())
    if choice == 2:  # Cancel
        return GATE_HANDLED
    restart = choice == 6  # Yes
    full_stop = choice == 7  # No
    if not (restart or full_stop):
        return GATE_HANDLED

    stopped = _graceful_stop(instance)
    if not stopped:
        pid = instance.primary_pid()
        if not _force_dialog(pid):
            return GATE_HANDLED
        stopped = instance.force_terminate_primary(timeout=10.0)
        if not stopped:
            _message_box(
                "Не удалось завершить предыдущий экземпляр NEO.\n"
                "Перезагрузите Windows или проверьте launcher.log.",
                "NEO",
                0x00000000 | 0x00000010 | 0x00010000,
            )
            return GATE_ERROR

    # Full stop deliberately consumes this launch. Restart continues into the
    # normal START_JARVIS bootstrap after the old lock is gone.
    return GATE_PROCEED if restart else GATE_HANDLED


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "show_main_window").strip() or "show_main_window"
    if action.casefold() in {"startup_gate", "launch_gate", "gate"}:
        return startup_gate()
    return 0 if InstanceCoordinator().request(action, wait_ack=True, timeout=3.0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
