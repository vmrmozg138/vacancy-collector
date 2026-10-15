"""Тесты источника «справочник регионов» — на фрагментах реальных страниц Википедии.

Сеть здесь не нужна: фикстуры лежат в `tests/fixtures` и обновляются скриптом
`python -m tools.refresh_fixtures`. Проверяем именно то, на чём обычно ломается
разбор HTML: строки-разделители внутри таблицы, служебные вставки в ячейках,
ссылки, заголовок которых не совпадает с видимым текстом, и связь двух документов.
"""

from __future__ import annotations

import json
import logging
import pathlib
from contextlib import contextmanager

import pytest

from collector.core.normalize import normalize_name
from collector.core.pipeline import run_source
from collector.sources.wikipedia_regions import (
    DISTRICTS_URL,
    SUBJECTS_URL,
    WikipediaRegionsSource,
    clean_text,
    to_int,
)
from collector.storage.repository import (
    count_regions,
    region_match_stats,
    upsert_regions,
    upsert_vacancies,
    vacancies_by_district,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def subjects_html() -> str:
    return _read("wikipedia_subjects.html")


@pytest.fixture(scope="module")
def districts_html() -> str:
    return _read("wikipedia_districts.html")


@pytest.fixture()
def source() -> WikipediaRegionsSource:
    return WikipediaRegionsSource(http=None)  # type: ignore[arg-type]


class _FixtureHttp:
    """Подменяет HTTP: отдаёт фикстуры вместо похода в сеть."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self.pages[url]


def test_to_int_extracts_number_from_noisy_text() -> None:
    assert to_int("↘210 095") == 210095
    assert to_int("26 517 (де-юре)") == 26517
    assert to_int("7792") == 7792
    assert to_int("") is None
    assert to_int("нет данных") is None


def test_to_int_takes_first_number_and_rejects_absurd_values() -> None:
    """В ячейке бывает несколько чисел: служебный ключ сортировки и само значение.

    Склейка всех цифр однажды дала квадриллион и уронила запись в PostgreSQL
    (`integer out of range`), поэтому проверяем и это.
    """
    assert to_int("17 125 191, 17 098 246") == 17125191
    assert to_int("↘210 095 (2021)") == 210095
    # Если числа разделены только пробелами, отличить одно от другого нельзя:
    # лучше не записать ничего, чем записать бессмыслицу.
    assert to_int("99999999999") is None
    assert to_int("17 125 19117 125 191") is None


def test_clean_text_drops_service_markup() -> None:
    from lxml import html

    element = html.fromstring(
        "<td>Показатель"
        "<style>.mw-parser-output .x{color:red}</style>"
        "<span style='display:none'>1e-06</span>"
        "<sup class='reference'>[6]</sup>"
        " 42</td>"
    )
    assert clean_text(element) == "Показатель 42"


def test_subject_types_come_from_section_rows(source: WikipediaRegionsSource, subjects_html: str) -> None:
    items = source.parse_subjects(subjects_html)

    assert items, "таблица не разобралась"
    types = {item.subject_type for item in items}
    # В фикстуре по три субъекта из каждого раздела, поэтому типы должны быть все.
    assert {"Республики", "Края", "Области", "Города федерального значения"} <= types


def test_first_item_fields(source: WikipediaRegionsSource, subjects_html: str) -> None:
    first = source.parse_subjects(subjects_html)[0]

    assert first.source == "wikipedia-regions"
    assert first.name == "Республика Адыгея"
    assert first.subject_type == "Республики"
    assert first.territory_km2 == 7792
    assert first.population and first.population > 0
    assert first.admin_center == "Майкоп"
    # Ссылка в Википедии ведёт на «Адыгея», а не на «Республика Адыгея»:
    # идентификатор берём из ссылки, он стабильнее заголовка.
    assert first.external_id == "Адыгея"


def test_no_service_text_in_fields(source: WikipediaRegionsSource, subjects_html: str) -> None:
    for item in source.parse_subjects(subjects_html):
        assert "mw-parser-output" not in (item.admin_center or "")
        assert not (item.name or "").startswith(".")
        assert item.territory_km2 is None or item.territory_km2 > 0


def test_districts_are_parsed_into_mapping(source: WikipediaRegionsSource, districts_html: str) -> None:
    mapping = source.parse_districts(districts_html)

    assert len(mapping) > 40
    assert mapping[normalize_name("Москва")] == "Центральный федеральный округ"
    assert mapping[normalize_name("Республика Крым")] == "Южный федеральный округ"
    assert mapping[normalize_name("Кемеровская область — Кузбасс")] == "Сибирский федеральный округ"


def test_two_documents_are_joined(source: WikipediaRegionsSource, subjects_html: str, districts_html: str) -> None:
    mapping = source.parse_districts(districts_html)
    items = source.parse_subjects(subjects_html, mapping)

    matched = [item for item in items if item.federal_district]
    assert len(matched) / len(items) > 0.9, "больше 90% субъектов должны получить федеральный округ"
    assert any(item.federal_district == "Центральный федеральный округ" for item in items)


def test_pipeline_stores_regions_and_is_idempotent(source: WikipediaRegionsSource, session) -> None:
    """Справочник проходит тот же конвейер, что и вакансии, но пишется в другую таблицу."""
    pages = {SUBJECTS_URL: _read("wikipedia_subjects.html"), DISTRICTS_URL: _read("wikipedia_districts.html")}
    source = WikipediaRegionsSource(http=_FixtureHttp(pages))  # type: ignore[arg-type]

    @contextmanager
    def session_factory():
        yield session

    stats = run_source(source, session_factory, pages=1, logger=logging.getLogger("test"), sink=upsert_regions)

    assert stats.status == "ok"
    assert stats.items_inserted == stats.items_parsed > 0
    assert count_regions(session) == stats.items_inserted

    # Повторный прогон обновляет, но не дублирует.
    stats_again = run_source(source, session_factory, pages=1, logger=logging.getLogger("test"), sink=upsert_regions)
    assert stats_again.items_inserted == 0
    assert stats_again.items_updated == stats_again.items_parsed
    assert count_regions(session) == stats.items_parsed


def test_district_report_joins_vacancies_with_reference(session, source: WikipediaRegionsSource) -> None:
    """Вакансии сопоставляются со справочником по нормализованному названию региона."""
    regions = source.parse_subjects(
        _read("wikipedia_subjects.html"), source.parse_districts(_read("wikipedia_districts.html"))
    )
    upsert_regions(session, regions)
    session.commit()

    from collector.sources.base import VacancyItem

    upsert_vacancies(
        session,
        [
            VacancyItem(source="trudvsem", external_id="1", title="А", region="Город Москва"),
            VacancyItem(source="trudvsem", external_id="2", title="Б", region="Республика Адыгея"),
            VacancyItem(source="trudvsem", external_id="3", title="В", region="Нигде не найдено"),
        ],
    )
    session.commit()

    report = dict(vacancies_by_district(session))
    assert report["Центральный федеральный округ"] == 1
    # Адыгея в фикстуре есть, но округ для неё известен только если попал в обрезку списка.
    assert report.get("не сопоставлено") == 1

    matched, total = region_match_stats(session)
    assert total == 3
    assert matched == 2
