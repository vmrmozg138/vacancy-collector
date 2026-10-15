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
from pathlib import Path

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
    VacancyQuery,
    count_regions,
    count_vacancies,
    region_match_stats,
    save_raw_document,
    search_vacancies,
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

    render = subparsers.add_parser("render", help="нужен ли браузер, чтобы получить данные со страницы")
    render.add_argument("--url", required=True, help="адрес страницы")
    render.add_argument("--xpath", required=True, help="что искать на странице, например //div[@class='quote']")
    render.add_argument("--wait-for", help="селектор, которого дождаться после загрузки")

    export = subparsers.add_parser("export", help="выгрузить отобранные вакансии в CSV")
    export.add_argument("--out", default="exports/vacancies.csv", help="куда записать файл")
    export.add_argument("--text", help="подстрока в названии, компании или регионе")
    export.add_argument("--region", help="регион (подстрока или точное название)")
    export.add_argument("--district", help="федеральный округ, например «Сибирский»")
    export.add_argument("--salary-min", type=int, help="зарплата не ниже указанной (по верхней границе вакансии)")
    export.add_argument("--remote", action="store_true", help="только вакансии с упоминанием удалёнки в описании")
    export.add_argument("--limit", type=int, default=200, help="сколько строк выгрузить")
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


def cmd_render(args: argparse.Namespace, http: HttpClient, session_factory, logger: logging.Logger) -> int:
    """Показать, нужен ли браузер: сравнить страницу до и после JavaScript и сохранить HTML."""
    from collector.core.browser import BrowserNotAvailable, diagnose, render_html

    renderer = lambda url: render_html(url, wait_for=args.wait_for)  # noqa: E731
    try:
        result = diagnose(args.url, args.xpath, http, renderer=renderer)
    except BrowserNotAvailable as exc:
        logger.error("браузер недоступен: %s", exc)
        return 1

    logger.info("без браузера (HTML от сервера): найдено %s элементов", result.static_count)
    logger.info("с браузером (chromium): найдено %s элементов", result.rendered_count)
    logger.info("вывод: %s", result.verdict)

    if result.rendered_count:
        page = renderer(args.url)
        with session_factory() as session:
            document = save_raw_document(
                session,
                source="browser-diagnostic",
                url=args.url,
                payload=page,
                note=f"xpath={args.xpath}; найдено {result.rendered_count}",
            )
            session.commit()
            logger.info("HTML после рендеринга сохранён в raw_documents (%s КБ)", document.size_bytes // 1024)
    return 0


def cmd_export(args: argparse.Namespace, session_factory, logger: logging.Logger) -> int:
    """Выгрузить вакансии по условиям в CSV-файл."""
    from collector.core.export import write_vacancies_csv

    query_spec = VacancyQuery(
        text=args.text,
        region=args.region,
        district=args.district,
        salary_min=args.salary_min,
        remote_only=args.remote,
        limit=args.limit,
    )
    with session_factory() as session:
        rows = search_vacancies(session, query_spec)

    if not rows:
        logger.info("по этим условиям ничего не найдено — проверьте фильтры или соберите больше данных")
        return 0

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: Excel иначе не понимает кириллицу в CSV.
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        written = write_vacancies_csv(rows, stream)

    logger.info("выгружено вакансий: %s", written)
    logger.info("файл: %s", destination.resolve())
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
    if args.command == "export":
        return cmd_export(args, session_factory, logger)

    http = HttpClient(
        user_agent=settings.user_agent,
        timeout=settings.timeout,
        delay=settings.delay,
        max_retries=settings.max_retries,
        logger=logger,
    )

    if args.command == "render":
        return cmd_render(args, http, session_factory, logger)

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
