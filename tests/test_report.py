"""Тесты HTML-отчёта.

Проверяем то, за что отчёт и нужен: цифры в сводке, разделение двух признаков удалёнки
и безопасность вывода — данные приходят из внешнего источника, поэтому название компании
не должно попадать в страницу как разметка.
"""

from __future__ import annotations

from collector.core.report import build_summary, render_report_html
from collector.sources.base import RegionItem, VacancyItem
from collector.storage.repository import VacancyQuery, search_vacancies, upsert_regions, upsert_vacancies


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
                url="https://trudvsem.ru/vacancy/card/1",
                raw={"employment": "Дистанционная (удаленная) работа", "duty": "Писать код на Python"},
            ),
            VacancyItem(
                source="trudvsem",
                external_id="2",
                title="Аналитик",
                # Название компании — проверка экранирования: попади оно в разметку как есть,
                # страница сломалась бы, а чужой текст выполнялся бы в браузере.
                company='ООО "<script>alert(1)</script>"',
                region="Город Москва",
                salary_min=90_000,
                salary_max=120_000,
                raw={"duty": "Работа удалённо, отчёты"},
            ),
            VacancyItem(
                source="trudvsem",
                external_id="3",
                title="Дворник",
                company="ЖЭК",
                region="Город Москва",
                salary_min=None,
                salary_max=None,
                # «удаление» не должно считаться удалёнкой — именно на этом ловился старый фильтр.
                raw={"duty": "Удаление мусора и уборка территории"},
            ),
        ],
    )
    session.commit()


def test_summary_counts(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery())
    summary = build_summary(rows, len(rows))

    assert summary.shown == 3
    # Официальный признак — один, «упоминается в тексте» — тоже один: это разные вещи.
    assert summary.remote_official == 1
    assert summary.remote_mentioned == 1
    assert summary.with_salary == 2
    assert summary.salary_high == 1
    assert summary.max_salary == 200_000
    assert ("Центральный федеральный округ", 3) in summary.by_district


def test_remote_filter_ignores_deletion_word(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery(remote_only=True))
    assert sorted(row.vacancy.external_id for row in rows) == ["1", "2"]


def test_report_escapes_source_data(session) -> None:
    _fill(session)
    rows = search_vacancies(session, VacancyQuery())
    page = render_report_html(rows, query_spec=VacancyQuery(), db_url="sqlite://")

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_report_is_self_contained(session) -> None:
    """Никаких внешних скриптов и стилей: страница должна открываться без интернета."""
    _fill(session)
    rows = search_vacancies(session, VacancyQuery())
    page = render_report_html(rows, query_spec=VacancyQuery())

    assert 'src="http' not in page
    assert 'href="http' in page  # ссылки на сами вакансии, наоборот, обязаны быть
    assert 'id="f-search"' in page
    assert "показано 3 из 3" in page


def test_report_shows_filters_in_header(session) -> None:
    _fill(session)
    query = VacancyQuery(region="Москва", salary_min=100_000)
    rows = search_vacancies(session, query)
    page = render_report_html(rows, query_spec=query)

    assert "регион: «Москва»" in page
    assert "зарплата от 100 000" in page
    # В выпадающий список регионов попадают только регионы из выборки.
    assert '<option value="Город Москва">' in page
