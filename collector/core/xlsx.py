"""Выгрузка вакансий в Excel.

CSV хорош тем, что открывается чем угодно, но у него нет ни автофильтра, ни ширины колонок,
ни кликабельных ссылок, ни второго листа со сводкой. Для работы с вакансиями руками
(отметить отклики, отфильтровать по зарплате) удобнее один файл .xlsx.

Запись идёт в поток, а не в путь: так файл собирается в памяти, и выгрузку можно проверить
тестом, не создавая файлов на диске — ровно как у CSV-выгрузки.

Зависимость `openpyxl` подключена только ради этого модуля. Она импортируется внутри функции:
если пакет не установлен, остальные команды проекта работают как обычно, а выгрузка в Excel
сообщает, что именно нужно поставить.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import BinaryIO, Sequence

from collector.core.remote import MENTIONED, OFFICIAL, classify_remote
from collector.core.vacancy_view import salary_text, shorten, vacancy_details
from collector.storage.repository import VacancyQuery, VacancyRow

#: Колонки листа «Вакансии»: сначала то, по чему выбирают глазами, потом подробности.
COLUMNS = (
    ("Вакансия", 46),
    ("Компания", 30),
    ("Регион", 26),
    ("Федеральный округ", 28),
    ("Зарплата от", 12),
    ("Зарплата до", 12),
    ("Зарплата (как в вакансии)", 24),
    ("Удалённо", 22),
    ("Опыт, лет", 11),
    ("Занятость", 22),
    ("График", 20),
    ("Навыки", 40),
    ("Требования", 50),
    ("Опубликована", 14),
    ("Ссылка", 60),
)


def write_vacancies_xlsx(
    rows: Sequence[VacancyRow],
    stream: BinaryIO,
    *,
    query_spec: VacancyQuery | None = None,
    db_url: str = "",
    generated_at: datetime | None = None,
) -> int:
    """Записать вакансии и сводку в xlsx (в открытый на запись бинарный поток)."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "для выгрузки в Excel нужен пакет openpyxl: poetry add openpyxl"
        ) from exc

    generated_at = generated_at or datetime.now()
    query_spec = query_spec or VacancyQuery()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Вакансии"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4A7CF6")
    sheet.append([name for name, _ in COLUMNS])
    for index, (_, width) in enumerate(COLUMNS, start=1):
        cell = sheet.cell(row=1, column=index)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = width

    for row in rows:
        vacancy = row.vacancy
        details = vacancy_details(vacancy.raw)
        kind = classify_remote(vacancy.raw)
        sheet.append(
            [
                vacancy.title,
                vacancy.company or "",
                vacancy.region or "",
                row.federal_district or "",
                vacancy.salary_min,
                vacancy.salary_max,
                salary_text(vacancy.salary_min, vacancy.salary_max, vacancy.currency),
                {"official": "да (по данным работодателя)", "mentioned": "упоминается в тексте"}.get(kind or "", "нет"),
                details["experience"],
                details["employment"],
                details["schedule"],
                details["skills"],
                shorten(details["requirements"] or details["education"], 300),
                vacancy.published_at.isoformat() if vacancy.published_at else "",
                vacancy.url or "",
            ]
        )

    last_row = sheet.max_row
    if last_row > 1:
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{last_row}"
        # Даты и зарплаты — с форматами, чтобы Excel сортировал их как числа, а не как текст.
        for row_index in range(2, last_row + 1):
            for column in (5, 6):
                sheet.cell(row=row_index, column=column).number_format = "#,##0"
        for row_index in range(2, last_row + 1):
            url_cell = sheet.cell(row=row_index, column=len(COLUMNS))
            if url_cell.value:
                url_cell.hyperlink = url_cell.value
                url_cell.font = Font(color="1A4FD6", underline="single")
    sheet.freeze_panes = "A2"

    _write_summary_sheet(workbook, rows, query_spec=query_spec, db_url=db_url, generated_at=generated_at)

    workbook.save(stream)
    return len(rows)


def _write_summary_sheet(
    workbook,
    rows: Sequence[VacancyRow],
    *,
    query_spec: VacancyQuery,
    db_url: str,
    generated_at: datetime,
) -> None:
    """Второй лист: цифры и разрезы. Считается по той же выборке, что и первый лист."""
    from openpyxl.styles import Font

    sheet = workbook.create_sheet("Сводка")
    sheet.column_dimensions["A"].width = 46
    sheet.column_dimensions["B"].width = 16

    salaries = [row.vacancy.salary_max or row.vacancy.salary_min for row in rows]
    salaries = [value for value in salaries if value]
    official = sum(1 for row in rows if classify_remote(row.vacancy.raw) == OFFICIAL)
    mentioned = sum(1 for row in rows if classify_remote(row.vacancy.raw) == MENTIONED)

    title = Font(bold=True, size=13)
    sheet.append(["Вакансии: сводка"])
    sheet["A1"].font = title
    sheet.append([f"сформирован {generated_at:%d.%m.%Y %H:%M}"])
    sheet.append([f"источник: «Работа России» (открытое API)"])
    sheet.append([f"база: {db_url}"])
    sheet.append([])

    sheet.append(["Показатель", "Значение"])
    sheet["A6"].font = Font(bold=True)
    sheet["B6"].font = Font(bold=True)

    filters = [
        "текст: " + ", ".join(query_spec.texts) if query_spec.texts else "",
        f"регион: {query_spec.region}" if query_spec.region else "",
        f"округ: {query_spec.district}" if query_spec.district else "",
        f"зарплата от {query_spec.salary_min}" if query_spec.salary_min else "",
        "только удалённые" if query_spec.remote_only else "",
        "исключено: " + ", ".join(query_spec.exclude_text) if query_spec.exclude_text else "",
    ]
    sheet.append(["отбор", "; ".join(filter(None, filters)) or "без фильтров"])
    sheet.append(["вакансий в отчёте", len(rows)])
    sheet.append(["с указанной зарплатой", sum(1 for row in rows if row.vacancy.salary_min or row.vacancy.salary_max)])
    sheet.append(["зарплата от 150 000 ₽", sum(1 for value in salaries if value >= 150_000)])
    sheet.append(["удалённо (по данным работодателя)", official])
    sheet.append(["удалёнка упоминается в тексте", mentioned])
    sheet.append(["средняя зарплата", round(sum(salaries) / len(salaries)) if salaries else "—"])
    sheet.append(["максимальная зарплата", max(salaries) if salaries else "—"])
    sheet.append([])

    for header, counter in (
        ("Федеральные округа", Counter(row.federal_district or "не сопоставлено" for row in rows)),
        ("Регионы", Counter(row.vacancy.region or "—" for row in rows)),
        ("Компании", Counter(row.vacancy.company or "—" for row in rows)),
    ):
        sheet.append([header, "Вакансий"])
        sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)
        sheet.cell(row=sheet.max_row, column=2).font = Font(bold=True)
        for name, count in counter.most_common(15):
            sheet.append([name, count])
        sheet.append([])
