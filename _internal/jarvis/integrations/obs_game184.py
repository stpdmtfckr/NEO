"""One opt-in, cancellable game-start worker; never starts a live stream."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import threading
import time


class GameStart:
    def __init__(self, service):
        self.service=service
        self._lock=threading.Lock()
        self._epoch=0
        self._session=None
        self._notify=None
        self._thread=None

    def _settings(self):
        config=self.service.config
        return self.service._connection_settings()+tuple(config.get("streaming."+k,d) for k,d in (
            ("enabled",False),("auto_launch_on_game",False),("auto_replay_on_game",False),("executable","")))

    def ended(self):
        with self._lock:
            self._epoch+=1
            self._session=None

    def started(self, session, notify=None):
        with self._lock:
            if session==self._session:
                return
            self._epoch+=1
            self._session=session
            self._notify=notify
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread=threading.Thread(target=self._work,name="NEO-OBS-GameStart",daemon=True)
            self._thread.start()

    def _work(self):
        while True:
            with self._lock:
                epoch,session,notify=self._epoch,self._session,self._notify
                if session is None:
                    self._thread=None
                    return
            config=self.service.config
            settings=self._settings()
            def current():
                return epoch==self._epoch and settings==self._settings() and config.get("streaming.enabled",False) is True
            def tell(result):
                if notify and current():
                    try:
                        notify(result["message"])
                    except Exception:
                        pass
            result=self.service.execute({"action":"connect"},guard=current)
            if result["status"]=="connection_failed" and current() and config.get("streaming.auto_launch_on_game",False) is True:
                try:
                    path=Path(str(config.get("streaming.executable","") or ""))
                    if not path.is_absolute() or not path.is_file() or path.name.casefold() not in {"obs64.exe","obs32.exe","obs"}:
                        raise ValueError("Configured OBS executable unavailable")
                    import psutil
                    if any(str(p.info.get("name","")).casefold() in {"obs64.exe","obs32.exe","obs"} for p in psutil.process_iter(["name"])):
                        raise ValueError("OBS already running; verify its connection settings")
                    if not current():
                        raise ValueError("Superseded game session")
                    # No shell, credentials, installer, service key or capture flags.
                    subprocess.Popen([str(path),"--minimize-to-tray"],cwd=str(path.parent),stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000 if os.name=="nt" else 0)
                except Exception:
                    result=self.service._result("launch_failed",ok=False)
                else:
                    deadline=time.monotonic()+30
                    while current() and time.monotonic()<deadline:
                        time.sleep(1)
                        result=self.service.execute({"action":"connect"},guard=current)
                        if result["ok"]:
                            break
            if current():
                if not result["ok"]:
                    tell(result)
                elif config.get("streaming.auto_replay_on_game",False) is True:
                    tell(self.service.execute({"action":"replay_start"},guard=current))
            with self._lock:
                if epoch==self._epoch or self._session is None:
                    self._thread=None
                    return
