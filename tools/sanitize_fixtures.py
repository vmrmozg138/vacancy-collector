"""Обезличивание фикстур перед публикацией репозитория.

В ответах источника вместе с вакансиями приходят контакты работодателей: почты, телефоны,
имена контактных лиц. В публичном репозитории им не место, поэтому фикстуры прогоняются
через этот скрипт, а не коммитятся как есть.

Запуск:
    python -m tools.sanitize_fixtures            # все .json в tests/fixtures
    python -m tools.sanitize_fixtures path.json  # конкретный файл
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")

EMAIL_PLACEHOLDER = "user@example.com"
PHONE_PLACEHOLDER = "+7 (900) 000-00-00"
PERSON_PLACEHOLDER = "Контактное лицо"

# Поля, значение которых заменяется целиком, независимо от содержимого.
REPLACED_FIELDS = {"contact_person": PERSON_PLACEHOLDER}


class Counter:
    def __init__(self) -> None:
        self.emails = 0
        self.phones = 0
        self.persons = 0
        self.files = 0

    def summary(self) -> str:
        return (
            f"файлов={self.files} почт={self.emails} телефонов={self.phones} "
            f"контактных лиц={self.persons}"
        )


def clean_text(value: str, counter: Counter) -> str:
    # Повторный запуск безопасен: уже обезличенные значения остаются как есть.
    if value in {EMAIL_PLACEHOLDER, PHONE_PLACEHOLDER}:
        return value
    value, emails = EMAIL_RE.subn(EMAIL_PLACEHOLDER, value)
    value, phones = PHONE_RE.subn(PHONE_PLACEHOLDER, value)
    counter.emails += emails
    counter.phones += phones
    return value


def sanitize(node: object, counter: Counter) -> object:
    if isinstance(node, dict):
        result: dict[str, object] = {}
        for key, value in node.items():
            if key in REPLACED_FIELDS and value:
                result[key] = REPLACED_FIELDS[key]
                counter.persons += 1
            else:
                result[key] = sanitize(value, counter)
        return result
    if isinstance(node, list):
        return [sanitize(item, counter) for item in node]
    if isinstance(node, str):
        return clean_text(node, counter)
    return node


def sanitize_file(path: pathlib.Path, counter: Counter) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cleaned = sanitize(payload, counter)
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counter.files += 1
    print(f"обезличено: {path.name}")


def main(argv: list[str]) -> int:
    counter = Counter()
    targets = [pathlib.Path(arg) for arg in argv] or sorted(FIXTURES_DIR.glob("*.json"))
    if not targets:
        print("фикстуры не найдены", file=sys.stderr)
        return 1
    for path in targets:
        sanitize_file(path, counter)
    print(counter.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
