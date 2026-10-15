"""Конвейер: связать источник и хранилище и записать статистику запуска.

Источник ничего не знает про базу, хранилище — про сеть. Здесь они встречаются.
Это позволяет тестировать парсеры отдельно (на фикстурах, без сети),
а хранилище — отдельно (на подготовленных объектах).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

from sqlalchemy.orm import Session

from collector.core.stats import RunStats
from collector.core.timeutils import utcnow
from collector.sources.base import VacancySource
from collector.storage.repository import save_run, upsert_vacancies


def run_source(
    source: VacancySource,
    session_factory: Callable[[], Session],
    pages: int,
    logger: logging.Logger,
    region_filter: str | None = None,
) -> RunStats:
    """Скачать `pages` страниц источника и записать данные в базу."""
    stats = RunStats(source=source.name)
    started_at = utcnow()

    try:
        page_iterator: Iterator[Any] = source.iter_pages(pages)
    except Exception as exc:  # источник не смог начать загрузку — фиксируем и выходим
        stats.errors += 1
        stats.status = "error"
        stats.message = str(exc)
        logger.error("источник %s: не удалось начать загрузку: %s", source.name, exc)
        page_iterator = iter(())

    while True:
        try:
            payload = next(page_iterator)
        except StopIteration:
            break
        except Exception as exc:  # сбой сети или API: запуск помечаем как ошибочный, но не падаем
            stats.errors += 1
            stats.status = "error"
            stats.message = str(exc)
            logger.error("источник %s: не удалось получить страницу: %s", source.name, exc)
            break

        stats.pages_fetched += 1
        try:
            items = source.parse_page(payload)
        except Exception as exc:
            stats.errors += 1
            logger.exception("источник %s: ошибка разбора страницы: %s", source.name, exc)
            continue

        if region_filter:
            needle = region_filter.casefold()
            before = len(items)
            items = [item for item in items if needle in (item.region or "").casefold()]
            stats.items_skipped += before - len(items)

        stats.items_parsed += len(items)
        if not items:
            continue

        with session_factory() as session:
            inserted, updated = upsert_vacancies(session, items)
            session.commit()
        stats.items_inserted += inserted
        stats.items_updated += updated
        logger.info(
            "страница %s: разобрано %s, новых %s, обновлено %s",
            stats.pages_fetched,
            len(items),
            inserted,
            updated,
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

    logger.info("готово: %s", stats.as_message())
    return stats
