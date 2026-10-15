"""CLI сборщика.

Примеры:
    python -m collector.cli run --source trudvsem --pages 3 --limit 50
    python -m collector.cli run --source trudvsem --pages 2 --text python
    python -m collector.cli stats
    python -m collector.cli sources
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from collector.config import load_settings
from collector.core.http import HttpClient
from collector.core.logging_setup import setup_logging
from collector.core.pipeline import run_source
from collector.sources.base import VacancySource
from collector.sources.trudvsem_api import TrudvsemSource
from collector.storage.db import create_db, healthcheck
from collector.storage.models import RunLog
from collector.storage.repository import count_vacancies, top_regions

AVAILABLE_SOURCES = {"trudvsem": "открытые данные «Работа России» (API, JSON)"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collector", description="Сбор данных из открытых источников")
    parser.add_argument("--db", help="строка подключения к базе (по умолчанию из .env)")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="запустить сбор")
    run.add_argument("--source", default="trudvsem", choices=sorted(AVAILABLE_SOURCES))
    run.add_argument("--pages", type=int, default=1, help="сколько страниц забрать")
    run.add_argument("--limit", type=int, default=50, help="размер страницы (1..100)")
    run.add_argument("--text", help="поисковый запрос на стороне источника")
    run.add_argument("--region", help="фильтр по названию региона (уже на нашей стороне)")

    subparsers.add_parser("stats", help="что уже собрано")
    subparsers.add_parser("sources", help="список доступных источников")
    return parser


def make_source(name: str, http: HttpClient, args: argparse.Namespace) -> VacancySource:
    if name == "trudvsem":
        return TrudvsemSource(http, limit=args.limit, text=args.text)
    raise SystemExit(f"неизвестный источник: {name}")


def cmd_stats(session_factory, logger: logging.Logger) -> int:
    with session_factory() as session:
        total = count_vacancies(session)
        logger.info("вакансий в базе: %s", total)
        for region, count in top_regions(session):
            logger.info("  %-40s %s", region, count)
        last_runs = session.scalars(select(RunLog).order_by(RunLog.id.desc()).limit(5)).all()
        for run in last_runs:
            logger.info("запуск: %s", run.as_dict())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    logger = setup_logging(settings.log_file, logging.DEBUG if args.verbose else logging.INFO)

    if args.command == "sources":
        for name, description in AVAILABLE_SOURCES.items():
            print(f"{name:10} {description}")
        return 0

    db_url = args.db or settings.db_url
    engine, session_factory = create_db(db_url)
    logger.info("база: %s (диалект: %s)", db_url, healthcheck(engine))

    if args.command == "stats":
        return cmd_stats(session_factory, logger)

    http = HttpClient(
        user_agent=settings.user_agent,
        timeout=settings.timeout,
        delay=settings.delay,
        max_retries=settings.max_retries,
        logger=logger,
    )
    source = make_source(args.source, http, args)
    stats = run_source(source, session_factory, args.pages, logger, region_filter=args.region)
    with session_factory() as session:
        logger.info("в базе теперь %s вакансий", count_vacancies(session))
    return 0 if stats.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
