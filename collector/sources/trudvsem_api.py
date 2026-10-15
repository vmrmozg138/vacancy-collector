"""Источник: открытые данные портала «Работа России».

API: https://opendata.trudvsem.ru/api/v1/vacancies
Публичное, без ключа, отдаёт JSON; постранично через offset/limit.
Проверено 15.10.2026: в базе ~491 000 вакансий, фильтр text работает (например, text=python → 409),
пагинация через offset подтверждена.

Почему этот источник первым: у него есть официальное API, а значит не нужен парсинг HTML
и нет риска сломаться от изменения вёрстки. Правило простое: сначала API, HTML — только
если API нет, браузер — только если данные рисует JavaScript.
"""

from __future__ import annotations

from typing import Any, Iterator

from collector.core.http import HttpClient
from collector.core.timeutils import parse_date
from collector.sources.base import VacancyItem, VacancySource


def _positive_int(value: Any) -> int | None:
    """Привести зарплату к int; 0 и мусор считаем отсутствием данных."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class TrudvsemSource(VacancySource):
    """Вакансии с портала «Работа России»."""

    name = "trudvsem"
    BASE_URL = "https://opendata.trudvsem.ru/api/v1/vacancies"
    MAX_LIMIT = 100

    def __init__(self, http: HttpClient, limit: int = 50, text: str | None = None) -> None:
        super().__init__(http)
        if not 1 <= limit <= self.MAX_LIMIT:
            raise ValueError(f"limit должен быть от 1 до {self.MAX_LIMIT}, получено {limit}")
        self.limit = limit
        self.text = text or None

    def iter_pages(self, pages: int) -> Iterator[dict[str, Any]]:
        """Отдать страницы API по offset. Пагинация через offset, а не page number."""
        for page_number in range(pages):
            params: dict[str, Any] = {"offset": page_number * self.limit, "limit": self.limit}
            if self.text:
                params["text"] = self.text
            yield self.http.get_json(self.BASE_URL, params=params)

    def parse_page(self, payload: Any) -> list[VacancyItem]:
        """Разобрать ответ API. Битые записи пропускаем, а не роняем запуск."""
        wrappers = ((payload or {}).get("results") or {}).get("vacancies") or []
        items: list[VacancyItem] = []
        for wrapper in wrappers:
            vacancy = (wrapper or {}).get("vacancy") or {}
            if not vacancy.get("id"):
                continue
            items.append(self._to_item(vacancy))
        return items

    @staticmethod
    def _to_item(vacancy: dict[str, Any]) -> VacancyItem:
        """Привести поля API к единому виду VacancyItem."""
        company = vacancy.get("company") or {}
        region = vacancy.get("region") or {}
        title = (vacancy.get("job-name") or vacancy.get("typicalPosition") or "").strip()
        return VacancyItem(
            source=TrudvsemSource.name,
            external_id=str(vacancy["id"]),
            title=title or "без названия",
            company=(company.get("name") or "").strip() or None,
            company_inn=(company.get("inn") or "").strip() or None,
            region=(region.get("name") or "").strip() or None,
            salary_min=_positive_int(vacancy.get("salary_min")),
            salary_max=_positive_int(vacancy.get("salary_max")),
            currency=(vacancy.get("currency") or "").strip() or None,
            url=(vacancy.get("vac_url") or "").strip() or None,
            published_at=parse_date(vacancy.get("creation-date")),
            raw=vacancy,
        )
