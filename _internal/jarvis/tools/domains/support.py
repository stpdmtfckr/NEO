# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
from typing import Any, Callable

import psutil

from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}

def _enum(values: list[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": description}

_HOST_LABEL187 = __import__("re").compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def _valid_ping_host187(host: str) -> bool:
    import ipaddress
    value = str(host or "").strip()
    if not value or len(value) > 253 or value.startswith("-") or any(c.isspace() for c in value):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    labels = value.rstrip(".").split(".")
    return all(_HOST_LABEL187.match(label) for label in labels)


def register_support_tools(
    registry: Any, *, config: Any, db: Any, ollama: Any,
    run_command: Callable[..., Any], hidden_subprocess_kwargs: Callable[[], dict[str, Any]],
    known_folder: Callable[..., Any], require_path_value: Callable[..., Any], safe_filename: Callable[[str], str],
) -> None:
    _run = run_command
    _hidden_subprocess_kwargs = hidden_subprocess_kwargs
    _known_folder = known_folder
    _require_path_value = require_path_value
    _safe_filename = safe_filename
    # NETWORK
    def network_risk(args: dict[str, Any]) -> Risk:
        return Risk.MEDIUM if args.get("action") == "flush_dns" else Risk.SAFE

    def network(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", ""))
        if action == "ping":
            host = str(args.get("host", "8.8.8.8") or "8.8.8.8").strip()
            if not _valid_ping_host187(host):
                # HIST-061: only an IP address or a host name reaches argv; an
                # option-looking or malformed value is refused, not passed on.
                raise ValueError("Укажите адрес для проверки: доменное имя или IP-адрес.")
            count = max(1, min(10, int(args.get("count", 4))))
            flag = "-n" if os.name == "nt" else "-c"
            proc = _run(["ping", flag, str(count), host], timeout=30)
            return ToolExecution(proc.returncode == 0, "Проверка завершена", proc.stdout or proc.stderr)
        if action == "info":
            # HIST-063: independent parts are collected independently; a resolver
            # failure leaves the interface data and says what is missing.
            data: dict[str, Any] = {"hostname": socket.gethostname()}
            missing: dict[str, str] = {}
            for key, read in (("addresses", lambda: socket.getaddrinfo(data["hostname"], None)),
                              ("interfaces", psutil.net_if_addrs), ("stats", psutil.net_if_stats)):
                try:
                    data[key] = read()
                except Exception as exc:
                    missing[key] = type(exc).__name__
            if missing:
                data["partial"] = True
                data["missing"] = missing
            # HIST-060: the useful fields reach the answer through a dedicated,
            # privacy-aware formatter (no MAC addresses, no raw dumps).
            from jarvis.language import language_code
            from jarvis.runtime.network_info188 import summary
            text = summary(data, language_code(config))
            data["localized186"] = True
            return ToolExecution(True, text, data)
        if action == "flush_dns":
            if os.name != "nt":
                raise OSError("Команда реализована для Windows")
            proc = _run(["ipconfig", "/flushdns"])
            return ToolExecution(proc.returncode == 0, proc.stdout.strip() or proc.stderr.strip())
        raise ValueError(f"Неизвестное действие network: {action}")

    registry.register(
        ToolDefinition(
            "network",
            "Диагностика сети: ping, адреса и очистка DNS-кэша.",
            _object_schema(
                {
                    "action": _enum(["ping", "info", "flush_dns"], "Операция"),
                    "host": {"type": "string"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                ["action"],
            ),
            network,
            network_risk,
        )
    )

    # MEMORY
    def memory_risk(args: dict[str, Any]) -> Risk:
        # Clearing all memory cannot be undone: always asks first.
        if args.get("action") == "clear":
            return Risk.HIGH
        return Risk.MEDIUM if args.get("action") in {"forget", "forget_last"} else Risk.SAFE

    def memory(args: dict[str, Any]) -> ToolExecution:
        # NEO 169: preferences, notes, listing, forgetting, clearing and an on/off switch.
        from jarvis.runtime import memory169
        action = str(args.get("action", ""))
        key = str(args.get("key", "")).strip()
        code = str(config.get("app.language", "ru") or "ru")
        if action == "remember":
            ok, text = memory169.remember(db, config, key, str(args.get("value", "")), code)
            return ToolExecution(ok, text, {"key": key})
        if action == "note":
            ok, text = memory169.note(db, config, str(args.get("value", "") or key), code)
            return ToolExecution(ok, text, {})
        if action in {"forget", "forget_last"}:
            ok, text = memory169.forget(db, "" if action == "forget_last" else key, code)
            return ToolExecution(ok, text, {})
        if action == "list":
            return ToolExecution(True, memory169.listing(db, config, code), {"count": len(db.list_memories())})
        if action == "clear":
            return ToolExecution(True, memory169.clear(db, config, code), {})
        if action in {"enable", "disable"}:
            data = json.loads(json.dumps(config.data))
            data.setdefault("memory", {})["enabled"] = action == "enable"
            config.save(data)
            return ToolExecution(True, memory169.t("enabled" if action == "enable" else "disabled", code), {})
        raise ValueError(f"Неизвестное действие memory: {action}")

    registry.register(
        ToolDefinition(
            "memory",
            "Локальная долговременная память пользователя: запомнить, забыть или показать сохранённые факты.",
            _object_schema(
                {
                    "action": _enum(["remember", "note", "forget", "forget_last", "list", "clear", "enable", "disable"], "Операция"),
                    "key": {"type": "string", "description": "Короткое название факта"},
                    "value": {"type": "string"},
                },
                ["action"],
            ),
            memory,
            memory_risk,
        )
    )

    # DEVELOPMENT
    def developer_risk(_: dict[str, Any]) -> Risk:
        return Risk.HIGH

    def _extract_json(text: str) -> Any:
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
            if match:
                return json.loads(match.group(1))
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise

    def developer(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", ""))
        if action == "generate_python_app":
            return ToolExecution(False, "Создание программы по свободному описанию требует генерации текста, которая отключена. Можно создать файл с заданным содержимым или запустить существующий Python-файл.", {"error": "text_generation_disabled", "capabilities": ["file_create", "file_write", "run_python_file"]})

        if action == "run_python_file":
            file_path = _known_folder(str(args.get("file_path", "")))
            if not file_path.is_file() or file_path.suffix.lower() != ".py":
                raise ValueError("Нужен существующий .py файл")
            cwd = str(file_path.parent)
            from jarvis.runtime.distribution164 import console_python
            proc = subprocess.Popen([str(console_python()), str(file_path)], cwd=cwd, **_hidden_subprocess_kwargs())
            return ToolExecution(True, f"Python-файл запущен: {file_path}", {"pid": proc.pid})
        raise ValueError(f"Неизвестное действие developer: {action}")

    registry.register(
        ToolDefinition(
            "developer",
            "Создание небольшого Python-приложения в указанной папке или запуск конкретного Python-файла. Всегда требует подтверждения.",
            _object_schema(
                {
                    "action": _enum(["generate_python_app", "run_python_file"], "Операция"),
                    "output_dir": {"type": "string"},
                    "project_name": {"type": "string"},
                    "description": {"type": "string"},
                    "file_path": {"type": "string"},
                },
                ["action"],
            ),
            developer,
            developer_risk,
        )
    )

    # POWERSHELL ADVANCED MODE
    def powershell(args: dict[str, Any]) -> ToolExecution:
        if not config.get("security.allow_arbitrary_powershell", False):
            raise PermissionError("Произвольный PowerShell выключен в настройках безопасности")
        script = str(args.get("script", "")).strip()
        if not script:
            raise ValueError("Пустой PowerShell-скрипт")
        timeout = max(1, min(300, int(args.get("timeout_seconds", 60))))
        proc = _run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            timeout=timeout,
        )
        data = {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        return ToolExecution(proc.returncode == 0, "PowerShell завершён", data)

    registry.register(
        ToolDefinition(
            "powershell",
            "КРИТИЧЕСКИЙ режим: выполнить произвольный PowerShell только когда нет специального инструмента и пользователь явно попросил. По умолчанию выключен.",
            _object_schema(
                {
                    "script": {"type": "string", "description": "Полный PowerShell-скрипт"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                },
                ["script"],
            ),
            powershell,
            lambda _: Risk.CRITICAL,
        )
    )
