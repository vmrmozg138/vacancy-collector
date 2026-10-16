# Как этим пользоваться: локальный запуск и просмотр данных

Практическая инструкция: как самому собрать вакансии, куда они попадают и как с ними
работать. Теория и устройство проекта — в [../README.md](../README.md).

Все команды выполняются из папки `collector` (там, где лежит `pyproject.toml`).

---

## 1. Подготовка (один раз)

```powershell
cd C:\Users\user\Documents\deepseek-harness\default-workspace\collector
poetry install                 # поставит зависимости в .venv рядом с проектом
```

Файл `.env` уже создан и настроен на PostgreSQL в Docker (см. раздел 3). Если его нет —
скопируй из образца: `copy .env.example .env` (тогда по умолчанию будет SQLite).

Проверка, что всё живо:

```powershell
poetry run python -m collector.cli stats
```

Ожидаешь строку вида `база: postgresql+psycopg://collector:collector@localhost:5433/collector
(диалект: postgresql)` и число собранных вакансий. Если Docker не запущен, будет ошибка
подключения — запусти Docker Desktop и повтори.

> **Про Poetry.** На этой машине их несколько, и короткая команда `poetry` должна быть
> версии 2.x (`poetry --version` → 2.5.1). Если увидишь 1.8.0 — это старый Poetry из PATH,
> он не понимает `[project]` в `pyproject.toml`. Тогда запускай через `py -m poetry ...`
> или поправь порядок путей в переменной среды `Path`.

### Запуск из PyCharm

1. Открыть папку `collector` как проект.
2. Убедиться, что выбран интерпретатор из `collector/.venv` (Settings → Project → Python Interpreter).
3. `Run` → `Edit Configurations` → `+` → `Python`:
   - **Module name**: `collector.cli`
   - **Parameters**: `stats` (или любую другую команду, например `report --region Москва`)
   - **Working directory**: `...\default-workspace\collector`
4. Сохранить и запускать кнопкой. Для других команд удобно сделать несколько конфигураций.

Либо просто открыть встроенный терминал PyCharm: команды ниже работают в нём как есть.

---

## 2. Собрать вакансии

### Обычный сбор (быстрый, первые сто записей на запрос)

```powershell
poetry run python -m collector.cli run --source trudvsem --pages 2 --limit 100 --text python
```

Этот способ годится для быстрой пробы: источник отдаёт около сотни записей на запрос,
а глубже пагинация у него сломана (`offset=100` → HTTP 500).

### Массовый сбор: `sweep` — «собрать всё за период»

Источник умеет фильтровать по дате изменения (`modifiedFrom`/`modifiedTo`), и если в окне
времени записей меньше лимита, он отдаёт их все. Поэтому `sweep` **делит период пополам**,
пока окно не станет достаточно узким. Так собирается больше сотни записей — сколько угодно,
вплоть до всего, что есть в источнике.

Сначала стоит прикинуть объём:

```powershell
# сколько вакансий с «python» за последние сутки и сколько это будет запросов
poetry run python -m collector.cli sweep --since 24h --text python --estimate
```

Потом собрать:

```powershell
# python за сутки
poetry run python -m collector.cli sweep --since 24h --text python

# несколько направлений сразу: каждый запрос — отдельный проход
poetry run python -m collector.cli sweep --since 7d --text "python,django,аналитик данных,sql"

# конкретный период (время без пояса считается местным, Москва)
poetry run python -m collector.cli sweep --since 2026-10-15T00:00 --until 2026-10-16T00:00 --text python

# всё за час без фильтра по тексту (проверка механики)
poetry run python -m collector.cli sweep --since 2026-10-15T12:00 --until 2026-10-15T13:00
```

Параметры `sweep`:

| Ключ | Смысл |
|---|---|
| `--since` | начало периода: `2026-10-15`, `2026-10-15T08:00`, `24h`, `7d`, `2h` |
| `--until` | конец периода (по умолчанию — сейчас) |
| `--text` | поисковые запросы через запятую; не указан — собираем всё |
| `--window` | минимальная ширина окна, по умолчанию `1m` (минута вмещает ~40 записей) |
| `--limit` | размер страницы источника, 1..100 |
| `--max-requests` | бюджет запросов: сбор остановится и продолжит в следующий раз |
| `--estimate` | только оценить объём, ничего не собирать |

**Сколько это занимает.** Здесь важная цифра: источник отвечает **12–16 секунд на запрос**
(измерено 16.10.2026, три замера подряд — 12,7 / 12,1 / 14,2 с; от размера запроса время
почти не зависит). Поэтому считать надо по числу запросов, а не по объёму данных:

| Что собираем | Запросов (примерно) | Время |
|---|---|---|
| один текст за сутки (`--text python`) | 2 | ~30 секунд |
| 10 текстов за сутки | 20–40 | 5–10 минут |
| час без фильтра по тексту (около 2 000 записей) | 40–80 | 10–17 минут |
| сутки без фильтра по тексту (22 000–32 000 записей) | 450–600 | 1,5–2 часа |
| неделя без фильтра | тысячи | 10+ часов — не советую |

Отсюда практический вывод: **собирай по нужным направлениям** (`--text`), а не «всё подряд».
Для поиска работы этого достаточно: по каждому направлению за неделю набирается
от нескольких десятков до сотен вакансий, и каждый текст стоит 2–5 запросов.
`--estimate` показывает объём до запуска, `--max-requests` ограничивает расход.

**Если прервать** (Ctrl+C, обрыв сети): уже записанные окна остаются в базе, повторный
запуск не создаст дублей — запись идёт через upsert по паре «источник + id вакансии».
Лог показывает, сколько окон обработано и какое покрытие от заявленного источником.

---

## 3. Где смотреть данные

### Основной вариант: PostgreSQL в Docker + pgAdmin

Так проект настроен сейчас: в `.env` стоит строка

```
COLLECTOR_DB_URL=postgresql+psycopg://collector:collector@localhost:5433/collector
```

Docker Desktop должен быть запущен — при старте он сам поднимает контейнер `collector-postgres`
(в compose стоит `restart: unless-stopped`). Управление базой:

```powershell
docker compose up -d        # поднять (данные хранятся в томе, переживают перезапуск)
docker compose ps           # ждём статус healthy
docker compose down         # остановить, данные останутся
docker compose down -v      # остановить и стереть данные вместе с томом
```

Проверить, что проект видит базу:

```powershell
poetry run python -m collector.cli stats
```

Подключение в pgAdmin 4 (`Add New Server` → Connection):

| Поле | Значение |
|---|---|
| Host | `localhost` |
| Port | `5433` (**не** 5432 — там локальный PostgreSQL 18) |
| Maintenance database | `collector` |
| Username | `collector` |
| Password | `collector` |

Почему порт 5433: на 5432 слушает локальный PostgreSQL, установленный в систему. Подробности —
в [../README.md](../README.md), раздел «Если порт занят».

### Вариант Б: SQLite — без Docker

Если Docker не нужен, поставь в `.env` строку `COLLECTOR_DB_URL=sqlite:///collector.db` —
и всё заработает с файлом `collector/collector.db`. Минус один: **pgAdmin её не откроет**,
он работает только с PostgreSQL; смотреть придётся командами проекта, через Python/pandas
или DB Browser for SQLite.

Важно: SQLite и PostgreSQL — **разные базы**, данные между ними не переезжают сами.
Выбери одну и держись её; если нужно перенести уже собранное, проще всего прогнать `sweep`
за тот же период в новой базе — upsert не создаст дублей.

### Вариант В. Excel / pandas

```powershell
poetry run python -m collector.cli export --format xlsx --salary-min 100000 --out exports/moscow.xlsx
poetry run python -m collector.cli export --region Москва --text аналитик --out exports/moscow.csv
```

```python
import pandas as pd
df = pd.read_csv("exports/moscow.csv", sep=";", encoding="utf-8-sig")
df.sort_values("salary_max", ascending=False).head(20)
```

---

## 4. SQL-запросы, с которых удобно начать (pgAdmin)

```sql
-- сколько всего собрано и когда последний раз
select count(*) from vacancies;
select source, started_at, items_inserted, items_updated, errors, status
from runs order by id desc limit 10;

-- топ регионов
select region, count(*) as cnt from vacancies group by region order by cnt desc limit 20;

-- вакансии с официальным признаком удалённой работы и зарплатой от 150 000
select title, company, region, salary_min, salary_max, url
from vacancies
where raw like '%Дистанционная (удаленная) работа%'
  and coalesce(salary_max, salary_min) >= 150000
order by coalesce(salary_max, salary_min) desc;

-- что просят в требованиях: поля лежат в исходном JSON
select title, company,
       raw::json ->> 'duty'         as обязанности,
       raw::json ->> 'requirements' as требования,
       raw::json #>> '{requirement,experience}' as опыт_лет
from vacancies
where title_key like '%аналитик%'
limit 20;

-- поиск по описанию и навыкам (в PostgreSQL ilike работает с кириллицей)
select title, company, region
from vacancies
where raw ilike '%GreenData%' or raw ilike '%low-code%';

-- свежие вакансии
select title, company, region, published_at, url
from vacancies order by published_at desc nulls last limit 30;
```

В SQLite те же запросы работают, если заменить `raw::json ->> 'duty'` на `json_extract(raw, '$.duty')`,
а `ilike` — на `like` (регистр кириллицы там не учитывается, поэтому для поиска лучше
пользоваться колонками `title_key` / `company_key`: они хранятся в нижнем регистре).

---

## 5. Фильтры: как не тащить в отчёт дворников и плиточников

Полный список ключей — в [../README.md](../README.md). Главное по порядку:

1. **Не собирать лишнее** — самый дешёвый способ. Собирай по нужным направлениям:
   `sweep --since 7d --text "python,аналитик данных,sql,low-code"`. Источник ищет по тексту
   у себя, поэтому в базу попадут только эти вакансии.
2. **Отсечь остаточный мусор при показе** — ключ `--exclude`: вакансия не попадёт в отчёт
   или файл, если такое слово есть в её названии.

```powershell
poetry run python -m collector.cli report --region Москва --salary-min 150000 `
  --exclude "дворник,плиточник,уборщик,продавец,водитель,грузчик,кассир,повар,охранник,курьер,кладовщик,оператор,слесарь,монтажник,маляр,штукатур,сварщик,электрик,сантехник,подсобный,сиделка,сторож,вахтёр"

poetry run python -m collector.cli export --format xlsx --remote `
  --exclude "дворник,плиточник,уборщик,продавец" --out exports/remote.xlsx
```

3. **Остальные фильтры**: `--text` (подстрока в названии, компании или регионе — регистр
   русских букв больше не важен), `--region`, `--district`, `--salary-min`, `--remote`
   (сначала вакансии, где работодатель сам указал дистанционную работу, затем те, где
   удалёнка упомянута в тексте).

---

## 6. Отчёты для чтения

```powershell
# HTML-страница: сводка, фильтры в браузере, ссылки на вакансии
poetry run python -m collector.cli report --region Москва --salary-min 100000
# → exports/report.html, открывается двойным кликом

# Excel с автофильтром и листом «Сводка»
poetry run python -m collector.cli export --format xlsx --region Москва --out exports/moscow.xlsx
```

Оба файла собираются по текущему состоянию базы: сначала собери (`sweep`), потом строй отчёт.

---

## 7. Если что-то пошло не так

| Симптом | Причина и что делать |
|---|---|
| `No module named poetry` | запускаешь `py -m poetry` из активированного venv проекта; выйди (`deactivate`) или используй команду `poetry` |
| `[tool.poetry] section not found` | сработал старый Poetry 1.8.0 из PATH; нужен 2.x (`poetry --version`) |
| `HTTP 500` при сборе | пагинация источника; используй `sweep`, а не `run --pages` |
| Сбор идёт слишком долго | прикинь объём через `--estimate`, сузь период или список `--text`, ограничь `--max-requests` |
| Отчёт пустой | сначала собери данные; проверь фильтры — возможно, слишком узкие |
| В отчёте не видно вакансий, которые есть в базе | проверь `--limit` (по умолчанию 500 у отчёта) |
