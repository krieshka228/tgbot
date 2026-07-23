"""
validators.py — чистые функции валидации пользовательского ввода.

Вынесены отдельно, чтобы:
    * переиспользовать в разных хендлерах (корзина, оформление, админка);
    * легко покрыть unit-тестами (функции не зависят от Telegram/БД).

Все функции детерминированы и не имеют побочных эффектов.
"""

from __future__ import annotations

import re

# Телефон: опциональный «+», затем 10–15 цифр (с учётом возможных пробелов,
# скобок и дефисов, которые мы вычищаем перед проверкой).
_PHONE_CLEANUP_RE = re.compile(r"[\s()\-]")
_PHONE_RE = re.compile(r"^\+?\d{10,15}$")
# Артикул: буквы/цифры/дефис/подчёркивание, до 32 символов (как в модели).
_ARTICLE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")

MAX_QUANTITY = 100_000  # разумный потолок против переполнения/абьюза


def normalize_phone(text: str | None) -> str | None:
    """Очищает телефон и валидирует его. Возвращает нормализованный номер
    (только «+» и цифры) или ``None``, если ввод некорректен."""
    if not text:
        return None
    cleaned = _PHONE_CLEANUP_RE.sub("", text.strip())
    if _PHONE_RE.match(cleaned):
        return cleaned
    return None


def parse_positive_int(text: str | None, *, max_value: int = MAX_QUANTITY) -> int | None:
    """Парсит строго положительное целое в диапазоне ``1..max_value``.

    Возвращает ``None`` при любом некорректном вводе (нечисло, ноль,
    отрицательное, превышение лимита). Используется для количества товара.
    """
    if text is None:
        return None
    try:
        value = int(text.strip())
    except (ValueError, AttributeError):
        return None
    if value < 1 or value > max_value:
        return None
    return value


def parse_non_negative_int(text: str | None, *, max_value: int = MAX_QUANTITY) -> int | None:
    """Парсит целое ``0..max_value`` (для остатка на складе: 0 = скрыть)."""
    if text is None:
        return None
    try:
        value = int(text.strip())
    except (ValueError, AttributeError):
        return None
    if value < 0 or value > max_value:
        return None
    return value


def is_valid_article(text: str | None) -> bool:
    """Проверяет формат артикула."""
    return bool(text) and bool(_ARTICLE_RE.match(text.strip()))
