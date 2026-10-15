"""Тесты источника «Работа России» и записи в базу.

Проверяем две вещи, которые важнее всего в реальном сборщике:
  1) разбор ответа источника — на сохранённой фикстуре, без сети;
  2) идемпотентность записи — повторный запуск не создаёт дублей.
"""

from __future__ import annotations

from typing import Any, Iterator
from sqlalchemy import select

from collector.storage.models import Vacancy

from collector.core.http import HttpClient
from collector.core.pipeline import run_source
from collector.core.stats import RunStats
from collector.sources.base import VacancyItem, Source
from collector.sources.trudvsem_api import TrudvsemSource
from collector.storage.repository import count_vacancies, upsert_vacancies


def make_source(limit: int = 5) -> TrudvsemSource:
    """Источник с фиктивным HTTP-клиентом: сеть в тестах не нужна."""
    return TrudvsemSource(HttpClient("test-agent", delay=0), limit=limit)


def test_parse_page_returns_items(trudvsem_payload: dict) -> None:
    items = make_source().parse_page(trudvsem_payload)
    assert len(items) == len(trudvsem_payload["results"]["vacancies"])
    assert all(item.source == "trudvsem" for item in items)
    assert all(item.external_id for item in items)
    assert all(item.title for item in items)


def test_items_are_normalized(trudvsem_payload: dict) -> None:
    items = make_source().parse_page(trudvsem_payload)
    first = items[0]
    # Поля API приведены к нашим именам, данные не потеряны.
    assert first.region
    assert first.url and first.url.startswith("http")
    assert first.company_inn is None or first.company_inn.isdigit()
    assert first.salary_min is None or first.salary_min > 0
    assert first.raw, "исходный JSON должен сохраняться для повторного разбора"


def test_broken_items_are_skipped() -> None:
    payload: dict[str, Any] = {
        "results": {"vacancies": [{"vacancy": {}}, {"vacancy": {"id": "1", "job-name": "Тест"}}, {}]}
    }
    items = make_source().parse_page(payload)
    assert len(items) == 1
    assert items[0].external_id == "1"


def test_parse_empty_payload_is_safe() -> None:
    assert make_source().parse_page({}) == []
    assert make_source().parse_page(None) == []


def test_upsert_is_idempotent(session, trudvsem_payload: dict) -> None:
    items = make_source().parse_page(trudvsem_payload)

    inserted, updated = upsert_vacancies(session, items)
    session.commit()
    assert (inserted, updated) == (len(items), 0)
    assert count_vacancies(session) == len(items)

    # Второй запуск: ни одной новой строки, все записи обновлены.
    inserted, updated = upsert_vacancies(session, items)
    session.commit()
    assert (inserted, updated) == (0, len(items))
    assert count_vacancies(session) == len(items)

def test_same_external_id_from_different_sources(session) -> None:
    """Ключ — пара (source, external_id): совпадение id у разных источников не конфликтует."""
    from_first = VacancyItem(source="trudvsem", external_id="42", title="Вакансия из первого источника")
    from_second = VacancyItem(source="hh-подобный", external_id="42", title="Вакансия из второго источника")

    inserted, updated = upsert_vacancies(session, [from_first])
    session.commit()
    assert (inserted, updated) == (1, 0)

    # Тот же external_id, но другой источник: это ДОБАВЛЕНИЕ, а не обновление первой записи.
    inserted, updated = upsert_vacancies(session, [from_second])
    session.commit()
    assert (inserted, updated) == (1, 0)

    assert count_vacancies(session) == 2
    titles = set(session.scalars(select(Vacancy.title)).all())
    assert titles == {"Вакансия из первого источника", "Вакансия из второго источника"}




def test_upsert_updates_changed_field(session, trudvsem_payload: dict) -> None:
    items = make_source().parse_page(trudvsem_payload)
    upsert_vacancies(session, items)
    session.commit()

    changed = items[0]
    changed.title = "Обновлённое название"
    upsert_vacancies(session, [changed])
    session.commit()

    from collector.storage.models import Vacancy

    stored = session.query(Vacancy).filter_by(external_id=changed.external_id).one()
    assert stored.title == "Обновлённое название"
    assert count_vacancies(session) == len(items)


class _StubSource(Source):
    """Источник-заглушка: отдаёт заранее подготовленные страницы."""

    name = "stub"

    def __init__(self, pages: list[Any]) -> None:
        self._pages = pages
        self.http = None  # type: ignore[assignment]

    def iter_pages(self, pages: int) -> Iterator[Any]:
        yield from self._pages[:pages]

    def parse_page(self, payload: Any) -> list[VacancyItem]:
        return [
            VacancyItem(source=self.name, external_id=str(payload), title=f"Вакансия {payload}")
        ]

class _FlakySource(_StubSource):
    """Страница «плохая» падает при разборе, остальные разбираются нормально."""

    def parse_page(self, payload):
        if payload == "плохая":
            raise ValueError("битая страница")
        return super().parse_page(payload)

def test_pipeline_continues_after_parse_error(session) -> None:
    """Сбой разбора одной страницы не останавливает прогон: остальные записываются."""
    import logging

    _, session_factory = _session_factory_for(session)
    stats = run_source(
        _FlakySource(["один", "плохая", "три"]),
        session_factory,
        pages=3,
        logger=logging.getLogger("test"),
    )

    assert stats.pages_fetched == 3   # страницу скачали и посчитали
    assert stats.errors == 1          # ошибку зафиксировали
    assert stats.status == "ok"       # но прогон не провальный — это не сбой получения
    assert stats.items_inserted == 2  # уцелевшие страницы записаны
    assert count_vacancies(session) == 2


def test_pipeline_writes_data_and_run_log(session) -> None:
    import logging

    _, session_factory = _session_factory_for(session)
    stats = run_source(_StubSource([1, 2, 3]), session_factory, pages=2, logger=logging.getLogger("test"))

    assert isinstance(stats, RunStats)
    assert stats.pages_fetched == 2
    assert stats.items_inserted == 2
    assert stats.errors == 0
    assert count_vacancies(session) == 2


def test_pipeline_survives_source_failure(session) -> None:
    import logging

    class _BrokenSource(_StubSource):
        def iter_pages(self, pages: int) -> Iterator[Any]:
            raise RuntimeError("источник недоступен")

    _, session_factory = _session_factory_for(session)
    stats = run_source(_BrokenSource([]), session_factory, pages=1, logger=logging.getLogger("test"))

    assert stats.status == "error"
    assert stats.errors == 1
    assert "недоступен" in (stats.message or "")


def _session_factory_for(session):
    """Фабрика сессий, отдающая ту же самую сессию, что и тест."""
    from contextlib import contextmanager

    @contextmanager
    def factory():
        yield session

    return session.get_bind(), factory
