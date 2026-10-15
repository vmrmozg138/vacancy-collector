"""CLI сборщика.

Примеры:
    python -m collector.cli run --source trudvsem --pages 3 --limit 50
    python -m collector.cli run --source trudvsem --pages 2 --text python
    python -m collector.cli run --source wikipedia-regions      # справочник регионов
    python -m collector.cli stats
    python -m collector.cli districts                            # вакансии по федеральным округам
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
from collector.core.pipeline import Sink, run_source
from collector.core.robots import RobotsCache
from collector.sources.base import Source
from collector.sources.trudvsem_api import TrudvsemSource
from collector.sources.wikipedia_regions import WikipediaRegionsSource
from collector.storage.db import create_db, healthcheck
from collector.storage.models import RunLog
from collector.storage.repository import (
    UNMATCHED,
    count_regions,
    count_vacancies,
    region_match_stats,
    top_regions,
    upsert_regions,
    upsert_vacancies,
    vacancies_by_district,
)

AVAILABLE_SOURCES = {
    "trudvsem": "открытые данные «Работа России» (API, JSON) — вакансии",
    "wikipedia-regions": "справочник субъектов РФ из Википедии (HTML) — регионы, округа, население",
}

#: Куда пишет каждый источник: вакансии в одну таблицу, справочник — в другую.
SINKS: dict[str, Sink] = {
    "trudvsem": upsert_vacancies,
    "wikipedia-regions": upsert_regions,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collector", description="Сбор данных из открытых источников")
    parser.add_argument("--db", help="строка подключения к базе (по умолчанию из .env)")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="запустить сбор")
    run.add_argument("--source", default="trudvsem", choices=sorted(AVAILABLE_SOURCES))
    run.add_argument("--pages", type=int, default=1, help="сколько страниц забрать")
    run.add_argument("--limit", type=int, default=50, help="размер страницы источника (1..100)")
    run.add_argument("--text", help="поисковый запрос на стороне источника")
    run.add_argument("--region", help="фильтр по названию региона (уже на нашей стороне)")

    subparsers.add_parser("stats", help="что уже собрано")
    subparsers.add_parser("districts", help="вакансии в разрезе федеральных округов")
    subparsers.add_parser("sources", help="список доступных источников")
    return parser


def make_source(name: str, http: HttpClient, robots: RobotsCache, args: argparse.Namespace) -> Source:
    if name == "trudvsem":
        return TrudvsemSource(http, limit=args.limit, text=args.text)
    if name == "wikipedia-regions":
        return WikipediaRegionsSource(http, robots=robots)
    raise SystemExit(f"неизвестный источник: {name}")


def cmd_stats(session_factory, logger: logging.Logger) -> int:
    with session_factory() as session:
        total = count_vacancies(session)
        logger.info("вакансий в базе: %s", total)
        for region, count in top_regions(session):
            logger.info("  %-40s %s", region, count)

        regions = count_regions(session)
        if regions:
            matched, all_vacancies = region_match_stats(session)
            share = (matched / all_vacancies * 100) if all_vacancies else 0
            logger.info("регионов в справочнике: %s", regions)
            logger.info("сопоставлено со справочником: %s из %s (%.0f%%)", matched, all_vacancies, share)
        else:
            logger.info("справочник регионов пуст — загрузите его: run --source wikipedia-regions")

        last_runs = session.scalars(select(RunLog).order_by(RunLog.id.desc()).limit(5)).all()
        for run in last_runs:
            logger.info("запуск: %s", run.as_dict())
    return 0


def cmd_districts(session_factory, logger: logging.Logger) -> int:
    """Показать, ради чего собирался справочник: разрез вакансий по федеральным округам."""
    with session_factory() as session:
        total = count_vacancies(session)
        if not total:
            logger.info("в базе нет вакансий — сначала запустите сбор: run --source trudvsem")
            return 0
        if not count_regions(session):
            logger.info("справочник регионов пуст — запустите: run --source wikipedia-regions")
            return 1

        rows = vacancies_by_district(session)
        without_district = dict(rows).get(UNMATCHED, 0)
        matched, _ = region_match_stats(session)

        # Два разных показателя, и путать их нельзя: «нашёлся в справочнике» и «известен округ».
        # Регион может быть в справочнике, но отсутствовать в списках округов на странице-источнике.
        logger.info("регион найден в справочнике: %s из %s (%.0f%%)", matched, total, matched / total * 100)
        logger.info("из них с известным федеральным округом: %s", total - without_district)
        logger.info("вакансии по федеральным округам:")
        for district, count in rows:
            logger.info("  %-34s %s", district, count)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    logger = setup_logging(settings.log_file, logging.DEBUG if args.verbose else logging.INFO)

    if args.command == "sources":
        for name, description in AVAILABLE_SOURCES.items():
            print(f"{name:18} {description}")
        return 0

    db_url = args.db or settings.db_url
    engine, session_factory = create_db(db_url)
    logger.info("база: %s (диалект: %s)", db_url, healthcheck(engine))

    if args.command == "stats":
        return cmd_stats(session_factory, logger)
    if args.command == "districts":
        return cmd_districts(session_factory, logger)

    http = HttpClient(
        user_agent=settings.user_agent,
        timeout=settings.timeout,
        delay=settings.delay,
        max_retries=settings.max_retries,
        logger=logger,
    )
    robots = RobotsCache(settings.user_agent, http, logger)
    source = make_source(args.source, http, robots, args)
    stats = run_source(
        source,
        session_factory,
        args.pages,
        logger,
        region_filter=args.region,
        sink=SINKS[args.source],
    )
    with session_factory() as session:
        logger.info(
            "в базе теперь: вакансий %s, регионов %s",
            count_vacancies(session),
            count_regions(session),
        )
    return 0 if stats.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
