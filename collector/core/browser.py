"""Диагностика страниц: можно ли получить данные без браузера.

Браузер — самый дорогой способ сбора данных: он медленный, тяжёлый, требует скачивания
самого браузера и легко ломается при малейшем изменении страницы. Поэтому его применение
надо обосновывать фактом, а не привычкой. Этот модуль даёт такой факт: считает нужные
элементы в HTML, который отдал сервер, затем — в HTML после выполнения JavaScript,
и показывает разницу.

Типичный результат на реальных сайтах: данные уже в HTML, браузер не нужен (так оказалось
и с карточками вакансий на «Работе России»). Но на страницах, которые рисуются на клиенте,
без браузера не обойтись — и тогда это видно числом: 0 элементов до, 10 после.

Импорт Playwright стоит внутри функции: он нужен только этому шагу, а остальные команды
должны работать без установленного браузера.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lxml import html

from collector.config import load_settings
from collector.core.http import HttpClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_BROWSERS_DIR = PROJECT_ROOT / ".pw-browsers"


class BrowserNotAvailable(RuntimeError):
    """Браузер не установлен или не запускается."""


def count_matches(page: str, xpath: str) -> int:
    """Сколько элементов страницы подходят под XPath."""
    return len(html.fromstring(page).xpath(xpath))


def browsers_path() -> str:
    """Где искать браузеры: переменная окружения, затем папка проекта, затем путь Playwright."""
    configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH") or load_settings().playwright_browsers_path
    if configured:
        return configured
    if LOCAL_BROWSERS_DIR.exists():
        return str(LOCAL_BROWSERS_DIR)
    return ""


def render_html(url: str, wait_for: str | None = None, timeout_ms: int = 30_000) -> str:
    """Загрузить страницу в headless-браузере и вернуть HTML после выполнения JavaScript."""
    path = browsers_path()
    if path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise BrowserNotAvailable(
            "Playwright не установлен: poetry add playwright && playwright install chromium"
        ) from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale="ru-RU", user_agent=load_settings().user_agent)
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=timeout_ms)
                return page.content()
            finally:
                browser.close()
    except BrowserNotAvailable:
        raise
    except Exception as exc:  # noqa: BLE001 - сообщение важнее типа исключения
        raise BrowserNotAvailable(f"не удалось запустить браузер: {exc}") from exc


@dataclass(slots=True)
class RenderDiagnosis:
    """Результат сравнения: что видно в HTML и что появляется после JavaScript."""

    url: str
    xpath: str
    static_count: int
    rendered_count: int

    @property
    def needs_browser(self) -> bool:
        return self.static_count == 0 and self.rendered_count > 0

    @property
    def verdict(self) -> str:
        if self.needs_browser:
            return "данные рисует JavaScript — без браузера их не получить"
        if self.static_count > 0:
            return "данные уже в HTML — браузер не нужен, хватит requests"
        return "элементы не найдены ни до, ни после рендеринга — проверьте XPath"


def diagnose(
    url: str,
    xpath: str,
    http: HttpClient,
    renderer: Callable[[str], str] = render_html,
) -> RenderDiagnosis:
    """Сравнить страницу до и после выполнения JavaScript.

    `renderer` передаётся параметром, чтобы логику можно было проверить тестом
    без запуска настоящего браузера.
    """
    static_page = http.get_text(url)
    return RenderDiagnosis(
        url=url,
        xpath=xpath,
        static_count=count_matches(static_page, xpath),
        rendered_count=count_matches(renderer(url), xpath),
    )
