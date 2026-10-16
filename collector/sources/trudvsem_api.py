"""Источник: открытые данные портала «Работа России».

API: https://opendata.trudvsem.ru/api/v1/vacancies
Публичное, без ключа, отдаёт JSON; постранично через offset/limit.
Проверено 15.10.2026: в базе ~491 000 вакансий, фильтр text работает (например, text=python → 409),
пагинация через offset подтверждена.

**Что выяснилось 16.10.2026 (важно для сбора больших объёмов):**

* пагинация фактически ограничена: `limit=100&offset=100` отвечает HTTP 500, глубокий offset —
  пустой ответ, а `meta.total` не описывает доступное (заявлено 406 по «python», реально видны 106).
  То есть **один запрос отдаёт около сотни записей, и страницами больше не получить**;
* зато работает фильтр по дате изменения — `modifiedFrom`/`modifiedTo`, причём формат строгий:
  только ISO с поясом (`2026-10-15T00:00:00+03:00` или `...Z`); `+0300`, миллисекунды и просто дата
  дают HTTP 500;
* если в окне времени записей меньше лимита, источник отдаёт их **все** (проверено: `text=python`
  за сутки — 16 записей из 16). Это и есть способ собрать больше сотни: `core/sweep.py` делит
  период пополам, пока окно не станет достаточно узким.

Прочие параметры (`dateFrom`, `regionCode`, `companyCode`, `sort`, `order`) источник молча
игнорирует — проверено, выдача и `total` не меняются.

Почему этот источник первым: у него есть официальное API, а значит не нужен парсинг HTML
и нет риска сломаться от изменения вёрстки. Правило простое: сначала API, HTML — только
если API нет, браузер — только если данные рисует JavaScript.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from collector.core.http import HttpClient
from collector.core.timeutils import parse_date, to_api_moment
from collector.sources.base import VacancyItem, Source


def _positive_int(value: Any) -> int | None:
    """Привести зарплату к int; 0 и мусор считаем отсутствием данных."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class TrudvsemSource(Source):
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

    def fetch_window(
        self,
        modified_from: datetime | None = None,
        modified_to: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Запросить вакансии, изменённые в указанном промежутке времени.

        Это единственный рабочий способ получить больше сотни записей: пагинация у источника
        сломана, а окно по времени отдаётся целиком, если записей в нём меньше лимита.
        Границы передаются в UTC с «Z» — см. `to_api_moment`.
        """
        params: dict[str, Any] = {"limit": limit or self.limit}
        if self.text:
            params["text"] = self.text
        if modified_from is not None:
            params["modifiedFrom"] = to_api_moment(modified_from)
        if modified_to is not None:
            params["modifiedTo"] = to_api_moment(modified_to)
        return self.http.get_json(self.BASE_URL, params=params)

    @staticmethod
    def window_total(payload: Any) -> int:
        """Сколько всего записей в этом окне по данным источника.

        Именно с этим числом сравнивается размер полученной пачки: если источник отдал меньше,
        чем у него есть, окно надо делить.
        """
        meta = (payload or {}).get("meta") or {}
        try:
            return int(meta.get("total") or 0)
        except (TypeError, ValueError):
            return 0

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
