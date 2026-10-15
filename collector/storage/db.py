"""Подключение к базе.

Один и тот же код работает с SQLite (локальный запуск) и с PostgreSQL (рабочий вариант):
отличается только строка подключения в настройках.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from collector.storage.models import Base


def create_db(db_url: str, echo: bool = False) -> tuple[Engine, sessionmaker[Session]]:
    """Создать движок и фабрику сессий, при необходимости — таблицы."""
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
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def healthcheck(engine: Engine) -> str:
    """Быстрая проверка, что база доступна. Возвращает имя диалекта."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return engine.dialect.name
