# Приложение в контейнере: сам сборщик. База поднимается отдельным сервисом (docker-compose.yml).
#
# Почему базовый образ slim и без браузера: Chromium с зависимостями весит сотни мегабайт
# и нужен только команде `render` (диагностика JavaScript). Добавлять его в основной образ —
# значит платить размером за возможность, которой пользуются раз в месяц.
#
# Сборка:  docker compose --profile app build
# Запуск:  docker compose --profile app run --rm app run --source trudvsem --pages 2
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install --no-cache-dir "poetry>=2.0,<3.0"

# Сначала только зависимости: этот слой кэшируется и не пересобирается при правке кода.
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root

COPY collector ./collector
COPY tools ./tools

# По умолчанию показываем список источников; конкретную команду передают при запуске.
ENTRYPOINT ["python", "-m", "collector.cli"]
CMD ["sources"]
