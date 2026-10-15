"""Настройка логирования: консоль + файл.

Логи пишутся и в файл, потому что запуск может быть по расписанию (в планировщике
консоль никто не увидит). Русский текст в консоли Windows требует явной кодировки —
иначе логи падают с UnicodeEncodeError.
"""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_file: str = "collector.log", level: int = logging.INFO) -> logging.Logger:
    """Создать логгер приложения (повторный вызов вернёт уже настроенный)."""
    logger = logging.getLogger("collector")
    if logger.handlers:
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
