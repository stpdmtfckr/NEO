# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pyperclip
import psutil

from jarvis.browser_control import (
    activate_tab_by_title,
    list_browser_tabs,
    selected_browser_tab_context,
)
from jarvis.desktop_resolver import normalize as normalize_target, target_similarity
from jarvis.runtime.audio_focus import audio_focus_manager
from jarvis.runtime.cancellation import OperationCancelled, cancellation_manager
from jarvis.runtime.clipboard_history import clipboard_history
from jarvis.runtime.file_search import resolve_search_roots, format_search_results
from jarvis.runtime.search_jobs162 import run_search
from jarvis.runtime.hardware import average as hardware_average
from jarvis.runtime.latency import latency_profiler
from jarvis.runtime.objects import DesktopObject, desktop_objects
from jarvis.runtime.plugins import PluginManager
from jarvis.runtime.scenarios import run_scenario, scenario_risk
from jarvis.runtime.self_test import run_self_test
from jarvis.runtime.undo import undo_manager
from jarvis.integrations.mail_services import provider_client, enabled_providers, display_detail_for_provider, MailMessage
from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _enum(values: list[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": description}


def _preferred_browser_exe(config: Any) -> str:
    value = str(config.get("desktop.preferred_browser", "firefox") or "firefox").casefold()
    if "chrome" in value: return "chrome.exe"
    if "edge" in value: return "msedge.exe"
    if "opera" in value: return "opera.exe"
    if "brave" in value: return "brave.exe"
    if "vivaldi" in value: return "vivaldi.exe"
    if "yandex" in value or "яндекс" in value: return "browser.exe"
    return "firefox.exe"




def _browser_scope(value: str = "") -> str:
    raw = normalize_target(value)
    if not raw or raw in {"browser", "браузер", "active", "current", "текущий"}: return ""
    if "firefox" in raw or "mozilla" in raw or "мозил" in raw: return "firefox"
    if "chrome" in raw or "хром" in raw: return "chrome"
    if "edge" in raw or "эдж" in raw: return "edge"
    if "opera" in raw or "опера" in raw: return "opera"
    if "yandex" in raw or "яндекс браузер" in raw: return "yandex"
    if "brave" in raw: return "brave"
    if "vivaldi" in raw: return "vivaldi"
    return ""

def register_extended(registry: Any) -> None:
    config = registry.config
    db = registry.database
    ollama = registry.ollama
    root = Path(__file__).resolve().parents[2]

    # --- CONTROL PLANE: interrupt, queue, safe parallel actions ---
    def automation(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "") or "").casefold()
        cancel = cancellation_manager()
        if action == "cancel":
            cancel.cancel(str(args.get("reason", "") or "Пользователь попросил остановить текущую операцию"))
            return ToolExecution(True, "Текущая длинная операция остановлена или получила сигнал отмены.", {"cancelled": True})
        from jarvis.runtime.automation_requests157 import reviewed_call, execute_reviewed_child, result_text, plan_error
        from jarvis.language import language_code
        code = language_code(config)
        calls = args.get("calls", []) or []
        limit = max(1, min(12, int(config.get('automation.max_queue', 12) or 12)))
        if action not in {'queue', 'parallel'}:
            raise ValueError(plan_error(code))
        if not isinstance(calls, list) or not 1 <= len(calls) <= limit:
            raise ValueError(plan_error(code))
        from jarvis.tools.argument_validation import normalize_tool_call, validate
        from jarvis.routing.ownership_firewall import current_turn_claim
        clean: list[tuple[str, dict[str, Any]]] = []
        for raw in calls:
            if not isinstance(raw, dict):
                raise ValueError(plan_error(code))
            name = str(raw.get("tool", "") or "").strip()
            arguments = raw.get("arguments", {}) or {}
            if name == "automation" or not isinstance(arguments, dict):
                raise ValueError(plan_error(code))
            name, arguments = normalize_tool_call(name, dict(arguments))
            definition = registry.get(name)  # fail before doing any work
            if validate(arguments, definition.parameters):
                raise ValueError(plan_error(code))
            clean.append((name, arguments))
        reviewed = all(reviewed_call(name, arguments, action)
                       and registry.risk_for(name, arguments) <= (Risk.SAFE if action == 'parallel' else Risk.MEDIUM)
                       for name, arguments in clean)
        # A clarified native task list is deliberately restricted to reviewed
        # reads/opening. Other legacy compound routes keep their existing
        # per-clause ownership scope; they cannot borrow this child scope.
        if not reviewed and (current_turn_claim().owner == 'native:automation' or action == 'parallel'):
            raise ValueError(plan_error(code, unsupported=True))
        run_child = (lambda name, arguments: execute_reviewed_child(registry, name, arguments)) if reviewed else registry.execute
        # NEO 187: a Stop pressed during the preflight above belongs to this
        # plan. An explicitly bound worker keeps its generation (no reset that
        # would revive it); an unbound caller resets only after this check.
        cancel.raise_if_cancelled()
        if not cancel.is_explicitly_bound():
            cancel.reset()
        if action == "queue":
            results = []
            for name, arguments in clean:
                cancel.raise_if_cancelled()
                started = time.perf_counter()
                result = run_child(name, arguments)
                latency_profiler().record(f"tool:{name}", time.perf_counter() - started, "queue")
                results.append({"tool": name, "success": result.success, "message": result.text, "data": result.data})
                if not result.success and bool(args.get("stop_on_error", True)):
                    break
            ok = all(x["success"] for x in results)
            return ToolExecution(ok, result_text(code, action, results, len(clean)), results)
        if action == "parallel":
            if not config.get('automation.parallel_enabled', True):
                raise ValueError(plan_error(code))
            results: list[dict[str, Any]] = []
            workers = max(1, min(4, int(config.get('automation.max_parallel', 4) or 4), len(clean)))
            # NEO 184: every child runs in this plan's cancellation generation. Without the bind a
            # pool thread read the newest generation, so «стоп» followed by a new command let the
            # old children carry on while the plan already reported «отменено».
            generation = cancel.current_generation()

            def bound_child(name: str, arguments: dict[str, Any]) -> ToolExecution:
                cancel.bind(generation)
                cancel.raise_if_cancelled()
                return run_child(name, arguments)

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jarvis-parallel") as pool:
                futures = {pool.submit(bound_child, name, arguments): (index, name, arguments, time.perf_counter()) for index, (name, arguments) in enumerate(clean)}
                for future in as_completed(futures):
                    index, name, _arguments, started = futures[future]
                    cancel.raise_if_cancelled()
                    result = future.result()
                    latency_profiler().record(f"tool:{name}", time.perf_counter() - started, "parallel")
                    results.append({"index": index, "tool": name, "success": result.success, "message": result.text, "data": result.data})
            results.sort(key=lambda row: row.pop('index'))
            return ToolExecution(all(x["success"] for x in results), result_text(code, action, results, len(clean)), results)
        raise ValueError(f"Неизвестное automation action: {action}")

    registry.register(ToolDefinition(
        "automation", "Управление длинными операциями: немедленная отмена, очередь команд и безопасные параллельные действия.",
        _schema({"action": _enum(["cancel", "queue", "parallel"], "Операция"), "reason": {"type": "string"},
                 "calls": {"type": "array", "items": {"type": "object", "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["tool", "arguments"]}},
                 "stop_on_error": {"type": "boolean"}}, ["action"]),
        automation, lambda args: Risk.SAFE if args.get("action") == "cancel" else Risk.MEDIUM,
    ))

    # --- HARDWARE ---
    def hardware(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "status") or "status")
        if action != "status":
            raise ValueError("Поддерживается hardware.status")
        data = hardware_average(int(args.get("samples", 3) or 3))
        target = str(args.get("target", "all") or "all").casefold()
        if target == "cpu":
            temp = data.get("cpu_temp_c"); freq = data.get("cpu_freq_mhz"); load = data.get("cpu_percent"); power = data.get("cpu_power_w")
            text = f"CPU сейчас: нагрузка {load:.0f}%, частота {freq:.0f} МГц"
            if isinstance(temp, (int,float)): text += f", температура {temp:.0f}°C"
            else: text += ", температура недоступна"
            if isinstance(power, (int,float)): text += f", потребление {power:.1f} Вт"
            avg=[]
            if data.get("avg_cpu_percent") is not None: avg.append(f"нагрузка {data['avg_cpu_percent']:.0f}%")
            if data.get("avg_cpu_freq_mhz") is not None: avg.append(f"частота {data['avg_cpu_freq_mhz']:.0f} МГц")
            if data.get("avg_cpu_temp_c") is not None: avg.append(f"температура {data['avg_cpu_temp_c']:.0f}°C")
            if data.get("avg_cpu_power_w") is not None: avg.append(f"потребление {data['avg_cpu_power_w']:.1f} Вт")
            if avg: text += ". Среднее за короткую выборку: " + ", ".join(avg)
        elif target == "gpu":
            text = f"GPU {data.get('gpu_name') or ''} сейчас: нагрузка {data.get('gpu_load_percent') if data.get('gpu_load_percent') is not None else 'н/д'}%"
            if data.get("gpu_temp_c") is not None: text += f", температура {data['gpu_temp_c']:.0f}°C"
            if data.get("gpu_clock_mhz") is not None: text += f", частота {data['gpu_clock_mhz']:.0f} МГц"
            if data.get("gpu_power_w") is not None: text += f", потребление {data['gpu_power_w']:.1f} Вт"
            if data.get("vram_used_mb") is not None and data.get("vram_total_mb") is not None: text += f", VRAM {data['vram_used_mb']:.0f}/{data['vram_total_mb']:.0f} МБ"
            avg=[]
            for key,label,fmt in (("avg_gpu_load_percent","нагрузка",".0f"),("avg_gpu_temp_c","температура",".0f"),("avg_gpu_clock_mhz","частота",".0f"),("avg_gpu_power_w","потребление",".1f")):
                if data.get(key) is not None:
                    suffix="%" if "load" in key else "°C" if "temp" in key else " МГц" if "clock" in key else " Вт"
                    avg.append(f"{label} {format(data[key],fmt)}{suffix}")
            if avg: text += ". Среднее: " + ", ".join(avg)
        elif target == "ram":
            text = f"RAM сейчас: {data.get('ram_percent',0):.0f}% — {data.get('ram_used_gb',0):.1f} из {data.get('ram_total_gb',0):.1f} ГБ"
        else:
            text = f"CPU {data.get('cpu_percent',0):.0f}% · RAM {data.get('ram_percent',0):.0f}%"
            if data.get("gpu_load_percent") is not None: text += f" · GPU {data['gpu_load_percent']:.0f}%"
            if data.get("fps") is not None: text += f" · FPS {data['fps']:.0f}"
        return ToolExecution(True, text, data)

    registry.register(ToolDefinition(
        "hardware", "Локальный мониторинг CPU/GPU/RAM/VRAM: текущие и усреднённые нагрузка, частоты, температура и потребление при наличии источника.",
        _schema({"action": _enum(["status"], "Операция"), "target": _enum(["all", "cpu", "gpu", "ram"], "Цель"), "samples": {"type": "integer", "minimum": 1, "maximum": 8}}, ["action"]),
        hardware, lambda _: Risk.SAFE,
    ))

    # --- SMART FILE SEARCH ---
    def search_tool(args: dict[str, Any]) -> ToolExecution:
        from jarvis.language import language_code
        action = str(args.get("action", "files") or "files")
        query = str(args.get("query", "") or "").strip()
        if not query:
            raise ValueError("Пустой поисковый запрос")
        roots = resolve_search_roots(str(args.get("root", "") or ""), config=config)
        if action not in {'files', 'content'}:
            raise ValueError(f"Неизвестный search action: {action}")
        rows = run_search(query, action=action, roots=roots, language=language_code(config),
                          limit=int(args.get('limit', 30 if action == 'content' else 50) or 50),
                          literal=bool(args.get('literal', False)))
        incomplete = bool(getattr(rows, 'incomplete', False))
        data = {'results': list(rows), 'complete': not incomplete,
                'continuable': bool(getattr(rows, 'continuable', False)),
                'indexing': bool(getattr(rows, 'indexing', False)),
                'roots': list(getattr(rows, 'roots', []))}
        return ToolExecution(bool(rows) or incomplete, format_search_results(rows, language_code(config)), data)

    registry.register(ToolDefinition(
        "search", "Локальный поиск файлов и папок по имени/времени и поиск текста внутри документов. Возвращает полные пути; не открывает файлы.",
        _schema({"action": _enum(["files", "content"], "Режим"), "query": {"type": "string"},
                 "root": {"type": "string", "description": "Папка поиска: полный путь или системное имя, например Downloads; без поля поиск по локальным папкам."},
                 "literal": {"type": "boolean", "description": "Имя в кавычках: слова вроде Yesterday являются частью имени, а не фильтром даты."},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["action", "query"]),
        search_tool, lambda _: Risk.SAFE,
    ))

    # --- CLIPBOARD ---
    def clipboard(args: dict[str, Any]) -> ToolExecution:
        import pyautogui
        action = str(args.get("action", "") or "")
        history = clipboard_history()
        if action == "current":
            value = history.capture()
            return ToolExecution(True, "Текущий буфер прочитан.", {"text": value})
        if action == "list":
            rows = history.list(int(args.get("limit", 10) or 10))
            return ToolExecution(True, f"В истории буфера: {len(rows)}", rows)
        if action == "set":
            history.set(str(args.get("text", "") or ""))
            return ToolExecution(True, "Текст помещён в буфер.")
        if action == "clear":
            history.clear(); return ToolExecution(True, "Буфер обмена очищен.")
        if action in {"paste_history", "copy_history"}:
            offset = max(0, int(args.get("offset", 0) or 0))
            value = history.get(offset)
            history.set(value)
            if action == "paste_history":
                pyautogui.hotkey("ctrl", "v")
                return ToolExecution(True, f"Вставлен элемент истории #{offset + 1}.", {"text": value})
            return ToolExecution(True, f"Элемент истории #{offset + 1} возвращён в текущий буфер.", {"text": value})
        if action == "copy_page_url":
            import pyautogui
            previous = pyperclip.paste()
            pyautogui.hotkey("ctrl", "l"); time.sleep(0.08); pyautogui.hotkey("ctrl", "c"); time.sleep(0.08)
            value = str(pyperclip.paste() or "").strip(); pyautogui.press("esc")
            if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
                pyperclip.copy(previous); raise RuntimeError("Не удалось подтвердить адрес страницы")
            history.capture(); return ToolExecution(True, "Адрес текущей страницы скопирован.", {"url": value})
        raise ValueError(f"Неизвестное clipboard action: {action}")

    registry.register(ToolDefinition(
        "clipboard", "История буфера обмена: текущий текст, предыдущие элементы, вставка N-го предыдущего элемента, очистка и копирование URL страницы.",
        _schema({"action": _enum(["current", "list", "set", "clear", "paste_history", "copy_history", "copy_page_url"], "Операция"), "text": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "maximum": 49}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["action"]),
        clipboard, lambda args: Risk.MEDIUM if args.get("action") in {"set", "clear", "paste_history"} else Risk.SAFE,
    ))

    # --- EMAIL: Gmail API + Mail.ru IMAP/SMTP ---
    def _open_url(url: str) -> bool:
        # REC-SEMANTIC-SOURCE-01: webbrowser.open may refuse without raising.
        # The admission result is returned; a refusal is never "opened".
        return bool(webbrowser.open(url, new=0, autoraise=True))

    def _provider_label(provider: str) -> str:
        return "Gmail" if str(provider).casefold() == "gmail" else "Mail.ru"

    def _mail_requested_providers(provider: str) -> list[str]:
        value = str(provider or "auto").casefold()
        if value in {"gmail", "mailru"}:
            return [value]
        rows = enabled_providers(config)
        if not rows:
            raise RuntimeError("Ни Gmail, ни Mail.ru не подключены")
        return rows

    def _mail_rows_text(rows: list[MailMessage], *, provider: str, detail: str | None = None) -> str:
        mode = str(detail or display_detail_for_provider(config, provider) or "sender")
        if mode == "count_only":
            return f"Найдено писем: {len(rows)}."
        lines: list[str] = []
        for index, item in enumerate(rows, 1):
            sender = str(getattr(item, "sender", "") or getattr(item, "sender_email", "") or "Неизвестный отправитель")
            subject = str(getattr(item, "subject", "") or "(без темы)")
            preview = str(getattr(item, "preview", "") or "").strip()
            folder = str(getattr(item, "folder", "") or "").strip()
            folder_prefix = f"[{folder}] " if folder else ""
            marker = " • непрочитано" if bool(getattr(item, "unread", False)) else ""
            if mode == "sender_subject":
                line = f"{index}. {folder_prefix}{sender} — {subject}{marker}"
            elif mode == "sender_preview":
                line = f"{index}. {folder_prefix}{sender}{marker}"
                if preview:
                    line += f"\n   {preview[:260]}"
            elif mode == "sender_subject_preview":
                line = f"{index}. {folder_prefix}{sender} — {subject}{marker}"
                if preview:
                    line += f"\n   {preview[:260]}"
            else:
                line = f"{index}. {folder_prefix}{sender}{marker}"
            lines.append(line)
        return "\n".join(lines)

    def _mail_mixed_rows_text(rows: list[MailMessage], *, detail: str | None = None) -> str:
        # 186: a provider whose chosen detail is «count only» contributes just its
        # count to the combined list; no sender/subject is shown for it.
        lines: list[str] = []
        counts: dict[str, int] = {}
        listed: list[tuple[MailMessage, str, str]] = []
        for item in rows:
            actual = str(getattr(item, "provider", "") or "gmail")
            mode = str(detail or display_detail_for_provider(config, actual) or "sender")
            if mode == "count_only":
                counts[actual] = counts.get(actual, 0) + 1
                continue
            listed.append((item, actual, mode))
        for index, (item, actual, mode) in enumerate(listed, 1):
            rendered = _mail_rows_text([item], provider=actual, detail=mode)
            rendered = re.sub(r"^1\.\s*", "", rendered, count=1)
            lines.append(f"{index}. {_provider_label(actual)} · {rendered}")
        for actual, count in counts.items():
            lines.append(f"{_provider_label(actual)} · Найдено писем: {count}.")
        return "\n".join(lines)

    def _mail_folder_breakdown(status: Any, metric: str) -> list[str]:
        rows = list(getattr(status, "folders", None) or [])
        if not rows:
            return []
        labels = {
            "total": "всего",
            "unread": "непрочитанных",
            "recent24h": "за 24 ч",
            "today": "за сегодня",
        }
        word = labels.get(metric, metric)
        return [f"• {getattr(row, 'name', 'Папка')}: {word} {int(getattr(row, metric, 0) or 0)}" for row in rows]

    def _mail_metric_text(actual: str, status: Any, action: str) -> str:
        """Render mailbox counters as a scan-friendly multi-line card."""
        label = _provider_label(actual)
        metrics = {
            "total": int(getattr(status, "total", 0) or 0),
            "unread": int(getattr(status, "unread", 0) or 0),
            "recent24h": int(getattr(status, "recent24h", 0) or 0),
            "today": int(getattr(status, "today", 0) or 0),
        }
        lines = [f"{label}"]
        if action == "unread":
            lines.append(f"Непрочитанные: {metrics['unread']}")
            metric = "unread"
        elif action == "recent24h":
            lines.append(f"За последние 24 часа: {metrics['recent24h']}")
            metric = "recent24h"
        elif action == "today":
            lines.append(f"Сегодня: {metrics['today']}")
            metric = "today"
        elif action == "total":
            lines.append(f"Всего писем: {metrics['total']}")
            metric = "total"
        else:
            lines.extend([
                f"Всего писем: {metrics['total']}",
                f"Непрочитанные: {metrics['unread']}",
                f"За последние 24 часа: {metrics['recent24h']}",
                f"Сегодня: {metrics['today']}",
            ])
            metric = ""
        folder_rows = list(getattr(status, "folders", None) or [])
        if folder_rows:
            lines.append("")
            lines.append("Папки:")
            for row in folder_rows:
                name = str(getattr(row, "name", "Папка") or "Папка")
                if metric:
                    value = int(getattr(row, metric, 0) or 0)
                    labels = {"total":"всего", "unread":"непрочитанные", "recent24h":"за 24 часа", "today":"сегодня"}
                    lines.append(f"• {name}: {labels.get(metric, metric)} — {value}")
                else:
                    lines.extend([
                        f"• {name}",
                        f"  Всего: {int(getattr(row, 'total', 0) or 0)}",
                        f"  Непрочитанные: {int(getattr(row, 'unread', 0) or 0)}",
                        f"  За 24 часа: {int(getattr(row, 'recent24h', 0) or 0)}",
                        f"  Сегодня: {int(getattr(row, 'today', 0) or 0)}",
                    ])
        return "\n".join(lines)

    def _mail_status_for(client: Any, folder: str) -> Any:
        if not folder:
            return client.status(include_latest=5)
        row = client._folder_status(folder)  # both providers expose the same bounded folder primitive
        return type("OneFolderStatus", (), {
            "total": row.total, "unread": row.unread, "recent24h": row.recent24h, "today": row.today,
            "folders": [row], "compact": lambda self: {
                "provider": "", "total": row.total, "unread": row.unread,
                "recent24h": row.recent24h, "today": row.today,
                "folders": [row.compact()], "latest": [], "unread_latest": [],
            }
        })()

    def _mail_latest_across(providers: list[str], *, query: str = "", folder: str = "", full: bool = False, limit: int = 10) -> list[MailMessage]:
        rows: list[MailMessage] = []
        per_provider = max(3, min(25, int(limit)))
        for actual in providers:
            client = provider_client(config, actual)
            rows.extend(client.list_messages(query=query, limit=per_provider, full=full, folder=folder or None))
        rows.sort(key=lambda item: float(getattr(item, "received_ts", 0.0) or 0.0), reverse=True)
        seen: set[tuple[str, str]] = set()
        out: list[MailMessage] = []
        for item in rows:
            key = (str(item.provider), str(item.message_id))
            if key in seen:
                continue
            seen.add(key); out.append(item)
        return out

    def email(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "status") or "status").casefold().strip()
        # HOTFIX106 compatibility aliases: local/older models sometimes emit
        # count-style names although the stable public action is simply unread.
        action = {
            "unread_count": "unread", "count_unread": "unread",
            "unread_total": "unread", "unread_today_count": "unread_today",
            "recent24h_count": "recent24h", "today_count": "today",
            "total_count": "total",
        }.get(action, action)
        provider = str(args.get("provider", "auto") or "auto")
        folder = str(args.get("folder", "") or "").strip()
        if action == "providers":
            rows = enabled_providers(config)
            text = "Подключённая почта: " + (", ".join(_provider_label(x) for x in rows) if rows else "нет")
            return ToolExecution(True, text, {"providers": rows})
        if action == "folders":
            blocks: list[str] = []
            data: dict[str, Any] = {}
            for actual in _mail_requested_providers(provider):
                client = provider_client(config, actual)
                folders = client.list_folders()
                data[actual] = folders
                blocks.append(_provider_label(actual) + ": " + (", ".join(folders) if folders else "папки не найдены"))
            return ToolExecution(True, "\n".join(blocks), data)
        if action == "open_inbox":
            providers = _mail_requested_providers(provider)
            opened: list[str] = []
            refused: list[str] = []
            for actual in providers:
                url = "https://mail.google.com/mail/u/0/#inbox" if actual == "gmail" else "https://e.mail.ru/inbox/"
                (opened if _open_url(url) else refused).append(actual)
            from jarvis.language import language_code as _lang187
            words = {
                "ru": ("Браузер не принял запрос на открытие: {x}.", "Отправил браузеру запрос открыть: {x}.", " Не принято: {x}."),
                "en": ("The browser did not accept the request to open: {x}.", "Asked the browser to open: {x}.", " Not accepted: {x}."),
                "de": ("Der Browser hat das Öffnen nicht angenommen: {x}.", "Browser gebeten zu öffnen: {x}.", " Nicht angenommen: {x}."),
                "fr": ("Le navigateur a refusé d’ouvrir : {x}.", "Demande d’ouverture envoyée au navigateur : {x}.", " Refusé : {x}."),
                "es": ("El navegador no aceptó abrir: {x}.", "Pedí al navegador que abra: {x}.", " No aceptado: {x}."),
                "it": ("Il browser non ha accettato l’apertura: {x}.", "Ho chiesto al browser di aprire: {x}.", " Non accettato: {x}."),
                "pt": ("O navegador não aceitou abrir: {x}.", "Pedi ao navegador para abrir: {x}.", " Não aceite: {x}."),
            }[_lang187(config)]
            if not opened:
                return ToolExecution(False, words[0].format(x=", ".join(_provider_label(x) for x in refused)),
                                     {"providers": [], "refused": refused, "verified": False, "localized186": True})
            text = words[1].format(x=", ".join(_provider_label(x) for x in opened))
            if refused:
                text += words[2].format(x=", ".join(_provider_label(x) for x in refused))
            # A browser admission is not a verified page load.
            return ToolExecution(True, text, {"providers": opened, "refused": refused, "requested": True, "localized186": True})

        # Generic read-only questions intentionally aggregate every enabled mailbox.
        # Explicit Gmail/Mail.ru wording narrows the same action to one provider.
        if action in {"unread_today", "unread_recent24h"}:
            blocks: list[str] = []
            payload: dict[str, Any] = {}
            for actual in _mail_requested_providers(provider):
                client = provider_client(config, actual)
                counter = client.count_unread_today if action == "unread_today" else client.count_unread_recent24h
                label = "сегодня" if action == "unread_today" else "за последние 24 часа"
                count = int(counter(folder=folder or None))
                lines = [f"{_provider_label(actual)}: непрочитанных писем {label} {count}."]
                folder_counts: list[dict[str, Any]] = []
                source_folders = [folder] if folder else list(client.selected_folders())
                for name in source_folders:
                    try:
                        value = int(counter(folder=str(name)))
                    except Exception:
                        continue
                    folder_counts.append({"name": str(name), "count": value})
                    lines.append(f"• {name}: {value}")
                blocks.append("\n".join(lines))
                payload[actual] = {"count": count, "period": label, "folder": folder, "folders": folder_counts}
            return ToolExecution(True, "\n\n".join(blocks), payload)

        if action in {"status", "unread", "recent24h", "today", "total"}:
            blocks: list[str] = []
            payload: dict[str, Any] = {}
            for actual in _mail_requested_providers(provider):
                client = provider_client(config, actual)
                status = _mail_status_for(client, folder)
                blocks.append(_mail_metric_text(actual, status, action))
                payload[actual] = status.compact()
            return ToolExecution(True, "\n\n".join(blocks), payload)

        if action in {"unread_list", "unread_today_list", "unread_recent24h_list", "recent24h_list", "today_list", "latest", "search"}:
            limit = max(1, min(25, int(args.get("limit", 10) or 10)))
            detail_override = str(args.get("detail", "") or "").strip() or None
            query = str(args.get("query", "") or "").strip()
            sender = str(args.get("sender", "") or "").strip()
            if sender:
                query = f"from:{sender}"
            providers = _mail_requested_providers(provider)
            if action == "latest" and len(providers) > 1:
                rows = _mail_latest_across(providers, query=query, folder=folder, full=False, limit=limit)[:limit]
                if not rows:
                    return ToolExecution(True, "Gmail + Mail.ru: писем не найдено.", {"messages": []})
                return ToolExecution(
                    True,
                    "Последние письма\n" + _mail_mixed_rows_text(rows, detail=detail_override),
                    {"messages": [x.compact() for x in rows]},
                )
            blocks: list[str] = []
            payload: dict[str, Any] = {}
            for actual in providers:
                client = provider_client(config, actual)
                if action == "unread_list":
                    rows = client.list_messages(query="is:unread", limit=limit, folder=folder or None)
                    heading = f"{_provider_label(actual)}: непрочитанные письма"
                elif action == "unread_today_list":
                    rows = client.list_unread_today(limit=limit, folder=folder or None)
                    heading = f"{_provider_label(actual)}: непрочитанные письма за сегодня"
                elif action == "unread_recent24h_list":
                    rows = client.list_unread_recent24h(limit=limit, folder=folder or None)
                    heading = f"{_provider_label(actual)}: непрочитанные письма за последние 24 часа"
                elif action == "recent24h_list":
                    rows = client.list_recent24h(limit=limit, folder=folder or None)
                    heading = f"{_provider_label(actual)}: письма за последние 24 часа"
                elif action == "today_list":
                    rows = client.list_today(limit=limit, folder=folder or None)
                    heading = f"{_provider_label(actual)}: письма за сегодня"
                else:
                    rows = client.list_messages(query=query, limit=limit, folder=folder or None)
                    heading = f"{_provider_label(actual)}: " + ("последние письма" if action == "latest" else "найденные письма")
                payload[actual] = [x.compact() for x in rows]
                if rows:
                    blocks.append(heading + "\n" + _mail_rows_text(rows, provider=actual, detail=detail_override))
                else:
                    blocks.append(heading + ": не найдено.")
            return ToolExecution(True, "\n\n".join(blocks), payload)

        if action == "read":
            message_id = str(args.get("message_id", "") or "").strip()
            providers = _mail_requested_providers(provider)
            if message_id and len(providers) == 1:
                actual = providers[0]
                item = provider_client(config, actual).get_message(message_id, full=True, folder=folder)
            else:
                query = str(args.get("query", "") or "").strip()
                rows = _mail_latest_across(providers, query=query, folder=folder, full=True, limit=3)
                if not rows:
                    raise RuntimeError("Письмо не найдено")
                item = rows[0]; actual = str(item.provider)
            text = f"{_provider_label(actual)} · {item.sender}\nТема: {item.subject or '(без темы)'}\nПапка: {item.folder or 'выбранная'}\n\n{item.body or item.preview or '(пустое письмо)'}"
            return ToolExecution(True, text, item.compact())

        # Mutating actions use an explicitly named provider when supplied. For a
        # generic request, keep the configured default-provider behavior.
        client = provider_client(config, provider)
        actual = "gmail" if client.__class__.__name__.startswith("Gmail") else "mailru"
        label = _provider_label(actual)
        if action == "mark_read":
            message_id = str(args.get("message_id", "") or "").strip()
            if not message_id: raise ValueError("Не указан message_id")
            client.mark_read(message_id)
            return ToolExecution(True, f"{label}: письмо отмечено прочитанным.", {"provider": actual, "message_id": message_id})
        if action in {"send", "draft"}:
            to = str(args.get("to", "") or "").strip()
            subject = str(args.get("subject", "") or "").strip()
            body = str(args.get("body", "") or "").strip()
            if action == "draft":
                url = "https://mail.google.com/mail/u/0/#inbox?compose=new" if actual == "gmail" else "https://e.mail.ru/compose/"
                if not _open_url(url):
                    return ToolExecution(False, f"Браузер не принял запрос на создание письма в {label}.", {"provider": actual, "url": url, "verified": False})
                return ToolExecution(True, f"Открыл создание письма в {label}.", {"provider": actual, "url": url, "requested": True})
            if not to: raise ValueError("Не указан получатель")
            if not body: raise ValueError("Текст письма пуст")
            result = client.send(to, subject, body)
            return ToolExecution(True, f"{label}: письмо отправлено на {to}.", result)
        if action == "reply":
            message_id = str(args.get("message_id", "") or "").strip()
            body = str(args.get("body", "") or "").strip()
            if not body: raise ValueError("Текст ответа пуст")
            if not message_id:
                rows = client.list_messages(query=str(args.get("query", "") or ""), limit=1, full=True, folder=folder or None)
                if not rows: raise RuntimeError("Исходное письмо не найдено")
                original = rows[0]
            else:
                original = client.get_message(message_id, full=True, folder=folder)
            to = original.sender_email or original.sender
            if not to: raise RuntimeError("Не удалось определить адрес отправителя")
            subject = original.subject or ""
            if not subject.casefold().startswith("re:"):
                subject = "Re: " + subject
            result = client.send(to, subject, body, reply_to=original)
            return ToolExecution(True, f"{label}: ответ отправлен {to}.", result)
        raise ValueError(f"Неизвестное email action: {action}")

    registry.register(ToolDefinition(
        "email",
        "Полная почтовая интеграция Gmail и Mail.ru. Generic read-only запросы агрегируют все включённые почтовые сервисы; явное Gmail/Mail.ru ограничивает запрос одним сервисом. Поддерживает выбранные папки/labels, сегодня, 24 часа, всё время, непрочитанные, поиск, чтение, отправку и ответы.",
        _schema({
            "action": _enum(["providers", "folders", "open_inbox", "status", "unread", "unread_list", "unread_today", "unread_today_list", "unread_recent24h", "unread_recent24h_list", "recent24h", "recent24h_list", "today", "today_list", "total", "latest", "search", "read", "mark_read", "draft", "send", "reply"], "Операция"),
            "provider": _enum(["auto", "gmail", "mailru"], "Почтовый сервис"),
            "folder": {"type": "string", "description": "Необязательная конкретная папка/ярлык"},
            "detail": _enum(["count_only", "sender", "sender_subject", "sender_preview", "sender_subject_preview"], "Необязательное явное переопределение формата"),
            "sender": {"type": "string"},
            "query": {"type": "string"},
            "message_id": {"type": "string"},
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        }, ["action"]),
        email,
        lambda args: Risk.MEDIUM if args.get("action") in {"send", "reply", "mark_read"} else Risk.SAFE,
    ))

    # --- NEO PROCESS CONTROL ---
    def jarvis_control(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "status") or "status").strip().casefold()
        if action == "status":
            return ToolExecution(True, "NEO работает.", {"running": True})
        if action in {"shutdown", "quit", "exit"}:
            from jarvis.runtime.ui_commands import request_quit_application
            import threading
            # Give Telegram/desktop enough time to publish the acknowledgement,
            # then enter the normal Qt shutdown path (workers, logs and state).
            timer = threading.Timer(0.35, request_quit_application)
            timer.daemon = True
            timer.start()
            return ToolExecution(True, "NEO завершает работу.", {"shutdown_requested": True})
        raise ValueError(f"Неизвестное действие управления NEO: {action}")

    registry.register(ToolDefinition(
        "jarvis",
        "Управление самим процессом NEO: состояние и полное завершение ассистента.",
        _schema({"action": _enum(["status", "shutdown"], "Операция")}, ["action"]),
        jarvis_control,
        lambda args: Risk.CRITICAL if str(args.get("action", "")).casefold() == "shutdown" else Risk.SAFE,
    ))

    # --- SCENARIOS ---
    def scenario(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "") or "")
        if action == "list":
            rows = config.get("scenarios.items", []) or []
            return ToolExecution(True, f"Сценариев: {len(rows)}", rows)
        if action in ("run", "retry"):
            from jarvis.ui.scenario_strings import st
            from jarvis.language import language_code
            code = language_code(config)
            if not args.get("fingerprint"):
                return ToolExecution(False, st("changed", code), {"status": "approval_required"})
            if action == "retry":
                from jarvis.runtime.scenarios import retry_scenario
                result = retry_scenario(config, args)
            else:
                result = run_scenario(config, str(args.get("id") or args.get("name", "") or ""), expected_fingerprint=args["fingerprint"],
                                      inputs=args.get("inputs"), expected_inputs=args.get("inputs_fingerprint"))
            return ToolExecution(result.success, f"{result.name}: {st(result.status, code)}", {"name": result.name, "status": result.status, "steps": result.steps, "run_id": result.run_id, "error": result.error, "summary": result.summary, "outcomes": result.outcomes, "retry_of": result.retry_of})
        raise ValueError(f"Неизвестное scenario action: {action}")

    registry.register(ToolDefinition(
        "scenario", "Пользовательские сценарии из Настройки → Сценарии: кодовая фраза запускает набор шагов открытия/ожидания/команд.",
        _schema({"action": _enum(["list", "run", "retry"], "Операция"), "name": {"type": "string"}, "id": {"type": "string"}, "fingerprint": {"type": "string"}, "run_id": {"type": "string"}, "step_index": {"type": "integer", "minimum": 1, "maximum": 100}, "inputs": {"type": "object", "description": "NEO 188: значения именованных параметров этого запуска"}, "inputs_fingerprint": {"type": "string"}}, ["action"]),
        scenario, lambda args: scenario_risk(config,args),
    ))

    # --- HISTORY + UNDO ---
    def history(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "recent") or "recent")
        if action == "recent":
            rows = db.recent_actions(int(args.get("limit", 20) or 20))
            return ToolExecution(True, f"Последних действий: {len(rows)}", rows)
        if action == "undo":
            count = max(1, min(20, int(args.get("count", 1) or 1)))
            in_memory = undo_manager().undo(count)
            if in_memory:
                return ToolExecution(all(x.get("success") for x in in_memory), f"Отменено операций: {len(in_memory)}", in_memory)
            # Existing filesystem backup mechanism remains a safe fallback.
            result = registry.execute("filesystem", {"action": "undo_last_change"})
            return ToolExecution(result.success, result.text, result.data)
        raise ValueError(f"Неизвестное history action: {action}")

    registry.register(ToolDefinition(
        "history", "История действий NEO и Undo. Для новых окон/вкладок использует reversible stack, для файлов — существующие backup/undo.",
        _schema({"action": _enum(["recent", "undo"], "Операция"), "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "count": {"type": "integer", "minimum": 1, "maximum": 20}}, ["action"]),
        history, lambda args: Risk.MEDIUM if args.get("action") == "undo" else Risk.SAFE,
    ))

    # --- MULTI-WINDOW OBJECT MODEL / INSTANCE OPERATIONS ---
    def _process_name_for_hwnd(hwnd: int) -> str:
        if os.name != "nt" or not hwnd:
            return ""
        try:
            import ctypes
            from ctypes import wintypes
            pid = wintypes.DWORD(0)
            ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            return str(psutil.Process(int(pid.value)).name() or "").casefold() if pid.value else ""
        except Exception:
            return ""

    def _window_instances(query: str) -> list[dict[str, Any]]:
        if os.name != "nt":
            raise OSError("Windows-only")
        import pygetwindow as gw
        q = normalize_target(query)
        browser_procs = {"chrome.exe", "firefox.exe", "msedge.exe", "opera.exe", "opera_gx.exe", "browser.exe", "brave.exe", "vivaldi.exe"}
        rows: list[tuple[float, dict[str, Any]]] = []
        for w in gw.getAllWindows():
            title = str(getattr(w, "title", "") or "").strip()
            if not title:
                continue
            hwnd = int(getattr(w, "_hWnd", 0) or 0)
            proc = _process_name_for_hwnd(hwnd)
            proc_label = proc.removesuffix(".exe").replace("_", " ")
            if q in {"browser", "браузер", "chrome", "хром"} and q in {"browser", "браузер"}:
                score = 1.0 if proc in browser_procs else 0.0
            else:
                score = max(target_similarity(q, title), target_similarity(q, proc_label))
                if q in {"chrome", "хром", "google chrome"} and proc == "chrome.exe": score = 1.0
                if q in {"firefox", "mozilla", "мозила", "мозилла"} and proc == "firefox.exe": score = 1.0
            if score < 0.55:
                continue
            contents: list[str] = []
            if proc in browser_procs:
                try:
                    from pywinauto import Desktop
                    win = Desktop(backend="uia").window(handle=hwnd)
                    contents = [str(x.window_text() or "").strip() for x in win.descendants(control_type="TabItem") if str(x.window_text() or "").strip()][:80]
                except Exception:
                    contents = []
            rows.append((score, {
                "hwnd": hwnd, "title": title, "process": proc,
                "left": int(getattr(w,"left",0)), "top": int(getattr(w,"top",0)),
                "width": int(getattr(w,"width",0)), "height": int(getattr(w,"height",0)),
                "contents": contents, "_wrapper": w,
            }))
        rows.sort(key=lambda x: (-x[0], x[1]["left"], x[1]["top"], x[1]["title"].casefold()))
        return [x[1] for x in rows]

    def _move_instance_to_monitor(row: dict[str, Any], monitor_number: int) -> dict[str, Any]:
        if os.name != "nt": raise OSError("Windows-only")
        from jarvis.display_mapping import windows_monitor_geometries
        geoms = windows_monitor_geometries()
        if not geoms:
            raise RuntimeError("Мониторы не найдены")
        target_geom = next((x for x in geoms if int(x[0]) == int(monitor_number)), None)
        if target_geom is None and 1 <= int(monitor_number) <= len(geoms):
            target_geom = geoms[int(monitor_number)-1]
        if target_geom is None:
            raise ValueError(f"Монитор {monitor_number} не найден")
        _num, rect, _device = target_geom
        left, top, right, bottom = map(int, rect)
        w = row["_wrapper"]
        old = {k: row[k] for k in ("hwnd","title","left","top","width","height")}
        old["maximized"] = bool(getattr(w,"isMaximized",False)); old["minimized"] = bool(getattr(w,"isMinimized",False))
        try:
            if old["maximized"] or old["minimized"]: w.restore(); time.sleep(0.06)
        except Exception: pass
        width=max(320,min(max(320,int(row.get("width") or 1000)),int((right-left)*0.92)))
        height=max(240,min(max(240,int(row.get("height") or 700)),int((bottom-top)*0.92)))
        x=left+(right-left-width)//2; y=top+(bottom-top-height)//2
        w.moveTo(x,y); w.resizeTo(width,height)
        if old["maximized"]:
            time.sleep(0.05); w.maximize()
        def _undo():
            current = next((r for r in _window_instances(str(old["title"])) if int(r.get("hwnd",0)) == int(old["hwnd"])), None)
            if current is None: raise RuntimeError("Окно для Undo не найдено")
            cw=current["_wrapper"]
            try: cw.restore()
            except Exception: pass
            cw.moveTo(int(old["left"]),int(old["top"])); cw.resizeTo(max(100,int(old["width"])),max(100,int(old["height"])))
            if old["maximized"]: cw.maximize()
            elif old["minimized"]: cw.minimize()
            return {"restored":True,"title":old["title"]}
        undo_manager().push(f"Вернуть окно «{old['title']}»", "window", old, _undo)
        return {"title": str(w.title), "monitor": monitor_number, "x":x,"y":y,"width":width,"height":height,"hwnd":row["hwnd"]}

    def window_advanced(args: dict[str, Any]) -> ToolExecution:
        action=str(args.get("action","") or "")
        query=str(args.get("query","") or "browser").strip()
        rows=_window_instances(query)
        public=lambda r:{k:v for k,v in r.items() if not k.startswith("_")}
        if action=="list_instances":
            return ToolExecution(True,f"Найдено окон: {len(rows)}",[dict(public(r),index=i+1) for i,r in enumerate(rows)])
        if action in {"focus_instance","move_instance"}:
            index=max(1,int(args.get("index",1) or 1))
            if index>len(rows): return ToolExecution(False,f"У {query} нет окна №{index}.",{"count":len(rows)})
            row=rows[index-1]; w=row["_wrapper"]
            if action=="focus_instance":
                try: w.restore()
                except Exception: pass
                w.activate(); desktop_objects().remember(DesktopObject("window",f"{normalize_target(query)}#{index}",str(row["title"]),{"index":index,"hwnd":row["hwnd"],"process":row["process"]}))
                return ToolExecution(True,f"Активировал окно №{index}: {row['title']}",public(row))
            data=_move_instance_to_monitor(row,int(args.get("monitor",1) or 1))
            desktop_objects().remember(DesktopObject("window",f"{normalize_target(query)}#{index}",str(row["title"]),{"index":index,**data}))
            return ToolExecution(True,f"Перенёс окно №{index} «{row['title']}» на монитор {data['monitor']}.",data)
        if action in {"close_with_content","keep_only_content"}:
            content=str(args.get("content","") or "").strip()
            if not content: raise ValueError("Не указан content")
            matched=[]
            for row in rows:
                hay=[row["title"],*row.get("contents",[])]
                score=max((target_similarity(content,x) for x in hay if x),default=0.0)
                if score>=0.72: matched.append((score,row))
            matched.sort(key=lambda x:x[0],reverse=True)
            if action=="close_with_content":
                if not matched: return ToolExecution(False,f"Не нашёл окно {query} с содержимым «{content}».",{"safe_failure":True})
                row=matched[0][1]; row["_wrapper"].close()
                return ToolExecution(True,f"Закрыл окно «{row['title']}» с содержимым «{content}».",public(row))
            keep_hwnd=int(matched[0][1]["hwnd"]) if matched else 0
            if not keep_hwnd: return ToolExecution(False,f"Не нашёл окно, которое нужно оставить: {content}.",{"safe_failure":True})
            closed=[]
            for row in rows:
                if int(row["hwnd"])==keep_hwnd: continue
                try: row["_wrapper"].close(); closed.append(row["title"])
                except Exception: pass
            return ToolExecution(True,f"Оставил окно с «{content}», закрыл остальных: {len(closed)}.",{"kept":matched[0][1]["title"],"closed":closed})
        raise ValueError(f"Неизвестное window_advanced action: {action}")

    registry.register(ToolDefinition(
        "window_advanced", "Модель нескольких окон одного приложения: список экземпляров, фокус/перенос N-го окна, закрытие окна по содержимому и оставить только окно с нужной вкладкой/контентом.",
        _schema({"action":_enum(["list_instances","focus_instance","move_instance","close_with_content","keep_only_content"],"Операция"),"query":{"type":"string"},"index":{"type":"integer","minimum":1,"maximum":50},"monitor":{"type":"integer","minimum":1,"maximum":16},"content":{"type":"string"}},["action"]),
        window_advanced, lambda args: Risk.MEDIUM if args.get("action") in {"move_instance","close_with_content","keep_only_content"} else Risk.SAFE,
    ))

    # --- ADVANCED BROWSER CONTEXT/TABS ---
    def _active_browser_window():
        if os.name != "nt": raise OSError("Windows-only")
        from pywinauto import Desktop
        import ctypes
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd: raise RuntimeError("Нет активного окна")
        window = Desktop(backend="uia").window(handle=hwnd)
        try:
            proc = str(psutil.Process(int(window.element_info.process_id)).name() or "").casefold()
        except Exception:
            proc = ""
        browsers = {"chrome.exe","firefox.exe","msedge.exe","opera.exe","opera_gx.exe","browser.exe","brave.exe","vivaldi.exe"}
        if proc not in browsers:
            # If another app is foreground, prefer any already-open browser rather
            # than sending Ctrl+Shift+T/context-menu keys into the wrong program.
            tabs = list_browser_tabs("")
            if not tabs:
                raise RuntimeError("Не вижу открытого окна браузера")
            return tabs[0].window
        return window

    def _context_menu_action(labels: tuple[str, ...]) -> bool:
        import pyautogui
        window = _active_browser_window()
        tabs = [x for x in window.descendants(control_type="TabItem") if x.window_text().strip()]
        selected = next((x for x in tabs if bool(getattr(x, "is_selected", lambda: False)())), tabs[0] if tabs else None)
        if selected is None: return False
        try:
            rect = selected.rectangle(); pyautogui.rightClick((rect.left + rect.right)//2, (rect.top + rect.bottom)//2); time.sleep(0.25)
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
            for item in desktop.windows(visible_only=True):
                try:
                    for ctrl in item.descendants(control_type="MenuItem"):
                        name = str(ctrl.window_text() or "").casefold()
                        if any(label.casefold() in name for label in labels):
                            ctrl.click_input(); return True
                except Exception:
                    continue
        finally:
            pass
        return False

    def browser_advanced(args: dict[str, Any]) -> ToolExecution:
        import pyautogui
        action = str(args.get("action", "") or "")
        browser = _browser_scope(str(args.get("browser", "") or ""))
        if action == "context":
            tabs = list_browser_tabs(browser)
            selected = selected_browser_tab_context(browser)
            data = {"selected": selected, "tabs": [{"index": i+1, "title": t.title, "url": t.url, "selected": t.selected} for i,t in enumerate(tabs)]}
            return ToolExecution(True, f"Открыто вкладок: {len(tabs)}", data)
        if action == "restore_closed":
            pyautogui.hotkey("ctrl", "shift", "t"); return ToolExecution(True, "Отправил штатную команду восстановления закрытой вкладки.")
        if action == "close_others":
            tabs = list_browser_tabs(browser); selected = selected_browser_tab_context(browser)
            before = len(tabs)
            if before <= 1: return ToolExecution(True, "Открыта только одна вкладка.")
            if not _context_menu_action(("close other tabs", "закрыть другие вкладки")):
                return ToolExecution(False, "Браузер не показал штатный пункт «Закрыть другие вкладки». NEO не будет закрывать вкладки циклом наугад.", {"before": before, "safe_failure": True})
            time.sleep(0.35); after = len(list_browser_tabs(browser))
            return ToolExecution(after == 1, "Оставил только текущую вкладку." if after == 1 else f"Осталось вкладок: {after}", {"before": before, "after": after})
        if action == "pin_current":
            query = str(args.get("query", "") or "").strip()
            if query:
                activated = activate_tab_by_title(query, browser)
                if not bool(activated.get("verified", True)):
                    return ToolExecution(False, f"Не удалось подтвердить вкладку «{query}». Закрепление отменено.", {"safe_failure": True, "activation": activated})
            ok = _context_menu_action(("pin tab", "закрепить вкладку", "закрепить"))
            return ToolExecution(ok, "Вкладка закреплена." if ok else "Не удалось найти штатный пункт закрепления вкладки в текущем браузере.")
        if action == "move_to_new_window":
            query = str(args.get("query", "") or "").strip()
            if query:
                activated = activate_tab_by_title(query, browser)
                if not bool(activated.get("verified", True)):
                    return ToolExecution(False, f"Не удалось подтвердить вкладку «{query}». Перенос отменён.", {"safe_failure": True, "activation": activated})
            ok = _context_menu_action(("move tab to new window", "переместить вкладку в новое окно", "move tab"))
            return ToolExecution(ok, "Вкладка перемещена в новое окно." if ok else "Браузер не открыл доступный пункт переноса вкладки.")
        if action == "activate_by_topic":
            query = str(args.get("query", "") or "").strip()
            data = activate_tab_by_title(query, browser)
            return ToolExecution(bool(data.get("verified", True)), f"Перешёл на вкладку: {query}", data)
        if action == "group_by_topic":
            # Native tab groups are browser-specific. Use only an exposed context
            # menu action; never synthesize screen coordinates for unknown builds.
            ok = _context_menu_action(("add tab to new group", "добавить вкладку в новую группу", "add tab to group", "добавить вкладку в группу"))
            return ToolExecution(ok, "Открыл штатное создание группы вкладок. Название/цвет можно продиктовать в появившемся поле." if ok else "Текущий браузер не предоставил доступный пункт группировки вкладок. Ничего не изменено.", {"safe_failure": not ok, "capability": "native_tab_group_menu"})
        raise ValueError(f"Неизвестное browser_advanced action: {action}")

    registry.register(ToolDefinition(
        "browser_advanced", "Контекст вкладок и расширенные операции: вкладка по теме, закрыть остальные, закрепить, восстановить закрытую, перенести в новое окно. Ненадёжные UI действия завершаются безопасной ошибкой.",
        _schema({"action": _enum(["context", "activate_by_topic", "close_others", "restore_closed", "pin_current", "move_to_new_window", "group_by_topic"], "Операция"), "query": {"type": "string"}, "browser": {"type": "string"}}, ["action"]),
        browser_advanced, lambda args: Risk.MEDIUM if args.get("action") in {"close_others", "move_to_new_window"} else Risk.SAFE,
    ))

    # --- VOICE TEXT EDITING ANYWHERE ---
    def text_edit(args: dict[str, Any]) -> ToolExecution:
        import pyautogui
        action = str(args.get("action", "") or "")
        if action == "dictate":
            text = str(args.get("text", "") or "")
            previous = pyperclip.paste()
            try:
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.05)
            finally:
                try: pyperclip.copy(previous)
                except Exception: pass
            return ToolExecution(True, f"Ввёл текст: {len(text)} символов.")
        if action == "select_previous_paragraph":
            pyautogui.hotkey("ctrl", "shift", "up"); return ToolExecution(True, "Выделил предыдущий абзац.")
        if action in {"fix_selection", "replace_last_sentence"}:
            previous = pyperclip.paste()
            if action == "replace_last_sentence":
                pyautogui.hotkey("ctrl", "shift", "left"); pyautogui.hotkey("ctrl", "c")
            else:
                pyautogui.hotkey("ctrl", "c")
            time.sleep(0.08); selected = str(pyperclip.paste() or "")
            if not selected.strip(): pyperclip.copy(previous); raise RuntimeError("Нет выделенного текста")
            try:
                # Explicit replacement remains fully supported without a model.
                # Unqualified correction only normalizes spacing: never pretend
                # a deterministic formatter is a universal grammar checker.
                fixed=str(args.get('text',''))
                explicit=bool(fixed)
                if not explicit:
                    fixed=re.sub(r'[ \t]+([,.;:!?])',r'\1',selected)
                    fixed=re.sub(r'([,;!?])(?=[^\s\d])',r'\1 ',fixed)
                    fixed=re.sub(r'[ \t]{2,}',' ',fixed)
                if fixed==selected:
                    return ToolExecution(True,"Текст не изменён. Для смысловой правки продиктуйте новый текст.",{'changed':False,'mode':'deterministic'})
                pyperclip.copy(fixed); pyautogui.hotkey("ctrl", "v"); time.sleep(0.05)
                return ToolExecution(True,"Выделение заменено." if explicit else "Пробелы и отступы у знаков препинания исправлены.",{'changed':True,'mode':'explicit' if explicit else 'spacing'})
            finally:
                pyperclip.copy(previous)
        raise ValueError(f"Неизвестное text_edit action: {action}")

    registry.register(ToolDefinition(
        "text_edit", "Голосовое редактирование текста в текущем приложении: диктовка, выделение предыдущего абзаца, исправление выделения/последнего фрагмента.",
        _schema({"action": _enum(["dictate", "select_previous_paragraph", "fix_selection", "replace_last_sentence"], "Операция"), "text": {"type": "string"}, "instruction": {"type": "string"}}, ["action"]),
        text_edit, lambda _: Risk.MEDIUM,
    ))

    # --- SMART CLIPBOARD BETWEEN APPS / FILE ATTACH ---
    def transfer(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "") or "")
        if action == "clipboard_to_discord":
            text = clipboard_history().capture()
            if not text: raise RuntimeError("Буфер пуст")
            result = registry.execute("discord", {"action": "send_message", "text": text, "server": str(args.get("server", "") or ""), "channel": str(args.get("channel", "") or "")})
            return ToolExecution(result.success, result.text, result.data)
        if action == "page_url_to_discord":
            # Copy the current page URL through the browser's own address bar, then
            # hand the verified URL to the existing Discord send_message action.
            topic = str(args.get("topic", "") or "").strip()
            if topic:
                try: registry.execute("browser_advanced", {"action": "activate_by_topic", "query": topic})
                except Exception: pass
            copied = registry.execute("clipboard", {"action": "copy_page_url"})
            if not copied.success:
                return copied
            payload = copied.data if isinstance(copied.data, dict) else {}
            url = str(payload.get("url", "") or clipboard_history().capture()).strip()
            if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
                return ToolExecution(False, "Адрес страницы не подтверждён; сообщение в Discord не отправлено.", {"safe_failure": True})
            result = registry.execute("discord", {"action": "send_message", "text": url, "server": str(args.get("server", "") or ""), "channel": str(args.get("channel", "") or "")})
            return ToolExecution(result.success, result.text, {"url": url, "discord": result.data})
        if action == "last_screenshot_to_discord":
            shots=[]
            try:
                from jarvis.runtime.screenshot_storage import latest_screenshot
                newest=latest_screenshot(config.paths.screenshots)      # HIST-066: app folders too
                shots=[newest] if newest is not None else []
            except Exception:
                shots=[]
            if not shots:
                return ToolExecution(False, "Нет сохранённых скриншотов для отправки.", {"safe_failure": True})
            nested=dict(args); nested["action"]="attach_file_discord"; nested["path"]=str(shots[0])
            return transfer(nested)
        if action == "selected_explorer_file_to_discord":
            if os.name != "nt":
                raise OSError("Выбранный файл Проводника поддерживается только в Windows")
            import ctypes
            import pyautogui
            from ctypes import wintypes
            hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            if not hwnd:
                return ToolExecution(False, "Нет активного окна Проводника.", {"safe_failure": True})
            pid = wintypes.DWORD(0)
            ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            try: foreground_process = str(psutil.Process(int(pid.value)).name() or "").casefold()
            except Exception: foreground_process = ""
            if foreground_process != "explorer.exe":
                return ToolExecution(False, "Текущим окном является не Проводник. Ничего не прикреплено.", {"safe_failure": True, "process": foreground_process})
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.18)
            script = "$x=Get-Clipboard -Format FileDropList -ErrorAction SilentlyContinue; if($x){$x | Select-Object -First 1 -ExpandProperty FullName}"
            read_drop = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                capture_output=True, text=True, timeout=5, check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
            )
            selected = [x.strip() for x in str(read_drop.stdout or "").splitlines() if x.strip()]
            path = Path(selected[0]) if selected else Path("__missing__")
            if read_drop.returncode != 0 or not path.is_file():
                return ToolExecution(False, "Не удалось подтвердить выбранный файл в Проводнике. Ничего не отправлено.", {"safe_failure": True})
            nested = dict(args); nested["action"] = "attach_file_discord"; nested["path"] = str(path)
            return transfer(nested)
        if action == "attach_file_discord":
            path = Path(str(args.get("path", "") or "")).expanduser()
            if not path.is_file(): raise FileNotFoundError(path)
            # Discord documents Ctrl+Shift+U as upload file.  Fill the native file chooser.
            result = registry.execute("discord", {"action": "open_text_channel", "server": str(args.get("server", "") or ""), "channel": str(args.get("channel", "") or "")})
            if not result.success: return result
            import pyautogui
            previous_clip = pyperclip.paste()
            pyautogui.hotkey("ctrl", "shift", "u"); time.sleep(0.55); pyperclip.copy(str(path)); pyautogui.hotkey("ctrl", "v"); pyautogui.press("enter")
            try: pyperclip.copy(previous_clip)
            except Exception: pass
            time.sleep(0.8)
            # Confirm the attachment preview is visible in the real Discord window
            # before sending; no blind second Enter when upload did not attach.
            visible=False
            try:
                from pywinauto import Desktop
                for win in Desktop(backend="uia").windows(visible_only=True):
                    try:
                        pid=int(win.element_info.process_id); proc=str(psutil.Process(pid).name() or "").casefold()
                        if not proc.startswith("discord"): continue
                        texts=[str(x.window_text() or "") for x in win.descendants()]
                        if any(path.name.casefold() in x.casefold() for x in texts): visible=True; break
                    except Exception: continue
            except Exception: pass
            if not visible:
                return ToolExecution(False, f"Discord открыт, но превью вложения {path.name} не подтверждено. Отправка отменена.", {"path":str(path),"verified":False,"safe_failure":True})
            pyautogui.press("enter"); time.sleep(0.4)
            return ToolExecution(True, f"Файл прикреплён и отправлен в Discord: {path.name}.", {"path": str(path), "verified": True})
        raise ValueError(f"Неизвестное transfer action: {action}")

    registry.register(ToolDefinition(
        "transfer", "Связывает приложения: clipboard/URL текущей страницы/последний скриншот/выбранный файл Проводника → Discord, а также безопасное прикрепление конкретного файла.",
        _schema({"action": _enum(["clipboard_to_discord", "page_url_to_discord", "last_screenshot_to_discord", "selected_explorer_file_to_discord", "attach_file_discord"], "Операция"), "server": {"type": "string"}, "channel": {"type": "string"}, "path": {"type": "string"}, "topic": {"type": "string"}}, ["action"]),
        transfer, lambda _: Risk.MEDIUM,
    ))

    # --- SELF TEST / PLUGINS / LATENCY ---
    def diagnostics(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "") or "")
        if action == "self_test":
            data = run_self_test(config, quick=bool(args.get("quick", True)))
            from jarvis.runtime.self_test_localization import render_report
            return ToolExecution(bool(data.get("ok")), render_report(data, str(config.get("app.language","en"))), data)
        if action == "latency":
            rows = latency_profiler().recent(int(args.get("limit", 30) or 30))
            return ToolExecution(True, latency_profiler().summary(), rows)
        if action == "plugins":
            manager = getattr(getattr(registry,"_registry",registry),"plugin_manager",None)
            return ToolExecution(True, "Список плагинов получен.", manager.status() if manager else [])
        if action == "objects":
            return ToolExecution(True, "Контекст объектов рабочего стола получен.", desktop_objects().snapshot())
        raise ValueError(f"Неизвестное diagnostics action: {action}")

    registry.register(ToolDefinition(
        "diagnostics", "Self-Test без движения мыши, latency profiler, список plugins и текущая модель объектов рабочего стола.",
        _schema({"action": _enum(["self_test", "latency", "plugins", "objects"], "Операция"), "quick": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 300}}, ["action"]),
        diagnostics, lambda _: Risk.SAFE,
    ))
