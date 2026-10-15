"""Тесты приведения схемы базы к текущей модели.

Ситуация из жизни: таблица уже создана, в модель добавили колонку — `create_all`
существующую таблицу не меняет, и приложение падает на запросе к новой колонке.
Проверяем, что `ensure_schema` доводит такую базу до актуального вида.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from collector.storage.db import create_db, ensure_schema


def _old_database_without_region_key(engine) -> None:
    """Сделать вид, что база создана до появления колонки region_key."""
    with engine.begin() as connection:
        # Индекс держит колонку, поэтому сначала снимаем его.
        connection.execute(text("DROP INDEX IF EXISTS ix_vacancy_region_key"))
        connection.execute(text("ALTER TABLE vacancies DROP COLUMN region_key"))


def test_missing_column_and_index_are_restored(session) -> None:
    engine = session.get_bind()
    _old_database_without_region_key(engine)

    assert "region_key" not in {column["name"] for column in inspect(engine).get_columns("vacancies")}

    changes = ensure_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("vacancies")}
    indexes = {index["name"] for index in inspect(engine).get_indexes("vacancies")}
    assert "region_key" in columns
    assert "ix_vacancy_region_key" in indexes
    assert any("region_key" in change for change in changes)


def test_second_run_changes_nothing(session) -> None:
    engine = session.get_bind()

    assert ensure_schema(engine) == []
    assert ensure_schema(engine) == []


def test_indexes_are_created_in_existing_database() -> None:
    engine, _ = create_db("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX IF EXISTS ix_vacancy_region_key"))

    changes = ensure_schema(engine)

    indexes = {index["name"] for index in inspect(engine).get_indexes("vacancies")}
    assert "ix_vacancy_region_key" in indexes
    assert any("ix_vacancy_region_key" in change for change in changes)
