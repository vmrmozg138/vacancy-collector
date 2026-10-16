"""Представление вакансии для отчётов: человеческие подписи и разбор исходного JSON.

Один и тот же вид нужен и HTML-странице, и Excel-файлу — поэтому он здесь, а не в каждом
формате по отдельности. Иначе подписи «от 90 000 ₽» и обрезка описания разъехались бы
между отчётами.

Исходный JSON (`raw`) хранится в базе именно для этого: источник отдаёт в нём поля,
которых нет в модели — обязанности, требования, график, навыки, — и их можно достать,
не переделывая сбор и не меняя схему.
"""

from __future__ import annotations

import json
from typing import Any

from collector.core.remote import MENTIONED, OFFICIAL, classify_remote

#: Сколько символов описания показывать в отчёте. Полный текст — в вакансии по ссылке.
DUTY_LIMIT = 240


def shorten(text: Any, limit: int = DUTY_LIMIT) -> str:
    """Обрезать текст до лимита по границе слова.

    Обрезка «по буквам» выглядит неряшливо: «Разработка внутренних инструменто…».
    Поэтому отступаем назад до последнего пробела — но только если он не в самом начале,
    иначе на коротком лимите от текста ничего не останется.
    """
    cleaned = as_text(text)
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[: limit - 1].rstrip()
    space = cut.rfind(" ")
    if space >= (limit - 1) * 0.6:
        cut = cut[:space].rstrip()
    return cut + "…"


def as_text(value: Any) -> str:
    """Привести значение источника к строке.

    Источник отдаёт часть полей то строкой, то списком, то словарём (`skills` — список,
    `requirement` — словарь). Разбор не должен падать из-за этого: приводим к тексту
    и схлопываем пробелы.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        parts = [f"{key}: {item}" for key, item in value.items() if item not in (None, "")]
        return " ".join(parts)
    if isinstance(value, (list, tuple, set)):
        return " ".join(filter(None, (as_text(item) for item in value)))
    return " ".join(str(value).split())


def as_int(value: Any) -> int | None:
    """Привести значение к целому числу; всё непонятное — None, а не исключение."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_raw(raw: str | None) -> dict[str, Any]:
    """Разобрать исходный JSON вакансии. Битая запись не должна ронять отчёт."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def vacancy_details(raw: str | None) -> dict[str, Any]:
    """Поля из исходного JSON, которых нет в модели, но которые нужны человеку."""
    data = parse_raw(raw)
    requirement = data.get("requirement")
    requirement = requirement if isinstance(requirement, dict) else {}
    return {
        "duty": shorten(data.get("duty")),
        "requirements": shorten(data.get("requirements")),
        "skills": shorten(data.get("skills"), 160),
        "employment": as_text(data.get("employment")),
        "schedule": as_text(data.get("schedule")),
        "education": as_text(requirement.get("education")),
        "experience": as_int(requirement.get("experience")),
    }


def experience_text(years: int | None) -> str:
    """Требуемый опыт словами: «3 года», «1 год», «не указан».

    Ноль у источника означает не «без опыта», а «не заполнено»: из 83 вакансий с нулём
    в поле опыта часть требует опыт прямо в тексте требований (например, руководитель
    риск-менеджмента инвестиционно-банковского бизнеса). Поэтому ноль показывается как
    «не указан» — утверждать «без опыта» там, где источник этого не сказал, нельзя.
    """
    if years is None or years <= 0:
        return "не указан"
    tail = "год" if years % 10 == 1 and years % 100 != 11 else ("года" if 2 <= years % 10 <= 4 and not 12 <= years % 100 <= 14 else "лет")
    return f"{years} {tail}"


def currency_symbol(currency: str | None) -> str:
    """Знак валюты для отчёта.

    Источник присылает валюту как ««руб.»» — с кавычками-ёлочками и точкой. Показывать это
    рядом с числом некрасиво, поэтому рубль (в любом написании) заменяется на «₽»,
    а всё остальное выводится как есть, чтобы не соврать про валюту.
    """
    cleaned = (currency or "").strip().strip("«»\"'").strip().lower()
    if not cleaned or cleaned.startswith("руб") or cleaned in {"rub", "rur", "₽", "р", "р."}:
        return "₽"
    return (currency or "").strip().strip("«»\"'")


def salary_text(salary_min: int | None, salary_max: int | None, currency: str | None = None) -> str:
    """Зарплата словами: «от 90 000 ₽», «150 000 – 200 000 ₽», «не указана».

    Пробел внутри разрядов — неразрывный: иначе Excel и браузер переносят «150» и «000»
    на разные строки, и цифра читается неправильно.
    """
    symbol = currency_symbol(currency)

    def money(value: int) -> str:
        return f"{value:,}".replace(",", "\u00a0")

    if salary_min and salary_max and salary_min != salary_max:
        return f"{money(salary_min)} – {money(salary_max)} {symbol}".strip()
    if salary_min and salary_max:
        return f"{money(salary_min)} {symbol}".strip()
    if salary_min:
        return f"от {money(salary_min)} {symbol}".strip()
    if salary_max:
        return f"до {money(salary_max)} {symbol}".strip()
    return "не указана"


def remote_text(raw: str | None) -> str:
    """Как показать признак удалёнки в отчёте.

    Два вида признака намеренно не смешиваются: «удалённо» по полю работодателя
    и «упоминается удалёнка» в тексте — разные по надёжности вещи.
    """
    kind = classify_remote(raw)
    if kind == OFFICIAL:
        return "удалённо (указал работодатель)"
    if kind == MENTIONED:
        return "упоминается в тексте"
    return ""


#: Короткая метка для таблицы — полный текст не влезает в колонку.
REMOTE_BADGES = {
    OFFICIAL: "удалённо",
    MENTIONED: "есть упоминание",
}


def remote_badge(raw: str | None) -> str:
    """Короткая метка удалёнки для ячейки таблицы."""
    return REMOTE_BADGES.get(classify_remote(raw) or "", "")
