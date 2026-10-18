"""Pure local-model identifiers/endpoints; safe for headless clients, no Qt import."""
from urllib.parse import urlsplit

DEFAULT_CHAT_MODEL = "qwen2.5:0.5b"


def local_endpoint(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("Модели доступны только через локальный сервер Ollama без пароля в URL.")
    _ = parsed.port  # validate malformed ports before a request
    return url


def exact_model(model: str, models: list[str]) -> str:
    """Never substitute a different size/quantization for a selected tag."""
    return next((name for name in models if name == model or (":" not in model and name == model + ":latest")), "")

