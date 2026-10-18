# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from jarvis.gaming.normalize import norm

logger = logging.getLogger(__name__)

CACHE_SCHEMA = 6  # 186: punctuation-preserving keys (C/C++/C# no longer collide)
CACHE_PROVENANCE = "searchapi_google_facts_v2"
VALID_PROVIDERS = {"searchapi_google"}

_DYNAMIC_RE = re.compile(
    r"\b(?:сейчас|сегодня|актуаль\w*|последн\w*|нов(?:ый|ая|ое|ые)\w*|патч\w*|мета\w*|"
    r"current|today|latest|newest|patch|meta|hotfix|update)\b",
    re.I,
)
# SQ-09: locale-aware temporal markers and volatile topics (all seven languages,
# straight or curly apostrophes) share one short freshness policy.
_TEMPORAL186 = re.compile(
    r"(?:^|[^\w])(?:сейчас|сегодня|завтра|вчера|текущ\w*|актуальн\w*|последн\w*|свеж\w*|"
    r"now|today|tonight|tomorrow|yesterday|current\w*|latest|this\s+week|"
    r"heute|morgen|gestern|jetzt|aktuell\w*|neueste\w*|"
    r"aujourd'hui|demain|hier|maintenant|actuel\w*|dernier\w*|derni[eè]re\w*|"
    r"hoy|mañana|manana|ayer|ahora|actual\w*|últim\w*|ultim\w*|"
    r"oggi|domani|ieri|adesso|attual\w*|"
    r"hoje|amanhã|amanha|ontem|agora|atual\w*)(?=[^\w]|$)",
    re.I,
)
_VOLATILE186 = re.compile(
    r"(?:погод\w*|температур\w*|прогноз\w*|курс\w*|котировк\w*|акци[яи]\b|биткоин\w*|новост\w*|счет\s+матча|счёт\s+матча|"
    r"weather|forecast|temperature|exchange\s+rate|rate\s+of|stock\s+price|bitcoin|news|score|"
    r"wetter|vorhersage|wechselkurs|nachrichten|météo|meteo|prévisions|taux\s+de\s+change|actualités|nouvelles|"
    r"tiempo\s+(?:hace|en)|pronóstico|tipo\s+de\s+cambio|noticias|previsioni|cambio|notizie|"
    r"previsão|câmbio|cotação|notícias)",
    re.I,
)
_RELATIVE_DAY186 = re.compile(
    r"(?:^|[^\w])(?:сегодня|завтра|вчера|какое\s+(?:сегодня\s+)?число|today|tomorrow|yesterday|heute|morgen|gestern|"
    r"aujourd'hui|demain|hier|hoy|mañana|ayer|oggi|domani|ieri|hoje|amanhã|ontem)(?=[^\w]|$)",
    re.I,
)


def _fold186(value: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", str(value or "")).casefold().replace("’", "'").replace("ё", "е")


def normalize_question(value: str) -> str:
    return norm(value)


def cache_key_text(value: str) -> str:
    """SQ-08: search-cache identity keeps significant symbols (C, C++, C#, 2.0,
    URLs); only the search verb, case, ё/е, spacing and closing punctuation of
    an equivalent request shell are ignored."""
    from jarvis.runtime.web_relevance186 import canonical_question
    return canonical_question(value)


class GameWebCache:
    """Small JSON library containing only Google AI-backed answers.

    This is deliberately not a gameplay database. Each record is a cached live-web
    answer with provenance and expiry. Older retired-provider cache entries
    are rejected so a retired provider can never leak into the Google AI runtime.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.base = Path(config.paths.cache) / "game_web"
        self.root = self.base / "answers"
        self.root.mkdir(parents=True, exist_ok=True)
        self.purge_legacy_entries()

    def _game_dir(self, game_id: str) -> Path:
        safe = re.sub(r"[^a-z0-9_.-]+", "_", str(game_id or "generic").casefold()).strip("_") or "generic"
        path = self.root / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _key(self, game_id: str, question: str) -> str:
        language = str(self.config.get("app.language", "ru") or "ru")
        country = str(self.config.get("gaming.web_knowledge.searchapi_gl", "us") or "us")
        payload = f"v186\0{language}\0{country}\0{str(game_id or '').casefold()}\0{cache_key_text(question)}".encode("utf-8", errors="ignore")
        return hashlib.sha256(payload).hexdigest()

    def _entry_path(self, game_id: str, question: str) -> Path:
        return self._game_dir(game_id) / f"{self._key(game_id, question)}.json"

    def ttl_seconds(self, question: str) -> int:
        folded = _fold186(question)
        if _DYNAMIC_RE.search(norm(question)) or _TEMPORAL186.search(folded) or _VOLATILE186.search(folded):
            minutes = int(self.config.get("gaming.web_knowledge.dynamic_cache_ttl_minutes", 60) or 60)
            return max(5, min(720, minutes)) * 60
        hours = int(self.config.get("gaming.web_knowledge.cache_ttl_hours", 72) or 72)
        return max(1, min(24 * 30, hours)) * 3600

    @staticmethod
    def day_boundary(question: str, created: float) -> float:
        if not _RELATIVE_DAY186.search(_fold186(question)):
            return 0.0
        import datetime as _dt
        moment = _dt.datetime.fromtimestamp(float(created))
        midnight = (moment + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except Exception:
            return None

    @staticmethod
    def _valid_row(row: dict[str, Any] | None) -> bool:
        if not isinstance(row, dict):
            return False
        return (
            int(row.get("schema", 0) or 0) == CACHE_SCHEMA
            and str(row.get("provenance") or "") == CACHE_PROVENANCE
            and str(row.get("provider") or "") in VALID_PROVIDERS
            and bool(str(row.get("answer") or "").strip())
        )

    def purge_legacy_entries(self) -> int:
        """Delete obsolete answer-cache JSON and the retired NEO Google profile."""
        removed = 0
        # Old V7.0/V7.1 cache lived directly under game_web/<game>/*.json.
        try:
            for child in self.base.iterdir():
                if not child.is_dir() or child.name == "answers":
                    continue
                if child.name == "google_ai_profile":
                    try:
                        shutil.rmtree(child); removed += 1
                    except OSError:
                        pass
                    continue
                for p in child.glob("*.json"):
                    try:
                        p.unlink(); removed += 1
                    except OSError:
                        pass
        except Exception:
            pass
        # Also remove malformed/old-schema entries that somehow entered answers/.
        try:
            for p in self.root.glob("*/*.json"):
                if not self._valid_row(self._read(p)):
                    try:
                        p.unlink(); removed += 1
                    except OSError:
                        pass
        except Exception:
            pass
        if removed:
            logger.info("GameWeb invalidated %d retired-provider cache/artifact item(s)", removed)
        return removed

    def get(self, game_id: str, question: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
        qn = normalize_question(question)
        if not qn:
            return None
        now = time.time()
        path = self._entry_path(game_id, question)
        row = self._read(path) if path.exists() else None
        if row is not None and not self._valid_row(row):
            try: path.unlink()
            except OSError: pass
            row = None
        # Similar factual questions can have opposite answers (dates, negation,
        # names). Exact, language-specific cache matching is intentional.
        if not self._valid_row(row):
            return None
        expires = float(row.get("expires_at", 0.0) or 0.0)
        stale_until = float(row.get("stale_until", 0.0) or 0.0)
        created = float(row.get("created_at", 0.0) or 0.0)
        if created > 0:
            requested_expiry = created + self.ttl_seconds(question)
            expires = min(expires, requested_expiry) if expires else requested_expiry
            day_end = self.day_boundary(question, created)
            if day_end:
                # «today/tomorrow» answers expire with their local calendar day.
                expires = min(expires, day_end)
        if expires and now <= expires:
            row["cache_stale"] = False
        elif allow_stale and stale_until and now <= stale_until:
            row["cache_stale"] = True
        else:
            return None
        try:
            row["last_used_at"] = now
            row["hit_count"] = int(row.get("hit_count", 0) or 0) + 1
            self._atomic_write(path, row)
        except Exception:
            pass
        return row

    def _find_similar(self, game_id: str, qn: str) -> dict[str, Any] | None:
        game_dir = self._game_dir(game_id)
        candidates: list[tuple[float, Path, dict[str, Any]]] = []
        try:
            files = sorted(game_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:160]
        except Exception:
            files = []
        q_tokens = set(qn.split())
        for path in files:
            row = self._read(path)
            if not self._valid_row(row):
                continue
            other = normalize_question(str(row.get("question") or ""))
            if not other:
                continue
            o_tokens = set(other.split())
            union = q_tokens | o_tokens
            jaccard = (len(q_tokens & o_tokens) / len(union)) if union else 0.0
            ratio = SequenceMatcher(None, qn, other).ratio()
            score = max(ratio, 0.55 * ratio + 0.45 * jaccard)
            if score >= 0.92 and jaccard >= 0.72:
                candidates.append((score, path, row))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        row = dict(candidates[0][2])
        row["_path"] = str(candidates[0][1])
        row["cache_similarity"] = candidates[0][0]
        return row

    def put(
        self, game_id: str, game_name: str, question: str, *, search_query: str,
        answer: str, sources: list[dict[str, Any]], provider: str,
        related_questions: list[str] | None = None, continuation_token: str = "",
        suggested_followup: str = "", grounded: bool | None = None, answer_kind: str = "",
    ) -> dict[str, Any]:
        provider = str(provider or "")
        if provider not in VALID_PROVIDERS:
            raise ValueError(f"GameWeb cache refuses non-Google AI provider: {provider!r}")
        now = time.time()
        ttl = self.ttl_seconds(question)
        stale_days = int(self.config.get("gaming.web_knowledge.stale_cache_days", 14) or 14)
        row = {
            "schema": CACHE_SCHEMA,
            "provenance": CACHE_PROVENANCE,
            "game_id": str(game_id or "generic"),
            "language": str(self.config.get("app.language", "ru") or "ru"),
            "game_name": str(game_name or ""),
            "question": str(question or "").strip(),
            "normalized_question": normalize_question(question),
            "search_query": str(search_query or "").strip(),
            "answer": str(answer or "").strip(),
            "provider": provider,
            "sources": list(sources or [])[:10],
            "related_questions": [str(x).strip() for x in (related_questions or []) if str(x).strip()][:5],
            "continuation_token": str(continuation_token or ""),
            "suggested_followup": str(suggested_followup or "").strip()[:220],
            # The support level recorded with the answer; a cache hit restores it
            # instead of upgrading it.
            "grounded": bool(sources) if grounded is None else bool(grounded),
            "answer_kind": str(answer_kind or ""),
            "created_at": now,
            "last_used_at": now,
            "hit_count": 0,
            "expires_at": now + ttl,
            "stale_until": now + max(ttl, max(1, min(90, stale_days)) * 86400),
        }
        path = self._entry_path(game_id, question)
        self._atomic_write(path, row)
        self.prune()
        return row

    @staticmethod
    def _atomic_write(path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(row, fh, ensure_ascii=False, indent=2)
                fh.flush(); os.fsync(fh.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                if os.path.exists(temp_name): os.remove(temp_name)
            except OSError:
                pass

    def prune(self) -> None:
        max_mb = int(self.config.get("gaming.web_knowledge.cache_max_mb", 100) or 100)
        max_bytes = max(10, min(1024, max_mb)) * 1024 * 1024
        files: list[tuple[float, int, Path]] = []
        total = 0
        try:
            for p in self.root.glob("*/*.json"):
                st = p.stat(); files.append((st.st_mtime, st.st_size, p)); total += st.st_size
        except Exception:
            return
        if total <= max_bytes:
            return
        files.sort(key=lambda x: x[0])
        for _mtime, size, path in files:
            try: path.unlink(); total -= size
            except OSError: pass
            if total <= int(max_bytes * 0.90): break

    def stats(self) -> dict[str, int]:
        return self._stats_at(self.root)

    @classmethod
    def read_stats(cls, config: Any) -> dict[str, int]:
        """Read-only statistics without constructor migrations or directory writes."""
        return cls._stats_at(Path(config.paths.cache) / "game_web" / "answers")

    @classmethod
    def _stats_at(cls, root: Path) -> dict[str, int]:
        count = size = 0
        try:
            for p in root.glob("*/*.json"):
                if not cls._valid_row(cls._read(p)):
                    continue
                st = p.stat(); count += 1; size += st.st_size
        except Exception:
            pass
        return {"entries": count, "bytes": size}

    def clear(self) -> int:
        removed = 0
        for p in list(self.root.glob("*/*.json")):
            try: p.unlink(); removed += 1
            except OSError: pass
        return removed
