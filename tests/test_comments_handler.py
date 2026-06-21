"""Тесты обработчика комментариев: сохранение в БД, WARNING, уведомление админа."""

import datetime as dt
from types import SimpleNamespace

import pytest
from telegram import Chat, MessageOriginChannel

import bot.handlers.posts as posts
from bot.db import Comment


class FakeResult:
    def __init__(self, product):
        self._product = product

    def scalar_one_or_none(self):
        return self._product


class FakeSession:
    def __init__(self, product):
        self._product = product
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        return FakeResult(self._product)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def get(self, model, pk):
        return None


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text=None, **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)


class FakeMessage:
    def __init__(self, text, post_id=555):
        self.chat_id = -100
        self.text = text
        self.from_user = SimpleNamespace(id=42, username="buyer", full_name="Иван")
        origin = MessageOriginChannel(
            date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=-100, type="channel"), message_id=post_id)
        self.reply_to_message = SimpleNamespace(forward_origin=origin, message_id=999)
        self.message_thread_id = None
        self.deleted = False

    async def delete(self):
        self.deleted = True


def _patch(monkeypatch, product):
    async def fake_session():
        yield FakeSession(product)

    async def fake_setting(session, key):
        return "qr_token"

    monkeypatch.setattr(posts, "DISCUSSION_GROUP_ID", -100)
    monkeypatch.setattr(posts, "ADMIN_CHAT_ID", 999)
    monkeypatch.setattr(posts, "get_session", fake_session)
    monkeypatch.setattr(posts, "get_bot_setting", fake_setting)


async def test_comment_saved_and_admin_notified(monkeypatch):
    product = SimpleNamespace(id=7, name="Платье", price=1000.0, stock=5, is_active=True)
    session_holder = {}

    async def fake_session():
        s = FakeSession(product)
        session_holder["s"] = s
        yield s

    _patch(monkeypatch, product)
    monkeypatch.setattr(posts, "get_session", fake_session)

    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot)
    msg = FakeMessage("Отличная вещь, беру!")  # без количества → не заказ
    await posts.handle_comment(SimpleNamespace(message=msg), ctx)

    saved = [o for o in session_holder["s"].added if isinstance(o, Comment)]
    assert saved and saved[0].product_id == 7 and saved[0].user_id == 42
    # Админ уведомлён, текст содержит название товара.
    assert bot.sent and bot.sent[0][0] == 999 and "Платье" in bot.sent[0][1]


async def test_no_product_warns_and_skips(monkeypatch, caplog):
    _patch(monkeypatch, None)  # товар не найден
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot)
    msg = FakeMessage("3")
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.handlers.posts"):
        await posts.handle_comment(SimpleNamespace(message=msg), ctx)
    assert any("no product for post_id" in r.message for r in caplog.records)
    assert bot.sent == []          # админа не уведомляем
    assert msg.deleted is False    # сообщение не трогаем
