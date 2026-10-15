"""Контракт источника данных.

Источник умеет три вещи:
  1) iter_pages() — отдать сырые ответы (страницы API или HTML);
  2) parse_page() — превратить сырой ответ в список VacancyItem;
  3) сообщить своё имя.

Про базу источник не знает ничего: этим занимается конвейер.
Благодаря этому парсер тестируется на сохранённой фикстуре, без сети.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterator

from collector.core.http import HttpClient


@dataclass(slots=True)
class VacancyItem:
    """Вакансия в том виде, в каком её понимает наше приложение.

    Поля одинаковы для любого источника: разные API называют их по-своему,
    а приводятся они к этому виду в parse_page() конкретного источника.
    """

    source: str
    external_id: str
    title: str
    company: str | None = None
    company_inn: str | None = None
    region: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    currency: str | None = None
    url: str | None = None
    published_at: date | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Поля без raw: raw сериализуется отдельно."""
        data = asdict(self)
        data.pop("raw", None)
        return data


class VacancySource(ABC):
    """Базовый класс источника вакансий."""

    name: str = "base"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @abstractmethod
    def iter_pages(self, pages: int) -> Iterator[Any]:
        """Отдать сырые страницы данных (по одной за итерацию)."""

    @abstractmethod
    def parse_page(self, payload: Any) -> list[VacancyItem]:
        """Разобрать сырую страницу в список вакансий."""

    def __repr__(self) -> str:  # pragma: no cover - удобство отладки
        return f"<{type(self).__name__} name={self.name!r}>"
