# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import logging
import math
import time
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests

from jarvis.secrets import unprotect_secret

from .models import SearchResponse, SearchResult

logger = logging.getLogger(__name__)

SEARCHAPI_SEARCH_URL = "https://www.searchapi.io/api/v1/search"
SEARCHAPI_ACCOUNT_URL = "https://www.searchapi.io/api/v1/me"


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except Exception:
        return default


def _safe_detail(response: requests.Response, limit: int = 220, secrets: tuple[str, ...] = ()) -> str:
    """Return a short diagnostic without ever exposing request headers/API keys.

    186: the owning caller passes the known credential; it is removed *before*
    whitespace normalization and clipping can break its full match, then the
    generic redactor runs, and only the cleaned text is shortened."""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            candidate = body.get("error") or body.get("message") or body.get("detail")
            if isinstance(candidate, dict):
                candidate = candidate.get("message") or candidate.get("detail") or candidate.get("type")
            detail = str(candidate or "").strip()
    except Exception:
        try:
            detail = str(response.text or "").strip()
        except Exception:
            detail = ""
    for secret in secrets:
        secret = str(secret or "")
        if len(secret) >= 4:
            detail = detail.replace(secret, "[REDACTED]")
    from jarvis.runtime.diagnostics import redact
    detail = redact(re.sub(r"\s+", " ", detail))
    return detail[:limit]


def _flatten_text_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    lines: list[str] = []

    def emit(block: Any, depth: int = 0) -> None:
        if not isinstance(block, dict):
            return
        kind = str(block.get("type") or "paragraph").casefold()
        answer = re.sub(r"\s+", " ", str(block.get("answer") or "")).strip()
        if answer:
            if kind == "header":
                lines.append(f"## {answer}")
            elif kind in {"code", "code_block"}:
                lines.extend(["```", answer, "```"])
            elif depth:
                lines.append(f"- {answer}")
            else:
                lines.append(answer)
        items = block.get("items")
        if isinstance(items, list):
            ordered = kind == "ordered_list"
            for idx, item in enumerate(items, 1):
                if isinstance(item, dict):
                    item_answer = re.sub(r"\s+", " ", str(item.get("answer") or "")).strip()
                    if item_answer:
                        prefix = f"{idx}. " if ordered else "- "
                        lines.append(prefix + item_answer)
                    nested = item.get("items")
                    if isinstance(nested, list):
                        for child in nested:
                            emit(child, depth + 1)
                else:
                    text = re.sub(r"\s+", " ", str(item or "")).strip()
                    if text:
                        prefix = f"{idx}. " if ordered else "- "
                        lines.append(prefix + text)

    for row in blocks:
        emit(row)
    # Keep paragraph boundaries while avoiding huge vertical gaps.
    return "\n\n".join(x for x in lines if x).strip()


def _url_from(row: dict[str, Any]) -> str:
    """A usable http(s) URL of one provider row, or '' (SQ-06: never raises)."""
    if not isinstance(row, dict):
        return ""
    for key in ("link", "url", "source_url"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            try:
                parsed = urlparse(candidate)
            except ValueError:
                return ""
            return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""
    return ""


def _text_field(row: dict[str, Any], *keys: str) -> str:
    """Only real text is evidence: objects, lists and booleans are schema noise."""
    for key in keys:
        value = row.get(key) if isinstance(row, dict) else None
        if isinstance(value, bool):
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and key in {"answer"}:
            return str(value)
    return ""


class SearchAPIGoogleProvider:
    """Google search facts/snippets, without an AI answer or local generation.

    SearchAPI is a retrieval API. A source excerpt is never advertised as a
    verified synthesized answer. Screenshots are never uploaded by this client.
    """

    name = "searchapi_google"

    def __init__(self, config: Any, session: requests.Session | None = None) -> None:
        self.config = config
        if session is None:
            # NEO 173: the shared session, warmed up when the wake word is heard.
            from jarvis.runtime import http_pool173
            session = http_pool173.session("searchapi")
        self.session = session
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "NEO/4.1 SearchAPI-Google",
        })

    def close(self) -> None:
        # The shared session stays open for the next search (NEO 173).
        from jarvis.runtime import http_pool173
        if self.session is not http_pool173.session("searchapi"):
            try:
                self.session.close()
            except Exception:
                pass

    def api_key_with_source(self) -> tuple[str, str]:
        # GUI/DPAPI is authoritative. Environment variables are developer/portable
        # fallbacks only, so a stale machine variable can no longer silently
        # override the key the user just saved in Settings.
        encrypted = str(self.config.get("gaming.web_knowledge.searchapi_api_key_encrypted", "") or "")
        key = unprotect_secret(encrypted).strip()
        if key:
            return key, "settings_dpapi"
        env = str(os.getenv("NEO_SEARCHAPI_API_KEY", "") or "").strip()
        if env:
            return env, "NEO_SEARCHAPI_API_KEY"
        env = str(os.getenv("NEO_JARVIS_SEARCHAPI_API_KEY", "") or "").strip()
        if env:
            return env, "NEO_JARVIS_SEARCHAPI_API_KEY"
        env = str(os.getenv("SEARCHAPI_API_KEY", "") or "").strip()
        if env:
            return env, "SEARCHAPI_API_KEY"
        return "", "missing"

    def api_key(self) -> str:
        return self.api_key_with_source()[0]

    def available(self) -> bool:
        return bool(self.api_key())

    def _params(self, query: str, language: str, *, engine: str = "google") -> dict[str, Any]:
        params: dict[str, Any] = {"engine": engine, "q": str(query or "").strip()}
        lang = str(language or "").strip().casefold().replace("_", "-").split("-", 1)[0]
        if lang in {"ru", "en", "de", "fr", "es", "it", "pt", "pl", "tr", "uk", "nl"}:
            params["hl"] = lang
        gl = re.sub(r"[^a-z]", "", str(self.config.get("gaming.web_knowledge.searchapi_gl", "us") or "us").casefold())[:2]
        if len(gl) == 2:
            params["gl"] = gl
        return params

    def _timeout(self, maximum: float = 60.0) -> float:
        try:
            value = float(self.config.get("gaming.web_knowledge.searchapi_timeout_seconds", 18.0))
            if not math.isfinite(value):
                value = 18.0
        except (TypeError, ValueError):
            value = 18.0
        return max(6.0, min(maximum, value))

    def _request(self, params: dict[str, Any], *, stage: str) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
        key, key_source = self.api_key_with_source()
        if not key:
            err = "searchapi_key_missing"
            return None, {"provider": self.name, "stage": stage, "status": 0, "error": err, "key_source": key_source}, err
        timeout = self._timeout()
        started = time.perf_counter()
        try:
            response = self.session.get(
                SEARCHAPI_SEARCH_URL,
                params=params,
                headers={"Authorization": f"Bearer {key}"},
                timeout=(4.0, timeout),
            )
            status = int(response.status_code or 0)
            size = len(response.content or b"")
            detail = _safe_detail(response, secrets=(key,))
            if status in {401, 403}:
                err = "searchapi_auth_error"
            elif status in {402}:
                err = "searchapi_billing_required"
            elif status == 429:
                err = "searchapi_rate_limited"
            elif status >= 500:
                err = "searchapi_server_error"
            elif status >= 400:
                err = "searchapi_bad_request"
            else:
                err = ""
            attempt = {
                "provider": self.name,
                "stage": stage,
                "status": status,
                "bytes": size,
                "error": err,
                "key_source": key_source,
            }
            if detail:
                attempt["detail"] = detail
            from jarvis.runtime import http_pool173
            http_pool173.record("searchapi", stage, (time.perf_counter() - started) * 1000, ok=not err, status=status)
            if err:
                return None, attempt, err
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("SearchAPI response is not a JSON object")
            metadata = data.get("search_metadata")
            if isinstance(metadata, dict):
                for field in ("request_time_taken", "parsing_time_taken", "total_time_taken", "status"):
                    if field in metadata:
                        attempt[field] = metadata[field]
            return data, attempt, ""
        except requests.Timeout:
            err = "searchapi_timeout"
            return None, {"provider": self.name, "stage": stage, "status": 0, "error": err, "key_source": key_source}, err
        except requests.RequestException as exc:
            err = f"searchapi_network_error:{type(exc).__name__}"
            return None, {"provider": self.name, "stage": stage, "status": 0, "error": err, "key_source": key_source}, err
        except Exception as exc:
            err = f"searchapi_response_error:{type(exc).__name__}"
            return None, {"provider": self.name, "stage": stage, "status": 0, "error": err, "key_source": key_source}, err

    def _parse_sources(self, data: dict[str, Any]) -> list[SearchResult]:
        maximum = _clamp_int(self.config.get("gaming.web_knowledge.searchapi_max_sources", 8), 1, 20, 8)
        rows: list[dict[str, Any]] = []
        answer_box = data.get("answer_box")
        if isinstance(answer_box, dict):
            if _url_from(answer_box):
                rows.append(answer_box)
            box_sources = answer_box.get("sources")
            if isinstance(box_sources, list):
                rows.extend(x for x in box_sources if isinstance(x, dict))
        for key in ("organic_results",):
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(x for x in value if isinstance(x, dict))
        out: list[SearchResult] = []
        seen: set[str] = set()
        for row in rows:
            url = _url_from(row)
            if not url or url in seen:
                continue
            seen.add(url)
            title = _text_field(row, "title", "source", "displayed_link") or url
            snippet = _text_field(row, "snippet", "description", "text")
            out.append(SearchResult(
                title=(title or url)[:500],
                url=url,
                snippet=snippet[:3000],
                provider=self.name,
                rank=len(out) + 1,
                score=0.0,
            ))
            if len(out) >= maximum:
                break
        return out

    @staticmethod
    def _parse_related(data: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for key in ("related_questions", "related_searches"):
            rows = data.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                text = ""
                if isinstance(row, str):
                    text = row
                elif isinstance(row, dict):
                    text = str(row.get("question") or row.get("query") or row.get("text") or "")
                text = re.sub(r"\s+", " ", text).strip()
                if text and text not in out:
                    out.append(text)
                    if len(out) >= 5:
                        return out
        return out

    @staticmethod
    def _answer_candidates(data: dict[str, Any], query: str = "") -> list[dict[str, Any]]:
        """SQ-01/SQ-03/SQ-06: every usable retrieved fragment with its own source,
        ranked against the question. The answer box is a strong candidate, not an
        unconditional winner; echoes, schema noise and duplicates are dropped."""
        from jarvis.runtime.web_relevance186 import fold, is_echo, is_noise, score
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(text: str, url: str, kind: str, bonus: float) -> None:
            text = str(text or "").strip()
            key = fold(text)[:400]
            if not text or key in seen:
                return
            seen.add(key)
            value = score(text, query) if query else 1.0
            if value <= 0 and not is_echo(text, query) and not is_noise(text, query):
                # No shared words (another language, a bare value) is weak
                # evidence, not a reason to discard; echoes and ads are dropped.
                value = 0.05
            if value <= 0:
                return
            out.append({"text": text[:6000], "url": url, "kind": kind, "score": value + bonus})

        box = data.get("answer_box") if isinstance(data, dict) else None
        if isinstance(box, dict):
            text = _text_field(box, "answer", "snippet", "description")
            if not text:
                rows = box.get("list")
                if isinstance(rows, list):
                    items = [str(x).strip() for x in rows if isinstance(x, (str, int, float)) and not isinstance(x, bool) and str(x).strip()][:10]
                    text = "\n".join(f"• {item}" for item in items)
            if text:
                box_url = _url_from(box)
                if not box_url and isinstance(box.get("sources"), list):
                    box_url = next((_url_from(x) for x in box["sources"] if _url_from(x)), "")
                add(text, box_url, "answer_box", 0.4)
        rows = data.get("organic_results") if isinstance(data, dict) else None
        if isinstance(rows, list):
            for row in rows[:10]:
                if not isinstance(row, dict):
                    continue
                url = _url_from(row)
                if url:
                    add(_text_field(row, "snippet")[:1500], url, "organic", 0.0)
        out.sort(key=lambda item: item["score"], reverse=True)
        return out

    @staticmethod
    def _answer_from(data: dict[str, Any]) -> str:
        # Deliberately ignore ai_overview/markdown/text_blocks. These are generated
        # answers, whereas this product's information lane uses retrieved facts.
        box = data.get("answer_box")
        if isinstance(box, dict):
            for key in ("answer", "snippet", "description"):
                value = box.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    return str(value).strip()[:6000]
            rows = box.get("list")
            if isinstance(rows, list):
                items = [str(x).strip() for x in rows if isinstance(x, (str, int, float))][:10]
                if items:
                    return "\n".join(f"• {item}" for item in items)
        rows = data.get("organic_results")
        if isinstance(rows, list):
            excerpts = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                text = _text_field(row, "snippet")
                if text and _url_from(row):
                    excerpts.append(text[:1500])
                if len(excerpts) == 3:
                    break
            return "\n\n".join(excerpts)
        return ""

    def search(self, query: str, *, language: str = "ru", **_kwargs: Any) -> SearchResponse:
        query = str(query or "").strip()
        if not query:
            return SearchResponse(query=query, provider=self.name, error="searchapi_empty_query")

        params = self._params(query, language)
        data, attempt, error = self._request(params, stage="google")
        attempts = [attempt]
        provider = self.name

        if not isinstance(data, dict):
            return SearchResponse(query=query, provider=provider, error=error, attempts=attempts)

        try:
            candidates = self._answer_candidates(data, query)
            results = self._parse_sources(data)
            related = self._parse_related(data)
        except Exception as exc:  # SQ-06: a malformed payload is a controlled failure
            err = f"searchapi_schema_error:{type(exc).__name__}"
            attempt["error"] = err
            return SearchResponse(query=query, provider=provider, error=err, attempts=attempts)
        best = candidates[0]["score"] if candidates else 0.0
        # Weaker fragments join the best one only when they are comparably
        # relevant (no padding with ads, forum chatter or narrower varieties).
        chosen = [row for index, row in enumerate(candidates)
                  if index == 0 or row["score"] >= max(1.0, 0.65 * best)][:3]
        if chosen and chosen[0]["kind"] == "answer_box":
            # A strong card answers alone; weaker excerpts are not glued to it.
            chosen = [row for row in chosen if row is chosen[0] or row["score"] >= chosen[0]["score"] - 0.3][:2]
        answer = "\n\n".join(row["text"] for row in chosen)
        answer_sources = [row["url"] for row in chosen if row.get("url")]
        answer_kind = chosen[0]["kind"] if chosen else ""
        # REC-SQ03-ATTRIBUTION-CACHE-SOURCE-01: a selected fragment keeps its own
        # page even when the source list limit cut that row off.
        known = {r.url for r in results}
        for row in chosen:
            url = str(row.get("url") or "")
            if url and url not in known:
                results.append(SearchResult(
                    title=str(row.get("title") or url)[:500], url=url, snippet=str(row.get("text") or "")[:3000],
                    provider=self.name, rank=len(results) + 1, score=0.0))
                known.add(url)
        if not answer and not error:
            error = "searchapi_answer_missing" if results else "searchapi_no_results"
        if attempts:
            attempts[-1]["direct"] = bool(answer)
            attempts[-1]["results"] = len(results)
            if error and not attempts[-1].get("error"):
                attempts[-1]["error"] = error

        metadata = data.get("search_metadata") if isinstance(data.get("search_metadata"), dict) else {}
        return SearchResponse(
            query=query,
            provider=provider,
            results=results,
            direct_answer=answer,
            direct_answer_kind="answer_box" if answer_kind == "answer_box" else "source_excerpts",
            related_questions=related,
            error=error,
            attempts=attempts,
            metadata={
                "search_id": str(metadata.get("id") or ""),
                "total_time_taken": metadata.get("total_time_taken", 0),
                "engine": "google",
                "key_source": self.api_key_with_source()[1],
                # Which retrieved rows actually support the answer text.
                "answer_sources": answer_sources,
                "answer_box_without_link": bool(chosen and chosen[0]["kind"] == "answer_box" and not chosen[0].get("url")),
            },
        )

    def account_status(self, key_override: str = "") -> dict[str, Any]:
        """Small authenticated /me probe for the Settings 'Check key' action."""
        key = str(key_override or "").strip() or self.api_key()
        if not key:
            raise ValueError("SearchAPI API key is empty")
        timeout = self._timeout(30.0)
        response = self.session.get(
            SEARCHAPI_ACCOUNT_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=(4.0, max(6.0, min(30.0, timeout))),
        )
        if int(response.status_code or 0) >= 400:
            detail = _safe_detail(response, secrets=(key,))
            raise RuntimeError(f"SearchAPI HTTP {int(response.status_code or 0)}" + (f": {detail}" if detail else ""))
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("SearchAPI account response is not JSON")
        return data


__all__ = ["SEARCHAPI_SEARCH_URL", "SEARCHAPI_ACCOUNT_URL", "SearchAPIGoogleProvider"]
