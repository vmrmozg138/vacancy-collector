"""Выгрузка отобранных вакансий в CSV.

Отдельный модуль, потому что это представление данных, а не их хранение: в репозитории
лежат запросы, здесь — формат файла. Запись идёт в переданный поток, а не в путь:
так выгрузку можно проверить тестом, не создавая файлов на диске.
"""

from __future__ import annotations

import csv
from typing import IO, Sequence

from collector.storage.repository import VacancyRow

#: Порядок колонок в файле. Первые — то, по чему ищут глазами.
FIELDS = (
    "title",
    "company",
    "region",
    "federal_district",
    "salary_min",
    "salary_max",
    "currency",
    "published_at",
    "url",
    "source",
    "external_id",
)


def write_vacancies_csv(rows: Sequence[VacancyRow], stream: IO[str]) -> int:
    """Записать вакансии в CSV. Возвращает количество строк.

    Разделитель — точка с запятой: так файл открывается в русском Excel без танцев
    с настройками, потому что запятая в русской локали служит десятичным разделителем.
    """
    writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter=";", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        vacancy = row.vacancy
        writer.writerow(
            {
                "title": vacancy.title,
                "company": vacancy.company or "",
                "region": vacancy.region or "",
                "federal_district": row.federal_district or "",
                "salary_min": vacancy.salary_min if vacancy.salary_min is not None else "",
                "salary_max": vacancy.salary_max if vacancy.salary_max is not None else "",
                "currency": vacancy.currency or "",
                "published_at": vacancy.published_at.isoformat() if vacancy.published_at else "",
                "url": vacancy.url or "",
                "source": vacancy.source,
                "external_id": vacancy.external_id,
            }
        )
    return len(rows)
