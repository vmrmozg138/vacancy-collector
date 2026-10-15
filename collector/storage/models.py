"""Модели базы данных.

Две таблицы:
  vacancies — сами вакансии, по одной строке на вакансию;
  runs      — журнал запусков (сколько скачали, сколько записали, были ли ошибки).

Ключевое решение: уникальный индекс по паре (source, external_id).
Именно он делает повторный запуск безопасным: одна и та же вакансия из одного
и того же источника не может попасть в таблицу дважды.
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
        Index("ix_vacancy_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[str | None] = mapped_column(String(500))
    company_inn: Mapped[str | None] = mapped_column(String(20))
    region: Mapped[str | None] = mapped_column(String(200))
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
