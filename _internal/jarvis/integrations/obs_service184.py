"""Separate OBS backend: explicit state changes and observed, local outcomes."""
from __future__ import annotations
import hashlib
import math
import os
from pathlib import Path
import threading
import time
import weakref

from jarvis.integrations.obs_websocket184 import ObsClient, ObsError, endpoint

OUTPUTS = {
    "record_start":("GetRecordStatus","outputActive",True,"StartRecord"),
    "record_stop":("GetRecordStatus","outputActive",False,"StopRecord"),
    "record_pause":("GetRecordStatus","outputPaused",True,"PauseRecord"),
    "record_resume":("GetRecordStatus","outputPaused",False,"ResumeRecord"),
    "stream_start":("GetStreamStatus","outputActive",True,"StartStream"),
    "stream_stop":("GetStreamStatus","outputActive",False,"StopStream"),
    "replay_start":("GetReplayBufferStatus","outputActive",True,"StartReplayBuffer"),
    "replay_stop":("GetReplayBufferStatus","outputActive",False,"StopReplayBuffer"),
}
ACTIONS = frozenset(OUTPUTS) | {"connect","reconnect","disconnect","status","replay_save","scenes_list",
                              "scene_switch","input_mute","input_unmute","input_volume","source_show","source_hide"}


def _flag(data, key):
    if type(data.get(key)) is not bool:
        raise ObsError("protocol_error")
    return data[key]


def _number(data, key):
    value = data.get(key)
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0:
        raise ObsError("protocol_error")
    return value


class ObsService:
    def __init__(self, config, *, client_factory=ObsClient, file_timeout=4):
        self.config = config
        self._factory = client_factory
        self._client = None
        self._signature = None
        self._connection_epoch = 0
        self._session_password = ""
        self._credential_revision = 0
        self._credential_lock = threading.Lock()
        self._lock = threading.RLock()
        self._last_error = ""
        self._file_timeout = max(.12,min(float(file_timeout),8))
        self._operation_connection = None
        self._operation_collection = None
        self._operation_guard = None
        self._live_binding = None
        self._game = None

    def _message(self, key, **values):
        from jarvis.language import language_code
        from jarvis.integrations.obs_text184 import tr
        return tr(key,language_code(self.config),**values)

    def _result(self, key, *, ok=True, **values):
        if ok:
            self._last_error = ""
        return {"ok":ok,"status":key,"message":self._message(key,**values),**values}

    def _password_for(self, signature):
        with self._credential_lock:
            session=self._session_password if signature[3]==self._credential_revision else ""
        token=str(signature[2] or "")
        def read():
            if self._connection_settings()!=signature or self.config.get("streaming.enabled",False) is not True:
                return ""
            if session:
                return session
            return self._unprotect(token)
        return read

    @staticmethod
    def _unprotect(token):
        if os.name != "nt" or not token.startswith("dpapi:"):
            return ""
        from jarvis.secrets import unprotect_secret
        try:
            return unprotect_secret(token)
        except Exception:
            return ""

    def set_session_password(self, value):
        # Apply/Save runs on GUI: no network, command lock or socket close here.
        with self._credential_lock:
            self._session_password = str(value or "")
            self._credential_revision += 1
        self._last_error = ""
        self.game_ended()

    def _connection_settings(self):
        with self._credential_lock:
            return (self.config.get("streaming.host","127.0.0.1"),self.config.get("streaming.port",4455),
                    self.config.get("streaming.password_encrypted",""),self._credential_revision)

    def confirmation_identity(self):
        """Opaque read-only pending-approval identity. Never contacts OBS."""
        host,port,token,revision = self._connection_settings()
        return (id(self),host,port,hashlib.sha256(str(token).encode()).hexdigest(),revision,self._connection_epoch,
                self.config.get("streaming.enabled",False) is True,bool(self._client and self._client.connected))

    def _close(self):
        self._connection_epoch += 1
        if self._client is not None:
            self._client.close()
        self._client = None
        self._signature = None

    def _ensure(self):
        if self.config.get("streaming.enabled",False) is not True:
            self._close()
            raise ObsError("disabled")
        signature = self._connection_settings()
        if self._operation_connection is not None and signature != self._operation_connection:
            # HIST-037: the integration is on; its connection settings changed while
            # this operation waited. Refuse it safely and say exactly that.
            raise ObsError("configuration_changed")
        host,port = signature[:2]
        endpoint(host,port)
        if self._signature != signature:
            self._close()
            self._client = self._factory(host=host,port=port,password_getter=self._password_for(signature))
            self._signature = signature
        if not self._client.connected:
            self._connection_epoch += 1
            self._client.connect()

    def _check_guard(self):
        if self._operation_guard is not None and not self._operation_guard():
            raise ObsError("disabled")

    def _request(self, kind, data=None):
        self._check_guard()
        self._ensure()
        self._check_guard()
        if kind == "StartStream" and self._live_binding != self.confirmation_identity():
            raise ObsError("confirmation_required")
        if self._operation_collection is None:
            self._operation_collection = (id(self._client),self._client.collection_generation)
        if self._operation_collection[0] != id(self._client):
            raise ObsError("busy")
        try:
            return self._client.request(kind,data,expected_collection=self._operation_collection[1])
        except ObsError as error:
            if not kind.startswith("Get") or error.key != "connection_failed":
                raise
            self._check_guard()
            if self.config.get("streaming.enabled",False) is not True:
                raise ObsError("disabled")
            if self._connection_settings()!=self._operation_connection:
                raise ObsError("configuration_changed")
            self._connection_epoch += 1
            self._client.connect()
            self._check_guard()
            # Recover this observation only. Old collection targets/live grants
            # remain invalid for any subsequent write in this operation.
            return self._client.request(kind,data)

    @staticmethod
    def _name(value):
        if not isinstance(value,str) or not value.strip() or len(value)>512 or any(ord(c)<32 for c in value):
            raise ObsError("invalid_argument")
        return value  # Quoted literal names retain case, punctuation and spaces.

    @staticmethod
    def _resolve(name, rows, key):
        if not isinstance(rows,list):
            raise ObsError("protocol_error")
        exact = [row for row in rows if isinstance(row,dict) and row.get(key)==name]
        found = exact or [row for row in rows if isinstance(row,dict) and str(row.get(key,"")).casefold()==name.casefold()]
        if not found:
            raise ObsError("not_found")
        if len(found)!=1:
            raise ObsError("ambiguous")
        return found[0]

    def _scene(self, value):
        data = self._request("GetSceneList")
        name = self._name(value or data.get("currentProgramSceneName",""))
        return self._resolve(name,data.get("scenes"),"sceneName")["sceneName"]

    def _input(self, value):
        name = self._name(value or self.config.get("streaming.microphone_input",""))
        return self._resolve(name,self._request("GetInputList").get("inputs"),"inputName")["inputName"]

    @staticmethod
    def _file_signature(raw):
        try:
            if not isinstance(raw,str) or raw.startswith(("\\\\","//")) or "\x00" in raw:
                return None
            path = Path(raw)
            if not path.is_absolute() or not path.is_file():
                return None
            path = path.resolve(strict=True)
            if str(path).startswith(("\\\\","//")):
                return None
            stat = path.stat()
            return (str(path),stat.st_size,stat.st_mtime_ns,stat.st_ino) if stat.st_size>0 else None
        except (OSError,ValueError):
            return None

    def _verify_file(self, getter, started_ns, before=None):
        deadline = time.monotonic()+self._file_timeout
        previous = None
        while time.monotonic()<deadline:
            signature = self._file_signature(getter())
            if signature and signature!=before and signature[2]>=started_ns-2_000_000_000:
                if signature==previous:
                    return signature[0]
                previous=signature
            else:
                previous=None
            time.sleep(.05)
        raise ObsError("file_unverified")

    def _output(self, action):
        query,field,desired,mutation = OUTPUTS[action]
        state = self._request(query)
        if action in {"record_pause","record_resume"} and not _flag(state,"outputActive"):
            raise ObsError("request_failed",code=501)
        if _flag(state,field) is desired:
            return self._result("already")
        started=time.time_ns()
        response=self._request(mutation)
        if action=="record_stop":
            return self._result("saved",path=self._verify_file(lambda:response.get("outputPath"),started))
        deadline=time.monotonic()+2.5
        while True:
            if _flag(self._request(query),field) is desired:
                return self._result("changed")
            if time.monotonic()>=deadline:
                raise ObsError("uncertain")
            time.sleep(.1)

    def _status(self):
        self._ensure()
        values={"connected":True,"recording":None,"recording_paused":None,"streaming":None,"replay_active":None,
                "microphone_muted":None,"scene":"","duration_seconds":None,"recording_duration_seconds":None,
                "dropped_frames":None,"total_frames":None,"reconnecting":None,"error":""}
        errors=[]
        for query in ("GetRecordStatus","GetStreamStatus","GetReplayBufferStatus","GetSceneList"):
            try:
                d=self._request(query)
                if query=="GetRecordStatus":
                    values.update(recording=_flag(d,"outputActive"),recording_paused=_flag(d,"outputPaused"),recording_duration_seconds=_number(d,"outputDuration")/1000)
                elif query=="GetStreamStatus":
                    values.update(streaming=_flag(d,"outputActive"),reconnecting=_flag(d,"outputReconnecting"),duration_seconds=_number(d,"outputDuration")/1000,
                                  dropped_frames=_number(d,"outputSkippedFrames"),total_frames=_number(d,"outputTotalFrames"))
                elif query=="GetReplayBufferStatus":
                    values["replay_active"]=_flag(d,"outputActive")
                else:
                    values["scene"]=str(d.get("currentProgramSceneName",""))
            except ObsError as error:
                errors.append(self._message(error.key,code=error.code))
                if not self._client or not self._client.connected:
                    break
        mic=self.config.get("streaming.microphone_input","")
        if mic and self._client and self._client.connected:
            try:
                values["microphone_muted"]=_flag(self._request("GetInputMute",{"inputName":mic}),"inputMuted")
            except ObsError as error:
                errors.append(self._message(error.key,code=error.code))
        values["connected"]=bool(self._client and self._client.connected)
        values["error"]="; ".join(dict.fromkeys(errors))
        self._last_error=values["error"]
        return values

    def status(self):
        with self._lock:
            self._operation_connection=self._connection_settings()
            try:
                return self._status()
            except ObsError as error:
                return {"connected":False,"recording":None,"streaming":None,"replay_active":None,"microphone_muted":None,
                        "scene":"","error":self._message(error.key,code=error.code)}
            finally:
                self._operation_connection=self._operation_collection=None

    def execute(self, arguments, *, confirmed_stream=False, expected_identity=None, guard=None):
        with self._lock:
            self._operation_guard=guard
            self._operation_connection=self._connection_settings()
            try:
                self._check_guard()
                if not isinstance(arguments,dict) or arguments.get("action") not in ACTIONS:
                    raise ObsError("invalid_argument")
                action=arguments["action"]
                if action=="stream_start" and (confirmed_stream is not True or expected_identity!=self.confirmation_identity()):
                    raise ObsError("confirmation_required")
                if action=="disconnect":
                    self.game_ended();self._close()
                    return self._result("disconnected")
                if action=="reconnect":
                    self._close()
                self._ensure()
                if action=="stream_start":
                    if expected_identity[-1] is True and expected_identity!=self.confirmation_identity():
                        raise ObsError("confirmation_required")
                    self._live_binding=self.confirmation_identity()
                if action in {"connect","reconnect"}:
                    return self._result("connected")
                if action in OUTPUTS:
                    return self._output(action)
                if action=="status":
                    d=self._status()
                    label=lambda v:self._message("active" if v is True else "inactive" if v is False else "unknown")
                    duration=d["duration_seconds"] if d["streaming"] else d["recording_duration_seconds"]
                    duration=self._message("unknown") if duration is None else f"{int(duration)//3600:02}:{int(duration)//60%60:02}:{int(duration)%60:02}"
                    message=self._message("status_summary",recording=self._message("paused") if d["recording_paused"] else label(d["recording"]),
                        streaming=label(d["streaming"]),replay=label(d["replay_active"]),duration=duration,
                        dropped=d["dropped_frames"] if d["dropped_frames"] is not None else self._message("unknown"),scene=d["scene"])
                    return {"ok":d["connected"] and not d["error"],"status":"status","message":message+("\n"+d["error"] if d["error"] else ""),**d}
                if action=="replay_save":
                    if not _flag(self._request("GetReplayBufferStatus"),"outputActive"):
                        raise ObsError("request_failed",code=501)
                    try:
                        previous=self._request("GetLastReplayBufferReplay").get("savedReplayPath")
                    except ObsError as error:
                        if error.key!="request_failed" or error.code!=600:
                            raise
                        previous=""
                    before=self._file_signature(previous);started=time.time_ns()
                    self._request("SaveReplayBuffer")
                    path=self._verify_file(lambda:self._request("GetLastReplayBufferReplay").get("savedReplayPath"),started,before)
                    return self._result("saved",path=path)
                if action=="scenes_list":
                    rows=self._request("GetSceneList").get("scenes")
                    if not isinstance(rows,list):
                        raise ObsError("protocol_error")
                    names=[str(r["sceneName"]) for r in rows if isinstance(r,dict) and isinstance(r.get("sceneName"),str)]
                    return self._result("scenes",names=", ".join(names),scenes=names)
                if action=="scene_switch":
                    scene=self._scene(self._name(arguments.get("scene") or self.config.get("streaming.preferred_scene","")))
                    self._request("SetCurrentProgramScene",{"sceneName":scene})
                    if self._request("GetSceneList").get("currentProgramSceneName")!=scene:
                        raise ObsError("uncertain")
                    return self._result("changed")
                if action in {"input_mute","input_unmute","input_volume"}:
                    name=self._input(arguments.get("input"))
                    if action=="input_volume":
                        value=arguments.get("volume")
                        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=100:
                            raise ObsError("invalid_argument")
                        self._request("SetInputVolume",{"inputName":name,"inputVolumeMul":value/100})
                        if abs(_number(self._request("GetInputVolume",{"inputName":name}),"inputVolumeMul")-value/100)>.001:
                            raise ObsError("uncertain")
                    else:
                        muted=action=="input_mute"
                        self._request("SetInputMute",{"inputName":name,"inputMuted":muted})
                        if _flag(self._request("GetInputMute",{"inputName":name}),"inputMuted") is not muted:
                            raise ObsError("uncertain")
                    return self._result("changed")
                if action in {"source_show","source_hide"}:
                    scene=self._scene(arguments.get("scene"));source=self._name(arguments.get("source"))
                    row=self._resolve(source,self._request("GetSceneItemList",{"sceneName":scene}).get("sceneItems"),"sourceName")
                    item=row.get("sceneItemId")
                    if type(item) is not int or item<0:
                        raise ObsError("protocol_error")
                    target={"sceneName":scene,"sceneItemId":item};enabled=action=="source_show"
                    self._request("SetSceneItemEnabled",{**target,"sceneItemEnabled":enabled})
                    if _flag(self._request("GetSceneItemEnabled",target),"sceneItemEnabled") is not enabled:
                        raise ObsError("uncertain")
                    return self._result("changed")
                raise ObsError("invalid_argument")
            except ObsError as error:
                # HIST-075: a terminal «busy» never shows the progress text «checking…».
                text_key = "busy_terminal" if error.key == "busy" else error.key
                self._last_error=self._message(text_key,code=error.code)
                return {"ok":False,"status":error.key,"message":self._last_error,"code":error.code}
            except Exception:
                self._last_error=self._message("connection_failed")
                return self._result("connection_failed",ok=False)
            finally:
                self._operation_guard=self._operation_connection=self._operation_collection=self._live_binding=None

    def game_started(self, session, notify=None):
        if (self.config.get("streaming.enabled",False) is not True or not
                (self.config.get("streaming.auto_launch_on_game",False) is True or self.config.get("streaming.auto_replay_on_game",False) is True)):
            return
        if self._game is None:
            from jarvis.integrations.obs_game184 import GameStart
            self._game=GameStart(self)
        self._game.started(session,notify)

    def game_ended(self):
        if self._game is not None:
            self._game.ended()


_SERVICES=weakref.WeakKeyDictionary()
_SERVICES_LOCK=threading.Lock()


def shared(config):
    with _SERVICES_LOCK:
        service=_SERVICES.get(config)
        if service is None:
            service=ObsService(config)
            service.config=weakref.proxy(config)
            _SERVICES[config]=service
        return service


get_obs_service=shared


def set_session_password(config,value):
    shared(config).set_session_password(value)
