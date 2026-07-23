"""Unit-тесты для bot.validators — чистые функции валидации ввода."""

import pytest

from bot.validators import (
    MAX_QUANTITY,
    is_valid_article,
    normalize_phone,
    parse_non_negative_int,
    parse_positive_int,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+7 (999) 123-45-67", "+79991234567"),
        ("89991234567", "89991234567"),
        ("  +1234567890  ", "+1234567890"),
    ],
)
def test_normalize_phone_valid(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "123", "abcdefghij", "+", "12345678901234567890"])
def test_normalize_phone_invalid(raw):
    assert normalize_phone(raw) is None


@pytest.mark.parametrize("raw,expected", [("1", 1), (" 3 ", 3), ("100", 100)])
def test_parse_positive_int_valid(raw, expected):
    assert parse_positive_int(raw) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "", None, "1.5", str(MAX_QUANTITY + 1)])
def test_parse_positive_int_invalid(raw):
    assert parse_positive_int(raw) is None


def test_parse_non_negative_int_allows_zero():
    assert parse_non_negative_int("0") == 0
    assert parse_non_negative_int("-1") is None


@pytest.mark.parametrize("raw,ok", [("ABC123", True), ("a_b-1", True), ("", False), ("бук", False), ("x" * 33, False)])
def test_is_valid_article(raw, ok):
    assert is_valid_article(raw) is ok
