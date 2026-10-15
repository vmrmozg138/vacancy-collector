"""Модели базы данных.

Таблицы:

* `vacancies` — вакансии, по одной строке на вакансию;
* `regions` — справочник регионов (источник — Википедия): тип субъекта, федеральный округ,
  население, административный центр. Нужен, чтобы обогащать основные данные;
* `runs` — журнал запусков: сколько скачали, сколько записали, были ли ошибки;
* `raw_documents` — сырые ответы источников как есть (например, HTML после рендеринга браузером).

Ключевое решение: уникальный индекс по паре (source, external_id) в каждой таблице данных.
Именно он делает повторный запуск безопасным: одна и та же запись из одного источника
не может попасть в таблицу дважды.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from collector.core.timeutils import utcnow


class Base(DeclarativeBase):
    """Базовый класс моделей."""


class Vacancy(Base):
    """Вакансия из внешнего источника."""

    __tablename__ = "vacancies"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_vacancy_source_external"),
        Index("ix_vacancy_region", "region"),
        Index("ix_vacancy_region_key", "region_key"),
        Index("ix_vacancy_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[str | None] = mapped_column(String(500))
    company_inn: Mapped[str | None] = mapped_column(String(20))
    region: Mapped[str | None] = mapped_column(String(200))
    #: Нормализованное название региона — ключ для связи со справочником `regions`.
    region_key: Mapped[str | None] = mapped_column(String(200))
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(20))
    url: Mapped[str | None] = mapped_column(String(1000))
    published_at: Mapped[date | None] = mapped_column(Date)
    # Исходный JSON: позволяет переразобрать данные, не скачивая их заново.
    raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - удобство отладки
        return f"<Vacancy {self.source}:{self.external_id} {self.title[:40]!r}>"


class Region(Base):
    """Субъект РФ из справочника.

    Название и нормализованный ключ хранятся рядом: ключ нужен для сопоставления,
    а показывать пользователю всегда нужно исходное название источника.
    """

    __tablename__ = "regions"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_region_source_external"),
        Index("ix_region_name_key", "name_key"),
        Index("ix_region_district", "federal_district"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Нормализованное название — ключ связи с вакансиями.
    name_key: Mapped[str | None] = mapped_column(String(300))
    subject_type: Mapped[str | None] = mapped_column(String(100))
    federal_district: Mapped[str | None] = mapped_column(String(200))
    territory_km2: Mapped[int | None] = mapped_column(Integer)
    population: Mapped[int | None] = mapped_column(Integer)
    admin_center: Mapped[str | None] = mapped_column(String(200))
    raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - удобство отладки
        return f"<Region {self.name!r} district={self.federal_district!r}>"


class RawDocument(Base):
    """Сырой ответ источника, сохранённый как есть.

    Нужен для случаев, когда разбор данных откладывается: например, HTML, полученный
    после выполнения JavaScript в браузере. На один адрес хранится один ответ,
    поэтому повторный прогон не раздувает таблицу.
    """

    __tablename__ = "raw_documents"
    __table_args__ = (UniqueConstraint("source", "url", name="uq_raw_document_source_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), default="text/html")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class RunLog(Base):
    """Журнал запусков сборщика."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    items_parsed: Mapped[int] = mapped_column(Integer, default=0)
    items_inserted: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    message: Mapped[str | None] = mapped_column(Text)

    def as_dict(self) -> dict[str, Any]:  # pragma: no cover - используется в CLI
        return {
            "source": self.source,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pages": self.pages_fetched,
            "parsed": self.items_parsed,
            "inserted": self.items_inserted,
            "updated": self.items_updated,
            "skipped": self.items_skipped,
            "errors": self.errors,
            "status": self.status,
        }
