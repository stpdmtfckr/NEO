"""Suppress NEO implementation traffic by provenance and path, never extension.

The policy enumerates known/configured roots once; it does not scan directories.
User files written by a NEO action remain observable outside these owned roots.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import re
import tempfile


def normalized_path(value) -> str:
    raw = str(value or "").strip().strip('"')
    if not raw or "\x00" in raw:
        return ""
    raw = re.sub(r"%([^%]+)%",lambda m:os.environ.get(m[1],m[0]),os.path.expandvars(raw))
    if re.match(r"^[A-Za-z]:[\\/]",raw) or raw.startswith(("\\\\","//")):
        return ntpath.normpath(raw).replace("\\","/").rstrip("/").casefold()
    try:
        return str(Path(raw).expanduser().resolve(strict=False)).replace("\\","/").rstrip("/").casefold()
    except (OSError,RuntimeError,ValueError):
        return ""


def under(path: str, root: str) -> bool:
    return bool(root and path and (path == root or path.startswith(root + "/")))


class ObservationPolicy:
    _INTERNAL_ORIGINS = frozenset({"neo.runtime","neo.bootstrap","neo.cache","neo.model","neo.profile",
                                  "stt.worker","wake.worker","music.cache","ollama.model"})

    def __init__(self, config=None, *, root=None):
        self.config = config
        get = config.get if config is not None else lambda key,default=None:default
        state = root or getattr(getattr(config,"paths",None),"root",None)
        internal = Path(__file__).resolve().parents[2]
        roots = [internal,internal.parent/".venv",internal.parent/"venv",internal.parent/".runtime"]
        exact = [internal.parent/"NEO.exe",internal.parent/"NEO.cmd"]
        if state:
            state = Path(state)
            roots.extend(state / name for name in ("runtime","bootstrap","cache","models","logs","data","diagnostics","profiles","backups","настройки"))
            exact.extend(state / name for name in ("settings.json","settings.json.tmp","assistant.db","assistant.db-shm","assistant.db-wal"))
        for attr in ("cache","logs","backups"):
            value = getattr(getattr(config,"paths",None),attr,None)
            if value:
                roots.append(value)
        for key in ("paths.internal_dir","paths.runtime_dir","paths.models_dir","paths.cache_dir","paths.temp_dir",
                    "voice.model_cache_dir","voice.download_root","voice.model_path","wake_word.model_path",
                    "ollama.models_dir","ollama.models_path","integrations.yandex_music.cache_dir","music.cache_dir"):
            value = get(key,"")
            if isinstance(value,(str,Path)) and str(value).strip():
                roots.append(value)
        for variable in ("NEO_VENV","NEOJARVIS_CACHE_DIR","OLLAMA_MODELS","HF_HOME","HF_HUB_CACHE","HUGGINGFACE_HUB_CACHE","TRANSFORMERS_CACHE"):
            if os.environ.get(variable):
                roots.append(os.environ[variable])
        home = Path.home()
        roots.extend((home/".ollama"/"models",home/".cache"/"huggingface",home/".cache"/"torch"))
        for variable in ("LOCALAPPDATA","APPDATA"):
            if os.environ.get(variable):
                base = Path(os.environ[variable])
                roots.extend(base/"NeoJarvis"/name for name in ("runtime","bootstrap","cache","models","logs","data","diagnostics","profiles","настройки"))
                # Browser profiles contain implementation databases/caches; a
                # real browser download outside them is not filtered by origin.
                roots.extend((base/"Google"/"Chrome"/"User Data",base/"Microsoft"/"Edge"/"User Data",
                              base/"BraveSoftware"/"Brave-Browser"/"User Data"))
        self.roots = tuple(dict.fromkeys(normalized_path(p) for p in roots if p))
        self.exact = frozenset(normalized_path(p) for p in exact)
        self.state = normalized_path(state) if state else ""
        self.temp = normalized_path(tempfile.gettempdir())
        self.extra = {}
        self.excluded_apps = {str(x).casefold() for x in (get('activity.excluded_apps',[]) or [])}
        for channel,key in (("activity","activity.excluded_paths"),("download","downloads.excluded_paths")):
            values = get(key,[]) or []
            if isinstance(values,str): values=[values]
            self.extra[channel] = tuple(normalized_path(x) for x in values[:100] if isinstance(x,(str,Path)))

    def allows_path(self, path, *, channel="activity") -> bool:
        value = normalized_path(path)
        if not value:
            return True  # No path evidence is not evidence of internal origin.
        if value in self.exact or any(under(value,root) for root in self.roots + self.extra.get(channel,())):
            return False
        if self.state and under(value,self.state) and value.rsplit("/",1)[-1].startswith(".neo-write-probe-"):
            return False
        if under(value,self.temp):
            relative = value[len(self.temp)+1:]
            if re.match(r"(?:neo_jarvis_deno_[^/]+(?:/|$)|neo-setup-(?:out|err)-[0-9a-f]{32}\.log$|neo-ollama-[0-9a-f]{32}\.exe$|neo-jarvis-python-[0-9.]+-amd64\.exe$)",relative):
                return False
        return True

    def allows_event(self, kind: str, payload: dict) -> bool:
        if not isinstance(payload,dict):
            return False
        origin = str(payload.get("origin",payload.get("source_kind",""))).casefold()
        if origin in self._INTERNAL_ORIGINS:
            return False
        channel = "download" if str(kind).startswith(("download.","installation.")) else "activity"
        for key in ("path","actual_path","destination","folder_path","source_path","target_path"):
            if payload.get(key) and not self.allows_path(payload[key],channel=channel):
                return False
        if str(kind).startswith(("activity.window","activity.session")):
            try:
                if int(payload.get("pid",0) or 0) == os.getpid():
                    return False
            except (TypeError,ValueError):
                pass
            executable = payload.get("process_path") or payload.get("exe")
            if executable and not self.allows_path(executable):
                return False
            process = str(payload.get("process_name",payload.get("process",""))).casefold()
            if process in self.excluded_apps:
                return False
            if process in {"neo.exe","neo.jarvis.exe","neo_jarvis.exe"}:
                return False
            # Managed Python can host unrelated user actions. Only NEO-branded
            # windows of Python workers are excluded when their exe is unknown.
            title = str(payload.get("title","")).strip().casefold()
            if any(marker in title for marker in ('incognito','inprivate','private browsing','приватн','инкогнито',
                    'navigation privée','privates fenster','navegación privada','navegação privada','navigazione in incognito')):
                return False
            if process in {"python.exe","pythonw.exe","neo.exe"} and re.match(r"^(?:neo(?:\s|$)|настройки neo(?:\s|$))",title):
                return False
        return True
