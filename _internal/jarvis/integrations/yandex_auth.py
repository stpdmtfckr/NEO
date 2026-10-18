# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import time
from typing import Any, Callable

HTTP_TIMEOUT_SECONDS = 45
NETWORK_RETRIES = 5
NETWORK_RETRY_DELAY = 2.0

ProgressCallback = Callable[[dict[str, Any]], None]


def _emit(progress_callback: ProgressCallback | None, **payload: Any) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(dict(payload))
    except Exception:
        # Progress reporting must never abort OAuth itself.
        pass


def _sleep_retry(attempt: int) -> None:
    time.sleep(min(NETWORK_RETRY_DELAY * max(1, int(attempt)), 8.0))


def authorize_yandex_music(*, progress_callback: ProgressCallback | None = None) -> dict[str, Any]:
    """Run Yandex Music Device OAuth without touching Qt or the config file.

    The caller owns UI/browser handling and persistence.  Keeping this helper UI-free
    allows the settings dialog to run it in QThreadPool without blocking the GUI.
    """

    try:
        from yandex_music import Client
        from yandex_music.exceptions import NetworkError, TimedOutError
        from yandex_music.utils.request import Request
    except Exception as exc:  # pragma: no cover - depends on user's runtime
        raise RuntimeError(
            "Библиотека yandex-music не установлена или повреждена. "
            "Перезапустите NEO: встроенный bootstrap восстановит зависимости."
        ) from exc

    _emit(progress_callback, stage="preparing", message="Подготавливаю подключение к Яндекс Музыке…")
    try:
        client = Client(request=Request(timeout=HTTP_TIMEOUT_SECONDS))
    except Exception as exc:
        raise RuntimeError(f"Не удалось подготовить клиент Яндекс Музыки: {exc}") from exc

    code = None
    for attempt in range(1, NETWORK_RETRIES + 1):
        try:
            code = client.request_device_code()
            break
        except (TimedOutError, NetworkError) as exc:
            _emit(
                progress_callback,
                stage="network_retry",
                attempt=attempt,
                attempts=NETWORK_RETRIES,
                message=(
                    f"Сеть отвечает медленно. Повторяю получение кода "
                    f"({attempt}/{NETWORK_RETRIES})…"
                ),
                error=str(exc or type(exc).__name__),
            )
            if attempt < NETWORK_RETRIES:
                _sleep_retry(attempt)
        except Exception as exc:
            raise RuntimeError(f"Яндекс не выдал код авторизации: {exc}") from exc

    if code is None:
        raise RuntimeError(
            "Не удалось связаться с oauth.yandex.ru после нескольких попыток. "
            "Проверьте интернет, VPN/Proxy, антивирус или фаервол и попробуйте снова."
        )

    url = str(getattr(code, "verification_url", "") or getattr(code, "verification_uri", "") or "")
    user_code = str(getattr(code, "user_code", "") or "")
    device_code = str(getattr(code, "device_code", "") or "")
    if not device_code:
        raise RuntimeError("Яндекс вернул ответ без device_code. Попробуйте подключение ещё раз.")

    interval = max(1.0, float(getattr(code, "interval", 5) or 5))
    expires_in = max(30.0, float(getattr(code, "expires_in", 600) or 600))
    deadline = time.monotonic() + expires_in

    _emit(
        progress_callback,
        stage="code",
        message="Подтвердите подключение в браузере.",
        verification_url=url,
        user_code=user_code,
        expires_in=expires_in,
    )
    token_result = None
    transient_errors = 0
    while time.monotonic() < deadline:
        try:
            token_result = client.poll_device_token(device_code)
            transient_errors = 0
            if token_result is not None:
                break
        except (TimedOutError, NetworkError) as exc:
            transient_errors += 1
            _emit(
                progress_callback,
                stage="network_wait",
                attempt=transient_errors,
                message="Временный сетевой таймаут. Продолжаю ждать подтверждение…",
                error=str(exc or type(exc).__name__),
            )
            _sleep_retry(min(transient_errors, 4))
            continue
        except Exception as exc:
            raise RuntimeError(f"Не удалось завершить авторизацию: {exc}") from exc

        time.sleep(interval)

    if token_result is None:
        raise RuntimeError(
            "Время действия кода авторизации истекло до подтверждения. "
            "Нажмите «Подключить Яндекс Музыку» ещё раз — будет выдан новый код."
        )

    token = str(
        getattr(token_result, "access_token", "")
        or getattr(token_result, "token", "")
        or (token_result if isinstance(token_result, str) else "")
        or ""
    ).strip()
    if not token:
        raise RuntimeError("Яндекс подтвердил устройство, но не вернул access token.")
    refresh_token = str(getattr(token_result, "refresh_token", "") or "").strip()
    token_type = str(getattr(token_result, "token_type", "") or "").strip()
    try:
        token_expires_in = int(getattr(token_result, "expires_in", 0) or 0)
    except (TypeError, ValueError):
        token_expires_in = 0

    _emit(progress_callback, stage="done", message="Яндекс Музыка подключена. Токен получен и будет сохранён локально.")
    return {
        "token": token,
        "refresh_token": refresh_token,
        "token_type": token_type,
        "token_expires_in": token_expires_in,
        "verification_url": url,
        "user_code": user_code,
    }
