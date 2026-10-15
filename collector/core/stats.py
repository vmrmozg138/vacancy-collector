"""Итоги запуска сборщика.

Вынесены в отдельный модуль, чтобы конвейер и хранилище не зависели друг от друга
по кругу: оба используют только эту структуру.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RunStats:
    """Сколько скачали, разобрали, записали и сколько было ошибок."""

    source: str
    pages_fetched: int = 0
    items_parsed: int = 0
    items_inserted: int = 0
    items_updated: int = 0
    items_skipped: int = 0
    errors: int = 0
    status: str = "ok"
    message: str | None = None

    def as_message(self) -> str:
        return (
            f"источник={self.source} страниц={self.pages_fetched} разобрано={self.items_parsed} "
            f"новых={self.items_inserted} обновлено={self.items_updated} пропущено={self.items_skipped} "
            f"ошибок={self.errors}"
        )
