"""Интеграционные тесты хэндлеров каталога с фейковыми Telegram-объектами.

Проверяют реальный путь выполнения (без сети/БД): категории рендерятся
редактированием на месте, товары отправляются медиа-карточками + одно
навигационное сообщение, пустая категория показывает заглушку.
"""

from types import SimpleNamespace

import pytest

import bot.handlers.catalog as catalog


# --------------------------- фейки Telegram ---------------------------

class FakeMessage:
    def __init__(self, chat_id=1, message_id=10):
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = "main menu"
        self.caption = None
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data, message=None, user_id=123):
        self.data = data
        self.message = message or FakeMessage()
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.edited = None

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edited = (text, reply_markup)
        self.message.text = text


class FakeBot:
    def __init__(self):
        self.sent = []      # (kind, text/caption, reply_markup)
        self.deleted = []   # message_id
        self._mid = 1000

    def _next(self):
        self._mid += 1
        return self._mid

    async def send_message(self, chat_id, text=None, reply_markup=None, parse_mode=None):
        self.sent.append(("message", text, reply_markup))
        return SimpleNamespace(message_id=self._next())

    async def send_photo(self, chat_id, photo=None, caption=None, reply_markup=None, parse_mode=None):
        self.sent.append(("photo", caption, reply_markup))
        return SimpleNamespace(message_id=self._next())

    async def send_video(self, chat_id, video=None, caption=None, reply_markup=None, parse_mode=None):
        self.sent.append(("video", caption, reply_markup))
        return SimpleNamespace(message_id=self._next())

    async def send_media_group(self, chat_id, media=None):
        return [SimpleNamespace(message_id=self._next()) for _ in (media or [])]

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


class FakeUpdate:
    def __init__(self, query):
        self.callback_query = query


def _product(pid, name, category, **kw):
    base = dict(id=pid, name=name, category=category, price=1990.0, article=None,
                stock=5, description=None, photo_file_ids=None, video_file_ids=None,
                is_active=True)
    base.update(kw)
    return SimpleNamespace(**base)


PRODUCTS = [
    _product(1, "Платье, красное", "Одежда"),
    _product(2, "Платье, синее", "Одежда"),
    _product(3, "Кроссовки", "Обувь", photo_file_ids="file123"),
]


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture
def context():
    return SimpleNamespace(user_data={}, bot=FakeBot())


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    async def fake_session():
        yield object()

    async def fake_all(session):
        return PRODUCTS

    async def fake_in_cat(session, category):
        return [p for p in PRODUCTS if p.category == category]

    monkeypatch.setattr(catalog, "get_session", fake_session)
    monkeypatch.setattr(catalog, "get_all_active_products", fake_all)
    monkeypatch.setattr(catalog, "get_active_products_in_category", fake_in_cat)


# ------------------------------- тесты --------------------------------

async def test_categories_render_with_counts(context):
    query = FakeQuery("catalog:show")
    await catalog.catalog_show_categories(FakeUpdate(query), context)
    _, markup = query.edited
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Одежда (2)" in lbl for lbl in labels)
    assert any("Обувь (1)" in lbl for lbl in labels)
    assert "🏠 Главная" in labels


async def test_products_page_sends_cards_and_nav(context):
    context.user_data["catalog_current_cat"] = "Одежда"
    query = FakeQuery("catalog:ss:0")
    await catalog.show_products_page(query, context, page=0)

    # 2 карточки товаров + 1 навигационное сообщение.
    assert len(context.bot.sent) == 3
    assert len(context.user_data["catalog_product_msgs"]) == 2
    assert "catalog_nav_msg_id" in context.user_data

    # Карточки несут кнопку «Заказать» с привязкой к товару.
    card_callbacks = _callbacks(context.bot.sent[0][2]) + _callbacks(context.bot.sent[1][2])
    assert "order:start:1" in card_callbacks and "order:start:2" in card_callbacks

    # Навигация: счётчик и крошки.
    nav_kind, nav_text, _ = context.bot.sent[-1]
    assert "Найдено 2 товаров" in nav_text and "Страница 1 из 1" in nav_text
    assert "Одежда" in nav_text
    # Сообщение-источник (список подкатегорий) удалено — дублей нет.
    assert query.message.deleted is True


async def test_products_page_with_photo_uses_send_photo(context):
    context.user_data["catalog_current_cat"] = "Обувь"
    query = FakeQuery("catalog:ss:0")
    await catalog.show_products_page(query, context, page=0)
    kinds = [s[0] for s in context.bot.sent]
    assert "photo" in kinds      # карточка с фото
    assert kinds[-1] == "message"  # навигация — текст


async def test_empty_category_shows_placeholder(context):
    context.user_data["catalog_current_cat"] = "Пусто"
    query = FakeQuery("catalog:ss:0")
    await catalog.show_products_page(query, context, page=0)
    # Одно сообщение-заглушка, карточек нет.
    assert len(context.bot.sent) == 1
    _, text, _ = context.bot.sent[0]
    assert "😔" in text
    assert "catalog_product_msgs" not in context.user_data
