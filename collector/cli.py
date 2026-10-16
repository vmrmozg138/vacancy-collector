"""CLI сборщика.

Примеры:
    python -m collector.cli sweep --since 7d --text "python,аналитик данных"   # собрать за неделю
    python -m collector.cli sweep --since 2026-10-15T00:00 --until 2026-10-16T00:00 --estimate
    python -m collector.cli run --source trudvsem --pages 2 --limit 50
    python -m collector.cli run --source wikipedia-regions      # справочник регионов
    python -m collector.cli stats
    python -m collector.cli districts                            # вакансии по федеральным округам
    python -m collector.cli report --region Москва --salary-min 150000   # HTML-отчёт
    python -m collector.cli export --format xlsx --exclude "дворник,плиточник"
    python -m collector.cli sources
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from collector.config import load_settings
from collector.core.http import HttpClient
from collector.core.logging_setup import setup_logging
from collector.core.pipeline import Sink, run_source
from collector.core.robots import RobotsCache
from collector.core.timeutils import parse_duration, parse_moment
from collector.sources.base import Source
from collector.sources.trudvsem_api import TrudvsemSource
from collector.sources.wikipedia_regions import WikipediaRegionsSource
from collector.storage.db import create_db, healthcheck
from collector.storage.models import RunLog
from collector.storage.repository import (
    UNMATCHED,
    VacancyQuery,
    count_regions,
    count_search_vacancies,
    count_vacancies,
    ensure_search_keys,
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


def split_list(value: str | None) -> tuple[str, ...]:
    """Разобрать список через запятую: «python, аналитик данных» → два запроса."""
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def add_filter_arguments(parser: argparse.ArgumentParser, default_limit: int) -> None:
    """Фильтры отбора вакансий. Одинаковы для отчёта и выгрузки — их и описываем один раз."""
    parser.add_argument(
        "--text",
        help="поисковые подстроки через запятую: «python,django,backend» — подходит, если совпала любая",
    )
    parser.add_argument("--region", help="регион (подстрока или точное название)")
    parser.add_argument("--district", help="федеральный округ, например «Сибирский»")
    parser.add_argument("--salary-min", type=int, help="зарплата не ниже указанной (по верхней границе вакансии)")
    parser.add_argument("--remote", action="store_true", help="только вакансии с признаком удалённой работы")
    parser.add_argument(
        "--exclude",
        help="слова через запятую: вакансии с ними в названии не попадут в отчёт или файл "
        "(например: «дворник,плиточник,уборщик,продавец,водитель»)",
    )
    parser.add_argument("--limit", type=int, default=default_limit, help="сколько записей взять")


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

    export = subparsers.add_parser("export", help="выгрузить отобранные вакансии в CSV или Excel")
    export.add_argument("--format", choices=("csv", "xlsx"), default="csv", help="формат файла")
    export.add_argument("--out", help="куда записать файл (по умолчанию exports/vacancies.<формат>)")
    add_filter_arguments(export, default_limit=200)

    report = subparsers.add_parser("report", help="собрать HTML-страницу с отобранными вакансиями")
    report.add_argument("--out", default="exports/report.html", help="куда записать страницу")
    add_filter_arguments(report, default_limit=500)

    sweep = subparsers.add_parser(
        "sweep",
        help="собрать всё за период, деля окно времени (обходит сломанную пагинацию источника)",
    )
    sweep.add_argument("--since", default="24h", help="начало периода: 2026-10-15, 2026-10-15T08:00 или 24h, 7d, 2h")
    sweep.add_argument("--until", help="конец периода (по умолчанию — сейчас)")
    sweep.add_argument(
        "--text",
        help="поисковые запросы через запятую: «python,аналитик данных,sql». Не указан — собираем всё",
    )
    sweep.add_argument("--window", default="1m", help="минимальная ширина окна: 30s, 1m, 2h")
    sweep.add_argument("--limit", type=int, default=100, help="размер страницы источника (1..100)")
    sweep.add_argument("--max-requests", type=int, help="бюджет запросов: остановиться, когда исчерпан")
    sweep.add_argument("--estimate", action="store_true", help="только оценить объём, ничего не собирать")
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


def make_query_spec(args: argparse.Namespace) -> VacancyQuery:
    """Условия отбора собираются в одном месте — отчёт и выгрузка понимают фильтры одинаково."""
    return VacancyQuery(
        texts=split_list(getattr(args, "text", None)),
        region=args.region,
        district=args.district,
        salary_min=args.salary_min,
        remote_only=args.remote,
        exclude_text=split_list(getattr(args, "exclude", None)),
        limit=args.limit,
    )


def cmd_export(args: argparse.Namespace, session_factory, logger: logging.Logger, db_url: str) -> int:
    """Выгрузить вакансии по условиям в CSV или Excel."""
    from collector.core.export import write_vacancies_csv

    query_spec = make_query_spec(args)
    with session_factory() as session:
        rows = search_vacancies(session, query_spec)
        found = count_search_vacancies(session, query_spec)

    if not rows:
        logger.info("по этим условиям ничего не найдено — проверьте фильтры или соберите больше данных")
        return 0

    destination = Path(args.out) if args.out else Path(f"exports/vacancies.{args.format}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    if args.format == "xlsx":
        from collector.core.xlsx import write_vacancies_xlsx

        with destination.open("wb") as stream:
            written = write_vacancies_xlsx(rows, stream, query_spec=query_spec, db_url=db_url)
    else:
        # utf-8-sig: Excel иначе не понимает кириллицу в CSV.
        with destination.open("w", encoding="utf-8-sig", newline="") as stream:
            written = write_vacancies_csv(rows, stream)

    logger.info("выгружено вакансий: %s (всего под условия подходит %s)", written, found)
    if found > written:
        logger.info("в файл попали не все: поднимите --limit, сейчас он %s", args.limit)
    logger.info("файл: %s", destination.resolve())
    return 0


def cmd_report(args: argparse.Namespace, session_factory, logger: logging.Logger, db_url: str) -> int:
    """Собрать HTML-страницу с отобранными вакансиями: её открывают двойным кликом."""
    from collector.core.report import write_report_html

    query_spec = make_query_spec(args)
    with session_factory() as session:
        rows = search_vacancies(session, query_spec)
        found = count_search_vacancies(session, query_spec)

    if not rows:
        logger.info("по этим условиям ничего не найдено — проверьте фильтры или соберите больше данных")
        return 0

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        written = write_report_html(rows, stream, query_spec=query_spec, db_url=db_url)

    logger.info("в отчёте вакансий: %s (всего под условия подходит %s)", written, found)
    if found > written:
        logger.info("в отчёт попали не все: поднимите --limit, сейчас он %s", args.limit)
    logger.info("страница: %s", destination.resolve())
    return 0


def cmd_sweep(args: argparse.Namespace, http: HttpClient, session_factory, logger: logging.Logger) -> int:
    """Собрать всё за период: окна времени делятся пополам, пока не уложатся в лимит источника.

    Зачем так, а не страницами: пагинация источника сломана (`offset` глубже сотни записей даёт
    HTTP 500), зато работает фильтр по дате изменения. Подробности — в core/sweep.py.
    """
    from collector.core.sweep import estimate_period, sweep_period

    try:
        since = parse_moment(args.since)
        until = parse_moment(args.until) if args.until else datetime.now(timezone.utc)
        min_window = parse_duration(args.window)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    texts = split_list(args.text) or (None,)
    if not texts:
        texts = (None,)

    logger.info(
        "период: %s — %s (запросов на источнике: %s)",
        since.isoformat(timespec="seconds"),
        until.isoformat(timespec="seconds"),
        ", ".join(f"«{text}»" if text else "без фильтра по тексту" for text in texts),
    )

    if args.estimate:
        total_requests = 0
        for text in texts:
            source = TrudvsemSource(http, limit=args.limit, text=text)
            try:
                total = estimate_period(source, since, until)
            except Exception as exc:
                logger.error("«%s»: не удалось получить оценку: %s", text or "без фильтра", exc)
                continue
            # Грубая, но полезная прикидка: листьев примерно total/limit, плюс столько же
            # внутренних узлов дерева. Реальное число будет меньше — окна заполняются неравномерно.
            requests_estimate = max(2, 2 * -(-total // max(1, args.limit)))
            total_requests += requests_estimate
            logger.info(
                "«%s»: источник заявляет %s записей; ориентировочно %s запросов (это нижняя оценка)",
                text or "без фильтра",
                total,
                requests_estimate,
            )
        logger.info("итого ориентировочно запросов: %s (пауза между ними %s с)", total_requests, http.delay)
        return 0

    summary: list[str] = []
    for text in texts:
        source = TrudvsemSource(http, limit=args.limit, text=text)
        label = text or "без фильтра по тексту"
        logger.info("=== сбор: %s ===", label)
        try:
            result = sweep_period(
                source,
                session_factory,
                since,
                until,
                logger,
                min_window=min_window,
                max_requests=args.max_requests,
            )
        except ValueError as exc:
            logger.error("%s", exc)
            return 2
        summary.append(
            f"{label}: окон {result.windows_done}, записей {result.stats.items_parsed}, "
            f"новых {result.stats.items_inserted}"
            + (f", обрезанных окон {result.truncated_windows}" if result.truncated_windows else "")
            + (", бюджет запросов исчерпан" if result.stopped_early else "")
        )

    with session_factory() as session:
        logger.info("в базе теперь: вакансий %s, регионов %s", count_vacancies(session), count_regions(session))
    for line in summary:
        logger.info("итог: %s", line)
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

    # Поисковые ключи появились позже самих данных: у старых записей они пустые, и фильтры
    # по русским словам молча теряли бы вакансии. Заполняем при первом запуске.
    filled = ensure_search_keys(session_factory)
    if filled:
        logger.info("поисковые ключи заполнены у %s записей", filled)

    if args.command == "stats":
        return cmd_stats(session_factory, logger)
    if args.command == "districts":
        return cmd_districts(session_factory, logger)
    if args.command == "export":
        return cmd_export(args, session_factory, logger, db_url)
    if args.command == "report":
        return cmd_report(args, session_factory, logger, db_url)

    http = HttpClient(
        user_agent=settings.user_agent,
        timeout=settings.timeout,
        delay=settings.delay,
        max_retries=settings.max_retries,
        logger=logger,
    )

    if args.command == "sweep":
        return cmd_sweep(args, http, session_factory, logger)

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
