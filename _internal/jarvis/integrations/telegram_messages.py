# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Telegram personal-message center backed by official Business/Secretary updates.

The existing Telegram bot remains the remote-control transport.  When Secretary
Mode is enabled in BotFather and the same bot is connected to the user's account,
Bot API business_connection/business_message updates are mirrored here.  This
module stores only a bounded local history and never treats personal messages as
JARVIS commands.
"""

from pathlib import Path
from typing import Any
import json
import re
import threading
import time

import requests

from jarvis.secrets import telegram_bot_token


def _norm(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[^\w@.+-]+", " ", text, flags=re.U)
    return re.sub(r"\s+", " ", text).strip()


def _person_token_stem(token: str) -> str:
    value = _norm(token).strip("@")
    if not value:
        return ""
    # Conservative Russian case/adjective endings. This is deliberately not a
    # fuzzy edit-distance matcher: it only normalizes grammatical inflection so
    # «Ивану Грозному» can match an allow-listed «Иван Грозный».
    endings = (
        "иями", "ями", "ами", "ому", "ему", "ого", "его", "ыми", "ими",
        "ую", "юю", "ой", "ей", "ым", "им", "ий", "ый", "ая", "яя",
        "ии", "ию", "ия", "ью", "ью", "ом", "ем", "ов", "ев",
        "у", "ю", "е", "а", "я", "й",
    )
    for suffix in endings:
        if len(value) - len(suffix) >= 3 and value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def _person_signature(value: str) -> tuple[str, ...]:
    normalized = _norm(value)
    if normalized.startswith("@") and " " not in normalized:
        return (normalized,)
    return tuple(x for x in (_person_token_stem(t) for t in normalized.split()) if x)


def _sender_name(user: dict[str, Any]) -> str:
    first = str(user.get("first_name", "") or "").strip()
    last = str(user.get("last_name", "") or "").strip()
    username = str(user.get("username", "") or "").strip()
    name = " ".join(x for x in (first, last) if x).strip()
    return name or (f"@{username}" if username else str(user.get("id", "") or "Telegram"))


def _message_text(message: dict[str, Any]) -> str:
    text = str(message.get("text", "") or message.get("caption", "") or "").strip()
    if text:
        return text
    if message.get("photo"):
        return "[фото]"
    if message.get("video"):
        return "[видео]"
    if message.get("voice"):
        return "[голосовое сообщение]"
    if message.get("audio"):
        return "[аудио]"
    if message.get("document"):
        return "[документ]"
    if message.get("sticker"):
        return "[стикер]"
    if message.get("location"):
        return "[геолокация]"
    return "[сообщение]"


class TelegramMessageCenter:
    def __init__(self, config: Any) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._path = Path(config.paths.root) / "telegram_business_messages.json"
        self._state = self._load()
        # HOTFIX105: persistent HTTPS pool for Secretary/Business sends.
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0)
        self._session.mount("https://", adapter)

    def reconfigure(self, config: Any) -> None:
        self.config = config

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("connections", {})
                raw.setdefault("messages", [])
                raw.setdefault("bot_groups", {})
                raw.setdefault("first_seen_at", 0.0)
                raw.setdefault("last_business_update_at", 0.0)
                raw.setdefault("last_connection_update_at", 0.0)
                return raw
        except Exception:
            pass
        return {"connections": {}, "messages": [], "bot_groups": {}, "first_seen_at": 0.0, "last_business_update_at": 0.0, "last_connection_update_at": 0.0}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass

    def record_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        connection_id = str(payload.get("id", "") or "").strip()
        if not connection_id:
            return {}
        user = dict(payload.get("user") or {}) if isinstance(payload.get("user"), dict) else {}
        rights = dict(payload.get("rights") or {}) if isinstance(payload.get("rights"), dict) else {}
        row = {
            "id": connection_id,
            "owner_user_id": str(user.get("id", "") or ""),
            "owner_name": _sender_name(user),
            "user_chat_id": str(payload.get("user_chat_id", "") or ""),
            "is_enabled": bool(payload.get("is_enabled", False)),
            "can_reply": bool(rights.get("can_reply", False)),
            "rights": rights,
            "updated_at": time.time(),
        }
        with self._lock:
            connections = self._state.setdefault("connections", {})
            connections[connection_id] = row
            self._state["last_connection_update_at"] = time.time()
            self._save()
        return row

    def connection(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            return dict((self._state.get("connections") or {}).get(str(connection_id or "")) or {})

    def record_bot_group(self, chat: dict[str, Any]) -> dict[str, Any]:
        chat_type = str(chat.get("type", "") or "").casefold()
        chat_id = str(chat.get("id", "") or "").strip()
        title = str(chat.get("title", "") or "").strip()
        if chat_type not in {"group", "supergroup"} or not chat_id or not title:
            return {}
        row = {"chat_id": chat_id, "title": title, "chat_type": chat_type, "updated_at": time.time()}
        with self._lock:
            self._state.setdefault("bot_groups", {})[chat_id] = row
            self._save()
        return row

    def bot_groups(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(x) for x in (self._state.get("bot_groups") or {}).values() if isinstance(x, dict)]
        rows.sort(key=lambda x: str(x.get("title", "")).casefold())
        return rows

    def record_message(self, message: dict[str, Any]) -> dict[str, Any]:
        connection_id = str(message.get("business_connection_id", "") or "").strip()
        sender = dict(message.get("from") or {}) if isinstance(message.get("from"), dict) else {}
        chat = dict(message.get("chat") or {}) if isinstance(message.get("chat"), dict) else {}
        sender_id = str(sender.get("id", "") or "")
        with self._lock:
            connection = dict((self._state.get("connections") or {}).get(connection_id) or {})
        owner_id = str(connection.get("owner_user_id", "") or "")
        incoming = not bool(owner_id and sender_id == owner_id)
        row = {
            "business_connection_id": connection_id,
            "message_id": int(message.get("message_id", 0) or 0),
            "chat_id": str(chat.get("id", "") or ""),
            "chat_title": str(chat.get("title", "") or ""),
            "chat_type": str(chat.get("type", "") or ""),
            "sender_id": sender_id,
            "sender_name": _sender_name(sender),
            "sender_username": str(sender.get("username", "") or ""),
            "text": _message_text(message),
            "date": int(message.get("date", time.time()) or time.time()),
            "incoming": incoming,
        }
        with self._lock:
            messages = self._state.setdefault("messages", [])
            key = (connection_id, row["chat_id"], row["message_id"])
            existing_index = next((i for i in range(max(0, len(messages)-200), len(messages)) if (str(messages[i].get("business_connection_id", "")), str(messages[i].get("chat_id", "")), int(messages[i].get("message_id", 0) or 0)) == key), None)
            if existing_index is None:
                messages.append(row)
            else:
                messages[existing_index] = row
            max_items = max(200, min(20000, int(self.config.get("integrations.telegram.messages_history_limit", 5000) or 5000)))
            if len(messages) > max_items:
                del messages[:-max_items]
            if incoming and not float(self._state.get("first_seen_at", 0.0) or 0.0):
                self._state["first_seen_at"] = float(row["date"] or time.time())
            self._state["last_business_update_at"] = time.time()
            self._save()
        return row

    def delete_messages(self, payload: dict[str, Any]) -> int:
        connection_id = str(payload.get("business_connection_id", "") or "")
        chat = dict(payload.get("chat") or {}) if isinstance(payload.get("chat"), dict) else {}
        chat_id = str(chat.get("id", "") or payload.get("chat_id", "") or "")
        ids = {int(x or 0) for x in (payload.get("message_ids") or []) if int(x or 0)}
        if not ids:
            return 0
        with self._lock:
            before = len(self._state.get("messages", []) or [])
            self._state["messages"] = [
                x for x in (self._state.get("messages", []) or [])
                if not (str(x.get("business_connection_id", "")) == connection_id and str(x.get("chat_id", "")) == chat_id and int(x.get("message_id", 0) or 0) in ids)
            ]
            removed = before - len(self._state["messages"])
            if removed:
                self._save()
            return removed

    def connection_status(self) -> dict[str, Any]:
        with self._lock:
            rows = [dict(x) for x in (self._state.get("connections") or {}).values() if isinstance(x, dict)]
        enabled = [x for x in rows if bool(x.get("is_enabled", False))]
        return {
            "connected": bool(enabled),
            "connections": enabled,
            "can_reply": any(bool(x.get("can_reply", False)) for x in enabled),
            "first_seen_at": float(self._state.get("first_seen_at", 0.0) or 0.0),
            "last_business_update_at": float(self._state.get("last_business_update_at", 0.0) or 0.0),
            "last_connection_update_at": float(self._state.get("last_connection_update_at", 0.0) or 0.0),
            "stored_messages": len(self._state.get("messages", []) or []),
        }

    def _incoming(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(x) for x in (self._state.get("messages", []) or []) if isinstance(x, dict) and bool(x.get("incoming", True))]
        rows.sort(key=lambda x: int(x.get("date", 0) or 0), reverse=True)
        return rows

    def _match_sender(self, row: dict[str, Any], sender: str) -> bool:
        wanted = _norm(sender)
        if not wanted:
            return True
        wanted_sig = _person_signature(wanted)
        candidates = [row.get("sender_name", ""), row.get("sender_username", ""), row.get("sender_id", "")]
        for candidate in candidates:
            c = _norm(str(candidate or ""))
            if not c:
                continue
            if c == wanted:
                return True
            # Usernames and numeric IDs stay exact. Human names may differ only
            # by grammatical inflection, not by arbitrary fuzzy spelling.
            if c.startswith("@") or wanted.startswith("@") or c.isdigit() or wanted.isdigit():
                continue
            if wanted_sig and _person_signature(c) == wanted_sig:
                return True
        return False

    def stats(self, *, sender: str = "", recent24h: bool = False) -> dict[str, Any]:
        rows = [x for x in self._incoming() if self._match_sender(x, sender)]
        if recent24h:
            cutoff = time.time() - 86400
            rows = [x for x in rows if int(x.get("date", 0) or 0) >= cutoff]
        by_sender: dict[str, int] = {}
        for row in rows:
            name = str(row.get("sender_name", "") or "Неизвестный отправитель")
            by_sender[name] = by_sender.get(name, 0) + 1
        return {
            "total": len(rows),
            "recent24h": sum(1 for x in self._incoming() if int(x.get("date", 0) or 0) >= time.time() - 86400),
            "by_sender": dict(sorted(by_sender.items(), key=lambda kv: (-kv[1], kv[0].casefold()))),
            "first_seen_at": float(self._state.get("first_seen_at", 0.0) or 0.0),
        }

    def recent(self, *, sender: str = "", limit: int = 10, recent24h: bool = False) -> list[dict[str, Any]]:
        rows = [x for x in self._incoming() if self._match_sender(x, sender)]
        if recent24h:
            cutoff = time.time() - 86400
            rows = [x for x in rows if int(x.get("date", 0) or 0) >= cutoff]
        return rows[:max(1, min(100, int(limit)))]

    def latest(self, sender: str = "") -> dict[str, Any] | None:
        rows = self.recent(sender=sender, limit=1)
        return rows[0] if rows else None

    @staticmethod
    def _configured_allowlist(config: Any, kind: str) -> list[str]:
        key = "outbound_allowed_groups" if kind == "group" else "outbound_allowed_people"
        raw = config.get(f"integrations.telegram.{key}", []) or []
        if isinstance(raw, str): raw = [x.strip() for x in re.split(r"[;\n]+", raw) if x.strip()]
        return [str(x).strip() for x in raw if str(x).strip()]

    def _resolve_allowed_name(self, target: str, *, kind: str) -> str:
        wanted = _norm(target)
        if not wanted:
            raise ValueError("Не указан получатель Telegram")
        allowed = self._configured_allowlist(self.config, kind)
        if kind == "group":
            exact = [x for x in allowed if _norm(x.split("=", 1)[0]) == wanted]
        else:
            exact = [x for x in allowed if _norm(x) == wanted]
            if not exact and not wanted.startswith("@"):
                sig = _person_signature(wanted)
                exact = [x for x in allowed if _person_signature(x) == sig]
        if not exact:
            raise RuntimeError(f"Получатель «{target}» не входит в разрешённый список Telegram. Добавьте его в Настройки → Telegram.")
        if len(exact) > 1:
            raise RuntimeError(f"Получатель «{target}» неоднозначен в разрешённом списке Telegram. Уточните имя.")
        return str(exact[0]).strip()

    def parse_outbound_target_and_text(self, remainder: str, *, kind: str = "person") -> tuple[str, str] | None:
        """Split an outbound command using the configured allowlist as the boundary.

        This is safer than guessing how many words belong to a person's/group's
        name. Only a configured target may consume the prefix of the utterance.
        """
        raw = str(remainder or "").strip(" ,.:;«»\"'")
        if not raw:
            return None
        allowed = self._configured_allowlist(self.config, kind)
        candidates: list[tuple[int, str, str]] = []
        raw_tokens = raw.split()
        for configured in allowed:
            display = str(configured).split("=", 1)[0].strip() if kind == "group" else str(configured).strip()
            n = len(display.split())
            if n <= 0 or len(raw_tokens) <= n:
                continue
            prefix = " ".join(raw_tokens[:n])
            if kind == "group":
                match = _norm(prefix) == _norm(display)
            else:
                match = _norm(prefix) == _norm(display) or _person_signature(prefix) == _person_signature(display)
            if not match:
                continue
            body = " ".join(raw_tokens[n:]).strip()
            body = re.sub(r"^(?:,|:|—|-)?\s*(?:то\s+что|что|сообщение|сообщением|текст)\s*[:,-]?\s*", "", body, flags=re.I).strip()
            if body:
                candidates.append((n, display, body))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        longest = candidates[0][0]
        top = [x for x in candidates if x[0] == longest]
        unique = {(x[1], x[2]) for x in top}
        if len(unique) != 1:
            return None
        _, target, body = top[0]
        return target, body

    def _outbound_target(self, target: str, *, kind: str = "person") -> dict[str, Any]:
        canonical = self._resolve_allowed_name(target, kind=kind)
        candidates: list[dict[str, Any]] = []
        if kind == "group":
            # Groups use the normal NeoBot identity. Business reply rights are
            # intentionally not assumed for group chats. The bot learns group IDs
            # from ordinary group updates/my_chat_member events and still requires
            # an exact user-configured allowlist name.
            wanted = _norm(canonical.split("=", 1)[0])
            explicit_id = canonical.split("=", 1)[1].strip() if "=" in canonical else ""
            for row in self.bot_groups():
                if (explicit_id and str(row.get("chat_id", "")) == explicit_id) or _norm(row.get("title", "")) == wanted:
                    candidates.append(row)
        else:
            wanted_sig = _person_signature(canonical)
            wanted_norm = _norm(canonical)
            seen: set[tuple[str, str]] = set()
            for row in self._incoming():
                if str(row.get("chat_type", "")).casefold() in {"group", "supergroup"}:
                    continue
                names = [str(row.get("sender_name", "") or ""), str(row.get("sender_username", "") or "")]
                matched = any(
                    _norm(x) == wanted_norm
                    or (not wanted_norm.startswith("@") and _person_signature(x) == wanted_sig)
                    for x in names if x
                )
                if matched:
                    key = (str(row.get("business_connection_id", "")), str(row.get("chat_id", "")))
                    if key not in seen:
                        seen.add(key); candidates.append(row)
        if not candidates:
            if kind == "group":
                raise RuntimeError(
                    f"Разрешённая группа «{target}» ещё не известна NeoBot. Добавьте бота в группу и отправьте там команду/упоминание один раз, затем повторите."
                )
            raise RuntimeError(f"Разрешённый получатель «{target}» ещё не наблюдался через Telegram Secretary Mode. Получите от него хотя бы одно сообщение.")
        if len(candidates) > 1:
            raise RuntimeError(f"Найдено несколько Telegram-чатов для «{target}». Отправка заблокирована во избежание ошибки.")
        return candidates[0]

    def send(self, target: str, text: str, *, kind: str = "person") -> dict[str, Any]:
        body = str(text or "").strip()
        if not body:
            raise ValueError("Текст сообщения пуст")
        row = self._outbound_target(target, kind=kind)
        token = telegram_bot_token(self.config)
        if not token:
            raise RuntimeError("Telegram Bot token не настроен")
        if kind == "group":
            chat_id = str(row.get("chat_id", "") or "")
            payload = {"chat_id": chat_id, "text": body}
            connection_id = ""
        else:
            connection_id = str(row.get("business_connection_id", "") or "")
            chat_id = str(row.get("chat_id", "") or "")
            with self._lock:
                connection = dict((self._state.get("connections") or {}).get(connection_id) or {})
            if not connection or not bool(connection.get("is_enabled", False)):
                raise RuntimeError("Telegram Business connection не активен")
            if not bool(connection.get("can_reply", False)):
                raise RuntimeError("Telegram не выдал Secretary Bot право отвечать/отправлять сообщения")
            payload = {"business_connection_id": connection_id, "chat_id": chat_id, "text": body}
        response = self._session.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=15)
        data = dict(response.json() or {}) if response.content else {}
        if response.status_code >= 400 or not bool(data.get("ok", False)):
            raise RuntimeError(str(data.get("description", "") or f"Telegram HTTP {response.status_code}"))
        result = dict(data.get("result") or {})
        return {
            "sent": True, "target": target, "kind": kind, "chat_id": chat_id,
            "business_connection_id": connection_id, "message_id": int(result.get("message_id", 0) or 0), "text": body,
        }

    def reply(self, text: str, *, sender: str = "", reply_to_message_id: int = 0) -> dict[str, Any]:
        body = str(text or "").strip()
        if not body:
            raise ValueError("Текст ответа пуст")
        target: dict[str, Any] | None = None
        if reply_to_message_id:
            for row in self._incoming():
                if int(row.get("message_id", 0) or 0) == int(reply_to_message_id):
                    target = row; break
        if target is None:
            target = self.latest(sender)
        if target is None:
            raise RuntimeError("Не найдено сообщение, на которое можно ответить")
        connection_id = str(target.get("business_connection_id", "") or "")
        chat_id = str(target.get("chat_id", "") or "")
        with self._lock:
            connection = dict((self._state.get("connections") or {}).get(connection_id) or {})
        if not connection or not bool(connection.get("is_enabled", False)):
            raise RuntimeError("Telegram Business connection не активен")
        if not bool(connection.get("can_reply", False)):
            raise RuntimeError("Telegram не выдал Secretary Bot право отвечать на сообщения")
        token = telegram_bot_token(self.config)
        if not token:
            raise RuntimeError("Telegram Bot token не настроен")
        payload: dict[str, Any] = {
            "business_connection_id": connection_id,
            "chat_id": chat_id,
            "text": body,
            "reply_parameters": {"message_id": int(target.get("message_id", 0) or 0)},
        }
        response = self._session.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=15)
        data = dict(response.json() or {}) if response.content else {}
        if response.status_code >= 400 or not bool(data.get("ok", False)):
            raise RuntimeError(str(data.get("description", "") or f"Telegram HTTP {response.status_code}"))
        result = dict(data.get("result") or {})
        return {
            "sent": True,
            "sender": str(target.get("sender_name", "") or ""),
            "chat_id": chat_id,
            "business_connection_id": connection_id,
            "message_id": int(result.get("message_id", 0) or 0),
            "text": body,
        }


def format_notification(config: Any, row: dict[str, Any]) -> str:
    detail = str(config.get("integrations.telegram.messages_notification_detail", "sender_text") or "sender_text")
    if detail == "notification_only":
        return "📨 Telegram · Новое сообщение"
    sender = str(row.get("sender_name", "") or "Неизвестный отправитель")
    if detail == "sender":
        return f"📨 Telegram · Новое сообщение от {sender}"
    text = str(row.get("text", "") or "")
    if len(text) > 260:
        text = text[:257] + "…"
    return f"📨 Telegram · {sender}\n{text}" if text else f"📨 Telegram · {sender}"


def format_briefing(config: Any, center: TelegramMessageCenter) -> str:
    total_stats = center.stats()
    recent = center.stats(recent24h=True)
    lines = ["Telegram"]
    if bool(config.get("integrations.telegram.briefing_show_recent24h", True)):
        lines.append(f"• Сообщений за последние 24 часа: {int(recent.get('total', 0) or 0)}")
    if bool(config.get("integrations.telegram.briefing_show_total", False)):
        lines.append(f"• Всего учтено сообщений: {int(total_stats.get('total', 0) or 0)}")
    if bool(config.get("integrations.telegram.briefing_show_senders", False)):
        source = recent if bool(config.get("integrations.telegram.briefing_show_recent24h", True)) else total_stats
        by_sender = dict(source.get("by_sender") or {})
        if by_sender:
            lines.append("• Отправители: " + ", ".join(f"{name} — {count}" for name, count in list(by_sender.items())[:8]))
    return "\n".join(lines)


_CENTERS: dict[str, TelegramMessageCenter] = {}
_CENTERS_LOCK = threading.Lock()


def telegram_message_center(config: Any) -> TelegramMessageCenter:
    key = str(Path(config.paths.root).resolve())
    with _CENTERS_LOCK:
        center = _CENTERS.get(key)
        if center is None:
            center = TelegramMessageCenter(config)
            _CENTERS[key] = center
        else:
            center.reconfigure(config)
        return center
