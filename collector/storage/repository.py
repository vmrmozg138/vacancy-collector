"""Операции записи в базу.

Главное здесь — upsert: вставка с обновлением по конфликту уникального ключа.
Обычный INSERT на втором запуске упал бы с ошибкой уникальности, а «сначала SELECT,
потом INSERT/UPDATE в цикле» превращается в N+1 запрос. Здесь одна команда на пачку строк.

Логика upsert одна для всех таблиц с парой (source, external_id): вакансии, справочник
регионов, сырые ответы. Отличаются только наборы обновляемых колонок.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from collector.core.normalize import normalize_name
from collector.core.stats import RunStats
from collector.core.timeutils import utcnow
from collector.sources.base import Item, RegionItem, VacancyItem
from collector.storage.models import RawDocument, Region, RunLog, Vacancy

#: Поля, которые обновляются при повторной встрече с той же вакансией.
UPDATABLE_VACANCY_COLUMNS = (
    "title",
    "company",
    "company_inn",
    "region",
    "region_key",
    "salary_min",
    "salary_max",
    "currency",
    "url",
    "published_at",
    "raw",
    "updated_at",
)

#: Поля, которые обновляются при повторной встрече с тем же регионом справочника.
UPDATABLE_REGION_COLUMNS = (
    "name",
    "name_key",
    "subject_type",
    "federal_district",
    "territory_km2",
    "population",
    "admin_center",
    "raw",
    "updated_at",
)


def item_to_row(item: Item) -> dict[str, Any]:
    """Преобразовать объект источника в словарь колонок.

    Здесь же считаются производные ключи (`region_key`, `name_key`): они нужны, чтобы
    сопоставлять вакансии со справочником регионов, и считаются один раз при записи,
    а не при каждом запросе.
    """
    row = item.to_dict()
    row["raw"] = json.dumps(item.raw, ensure_ascii=False)
    row["updated_at"] = utcnow()

    if isinstance(item, VacancyItem):
        row["region_key"] = normalize_name(item.region)
    elif isinstance(item, RegionItem):
        row["name_key"] = normalize_name(item.name)
    return row


def upsert_items(
    session: Session,
    model: type,
    items: Sequence[Item],
    updatable_columns: Sequence[str],
) -> tuple[int, int]:
    """Записать пачку объектов одного источника. Возвращает (вставлено, обновлено)."""
    if not items:
        return 0, 0

    rows = [item_to_row(item) for item in items]
    sources = {item.source for item in items}
    if len(sources) > 1:
        raise ValueError(f"в одной пачке ожидается один источник, получено: {sorted(sources)}")
    source = sources.pop()

    existing = _existing_ids(session, model, source, [item.external_id for item in items])
    inserted = sum(1 for item in items if item.external_id not in existing)
    updated = len(items) - inserted

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(model).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["source", "external_id"],
            set_={column: statement.excluded[column] for column in updatable_columns},
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(model).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["source", "external_id"],
            set_={column: getattr(statement.excluded, column) for column in updatable_columns},
        )
    else:  # pragma: no cover - на случай экзотической СУБД
        raise NotImplementedError(f"upsert не реализован для диалекта {dialect}")

    session.execute(statement)
    return inserted, updated


def upsert_vacancies(session: Session, items: Sequence[Item]) -> tuple[int, int]:
    """Записать вакансии."""
    return upsert_items(session, Vacancy, items, UPDATABLE_VACANCY_COLUMNS)


def upsert_regions(session: Session, items: Sequence[Item]) -> tuple[int, int]:
    """Записать справочник регионов."""
    return upsert_items(session, Region, items, UPDATABLE_REGION_COLUMNS)


def _existing_ids(session: Session, model: type, source: str, external_ids: Iterable[str]) -> set[str]:
    """Какие из этих записей уже есть в базе (нужно только для статистики)."""
    ids = list(external_ids)
    if not ids:
        return set()
    query = select(model.external_id).where(model.source == source, model.external_id.in_(ids))
    return set(session.scalars(query).all())


def save_raw_document(
    session: Session,
    *,
    source: str,
    url: str,
    payload: str,
    content_type: str = "text/html",
    note: str | None = None,
) -> RawDocument:
    """Сохранить сырой ответ источника, обновив его, если такой адрес уже встречался.

    На один адрес хранится один документ: иначе повторные прогоны раздували бы таблицу
    копиями одной и той же страницы.
    """
    document = session.scalar(
        select(RawDocument).where(RawDocument.source == source, RawDocument.url == url)
    )
    if document is None:
        document = RawDocument(source=source, url=url)
        session.add(document)

    document.content_type = content_type
    document.size_bytes = len(payload.encode("utf-8"))
    document.payload = payload
    document.note = note
    document.fetched_at = utcnow()
    return document


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


def count_regions(session: Session, source: str | None = None) -> int:
    """Сколько регионов в справочнике."""
    query = select(func.count(Region.id))
    if source:
        query = query.where(Region.source == source)
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


UNMATCHED = "не сопоставлено"


def _region_map_subquery():
    """Соответствие «нормализованное название региона → федеральный округ».

    Подзапрос с группировкой нужен, чтобы повтор ключа в справочнике не размножал вакансии
    и не завышал цифры в отчётах.
    """
    return (
        select(
            Region.name_key.label("name_key"),
            func.min(Region.federal_district).label("federal_district"),
        )
        .where(Region.name_key != "")
        .group_by(Region.name_key)
        .subquery()
    )


def vacancies_by_district(session: Session, limit: int = 20) -> list[tuple[str, int]]:
    """Вакансии в разрезе федеральных округов — то, ради чего собирался справочник."""
    region_map = _region_map_subquery()
    district = func.coalesce(region_map.c.federal_district, UNMATCHED).label("district")
    query = (
        select(district, func.count(Vacancy.id).label("total"))
        .select_from(Vacancy)
        .outerjoin(region_map, region_map.c.name_key == Vacancy.region_key)
        .group_by(district)
        .order_by(func.count(Vacancy.id).desc())
        .limit(limit)
    )
    return [(name or UNMATCHED, int(total)) for name, total in session.execute(query).all()]


@dataclass(slots=True)
class VacancyQuery:
    """Условия отбора вакансий для отчёта и выгрузки."""

    text: str | None = None
    region: str | None = None
    district: str | None = None
    salary_min: int | None = None
    remote_only: bool = False
    limit: int = 200


@dataclass(slots=True)
class VacancyRow:
    """Вакансия вместе с федеральным округом из справочника."""

    vacancy: Vacancy
    federal_district: str | None


def search_vacancies(session: Session, query_spec: VacancyQuery) -> list[VacancyRow]:
    """Найти вакансии по условиям. Используется командой выгрузки."""
    region_map = _region_map_subquery()
    query = (
        select(Vacancy, region_map.c.federal_district)
        .select_from(Vacancy)
        .outerjoin(region_map, region_map.c.name_key == Vacancy.region_key)
    )

    if query_spec.text:
        pattern = f"%{query_spec.text}%"
        query = query.where(
            or_(
                Vacancy.title.ilike(pattern),
                Vacancy.company.ilike(pattern),
                Vacancy.region.ilike(pattern),
            )
        )
    if query_spec.region:
        query = query.where(
            or_(Vacancy.region.ilike(f"%{query_spec.region}%"), Vacancy.region_key == normalize_name(query_spec.region))
        )
    if query_spec.district:
        query = query.where(region_map.c.federal_district.ilike(f"%{query_spec.district}%"))
    if query_spec.salary_min is not None:
        # Если верхняя граница не указана, сравниваем с нижней: это лучше, чем отбросить вакансию.
        query = query.where(
            func.coalesce(Vacancy.salary_max, Vacancy.salary_min) >= query_spec.salary_min
        )
    if query_spec.remote_only:
        # Признак удалёнки ищем в исходном JSON: отдельного поля источник не отдаёт.
        # Регистр проверяем явно, потому что SQLite-функция lower() не знает кириллицу.
        query = query.where(
            or_(
                Vacancy.raw.like("%удал%"),
                Vacancy.raw.like("%Удал%"),
                Vacancy.raw.like("%дистанц%"),
                Vacancy.raw.like("%Дистанц%"),
            )
        )

    query = query.order_by(Vacancy.published_at.desc(), Vacancy.id.desc()).limit(query_spec.limit)
    return [VacancyRow(vacancy=row[0], federal_district=row[1]) for row in session.execute(query).all()]


def region_match_stats(session: Session) -> tuple[int, int]:
    """Сколько вакансий удалось сопоставить со справочником из общего числа."""
    matched = (
        select(func.count(Vacancy.id))
        .select_from(Vacancy)
        .join(Region, Region.name_key == Vacancy.region_key)
        .where(Vacancy.region_key != "")
    )
    return int(session.scalar(matched) or 0), count_vacancies(session)
