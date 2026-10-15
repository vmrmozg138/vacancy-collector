"""Работа со временем.

В базе держим наивное UTC-время: оно одинаково ведёт себя и в SQLite, и в PostgreSQL,
и не зависит от часового пояса машины, на которой запущен сборщик.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def utcnow() -> datetime:
    """Текущее время в UTC без tzinfo (для колонок DateTime)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_date(value: str | None) -> date | None:
    """Разобрать дату вида 2026-10-14 или 2026-10-14T13:40:00+0300.

    Возвращает None, если строки нет или формат неожиданный: одна битая дата
    не должна ронять весь запуск.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
