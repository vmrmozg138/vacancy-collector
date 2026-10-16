"""Работа со временем.

В базе держим наивное UTC-время: оно одинаково ведёт себя и в SQLite, и в PostgreSQL,
и не зависит от часового пояса машины, на которой запущен сборщик.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone


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


def to_api_moment(moment: datetime) -> str:
    """Момент времени в том виде, который принимает API «Работы России».

    Проверено на живом API: `2026-10-15T00:00:00+03:00` и `2026-10-14T21:00:00Z`
    принимаются, а `+0300` (без двоеточия), миллисекунды и просто `2026-10-15` —
    отвечают HTTP 500. Поэтому всегда отдаём UTC с «Z»: этот вид источник понимает точно.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_RELATIVE = re.compile(r"^(\d+)\s*([dhm])$", re.IGNORECASE)


def parse_duration(value: str) -> timedelta:
    """Разобрать длительность: `30s`, `1m`, `2h`, `1d`.

    Нужна для минимальной ширины окна при сборе за период: «делить, пока окно
    не станет меньше минуты» записывается как `--window 1m`.
    """
    text = (value or "").strip().lower()
    match = re.match(r"^(\d+)\s*([smhd])$", text)
    if not match:
        raise ValueError(f"не понимаю длительность: {value!r} (примеры: 30s, 1m, 2h, 1d)")
    amount, unit = int(match.group(1)), match.group(2)
    return {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]


def parse_moment(value: str, *, now: datetime | None = None) -> datetime:
    """Разобрать момент времени из строки для командной строки.

    Понимает три вида записи:

    * `2026-10-15` или `2026-10-15T13:40` — время без пояса считаем местным;
    * `2026-10-15T13:40:00+03:00` — с поясом, как есть;
    * `24h`, `7d`, `30m` — «сколько назад от сейчас»: так удобнее, чем считать дату руками.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("пустое значение времени")

    relative = _RELATIVE.match(text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]
        return (now or datetime.now(timezone.utc)) - delta

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"не понимаю время: {text!r} (примеры: 2026-10-15, 2026-10-15T13:40, 24h, 7d)") from exc

    if parsed.tzinfo is None:
        # Время без пояса — местное: так человек и думает, когда пишет дату руками.
        return parsed.astimezone()
    return parsed
