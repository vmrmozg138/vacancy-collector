"""Тесты сбора за период делением окна.

Источник-заглушка отдаёт заранее известное число вакансий на каждую минуту периода,
поэтому видно, как алгоритм делит окна и что он не теряет и не дублирует записи.
Сети тесты не требуют.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from collector.core.http import HttpClient
from collector.core.sweep import estimate_period, sweep_period
from collector.sources.base import VacancyItem
from collector.sources.trudvsem_api import TrudvsemSource
from collector.storage.repository import count_vacancies

START = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
LOGGER = logging.getLogger("test.sweep")


class _FakeWindowSource(TrudvsemSource):
    """Источник-заглушка: `per_minute` вакансий на каждую минуту периода.

    Отдаёт не больше `limit` записей за раз — как настоящий источник, у которого
    пагинация сломана и видно только первые записи окна.
    """

    name = "fake-window"

    def __init__(self, per_minute: int = 1, limit: int = 2) -> None:
        super().__init__(HttpClient("test-agent", delay=0), limit=limit)
        self.per_minute = per_minute
        self.calls: list[tuple[datetime, datetime]] = []
        self.fail_windows: set[str] = set()

    def fetch_window(self, modified_from=None, modified_to=None, limit=None):  # type: ignore[override]
        self.calls.append((modified_from, modified_to))
        key = f"{modified_from.isoformat()}|{modified_to.isoformat()}"
        if key in self.fail_windows:
            raise RuntimeError("сеть недоступна")

        minutes = int((modified_to - modified_from).total_seconds() // 60)
        total = max(0, minutes) * self.per_minute
        size = min(total, limit or self.limit)
        vacancies = [
            {
                "vacancy": {
                    "id": f"{modified_from.isoformat()}#{index}",
                    "job-name": f"Вакансия {index}",
                    "region": {"name": "Город Москва"},
                }
            }
            for index in range(size)
        ]
        return {"meta": {"total": total}, "results": {"vacancies": vacancies}}


def _session_factory_for(session):
    """Фабрика сессий, отдающая ту же сессию, что и тест (см. test_trudvsem.py)."""
    from contextlib import contextmanager

    @contextmanager
    def factory():
        yield session

    return session.get_bind(), factory


def test_window_is_split_until_it_fits(session) -> None:
    """Окно в 4 минуты с 4 записями при лимите 2 делится на два окна по 2 записи."""
    source = _FakeWindowSource(per_minute=1, limit=2)
    _, session_factory = _session_factory_for(session)

    result = sweep_period(
        source, session_factory, START, START + timedelta(minutes=4), LOGGER, min_window=timedelta(minutes=1)
    )

    assert result.stats.items_parsed == 4, "должны быть собраны все 4 вакансии"
    assert result.windows_done == 2, "два листовых окна по 2 записи"
    assert result.truncated_windows == 0
    assert result.stopped_early is False
    assert result.stats.status == "ok"
    assert count_vacancies(session) == 4
    assert result.coverage == 100.0


def test_no_split_when_window_already_fits(session) -> None:
    """Если записей в окне меньше лимита, делить нечего: один запрос — все записи."""
    source = _FakeWindowSource(per_minute=1, limit=10)
    _, session_factory = _session_factory_for(session)

    result = sweep_period(
        source, session_factory, START, START + timedelta(minutes=3), LOGGER, min_window=timedelta(minutes=1)
    )

    assert result.windows_done == 1
    assert result.stats.items_parsed == 3
    assert len(source.calls) == 2, "один запрос на оценку периода и один на сбор"


def test_truncated_when_minimum_window_still_too_wide(session) -> None:
    """На минимальном окне записей больше лимита — берём что дают и говорим об этом честно."""
    source = _FakeWindowSource(per_minute=4, limit=2)
    _, session_factory = _session_factory_for(session)

    result = sweep_period(
        source, session_factory, START, START + timedelta(minutes=2), LOGGER, min_window=timedelta(minutes=1)
    )

    # Две минуты по 4 записи при лимите 2: каждое окно-минута теряет половину записей.
    assert result.truncated_windows == 2
    assert result.stats.items_parsed == 4, "источник отдал по две записи из четырёх в каждом окне"
    assert result.stats.status == "partial"
    assert len(result.problem_windows) == 2, "каждое урезанное окно должно быть перечислено"


def test_request_budget_stops_sweep(session) -> None:
    """Бюджет запросов останавливает сбор: часть данных остаётся на следующий прогон."""
    source = _FakeWindowSource(per_minute=1, limit=2)
    _, session_factory = _session_factory_for(session)

    result = sweep_period(
        source,
        session_factory,
        START,
        START + timedelta(minutes=8),
        LOGGER,
        min_window=timedelta(minutes=1),
        max_requests=1,
    )

    assert result.stopped_early is True
    assert result.stats.status == "partial"
    assert result.windows_done < 4, "сбор не должен успеть обойти все окна"


def test_failed_window_does_not_break_sweep(session) -> None:
    """Сбой одного окна не роняет прогон: остальные окна записываются, ошибка видна в статистике."""
    source = _FakeWindowSource(per_minute=1, limit=2)
    # Правая половина периода (вторые две минуты) отвечает ошибкой.
    right_start = START + timedelta(minutes=2)
    right_end = START + timedelta(minutes=4)
    source.fail_windows.add(f"{right_start.isoformat()}|{right_end.isoformat()}")
    _, session_factory = _session_factory_for(session)

    result = sweep_period(
        source, session_factory, START, START + timedelta(minutes=4), LOGGER, min_window=timedelta(minutes=1)
    )

    assert result.stats.errors == 1
    assert result.stats.status == "partial"
    assert result.problem_windows
    assert count_vacancies(session) == 2, "уцелевшее окно записано"


def test_estimate_period_uses_source_total(session) -> None:
    source = _FakeWindowSource(per_minute=3, limit=2)
    assert estimate_period(source, START, START + timedelta(minutes=5)) == 15


def test_sweep_rejects_backwards_period(session) -> None:
    source = _FakeWindowSource()
    _, session_factory = _session_factory_for(session)
    try:
        sweep_period(source, session_factory, START, START - timedelta(minutes=1), LOGGER)
    except ValueError as exc:
        assert "позже" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("ожидалась ошибка о неверном периоде")


def test_parse_page_works_with_window_payload() -> None:
    """Разбор ответа одинаков для страницы и для окна: структура у источника одна."""
    source = TrudvsemSource(HttpClient("test-agent", delay=0), limit=5)
    payload = {
        "meta": {"total": 1},
        "results": {
            "vacancies": [
                {
                    "vacancy": {
                        "id": "abc",
                        "job-name": "Python-разработчик",
                        "region": {"name": "Город Москва"},
                        "salary_min": 150000,
                    }
                }
            ]
        },
    }
    items = source.parse_page(payload)
    assert isinstance(items[0], VacancyItem)
    assert items[0].external_id == "abc"
    assert source.window_total(payload) == 1
