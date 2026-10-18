# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

logger = logging.getLogger(__name__)
# NEO 184: the longest timer / reminder NEO stores (about ten years).
MAX_SECONDS = 10 * 366 * 86400.0


class ActionExpiredError(ValueError):
    """A durably consumed action missed its execution window."""


@dataclass
class TimerItem:
    id: str
    label: str
    due_at: float
    created_at: float
    kind: str = "timer"
    warned: list[int] = field(default_factory=list)
    auto_label: bool = False
    # NEO 172: repeating reminders and scheduled commands («каждый день в 9 открывай почту»).
    repeat: str = ""          # daily | weekdays | weekends | mon,tue,…
    hour: int = -1
    minute: int = 0
    action_spec: dict = field(default_factory=dict)  # reviewed one-shot action, never command text
    command: str = ""         # a NEO command run visibly at that time; "" = a reminder


_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def repeat_days(repeat: str) -> set[int]:
    value = str(repeat or "").casefold()
    if value == "daily":
        return set(range(7))
    if value == "weekdays":
        return {0, 1, 2, 3, 4}
    if value == "weekends":
        return {5, 6}
    return {_DAYS.index(x) for x in value.split(",") if x in _DAYS}


def next_due(hour: int, minute: int, repeat: str, after: float | None = None) -> float:
    """The next moment (local time) matching hour:minute on one of the repeat days, strictly after ``after``."""
    from datetime import datetime, timedelta
    days = repeat_days(repeat) or set(range(7))
    base = datetime.fromtimestamp(after if after is not None else time.time())
    candidate = base.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    for _ in range(9):
        if candidate.timestamp() > base.timestamp() and candidate.weekday() in days:
            return candidate.timestamp()
        candidate += timedelta(days=1)
    return candidate.timestamp()


@dataclass(frozen=True)
class TimerEvent:
    kind: str  # chat/system
    text: str
    timer_id: str
    language: str = ""   # the language ``text`` was written in (its label is user data)


class TimerManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._timers: dict[str, TimerItem] = {}
        self._events: Queue[TimerEvent] = Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._path: Path | None = None
        self._lang: str = "ru"
        self._action_deadlines: dict[str, float] = {}
        # 186: an elapsed-duration timer created in this process counts down on the
        # monotonic clock, so a wall-clock jump cannot change the promised length.
        # due_at stays the persisted wall estimate used after a restart; absolute
        # alarms (create_at) and repeating local-time items keep wall-clock targets.
        self._duration_deadlines: dict[str, float] = {}
        self._action_ready: set[str] = set()
        self._storage_dirty = False
        self._storage_retry_at = 0.0
        self._storage_retry_delay = 5.0
        self._storage_outage = False
        self._kick = threading.Event()     # NEO 172: wake the loop when timers change

    def configure(self, data_root: Path, language: str = "ru") -> None:
        path = Path(data_root) / "timers.json"
        self._lang = language if language in {"ru","en","de","fr","es","it","pt"} else "ru"
        with self._lock:
            if self._path == path:
                return
            self._path = path
            self._load_locked()
            self._ensure_thread_locked()

    def _ensure_thread_locked(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="NeoJarvis-Timers", daemon=True)
        self._thread.start()

    def _load_locked(self) -> None:
        self._timers.clear()
        self._action_deadlines.clear()
        self._duration_deadlines.clear()
        self._action_ready.clear()
        self._storage_dirty = False
        self._storage_retry_at = 0.0
        self._storage_retry_delay = 5.0
        self._storage_outage = False
        if not self._path or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("timers.json could not be read; timers start empty")
            return
        known = set(TimerItem.__dataclass_fields__)
        skipped = 0
        # NEO 184: one damaged row is skipped on its own; the valid rows after it still load.
        for item in raw if isinstance(raw, list) else []:
            try:
                timer = TimerItem(**{k: v for k, v in item.items() if k in known})
                if timer.kind == "action" or timer.action_spec:
                    # A previous process cannot retain a live target or authorization.
                    # Even future actions require rescheduling after restart.
                    from jarvis.runtime.delayed_actions184 import message
                    self._emit("chat", message("restart", self._lang, label=str(timer.label)), str(timer.id))
                    continue
                timer.due_at = float(timer.due_at)
                if not math.isfinite(timer.due_at) or not str(timer.id or "").strip():
                    raise ValueError("bad timer row")
                datetime.fromtimestamp(timer.due_at)
                if timer.repeat and int(timer.hour) >= 0:
                    # A repeating item missed while NEO was closed is not run late: the next time counts.
                    if timer.due_at <= time.time():
                        timer.due_at = next_due(timer.hour, timer.minute, timer.repeat)
                    self._timers[timer.id] = timer
                elif timer.due_at > time.time() - 3600:
                    self._timers[timer.id] = timer
            except Exception:
                skipped += 1
        if any(isinstance(row, dict) and (row.get("kind") == "action" or row.get("action_spec")) for row in (raw if isinstance(raw, list) else [])):
            try:
                self._save_locked()
            except OSError:
                # The in-memory set already excludes every action. A read-only
                # store must not prevent ordinary timers or the desktop starting.
                logger.warning("Could not persist restart cancellation of delayed actions", exc_info=True)
                from jarvis.runtime.delayed_actions184 import message
                self._emit("chat", message("restart_save_failed", self._lang), "")
        if skipped:
            logger.warning("Skipped %d damaged timer row(s) in timers.json", skipped)

    def _save_locked(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(x) for x in self._timers.values()], ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def create(self, seconds: float, label: str = "", kind: str = "timer", *, absolute: bool = False) -> TimerItem:
        seconds = float(seconds)
        # NEO 184: the time must exist on the calendar before anything is stored.
        if not math.isfinite(seconds) or seconds > MAX_SECONDS:
            raise ValueError("Timer duration is out of range")
        seconds = max(1.0, seconds)
        datetime.fromtimestamp(time.time() + seconds)
        defaults={"ru":("Таймер","Напоминание","Будильник"),"en":("Timer","Reminder","Alarm"),"de":("Timer","Erinnerung","Wecker"),"fr":("Minuteur","Rappel","Alarme"),"es":("Temporizador","Recordatorio","Alarma"),"it":("Timer","Promemoria","Sveglia"),"pt":("Temporizador","Lembrete","Alarme")}
        names=defaults.get(self._lang, defaults["ru"])
        fallback=names[0] if kind == "timer" else (names[2] if kind == "alarm" else names[1])
        # HOTFIX125: generic labels such as four identical “Таймер” rows are
        # difficult to distinguish in the desktop/Telegram/Discord surfaces. For
        # unlabeled timers include their duration in the generated name. Explicit
        # user labels are never rewritten.
        if not str(label or "").strip() and kind == "timer":
            rounded = max(1, int(round(seconds)))
            if rounded >= 3600 and rounded % 3600 == 0:
                amount = rounded // 3600
                fallback = {
                    "ru": f"Таймер на {amount} ч", "en": f"Timer · {amount} h", "de": f"Timer · {amount} Std.",
                    "fr": f"Minuteur · {amount} h", "es": f"Temporizador · {amount} h", "it": f"Timer · {amount} h", "pt": f"Temporizador · {amount} h",
                }.get(self._lang, f"Timer · {amount} h")
            elif rounded >= 60 and rounded % 60 == 0:
                amount = rounded // 60
                fallback = {
                    "ru": f"Таймер на {amount} мин", "en": f"Timer · {amount} min", "de": f"Timer · {amount} Min.",
                    "fr": f"Minuteur · {amount} min", "es": f"Temporizador · {amount} min", "it": f"Timer · {amount} min", "pt": f"Temporizador · {amount} min",
                }.get(self._lang, f"Timer · {amount} min")
            else:
                fallback = {
                    "ru": f"Таймер на {rounded} сек", "en": f"Timer · {rounded} sec", "de": f"Timer · {rounded} Sek.",
                    "fr": f"Minuteur · {rounded} s", "es": f"Temporizador · {rounded} s", "it": f"Timer · {rounded} s", "pt": f"Temporizador · {rounded} s",
                }.get(self._lang, f"Timer · {rounded} sec")
        timer = TimerItem(uuid.uuid4().hex[:8], str(label or fallback).strip(), time.time()+seconds, time.time(), kind)
        timer.auto_label = kind == "timer" and not str(label or "").strip()
        with self._lock:
            self._timers[timer.id] = timer
            try:
                self._save_locked()
            except BaseException:
                self._timers.pop(timer.id, None)     # NEO 184: memory stays what the file holds
                raise
            if not absolute:
                self._duration_deadlines[timer.id] = time.monotonic() + seconds
            self._ensure_thread_locked()
        self._kick.set()
        return timer

    def reschedule(self, timer_id: str, seconds: float) -> TimerItem:
        import math
        seconds = float(seconds)
        if not math.isfinite(seconds) or not 1 <= seconds <= 604800:
            raise ValueError("Timer duration must be between 1 second and 7 days")
        with self._lock:
            timer = self._timers.get(str(timer_id))
            if timer is not None and timer.kind == "action":
                raise ValueError("Cancel and recreate delayed actions to change their time")
            if timer is None:
                raise KeyError("Timer no longer exists")
            previous = (timer.due_at, timer.created_at, list(timer.warned), timer.label)
            timer.created_at = time.time()
            timer.due_at = timer.created_at + seconds
            timer.warned = []
            if timer.auto_label:
                from jarvis.runtime.duration import format_duration
                name = {"ru": "Таймер", "en": "Timer", "de": "Timer", "fr": "Minuteur",
                        "es": "Temporizador", "it": "Timer", "pt": "Temporizador"}[self._lang]
                timer.label = f"{name} · {format_duration(seconds, self._lang)}"
            try:
                self._save_locked()
            except OSError:
                timer.due_at, timer.created_at, timer.warned, timer.label = previous
                raise
            if not timer.repeat:
                self._duration_deadlines[timer.id] = time.monotonic() + seconds
            return timer

    def create_action(self, seconds: float, label: str, spec: dict) -> TimerItem:
        """A bounded in-process action; persist before publishing a countdown."""
        from copy import deepcopy
        from jarvis.runtime.delayed_actions184 import validate_spec
        seconds = float(seconds)
        if not math.isfinite(seconds) or not 1 <= seconds <= 604800:
            raise ValueError("Action duration must be between 1 second and 7 days")
        validate_spec(spec)
        now = time.time()
        timer = TimerItem(uuid.uuid4().hex[:8], str(label)[:240], now + seconds, now,
                          "action", action_spec=deepcopy(spec))
        with self._lock:
            if sum(t.kind == "action" for t in self._timers.values()) >= 32:
                raise ValueError("Too many delayed actions")
            self._timers[timer.id] = timer
            self._action_deadlines[timer.id] = time.monotonic() + seconds
            try:
                self._save_locked()
            except BaseException:
                self._timers.pop(timer.id, None)
                self._action_deadlines.pop(timer.id, None)
                raise
            self._ensure_thread_locked()
        self._kick.set()
        return timer

    def remaining(self, timer: TimerItem) -> float:
        with self._lock:
            return max(0., self._left_locked(timer))

    def display_due(self, timer: TimerItem, now: float | None = None) -> float:
        """HIST-030: the wall time to SHOW for a timer.

        A duration (or delayed action) fires on its monotonic deadline, so after a
        wall-clock correction its shown due time is the current projection
        «now + monotonic remaining», not the stale persisted due_at. Absolute
        alarms/reminders keep their calendar time."""
        wall = time.time() if now is None else now
        with self._lock:
            if timer.kind == "action" and timer.id in self._action_deadlines:
                return wall + max(0.0, self._action_deadlines[timer.id] - time.monotonic())
            deadline = self._duration_deadlines.get(timer.id)
            if deadline is not None:
                return wall + max(0.0, deadline - time.monotonic())
            return float(timer.due_at)

    def _left_locked(self, timer: TimerItem, now: float | None = None) -> float:
        """Signed seconds left; elapsed durations use the monotonic deadline."""
        if timer.kind == "action":
            return self._action_deadlines.get(timer.id, time.monotonic()) - time.monotonic()
        deadline = self._duration_deadlines.get(timer.id)
        if deadline is not None:
            return deadline - time.monotonic()
        return timer.due_at - (time.time() if now is None else now)

    def take_action(self, timer_id: str) -> TimerItem | None:
        """Consume once, durably, before an effect. Cancel still wins while queued."""
        with self._lock:
            item = self._timers.get(timer_id)
            if item is None or item.kind != "action" or timer_id not in self._action_ready:
                return None
            overdue = time.monotonic() - self._action_deadlines[timer_id]
            self._timers.pop(timer_id)
            try:
                self._save_locked()
            except BaseException:
                self._timers[timer_id] = item
                raise
            self._action_ready.discard(timer_id)
            self._action_deadlines.pop(timer_id, None)
            if overdue > 30:
                raise ActionExpiredError(item.label)
            return item

    def create_repeating(self, hour: int, minute: int, repeat: str, label: str, command: str = "") -> TimerItem:
        """A reminder or a scheduled NEO command at hour:minute on the repeat days."""
        if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59 or not repeat_days(repeat):
            raise ValueError("bad schedule")
        now = time.time()
        timer = TimerItem(uuid.uuid4().hex[:8], str(label).strip()[:160], next_due(hour, minute, repeat, now), now,
                          "routine" if command else "reminder", repeat=repeat, hour=int(hour), minute=int(minute),
                          command=str(command).strip()[:240])
        with self._lock:
            self._timers[timer.id] = timer
            try:
                self._save_locked()
            except BaseException:
                self._timers.pop(timer.id, None)
                raise
            self._ensure_thread_locked()
        self._kick.set()
        return timer

    def create_at(self, timestamp: float, label: str = "", kind: str = "alarm") -> TimerItem:
        return self.create(max(1.0, timestamp-time.time()), label, kind, absolute=True)

    def list(self) -> list[TimerItem]:
        with self._lock:
            return sorted(self._timers.values(), key=lambda x: x.due_at)

    def cancel(self, timer_id: str = "") -> int:
        with self._lock:
            if not self._timers:
                return 0
            snapshot = dict(self._timers)
            if timer_id:
                removed = 1 if self._timers.pop(timer_id, None) else 0
            else:
                # No id means cancel the next timer, matching natural "cancel timer" (a schedule needs its number).
                once = [x for x in self._timers.values() if not x.repeat] or list(self._timers.values())
                # REC-TIMER-CANCEL-CROSS-CLOCK-ORDER-01: "the next one" is ranked
                # by the same remaining-time model that makes it expire (monotonic
                # for durations), from one clock snapshot, not by a stale due_at.
                wall, mono = time.time(), time.monotonic()

                def left(item: TimerItem) -> float:
                    if item.kind == "action":
                        return self._action_deadlines.get(item.id, mono) - mono
                    deadline = self._duration_deadlines.get(item.id)
                    return deadline - mono if deadline is not None else item.due_at - wall
                next_item = min(once, key=lambda x: (left(x), x.due_at, x.id))
                self._timers.pop(next_item.id, None); removed = 1
            if not removed:
                return 0
            try:
                self._save_locked()
            except BaseException:
                self._timers.clear()
                self._timers.update(snapshot)        # NEO 184: the cancelled timer is still on disk, so still here
                raise
            for removed_id in set(snapshot) - set(self._timers):
                self._action_deadlines.pop(removed_id, None)
                self._duration_deadlines.pop(removed_id, None)
                self._action_ready.discard(removed_id)
        self._kick.set()
        return removed

    def cancel_kind(self, kind: str = "timer") -> list[str]:
        """NEO 187: «отмени все мои таймеры» — one atomic save, only this kind."""
        with self._lock:
            snapshot = dict(self._timers)
            gone = [key for key, item in snapshot.items() if item.kind == kind]
            if not gone:
                return []
            for key in gone:
                self._timers.pop(key, None)
            try:
                self._save_locked()
            except BaseException:
                self._timers.clear()
                self._timers.update(snapshot)
                raise
            for removed_id in gone:
                self._action_deadlines.pop(removed_id, None)
                self._duration_deadlines.pop(removed_id, None)
                self._action_ready.discard(removed_id)
        self._kick.set()
        return gone

    def drain(self, limit: int = 64) -> list[TimerEvent]:
        out=[]
        for _ in range(max(1, int(limit))):
            try: out.append(self._events.get_nowait())
            except Empty: break
        return out

    def _emit(self, kind: str, text: str, timer_id: str) -> None:
        self._events.put(TimerEvent(kind, text, timer_id, self._lang))

    def _sleep_seconds(self, now: float) -> float:
        """NEO 172: sleep until the next due time or warning (at most 30 s) instead of waking twice a second."""
        with self._lock:
            moments = []
            if self._storage_dirty:
                # Wall-clock changes cannot accelerate persistence retries.
                moments.append(now + max(0.05, self._storage_retry_at - time.monotonic()))
            for timer in self._timers.values():
                if timer.kind == "action":
                    if timer.id not in self._action_ready:
                        moments.append(now + self.remaining(timer))
                    continue
                due_at = now + self._left_locked(timer, now)
                moments.append(due_at)
                if not timer.repeat:
                    moments.extend(due_at - t for t in (900, 600, 300, 60) if t not in timer.warned)
        future = [m - now for m in moments if m - now > 0]
        return max(0.05, min([30.0] + future)) if moments else 30.0

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._kick.wait(self._sleep_seconds(time.time()))
            self._kick.clear()
            if self._stop.is_set():
                break
            self._tick()

    def _tick(self) -> None:
        now=time.time(); changed=False; due=[]; commands=[]
        thresholds = (900, 600, 300, 60)
        with self._lock:
            for timer in list(self._timers.values()):
                if timer.kind == "action":
                    if timer.id not in self._action_ready and self.remaining(timer) <= 0:
                        self._action_ready.add(timer.id)
                        self._emit("action", timer.label, timer.id)
                    continue
                remaining = self._left_locked(timer, now)
                for threshold in thresholds:
                    if timer.repeat:
                        break
                    if threshold not in timer.warned and 0 < remaining <= threshold and timer.due_at-timer.created_at > threshold:
                        timer.warned.append(threshold); changed=True
                        mins=threshold//60
                        if mins > 1:
                            text={
                                "ru":f"{timer.label}: осталось {mins} мин.","en":f"{timer.label}: {mins} min remaining.",
                                "de":f"{timer.label}: noch {mins} Min.","fr":f"{timer.label} : encore {mins} min.",
                                "es":f"{timer.label}: quedan {mins} min.","it":f"{timer.label}: mancano {mins} min.",
                                "pt":f"{timer.label}: faltam {mins} min.",
                            }.get(self._lang, f"{timer.label}: {mins} min remaining.")
                        else:
                            text={
                                "ru":f"{timer.label}: осталась 1 минута.","en":f"{timer.label}: 1 minute remaining.",
                                "de":f"{timer.label}: noch 1 Minute.","fr":f"{timer.label} : encore 1 minute.",
                                "es":f"{timer.label}: queda 1 minuto.","it":f"{timer.label}: manca 1 minuto.",
                                "pt":f"{timer.label}: falta 1 minuto.",
                            }.get(self._lang, f"{timer.label}: 1 minute remaining.")
                        self._emit("chat", text, timer.id)
                        if threshold == 60:
                            self._emit("system", text, timer.id)
                if remaining <= 0:
                    due.append(timer)
            for timer in due:
                changed=True
                if timer.repeat and timer.hour >= 0:
                    timer.due_at = next_due(timer.hour, timer.minute, timer.repeat, now)
                    if timer.command:
                        prefix={"ru":"По расписанию","en":"Scheduled","de":"Nach Zeitplan","fr":"Programmé","es":"Programado","it":"Programmato","pt":"Agendado"}.get(self._lang,"Scheduled")
                        # An occurrence is never executable until its next due
                        # state commits. Failed/backed-off occurrences are skipped,
                        # not queued for a surprising replay after recovery.
                        commands.append((timer.id, f"{prefix}: «{timer.label or timer.command}».", timer.command))
                        continue
                else:
                    self._timers.pop(timer.id, None)
                    self._duration_deadlines.pop(timer.id, None)
                if timer.kind == "timer":
                    text={"ru":f"{timer.label}: время вышло.","en":f"{timer.label}: time is up.","de":f"{timer.label}: Zeit ist abgelaufen.","fr":f"{timer.label} : temps écoulé.","es":f"{timer.label}: se acabó el tiempo.","it":f"{timer.label}: tempo scaduto.","pt":f"{timer.label}: o tempo acabou."}.get(self._lang, f"{timer.label}: time is up.")
                else:
                    prefix={"ru":"Напоминание","en":"Reminder","de":"Erinnerung","fr":"Rappel","es":"Recordatorio","it":"Promemoria","pt":"Lembrete"}.get(self._lang,"Reminder")
                    text=f"{prefix}: {timer.label}."
                self._emit("chat", text, timer.id); self._emit("system", text, timer.id)
            self._storage_dirty = self._storage_dirty or changed
            if self._flush_scheduler_state_locked():
                for timer_id, text, command in commands:
                    self._emit("chat", text, timer_id)
                    self._emit("command", command, timer_id)

    def _flush_scheduler_state_locked(self) -> bool:
        """Keep notifications alive; commit before releasing scheduled commands.

        A failed occurrence stays advanced in memory and is never retried. Only
        the latest state is retried, at 5/10/20/40/60-second intervals. The existing
        user create/cancel and delayed-action take methods retain their own atomic
        save/rollback behavior and do not use this best-effort notification path.
        """
        if not self._storage_dirty:
            return True
        now = time.monotonic()
        if now < self._storage_retry_at:
            return False
        try:
            self._save_locked()
        except OSError:
            self._storage_retry_at = now + self._storage_retry_delay
            self._storage_retry_delay = min(60.0, self._storage_retry_delay * 2)
            if not self._storage_outage:
                self._storage_outage = True
                logger.warning("Timer state could not be saved; retrying with backoff; scheduled commands skipped", exc_info=True)
                self._emit("chat", self._storage_message(False), "")
            return False
        self._storage_dirty = False
        self._storage_retry_at = 0.0
        self._storage_retry_delay = 5.0
        if self._storage_outage:
            self._storage_outage = False
            self._emit("chat", self._storage_message(True), "")
        return True

    def _storage_message(self, recovered: bool) -> str:
        messages = {
            "ru": ("Не удалось сохранить таймеры. Отсчёт и напоминания продолжаются; команды по расписанию пропускаются до восстановления записи.", "Сохранение таймеров восстановлено. Пропущенные команды не будут выполнены задним числом."),
            "en": ("Could not save timers. Countdowns and reminders continue; scheduled commands are skipped until saving recovers.", "Timer saving recovered. Skipped commands will not be replayed."),
            "de": ("Timer konnten nicht gespeichert werden. Countdowns und Erinnerungen laufen weiter; geplante Befehle werden bis zur Wiederherstellung übersprungen.", "Timer können wieder gespeichert werden. Übersprungene Befehle werden nicht nachgeholt."),
            "fr": ("Impossible d’enregistrer les minuteurs. Les décomptes et rappels continuent ; les commandes programmées sont ignorées jusqu’au rétablissement.", "L’enregistrement des minuteurs est rétabli. Les commandes ignorées ne seront pas rejouées."),
            "es": ("No se pudieron guardar los temporizadores. Las cuentas atrás y recordatorios continúan; se omiten las órdenes programadas hasta restablecer el guardado.", "Se restableció el guardado de temporizadores. Las órdenes omitidas no se ejecutarán después."),
            "it": ("Impossibile salvare i timer. Conti alla rovescia e promemoria continuano; i comandi programmati vengono saltati fino al ripristino del salvataggio.", "Il salvataggio dei timer è stato ripristinato. I comandi saltati non saranno eseguiti in ritardo."),
            "pt": ("Não foi possível guardar os temporizadores. As contagens e lembretes continuam; os comandos agendados são ignorados até a gravação ser restabelecida.", "A gravação dos temporizadores foi restabelecida. Os comandos ignorados não serão executados depois."),
        }
        return messages.get(self._lang, messages["en"])[int(recovered)]


_MANAGER = TimerManager()


def timer_manager(data_root: Path | None = None, language: str = "ru") -> TimerManager:
    if data_root is not None:
        _MANAGER.configure(Path(data_root), language)
    return _MANAGER
