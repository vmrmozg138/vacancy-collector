"""HTTP-слой: одна сессия, корректный User-Agent, повторы с backoff и пауза между запросами.

Зачем отдельный слой: источники не должны знать про таймауты, ретраи и заголовки.
Иначе эту логику пришлось бы повторять в каждом парсере — и она бы в каждом различалась.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

# Коды, при которых имеет смысл повторить запрос: сервер перегружен или просит подождать.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpClient:
    """Обёртка над requests.Session с предсказуемым поведением при сбоях."""

    def __init__(
        self,
        user_agent: str,
        timeout: int = 30,
        delay: float = 0.5,
        max_retries: int = 4,
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.timeout = timeout
        self.delay = delay
        self.max_retries = max(1, max_retries)
        self.log = logger or logging.getLogger("collector.http")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
        )

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        """GET с повторами. Бросает RuntimeError, если все попытки исчерпаны."""
        last_error: str = "нет попыток"
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self.max_retries:
                    break
                self._wait(attempt, last_error)
                continue

            if response.status_code in RETRY_STATUSES:
                last_error = f"HTTP {response.status_code}"
                if attempt == self.max_retries:
                    break
                self._wait(attempt, last_error, response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            # Пауза после успешного запроса: не создаём нагрузку на чужой сервер.
            time.sleep(self.delay)
            return response

        raise RuntimeError(f"не удалось получить {url}: {last_error}")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET и разбор JSON."""
        return self.get(url, params=params).json()

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        """GET и текст ответа (для HTML-страниц)."""
        response = self.get(url, params=params)
        # Источники бывают в cp1251 и без указания кодировки в заголовках.
        if response.encoding is None:
            response.encoding = response.apparent_encoding
        return response.text

    def _wait(self, attempt: int, reason: str, retry_after: str | None = None) -> None:
        """Пауза перед следующей попыткой: сначала уважаем Retry-After, потом backoff."""
        pause = min(2**attempt, 30)
        if retry_after:
            try:
                pause = max(pause, min(float(retry_after), 60.0))
            except ValueError:
                pass
        self.log.warning(
            "попытка %s/%s не удалась (%s), пауза %.1f с", attempt, self.max_retries, reason, pause
        )
        time.sleep(pause)
