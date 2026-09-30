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
    # У репоста может не быть атрибута text ВОВСЕ — раньше здесь падал
    # AttributeError, и комментарий не обрабатывался.
    reply = SimpleNamespace(forward_origin=None, message_id=999)
    msg = SimpleNamespace(reply_to_message=reply, message_thread_id=None)
    assert _resolve_post_id(msg) == "999"


def test_reply_without_text_attribute_does_not_crash():
    """Регрессия: getattr вместо reply.text (медиа-репост без текста)."""
    reply = SimpleNamespace(forward_origin=None, message_id=42)
    assert not hasattr(reply, "text")
    msg = SimpleNamespace(reply_to_message=reply, message_thread_id=None)
    assert _resolve_post_id(msg) == "42"


def test_reply_with_empty_text_falls_back():
    reply = SimpleNamespace(forward_origin=None, message_id=7, text="")
    msg = SimpleNamespace(reply_to_message=reply, message_thread_id=None)
    assert _resolve_post_id(msg) == "7"


def test_link_in_reply_text_extracts_post_id():
    reply = SimpleNamespace(forward_origin=None, message_id=1,
                            text="https://t.me/c/123/456")
    msg = SimpleNamespace(reply_to_message=reply, message_thread_id=None)
    assert _resolve_post_id(msg) == "456"


def test_resolve_post_id_source_has_no_bare_reply_text():
    """В модуле не осталось обращения reply.text без getattr."""
    import inspect
    src = inspect.getsource(_resolve_post_id)
    assert "reply.text" not in src, "снова прямое обращение к reply.text"


def test_uses_thread_id_when_no_reply():
    msg = SimpleNamespace(reply_to_message=None, message_thread_id=777)
    assert _resolve_post_id(msg) == "777"


def test_returns_none_when_nothing():
    msg = SimpleNamespace(reply_to_message=None, message_thread_id=None)
    assert _resolve_post_id(msg) is None
