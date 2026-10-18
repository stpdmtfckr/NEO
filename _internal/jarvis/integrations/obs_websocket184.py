"""Authenticated local obs-websocket v5, with bounded I/O and no mutation retry.

Contract: obsproject/obs-websocket/docs/generated/protocol.md, RPC version 1.
The supported adapter is websockets 16.x sync.client. No server configuration,
stream-service credential retrieval, remote exposure or auth logging is used.
"""
from __future__ import annotations
import base64
import hashlib
import ipaddress
import json
import logging
import socket
import threading
import time
import uuid


class ObsError(Exception):
    def __init__(self, key, *, code=0):
        self.key, self.code = str(key), int(code)
        super().__init__(self.key)


def endpoint(host, port):
    host = str(host).strip().lower()
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ObsError("local_only") from None
    if not address.is_loopback:
        raise ObsError("local_only")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ObsError("invalid_argument")
    return f"ws://{'[' + host + ']' if address.version == 6 else host}:{port}"


def _connector(uri, timeout):
    try:
        from websockets.sync.client import connect
    except ImportError:
        raise ObsError("dependency_missing") from None
    logger = logging.Logger("neo.obs.wire")
    logger.disabled = True  # DEBUG frame logging would expose Identify auth.
    return connect(uri, open_timeout=timeout, close_timeout=1, max_size=1024*1024,
                   max_queue=16, compression=None, proxy=None, logger=logger)


class ObsClient:
    def __init__(self, host="127.0.0.1", port=4455, *, password_getter=None, connector=None, timeout=3):
        self.host, self.port = host, port
        self._password = password_getter or (lambda: "")
        self._connector = connector or _connector
        self.timeout = max(.1, min(float(timeout), 10))
        self._socket = None
        self._lock = threading.RLock()
        self.available_requests = frozenset()
        self.collection_changing = False
        self.collection_generation = 0

    @property
    def connected(self):
        return self._socket is not None and getattr(getattr(self._socket, "state", None), "name", "") not in {"CLOSING", "CLOSED"}

    @staticmethod
    def _abort(sock):
        raw = getattr(sock, "socket", None)
        if raw is None:
            abort = getattr(sock, "abort", None)  # Synthetic peers only.
            if callable(abort):
                abort()
            return
        try:
            raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            raw.close()
        except OSError:
            pass

    def close(self):
        with self._lock:
            sock, self._socket = self._socket, None
            self.available_requests = frozenset()
            self.collection_changing = False
            self.collection_generation += 1
            if sock is not None:
                try:
                    # Unblock sendall before close can acquire protocol_mutex.
                    self._abort(sock)
                    sock.close()
                except Exception:
                    pass

    @staticmethod
    def _decode(raw):
        if not isinstance(raw, str) or len(raw) > 1024*1024:
            raise ObsError("protocol_error")
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            raise ObsError("protocol_error") from None
        if not isinstance(message, dict) or not isinstance(message.get("d"), dict):
            raise ObsError("protocol_error")
        return message

    def _recv(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return self._decode(self._socket.recv(timeout=remaining))

    def _send(self, message, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock = self._socket
        expired = threading.Event()
        def expire():
            expired.set()
            # No WebSocket mutex: a blocked sendall may own that mutex.
            self._abort(sock)
        watchdog = threading.Timer(remaining, expire)
        watchdog.name = "NEO-OBS-SendDeadline"
        watchdog.daemon = True
        watchdog.start()
        try:
            sock.send(json.dumps(message, ensure_ascii=False))
            if expired.is_set():
                raise TimeoutError
        finally:
            watchdog.cancel()
            watchdog.join(timeout=.1)

    def _event(self, data):
        kind = data.get("eventType")
        if kind in {"CurrentSceneCollectionChanging", "CurrentSceneCollectionChanged"}:
            self.collection_generation += 1
            self.collection_changing = kind.endswith("Changing")
        elif kind == "ExitStarted":
            raise ObsError("connection_failed")

    def _drain(self):
        for _ in range(256):
            try:
                message = self._decode(self._socket.recv(timeout=0))
            except TimeoutError:
                return
            if message.get("op") == 5:
                self._event(message["d"])
            elif message.get("op") != 7:
                raise ObsError("protocol_error")
            # Unmatched old responses never acknowledge a later request.
        raise ObsError("busy")

    def connect(self):
        with self._lock:
            self.close()
            uri = endpoint(self.host, self.port)
            try:
                self._socket = self._connector(uri, self.timeout)
                deadline = time.monotonic() + self.timeout
                hello = self._recv(deadline)
                data = hello["d"]
                if hello.get("op") != 0 or type(data.get("rpcVersion")) is not int or data["rpcVersion"] < 1:
                    raise ObsError("protocol_error")
                auth = data.get("authentication")
                if not isinstance(auth, dict):
                    raise ObsError("auth_required")
                if any(not isinstance(auth.get(k), str) or not auth[k] or len(auth[k]) > 1024 for k in ("salt", "challenge")):
                    raise ObsError("protocol_error")
                password = self._password()
                if not isinstance(password, str) or not password:
                    raise ObsError("credential_missing")
                secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
                response = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
                self._send({"op":1,"d":{"rpcVersion":1,"authentication":response,"eventSubscriptions":66}}, deadline)
                del password, secret, response
                identified = self._recv(deadline)
                if identified.get("op") != 2 or identified["d"].get("negotiatedRpcVersion") != 1:
                    raise ObsError("protocol_error")
                version = self._request("GetVersion", {})
                supported = version.get("availableRequests")
                if not isinstance(supported, list) or any(not isinstance(x,str) for x in supported):
                    raise ObsError("protocol_error")
                self.available_requests = frozenset(supported)
            except ObsError:
                self.close()
                raise
            except Exception:
                self.close()
                raise ObsError("connection_failed") from None

    def request(self, kind, data=None, *, expected_collection=None):
        with self._lock:
            if not self.connected:
                raise ObsError("connection_failed")
            try:
                self._drain()
            except ObsError as error:
                if error.key != "busy":
                    self.close()
                raise
            except Exception:
                self.close()
                raise ObsError("connection_failed") from None
            if self.collection_changing or (expected_collection is not None and expected_collection != self.collection_generation):
                raise ObsError("busy")
            if kind not in self.available_requests:
                raise ObsError("unsupported")
            return self._request(kind, data or {})

    def _request(self, kind, data):
        identity = uuid.uuid4().hex
        sent = False
        try:
            deadline = time.monotonic() + self.timeout
            sent = True
            self._send({"op":6,"d":{"requestType":kind,"requestId":identity,"requestData":data}},deadline)
            for _ in range(256):
                message = self._recv(deadline)
                d = message["d"]
                if message.get("op") == 5:
                    self._event(d)
                    continue
                if message.get("op") != 7:
                    raise ObsError("protocol_error")
                if d.get("requestId") != identity:
                    continue
                status = d.get("requestStatus")
                if (d.get("requestType") != kind or not isinstance(status,dict)
                        or type(status.get("result")) is not bool or type(status.get("code")) is not int):
                    raise ObsError("protocol_error")
                if status["result"] is False and status["code"] != 100:
                    raise ObsError("request_failed",code=status["code"])     # definite correlated rejection
                if not (status["result"] is True and status["code"] == 100):
                    raise ObsError("protocol_error")                         # contradictory True/500 or False/100
                response = d.get("responseData",{})
                if not isinstance(response,dict):
                    raise ObsError("protocol_error")
                return response
            raise ObsError("protocol_error")
        except ObsError as error:
            if error.key not in {"request_failed","busy","unsupported"}:
                self.close()
            if sent and not kind.startswith("Get") and error.key in {"protocol_error","connection_failed"}:
                # NEO 187: after a state-changing command was attempted, a broken
                # reply means "outcome unknown — check OBS before repeating".
                raise ObsError("uncertain") from None
            raise
        except Exception:
            self.close()
            raise ObsError("uncertain" if sent and not kind.startswith("Get") else "connection_failed") from None
