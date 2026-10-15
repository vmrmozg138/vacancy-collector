"""Общие фикстуры тестов.

Тесты не ходят в сеть: ответы источников сохраняются в tests/fixtures и разбираются
из файла. Иначе тесты падали бы при недоступности сайта и «зелёный прогон» ничего
не доказывал бы.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from collector.storage.db import create_db

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def trudvsem_payload() -> dict:
    """Сохранённый ответ API «Работа России» (5 вакансий)."""
    return json.loads((FIXTURES / "trudvsem_page.json").read_text(encoding="utf-8"))


@pytest.fixture()
def session() -> Iterator[Session]:
    """Сессия к базе в памяти — своя для каждого теста, без файлов на диске."""
    _, session_factory = create_db("sqlite://")
    with session_factory() as db_session:
        yield db_session
