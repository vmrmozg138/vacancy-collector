"""Тесты проверки robots.txt.

Проверяем три сценария, которые важны на практике: запрет соблюдается, отсутствие файла
не блокирует работу, а результат запрашивается один раз на домен, а не на каждую страницу.
"""

from __future__ import annotations

import logging

from collector.core.robots import RobotsCache


class _FakeHttp:
    """Подменяет HTTP-слой: отдаёт заранее заданные robots.txt и считает обращения."""

    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get_text(self, url: str, params: dict | None = None) -> str:
        self.calls.append(url)
        if url not in self.responses:
            raise RuntimeError(f"нет ответа для {url}")
        return self.responses[url]


ROBOTS = """
User-agent: *
Disallow: /private/
Allow: /private/open/

User-agent: CollectorBot
Crawl-delay: 2
Disallow: /no-bots/
"""


def test_wildcard_group_applies_to_other_agents() -> None:
    http = _FakeHttp({"https://example.org/robots.txt": ROBOTS})
    cache = RobotsCache("OtherBot/1.0", http, logging.getLogger("test"))

    assert cache.can_fetch("https://example.org/vacancies") is True
    assert cache.can_fetch("https://example.org/private/secret") is False
    # Правило из чужой группы на нас не действует.
    assert cache.can_fetch("https://example.org/no-bots/page") is True


DISALLOW_ALL_WITH_EXCEPTION = """
User-agent: *
Disallow: /
Allow: /vacancies/
"""


def test_longest_match_wins_over_file_order() -> None:
    """Стандартная библиотека берёт первое правило из файла; мы сортируем по длине пути.

    Без этой правки запись «Disallow: / + Allow: /vacancies/» запрещала бы вообще всё.
    """
    http = _FakeHttp({"https://example.org/robots.txt": DISALLOW_ALL_WITH_EXCEPTION})
    cache = RobotsCache("CollectorBot/1.0", http, logging.getLogger("test"))

    assert cache.can_fetch("https://example.org/vacancies/list") is True
    assert cache.can_fetch("https://example.org/other") is False


def test_specific_group_overrides_wildcard() -> None:
    """Если для нашего агента есть своя группа, правила группы «*» к ней не применяются."""
    http = _FakeHttp({"https://example.org/robots.txt": ROBOTS})
    cache = RobotsCache("CollectorBot/1.0", http, logging.getLogger("test"))

    assert cache.can_fetch("https://example.org/no-bots/page") is False
    assert cache.can_fetch("https://example.org/private/secret") is True
    assert cache.can_fetch("https://example.org/vacancies") is True


def test_crawl_delay_is_reported() -> None:
    http = _FakeHttp({"https://example.org/robots.txt": ROBOTS})
    cache = RobotsCache("CollectorBot/1.0", http, logging.getLogger("test"))

    assert cache.crawl_delay("https://example.org/anything") == 2.0


def test_robots_is_fetched_once_per_host() -> None:
    http = _FakeHttp({"https://example.org/robots.txt": ROBOTS})
    cache = RobotsCache("CollectorBot/1.0", http, logging.getLogger("test"))

    for path in ("/a", "/b", "/c"):
        cache.can_fetch(f"https://example.org{path}")

    assert http.calls == ["https://example.org/robots.txt"]


def test_missing_robots_means_allowed_but_logged() -> None:
    http = _FakeHttp({})  # файла нет
    cache = RobotsCache("CollectorBot/1.0", http, logging.getLogger("test"))

    assert cache.can_fetch("https://no-robots.example.org/page") is True
    assert cache.crawl_delay("https://no-robots.example.org/page") is None


def test_source_without_robots_allows_everything() -> None:
    from collector.sources.base import VacancyItem, Source

    class _Source(Source):
        name = "stub"

        def iter_pages(self, pages: int):
            yield from []

        def parse_page(self, payload):
            return []

    source = _Source(http=None)  # type: ignore[arg-type]
    assert source.can_fetch("https://example.org/whatever") is True
    assert VacancyItem(source="stub", external_id="1", title="Тест").title == "Тест"
