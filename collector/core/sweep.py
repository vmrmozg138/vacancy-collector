"""Сбор за период делением окна времени.

Зачем это нужно. У источника сломана пагинация: `limit=100&offset=100` отвечает HTTP 500,
а `meta.total` не описывает доступное (по запросу «python» заявлено 406 вакансий, реально
видны 106). Значит страницами больше сотни записей не собрать.

Но у источника работает фильтр по дате изменения (`modifiedFrom`/`modifiedTo`), и если
в окне времени записей меньше лимита, он отдаёт их все. Отсюда приём: **делить период
пополам, пока окно не станет достаточно узким**. Проверено на живом API: сумма половин
сходится с целым (18 849 + 3 524 = 22 373 за сутки 16.10.2026), а окно в одну минуту
вмещает 41 запись — с запасом меньше лимита в 100.

Алгоритм обхода — стек, а не рекурсия: так видно, сколько окон осталось, и нет риска
упереться в предел глубины вызовов. Каждое окно, которое уложилось в лимит, записывается
в базу сразу — прерванный сбор не теряет уже скачанное, а повторный прогон не создаёт дублей
(за это отвечает upsert по паре «источник + внешний идентификатор»).

Чего этот модуль не делает: не ходит в базу сам по себе и не знает про CLI. Он получает
источник, фабрику сессий и границы периода, а возвращает статистику.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from collector.core.pipeline import Sink
from collector.core.stats import RunStats
from collector.core.timeutils import utcnow
from collector.sources.base import Item
from collector.sources.trudvsem_api import TrudvsemSource
from collector.storage.repository import save_run, upsert_vacancies

#: Минимальная ширина окна по умолчанию: минута. Уже неё дробить смысла нет — проверено,
#: что минута вмещает около 40 записей при лимите 100.
DEFAULT_MIN_WINDOW = timedelta(minutes=1)


@dataclass
class SweepResult:
    """Итог обхода периода: статистика запуска плюс то, что важно знать про нарезку."""

    stats: RunStats
    windows_done: int = 0
    truncated_windows: int = 0
    stopped_early: bool = False
    #: Сколько записей в источнике за весь период (по первому запросу).
    estimated_total: int = 0
    #: Границы окон, которые не удалось забрать целиком или которые упали с ошибкой.
    problem_windows: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Доля собранного от заявленного источником за период, в процентах."""
        if not self.estimated_total:
            return 0.0
        return min(100.0, self.stats.items_parsed / self.estimated_total * 100)


def estimate_period(source: TrudvsemSource, since: datetime, until: datetime) -> int:
    """Сколько записей источник заявляет за период. Один запрос, без записи в базу.

    Нужно, чтобы перед долгим сбором понимать объём, а после — честно показать,
    сколько из заявленного собрано.
    """
    payload = source.fetch_window(since, until, limit=1)
    return source.window_total(payload)


def sweep_period(
    source: TrudvsemSource,
    session_factory: Callable[[], Session],
    since: datetime,
    until: datetime,
    logger: logging.Logger,
    *,
    min_window: timedelta = DEFAULT_MIN_WINDOW,
    max_requests: int | None = None,
    sink: Sink = upsert_vacancies,
    progress_every: int = 25,
) -> SweepResult:
    """Собрать всё, что источник отдаёт за период, деля окна времени.

    `max_requests` ограничивает число запросов: сбор можно остановить по бюджету
    и продолжить позже — уже записанное останется в базе.
    """
    if until <= since:
        raise ValueError("конец периода должен быть позже начала")
    if min_window <= timedelta(0):
        raise ValueError("минимальная ширина окна должна быть положительной")

    stats = RunStats(source=source.name)
    started_at = utcnow()
    result = SweepResult(stats=stats)

    try:
        result.estimated_total = estimate_period(source, since, until)
    except Exception as exc:  # оценка не должна мешать сбору
        logger.warning("не удалось оценить объём периода: %s", exc)

    logger.info(
        "период %s — %s: источник заявляет %s записей, минимальное окно %s",
        since.isoformat(timespec="seconds"),
        until.isoformat(timespec="seconds"),
        result.estimated_total or "?",
        min_window,
    )

    stack: list[tuple[datetime, datetime]] = [(since, until)]

    while stack:
        if max_requests is not None and stats.pages_fetched >= max_requests:
            result.stopped_early = True
            logger.warning("остановка по бюджету: сделано запросов %s, окон в очереди %s", stats.pages_fetched, len(stack))
            break

        start, end = stack.pop()
        try:
            payload = source.fetch_window(start, end, limit=source.limit)
        except Exception as exc:
            stats.errors += 1
            result.problem_windows.append(f"{start.isoformat()} — {end.isoformat()}: {exc}")
            logger.error("окно %s — %s не получено: %s", start.isoformat(), end.isoformat(), exc)
            continue

        stats.pages_fetched += 1
        items = source.parse_page(payload)
        total = source.window_total(payload)
        width = end - start

        # Источник отдал меньше, чем у него есть, — значит окно слишком широкое, делим.
        if total > len(items) and width > min_window:
            middle = start + width / 2
            stack.append((middle, end))
            stack.append((start, middle))
            continue

        if total > len(items):
            # Уже на минимальном окне, а записей больше лимита: часть окна недоступна.
            result.truncated_windows += 1
            result.problem_windows.append(
                f"{start.isoformat()} — {end.isoformat()}: в источнике {total}, получено {len(items)}"
            )
            logger.warning(
                "окно %s — %s: в источнике %s записей, получено %s — дробление остановлено на минимальном окне",
                start.isoformat(),
                end.isoformat(),
                total,
                len(items),
            )

        result.windows_done += 1
        stats.items_parsed += len(items)

        if items:
            try:
                with session_factory() as session:
                    inserted, updated = sink(session, items)
                    session.commit()
            except Exception as exc:
                stats.errors += 1
                logger.exception("не удалось записать окно %s — %s: %s", start.isoformat(), end.isoformat(), exc)
                continue
            stats.items_inserted += inserted
            stats.items_updated += updated

        if result.windows_done % progress_every == 0:
            logger.info(
                "прогресс: окон %s, записей %s, в очереди %s",
                result.windows_done,
                stats.items_parsed,
                len(stack),
            )

    if result.stopped_early or result.truncated_windows or stats.errors:
        stats.status = "partial"
    else:
        stats.status = "ok"

    stats.message = (
        f"период {since.isoformat(timespec='seconds')} — {until.isoformat(timespec='seconds')}; "
        f"окон {result.windows_done}, обрезанных {result.truncated_windows}, "
        f"ошибок {stats.errors}, бюджет {'исчерпан' if result.stopped_early else 'не исчерпан'}"
    )

    with session_factory() as session:
        save_run(
            session,
            source=source.name,
            started_at=started_at,
            finished_at=utcnow(),
            stats=stats,
        )
        session.commit()

    logger.info(
        "готово: окон=%s записей=%s новых=%s обновлено=%s ошибок=%s покрытие=%.1f%%",
        result.windows_done,
        stats.items_parsed,
        stats.items_inserted,
        stats.items_updated,
        stats.errors,
        result.coverage,
    )
    return result
