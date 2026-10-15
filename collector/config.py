"""Настройки приложения.

Все параметры читаются из переменных окружения (и из файла .env, если он есть),
поэтому один и тот же код работает и с SQLite, и с PostgreSQL, и на другой машине.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # python-dotenv нужен только для удобства локального запуска
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - запасной вариант, если пакет не установлен
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


load_dotenv()

DEFAULT_USER_AGENT = "OpenDataCollector/0.1 (+https://github.com/vmrmozg138)"


@dataclass(frozen=True)
class Settings:
    """Настройки одного запуска сборщика."""

    db_url: str = field(default_factory=lambda: os.getenv("COLLECTOR_DB_URL", "sqlite:///collector.db"))
    user_agent: str = field(default_factory=lambda: os.getenv("COLLECTOR_USER_AGENT", DEFAULT_USER_AGENT))
    timeout: int = field(default_factory=lambda: int(os.getenv("COLLECTOR_TIMEOUT", "30")))
    delay: float = field(default_factory=lambda: float(os.getenv("COLLECTOR_DELAY", "0.5")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("COLLECTOR_MAX_RETRIES", "4")))
    log_file: str = field(default_factory=lambda: os.getenv("COLLECTOR_LOG_FILE", "collector.log"))


def load_settings() -> Settings:
    """Собрать настройки из окружения."""
    return Settings()
