"""Подключение к базе.

Один и тот же код работает с SQLite (локальный запуск) и с PostgreSQL (рабочий вариант):
отличается только строка подключения в настройках.

Здесь же решается проблема, которая рано или поздно возникает у любого сборщика:
схема базы со временем меняется. `create_all` умеет создавать только отсутствующие
таблицы и никогда не трогает существующие, поэтому после добавления поля в модель
старая база остаётся без него — и приложение падает на первом же запросе.
Полноценное решение — миграции (Alembic); облегчённое, но честное — при старте
добавлять недостающие колонки и индексы и писать об этом в лог.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateIndex

from collector.storage.models import Base

log = logging.getLogger("collector.db")


def create_db(db_url: str, echo: bool = False) -> tuple[Engine, sessionmaker[Session]]:
    """Создать движок и фабрику сессий, при необходимости — таблицы и недостающие колонки."""
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {}

    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if ":memory:" in db_url or db_url.rstrip("/") == "sqlite:":
            # База в памяти живёт, пока живо соединение: держим одно на всех,
            # иначе каждая сессия получала бы пустую базу. Так удобно в тестах.
            engine_kwargs["poolclass"] = StaticPool

    engine = create_engine(db_url, echo=echo, future=True, connect_args=connect_args, **engine_kwargs)
    Base.metadata.create_all(engine)
    ensure_schema(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def ensure_schema(engine: Engine) -> list[str]:
    """Довести существующие таблицы до текущей модели: добавить колонки и индексы.

    Возвращает список выполненных изменений — он попадает в лог, чтобы правка схемы
    не была незаметной. Колонки без значения по умолчанию и с запретом NULL не добавляются:
    такие изменения нельзя угадать, для них нужна настоящая миграция.
    """
    changes: list[str] = []
    inspector = inspect(engine)

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue

            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    raise RuntimeError(
                        f"{table.name}.{column.name}: обязательную колонку нельзя добавить автоматически — "
                        "нужна миграция"
                    )
                column_type = column.type.compile(dialect=engine.dialect)
                connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}"))
                changes.append(f"добавлена колонка {table.name}.{column.name}")

            for index in table.indexes:
                existing_indexes = {item["name"] for item in inspector.get_indexes(table.name)}
                if index.name in existing_indexes:
                    continue
                connection.execute(CreateIndex(index, if_not_exists=True))
                changes.append(f"создан индекс {index.name}")

    for change in changes:
        log.warning("схема обновлена: %s", change)
    return changes


def healthcheck(engine: Engine) -> str:
    """Быстрая проверка, что база доступна. Возвращает имя диалекта."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return engine.dialect.name
