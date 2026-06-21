"""Тесты антифлуд-middleware (защита от флуда/брутфорса)."""

from types import SimpleNamespace

import pytest

import bot.middlewares as mw
from telegram.ext import ApplicationHandlerStop


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def _fake_update(user_id: int):
    msg = _FakeMessage()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=msg,
        callback_query=None,
    )


@pytest.fixture(autouse=True)
def _reset_state():
    mw._hits.clear()
    mw._last_warned.clear()
    # Маленький лимит для детерминированного теста.
    mw.settings.rate_limit_messages = 3
    mw.settings.rate_limit_window = 100.0
    yield
    mw._hits.clear()
    mw._last_warned.clear()


async def test_allows_up_to_limit():
    upd = _fake_update(111)
    for _ in range(3):
        await mw.antiflood_middleware(upd, context=None)  # не бросает


async def test_blocks_over_limit():
    upd = _fake_update(222)
    for _ in range(3):
        await mw.antiflood_middleware(upd, context=None)
    with pytest.raises(ApplicationHandlerStop):
        await mw.antiflood_middleware(upd, context=None)
    assert upd.effective_message.replies  # пользователю показано предупреждение


async def test_admin_not_rate_limited(monkeypatch):
    monkeypatch.setattr(mw, "ADMIN_USER_ID", 999)
    upd = _fake_update(999)
    for _ in range(10):
        await mw.antiflood_middleware(upd, context=None)  # лимит не действует
