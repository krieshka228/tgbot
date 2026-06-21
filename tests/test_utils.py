"""Unit-тесты для bot.utils — парсинг постов и форматирование."""

import pytest

from bot.utils import (
    _parse_post_link,
    escape_markdown,
    parse_post_product,
    parse_quantity,
)


@pytest.mark.parametrize(
    "text,expected",
    [("3 шт", 3), ("5", 5), ("хочу 2 штуки", 2), ("", None), ("нет числа", None)],
)
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == expected


def test_escape_markdown_escapes_special_chars():
    assert escape_markdown("a_b*c") == r"a\_b\*c"
    # Экранирование должно быть идемпотентно безопасным для парсера Telegram:
    assert "\\" in escape_markdown("price-100")


@pytest.mark.parametrize(
    "text,expected",
    [("123", 123), ("https://t.me/c/100/456", 456), ("/post/99", 99), ("", None), ("abc", None)],
)
def test_parse_post_link(text, expected):
    assert _parse_post_link(text) == expected


def test_parse_post_product_full():
    post = (
        "Платье вечернее, красное\n"
        "Цена: 5000 ₽\n"
        "Артикул: ABC123\n"
        "В наличии: 4\n"
        "Женская одежда\n"
        "Красивое платье из шёлка"
    )
    name, article, price, category, description, stock = parse_post_product(post)
    assert name == "Платье вечернее, красное"
    assert article == "ABC123"
    assert price == 5000.0
    assert category == "Женская одежда"
    assert stock == 4
    assert "Красивое платье" in description


def test_parse_post_product_empty():
    assert parse_post_product("") == (None, None, 0.0, None, None, None)
