"""Тесты привязки комментария к товару (bot.handlers.posts._resolve_post_id).

Главный баг: в группе обсуждения комментарий — это reply на АВТО-РЕПОСТ поста
канала; id этого репоста ≠ id поста в канале (Product.post_id). Правильный id
лежит в forward_origin (MessageOriginChannel.message_id).
"""

import datetime as dt
from types import SimpleNamespace

from telegram import Chat, MessageOriginChannel

from bot.handlers.posts import _resolve_post_id


def _channel_origin(message_id: int) -> MessageOriginChannel:
    return MessageOriginChannel(
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=-1001234567890, type="channel"),
        message_id=message_id,
    )


def test_uses_channel_origin_message_id():
    reply = SimpleNamespace(forward_origin=_channel_origin(555), message_id=999)
    msg = SimpleNamespace(reply_to_message=reply, message_thread_id=None)
    # Берём id поста КАНАЛА (555), а не id репоста в группе (999).
    assert _resolve_post_id(msg) == "555"


def test_falls_back_to_reply_message_id_without_origin():
    reply = SimpleNamespace(forward_origin=None, message_id=999)
    msg = SimpleNamespace(reply_to_message=reply, message_thread_id=None)
    assert _resolve_post_id(msg) == "999"


def test_uses_thread_id_when_no_reply():
    msg = SimpleNamespace(reply_to_message=None, message_thread_id=777)
    assert _resolve_post_id(msg) == "777"


def test_returns_none_when_nothing():
    msg = SimpleNamespace(reply_to_message=None, message_thread_id=None)
    assert _resolve_post_id(msg) is None
