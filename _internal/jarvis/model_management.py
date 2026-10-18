"""Explicit, cancellable local model maintenance. Never downloads at startup."""
from __future__ import annotations
import json
import inspect
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import DecodeError, ProtocolError, ReadTimeoutError, SSLError
from PySide6.QtCore import QObject, Signal, QCoreApplication, QThread, Qt

from jarvis.ollama_models import DEFAULT_CHAT_MODEL, local_endpoint, exact_model
from jarvis.runtime.diagnostics import redact

_MAX_PULL_RECORD_BYTES = 65536
# read1 returns available decoded metadata without waiting to fill this size.
_PULL_AVAILABLE_BYTES = 4096
# Older transports lack read1: a one-byte fallback avoids a fill-buffer wait.
_PULL_CHUNK_BYTES = 1
_PULL_TIMEOUT_SECONDS = 1800

class ModelOperation(QObject):
    progress = Signal(str, int)
    completed = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, action: str, model: str, base_url: str, *, auto_start: bool = False, parent=None):
        app = QCoreApplication.instance()
        owner = parent if parent is not None else (app if app is not None and QThread.currentThread() == app.thread() else None)
        super().__init__(owner)
        # The settings dialog can be destroyed immediately on close. Retain the
        # operation under the event loop until its bounded cancellation finishes,
        # then dispose of its QObject on the GUI thread, never the worker thread.
        if owner is not None:
            self.finished.connect(self.deleteLater, Qt.QueuedConnection)
            if app is not None:
                app.aboutToQuit.connect(self.cancel)
        self._target = (str(action), str(model).strip(), str(base_url).strip().rstrip("/"))
        self.auto_start = bool(auto_start)
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._response = None
        self._thread = None
        self._session = None
        self._mutation_started = False

    @property
    def action(self) -> str:
        return self._target[0]

    @property
    def model(self) -> str:
        return self._target[1]

    @property
    def base_url(self) -> str:
        return self._target[2]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="NEO-Model-" + self.action, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()
        # Do not block the GUI while urllib3 closes a socket being read. Read
        # timeout limits an idle socket read; active reads check cancellation
        # after each returned byte chunk before consuming further progress.

    def _emit(self, signal, *values) -> None:
        values = tuple(redact(value) if isinstance(value, str) else value for value in values)
        try:
            signal.emit(*values)
        except RuntimeError:
            pass  # owning dialog/application was destroyed during shutdown

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise InterruptedError("Операция с моделью отменена.")

    def _models(self, *, committed_delete: bool = False) -> list[str]:
        if not committed_delete:
            self._check_cancelled()
        with self._session.get(self.base_url + "/api/tags", timeout=(1.5, 4.0), allow_redirects=False) as response:
            self._validate_response(response)
            data = response.json()
        if not committed_delete:
            self._check_cancelled()
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError("Ollama вернула неверный список моделей.")
        return [str(row["name"]) for row in data["models"] if isinstance(row, dict) and row.get("name")]

    @staticmethod
    def _validate_response(response) -> None:
        if 300 <= response.status_code < 400:
            raise ValueError("Перенаправление локального сервера запрещено.")
        response.raise_for_status()

    def _run(self) -> None:
        committed_delete = False
        try:
            local_endpoint(self.base_url)
            if self.action not in {"list", "check", "download", "delete", "start"}:
                raise ValueError("Неизвестная операция с моделью.")
            if self.action not in {"list", "start"} and (not self.model or len(self.model) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", self.model)):
                raise ValueError("Введите корректное имя модели Ollama.")
            self._session = requests.Session()
            self._session.trust_env = False
            self._emit(self.progress, f"Ollama: {self.model or 'сервер'} — проверка…", -1)
            self._check_cancelled()
            try:
                models = self._models()
            except requests.RequestException:
                self._check_cancelled()
                if not (self.action == "start" or (self.action == "download" and self.auto_start)):
                    raise
                from jarvis.ollama_runtime import ensure_ollama_running
                self._emit(self.progress, "Запуск локальной службы Ollama…", -1)
                self._check_cancelled()
                status = ensure_ollama_running(self.base_url, timeout=12)
                self._check_cancelled()
                if not status.get("ready"):
                    raise RuntimeError("Ollama не запущена. Установите её с https://ollama.com/download или запустите установленную службу. Модель не скачивалась.")
                models = self._models()
            result = {"action": self.action, "model": self.model, "models": models, "ready": True, "status": "ready"}
            if self.action in {"list", "start"}:
                result["message"] = f"Ollama доступна. Установлено моделей: {len(models)}."
            elif self.action == "check":
                found = exact_model(self.model, models)
                result.update(ready=bool(found), status="installed" if found else "missing", message=f"Модель {self.model} установлена." if found else f"Модель {self.model} не установлена. Нажмите «Скачать».")
            elif self.action == "delete":
                self._check_cancelled()
                if not exact_model(self.model, models):
                    raise ValueError(f"Модель {self.model} не установлена; удалять нечего.")
                self._mutation_started = True
                with self._session.delete(self.base_url + "/api/delete", json={"model": self.model}, timeout=(1.5, 10), allow_redirects=False) as response:
                    self._validate_response(response)
                # Once delete is committed, always verify/report the outcome even
                # if Cancel arrived concurrently; never mislabel deletion cancelled.
                committed_delete = True
                models = self._models(committed_delete=True)
                if exact_model(self.model, models):
                    raise RuntimeError("Ollama не подтвердила удаление модели. Проверьте её состояние.")
                result.update(models=models, ready=False, status="deleted", message=f"Модель {self.model} удалена.")
            else:
                self._check_cancelled()
                if exact_model(self.model, models):
                    result.update(status="installed", message=f"Модель {self.model} уже установлена.")
                else:
                    self._pull()
                    self._check_cancelled()
                    models = self._models()
                    if not exact_model(self.model, models):
                        raise RuntimeError("Загрузка завершилась без установленной модели. Нажмите «Проверить» и повторите загрузку.")
                    result.update(models=models, status="installed", message=f"Модель {self.model} скачана и проверена.")
            if not committed_delete:
                self._check_cancelled()
            self._emit(self.completed, result)
        except InterruptedError as exc:
            self._emit(self.completed, {"action": self.action, "model": self.model, "ready": False, "status": "cancelled", "message": str(exc)})
        except requests.RequestException:
            if self._cancelled.is_set() and not (self.action == "delete" and self._mutation_started):
                self._emit(self.completed, {"action": self.action, "model": self.model, "ready": False, "status": "cancelled", "message": "Операция с моделью отменена. Если загрузка успела завершиться, её состояние можно проверить."})
            else:
                self._emit(self.failed, "Локальная служба Ollama не отвечает. Запустите Ollama и повторите проверку; если она не установлена: https://ollama.com/download. Результат операции не подтверждён.")
        except Exception as exc:
            if self._cancelled.is_set() and not (self.action == "delete" and self._mutation_started):
                self._emit(self.completed, {"action": self.action, "model": self.model, "ready": False, "status": "cancelled", "message": "Операция с моделью отменена."})
            else:
                self._emit(self.failed, str(exc))
        finally:
            if self._session is not None:
                self._session.close()
            self._session = None
            self._emit(self.finished)

    def _check_pull_read(self, deadline: float) -> None:
        self._check_cancelled()
        if time.monotonic() > deadline:
            raise TimeoutError("Истекло время загрузки (30 минут). Повторный запуск продолжит доступную загрузку.")

    def _pull_records(self, response, deadline: float):
        """Bound UTF-8 progress records before waiting for their newline.

        Existing connection/read timeouts remain in force. Between yielded
        chunks, cancellation and the absolute deadline are checked even
        if no complete JSON record has arrived. No extra worker or poll is used.
        """
        limit = _MAX_PULL_RECORD_BYTES

        def check() -> None:
            self._check_pull_read(deadline)

        def too_large() -> None:
            raise ValueError("Ollama вернула слишком длинную строку прогресса.")

        def invalid() -> None:
            raise ValueError("Неверный ответ загрузки Ollama.")

        def framed_adapter():
            # Compatibility for existing record-only adapters. Real requests
            # responses must use byte chunks and can never enter this branch.
            for line in response.iter_lines(chunk_size=_PULL_CHUNK_BYTES):
                check()
                if isinstance(line, str):
                    if len(line) > limit:
                        too_large()
                    try:
                        line = line.encode("utf-8")
                    except UnicodeError as exc:
                        raise ValueError("Неверный ответ загрузки Ollama.") from exc
                if not isinstance(line, bytes):
                    invalid()
                if len(line) > limit:
                    too_large()
                yield line
                yield b"\n"

        read_available = getattr(getattr(response, "raw", None), "read1", None)
        supports_available = False
        if (isinstance(response, requests.Response) and not response._content_consumed
                and callable(read_available)):
            try:
                # Plain file-like read1 methods need not support decompression.
                # Check before reading: never retry a possibly consumed read.
                inspect.signature(read_available).bind(_PULL_AVAILABLE_BYTES, decode_content=True)
                supports_available = True
            except (TypeError, ValueError):
                pass

        def available_chunks():
            while True:
                try:
                    chunk = read_available(_PULL_AVAILABLE_BYTES, decode_content=True)
                except ProtocolError as exc:
                    raise requests.exceptions.ChunkedEncodingError(exc) from exc
                except DecodeError as exc:
                    raise requests.exceptions.ContentDecodingError(exc) from exc
                except ReadTimeoutError as exc:
                    raise requests.exceptions.ConnectionError(exc) from exc
                except SSLError as exc:
                    raise requests.exceptions.SSLError(exc) from exc
                if chunk == b"":
                    response._content_consumed = True
                    return
                yield chunk

        read_chunks = getattr(response, "iter_content", None)
        if supports_available:
            chunks = available_chunks()
        elif callable(read_chunks):
            chunks = read_chunks(chunk_size=_PULL_CHUNK_BYTES, decode_unicode=False)
        elif not isinstance(response, requests.Response) and callable(getattr(response, "iter_lines", None)):
            chunks = framed_adapter()
        else:
            invalid()

        def reject_constant(_value):
            invalid()

        def parse(raw: bytearray):
            check()
            if not raw.strip(b" \t\r"):
                return None
            try:
                # Preserve the original byte-json loader's UTF-8 BOM support.
                value = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant)
            except (UnicodeError, ValueError, RecursionError) as exc:
                raise ValueError("Неверный ответ загрузки Ollama.") from exc
            if not isinstance(value, dict):
                invalid()
            return value

        pending = bytearray()
        iterator = iter(chunks)
        while True:
            check()
            try:
                chunk = next(iterator)
            except StopIteration:
                check()
                break
            except Exception:
                check()  # Cancellation/deadline wins over an interrupted read.
                raise
            check()
            if not isinstance(chunk, bytes):
                invalid()
            start = 0
            while start < len(chunk):
                check()
                end = chunk.find(b"\n", start)
                stop = len(chunk) if end < 0 else end
                size = len(pending) + stop - start
                last = chunk[stop - 1] if stop > start else (pending[-1] if pending else None)
                # Reserve at most one CR lookahead byte for a split CRLF.
                if size > limit + (1 if last == 13 else 0):
                    too_large()
                pending.extend(chunk[start:stop])
                if end < 0:
                    break
                if pending.endswith(b"\r"):
                    pending.pop()
                value = parse(pending)
                pending.clear()
                check()
                if value is not None:
                    yield value
                    check()
                start = end + 1
        if pending:
            if pending.endswith(b"\r"):
                pending.pop()
            value = parse(pending)
            check()
            if value is not None:
                yield value
        check()

    def _pull(self) -> None:
        self._check_cancelled()
        deadline = time.monotonic() + _PULL_TIMEOUT_SECONDS
        last_emit, previous = 0.0, None
        with self._session.post(self.base_url + "/api/pull", json={"model": self.model, "stream": True}, stream=True, timeout=(2, 5), allow_redirects=False) as response:
            self._validate_response(response)
            for data in self._pull_records(response, deadline):
                # REC-PRIVACY-SOURCE-DATA-01 R2: redact the full text first; a cut
                # token no longer matches the credential pattern.
                if data.get("error"):
                    raise RuntimeError(redact(str(data["error"]))[:500])
                total, completed = int(data.get("total") or 0), int(data.get("completed") or 0)
                percent = min(100, max(0, int(100 * completed / total))) if total > 0 else -1
                status = redact(str(data.get("status") or "Загрузка"))[:160]
                now = time.monotonic()
                current = (status, percent)
                if current != previous and (now - last_emit >= 0.2 or status == "success"):
                    self._emit(self.progress, f"{self.model}: {status}" + (f" ({percent}%)" if percent >= 0 else ""), percent)
                    previous, last_emit = current, now
