"""Тесты чистых хелперов каталога (крошки, пагинация, подписи карточек)."""

from types import SimpleNamespace

from bot.handlers.catalog import (
    _crumbs,
    _pagination_row,
    _product_caption,
    _subcategory_of,
)


def _product(**kw):
    base = dict(name="Товар", price=1990.0, article=None, stock=None,
                description=None, photo_file_ids=None, video_file_ids=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_crumbs_full_path_bolds_last_and_escapes():
    assert _crumbs("Электроника", "Смартфоны") == \
        "🏠 Главная → 📦 Электроника → <b>📱 Смартфоны</b>"


def test_crumbs_only_root():
    assert _crumbs() == "<b>🏠 Главная</b>"


def test_crumbs_escapes_html():
    assert _crumbs(category="A&B") == "🏠 Главная → <b>📦 A&amp;B</b>"


def test_pagination_row_middle_has_both():
    row = _pagination_row(1, 3, "catalog:prodpage")[0]
    assert [b.callback_data for b in row] == ["catalog:prodpage:0", "catalog:prodpage:2"]


def test_pagination_row_single_page_empty():
    assert _pagination_row(0, 1, "x") == []


def test_subcategory_of_splits_on_comma():
    assert _subcategory_of(_product(name="Платье, красное, M")) == "Платье"
    assert _subcategory_of(_product(name="Рубашка")) == "Рубашка"


def test_product_caption_html_escapes_and_has_price():
    cap = _product_caption(_product(name="Куртка <Pro>", price=5000.0,
                                    article="A-1", stock=3, description="100% хлопок"))
    assert "<b>Куртка &lt;Pro&gt;</b>" in cap
    assert "Цена: <b>5000 ₽</b>" in cap
    assert "A-1" in cap and "На складе: 3" in cap and "100% хлопок" in cap
