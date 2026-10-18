"""Bounded, redacted diagnostics; never exports settings, recordings or screenshots."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import re
import time
import zipfile

_CREDENTIAL = re.compile(r'''(?ix)(["']?(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|token|password|passwd|secret|authorization|cookie)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,&;}]+)''')
# 186: the whole RFC 6750 token alphabet (incl. «~»), so no suffix survives.
_BEARER = re.compile(r"(?i)\b(Bearer|OAuth|Basic)\s+[A-Za-z0-9_.+/=:~-]+")
# 186: a bot token is also recognized in its URL position («…/bot<id>:<secret>/getMe»).
_TELEGRAM = re.compile(r"(?:(?<=/bot)|(?<![A-Za-z0-9_-]))\d{6,12}:[A-Za-z0-9_-]{25,}(?![A-Za-z0-9_-])")
_DISCORD = re.compile(r"(?<![A-Za-z0-9_-])(?:mfa\.[A-Za-z0-9_-]{40,}|[A-Za-z0-9_-]{23,40}\.[A-Za-z0-9_-]{6,10}\.[A-Za-z0-9_-]{25,})(?![A-Za-z0-9_-])")
_URL_AUTH = re.compile(r"(?i)(https?://)[^/\s@]+@")
_HEADER = re.compile(r"(?im)(\b(?:authorization|cookie|set-cookie)\s*[:=]\s*)[^\r\n]+")
# NEO 182: provider secrets passed as URL query parameters (YouTube Data API «key=»,
# OpenWeather «appid=», OAuth «client_secret=», «access_token=», «code=» …).
_QUERY_SECRET = re.compile(r"(?i)([?&](?:key|api_?key|appid|app_id|client_secret|access_token|refresh_token|token|code|signature|sig|auth|password)=)[^&#\s\"']+")


# Ollama may print its newly generated public key during first startup. Neither
# public identifiers nor any accidentally logged private PEM belong in exports.
_PEM_KEY = re.compile(r"-----BEGIN ([A-Z0-9 ]*(?:PRIVATE|PUBLIC) KEY)-----.*?-----END \1-----", re.S)
_SSH_KEY = re.compile(r"(?im)\b(?:ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-[A-Za-z0-9-]+)\s+[A-Za-z0-9+/]+={0,3}(?:[ \t]+[^\r\n]*)?")


# NEO 188 NEO-PRIVACY-003: secret parts of URLs in DERIVED copies (logs, audit,
# history, reports, exports). The operational URL of an allowed action is never
# changed here and nothing is fetched. Unknown capability URLs cannot all be
# recognised; doubtful long opaque values are hidden rather than kept.
_URL_IN_TEXT = re.compile(r"(?i)\b(?:https?|wss?|ftp)://[^\s<>\"'`]+")
_SECRET_PARAM_WORDS = ("token", "secret", "signature", "password", "passwd", "credential", "session", "apikey",
                       "api_key", "api-key", "auth", "jwt", "otp", "ticket", "nonce", "hash")
_SECRET_PARAMS = {"sig", "sign", "key", "code", "pwd", "pass", "appid", "app_id", "sid", "ssid", "client_secret",
                  "x-amz-credential", "x-amz-security-token", "x-goog-credential", "x-ms-signature", "se", "sp", "sv", "skoid"}
_BENIGN_PARAMS = {"v", "list", "t", "index", "q", "query", "search_query", "page", "lang", "hl", "gl", "id", "p",
                  "start", "end", "si", "feature", "ref", "sort", "tab", "s", "w", "h", "width", "height", "format"}


def _secret_param(name: str) -> bool:
    from urllib.parse import unquote_plus
    key = unquote_plus(str(name or "")).strip().casefold()
    bare = re.sub(r"^(?:x-(?:amz|goog|oss|ms|cos|obs)-|x_)", "", key)
    return key in _SECRET_PARAMS or bare in _SECRET_PARAMS or any(word in bare.replace("_", "").replace("-", "") or word in bare
                                                                  for word in _SECRET_PARAM_WORDS)


def _opaque(value: str, limit: int = 40) -> bool:
    from urllib.parse import unquote_plus
    raw = unquote_plus(str(value or ""))
    return (len(raw) >= limit and bool(re.fullmatch(r"[A-Za-z0-9._~+/=%:-]+", raw))
            and bool(re.search(r"\d", raw)) and bool(re.search(r"[A-Za-z]", raw)))


def _clean_pairs(text: str, separator: str = "&") -> str:
    parts = []
    for part in re.split(r"([&;])", text):
        if part in {"&", ";"}:
            parts.append(part)
            continue
        name, eq, value = part.partition("=")
        if value == "[REDACTED]":
            parts.append(part)
            continue
        if eq and value and (_secret_param(name) or (name.casefold() not in _BENIGN_PARAMS and _opaque(value))):
            parts.append(name + "=[REDACTED]")
        elif not eq and _opaque(part, 24):
            parts.append("[REDACTED]")
        else:
            parts.append(part)
    return "".join(parts)


def redact_url(url: str) -> str:
    """One URL with userinfo, secret query/fragment values and opaque path tokens hidden."""
    from urllib.parse import urlsplit, urlunsplit
    value = str(url or "")
    trail = ""
    while value and (value[-1] in ".,;:!?)}»" or (value[-1] == "]" and not value.endswith("[REDACTED]") and "[" not in value)):
        trail = value[-1] + trail
        value = value[:-1]
    value = value.replace("[REDACTED]@", "redacted@", 1)
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
    except ValueError:
        return value.split("://", 1)[0] + "://[REDACTED_URL]" + trail
    if not host:
        return value.split("://", 1)[0] + "://[REDACTED_URL]" + trail
    netloc = parts.netloc
    if netloc.startswith("redacted@"):
        netloc = "[REDACTED]@" + netloc[len("redacted@"):]
    elif "@" in netloc:
        netloc = "[REDACTED]@" + netloc.rsplit("@", 1)[1]
    segments = []
    for segment in parts.path.split("/"):
        if _opaque(segment, 32) or re.search(r"(?i)(?:token|password|secret|signature|sig|auth)[=:]", segment):
            segment = "[REDACTED]"
        segments.append(segment)
    query = _clean_pairs(parts.query) if parts.query else ""
    fragment = parts.fragment
    if fragment:
        fragment = _clean_pairs(fragment) if "=" in fragment else ("[REDACTED]" if _opaque(fragment, 24) else fragment)
    return urlunsplit((parts.scheme, netloc, "/".join(segments), query, fragment)) + trail


def redact_urls(text: str) -> str:
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), str(text))


def redact(text: str) -> str:
    text = redact_credentials(text)
    # Tracebacks often render repr(path) with doubled backslashes; redact those
    # exactly as ordinary Windows paths before exporting diagnostic archives.
    text = re.sub(r"(?i)\b[A-Z]:(?:\\)+Users(?:\\)+[^\\\s\"']+", r"%USERPROFILE%", text)
    for name in ("USERPROFILE", "HOME"):
        value = os.environ.get(name, "")
        if len(value) > 3:
            text = text.replace(value, "%USERPROFILE%")
    return text


def redact_credentials(text: str) -> str:
    """Keys, tokens and passwords only (paths are left to the caller's policy)."""
    text = _PEM_KEY.sub("[REDACTED_KEY]", str(text))
    text = _SSH_KEY.sub("[REDACTED_SSH_KEY]", text)
    text = _HEADER.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _CREDENTIAL.sub(lambda m: m.group(1) + "[REDACTED]", str(text))
    text = _BEARER.sub(lambda m: m.group(1) + " [REDACTED]", text)
    text = _TELEGRAM.sub("[REDACTED_TELEGRAM_TOKEN]", text)
    text = _DISCORD.sub("[REDACTED_DISCORD_TOKEN]", text)
    text = _URL_AUTH.sub(r"\1[REDACTED]@", text)
    text = _QUERY_SECRET.sub(lambda m: m.group(1) + "[REDACTED]", text)
    try:
        text = redact_urls(text)
    except Exception:  # noqa: BLE001 - a sanitizer failure never writes the original URL
        text = _URL_IN_TEXT.sub("[REDACTED_URL]", text)
    return text


def clip_redacted(text: str, limit: int) -> str:
    """Redact the WHOLE text first, then cut; a URL cut at the edge keeps only its host."""
    value = redact_credentials(str(text or ""))
    if len(value) <= limit:
        return value
    cut = value[:limit]
    tail = re.search(r"(?i)\b(?:https?|wss?|ftp)://[^\s<>\"'`]*$", cut)
    if tail:
        head = re.match(r"(?i)((?:https?|wss?|ftp)://[^/?#\s]*)", tail.group(0))
        return cut[:tail.start()] + ((head.group(1) + "/…") if head else "…")
    return cut + "…"


def safe_error(exc: BaseException | str) -> str:
    """Error text that is safe to show to the user (no keys, tokens or full request URLs)."""
    return redact(str(exc))


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def sensitive_key(key: str) -> bool:
    # "first_token_timeout_seconds" refers to inference, not an API secret.
    if key.rsplit('.', 1)[-1] in {"first_token_timeout_seconds", "max_tokens", "num_tokens", "token_count"}:
        return False
    return bool(re.search(r"(?i)(?:token|password|secret|api.?key|authorization|cookie)", key))


def sanitize_structure(value, key=""):
    if sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {redact(str(k)): sanitize_structure(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_structure(v) for v in value]
    return redact(value) if isinstance(value, str) else value


def sanitize_credentials(value, key=""):
    """Like sanitize_structure, but keeps paths: only keys, tokens and secret URL parts go."""
    if sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize_credentials(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_credentials(v) for v in value]
    return redact_credentials(value) if isinstance(value, str) else value


def report_bytes(payload) -> bytes:
    return json.dumps(sanitize_structure(payload), ensure_ascii=False, indent=2).encode("utf-8")


def write_report(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(report_bytes(payload))
    temporary.replace(path)
    return path


def collect_logs(data_root: Path, output: Path | None = None) -> Path:
    """Allowlisted text logs only; no profile/DB/audio files. Logs may contain command text."""
    data_root = Path(data_root)
    output = output or data_root / "diagnostics" / (time.strftime("NEO_diagnostics_%Y%m%d_%H%M%S") + ".zip")
    output.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    total = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder, patterns in (("logs", ("*.log", "*.log.[1-4]")), ("diagnostics", ("self_test*.json", "test_results*.json", "integration_test*.json")), ("bootstrap", ("launcher.log", "gui-launcher.log", "runtime_report.json", "launch_status.json", "vision_status.json", "vision-bootstrap.log", "ollama-server-error.log"))):
            directory = data_root / folder
            files = sorted({p for pattern in patterns for p in directory.glob(pattern)})
            for path in files[:30]:
                if not path.is_file() or path.is_symlink():
                    continue
                length = min(path.stat().st_size, 2 * 1024 * 1024)
                if total + length > 16 * 1024 * 1024:
                    continue
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - length))
                    content = stream.read(length).decode("utf-8", errors="replace")
                if length < path.stat().st_size and path.suffix.lower() != '.json':
                    # NEO 188 URLP-04: a tail starting mid-line may hold a secret without its key.
                    content = content.split("\n", 1)[1] if "\n" in content else ""
                relative = path.relative_to(data_root).as_posix()
                if path.suffix.lower() == '.json':
                    try: blob = report_bytes(json.loads(content))
                    except ValueError: blob = report_bytes({'status':'INVALID_SOURCE_JSON','file':relative,'detail':'Truncated or malformed source; no unparsed content exported'})
                else: blob = redact(content).encode('utf-8')
                archive.writestr(relative, blob)
                entries.append({"file": relative, "tail_bytes": length})
                total += length
        runs = data_root / "bootstrap" / "runs"
        if runs.is_dir() and not runs.is_symlink():
            recent = sorted((p for p in runs.iterdir() if p.is_dir() and not p.is_symlink()),
                            key=lambda p: p.stat().st_mtime, reverse=True)[:3]
            for directory in recent:
                for name in ("launch_status.json", "startup_progress.json", "ready.json", "startup.log"):
                    path = directory / name
                    if not path.is_file() or path.is_symlink():
                        continue
                    length = min(path.stat().st_size, 2 * 1024 * 1024)
                    if total + length > 16 * 1024 * 1024:
                        continue
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - length))
                        content = stream.read(length).decode("utf-8", errors="replace")
                    if name == "ready.json":
                        try:
                            receipt = json.loads(content)
                            receipt.pop("proof", None)
                            content = json.dumps(receipt, ensure_ascii=False, indent=2)
                        except (ValueError, TypeError, AttributeError):
                            content = "Invalid readiness receipt; inspect local file."
                    relative = path.relative_to(data_root).as_posix()
                    if path.suffix.lower() == '.json':
                        try: blob = report_bytes(json.loads(content))
                        except ValueError: blob = report_bytes({'status':'INVALID_SOURCE_JSON','file':relative})
                    else: blob = redact(content).encode('utf-8')
                    archive.writestr(relative, blob)
                    entries.append({"file": relative, "tail_bytes": length})
                    total += length
        archive.writestr("manifest.json", json.dumps({"product": "NEO", "created_at": time.time(), "redacted": True, "excluded": ["settings", "database", "audio", "screenshots", "file content"], "files": entries}, indent=2))
    return output
