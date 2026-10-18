# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from jarvis.config import ConfigManager
from jarvis.database import Database
from jarvis.ollama_client import OllamaClient
from jarvis.security import Risk, SecurityPolicy
from jarvis.runtime.context_memory import ContextMemory
from jarvis.runtime.media_activity import note_tool_execution
from jarvis.runtime.domains import classify_domain
from jarvis.tools.argument_validation import normalize_arguments, normalize_tool_call, validate as validate_arguments
from jarvis.runtime.conversation_journal import ConversationJournal, current_scope

from contextlib import contextmanager
from contextvars import ContextVar
_SKIP_EXECUTION_LEARNING184 = ContextVar("skip_execution_learning184", default=False)

@contextmanager
def suppress_execution_learning184():
    """A GUI/queued job has no current Agent phrase; isolate only its listener."""
    token = _SKIP_EXECUTION_LEARNING184.set(True)
    try:
        yield
    finally:
        _SKIP_EXECUTION_LEARNING184.reset(token)

logger = logging.getLogger(__name__)

RiskResolver = Callable[[dict[str, Any]], Risk]
Handler = Callable[[dict[str, Any]], Any]
ExecutionGuard = Callable[[str, dict[str, Any], Risk], str | None]


def exception_outcome(exc: Exception) -> str:
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & {"OperationCancelled", "CancelledError"}:
        return "cancelled"
    # HIST-062: subprocess.TimeoutExpired is a timeout by type, not an internal bug.
    import subprocess
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or names & {"Timeout", "ReadTimeout", "ConnectTimeout"}:
        return "timeout"
    if isinstance(exc, subprocess.CalledProcessError):
        return "external_service"   # the external program failed; NEO's own code did not
    if isinstance(exc, ConnectionError) or names & {"ConnectionError", "ProxyError", "SSLError", "gaierror"}:
        return "network_failure"
    if names & {"HTTPError", "HTTPStatusError", "ClientResponseError"}:
        return "external_service"
    if isinstance(exc, (ValueError, FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError)):
        return "user_state"
    return "internal_bug"


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    risk_resolver: RiskResolver

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolExecution:
    success: bool
    text: str
    data: Any = None
    outcome: str = ""
    message_key: str = ""

    def __post_init__(self) -> None:
        if not self.outcome:
            flags = self.data if isinstance(self.data, dict) else {}
            self.outcome = "success" if self.success else "cancelled" if flags.get("cancelled") else "timeout" if flags.get("timeout") else "user_state"
        if not self.message_key:
            self.message_key = "tool.result." + self.outcome

    def as_model_text(self) -> str:
        payload = {"success": self.success, "message": self.text, "outcome": self.outcome, "message_key": self.message_key}
        if self.data is not None:
            payload["data"] = self.data
        return json.dumps(payload, ensure_ascii=False, default=str)


class ToolRegistry:
    def __init__(self, config: ConfigManager, database: Database, ollama: OllamaClient):
        self.config = config
        self.database = database
        self.ollama = ollama
        self.security = SecurityPolicy(config)
        self.context_memory = ContextMemory(config)
        self.conversation_journal = ConversationJournal(config)
        self.execution_guard: ExecutionGuard | None = None
        self.execution_transport = "local"
        self._tools: dict[str, ToolDefinition] = {}
        # All built-in tools are registered through plugin ownership boundaries.
        # Legacy implementation modules are reused behind a one-pass dispatcher,
        # preserving mature behavior while making feature ownership explicit.
        self.tool_plugin_owners: dict[str, str] = {}
        self.builtin_plugins: list[str] = []
        try:
            from jarvis.plugins.api import PluginContext
            from jarvis.plugins.builtin import register_all as register_builtin_plugins
            self.builtin_plugins = register_builtin_plugins(PluginContext(self, config, database, ollama))
        except Exception:
            logger.exception("Built-in plugin registration failed")
            raise

        # Third-party/local plugins are loaded last so they can depend on all core
        # services but cannot silently replace an existing tool name.
        try:
            from pathlib import Path
            from jarvis.runtime.plugins import PluginManager
            self.plugin_manager = PluginManager(Path(__file__).resolve().parents[2] / str(config.get("plugins.directory", "plugins") or "plugins"), config)
            if bool(config.get("plugins.enabled", True)):
                self.plugin_manager.load_into(self)
        except Exception:
            logger.exception("Plugin discovery failed")
            self.plugin_manager = None

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise KeyError(f"Duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """Remove a tool added by a user plugin (built-in tools are never removed this way)."""
        owner = str(getattr(self, "tool_plugin_owners", {}).get(name, ""))
        if not owner.startswith("user."):
            return False
        self.tool_plugin_owners.pop(name, None)
        return self._tools.pop(name, None) is not None

    def set_execution_guard(self, guard: ExecutionGuard | None) -> None:
        """Install a transport-specific hard-deny guard for this registry instance."""
        self.execution_guard = guard

    def schemas(self, names: list[str] | set[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        if names is None:
            return [tool.schema() for tool in self._tools.values()]
        wanted = set(names)
        return [tool.schema() for name, tool in self._tools.items() if name in wanted]

    def get(self, name: str) -> ToolDefinition:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def risk_for(self, name: str, arguments: dict[str, Any]) -> Risk:
        name, arguments = normalize_tool_call(name, arguments)
        return self.get(name).risk_resolver(arguments)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        try:
            # NEO 188 atlas: free-text slots carry the user's words, never the internal pivot words.
            from jarvis.runtime.multilingual_commands import restore_turn_slots188
            arguments = restore_turn_slots188(arguments)
        except Exception:
            logger.debug("Slot word restoration skipped", exc_info=True)
        result = self._execute_unlocalized(name, arguments)
        listener = getattr(self, "execution_listener", None)
        if listener is not None and not _SKIP_EXECUTION_LEARNING184.get():
            # NEO 172: the agent learns which command a phrase ran (for «не то» corrections).
            try:
                listener(name, arguments, result)
            except Exception:
                logger.debug("Execution listener failed", exc_info=True)
        journal = getattr(self, "conversation_journal", None)
        if journal is not None:
            try:
                journal.record_execution(current_scope(), name, arguments, result)
            except Exception:
                logger.debug("Scoped object journal unavailable", exc_info=True)
        if result.message_key == "tool.result." + result.outcome:
            action = str(arguments.get("action", "result")) if isinstance(arguments, dict) else "result"
            result.message_key = "tool." + name + "." + action + "." + result.outcome
        if str(getattr(self, "tool_plugin_owners", {}).get(name, "")).startswith("user."):
            return result      # user plugins answer in the user's language themselves
        try:
            from jarvis.language import language_code
            from jarvis.runtime.localized_text import render_action_result
            result.text = render_action_result(name, arguments if isinstance(arguments, dict) else {}, result.success, result.text, result.data, language_code(self.config))
        except Exception:
            # Translation errors must never replace a confirmed tool result.
            logger.debug("Could not localize tool result %s", name, exc_info=True)
        return result

    def _execute_unlocalized(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        import time
        from jarvis.runtime.cancellation import OperationCancelled, cancellation_manager
        from jarvis.runtime.developer import developer_bus
        from jarvis.runtime.latency import latency_profiler

        latency_profiler().mark("transcript_to_intent", name)
        try:
            name, arguments = normalize_tool_call(name, SecurityPolicy.sanitize_tool_arguments(arguments))
            tool = self.get(name)
        except (KeyError, TypeError, ValueError) as exc:
            return ToolExecution(False, "Invalid tool arguments: " + str(exc), {"invalid_tool_call": True})
        validation_errors = validate_arguments(arguments, tool.parameters)
        if validation_errors:
            return ToolExecution(False, "Invalid tool arguments: " + "; ".join(validation_errors), {"invalid_tool_call": True, "errors": validation_errors})
        domain_decision = classify_domain(name, arguments=dict(arguments or {}), transport=self.execution_transport)
        started = time.perf_counter()
        # HOTFIX92: final execution boundary.  This catches every call path,
        # including legacy direct actions and deterministic Atlas routes, not only
        # LLM function calls. Background tasks have no turn claim and are unchanged.
        try:
            from jarvis.routing.ownership_firewall import current_turn_claim, tool_allowed
            _claim = current_turn_claim()
            _ownership_ok, _ownership_reason = tool_allowed(name, dict(arguments or {}), _claim)
        except Exception:
            logger.exception("Turn ownership guard failed closed for %s", name)
            _claim = None
            _ownership_ok, _ownership_reason = False, "ошибка проверки владельца запроса"
        if not _ownership_ok:
            message = "Маршрутизация заблокировала несвязанное действие: " + str(_ownership_reason)
            execution = ToolExecution(False, message, {
                "blocked_by_routing_firewall": True,
                "owner": str(getattr(_claim, "owner", "") or ""),
                "tool": name,
            })
            risk = Risk.SAFE
            elapsed = time.perf_counter() - started
            latency_profiler().record(f"tool:{name}", elapsed, "routing_blocked")
            developer_bus().emit(
                "result", f"{name}: {message}",
                data={"domain": domain_decision.domain, "intent": domain_decision.intent, "success": False, "routing_blocked": True},
                duration_ms=elapsed * 1000.0,
            )
            self.database.log_action(name, arguments, risk.label, False, execution.as_model_text())
            logger.warning("Routing ownership blocked tool=%s args=%s reason=%s", name, arguments, _ownership_reason)
            return execution
        required = tool.parameters.get("required", []) if isinstance(tool.parameters, dict) else []
        missing = [
            str(key) for key in (required or [])
            if str(key) not in arguments or arguments.get(str(key)) is None
            or (isinstance(arguments.get(str(key)), str) and not str(arguments.get(str(key))).strip())
        ]
        if missing:
            # LLM tool calls are untrusted structured input. Reject malformed calls
            # before entering handlers/risk resolvers so an omitted action becomes
            # a normal recoverable result instead of a traceback in the UI log.
            message = "Не хватает обязательных параметров: " + ", ".join(missing)
            execution = ToolExecution(False, message, {"invalid_tool_call": True, "missing": missing})
            risk = Risk.SAFE
            elapsed = time.perf_counter() - started
            latency_profiler().record(f"tool:{name}", elapsed, "invalid_arguments")
            developer_bus().emit("result", f"{name}: {message}", data={"domain": domain_decision.domain, "intent": domain_decision.intent, "success": False, "invalid_arguments": True}, duration_ms=elapsed * 1000.0)
            self.database.log_action(name, arguments, risk.label, False, execution.as_model_text())
            return execution
        try:
            risk = tool.risk_resolver(arguments)
        except Exception:
            logger.exception("Tool risk assessment failed closed for %s", name)
            return ToolExecution(False, "Действие не выполнено: не удалось подтвердить безопасность или неизменность параметров.", {"risk_check_failed": True}, outcome="blocked")
        developer_bus().emit("tool", name, data={"domain": domain_decision.domain, "intent": domain_decision.intent, "arguments": dict(arguments or {}), "risk": risk.label})
        if self.execution_guard is not None:
            try:
                blocked_reason = self.execution_guard(name, dict(arguments or {}), risk)
            except Exception:
                logger.exception("Tool execution guard failed closed for %s", name)
                blocked_reason = "Действие заблокировано: ошибка удалённой политики безопасности."
            if blocked_reason:
                execution = ToolExecution(False, str(blocked_reason), {"blocked_by_policy": True})
                elapsed = time.perf_counter() - started
                latency_profiler().record(f"tool:{name}", elapsed, "blocked")
                developer_bus().emit("result", f"{name}: {execution.text}", data={"success": False, "blocked": True}, duration_ms=elapsed * 1000.0)
                self.database.log_action(name, arguments, risk.label, False, execution.as_model_text())
                try:
                    if current_scope() != "desktop" or not self.conversation_journal.enabled() or self.conversation_journal._safe(json.dumps(arguments, ensure_ascii=False)) is None:
                        return execution
                    self.context_memory.record_action(name, dict(arguments or {}), False, execution.text, execution.data)
                    self.context_memory.record_domain_action(
                        domain=domain_decision.domain, intent=domain_decision.intent, tool_name=name,
                        arguments=dict(arguments or {}), success=False, result_text=execution.text, data=execution.data,
                        transport=domain_decision.transport
                    )
                except Exception:
                    logger.debug("Context JSON blocked-action journal failed", exc_info=True)
                return execution
        try:
            # The cancel tool itself must remain executable after the cancellation
            # flag is set. Other tools cooperate at their entry point and long
            # operations additionally poll the same manager.
            if not (name == "automation" and str(arguments.get("action", "")).casefold() == "cancel"):
                cancellation_manager().raise_if_cancelled()
            latency_profiler().mark("intent_to_action", name)
            try:
                from jarvis.routing.ownership_firewall import tool_execution_scope
                with tool_execution_scope(name):
                    result = tool.handler(arguments)
            finally:
                latency_profiler().mark("action_execution", name)
            
            if isinstance(result, ToolExecution):
                execution = result
            else:
                try:
                    from jarvis.language import language_code
                    _lang = language_code(self.config)
                except Exception:
                    _lang = "ru"
                _action = str(arguments.get("action", "") or "").strip()
                _label = f"{name}.{_action}" if _action else name
                _templates = {
                    "ru": "Выполнено: {0}.", "en": "Completed: {0}.", "de": "Ausgeführt: {0}.",
                    "fr": "Terminé : {0}.", "es": "Completado: {0}.", "it": "Completato: {0}.",
                    "pt": "Concluído: {0}.",
                }
                execution = ToolExecution(True, _templates.get(_lang, _templates["en"]).format(_label), result)
        except OperationCancelled as exc:
            execution = ToolExecution(False, str(exc), {"cancelled": True}, outcome="cancelled")
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            from jarvis.runtime.diagnostics import safe_error
            raw_error = safe_error(exc or "").strip()       # NEO 182: no keys/tokens in user-facing errors
            # Internal enum/action names are useful in logs but confusing in the
            # assistant UI (for example: "email action: unread_count").  Keep the
            # traceback above for developers while returning a normal sentence to
            # voice/chat/Telegram users.
            lowered = raw_error.casefold()
            if ("неизвест" in lowered and "action" in lowered) or "unknown action" in lowered or "неизвестное действие" in lowered:
                execution = ToolExecution(False, "Не удалось однозначно определить требуемое действие. Переформулируйте команду, и я попробую ещё раз.", {"internal_error_hidden": True})
            else:
                execution = ToolExecution(False, raw_error or "Не удалось выполнить действие.")
            execution.outcome = exception_outcome(exc)
            execution.message_key = "tool.result." + execution.outcome
            if execution.outcome in {"network_failure", "external_service", "timeout", "internal_bug"}:
                # URLs may contain API credentials. Keep traceback in diagnostic
                # logging; ordinary chat gets a stable error category.
                execution.text = {
                    "network_failure": "Не удалось подключиться к сервису. Проверь соединение и попробуй ещё раз.",
                    "external_service": "Сервис не смог выполнить запрос. Попробуй позже или проверь его настройки.",
                    "timeout": "Время ожидания ответа истекло. Проверь состояние операции перед повтором.",
                    "internal_bug": "Внутренняя ошибка действия. Подробности сохранены в диагностическом журнале.",
                }[execution.outcome]
        elapsed = time.perf_counter() - started
        latency_profiler().record(f"tool:{name}", elapsed, f"success={execution.success}")
        developer_bus().emit("result", f"{name}: {execution.text}", data={"domain": domain_decision.domain, "intent": domain_decision.intent, "success": execution.success}, duration_ms=elapsed * 1000.0)
        self.database.log_action(name, arguments, risk.label, execution.success, execution.as_model_text())
        try:
            note_tool_execution(name, dict(arguments or {}), execution.success, execution.data)
        except Exception:
            logger.debug("Shared media activity update failed", exc_info=True)
        try:
            journal = getattr(self, "conversation_journal", None)
            if current_scope() != "desktop" or (journal is not None and (not journal.enabled() or journal._safe(json.dumps(arguments, ensure_ascii=False, default=str)) is None)):
                return execution
            self.context_memory.record_action(
                name, dict(arguments or {}), execution.success, execution.text, execution.data
            )
            self.context_memory.record_domain_action(
                domain=domain_decision.domain, intent=domain_decision.intent, tool_name=name,
                arguments=dict(arguments or {}), success=execution.success, result_text=execution.text, data=execution.data,
                transport=domain_decision.transport
            )
            if name == "memory" and execution.success:
                self.context_memory.sync_profile(self.database.list_memories(limit=200))
        except Exception:
            logger.debug("Context JSON action journal failed", exc_info=True)
        return execution
