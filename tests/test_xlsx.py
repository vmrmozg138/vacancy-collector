"""Тесты выгрузки в Excel и представления вакансии.

Excel проверяем по содержимому файла, а не по картинке: листы, число строк, автофильтр
и то, что зарплата лежит числом (иначе Excel не отсортирует её как число).
Файл собирается в памяти: тесту не нужны файлы на диске — как и у выгрузки в CSV.
"""

from __future__ import annotations

import datetime
import io

import pytest

from collector.core.vacancy_view import experience_text, salary_text, shorten, vacancy_details
from collector.core.xlsx import write_vacancies_xlsx
from collector.sources.base import RegionItem, VacancyItem
from collector.storage.repository import VacancyQuery, search_vacancies, upsert_regions, upsert_vacancies

openpyxl = pytest.importorskip("openpyxl")


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
                currency="RUB",
                url="https://trudvsem.ru/vacancy/card/1",
                published_at=datetime.date(2026, 10, 1),
                raw={
                    "employment": "Дистанционная (удаленная) работа",
                    "schedule": "Полный рабочий день",
                    "skills": ["Навык работы с SQL", "Разработка программного обеспечения"],
                    "requirements": "Опыт от трёх лет",
                    "requirement": {"education": "Высшее образование", "experience": 3},
                    "duty": "Разработка внутренних инструментов",
                },
            ),
            VacancyItem(
                source="trudvsem",
                external_id="2",
                title="Аналитик",
                company="Василёк",
                region="Город Москва",
                salary_min=None,
                salary_max=None,
                raw={"duty": "Работа в офисе"},
            ),
        ],
    )
    session.commit()


def _export(session) -> "openpyxl.workbook.workbook.Workbook":
    """Выгрузить выборку в память и открыть её как книгу Excel."""
    rows = search_vacancies(session, VacancyQuery())
    stream = io.BytesIO()
    write_vacancies_xlsx(rows, stream, query_spec=VacancyQuery(), db_url="sqlite://")
    stream.seek(0)
    return openpyxl.load_workbook(stream)


def test_xlsx_has_two_sheets_and_autofilter(session) -> None:
    _fill(session)
    workbook = _export(session)

    assert workbook.sheetnames == ["Вакансии", "Сводка"]

    sheet = workbook["Вакансии"]
    assert sheet.max_row == 3  # шапка + две вакансии
    assert sheet.auto_filter.ref == "A1:O3"
    assert sheet["A1"].value == "Вакансия"


def test_xlsx_keeps_salary_as_number_and_links(session) -> None:
    _fill(session)
    sheet = _export(session)["Вакансии"]
    by_title = {sheet.cell(row=index, column=1).value: index for index in range(2, sheet.max_row + 1)}

    python_row = by_title["Python-разработчик"]
    assert sheet.cell(row=python_row, column=5).value == 150_000
    assert isinstance(sheet.cell(row=python_row, column=5).value, int)
    assert sheet.cell(row=python_row, column=8).value == "да (по данным работодателя)"
    assert sheet.cell(row=python_row, column=15).hyperlink.target == "https://trudvsem.ru/vacancy/card/1"

    # Вакансия без зарплаты не должна показывать ноль или пустое место в колонке «как в вакансии».
    analyst_row = by_title["Аналитик"]
    assert sheet.cell(row=analyst_row, column=7).value == "не указана"
    # Опыт из справочного поля requirement: у первой вакансии он есть, у второй — нет.
    assert sheet.cell(row=python_row, column=9).value == 3
    assert sheet.cell(row=analyst_row, column=9).value is None


def test_xlsx_summary_sheet_counts(session) -> None:
    _fill(session)
    sheet = _export(session)["Сводка"]
    values = {
        sheet.cell(row=index, column=1).value: sheet.cell(row=index, column=2).value
        for index in range(1, sheet.max_row + 1)
    }
    assert values["вакансий в отчёте"] == 2
    assert values["удалённо (по данным работодателя)"] == 1
    assert values["максимальная зарплата"] == 200_000


def test_salary_text_variants() -> None:
    assert salary_text(150_000, 200_000, "RUB") == "150\u00a0000 – 200\u00a0000 ₽"
    assert salary_text(150_000, None, "RUB") == "от 150\u00a0000 ₽"
    assert salary_text(150_000, 150_000, "RUB") == "150\u00a0000 ₽"
    assert salary_text(None, None, "RUB") == "не указана"
    # Источник присылает валюту как ««руб.»» — в отчёте это должно стать знаком рубля.
    assert salary_text(90_000, None, "«руб.»") == "от 90\u00a0000 ₽"
    # А незнакомая валюта не подменяется рублём.
    assert salary_text(1_000, None, "USD") == "от 1\u00a0000 USD"


def test_shorten_keeps_words_and_adds_ellipsis() -> None:
    assert shorten("короткий текст") == "короткий текст"
    long_text = " ".join(["слово"] * 100)
    result = shorten(long_text, 40)
    assert len(result) <= 40
    assert result.endswith("…")
    # Обрезка идёт по границе слова: последнее слово не должно быть огрызком.
    assert result[:-1].strip().endswith("слово")


def test_vacancy_details_takes_fields_from_raw() -> None:
    details = vacancy_details(
        '{"employment": "Дистанционная (удаленная) работа", "skills": ["SQL", "Python"], '
        '"requirement": {"education": "Высшее", "experience": 3}, "duty": "текст"}'
    )
    assert details["employment"] == "Дистанционная (удаленная) работа"
    assert details["skills"] == "SQL Python"
    assert details["duty"] == "текст"
    assert details["experience"] == 3
    assert details["education"] == "Высшее"


def test_vacancy_details_survives_broken_json() -> None:
    assert vacancy_details("{не json")["duty"] == ""
    assert vacancy_details(None)["employment"] == ""
    # Источник может поменять тип поля: разбор обязан это пережить, а не упасть.
    details = vacancy_details('{"employment": ["Дистанционная работа"], "requirement": "нет данных"}')
    assert details["employment"] == "Дистанционная работа"
    assert details["experience"] is None


def test_experience_text_agrees_with_number() -> None:
    assert experience_text(1) == "1 год"
    assert experience_text(3) == "3 года"
    assert experience_text(5) == "5 лет"
    assert experience_text(11) == "11 лет"
    assert experience_text(21) == "21 год"
    # Ноль у источника — «не заполнено», а не «без опыта»: см. докстроку experience_text.
    assert experience_text(0) == "не указан"
    assert experience_text(None) == "не указан"
