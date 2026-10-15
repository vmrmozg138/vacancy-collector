"""Контракт источника данных.

Источник умеет три вещи:
  1) iter_pages() — отдать сырые ответы (страницы API, HTML, документы источника);
  2) parse_page() — превратить сырой ответ в список объектов предметной области;
  3) сообщить своё имя.

Про базу источник не знает ничего: этим занимается конвейер.
Благодаря этому парсер тестируется на сохранённой фикстуре, без сети.

Вакансии — основной предмет проекта, но не единственный: справочник регионов
приходит из другого источника и описывается своим объектом. Оба вида объектов
подчиняются одному требованию: у них есть `source`, `external_id` и `to_dict()`.
Именно поэтому запись в базу (upsert) реализована один раз для всех.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterator, Union

from collector.core.http import HttpClient
from collector.core.robots import RobotsCache


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


@dataclass(slots=True)
class RegionItem:
    """Субъект РФ из справочника (тип, федеральный округ, население, админ. центр)."""

    source: str
    external_id: str
    name: str
    subject_type: str | None = None
    federal_district: str | None = None
    territory_km2: int | None = None
    population: int | None = None
    admin_center: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        return data


#: Любой объект, который источник умеет отдавать, а хранилище — записывать.
Item = Union[VacancyItem, RegionItem]


class Source(ABC):
    """Базовый класс источника данных: API, HTML-страница, справочник."""

    name: str = "base"
    #: Класс-атрибут, чтобы источники-заглушки в тестах работали без вызова __init__.
    robots: RobotsCache | None = None

    def __init__(self, http: HttpClient, robots: RobotsCache | None = None) -> None:
        self.http = http
        self.robots = robots

    def can_fetch(self, url: str) -> bool:
        """Разрешён ли адрес по robots.txt. Без настроенной проверки — разрешено.

        Источники, которые ходят на сайты (а не в API), обязаны спрашивать это
        перед загрузкой страницы: отказ должен быть виден в логе, а не молча проигнорирован.
        """
        if self.robots is None:
            return True
        return self.robots.can_fetch(url)

    @abstractmethod
    def iter_pages(self, pages: int) -> Iterator[Any]:
        """Отдать сырые страницы данных (по одной за итерацию)."""

    @abstractmethod
    def parse_page(self, payload: Any) -> list[Item]:
        """Разобрать сырую страницу в список объектов предметной области."""

    def __repr__(self) -> str:  # pragma: no cover - удобство отладки
        return f"<{type(self).__name__} name={self.name!r}>"
