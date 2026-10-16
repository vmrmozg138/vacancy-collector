"""Тесты нормализации названий.

Ключ нужен для сопоставления данных из разных источников, поэтому проверяем две вещи:
похожие названия одного региона сходятся в один ключ, а разные регионы — не сходятся.
"""

from __future__ import annotations

import pytest

from collector.core.normalize import normalize_name, search_key

SAME_REGION = [
    ("Город Москва", "Москва"),
    ("Город Санкт-Петербург", "Санкт-Петербург"),
    ("Чувашская Республика - Чувашия", "Чувашская Республика — Чувашия"),
    ("Республика Саха (Якутия)", "Саха (Якутия)"),
    ("Ханты-Мансийский автономный округ - Югра", "Ханты-Мансийский автономный округ — Югра"),
    ("Кемеровская область - Кузбасс", "Кемеровская область — Кузбасс"),
    ("Еврейская автономная область", "Еврейская автономная область"),
]

DIFFERENT_REGIONS = [
    ("Москва", "Московская область"),
    ("Республика Алтай", "Алтайский край"),
    ("Ненецкий автономный округ", "Чукотский автономный округ"),
    ("Республика Коми", "Республика Карелия"),
    ("Свердловская область", "Саратовская область"),
]


@pytest.mark.parametrize(("left", "right"), SAME_REGION)
def test_same_region_gives_same_key(left: str, right: str) -> None:
    assert normalize_name(left) == normalize_name(right)


@pytest.mark.parametrize(("left", "right"), DIFFERENT_REGIONS)
def test_different_regions_give_different_keys(left: str, right: str) -> None:
    assert normalize_name(left) != normalize_name(right)


def test_empty_values() -> None:
    assert normalize_name(None) == ""
    assert normalize_name("") == ""
    assert normalize_name("   ") == ""


def test_key_is_readable() -> None:
    # Ключ не должен превращаться в набор символов: по нему потом ищут глазами в отчётах.
    assert normalize_name("Город Москва") == "москва"
    assert normalize_name("Чувашская Республика - Чувашия") == "чувашия чувашская"


def test_search_key_lowercases_russian() -> None:
    """Поисковый ключ приводит кириллицу к нижнему регистру — этого не умеет SQLite."""
    assert search_key("Программист 1С") == "программист 1с"
    assert search_key("ДВОРНИК") == "дворник"
    assert search_key("Королёв") == "королев"
    assert search_key("  Python   Developer ") == "python developer"
    assert search_key(None) == ""


def test_search_key_keeps_words_that_normalize_name_drops() -> None:
    """Поисковый ключ не выбрасывает служебные слова: он ищет подстроку, а не сравнивает регионы."""
    assert search_key("Город Москва") == "город москва"
    assert normalize_name("Город Москва") == "москва"
