"""Тесты диагностики «нужен ли браузер».

Настоящий браузер здесь не запускается: рендеринг подменяется функцией, которая отдаёт
заранее подготовленный HTML. Проверяем саму логику сравнения и сохранение результата —
то, что можно проверить быстро и без установленного Chromium.

Отдельный тест на живой браузер помечен как интеграционный и по умолчанию пропускается:
он нужен, когда проверяют окружение, а не код.
"""

from __future__ import annotations

import pytest

from collector.core.browser import RenderDiagnosis, count_matches, diagnose, render_html
from collector.storage.repository import save_raw_document

STATIC_PAGE = """
<html><body>
  <div class="quote">Цитата первая</div>
  <div class="quote">Цитата вторая</div>
</body></html>
"""

RENDERED_PAGE = """
<html><body>
  <div class="quote">Цитата первая</div>
  <div class="quote">Цитата вторая</div>
  <div class="quote">Цитата, добавленная JavaScript</div>
</body></html>
"""

JS_ONLY_PAGE = """
<html><body>
  <div id="app"></div>
  <script>document.getElementById('app').innerHTML = '<div class="quote">Из JavaScript</div>';</script>
</body></html>
"""


class _FakeHttp:
    def __init__(self, page: str) -> None:
        self.page = page

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self.page


def test_count_matches_counts_elements() -> None:
    assert count_matches(STATIC_PAGE, "//div[@class='quote']") == 2
    assert count_matches(STATIC_PAGE, "//div[@class='nope']") == 0


def test_page_without_javascript_needs_no_browser() -> None:
    """Если элементы есть в HTML, браузер не нужен — это и есть главный вывод."""
    diagnosis = diagnose(
        "https://example.org/static",
        "//div[@class='quote']",
        _FakeHttp(STATIC_PAGE),
        renderer=lambda url: STATIC_PAGE,
    )

    assert diagnosis.static_count == 2
    assert diagnosis.rendered_count == 2
    assert diagnosis.needs_browser is False
    assert "браузер не нужен" in diagnosis.verdict


def test_javascript_only_page_requires_browser() -> None:
    """Классический случай: до рендеринга пусто, после — данные появились."""
    diagnosis = diagnose(
        "https://example.org/js",
        "//div[@class='quote']",
        _FakeHttp(JS_ONLY_PAGE),
        renderer=lambda url: RENDERED_PAGE,
    )

    assert diagnosis.static_count == 0
    assert diagnosis.rendered_count == 3
    assert diagnosis.needs_browser is True
    assert "JavaScript" in diagnosis.verdict


def test_verdict_when_nothing_found() -> None:
    diagnosis = RenderDiagnosis(url="https://example.org", xpath="//div", static_count=0, rendered_count=0)
    assert diagnosis.needs_browser is False
    assert "проверьте XPath" in diagnosis.verdict


def test_rendered_page_is_saved_once_per_url(session) -> None:
    """Один адрес — один документ: повторное сохранение обновляет запись, а не создаёт копию."""
    from sqlalchemy import func, select

    from collector.storage.models import RawDocument

    save_raw_document(session, source="browser-diagnostic", url="https://example.org/js", payload=RENDERED_PAGE)
    session.commit()
    save_raw_document(session, source="browser-diagnostic", url="https://example.org/js", payload=RENDERED_PAGE + "<!-- изменилось -->")
    session.commit()

    total = session.scalar(select(func.count(RawDocument.id)))
    assert total == 1
    stored = session.scalar(select(RawDocument))
    assert stored is not None and stored.payload.endswith("<!-- изменилось -->")


@pytest.mark.integration
def test_real_browser_renders_page() -> None:
    """Интеграционная проверка: запускаем настоящий браузер. По умолчанию пропускается."""
    pytest.importorskip("playwright")
    try:
        page = render_html("https://quotes.toscrape.com/js/", timeout_ms=60_000)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"браузер недоступен в этом окружении: {exc}")

    assert count_matches(page, "//div[@class='quote']") > 0
