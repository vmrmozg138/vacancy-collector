"""Проверка robots.txt перед запросом страницы.

Правило проекта: прежде чем тянуть HTML, смотрим, разрешает ли это robots.txt сайта.
В отличие от API, где правила заданы условиями использования, у сайтов это формальный
машиночитаемый запрет — его нарушение видно сразу и бьёт по репутации.

Особенности реализации:

* robots.txt запрашивается один раз на домен и кэшируется, а не перед каждой страницей;
* запрос идёт через тот же HTTP-слой, что и всё остальное, — с нашим User-Agent, паузами и повторами;
* если robots.txt недоступен (404, таймаут, ошибка сервера), считаем, что запрета нет,
  но обязательно пишем это в лог: «не смог проверить» и «проверил, разрешено» — разные вещи.
"""

from __future__ import annotations

import logging
import urllib.robotparser
from urllib.parse import urlparse

from collector.core.http import HttpClient


class RobotsCache:
    """Кэш разобранных robots.txt по домену."""

    def __init__(self, user_agent: str, http: HttpClient, logger: logging.Logger | None = None) -> None:
        self.user_agent = user_agent
        self.http = http
        self.log = logger or logging.getLogger("collector.robots")
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def can_fetch(self, url: str) -> bool:
        """Разрешён ли доступ к адресу для нашего User-Agent."""
        parser = self._parser_for(url)
        if parser is None:
            return True
        allowed = parser.can_fetch(self.user_agent, url)
        if not allowed:
            self.log.warning("robots.txt запрещает доступ: %s", url)
        return allowed

    def crawl_delay(self, url: str) -> float | None:
        """Пауза, которую сайт просит соблюдать (если указал)."""
        parser = self._parser_for(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None

    def _parser_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host in self._parsers:
            return self._parsers[host]

        robots_url = f"{host}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.parse(self.http.get_text(robots_url).splitlines())
            self._sort_rules(parser)
        except Exception as exc:  # нет файла или сайт недоступен — это не запрет
            self.log.warning("robots.txt не удалось прочитать (%s): %s", robots_url, exc)
            parser = None
        self._parsers[host] = parser
        return parser

    @staticmethod
    def _sort_rules(parser: urllib.robotparser.RobotFileParser) -> None:
        """Разложить правила так, чтобы работало правило самого точного совпадения.

        Стандартная библиотека возвращает **первое** подходящее правило в порядке файла:
        в комментарии к `Entry.allowance` обещано «longest path wins», но по факту это не так.
        На практике ломается на очень распространённой записи:

            User-agent: *
            Disallow: /
            Allow: /vacancies/

        где Python запретил бы вообще всё. Поэтому сортируем правила сами: сначала более
        длинные пути (они конкретнее), при равной длине — разрешающие раньше запрещающих.
        """
        entries = list(parser.entries)
        if parser.default_entry is not None:
            entries.append(parser.default_entry)
        for entry in entries:
            entry.rulelines.sort(key=lambda line: (-len(line.path), not line.allowance))
