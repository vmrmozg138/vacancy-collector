"""Источник: справочник субъектов РФ из Википедии.

Первый источник проекта, который работает не с API, а с обычным HTML. Задача —
собрать справочник: название субъекта, его тип (республика, край, область…),
федеральный округ, территория, население и административный центр.

Почему Википедия: это открытый источник, который явно разрешает чтение роботами
(robots.txt не запрещает статью), данные регулярно обновляются, и структура страниц
не меняется хаотично. Но главное — здесь видно настоящую работу с HTML: таблица
с объединёнными по смыслу строками-разделами, служебные вставки для сортировки,
сноски, встроенные стили и ссылки, которые ведут не туда, куда показывает текст.

Из чего собирается справочник:

* статья «Субъекты Российской Федерации» — таблица `standard sortable`: 12 колонок,
  где нужны 1 (название), 4 (территория), 5 (население), 6 (административный центр).
  Строки-разделители («Республики», «Края», «Области»…) задают тип субъекта
  для всех последующих строк, пока не встретится следующий раздел;
* статья «Федеральные округа Российской Федерации» — по разделу на округ,
  внутри каждого список субъектов. Из него строится соответствие
  «субъект → федеральный округ», которое и позволяет потом отвечать на вопрос
  «сколько вакансий в каждом округе».

Два документа объединяются в одну «страницу» источника: справочник имеет смысл
только целиком, потому что округ для субъекта известен лишь из второго документа.

Сопоставление выполняется по нормализованному названию (`core.normalize`),
а не по точному совпадению: названия в источниках отличаются порядком слов,
тире и служебными словами. Часть субъектов может остаться без округа —
это нормально, и в отчёте видно, сколько именно.
"""

from __future__ import annotations

import copy
import re
import time
import urllib.parse
from typing import Any, Iterator

from lxml import html

from collector.core.http import HttpClient
from collector.core.normalize import normalize_name
from collector.core.robots import RobotsCache
from collector.sources.base import RegionItem, Source

SUBJECTS_URL = "https://ru.wikipedia.org/wiki/" + urllib.parse.quote("Субъекты Российской Федерации")
DISTRICTS_URL = "https://ru.wikipedia.org/wiki/" + urllib.parse.quote("Федеральные округа Российской Федерации")

#: Индексы нужных колонок в таблице субъектов (проверены по фактической вёрстке).
COL_NAME, COL_TERRITORY, COL_POPULATION, COL_CENTER = 1, 4, 5, 6

_HEADING_TAGS = {"h1", "h2", "h3", "h4"}
_REMOVE_TAGS = {"style", "script"}

#: Ссылки, которые не являются названием субъекта: иконки, файлы и общие пояснения.
_NOT_A_SUBJECT_PREFIXES = ("Флаг", "Герб", "Знамя", "Файл:", "Flag", "Coat")
_GENERIC_LINK_TITLES = frozenset(
    {"Город федерального значения", "Республика в составе Российской Федерации"}
)


def subject_link_title(element: Any) -> str | None:
    """Название субъекта из ссылки внутри ячейки или пункта списка.

    Брать видимый текст нельзя: у городов федерального значения он склеивается из двух ссылок —
    «Город федерального значения» и «Москва». Нужна предметная ссылка, поэтому отбрасываем
    иконки и общие пояснения, а из оставшихся берём последнюю: она и есть субъект.

    Ссылки внутри сносок (`sup`) тоже пропускаем — это ссылки на источники, а не на субъект.
    """
    links = [link for link in element.xpath(".//a[@title]") if not link.xpath("ancestor::sup")]
    titles = [
        title
        for title in (link.get("title") for link in links)
        if title and not title.startswith(_NOT_A_SUBJECT_PREFIXES)
    ]
    specific = [title for title in titles if title not in _GENERIC_LINK_TITLES]
    if specific:
        return specific[-1]
    return titles[-1] if titles else None


def drop_element(node: Any) -> None:
    """Удалить элемент, сохранив текст, который идёт после него.

    Ловушка lxml: у элемента есть не только содержимое, но и «хвост» — текст между ним
    и следующим элементом. При простом `parent.remove(node)` хвост исчезает вместе с ним,
    а в нём может лежать значимая часть данных: в вики-ячейке это, например, значение
    населения, стоящее сразу после сноски `<sup>[6]</sup>`.
    """
    parent = node.getparent()
    if parent is None:
        return
    tail = node.tail
    if tail:
        previous = node.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(node)


def clean_text(element: Any) -> str:
    """Текст элемента без служебных вставок.

    Википедия прячет в ячейки то, что не должен видеть читатель: ключи сортировки
    в скрытых блоках, встроенные стили, сноски-ссылки на источники. Всё это надо
    убрать, иначе в данные попадут куски CSS и номера сносок.
    """
    clone = copy.deepcopy(element)
    for tag in _REMOVE_TAGS:
        for node in clone.xpath(f".//{tag}"):
            drop_element(node)
    for node in clone.xpath(
        ".//*[contains(@style,'display:none') or contains(@style,'display: none')]"
        " | .//sup[contains(@class,'reference')] | .//sup[contains(@class,'noprint')]"
    ):
        drop_element(node)
    return " ".join(clone.text_content().split())


_NUMBER_RE = re.compile(r"\d[\d\s\u00a0]*(?:[.,]\d+)?")

#: Больше 32-битного целого в этих полях быть не может: такое значение означает,
#: что склеились несколько чисел, и записывать его в базу нельзя.
MAX_INT32 = 2**31 - 1


def to_int(text: str) -> int | None:
    """Вытащить первое число из текста вида «↘210 095» или «26 517 (де-юре)».

    Берём именно первое число, а не все цифры подряд. В ячейке их может быть несколько:
    служебный ключ сортировки, основное значение, уточнения в скобках. Склейка всех цифр
    даёт бессмысленные величины — такая ошибка однажды уронила запись в PostgreSQL
    (`integer out of range`), поэтому заодно отбрасываем значения, не помещающиеся в целое.
    """
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if not digits:
        return None
    number = int(digits)
    return number if number <= MAX_INT32 else None


class WikipediaRegionsSource(Source):
    """Справочник субъектов РФ: тип, федеральный округ, территория, население, центр."""

    name = "wikipedia-regions"

    def __init__(self, http: HttpClient, robots: RobotsCache | None = None) -> None:
        super().__init__(http, robots)

    def iter_pages(self, pages: int) -> Iterator[dict[str, str]]:
        """Отдать один документ: две страницы Википедии, которые нужны вместе.

        `pages` здесь не используется: справочник собирается целиком за один проход,
        потому что разделять два документа бессмысленно — округ известен только из второго.
        """
        payload: dict[str, str] = {}
        for key, url in (("subjects", SUBJECTS_URL), ("districts", DISTRICTS_URL)):
            if not self.can_fetch(url):
                raise PermissionError(f"robots.txt запрещает загрузку {url}")
            payload[key] = self.http.get_text(url)
            self._respect_crawl_delay(url)
        yield payload

    def parse_page(self, payload: dict[str, str]) -> list[RegionItem]:
        """Разобрать оба документа в список субъектов с заполненным федеральным округом."""
        district_map = self.parse_districts(payload["districts"])
        return self.parse_subjects(payload["subjects"], district_map)

    def parse_districts(self, page: str) -> dict[str, str]:
        """Разобрать статью об округах: нормализованное название субъекта → округ."""
        tree = html.fromstring(page)
        mapping: dict[str, str] = {}

        # Заголовки ищем внутри тела статьи; если обёртки нет (например, в тестовом фрагменте),
        # берём все заголовки страницы.
        headings = tree.xpath("//div[contains(@class,'mw-parser-output')]//h3") or tree.xpath("//h3")

        for heading in headings:
            title = clean_text(heading)
            if not title:
                continue
            district = title if "округ" in title.lower() else f"{title} федеральный округ"

            node = self._heading_anchor(heading).getnext()
            while node is not None and not self._is_heading(node):
                lists = [node] if node.tag == "ul" else node.xpath("./ul")
                for unordered in lists:
                    for item in unordered.xpath("./li"):
                        visible = re.sub(r"\s*\([^)]*\)", "", clean_text(item)).strip()
                        title = subject_link_title(item) or visible
                        # Ключей два: по названию статьи (совпадает у обоих документов)
                        # и по видимому тексту (на случай, если статья названа иначе).
                        for key in {normalize_name(title), normalize_name(visible)}:
                            if key:
                                mapping[key] = district
                node = node.getnext()

        return mapping

    def parse_subjects(self, page: str, district_map: dict[str, str] | None = None) -> list[RegionItem]:
        """Разобрать таблицу субъектов. Строки-разделители задают тип субъекта."""
        district_map = district_map or {}
        tree = html.fromstring(page)
        tables = tree.xpath("//table[contains(@class,'standard') and contains(@class,'sortable')]")
        if not tables:
            return []

        items: list[RegionItem] = []
        subject_type: str | None = None

        for row in tables[0].xpath(".//tr"):
            cells = row.xpath("./td")  # строки заголовков используют th и в данные не попадают
            if len(cells) <= COL_CENTER:
                continue

            name = clean_text(cells[COL_NAME])
            if not name:
                continue

            # Разделитель раздела: название есть, все остальные ячейки пустые.
            if all(not clean_text(cell) for cell in cells[COL_NAME + 1 :]):
                subject_type = name
                continue

            links = cells[COL_NAME].xpath(".//a/@title")
            link_title = subject_link_title(cells[COL_NAME])
            district = next(
                (
                    district_map[key]
                    for key in (normalize_name(link_title), normalize_name(name))
                    if key in district_map
                ),
                None,
            )
            items.append(
                RegionItem(
                    source=self.name,
                    # Идентификатор — название статьи Википедии: оно стабильнее заголовка в таблице.
                    external_id=(link_title or name)[:200],
                    name=name,
                    subject_type=subject_type,
                    federal_district=district,
                    territory_km2=to_int(clean_text(cells[COL_TERRITORY])),
                    population=to_int(clean_text(cells[COL_POPULATION])),
                    admin_center=clean_text(cells[COL_CENTER]) or None,
                    raw={"url": SUBJECTS_URL, "link_title": link_title, "links": links[:3]},
                )
            )

        return items

    # --- вспомогательное ---

    def _respect_crawl_delay(self, url: str) -> None:
        """Соблюсти паузу, которую сайт просит в robots.txt (если просит больше нашей)."""
        if self.robots is None:
            return
        delay = self.robots.crawl_delay(url)
        if delay:
            time.sleep(delay)

    @staticmethod
    def _heading_anchor(heading: Any) -> Any:
        """Вернуть элемент, от которого идти по соседям: в новой вёрстке заголовок обёрнут в div."""
        parent = heading.getparent()
        if parent is not None and "mw-heading" in (parent.get("class") or ""):
            return parent
        return heading

    @staticmethod
    def _is_heading(node: Any) -> bool:
        if node.tag in _HEADING_TAGS:
            return True
        return any(child.tag in _HEADING_TAGS for child in node)
