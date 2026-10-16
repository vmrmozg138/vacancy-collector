"""HTML-отчёт по собранным вакансиям.

Зачем отдельный формат, если есть CSV: CSV открывают в Excel, чтобы фильтровать числовые
колонки, а по вакансиям хочется читать глазами — название, компания, ссылка, описание.
HTML-страница закрывает именно это: файл открывается двойным кликом, ссылки кликабельны,
фильтры работают без интернета и без сервера.

Решения, которые здесь важны:

* **Страница самодостаточна.** Ни одного внешнего адреса — ни CDN, ни шрифтов,
  ни скриптов. Иначе отчёт перестанет открываться там, где нет сети, а он нужен
  именно как артефакт, который можно переслать и открыть у кого угодно.
* **Фильтры на стороне браузера.** Данные уже в файле, поэтому поиск и отбор — это
  перебор строк, а не запросы. Никакого сервера для просмотра не нужно.
* **Всё экранируется.** Данные приходят из внешнего источника: компания вправе назваться
  как угодно, и название не должно превращаться в разметку.
"""

from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Sequence

from collector.core.remote import OFFICIAL, classify_remote
from collector.core.vacancy_view import (
    REMOTE_BADGES,
    experience_text,
    salary_text,
    shorten,
    vacancy_details,
)
from collector.storage.repository import VacancyQuery, VacancyRow

#: Сколько позиций показывать в сводных списках («топ регионов» и т. п.).
TOP_LIMIT = 12


@dataclass(slots=True)
class ReportSummary:
    """Цифры, которые считаются по отобранным строкам."""

    shown: int = 0
    found: int = 0
    with_salary: int = 0
    salary_high: int = 0
    remote_official: int = 0
    remote_mentioned: int = 0
    average_salary: int | None = None
    max_salary: int | None = None
    by_district: list[tuple[str, int]] = field(default_factory=list)
    top_regions: list[tuple[str, int]] = field(default_factory=list)
    top_companies: list[tuple[str, int]] = field(default_factory=list)

    @property
    def salary_share(self) -> int:
        """Доля вакансий с указанной зарплатой, в процентах."""
        return round(self.with_salary / self.shown * 100) if self.shown else 0


def _salary_value(row: VacancyRow) -> int | None:
    """Зарплата для сравнений: верхняя граница, если её нет — нижняя."""
    vacancy = row.vacancy
    return vacancy.salary_max or vacancy.salary_min


def build_summary(rows: Sequence[VacancyRow], found: int, *, high_salary: int = 150_000) -> ReportSummary:
    """Посчитать сводку по отобранным вакансиям."""
    summary = ReportSummary(shown=len(rows), found=found)
    salaries: list[int] = []
    districts: Counter[str] = Counter()
    regions: Counter[str] = Counter()
    companies: Counter[str] = Counter()

    for row in rows:
        vacancy = row.vacancy
        salary = _salary_value(row)
        if salary:
            salaries.append(salary)
            if salary >= high_salary:
                summary.salary_high += 1
        if vacancy.salary_min or vacancy.salary_max:
            summary.with_salary += 1

        kind = classify_remote(vacancy.raw)
        if kind == OFFICIAL:
            summary.remote_official += 1
        elif kind:
            summary.remote_mentioned += 1

        districts[row.federal_district or "не сопоставлено"] += 1
        if vacancy.region:
            regions[vacancy.region] += 1
        if vacancy.company:
            companies[vacancy.company] += 1

    if salaries:
        summary.average_salary = round(sum(salaries) / len(salaries))
        summary.max_salary = max(salaries)
    summary.by_district = districts.most_common()
    summary.top_regions = regions.most_common(TOP_LIMIT)
    summary.top_companies = companies.most_common(TOP_LIMIT)
    return summary


CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 28px 64px;
  font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  color: #1c1f23; background: #f5f6f8;
}
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0 0 12px; text-transform: uppercase; letter-spacing: .04em; color: #5b6470; }
.meta { color: #5b6470; margin: 0 0 20px; font-size: 13px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.card {
  background: #fff; border: 1px solid #e2e5ea; border-radius: 10px;
  padding: 12px 16px; min-width: 150px;
}
.card .num { display: block; font-size: 24px; font-weight: 600; }
.card .cap { color: #5b6470; font-size: 12.5px; }
.panels { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 20px; }
.panel { background: #fff; border: 1px solid #e2e5ea; border-radius: 10px; padding: 16px; flex: 1 1 300px; }
.bars { list-style: none; margin: 0; padding: 0; }
.bars li { display: grid; grid-template-columns: 1fr auto; gap: 8px; margin-bottom: 6px; font-size: 13.5px; }
.bars .bar { grid-column: 1 / -1; height: 6px; background: #eef1f5; border-radius: 4px; overflow: hidden; }
.bars .bar span { display: block; height: 100%; background: #4a7cf6; }
.bars .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bars .count { color: #5b6470; font-variant-numeric: tabular-nums; }
.filters {
  position: sticky; top: 0; z-index: 2; background: #fff; border: 1px solid #e2e5ea;
  border-radius: 10px; padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 12px;
  align-items: flex-end; margin-bottom: 12px;
}
.filters label { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; color: #5b6470; }
.filters input, .filters select {
  font: inherit; padding: 6px 8px; border: 1px solid #cfd5de; border-radius: 6px; background: #fff; min-width: 120px;
}
.filters .check { flex-direction: row; align-items: center; gap: 6px; padding-bottom: 7px; }
.counter { margin-left: auto; color: #5b6470; font-size: 13px; padding-bottom: 7px; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e2e5ea; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid #eef1f5; vertical-align: top; font-size: 13.5px; }
/* Шапка «прилипает» под панелью фильтров. Высоту панели считает JS и кладёт в переменную:
   на узком окне фильтры переносятся на две строки, и жёсткие 73px тут не годятся.
   Важно: у самой таблицы не должно быть overflow: hidden — иначе она становится
   отдельным контейнером прокрутки, шапка прилипает к таблице и наезжает на первую строку. */
th { background: #f9fafc; position: sticky; top: var(--filters-height, 73px); z-index: 1; font-size: 12.5px; color: #5b6470; text-transform: uppercase; letter-spacing: .03em; }
tbody tr:hover { background: #f7f9ff; }
td.title { min-width: 220px; max-width: 320px; }
td.title a { color: #1a4fd6; text-decoration: none; font-weight: 500; }
td.title a:hover { text-decoration: underline; }
td.duty { color: #4a525c; max-width: 380px; }
.badge {
  display: inline-block; margin-top: 4px; padding: 1px 7px; border-radius: 20px;
  font-size: 11.5px; background: #e6f0ff; color: #1a4fd6; white-space: nowrap;
}
.badge.weak { background: #f1f2f4; color: #5b6470; }
.legend { color: #5b6470; font-size: 12.5px; margin: 10px 0 0; }
.empty { padding: 24px; text-align: center; color: #5b6470; display: none; }
.num-cell { font-variant-numeric: tabular-nums; white-space: nowrap; }
"""

JS = """
(function () {
  var rows = Array.prototype.slice.call(document.querySelectorAll('tbody tr'));
  var search = document.getElementById('f-search');
  var region = document.getElementById('f-region');
  var salary = document.getElementById('f-salary');
  var remote = document.getElementById('f-remote');
  var sort = document.getElementById('f-sort');
  var counter = document.getElementById('f-counter');
  var empty = document.getElementById('f-empty');
  var tbody = document.querySelector('tbody');

  function matches(row) {
    var needle = search.value.trim().toLowerCase();
    if (needle && row.dataset.text.indexOf(needle) === -1) return false;
    if (region.value && row.dataset.region !== region.value) return false;
    if (salary.value && Number(row.dataset.salary || 0) < Number(salary.value)) return false;
    if (remote.checked && row.dataset.remote !== 'official') return false;
    return true;
  }

  function apply() {
    var visible = 0;
    rows.forEach(function (row) {
      var ok = matches(row);
      row.style.display = ok ? '' : 'none';
      if (ok) visible += 1;
    });
    counter.textContent = 'показано ' + visible + ' из ' + rows.length;
    empty.style.display = visible ? 'none' : 'block';
  }

  function reorder() {
    var key = sort.value;
    var sorted = rows.slice().sort(function (a, b) {
      if (key === 'salary') return Number(b.dataset.salary || 0) - Number(a.dataset.salary || 0);
      if (key === 'company') return a.dataset.company.localeCompare(b.dataset.company, 'ru');
      return b.dataset.date.localeCompare(a.dataset.date);
    });
    sorted.forEach(function (row) { tbody.appendChild(row); });
    apply();
  }

  [search, region, salary, remote].forEach(function (el) {
    el.addEventListener('input', apply);
    el.addEventListener('change', apply);
  });
  sort.addEventListener('change', reorder);

  // Высота панели фильтров меняется (перенос строк на узком окне), а шапка таблицы
  // должна прилипать ровно под ней — поэтому считаем её и отдаём в CSS-переменную.
  var filters = document.querySelector('.filters');
  function syncFiltersHeight() {
    if (filters) {
      document.documentElement.style.setProperty('--filters-height', filters.offsetHeight + 'px');
    }
  }
  window.addEventListener('resize', syncFiltersHeight);
  syncFiltersHeight();

  apply();
})();
"""


def _bar_list(items: Sequence[tuple[str, int]]) -> str:
    """Список значений с полосками. Ширина полоски — доля от максимума."""
    if not items:
        return '<p class="meta">нет данных</p>'
    top = max(count for _, count in items) or 1
    parts = ['<ul class="bars">']
    for name, count in items:
        width = round(count / top * 100)
        parts.append(
            f'<li><span class="name">{html.escape(str(name))}</span>'
            f'<span class="count">{count}</span>'
            f'<span class="bar"><span style="width:{width}%"></span></span></li>'
        )
    parts.append("</ul>")
    return "".join(parts)


def _vacancy_row(row: VacancyRow) -> str:
    """Одна строка таблицы. Всё, что пришло из источника, экранируется."""
    vacancy = row.vacancy
    details = vacancy_details(vacancy.raw)
    kind = classify_remote(vacancy.raw)

    badge = ""
    if kind:
        css = "" if kind == OFFICIAL else " weak"
        badge = f'<br><span class="badge{css}">{html.escape(REMOTE_BADGES[kind])}</span>'

    title = html.escape(vacancy.title or "без названия")
    if vacancy.url:
        title = f'<a href="{html.escape(vacancy.url, quote=True)}" target="_blank" rel="noopener">{title}</a>'

    # Строка поиска собирается заранее и лежит в атрибуте: браузеру остаётся сравнить подстроки.
    # Ограничение в 400 символов заметно уменьшает вес страницы, не мешая искать по словам.
    searchable = shorten(
        " ".join(
            filter(None, [vacancy.title, vacancy.company, vacancy.region, details["duty"], details["skills"]])
        ),
        400,
    ).lower()
    salary = salary_text(vacancy.salary_min, vacancy.salary_max, vacancy.currency)
    published = vacancy.published_at.isoformat() if vacancy.published_at else ""
    employment = details["employment"] or details["schedule"]

    return (
        f'<tr data-region="{html.escape(vacancy.region or "—", quote=True)}" '
        f'data-remote="{kind or ""}" data-salary="{_salary_value(row) or 0}" '
        f'data-date="{published}" data-company="{html.escape(vacancy.company or "", quote=True)}" '
        f'data-experience="{details["experience"] if details["experience"] is not None else ""}" '
        f'data-text="{html.escape(searchable, quote=True)}">'
        f'<td class="title">{title}{badge}</td>'
        f'<td>{html.escape(vacancy.company or "—")}</td>'
        f'<td>{html.escape(vacancy.region or "—")}</td>'
        f'<td>{html.escape(row.federal_district or "—")}</td>'
        f'<td class="num-cell">{html.escape(salary)}</td>'
        f'<td class="num-cell">{html.escape(experience_text(details["experience"]))}</td>'
        f'<td>{html.escape(employment or "—")}</td>'
        f'<td class="num-cell">{published or "—"}</td>'
        f'<td class="duty">{html.escape(details["duty"] or "—")}</td>'
        f"</tr>"
    )


def render_report_html(
    rows: Sequence[VacancyRow],
    *,
    query_spec: VacancyQuery | None = None,
    db_url: str = "",
    source: str = "trudvsem",
    generated_at: datetime | None = None,
) -> str:
    """Собрать страницу отчёта целиком (HTML строка)."""
    query_spec = query_spec or VacancyQuery()
    generated_at = generated_at or datetime.now()
    summary = build_summary(rows, len(rows))

    conditions = [
        "текст: " + ", ".join(f"«{text}»" for text in query_spec.texts) if query_spec.texts else "",
        f"регион: «{query_spec.region}»" if query_spec.region else "",
        f"округ: «{query_spec.district}»" if query_spec.district else "",
        f"зарплата от {query_spec.salary_min:,}".replace(",", " ") if query_spec.salary_min else "",
        "только удалённые" if query_spec.remote_only else "",
        "исключено: " + ", ".join(query_spec.exclude_text) if query_spec.exclude_text else "",
    ]
    conditions_text = "; ".join(filter(None, conditions)) or "без фильтров"

    regions = sorted({row.vacancy.region for row in rows if row.vacancy.region})
    region_options = "".join(
        f'<option value="{html.escape(name, quote=True)}">{html.escape(name)}</option>' for name in regions
    )

    cards = [
        (summary.shown, "вакансий в отчёте"),
        (summary.with_salary, f"с зарплатой ({summary.salary_share}%)"),
        (summary.salary_high, "от 150 000 ₽"),
        (summary.remote_official, "удалённо по данным работодателя"),
        (summary.remote_mentioned, "удалёнка упоминается в тексте"),
    ]
    cards_html = "".join(
        f'<div class="card"><span class="num">{html.escape(str(value))}</span>'
        f'<span class="cap">{html.escape(caption)}</span></div>'
        for value, caption in cards
    )

    average = salary_text(summary.average_salary, None) if summary.average_salary else "—"
    maximum = salary_text(summary.max_salary, None) if summary.max_salary else "—"

    table_rows = "\n".join(_vacancy_row(row) for row in rows)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вакансии — отчёт от {generated_at:%d.%m.%Y}</title>
<style>{CSS}</style>
</head>
<body>
<h1>Вакансии: отчёт</h1>
<p class="meta">
  источник: «Работа России» (открытое API) &middot; сформирован {generated_at:%d.%m.%Y %H:%M} &middot;
  отбор: {html.escape(conditions_text)} &middot; база: {html.escape(db_url)}
</p>

<section class="cards">{cards_html}</section>

<section class="panels">
  <div class="panel"><h2>Федеральные округа</h2>{_bar_list(summary.by_district)}</div>
  <div class="panel"><h2>Регионы</h2>{_bar_list(summary.top_regions)}</div>
  <div class="panel"><h2>Компании</h2>{_bar_list(summary.top_companies)}</div>
</section>

<section class="panels">
  <div class="panel">
    <h2>Зарплаты</h2>
    <ul class="bars">
      <li><span class="name">средняя по верхней границе</span><span class="count">{html.escape(average)}</span></li>
      <li><span class="name">максимальная</span><span class="count">{html.escape(maximum)}</span></li>
      <li><span class="name">без зарплаты</span><span class="count">{summary.shown - summary.with_salary}</span></li>
    </ul>
    <p class="legend">Средняя считается по верхней границе вакансии, если она указана, иначе по нижней.</p>
  </div>
</section>

<section class="filters">
  <label>поиск по названию, компании, описанию
    <input id="f-search" type="search" placeholder="например: python, аналитик, sql">
  </label>
  <label>регион
    <select id="f-region"><option value="">все регионы</option>{region_options}</select>
  </label>
  <label>зарплата от
    <input id="f-salary" type="number" min="0" step="10000" placeholder="150000">
  </label>
  <label class="check"><input id="f-remote" type="checkbox"> только удалённо</label>
  <label>сортировка
    <select id="f-sort">
      <option value="date">по дате публикации</option>
      <option value="salary">по зарплате</option>
      <option value="company">по компании</option>
    </select>
  </label>
  <span class="counter" id="f-counter">показано {summary.shown} из {summary.shown}</span>
</section>

<table>
  <thead>
    <tr>
      <th>Вакансия</th><th>Компания</th><th>Регион</th><th>Округ</th>
      <th>Зарплата</th><th>Опыт</th><th>Занятость</th><th>Дата</th><th>Кратко из описания</th>
    </tr>
  </thead>
  <tbody>
{table_rows}
  </tbody>
</table>
<p class="empty" id="f-empty">По этим условиям ничего не нашлось.</p>
<p class="legend">
  «удалённо» — так указал работодатель в поле «занятость»; «есть упоминание» — слова про удалённую
  работу встречаются в тексте вакансии. Это разные по надёжности признаки, поэтому они не смешиваются.
</p>
<script>{JS}</script>
</body>
</html>
"""


def write_report_html(
    rows: Sequence[VacancyRow],
    stream: IO[str],
    *,
    query_spec: VacancyQuery | None = None,
    db_url: str = "",
    generated_at: datetime | None = None,
) -> int:
    """Записать отчёт в поток. Возвращает число строк — как и выгрузка CSV."""
    stream.write(
        render_report_html(
            rows,
            query_spec=query_spec,
            db_url=db_url,
            generated_at=generated_at,
        )
    )
    return len(rows)
