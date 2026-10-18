# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from jarvis.config import ConfigManager
from jarvis.ollama_runtime import ensure_ollama_running, is_local_ollama_url
from jarvis.runtime.cancellation import OperationCancelled, cancellation_manager


logger = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class IncompleteModelOutput(OllamaError):
    """NEO 188 NEO-TOOL-001: the model output ended without its terminal signal (or was cut
    by the token limit). Nothing from it may be executed; the model itself is not «unavailable»."""


def _strict_object_pairs188(pairs):
    """json object hook: a name that occurs twice (also via an escaped spelling) is ambiguous."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _strict_json188(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_strict_object_pairs188)


@dataclass(frozen=True)
class _RequestState:
    role: str
    base_url: str
    selection: tuple
    cancel_epoch: int
    settings_revision: int



# HOTFIX103: desktop voice/text and Telegram Remote use separate Agent/OllamaClient
# instances. Ollama may answer one local request reliably but return HTTP 500 or
# stall when two heavy generations arrive at the same instant. Keep deterministic
# tool commands parallel, but serialize only model generation across all clients in
# this process. The small state counters let transports show an explicit queue state
# instead of looking frozen.
_GENERATION_LOCK = threading.Lock()
_GENERATION_STATE_LOCK = threading.Lock()
_GENERATION_ACTIVE = 0
_GENERATION_WAITING = 0

# Bound only the pending NDJSON record, not the requested complete answer.
_MAX_STREAM_RECORD_BYTES = 1024 * 1024
_STREAM_READ_CHUNK_BYTES = 64

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.I | re.S)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>.*$", re.I | re.S)
_THINK_TAG_RE = re.compile(r"</?think\s*>", re.I)


def _acquire_generation_slot() -> None:
    global _GENERATION_ACTIVE, _GENERATION_WAITING
    with _GENERATION_STATE_LOCK:
        _GENERATION_WAITING += 1
    acquired = False
    try:
        while not acquired:
            cancellation_manager().raise_if_cancelled()
            acquired = _GENERATION_LOCK.acquire(timeout=0.15)
        with _GENERATION_STATE_LOCK:
            _GENERATION_ACTIVE += 1
    finally:
        with _GENERATION_STATE_LOCK:
            _GENERATION_WAITING = max(0, _GENERATION_WAITING - 1)


def _release_generation_slot() -> None:
    global _GENERATION_ACTIVE
    with _GENERATION_STATE_LOCK:
        _GENERATION_ACTIVE = max(0, _GENERATION_ACTIVE - 1)
    _GENERATION_LOCK.release()


def _generation_busy() -> bool:
    with _GENERATION_STATE_LOCK:
        return bool(_GENERATION_ACTIVE or _GENERATION_WAITING)


def _strip_thinking_markup(text: str) -> str:
    """Remove model-internal <think> blocks if a model leaks them into content."""

    value = str(text or "")
    value = _THINK_BLOCK_RE.sub("", value)
    value = _THINK_OPEN_RE.sub("", value)
    value = _THINK_TAG_RE.sub("", value)
    return value.strip()


def _safe_stream_visible(raw: str) -> str:
    visible = _strip_thinking_markup(raw)
    # Hold an incomplete opening tag so '<th' never flashes in the chat bubble.
    for prefix in ("<", "<t", "<th", "<thi", "<thin", "<think"):
        if visible.casefold().endswith(prefix):
            visible = visible[: -len(prefix)]
            break
    return visible


class ReplyStream:
    """NEO 172: the text of ``{"reply": "…"}`` (or ``"ask"``) while the JSON is still being generated.

    Feed raw JSON pieces; each call returns the newly decoded characters of the reply
    value. Nothing is returned for ``{"do": […]}``: commands are never shown half-made.
    """

    _KEY = re.compile(r'^\s*\{\s*"(reply|ask)"\s*:\s*"')

    def __init__(self) -> None:
        self.raw = ""
        self.position = -1        # index in raw where the value's characters start; -2 = not a reply
        self.done = False
        self.emitted = ""

    def feed(self, chunk: str) -> str:
        if self.done or self.position == -2:
            return ""
        self.raw += chunk
        if self.position == -1:
            match = self._KEY.match(self.raw)
            if match is None:
                key = re.match(r'^\s*\{\s*"([^"]*)"', self.raw)
                if (key is not None and key.group(1) not in {"reply", "ask"}) or len(self.raw) > 40:
                    self.position = -2
                return ""
            self.position = match.end()
        out: list[str] = []
        i = self.position
        while i < len(self.raw):
            ch = self.raw[i]
            if ch == '"':
                self.done = True
                i += 1
                break
            if ch == "\\":
                if i + 1 >= len(self.raw):
                    break                       # wait for the rest of the escape
                code = self.raw[i + 1]
                if code == "u":
                    if i + 6 > len(self.raw):
                        break
                    try:
                        value = int(self.raw[i + 2:i + 6], 16)
                    except ValueError:
                        i += 6
                        continue
                    if 0xD800 <= value <= 0xDBFF:
                        # A character outside the BMP (emoji) comes as two escapes: wait for the pair.
                        if i + 12 > len(self.raw):
                            break
                        low = self.raw[i + 6:i + 12]
                        if low.startswith("\\u"):
                            try:
                                second = int(low[2:], 16)
                            except ValueError:
                                second = 0
                            if 0xDC00 <= second <= 0xDFFF:
                                out.append(chr(0x10000 + ((value - 0xD800) << 10) + (second - 0xDC00)))
                                i += 12
                                continue
                        i += 6
                        continue
                    out.append(chr(value))
                    i += 6
                    continue
                out.append({"n": "\n", "t": "\t", "r": "", "b": "", "f": "", "/": "/"}.get(code, code))
                i += 2
                continue
            out.append(ch)
            i += 1
        self.position = i
        text = "".join(out)
        if not self.emitted:
            text = text.lstrip()
        self.emitted += text
        return text


class OllamaClient:
    def __init__(self, config: ConfigManager):
        self.config = config
        self._session = requests.Session()
        self._session.trust_env = False  # Local images must never use an ambient HTTP proxy.
        self._session.max_redirects = 0  # Never resend local text/images to a redirect target.
        self._active_lock = threading.RLock()
        self._model_cache_value: tuple[float, list[str]] = (0.0, [])
        self._model_cache_generation = 0
        self._model_cache_query_sequence = 0
        self._model_cache_published_sequence = 0
        self._model_cache_endpoint = ""
        self._active_streams: dict[int, requests.Response] = {}
        self._cancel_epoch = 0
        self._last_game_priority_guard = 0.0
        self._cancel_cleanup_thread = None
        self._cancel_pending_responses: dict[int, requests.Response] = {}
        self._model_settings_revisions = {role: 0 for role in ("inventory", "chat", "agent", "vision")}
        self._observed_model_settings = {role: (self.base_url, self._request_selection(role))
                                         for role in self._model_settings_revisions}

    def cancel_active(self) -> int:
        """Best-effort immediate cancellation of in-flight streaming generations.

        The UI previously marked a worker as stale but left Ollama generating in
        the background.  On a local model that can keep CPU/GPU saturated long
        after the user said «стоп».  Closing the HTTP stream causes Ollama to abort
        the disconnected request, while the cancellation generation below lets the
        worker distinguish a deliberate stop from a network failure.
        """

        with self._active_lock:
            self._cancel_epoch += 1
            responses = list(self._active_streams.values())
            # A cancelled streamed response can leave urllib3's pooled connection
            # in a half-closed state on Windows. Give the next user request a
            # fresh HTTP pool immediately instead of inheriting multi-second
            # stalls from the cancelled generation.
            old_session = getattr(self, "_session", None)
            self._session = requests.Session()
            self._session.trust_env = False
            self._session.max_redirects = 0
        if responses:
            with self._active_lock:
                self._cancel_pending_responses.update((id(response), response) for response in responses)
                cleanup = self._cancel_cleanup_thread
                if cleanup is None or not cleanup.is_alive():
                    def close_responses() -> None:
                        while True:
                            with self._active_lock:
                                if not self._cancel_pending_responses:
                                    self._cancel_cleanup_thread = None
                                    return
                                batch = list(self._cancel_pending_responses.values())
                                self._cancel_pending_responses.clear()
                            for response in batch:
                                try:
                                    response.close()
                                except Exception:
                                    pass
                    cleanup = threading.Thread(target=close_responses, name="NEO-Ollama-Cancel", daemon=True)
                    self._cancel_cleanup_thread = cleanup
                    cleanup.start()
        try:
            old_session.close()
        except Exception:
            pass
        return len(responses)

    def _request_selection(self, role: str) -> tuple:
        if role == "chat":
            return (bool(self.config.get("local_chat.enabled", True)),
                    str(self.config.get("local_chat.model", "") or self.config.get("ollama.chat_model", "qwen2.5:0.5b") or "qwen2.5:0.5b").strip())
        if role == "agent":
            return (bool(self.config.get("llm_agent.enabled", True)),
                    tuple(self.agent_model_candidates()),
                    str(self.config.get("llm_agent.size", "") or ""),
                    self._agent_provenance_selection())
        if role == "vision":
            return (bool(self.config.get("ollama.vision_enabled", True)),
                    str(self.config.get("ollama.vision_model", "qwen3.5:4b") or "qwen3.5:4b").strip())
        return ()

    @property
    def _model_cache(self) -> tuple[float, list[str]]:
        return self._model_cache_value

    @_model_cache.setter
    def _model_cache(self, value: tuple[float, list[str]]) -> None:
        # Existing UI/integration reset assignments must invalidate older reads,
        # even when the server URL is unchanged or the cache was already empty.
        with self._active_lock:
            self._model_cache_generation += 1
            self._model_cache_value = value

    def refresh_model_settings(self) -> bool:
        """Observe applied model settings, retaining every relevant transition.

        Agent.refresh_runtime_settings calls this on each settings application.
        Per-role revisions catch A -> B -> A even when no token or queued request
        runs between the two saves. Theme and other unrelated saves do not bump
        revisions or cancel active generation.
        """
        with self._active_lock:
            changed = False
            base_url = self.base_url
            for role in self._model_settings_revisions:
                current = (base_url, self._request_selection(role))
                if current != self._observed_model_settings[role]:
                    self._observed_model_settings[role] = current
                    self._model_settings_revisions[role] += 1
                    changed = True
            if changed:
                self._model_cache = (0.0, [])
            return changed

    def reserve_model_inventory(self) -> int:
        """Reserve ordering when a model operation starts, not when it ends."""
        with self._active_lock:
            self._model_cache_query_sequence += 1
            return self._model_cache_query_sequence

    def accept_model_inventory(self, base_url: str, models: list[str] | None, *,
                               sequence: int | None = None, invalidate: bool = False) -> bool:
        """Publish only inventory still owned by this operation's start order.

        An old read-only result cannot replace a newer read/operation. An old
        mutation may have committed, so invalidate conservatively and require a
        fresh read rather than publishing its potentially stale verification.
        """
        with self._active_lock:
            self.refresh_model_settings()
            if str(base_url).rstrip("/") != self.base_url:
                return False
            owned = type(sequence) is int and 0 < sequence <= self._model_cache_query_sequence
            if not owned or sequence < self._model_cache_published_sequence:
                if invalidate:
                    self._model_cache = (0.0, [])
                return False
            if invalidate:
                self._model_cache = (0.0, [])
                self._model_cache_published_sequence = sequence
            if models is not None:
                self._model_cache_published_sequence = sequence
                self._model_cache_value = (time.monotonic(), [str(model) for model in models])
                self._model_cache_endpoint = self.base_url
            return True

    def _capture_request(self, role: str) -> _RequestState:
        with self._active_lock:
            self.refresh_model_settings()
            base_url, selection = self._observed_model_settings[role]
            return _RequestState(role, base_url, selection, self._cancel_epoch,
                                 self._model_settings_revisions[role])

    def _request_message(self, kind: str, model: str = "", **values: Any) -> str:
        from jarvis.language import language_code
        messages = {
            "changed": {
                "ru": "Настройки локальной модели изменились; повторите запрос.",
                "en": "Local model settings changed; send the request again.",
                "de": "Die lokalen Modelleinstellungen wurden geändert. Senden Sie die Anfrage erneut.",
                "fr": "Les réglages du modèle local ont changé. Renvoyez la demande.",
                "es": "La configuración del modelo local cambió. Envía la solicitud de nuevo.",
                "it": "Le impostazioni del modello locale sono cambiate. Invia nuovamente la richiesta.",
                "pt": "As configurações do modelo local mudaram. Envie o pedido novamente.",
            },
            "stream_too_large": {
                "ru": "Запись потока Ollama превышает предел {limit} байт.",
                "en": "An Ollama stream record exceeds the {limit}-byte limit.",
                "de": "Ein Ollama-Stream-Datensatz überschreitet das Limit von {limit} Bytes.",
                "fr": "Un enregistrement du flux Ollama dépasse la limite de {limit} octets.",
                "es": "Un registro del flujo de Ollama supera el límite de {limit} bytes.",
                "it": "Un record del flusso Ollama supera il limite di {limit} byte.",
                "pt": "Um registro do fluxo Ollama excede o limite de {limit} bytes.",
            },
            "stream_invalid": {
                "ru": "Ollama вернула неверную запись потока UTF-8/JSON.",
                "en": "Ollama returned an invalid UTF-8/JSON stream record.",
                "de": "Ollama hat einen ungültigen UTF-8/JSON-Stream-Datensatz zurückgegeben.",
                "fr": "Ollama a renvoyé un enregistrement UTF-8/JSON non valide.",
                "es": "Ollama devolvió un registro de flujo UTF-8/JSON no válido.",
                "it": "Ollama ha restituito un record di flusso UTF-8/JSON non valido.",
                "pt": "Ollama retornou um registro de fluxo UTF-8/JSON inválido.",
            },
            "stream_truncated": {
                "ru": "Поток Ollama завершился с неверной или неполной записью UTF-8/JSON.",
                "en": "The Ollama stream ended with an invalid or incomplete UTF-8/JSON record.",
                "de": "Der Ollama-Stream endete mit einem ungültigen oder unvollständigen UTF-8/JSON-Datensatz.",
                "fr": "Le flux Ollama s’est terminé avec un enregistrement UTF-8/JSON non valide ou incomplet.",
                "es": "El flujo de Ollama terminó con un registro UTF-8/JSON no válido o incompleto.",
                "it": "Il flusso Ollama è terminato con un record UTF-8/JSON non valido o incompleto.",
                "pt": "O fluxo Ollama terminou com um registro UTF-8/JSON inválido ou incompleto.",
            },
            "incomplete": {
                "ru": "Ответ модели ассистента оборвался до конца — ничего не выполняю.",
                "en": "The assistant model's answer ended before it was complete, so I am not running anything.",
                "de": "Die Antwort des Assistenzmodells brach vor dem Ende ab – ich führe nichts aus.",
                "fr": "La réponse du modèle d’assistant s’est interrompue avant la fin ; je n’exécute rien.",
                "es": "La respuesta del modelo del asistente se cortó antes de terminar; no ejecuto nada.",
                "it": "La risposta del modello dell’assistente si è interrotta prima della fine: non eseguo nulla.",
                "pt": "A resposta do modelo do assistente terminou antes do fim; não executo nada.",
            },
            "ambiguous": {
                "ru": "Ответ модели ассистента неоднозначен (повторяющиеся поля) — ничего не выполняю.",
                "en": "The assistant model's answer is ambiguous (repeated fields), so I am not running anything.",
                "de": "Die Antwort des Assistenzmodells ist mehrdeutig (doppelte Felder) – ich führe nichts aus.",
                "fr": "La réponse du modèle d’assistant est ambiguë (champs répétés) ; je n’exécute rien.",
                "es": "La respuesta del modelo del asistente es ambigua (campos repetidos); no ejecuto nada.",
                "it": "La risposta del modello dell’assistente è ambigua (campi ripetuti): non eseguo nulla.",
                "pt": "A resposta do modelo do assistente é ambígua (campos repetidos); não executo nada.",
            },
            "missing": {
                "ru": "Модель ассистента «{model}» не установлена. Проверьте модель в настройках.",
                "en": "Assistant model {model} is not installed. Check the model in settings.",
                "de": "Das Assistenzmodell {model} ist nicht installiert. Prüfen Sie das Modell in den Einstellungen.",
                "fr": "Le modèle d’assistant {model} n’est pas installé. Vérifiez le modèle dans les réglages.",
                "es": "El modelo del asistente {model} no está instalado. Comprueba el modelo en la configuración.",
                "it": "Il modello dell’assistente {model} non è installato. Controlla il modello nelle impostazioni.",
                "pt": "O modelo do assistente {model} não está instalado. Verifique o modelo nas configurações.",
            },
        }
        choices = messages[kind]
        return choices.get(language_code(self.config), choices["en"]).format(model=model, **values)

    def _check_request(self, request: _RequestState) -> None:
        # A changed endpoint/model/role invalidates queued work and late output.
        # Treat it as cancellation, never a failed server (no conversation cooldown).
        cancellation_manager().raise_if_cancelled()
        with self._active_lock:
            self.refresh_model_settings()
            if request.cancel_epoch != self._cancel_epoch:
                raise OperationCancelled("Генерация остановлена пользователем")
            if request.settings_revision != self._model_settings_revisions[request.role]:
                raise OperationCancelled(self._request_message("changed"))

    def _resolve_for_request(self, request: _RequestState, resolver, *args, **kwargs):
        self._check_request(request)
        try:
            result = resolver(*args, **kwargs)
        except Exception:
            self._check_request(request)
            raise
        self._check_request(request)
        return result

    def _requests_enabled(self) -> bool:
        # Only explicitly requested screen/image analysis is an allowed consumer.
        # Legacy requests_enabled must never re-enable conversational fallback.
        return bool(self.config.get("ollama.vision_enabled", True))

    def _require_requests_enabled(self) -> None:
        if not self._requests_enabled():
            raise OllamaError("Анализ изображений Ollama отключён в настройках.")

    def _require_image_request(self, messages: list[dict[str, Any]], tools: Any = None) -> None:
        if tools or not any(isinstance(m, dict) and m.get("role") == "user" and
                            isinstance(m.get("images"), list) and
                            any(isinstance(x, str) and bool(x.strip()) for x in m["images"])
                            for m in messages):
            raise OllamaError("Ollama используется только для изображений. Текстовые вопросы обрабатывает SearchAPI.io; команды — инструменты NEO.")
        self._require_requests_enabled()
        if not is_local_ollama_url(self.base_url):
            raise OllamaError("Скриншоты можно анализировать только локальным сервером Ollama (localhost).")

    @property
    def base_url(self) -> str:
        return str(self.config.get("ollama.base_url", "http://localhost:11434")).rstrip("/")

    @staticmethod
    def generation_busy() -> bool:
        return _generation_busy()

    def health(self) -> tuple[bool, str]:
        if not self._requests_enabled() and not self.local_chat_enabled():
            return False, "Локальные модели отключены."
        from jarvis.ollama_status import readiness
        result = readiness(self.base_url, retries=1)
        return bool(result["ready"]), str(result["message"])

    def list_models(self, *, force: bool = False) -> list[str]:
        self._require_requests_enabled()
        return self._list_models(force=force)

    def _list_models(self, *, force: bool = False) -> list[str]:
        request = self._capture_request("inventory")
        self._check_request(request)
        with self._active_lock:
            cached_at, cached = self._model_cache
            if (not force and self._model_cache_endpoint == request.base_url
                    and cached_at and time.monotonic() - cached_at < 15.0):
                return list(cached)
            cache_generation = self._model_cache_generation
            self._model_cache_query_sequence += 1
            query_sequence = self._model_cache_query_sequence
        try:
            response = self._session.get(f"{request.base_url}/api/tags", timeout=5)
            response.raise_for_status()
        except requests.RequestException as first_exc:
            self._check_request(request)
            if not bool(self.config.get("ollama.auto_start", True)):
                raise
            status = ensure_ollama_running(
                request.base_url,
                timeout=float(self.config.get("ollama.startup_timeout_seconds", 20) or 20),
            )
            self._check_request(request)
            if not status.get("ready"):
                reason = str(status.get("reason", "unknown") or "unknown")
                raise OllamaError(f"Ollama не запущена и автозапуск не удался ({reason}).") from first_exc
            response = self._session.get(f"{request.base_url}/api/tags", timeout=5)
            response.raise_for_status()
        models = [str(item.get("name", "")) for item in response.json().get("models", []) if item.get("name")]
        self._check_request(request)
        with self._active_lock:
            if cache_generation != self._model_cache_generation:
                raise OperationCancelled(self._request_message("changed"))
            if query_sequence < self._model_cache_published_sequence:
                # Another harmless read may finish first. Reuse its newer
                # inventory rather than cancel a question or overwrite it.
                return list(self._model_cache_value[1])
            self._model_cache_value = (time.monotonic(), models)
            self._model_cache_endpoint = request.base_url
            self._model_cache_published_sequence = query_sequence
        return list(models)

    def resolve_model(self, requested: str | None = None, *, require_vision: bool = False) -> str:
        if not require_vision:
            from jarvis.ollama_models import DEFAULT_CHAT_MODEL, exact_model
            wanted = str(requested or self.config.get("ollama.chat_model", DEFAULT_CHAT_MODEL) or DEFAULT_CHAT_MODEL).strip()
            found = exact_model(wanted, self.list_models())
            if found:
                return found
            raise OllamaError(self._request_message("missing", wanted))
        configured = self.config.get("ollama.vision_model" if require_vision else "ollama.chat_model", "qwen3.5:4b")
        value = str(requested or configured or "qwen3.5:4b").strip()
        models = self.list_models()
        if not models:
            raise OllamaError("В Ollama не установлена ни одна модель.")
        if value in models:
            return value
        base = value.split(":", 1)[0].casefold()
        for model in models:
            if model.split(":", 1)[0].casefold() == base:
                return model

        preferred = (
            ["qwen3.5:4b", "qwen3.5", "qwen3-vl:4b", "qwen3-vl", "ministral-3:3b", "ministral-3"]
            if require_vision
            else ["qwen3.5:4b", "qwen3.5", "qwen3-vl:4b", "qwen3-vl", "qwen3:4b", "ministral-3:3b", "ministral-3"]
        )
        lowered = {model.casefold(): model for model in models}
        for candidate in preferred:
            if candidate.casefold() in lowered:
                return lowered[candidate.casefold()]
            candidate_base = candidate.split(":", 1)[0].casefold()
            for model in models:
                if model.split(":", 1)[0].casefold() == candidate_base:
                    return model
        raise OllamaError(
            f"Модель «{value}» не найдена. Установлены: {', '.join(models[:8])}. "
            "Выберите мультимодальную модель в настройках."
        )

    def _game_mode_active(self) -> bool:
        try:
            if not bool(self.config.get("ollama.yield_to_active_game", True)):
                return False
            from jarvis.gaming.runtime import active_game_context
            return bool(active_game_context(max_age_seconds=60.0))
        except Exception:
            return False

    def _apply_active_game_priority_guard(self, *, always: bool = False) -> None:
        """Lower Ollama's Windows priority while a verified game is active.

        This is a best-effort resource guard, never a correctness dependency.
        The scan is rate-limited so a local chat turn does not repeatedly walk
        the process table. Child runners normally inherit the server priority.
        NEO 168: screen analysis also runs below normal priority (``always``),
        so the desktop, a game or a video stay smooth while the model works.
        """
        if os.name != "nt" or not (always or self._game_mode_active()):
            return
        now = time.monotonic()
        if now - self._last_game_priority_guard < 10.0:
            return
        self._last_game_priority_guard = now
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                name = str((proc.info or {}).get("name") or "").casefold()
                if "ollama" not in name:
                    continue
                try:
                    proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    continue
        except Exception:
            return

    def _runner_resource_options(self) -> dict[str, int]:
        """Resource caps shared by generate/chat/warmup requests.

        Ollama supports num_thread/num_batch/num_gpu as runner options.  Keep GPU
        placement automatic by default (-1 => omitted), but cap CPU worker threads
        and batch memory so one assistant turn does not monopolize the machine.
        """
        try:
            threads = max(1, min(16, int(self.config.get("ollama.num_thread", 4) or 4)))
        except Exception:
            threads = 4
        try:
            batch = max(16, min(512, int(self.config.get("ollama.num_batch", 128) or 128)))
        except Exception:
            batch = 128
        if self._game_mode_active():
            try:
                threads = min(threads, max(1, int(self.config.get("ollama.active_game_num_thread", 2) or 2)))
            except Exception:
                threads = min(threads, 2)
            try:
                batch = min(batch, max(16, int(self.config.get("ollama.active_game_num_batch", 64) or 64)))
            except Exception:
                batch = min(batch, 64)
            self._apply_active_game_priority_guard()
        options: dict[str, int] = {"num_thread": threads, "num_batch": batch}
        try:
            gpu = int(self.config.get("ollama.num_gpu", -1))
        except Exception:
            gpu = -1
        if gpu >= 0:
            options["num_gpu"] = gpu
        return options

    # ------------------------------------------------------ NEO 169: local chat
    def local_chat_enabled(self) -> bool:
        return bool(self.config.get("local_chat.enabled", True)) and is_local_ollama_url(self.base_url)

    def model_capabilities(self, model: str) -> list[str]:
        """Read capabilities from the same server that owns this cache entry."""
        request = self._capture_request("inventory")
        self._check_request(request)
        key = (request.base_url, model)
        cache = getattr(self, "_capabilities_cache", {})
        if key in cache:
            return list(cache[key])
        try:
            response = self._session.post(f"{request.base_url}/api/show", json={"model": model}, timeout=5)
            response.raise_for_status()
            values = [str(x) for x in (response.json().get("capabilities") or [])]
        except (requests.RequestException, ValueError):
            self._check_request(request)
            return []
        self._check_request(request)
        cache[key] = values
        while len(cache) > 32:
            cache.pop(next(iter(cache)))
        self._capabilities_cache = cache
        return list(values)

    def _agent_provenance_selection(self) -> tuple:
        proofs = self.config.get("llm_agent.fine_tuned_provenance", {})
        if not isinstance(proofs, dict):
            return ()
        result = []
        for name in self.agent_model_candidates():
            key = str(name).removesuffix(":latest")
            proof = proofs.get(key)
            if isinstance(proof, dict):
                result.append((key, *(str(proof.get(field, "")) for field in
                    ("hf_base", "ollama_base", "model_digest", "base_url", "run_key"))))
        return tuple(result)

    def _trained_alias_matches_base(self, model: str, base: str, request: _RequestState) -> bool:
        """Bind a promoted local training result to its base, server and current digest."""
        from jarvis.runtime.settings170 import SIZES
        from jarvis.ollama_models import exact_model
        proofs = self.config.get("llm_agent.fine_tuned_provenance", {})
        proof = proofs.get(model.removesuffix(":latest")) if isinstance(proofs, dict) else None
        expected_hf = next((row["hf"] for row in SIZES.values() if row["ollama"] == base), "")
        if (not isinstance(proof, dict) or not expected_hf or proof.get("hf_base") != expected_hf
                or proof.get("ollama_base") != base or proof.get("base_url") != request.base_url
                or not isinstance(proof.get("run_key"), str) or not proof["run_key"].strip()):
            return False
        digest = str(proof.get("model_digest", ""))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            return False
        self._check_request(request)
        with self._session.get(f"{request.base_url}/api/tags", timeout=5) as response:
            response.raise_for_status()
            data = response.json()
        self._check_request(request)
        rows = data.get("models", []) if isinstance(data, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and exact_model(model, [str(row.get("name", ""))]):
                actual = str(row.get("digest", "")).lower().removeprefix("sha256:")
                return bool(re.fullmatch(r"[0-9a-f]{64}", actual) and digest == "sha256:" + actual)
        return False

    def _agent_alias_matches_base(self, model: str, base: str, request: _RequestState) -> bool:
        """Only reuse an alias whose FROM points to the selected base weights.

        Named FROM references and equal Ollama blob identities are accepted. If
        the server omits provenance, use the installed exact base, without pulls
        or silently trusting a stale neo-agent/fine-tuned alias.
        """
        def metadata(name: str) -> dict:
            self._check_request(request)
            response = self._session.post(f"{request.base_url}/api/show", json={"model": name}, timeout=5)
            response.raise_for_status()
            data = response.json()
            self._check_request(request)
            return data if isinstance(data, dict) else {}

        def source(data: dict) -> str:
            match = re.search(r"^FROM\s+([^\r\n]+)", str(data.get("modelfile", "")), re.M | re.I)
            return match.group(1).strip().strip('"') if match else ""
        try:
            if self._trained_alias_matches_base(model, base, request):
                return True
            if self._managed_agent_alias(model) and model.casefold().removesuffix(":latest") != "neo-agent":
                # A prompt-only FROM wrapper is not evidence of trained weights.
                # NEO-owned trained names require the promoted digest contract.
                return False
            alias_data = metadata(model)
            origin = source(alias_data)
            from jarvis.ollama_models import exact_model
            if origin and exact_model(base, [origin]):
                return True
            base_data = metadata(base)
            base_origin = source(base_data)
            digest = re.compile(r"(?:^|[/\\])sha256[-:]([0-9a-f]{64})$", re.I)
            alias_digest, base_digest = digest.search(origin), digest.search(base_origin)
            if alias_digest and base_digest and alias_digest.group(1).lower() == base_digest.group(1).lower():
                return True
            return False
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            self._check_request(request)
            return False

    # ------------------------------------------------ NEO 170: the small agent model
    def agent_model_candidates(self) -> list[str]:
        """Use the selected weights only; share the stock base with conversation."""
        base = str(self.config.get("llm_agent.base_model", "qwen2.5:0.5b") or "qwen2.5:0.5b")
        configured = str(self.config.get("llm_agent.model", "neo-agent") or "neo-agent")
        names = [
            str(self.config.get("llm_agent.fine_tuned", "") or ""),
            configured,
            base,
        ]
        if configured.casefold().removesuffix(":latest") == "neo-agent":
            # This default alias adds a prompt, not trained weights. The caller
            # already supplies that prompt; a shared tag avoids two loaded runners.
            names[1:] = [base, configured]
        return [n for i, n in enumerate(names) if n and n not in names[:i]]

    @staticmethod
    def _match_model(wanted: str, models: list[str]) -> str:
        if wanted in models or f"{wanted}:latest" in models:
            return wanted if wanted in models else f"{wanted}:latest"
        if ":" in wanted:
            return ""
        base = wanted.casefold()
        return next((m for m in models if m.split(":", 1)[0].casefold() == base), "")

    @staticmethod
    def _managed_agent_alias(model: str) -> bool:
        value = str(model).casefold().removesuffix(":latest")
        return (value in {"neo-agent", "neo-agent-ft", "neo-agent-ft-prev", "neo-agent-ft-candidate"}
                or re.fullmatch(r"neo-agent-ft-run-[0-9a-f]{32}", value) is not None)

    def resolve_agent_model(self) -> str:
        from jarvis.ollama_models import exact_model
        request = self._capture_request("agent")
        models = self._resolve_for_request(request, self._list_models)
        base = str(self.config.get("llm_agent.base_model", "qwen2.5:0.5b") or "qwen2.5:0.5b").strip()
        tuned = str(self.config.get("llm_agent.fine_tuned", "") or "").strip()
        configured = str(self.config.get("llm_agent.model", "neo-agent") or "neo-agent").strip()
        # A custom model is the user's explicit selection. Do not reinterpret it
        # as a managed NEO alias or replace it silently if it is missing.
        explicit = (tuned if tuned and not self._managed_agent_alias(tuned)
                    else configured if configured and not self._managed_agent_alias(configured) else "")
        if explicit:
            found = exact_model(explicit, models)
            self._check_request(request)
            if found:
                return found
            raise OllamaError(self._request_message("missing", explicit))
        rejected_aliases = []
        for wanted in self.agent_model_candidates():
            found = exact_model(wanted, models)
            if not found:
                continue
            if wanted != base and self._managed_agent_alias(wanted) and not self._agent_alias_matches_base(found, base, request):
                rejected_aliases.append(found)
                continue
            self._check_request(request)
            if rejected_aliases:
                logger.info("Using installed planner model %s; managed alias provenance not confirmed: %s", found, ", ".join(rejected_aliases))
            return found
        raise OllamaError("Модель ассистента не установлена: запустите установщик или NEO.cmd --install-deps.")

    def agent_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], *, num_predict: int | None = None,
                   on_reply: Callable[[str], None] | None = None) -> dict[str, Any]:
        """One structured answer of the agent model ({"reply": …} or {"do": […]}). Text only, no images, no tools."""
        request = self._capture_request("agent")
        if not bool(self.config.get("llm_agent.enabled", True)) or not is_local_ollama_url(request.base_url):
            self._check_request(request)
            raise OllamaError("Модель ассистента отключена в настройках.")
        if any(isinstance(m, dict) and m.get("images") for m in messages):
            raise OllamaError("Изображения анализируются отдельной командой.")
        model = self._resolve_for_request(request, self.resolve_agent_model)
        options: dict[str, Any] = {
            **self._runner_resource_options(),
            "temperature": float(self.config.get("llm_agent.temperature", 0.2) or 0.2),
            "num_ctx": int(self.config.get("llm_agent.num_ctx", 3072) or 3072),
            "num_predict": int(num_predict or self.config.get("llm_agent.max_tokens", 320) or 320),
        }
        payload = {"model": model, "messages": messages, "stream": False, "think": False, "format": schema,
                   "keep_alive": self.config.get("llm_agent.keep_alive", "5m"), "options": options}
        self._check_request(request)
        _acquire_generation_slot()
        try:
            self._check_request(request)
            timeout = (5, float(self.config.get("llm_agent.timeout_seconds", 60) or 60))
            if on_reply is not None:
                # NEO 172: stream, so a conversational reply is shown and spoken while it is generated.
                content = self._agent_json_stream(payload, timeout, on_reply, request)
            else:
                response = self._session.post(f"{request.base_url}/api/chat", json=payload, timeout=timeout)
                if response.status_code == 400 and "think" in response.text:
                    self._check_request(request)
                    payload.pop("think", None)   # models without a thinking switch reject the field on some versions
                    response = self._session.post(f"{request.base_url}/api/chat", json=payload, timeout=timeout)
                response.raise_for_status()
                body = response.json()
                if isinstance(body, dict) and str(body.get("done_reason", "") or "") == "length":
                    raise IncompleteModelOutput(self._request_message("incomplete"))
                content = _strip_thinking_markup(str((body.get("message") or {}).get("content", "") or ""))
        except requests.RequestException as exc:
            self._check_request(request)
            raise self._as_error(exc) from exc
        except Exception as exc:
            self._check_request(request)
            raise
        finally:
            _release_generation_slot()
        self._check_request(request)
        # NEO 188 NEO-TOOL-001: only a whole object is a decision. A name given twice
        # (also through an escaped spelling) is ambiguous and never silently resolved.
        try:
            value = _strict_json188(content)
        except ValueError as exc:
            if "duplicate key" in str(exc):
                raise OllamaError(self._request_message("ambiguous")) from exc
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                raise OllamaError("Модель ассистента ответила не в формате JSON.")
            try:
                value = _strict_json188(match.group(0))
            except ValueError as inner:
                if "duplicate key" in str(inner):
                    raise OllamaError(self._request_message("ambiguous")) from inner
                raise OllamaError("Модель ассистента ответила не в формате JSON.") from inner
        if not isinstance(value, dict):
            raise OllamaError("Модель ассистента ответила не объектом JSON.")
        self._check_request(request)
        value["_model"] = model
        return value

    def _stream_records(self, response, request: _RequestState, *, on_read=None):
        """Incrementally frame bounded UTF-8 NDJSON from transport byte chunks.

        Real requests responses always use iter_content: iter_lines may buffer
        indefinitely before a newline and hide cancellation from this client.
        Legacy record-only adapters are accepted as already framed records, with
        a size check before encoding; they are never used for requests.Response.
        """
        limit = _MAX_STREAM_RECORD_BYTES

        def check() -> None:
            self._check_request(request)
            if on_read is not None:
                on_read()

        def too_large() -> None:
            raise OllamaError(self._request_message("stream_too_large", limit=limit))

        def legacy_records():
            # Compatibility with existing pre-framed transport adapters. There
            # is no byte buffering here; the adapter already supplies a record.
            for line in response.iter_lines(chunk_size=_STREAM_READ_CHUNK_BYTES, decode_unicode=False):
                check()
                if isinstance(line, str):
                    if len(line) > limit:
                        too_large()
                    try:
                        line = line.encode("utf-8")
                    except UnicodeError as exc:
                        raise OllamaError(self._request_message("stream_invalid")) from exc
                if not isinstance(line, bytes):
                    raise OllamaError(self._request_message("stream_invalid"))
                if len(line) > limit:
                    too_large()
                yield line
                yield b"\n"

        iter_content = getattr(response, "iter_content", None)
        if callable(iter_content):
            chunks = iter_content(chunk_size=_STREAM_READ_CHUNK_BYTES, decode_unicode=False)
        elif not isinstance(response, requests.Response) and callable(getattr(response, "iter_lines", None)):
            chunks = legacy_records()
        else:
            raise OllamaError(self._request_message("stream_invalid"))

        def reject_constant(_value):
            raise ValueError("Non-finite JSON constant")

        def record(raw: bytearray, *, final: bool = False):
            check()
            if not raw.strip(b" \t\r"):
                return None
            try:
                value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
            except (UnicodeError, ValueError, RecursionError) as exc:
                kind = "stream_truncated" if final else "stream_invalid"
                raise OllamaError(self._request_message(kind)) from exc
            if not isinstance(value, dict) or (value.get("message") is not None and not isinstance(value["message"], dict)):
                raise OllamaError(self._request_message("stream_invalid"))
            return value

        pending = bytearray()
        iterator = iter(chunks)
        while True:
            check()
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            check()
            if not isinstance(chunk, bytes):
                raise OllamaError(self._request_message("stream_invalid"))
            start = 0
            while start < len(chunk):
                check()
                end = chunk.find(b"\n", start)
                stop = len(chunk) if end < 0 else end
                size = len(pending) + stop - start
                last_byte = chunk[stop - 1] if stop > start else (pending[-1] if pending else None)
                # One CR may be the first half of CRLF across a chunk boundary.
                # Never retain more than the record cap plus that one lookahead.
                if size > limit + (1 if last_byte == 13 else 0):
                    too_large()  # Check before a copy or extension of the buffer.
                pending.extend(chunk[start:stop])
                if end < 0:
                    break
                if pending.endswith(b"\r"):
                    pending.pop()
                value = record(pending)
                pending.clear()
                if value is not None:
                    yield value
                    check()
                start = end + 1
        # A complete final object without LF is compatible with iter_lines;
        # an incomplete UTF-8 sequence/JSON object must not become silent success.
        if pending:
            if pending.endswith(b"\r"):
                pending.pop()
            value = record(pending, final=True)
            if value is not None:
                yield value
        check()

    def _agent_json_stream(self, payload: dict[str, Any], timeout: tuple[float, float], on_reply: Callable[[str], None], request: _RequestState) -> str:
        payload = dict(payload, stream=True)
        extractor = ReplyStream()
        parts: list[str] = []
        completed = False
        cut = False
        for attempt in (0, 1):
            self._check_request(request)
            with self._session.post(f"{request.base_url}/api/chat", json=payload, stream=True, timeout=timeout) as response:
                if response.status_code == 400 and attempt == 0 and "think" in payload:
                    payload.pop("think", None)
                    continue
                with self._active_lock:
                    self._active_streams[id(response)] = response
                try:
                    response.raise_for_status()
                    response.encoding = "utf-8"
                    for data in self._stream_records(response, request):
                        self._check_request(request)
                        if data.get("error"):
                            raise OllamaError(str(data["error"]))
                        chunk = str((data.get("message") or {}).get("content", "") or "")
                        if chunk:
                            parts.append(chunk)
                            delta = extractor.feed(chunk)
                            if delta:
                                self._check_request(request)
                                on_reply(delta)
                                self._check_request(request)
                        if data.get("done"):
                            completed = True
                            cut = str(data.get("done_reason", "") or "") == "length"
                            break
                finally:
                    with self._active_lock:
                        self._active_streams.pop(id(response), None)
            break
        self._check_request(request)
        if not completed or cut:
            # NEO 188 NEO-TOOL-001: closed without the terminal record, or cut by the
            # token limit: the object is not complete, nothing from it is executed.
            raise IncompleteModelOutput(self._request_message("incomplete"))
        return _strip_thinking_markup("".join(parts))

    def _is_agent_model(self, model: str) -> bool:
        from jarvis.ollama_models import exact_model
        return any(exact_model(str(name), [str(model)]) for name in self.agent_model_candidates()[:3])

    def resolve_chat_model(self) -> str:
        # The visible conversation selector is authoritative. Never silently
        # prefer neo-agent or another parameter size over the user's selection.
        from jarvis.ollama_models import DEFAULT_CHAT_MODEL, exact_model
        wanted = str(self.config.get("local_chat.model", "") or self.config.get("ollama.chat_model", DEFAULT_CHAT_MODEL) or DEFAULT_CHAT_MODEL).strip()
        models = self._list_models()
        found = exact_model(wanted, models)
        if found:
            return found
        raise OllamaError(f"Модель для разговора «{wanted}» не установлена. Откройте настройки модели и нажмите «Скачать».")

    def chat_text(self, messages: list[dict[str, Any]], *, num_predict: int | None = None,
                  temperature: float | None = None, on_delta: Callable[[str], None] | None = None) -> str:
        """Plain local conversation (no images, no tools) with the installed chat model.

        The model stays loaded for ``local_chat.keep_alive`` after the last turn (Ollama
        unloads it itself afterwards), so an active conversation does not reload it
        every turn and an idle NEO does not keep it in memory.
        """
        request = self._capture_request("chat")
        if not self.local_chat_enabled():
            self._check_request(request)
            raise OllamaError("Локальный разговор отключён в настройках.")
        if any(isinstance(m, dict) and m.get("images") for m in messages):
            raise OllamaError("Изображения анализируются отдельной командой.")
        model = self._resolve_for_request(request, self.resolve_chat_model)
        num_ctx = int(self.config.get("local_chat.num_ctx", 4096) or 4096)
        keep_alive = self.config.get("local_chat.keep_alive", "5m")
        if self._is_agent_model(model):
            # NEO 172: the same small model also answers commands. Ollama reloads a model whenever
            # num_ctx changes, so conversation uses the agent's context size and keep-alive: one
            # loaded copy, no reload (CPU, RAM, a few seconds) each time chat and commands alternate.
            num_ctx = int(self.config.get("llm_agent.num_ctx", 3072) or 3072)
            keep_alive = self.config.get("llm_agent.keep_alive", "5m")
        options: dict[str, Any] = {
            **self._runner_resource_options(),
            "temperature": float(self.config.get("local_chat.temperature", 0.4) if temperature is None else temperature),
            "num_ctx": num_ctx,
            "num_predict": int(num_predict if num_predict is not None else self.config.get("local_chat.max_tokens", 600) or 600),
        }
        payload = {"model": model, "messages": messages, "stream": on_delta is not None, "think": False,
                   "keep_alive": keep_alive, "options": options}
        self._check_request(request)
        _acquire_generation_slot()
        try:
            self._check_request(request)
            timeout = (5, float(self.config.get("local_chat.timeout_seconds", 120) or 120))
            if on_delta is None:
                response = self._session.post(f"{request.base_url}/api/chat", json=payload, timeout=timeout)
                response.raise_for_status()
                message = (response.json().get("message") or {})
                self._check_request(request)
                return _strip_thinking_markup(str(message.get("content", "") or ""))
            # NEO 172: stream the answer so speech can start after the first sentence.
            parts: list[str] = []
            with self._session.post(f"{request.base_url}/api/chat", json=payload, stream=True, timeout=timeout) as response:
                with self._active_lock:
                    self._active_streams[id(response)] = response
                try:
                    response.raise_for_status()
                    response.encoding = "utf-8"
                    for data in self._stream_records(response, request):
                        self._check_request(request)
                        if data.get("error"):
                            raise OllamaError(str(data["error"]))
                        chunk = str((data.get("message") or {}).get("content", "") or "")
                        if chunk:
                            parts.append(chunk)
                            self._check_request(request)
                            on_delta(chunk)
                            self._check_request(request)
                        if data.get("done"):
                            break
                finally:
                    with self._active_lock:
                        self._active_streams.pop(id(response), None)
            self._check_request(request)
            return _strip_thinking_markup("".join(parts))
        except requests.RequestException as exc:
            self._check_request(request)
            raise self._as_error(exc) from exc
        except Exception as exc:
            self._check_request(request)
            raise
        finally:
            _release_generation_slot()

    def release_models(self) -> int:
        """Unload models NEO loaded (keep_alive=0) — used on exit or by the user."""
        try:
            # NEO 173: includes the NEO model (neo-agent), which the old list missed.
            from jarvis.runtime import model_life173
            return model_life173.release(self, reason="exit")
        except Exception:
            logger.debug("Model release through the lifecycle helper failed", exc_info=True)
        # Without a verified loaded-model inventory, do not guess unload targets.
        return 0

    def warmup(self, model: str | None = None) -> str:
        """Compatibility hook: no background model loading in the vision profile."""

        return "Модель загружается только при запросе анализа изображения."

    def _payload(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        model: str | None,
        response_format: str | dict[str, Any] | None,
        temperature: float | None,
        stream: bool,
        num_ctx_override: int | None = None,
        num_predict_override: int | None = None,
        think_override: bool | str | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            **self._runner_resource_options(),
            "temperature": self.config.get("ollama.temperature", 0.2)
            if temperature is None
            else temperature,
            "num_ctx": int(num_ctx_override or self.config.get("ollama.num_ctx", 4096)),
            "num_predict": int(
                num_predict_override if num_predict_override is not None
                else (-1 if bool(self.config.get("ollama.unlimited_response", True)) else self.config.get("ollama.num_predict", -1))
            ),
        }
        payload: dict[str, Any] = {
            "model": self.resolve_model(model, require_vision=True),
            "messages": messages,
            "stream": stream,
            "think": self.config.get("ollama.think", False) if think_override is None else think_override,
            "keep_alive": self.config.get("ollama.vision_keep_alive", 0),
            "options": options,
        }
        if tools:
            payload["tools"] = tools
        if response_format is not None:
            payload["format"] = response_format
        return payload

    def _response_read_timeout(self, num_predict_override: int | None = None) -> float | None:
        """Return no read-time cap for deliberately unlimited final responses.

        A token cap of -1 is only useful if the HTTP client is not separately
        killed after the legacy 75-second request timeout. Internal bounded tasks
        (query rewrites, tiny classifiers, etc.) keep their normal timeout.
        """
        unlimited = bool(self.config.get("ollama.unlimited_response", True))
        if num_predict_override is not None:
            unlimited = int(num_predict_override) < 0
        if unlimited:
            return None
        return float(self.config.get("ollama.request_timeout_seconds", 75))

    def generate_text(
        self,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.1,
        num_ctx_override: int | None = None,
        num_predict_override: int | None = None,
    ) -> str:
        """Compact non-chat fallback for models/templates that return empty chat content."""
        raise OllamaError("Генерация текста через Ollama отключена. Анализ изображений доступен отдельной командой.")

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        response_format: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        num_ctx_override: int | None = None,
        num_predict_override: int | None = None,
        think_override: bool | str | None = None,
        keep_alive_override: str | int | None = None,
    ) -> dict[str, Any]:
        request = self._capture_request("vision")
        self._resolve_for_request(request, self._require_image_request, messages, tools)
        payload = self._resolve_for_request(
            request, self._payload, messages,
            tools=tools,
            model=model,
            response_format=response_format,
            temperature=temperature,
            stream=False,
            num_ctx_override=num_ctx_override,
            num_predict_override=num_predict_override,
            think_override=think_override,
        )
        if keep_alive_override is not None:
            payload["keep_alive"] = keep_alive_override
        self._check_request(request)
        _acquire_generation_slot()
        try:
            self._check_request(request)
            response = self._session.post(
                f"{request.base_url}/api/chat",
                json=payload,
                timeout=(5, self._response_read_timeout(num_predict_override)),
            )
            response.raise_for_status()
            data = response.json()
            message = data.get("message") or {}
            if isinstance(message, dict):
                message["content"] = _strip_thinking_markup(str(message.get("content", "") or ""))
                data["message"] = message
            self._check_request(request)
            return data
        except requests.RequestException as exc:
            self._check_request(request)
            raise self._as_error(exc) from exc
        except Exception:
            self._check_request(request)
            raise
        finally:
            _release_generation_slot()

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        on_content: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        response_format: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        num_ctx_override: int | None = None,
        num_predict_override: int | None = None,
        think_override: bool | str | None = None,
        first_visible_timeout_override: float | None = None,
        total_timeout_override: float | None = None,
    ) -> dict[str, Any]:
        """Stream Ollama tokens without requests' default 512-byte buffering."""

        request = self._capture_request("vision")
        self._resolve_for_request(request, self._require_image_request, messages, tools)
        payload = self._resolve_for_request(
            request, self._payload, messages,
            tools=tools,
            model=model,
            response_format=response_format,
            temperature=temperature,
            stream=True,
            num_ctx_override=num_ctx_override,
            num_predict_override=num_predict_override,
            think_override=think_override,
        )
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        raw_content = ""
        emitted_visible = ""
        stream_markup_mode = False
        fast_pending_ws = ""
        tool_calls: list[dict[str, Any]] = []
        tool_ids188: set[str] = set()
        completed188 = False
        final_data: dict[str, Any] = {}
        first_timeout = float(
            first_visible_timeout_override
            if first_visible_timeout_override is not None
            else self.config.get("ollama.first_token_timeout_seconds", 35)
        )
        first_timeout = max(3.0, first_timeout)
        total_timeout = float(total_timeout_override or 0.0)
        started = time.monotonic()
        first_visible_at = 0.0
        self._check_request(request)
        _acquire_generation_slot()
        try:
            self._check_request(request)
            # Waiting behind another generation does not consume this response's
            # first-visible budget. The optional total budget still includes it.
            first_visible_started = time.monotonic()
            with self._session.post(
                f"{request.base_url}/api/chat",
                json=payload,
                stream=True,
                timeout=(5, first_timeout),
            ) as response:
                with self._active_lock:
                    self._active_streams[id(response)] = response
                response.raise_for_status()
                response.encoding = "utf-8"
                def check_read_timeout() -> None:
                    elapsed = time.monotonic() - started
                    if total_timeout > 0 and elapsed > total_timeout:
                        raise OllamaError(
                            f"Локальная модель превысила лимит ответа ({total_timeout:.0f} с)."
                        )
                    if not first_visible_at and time.monotonic() - first_visible_started > first_timeout:
                        raise OllamaError("Модель слишком долго не начала выдавать видимый ответ.")

                for data in self._stream_records(response, request, on_read=check_read_timeout):
                    self._check_request(request)
                    if data.get("error"):
                        raise OllamaError(str(data["error"]))
                    if completed188:
                        break                       # a record after the terminal one is not part of this answer
                    final_data = data
                    completed188 = bool(data.get("done"))
                    message = data.get("message") or {}
                    thinking_chunk = str(message.get("thinking", "") or "")
                    if thinking_chunk:
                        thinking_parts.append(thinking_chunk)
                        if on_thinking is not None:
                            self._check_request(request)
                            on_thinking(thinking_chunk)
                            self._check_request(request)
                    chunk = str(message.get("content", "") or "")
                    if chunk:
                        content_parts.append(chunk)
                        if not stream_markup_mode and "<" not in chunk:
                            # Common think=false path: incrementally reproduce the
                            # legacy cumulative ``.strip()`` semantics without
                            # rebuilding/regex-scanning the entire answer per token.
                            # Trailing whitespace is held until a later non-space
                            # token arrives, exactly as the old cumulative sanitizer.
                            candidate = fast_pending_ws + chunk
                            if not emitted_visible:
                                candidate = candidate.lstrip()
                            body = candidate.rstrip()
                            fast_pending_ws = candidate[len(body):]
                            if body:
                                if not first_visible_at:
                                    first_visible_at = time.monotonic()
                                if on_content is not None:
                                    self._check_request(request)
                                    on_content(body)
                                    self._check_request(request)
                                emitted_visible += body
                        else:
                            if not stream_markup_mode:
                                stream_markup_mode = True
                                raw_content = "".join(content_parts)
                                fast_pending_ws = ""
                            else:
                                raw_content += chunk
                            visible = _safe_stream_visible(raw_content)
                            if on_content is not None and visible.startswith(emitted_visible):
                                delta = visible[len(emitted_visible):]
                                if delta:
                                    if not first_visible_at:
                                        first_visible_at = time.monotonic()
                                    self._check_request(request)
                                    on_content(delta)
                                    self._check_request(request)
                                    emitted_visible = visible
                            elif visible and not first_visible_at:
                                first_visible_at = time.monotonic()
                    # A thinking-capable model can keep the socket active without
                    # producing any user-visible text.  ``think=false`` should
                    # normally prevent this, but the interactive assistant still
                    # needs an absolute first-visible-token guard.
                    if not first_visible_at and (time.monotonic() - first_visible_started) > first_timeout:
                        raise OllamaError(
                            "Модель слишком долго не начала выдавать видимый ответ."
                        )
                    calls = message.get("tool_calls") or []
                    for call in calls:
                        if not isinstance(call, dict):
                            continue
                        # NEO 188 NEO-TOOL-001: a call is identified by its own id/index; a
                        # repeated delivery of that call is not a new call, while a second
                        # call with the same text is kept.
                        function = call.get("function") if isinstance(call.get("function"), dict) else {}
                        ident = call.get("id", function.get("index"))
                        if ident is not None:
                            key = str(ident)
                            if key in tool_ids188:
                                continue
                            tool_ids188.add(key)
                        tool_calls.append(call)
        except OperationCancelled:
            raise
        except requests.Timeout as exc:
            self._check_request(request)
            raise OllamaError(
                "Модель не начала отвечать вовремя. Проверьте, что Ollama запущена, "
                "модель полностью помещается в память, и уменьшите контекст в настройках."
            ) from exc
        except requests.RequestException as exc:
            self._check_request(request)
            raise self._as_error(exc) from exc
        except Exception:
            self._check_request(request)
            raise
        finally:
            # ``response`` may not exist if the connection failed before headers.
            try:
                with self._active_lock:
                    self._active_streams.pop(id(response), None)  # type: ignore[name-defined]
            except Exception:
                pass
            _release_generation_slot()

        clean_content = _strip_thinking_markup("".join(content_parts))
        message: dict[str, Any] = {
            "role": "assistant",
            "content": clean_content,
        }
        if thinking_parts:
            message["thinking"] = "".join(thinking_parts)
        if tool_calls and completed188:
            message["tool_calls"] = tool_calls
        elif tool_calls:
            final_data["incomplete_tool_calls"] = len(tool_calls)   # never handed over for execution
        final_data["message"] = message
        self._check_request(request)
        return final_data

    def analyze_image(
        self,
        image_path: Path,
        prompt: str,
        *,
        response_format: str | dict[str, Any] | None = None,
        max_edge: int | None = None,
        num_predict: int | None = None,
        keep_alive: str | int | None = None,
    ) -> str:
        """Analyze a screenshot with a context budget large enough for image tokens.

        RC4 inherited 2048-token user settings from old builds. A single desktop
        screenshot can already consume more than that before the text prompt is
        counted, causing Ollama's ``exceeds available context size`` error. Vision
        now uses an independent minimum context and retries once with a smaller
        image if the local runner still rejects the request.
        """
        request = self._capture_request("vision")
        self._require_requests_enabled()
        self._check_request(request)
        from io import BytesIO
        from PIL import Image

        vision_model = self._resolve_for_request(
            request, self.resolve_model, str(self.config.get("ollama.vision_model", "qwen3.5:4b")), require_vision=True
        )
        vision_ctx = max(8192, int(self.config.get("ollama.vision_num_ctx", 8192) or 8192))
        if bool(self.config.get("ollama.vision_low_priority", True)):
            self._apply_active_game_priority_guard(always=True)
        first_edge = int(max_edge) if max_edge else 2200
        predict = int(num_predict) if num_predict else max(256, min(2048, int(self.config.get("ollama.vision_max_tokens", 1024) or 1024)))

        def encode(path: Path, max_edge: int) -> str:
            with Image.open(path) as image:
                image = image.convert("RGB")
                longest = max(image.size)
                if longest > max_edge:
                    scale = max_edge / longest
                    image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
                out = BytesIO()
                image.save(out, format="JPEG", quality=88, optimize=True)
                return base64.b64encode(out.getvalue()).decode("ascii")

        last_error: Exception | None = None
        for edge, ctx in ((first_edge, vision_ctx), (min(1600, max(768, first_edge * 3 // 4)), max(12288, vision_ctx))):
            self._check_request(request)
            encoded = encode(image_path, edge)
            self._check_request(request)
            messages = [{"role": "user", "content": prompt, "images": [encoded]}]
            try:
                self._check_request(request)
                response = self.chat(
                    messages,
                    model=vision_model,
                    temperature=0.1,
                    num_ctx_override=ctx,
                    num_predict_override=predict,
                    think_override=False,
                    response_format=response_format,
                    keep_alive_override=keep_alive,
                )
                answer = _strip_thinking_markup(str(response.get("message", {}).get("content", "")))
                self._check_request(request)
                if answer:
                    return answer

                # Rare Qwen/Ollama template edge case: retry with thinking enabled,
                # but keep the reasoning field private and return only final content.
                self._check_request(request)
                retry = self.chat(
                    messages,
                    model=vision_model,
                    temperature=0.05,
                    num_ctx_override=ctx,
                    num_predict_override=predict,
                    think_override=True,
                    response_format=response_format,
                    keep_alive_override=keep_alive,
                )
                answer = _strip_thinking_markup(str(retry.get("message", {}).get("content", "")))
                self._check_request(request)
                if answer:
                    return answer
                last_error = OllamaError("Мультимодальная модель не вернула итоговый ответ.")
            except OllamaError as exc:
                self._check_request(request)
                last_error = exc
                lowered = str(exc).casefold()
                if "context" not in lowered and "n_ctx" not in lowered and "exceeds" not in lowered:
                    raise
                continue
        if last_error:
            raise last_error
        raise OllamaError("Не удалось проанализировать изображение.")

    @staticmethod
    def _as_error(exc: requests.RequestException) -> OllamaError:
        detail = ""
        if getattr(exc, "response", None) is not None:
            try:
                detail = f" — {exc.response.text[:500]}"
            except Exception:
                detail = ""
        return OllamaError(f"Ошибка запроса к Ollama: {exc}{detail}")
