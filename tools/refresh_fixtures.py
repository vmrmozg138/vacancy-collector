"""Обновление фикстур из живых источников.

Тесты не должны зависеть от сети, поэтому ответы источников сохраняются в `tests/fixtures`.
Этот скрипт заново скачивает страницы и перезаписывает фикстуры — запускать, когда
источник изменил вёрстку или нужно добавить новые данные:

    poetry run python -m tools.refresh_fixtures

Что сохраняется:

* `wikipedia_subjects.html` — таблица субъектов РФ. Из неё выбрасываются встроенные
  стили (иначе файл раздувается до сотен килобайт) и оставляются строка-заголовок,
  все строки-разделители и по три первых субъекта после каждого раздела — этого
  достаточно, чтобы проверить разбор всех типов субъектов;
* `wikipedia_districts.html` — разделы статьи о федеральных округах со списками субъектов.

Оба файла обрезаны до фрагментов. В начале каждого стоит комментарий с адресом источника
и датой выгрузки: по нему всегда видно, откуда данные и насколько они свежие.
"""

from __future__ import annotations

import copy
import datetime
import pathlib
import sys

from lxml import html

from collector.config import load_settings
from collector.core.http import HttpClient
from collector.sources.wikipedia_regions import DISTRICTS_URL, SUBJECTS_URL, clean_text, drop_element

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
ROWS_PER_SECTION = 3


def _strip_heavy(node) -> None:
    """Убрать всё, что не нужно для проверки разбора: стили, скрипты, картинки и их атрибуты.

    В реальной странице каждая ячейка с флагом и гербом тянет за собой адреса картинок
    в нескольких размерах — в фикстуре от этого остаётся только объём.
    """
    for tag in ("style", "script", "img", "sup"):
        for child in node.xpath(f".//{tag}"):
            drop_element(child)

    keep = {"class", "colspan", "rowspan", "title", "href", "scope"}
    for element in node.iter():
        for attribute in list(element.attrib):
            if attribute not in keep:
                del element.attrib[attribute]


def build_subjects_fragment(page: str) -> str:
    """Собрать урезанную копию таблицы субъектов."""
    tree = html.fromstring(page)
    tables = tree.xpath("//table[contains(@class,'standard') and contains(@class,'sortable')]")
    if not tables:
        raise SystemExit("в статье не найдена таблица субъектов — вёрстка изменилась")

    source_table = tables[0]
    new_table = html.Element("table")
    for attribute, value in source_table.attrib.items():
        new_table.set(attribute, value)

    kept_rows: list = []
    taken_in_section = 0
    for row in source_table.xpath(".//tr"):
        cells = row.xpath("./td")
        # Разделитель раздела: название есть, начиная с колонки территории всё пусто.
        is_section = len(cells) > 2 and all(not clean_text(cell) for cell in cells[2:])
        if not cells or is_section:
            kept_rows.append(copy.deepcopy(row))
            taken_in_section = 0
            continue
        if taken_in_section < ROWS_PER_SECTION:
            kept_rows.append(copy.deepcopy(row))
            taken_in_section += 1

    for row in kept_rows:
        new_table.append(row)
    _strip_heavy(new_table)
    return html.tostring(new_table, encoding="unicode")


def build_districts_fragment(page: str) -> str:
    """Собрать фрагмент статьи об округах: заголовки округов и списки субъектов."""
    tree = html.fromstring(page)
    container = html.Element("div")

    # Заголовки округов — это h3; в новой вёрстке Википедии они обёрнуты в div.mw-heading,
    # поэтому идём по h3 на любой глубине и берём соседей до следующего заголовка.
    for heading in tree.xpath("//h3"):
        title = clean_text(heading)
        if not title:
            continue
        container.append(copy.deepcopy(heading.getparent() if "mw-heading" in (heading.getparent().get("class") or "") else heading))

        node = _wrapper(heading).getnext()
        while node is not None and node.tag not in ("h2", "h3") and not _has_heading(node):
            container.append(copy.deepcopy(node))
            node = node.getnext()

    _strip_heavy(container)
    return html.tostring(container, encoding="unicode")


def _wrapper(heading):
    parent = heading.getparent()
    if parent is not None and "mw-heading" in (parent.get("class") or ""):
        return parent
    return heading


def _has_heading(node) -> bool:
    return node.tag in ("h2", "h3") or any(child.tag in ("h2", "h3") for child in node)


def write_fixture(name: str, url: str, fragment: str) -> None:
    stamp = datetime.date.today().isoformat()
    document = (
        f"<!-- Источник: {url}\n"
        f"     Выгружено: {stamp} скриптом tools/refresh_fixtures.py.\n"
        f"     Фрагмент страницы: обрезан для тестов, структура сохранена. -->\n"
        f"{fragment}\n"
    )
    path = FIXTURES / name
    path.write_text(document, encoding="utf-8")
    print(f"{name}: {len(document) // 1024} КБ")


def main(argv: list[str]) -> int:
    settings = load_settings()
    http = HttpClient(
        user_agent=settings.user_agent,
        timeout=settings.timeout,
        delay=settings.delay,
        max_retries=settings.max_retries,
    )
    FIXTURES.mkdir(parents=True, exist_ok=True)

    write_fixture("wikipedia_subjects.html", SUBJECTS_URL, build_subjects_fragment(http.get_text(SUBJECTS_URL)))
    write_fixture("wikipedia_districts.html", DISTRICTS_URL, build_districts_fragment(http.get_text(DISTRICTS_URL)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
