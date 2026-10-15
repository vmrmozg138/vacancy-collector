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
    rows = search_vacancies(session, VacancyQuery(text="Ромашка"))
    assert sorted(row.vacancy.external_id for row in rows) == ["1", "3"]


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
