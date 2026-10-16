"""Тесты отбора вакансий и выгрузки в CSV.

Проверяем то, чем действительно пользуются: фильтры по региону, округу, зарплате и удалёнке,
а также формат файла. Выгрузка пишется в поток в памяти — тесту не нужны файлы на диске.
"""

from __future__ import annotations

import csv
import io

from collector.core.export import FIELDS, write_vacancies_csv
from collector.sources.base import RegionItem, VacancyItem
from collector.storage.repository import (
    VacancyQuery,
    count_search_vacancies,
    search_vacancies,
    upsert_regions,
    upsert_vacancies,
)


def _fill(session) -> None:
    upsert_regions(
        session,
        [
            RegionItem(
                source="wikipedia-regions",
                external_id="Москва",
                name="Москва",
                federal_district="Центральный федеральный округ",
            ),
            RegionItem(
                source="wikipedia-regions",
                external_id="Новосибирская область",
                name="Новосибирская область",
                federal_district="Сибирский федеральный округ",
            ),
        ],
    )
    upsert_vacancies(
        session,
        [
            VacancyItem(
                source="trudvsem",
                external_id="1",
                title="Python-разработчик",
                company="Ромашка",
                region="Город Москва",
                salary_min=150_000,
                salary_max=200_000,
                raw={"duty": "Работа удалённо, гибкий график"},
            ),
            VacancyItem(
                source="trudvsem",
                external_id="2",
                title="Аналитик",
                company="Василёк",
                region="Новосибирская область",
                salary_min=90_000,
                salary_max=120_000,
                raw={"duty": "Работа в офисе"},
            ),
            VacancyItem(
                source="trudvsem",
                external_id="3",
                title="Программист 1С",
                company="Ромашка",
                region="Новосибирская область",
                salary_min=None,
                salary_max=None,
                raw={"duty": "Удалённая работа по договорённости"},
            ),
        ],
    )
    session.commit()


def test_filter_by_region_substring(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery(region="москва"))
    assert [row.vacancy.external_id for row in rows] == ["1"]


def test_filter_by_district_uses_reference(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery(district="Сибирский"))
    assert sorted(row.vacancy.external_id for row in rows) == ["2", "3"]
    assert all(row.federal_district == "Сибирский федеральный округ" for row in rows)


def test_filter_by_salary_uses_upper_bound(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery(salary_min=130_000))
    assert [row.vacancy.external_id for row in rows] == ["1"]


def test_filter_by_text(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery(texts=("Ромашка",)))
    assert sorted(row.vacancy.external_id for row in rows) == ["1", "3"]


def test_text_search_ignores_case_in_russian(session) -> None:
    """«программист» должен находить «Программист 1С».

    В SQLite `lower()` не понимает кириллицу, поэтому поиск идёт по отдельному ключу
    в нижнем регистре: без него терялись названия с заглавной буквы (проверено на живой
    базе: 63 вакансии из 73).
    """
    _fill(session)
    lower = search_vacancies(session, VacancyQuery(texts=("программист",)))
    upper = search_vacancies(session, VacancyQuery(texts=("Программист",)))
    assert sorted(row.vacancy.external_id for row in lower) == ["3"]
    assert sorted(row.vacancy.external_id for row in upper) == ["3"]


def test_text_filter_accepts_several_words(session) -> None:
    """Несколько подстрок соединяются через «или»: одним отчётом собирается целое направление."""
    _fill(session)
    rows = search_vacancies(session, VacancyQuery(texts=("программист", "аналитик")))
    assert sorted(row.vacancy.external_id for row in rows) == ["2", "3"]

    # Пустой список — фильтр не применяется, видны все вакансии.
    assert len(search_vacancies(session, VacancyQuery(texts=()))) == 3


def test_exclude_removes_unwanted_titles(session) -> None:
    """Исключение отсекает мусор по названию и тоже не зависит от регистра."""
    upsert_vacancies(
        session,
        [
            VacancyItem(source="trudvsem", external_id="10", title="Дворник", company="ЖЭК"),
            VacancyItem(source="trudvsem", external_id="11", title="Плиточник", company="Ремстрой"),
            VacancyItem(source="trudvsem", external_id="12", title="Python-разработчик", company="Ромашка"),
        ],
    )
    session.commit()

    query = VacancyQuery(exclude_text=("дворник", "плиточник", "уборщик"))
    rows = search_vacancies(session, query)
    assert [row.vacancy.external_id for row in rows] == ["12"]

    # Исключение работает вместе с остальными фильтрами.
    assert search_vacancies(session, VacancyQuery(exclude_text=("дворник", "плиточник"), texts=("плиточник",))) == []
    assert count_search_vacancies(session, query) == 1


def test_search_keys_are_filled_for_old_rows(session) -> None:
    """Записи, добавленные до появления ключей, дозаполняются, а не выпадают из поиска."""
    from sqlalchemy import select

    from collector.storage.models import Vacancy
    from collector.storage.repository import ensure_search_keys

    session.add(
        Vacancy(source="trudvsem", external_id="99", title="Программист", company="Ромашка", title_key=None, company_key=None)
    )
    session.commit()
    assert search_vacancies(session, VacancyQuery(texts=("программист",))) == []

    filled = ensure_search_keys(_factory_for(session))
    session.commit()

    assert filled == 1
    stored = session.scalars(select(Vacancy).where(Vacancy.external_id == "99")).one()
    assert stored.title_key == "программист"
    assert stored.company_key == "ромашка"
    assert [row.vacancy.external_id for row in search_vacancies(session, VacancyQuery(texts=("программист",)))] == ["99"]


def _factory_for(session):
    from contextlib import contextmanager

    @contextmanager
    def factory():
        yield session

    return factory


def test_remote_filter_finds_both_cases(session) -> None:
    """Признак ищем в исходном JSON и не зависим от регистра: SQLite lower() не знает кириллицу."""
    _fill(session)
    rows = search_vacancies(session, VacancyQuery(remote_only=True))
    assert sorted(row.vacancy.external_id for row in rows) == ["1", "3"]


def test_limit_is_applied(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery(limit=1))
    assert len(rows) == 1


def test_csv_export_format(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery())

    stream = io.StringIO()
    written = write_vacancies_csv(rows, stream)

    assert written == 3
    stream.seek(0)
    reader = csv.DictReader(stream, delimiter=";")
    assert reader.fieldnames == list(FIELDS)

    # Порядок строк задаётся базой, поэтому сверяем по идентификатору, а не по позиции.
    by_id = {row["external_id"]: row for row in reader}
    assert set(by_id) == {"1", "2", "3"}
    assert by_id["1"]["title"] == "Python-разработчик"
    assert by_id["1"]["federal_district"] == "Центральный федеральный округ"
    assert by_id["1"]["salary_max"] == "200000"
    # Строки без зарплаты не должны превращаться в «None» в файле.
    assert by_id["3"]["salary_min"] == ""
