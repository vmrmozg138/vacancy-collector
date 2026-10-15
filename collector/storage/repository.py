"""Операции записи в базу.

Главное здесь — upsert: вставка с обновлением по конфликту уникального ключа.
Обычный INSERT на втором запуске упал бы с ошибкой уникальности, а «сначала SELECT,
потом INSERT/UPDATE в цикле» превращается в N+1 запрос. Здесь одна команда на пачку строк.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from collector.core.stats import RunStats
from collector.core.timeutils import utcnow
from collector.sources.base import VacancyItem
from collector.storage.models import RunLog, Vacancy

# Поля, которые обновляются при повторной встрече с той же вакансией.
UPDATABLE_COLUMNS = (
    "title",
    "company",
    "company_inn",
    "region",
    "salary_min",
    "salary_max",
    "currency",
    "url",
    "published_at",
    "raw",
    "updated_at",
)


def item_to_row(item: VacancyItem) -> dict[str, Any]:
    """Преобразовать объект источника в словарь колонок."""
    row = item.to_dict()
    row["raw"] = json.dumps(item.raw, ensure_ascii=False)
    row["updated_at"] = utcnow()
    return row


def upsert_vacancies(session: Session, items: Sequence[VacancyItem]) -> tuple[int, int]:
    """Записать вакансии. Возвращает (сколько вставлено, сколько обновлено)."""
    if not items:
        return 0, 0

    rows = [item_to_row(item) for item in items]
    source = items[0].source

    existing = _existing_ids(session, source, [item.external_id for item in items])
    inserted = sum(1 for item in items if item.external_id not in existing)
    updated = len(items) - inserted

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(Vacancy).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["source", "external_id"],
            set_={column: statement.excluded[column] for column in UPDATABLE_COLUMNS},
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(Vacancy).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["source", "external_id"],
            set_={column: getattr(statement.excluded, column) for column in UPDATABLE_COLUMNS},
        )
    else:  # pragma: no cover - на случай экзотической СУБД
        raise NotImplementedError(f"upsert не реализован для диалекта {dialect}")

    session.execute(statement)
    return inserted, updated


def _existing_ids(session: Session, source: str, external_ids: Iterable[str]) -> set[str]:
    """Какие из этих вакансий уже есть в базе (нужно только для статистики)."""
    ids = list(external_ids)
    if not ids:
        return set()
    query = select(Vacancy.external_id).where(Vacancy.source == source, Vacancy.external_id.in_(ids))
    return set(session.scalars(query).all())


def save_run(
    session: Session,
    *,
    source: str,
    started_at: datetime,
    finished_at: datetime,
    stats: RunStats,
) -> RunLog:
    """Записать итоги запуска в журнал."""
    run = RunLog(
        source=source,
        started_at=started_at,
        finished_at=finished_at,
        pages_fetched=stats.pages_fetched,
        items_parsed=stats.items_parsed,
        items_inserted=stats.items_inserted,
        items_updated=stats.items_updated,
        items_skipped=stats.items_skipped,
        errors=stats.errors,
        status=stats.status,
        message=stats.message,
    )
    session.add(run)
    return run


def count_vacancies(session: Session, source: str | None = None) -> int:
    """Сколько вакансий в базе (всего или по источнику)."""
    query = select(func.count(Vacancy.id))
    if source:
        query = query.where(Vacancy.source == source)
    return int(session.scalar(query) or 0)


def top_regions(session: Session, limit: int = 10) -> list[tuple[str, int]]:
    """Топ регионов по количеству вакансий — проверка, что данные осмысленные."""
    query = (
        select(Vacancy.region, func.count(Vacancy.id).label("total"))
        .where(Vacancy.region.is_not(None))
        .group_by(Vacancy.region)
        .order_by(func.count(Vacancy.id).desc())
        .limit(limit)
    )
    return [(region or "—", int(total)) for region, total in session.execute(query).all()]
